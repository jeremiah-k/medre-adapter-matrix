"""D2 docker qualification: encrypted outbound, restart identity, interrupted-send trichotomy.

Five scenarios on a real Synapse homeserver with real Megolm crypto,
driving the durable production shape (durable admission, durable outbox, retry
worker, real Matrix adapter) rather than direct adapter calls:

* Routed outbound: an independently controlled recipient client (its own
  account, device and nio store) decrypts a pipeline-routed encrypted message
  and verifies intended content, conversation scope (room) and relation scope
  (``m.in_reply_to`` bound to the parent's native event ID).
* Restart identity: in-process session reopen on durable state preserves the device
  identity (olm account keys), the durable sync consumer binding, and the
  application checkpoint stream, each tied to the contract that requires it.
* Interrupted send trichotomy: a server-refused submission (M_FORBIDDEN) is
  proven-unsent; a response-cut transport interruption is honestly uncertain
  (the homeserver actually accepted the send while the client could never
  know); recovery after restart reuses the same deterministic transaction
  identity and the homeserver deduplicates — the recipient observes exactly
  one copy.
* Native cooldown and unavailable server: a real Synapse rate limit response
  (M_LIMIT_EXCEEDED) drives the adapter cooldown and the durable retry
  schedule; a stopped homeserver yields a bounded unavailable-server case
  while the durable outbox and adapter diagnostics report the actual known
  work. No fake retry stands in for native server behavior.

Evidence classification: ``docker_encrypted_room_qualification``. Synapse
databases, homeserver logs, device keys and tokens live in the container data
directory and pytest tmp dirs and are never committed; verdict JSON files are
sanitized (redacted device and event IDs).

Running locally::

    .venv/bin/python -m pytest tests/test_matrix_d2_qualification_docker.py \\
        -m docker -v

Requires Docker and ``mindroom-nio[e2e]`` (``HAS_E2EE``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any
from uuid import uuid4

import pytest
from medre.config.model import RetryConfig
from medre.core.contracts.adapter import (
    AdapterContext,
    AdapterSendError,
)
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.events.bus import EventBus
from medre.core.events.canonical import EventRelation
from medre.core.ingress import DurableIngressWorker
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.planning.delivery_plan import RetryPolicy
from medre.core.rendering.renderer import RenderingPipeline, RenderingResult
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.core.supervision.accounting import RuntimeAccounting
from medre.runtime.retry import RetryWorker
from medre_matrix_tests.helpers.async_utils import wait_until
from medre_matrix_tests.helpers.d2_proxy import SendResponseCutProxy

from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.compat import HAS_E2EE
from medre.adapters.matrix.durable_sync import (
    _durable_database_name,
    _existing_consumer_id,
)
from medre.adapters.matrix.outbound import MatrixOutboundOperation
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.adapters.matrix.session import MatrixSession
from medre.config.adapters.matrix import MatrixConfig

pytestmark = pytest.mark.docker

logger = logging.getLogger("medre.matrix.d2")

_SYNAPSE_IMAGE = "matrixdotorg/synapse:v1.164.2"
_SERVER_NAME = "medre-d2.local"
_CONTAINER = "medre-d2-synapse"
_RL_CONTAINER = "medre-d2-ratelimit-synapse"
_READY_TIMEOUT = 180

_BOT_LOCALPART = "d2-bot"
_BOT_PASSWORD = "d2-bot-password"
_PEER_LOCALPART = "d2-peer"
_PEER_PASSWORD = "d2-peer-password"
_RL_BOT_LOCALPART = "d2rl-bot"
_RL_BOT_PASSWORD = "d2rl-bot-password"
_RL_PEER_LOCALPART = "d2rl-peer"
_RL_PEER_PASSWORD = "d2rl-peer-password"

_SOURCE_ADAPTER = "d2-source"
_SOURCE_CHANNEL = "ch-0"


# ---------------------------------------------------------------------------
# Docker bootstrap (same shape as the device-trust qualification harness)
# ---------------------------------------------------------------------------


def _docker(*args: str, timeout: int = 120) -> str:
    result = subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
    return result.stdout


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        _docker("info", "--format", "{{.ServerVersion}}", timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _synapse_image_digest() -> str:
    with contextlib.suppress(subprocess.SubprocessError, OSError, IndexError):
        return _docker(
            "image",
            "inspect",
            "--format",
            "{{index .RepoDigests 0}}",
            _SYNAPSE_IMAGE,
            timeout=15,
        ).strip()
    return "<unavailable>"


def _http_json(
    url: str,
    payload: dict[str, Any] | None = None,
    *,
    token: str | None = None,
    method: str = "GET",
    timeout: int = 20,
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    # The rate-limit scenario's homeserver throttles the boot's own room
    # setup; retry a bounded number of times on the server's retry-after
    # hint instead of treating the scenario's own limit as boot failure.
    for attempt in range(5):
        try:
            # URL schemes come from the harness's own localhost fixture.
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode()
            exc.close()
            if exc.code != 429 or attempt == 4:
                raise RuntimeError(
                    f"HTTP {exc.code} for {method} {url}: {body}"
                ) from exc
            delay = 0.5
            try:
                hinted = json.loads(body).get("retry_after_ms")
                if hinted is not None:
                    delay = float(hinted) / 1000.0
            except (ValueError, TypeError):
                pass
            time.sleep(max(0.05, min(delay, 5.0)))
    raise RuntimeError(f"unreachable: retries exhausted for {method} {url}")


def _wait_ready(base_url: str, timeout: int = _READY_TIMEOUT) -> None:
    """Bounded readiness wait on the versions endpoint (service bootstrap)."""
    monotonic_deadline = time.monotonic() + timeout
    while time.monotonic() < monotonic_deadline:
        try:
            _http_json(f"{base_url}/_matrix/client/versions", timeout=3)
            return
        except (RuntimeError, OSError):
            time.sleep(1.0)
    raise RuntimeError(f"Synapse did not become ready within {timeout}s")


def _login(base_url: str, localpart: str, password: str) -> dict[str, Any]:
    return _http_json(
        f"{base_url}/_matrix/client/v3/login",
        {"type": "m.login.password", "user": localpart, "password": password},
        method="POST",
    )


@dataclass
class SynapseEnv:
    """Connection details for one running qualification homeserver."""

    base_url: str
    container: str
    bot_user_id: str
    bot_access_token: str
    bot_device_id: str
    peer_user_id: str
    peer_access_token: str
    peer_device_id: str
    encrypted_room_id: str
    plain_room_id: str


def _clear_root_owned(data_dir: Path) -> None:
    """Clear a root-owned Synapse data directory using a throwaway container."""
    _docker(
        "run",
        "--rm",
        "--user",
        "root",
        "--entrypoint",
        "",
        "-v",
        f"{data_dir}:/data",
        _SYNAPSE_IMAGE,
        "find",
        "/data",
        "-mindepth",
        "1",
        "-delete",
        timeout=90,
    )


def _boot_synapse(
    container: str,
    *,
    bot_localpart: str,
    bot_password: str,
    peer_localpart: str,
    peer_password: str,
    rc_message: dict[str, float] | None,
) -> Any:
    """Boot one Synapse container, two users and the scenario rooms."""
    container = f"{container}-{uuid4().hex[:12]}"
    parent = Path(tempfile.mkdtemp(prefix="medre-d2-"))
    data_dir = parent / "synapse"
    data_dir.mkdir(mode=0o700)
    # Own all resources before running the first potentially failing command.
    uid = os.getuid() or 991
    gid = os.getgid() if os.getuid() else 991
    identity = f"{uid}:{gid}"
    try:
        _docker(
            "run",
            "--rm",
            "-e",
            f"SYNAPSE_SERVER_NAME={_SERVER_NAME}",
            "-e",
            "SYNAPSE_REPORT_STATS=no",
            "-v",
            f"{data_dir}:/data",
            _SYNAPSE_IMAGE,
            "generate",
            timeout=180,
        )
        _docker(
            "run",
            "--rm",
            "--user",
            "root",
            "--entrypoint",
            "",
            "-v",
            f"{data_dir}:/data",
            _SYNAPSE_IMAGE,
            "chown",
            "-R",
            identity,
            "/data",
            timeout=90,
        )

        _docker(
            "run",
            "--rm",
            "--user",
            "root",
            "--entrypoint",
            "",
            "-v",
            f"{data_dir}:/data",
            _SYNAPSE_IMAGE,
            "chmod",
            "-R",
            "u=rwX,go=",
            "/data",
            timeout=90,
        )

        homeserver_yaml = data_dir / "homeserver.yaml"
        with open(homeserver_yaml, "a", encoding="utf-8") as fh:
            fh.write("\n# medre d2 qualification overrides\n")
            fh.write("enable_registration: true\n")
            fh.write("enable_registration_without_verification: true\n")
            fh.write("rc_login:\n")
            fh.write("  account:\n    per_second: 5\n    burst_count: 100\n")
            fh.write("  address:\n    per_second: 5\n    burst_count: 100\n")
            if rc_message is not None:
                # Server-directed rate-limit scenario: a deliberately tiny,
                # slow-refilling message bucket makes the homeserver itself emit
                # M_LIMIT_EXCEEDED for the send that follows the first within the
                # refill window, regardless of observation timing in between.
                fh.write("rc_message:\n")
                fh.write(f"  per_second: {rc_message['per_second']}\n")
                fh.write(f"  burst_count: {rc_message['burst_count']}\n")

        _docker(
            "run",
            "-d",
            "--name",
            container,
            "--user",
            identity,
            "-e",
            f"SYNAPSE_SERVER_NAME={_SERVER_NAME}",
            "-e",
            "SYNAPSE_REPORT_STATS=no",
            "-p",
            "127.0.0.1::8008",
            "-v",
            f"{data_dir}:/data",
            _SYNAPSE_IMAGE,
            timeout=180,
        )
        base_url = (
            "http://" + _docker("port", container, "8008/tcp", timeout=15).strip()
        )
        assert urlsplit(base_url).hostname == "127.0.0.1"
        _wait_ready(base_url)

        for localpart, password in (
            (bot_localpart, bot_password),
            (peer_localpart, peer_password),
        ):
            _docker(
                "exec",
                container,
                "register_new_matrix_user",
                "-u",
                localpart,
                "-p",
                password,
                "--no-admin",
                "-c",
                "/data/homeserver.yaml",
                "http://localhost:8008",
                timeout=90,
            )

        bot_login = _login(base_url, bot_localpart, bot_password)
        peer_login = _login(base_url, peer_localpart, peer_password)

        encrypted_room = _http_json(
            f"{base_url}/_matrix/client/v3/createRoom",
            {"name": "MEDRE D2 encrypted", "preset": "public_chat"},
            token=bot_login["access_token"],
            method="POST",
        )
        encrypted_room_id = encrypted_room["room_id"]
        _http_json(
            f"{base_url}/_matrix/client/v3/join/{encrypted_room_id}",
            {},
            token=peer_login["access_token"],
            method="POST",
        )
        _http_json(
            f"{base_url}/_matrix/client/v3/rooms/{encrypted_room_id}"
            "/state/m.room.encryption",
            {"algorithm": "m.megolm.v1.aes-sha2"},
            token=bot_login["access_token"],
            method="PUT",
        )

        # Peer-owned plaintext room for the server-directed refusal scenario;
        # the bot's membership and send-permission setup lives in that
        # scenario, never in this shared boot helper.
        plain_room = _http_json(
            f"{base_url}/_matrix/client/v3/createRoom",
            {"name": "MEDRE D2 peer-only", "preset": "public_chat"},
            token=peer_login["access_token"],
            method="POST",
        )

        env = SynapseEnv(
            base_url=base_url,
            container=container,
            bot_user_id=bot_login["user_id"],
            bot_access_token=bot_login["access_token"],
            bot_device_id=bot_login.get("device_id", ""),
            peer_user_id=peer_login["user_id"],
            peer_access_token=peer_login["access_token"],
            peer_device_id=peer_login.get("device_id", ""),
            encrypted_room_id=encrypted_room_id,
            plain_room_id=plain_room["room_id"],
        )
        logger.info(
            "D2 Synapse ready: container=%s bot=%s peer=%s room=%s",
            container,
            env.bot_user_id,
            env.peer_user_id,
            env.encrypted_room_id,
        )
        yield env
    finally:
        with contextlib.suppress(subprocess.SubprocessError, OSError):
            _docker("rm", "-f", container, timeout=90)
        try:
            shutil.rmtree(data_dir)
        except PermissionError:
            _clear_root_owned(data_dir)
            shutil.rmtree(data_dir)
        parent.rmdir()


@pytest.fixture
def d2_env() -> Any:
    """Main qualification homeserver with ordinary rate limits."""
    if not _docker_available():
        pytest.skip("Docker is not available")
    if not HAS_E2EE:
        pytest.skip("mindroom-nio[e2e] is required for the D2 encrypted qualification")
    yield from _boot_synapse(
        _CONTAINER,
        bot_localpart=_BOT_LOCALPART,
        bot_password=_BOT_PASSWORD,
        peer_localpart=_PEER_LOCALPART,
        peer_password=_PEER_PASSWORD,
        rc_message=None,
    )


@pytest.fixture
def d2_rate_limit_env() -> Any:
    """Homeserver whose message bucket emits genuine M_LIMIT_EXCEEDED."""
    if not _docker_available():
        pytest.skip("Docker is not available")
    if not HAS_E2EE:
        pytest.skip("mindroom-nio[e2e] is required for the D2 encrypted qualification")
    yield from _boot_synapse(
        _RL_CONTAINER,
        bot_localpart=_RL_BOT_LOCALPART,
        bot_password=_RL_BOT_PASSWORD,
        peer_localpart=_RL_PEER_LOCALPART,
        peer_password=_RL_PEER_PASSWORD,
        rc_message={"per_second": 0.05, "burst_count": 1},
    )


# ---------------------------------------------------------------------------
# Recipient client (independently controlled) and observation helpers
# ---------------------------------------------------------------------------


def _redact(value: str | None) -> str:
    if not value:
        return "<none>"
    if len(value) <= 4:
        return "****"
    return value[:2] + "****" + value[-2:]


async def _init_peer_client(
    env: SynapseEnv, localpart: str, password: str, store_path: str
) -> Any:
    """Create the recipient nio client with genuine Megolm crypto enabled."""
    import nio

    peer_login = _login(env.base_url, localpart, password)
    client = nio.AsyncClient(
        homeserver=env.base_url,
        user=peer_login["user_id"],
        device_id=peer_login["device_id"],
        store_path=store_path,
        config=nio.AsyncClientConfig(
            encryption_enabled=True,
            max_timeouts=0,
            max_limit_exceeded=0,
            request_timeout=10,
        ),
    )
    try:
        client.restore_login(
            user_id=peer_login["user_id"],
            device_id=peer_login["device_id"],
            access_token=peer_login["access_token"],
        )
        initial = await asyncio.wait_for(client.sync(full_state=True), 20)
        assert isinstance(initial, nio.SyncResponse), "recipient initial sync failed"
        if client.should_upload_keys:
            await client.keys_upload()
        if client.should_query_keys:
            await client.keys_query()
        if client.should_claim_keys:
            with contextlib.suppress(Exception):
                users = client.get_users_for_key_claiming()
                if users:
                    await client.keys_claim(users)
        await client.send_to_device_messages()
    except BaseException:
        await client.close()
        raise
    return client


async def _peer_timeline(
    peer_client: Any, room_id: str, attempts: int = 6
) -> list[tuple[str, dict[str, Any]]]:
    """Sync the recipient and return decrypted (event_id, content) pairs.

    RoomMessageText also represents plaintext. Only the pinned SDK's
    explicit decrypted flag qualifies receipt of encrypted content. Track
    other sender message IDs separately to expose undecryptable duplicates.

    Syncs are incremental: every decrypted event is appended to a cumulative
    ``_d2_seen`` store on the client, because an event that arrives while an
    earlier search is polling will never reappear in a later sync window.
    """
    seen: dict[str, dict[str, Any]] = getattr(peer_client, "_d2_seen", None) or {}
    peer_client._d2_seen = seen
    collected: list[tuple[str, dict[str, Any]]] = []
    for _ in range(attempts):
        response = await asyncio.wait_for(peer_client.sync(timeout=3000), 15)
        if getattr(response, "next_batch", None) is None:
            raise AssertionError("recipient sync failed; no observation made")
        join = getattr(getattr(response, "rooms", None), "join", None) or {}
        info = join.get(room_id)
        timeline = getattr(info, "timeline", None) if info is not None else None
        if getattr(timeline, "limited", False):
            raise AssertionError(
                "recipient timeline is limited; observation is incomplete"
            )
        for event in getattr(timeline, "events", None) or []:
            sender = getattr(event, "sender", None)
            event_id = getattr(event, "event_id", "")
            if (
                event_id
                and sender
                and (
                    type(event).__name__ in {"RoomMessageText", "MegolmEvent"}
                    or getattr(event, "decrypted", False)
                )
            ):
                observed = getattr(peer_client, "_d2_message_events", {})
                observed[event_id] = sender
                peer_client._d2_message_events = observed
            if (
                type(event).__name__ == "RoomMessageText"
                and getattr(event, "decrypted", False) is True
            ):
                source = getattr(event, "source", None) or {}
                content = source.get("content") if isinstance(source, dict) else None
                if not isinstance(content, dict):
                    content = {"body": getattr(event, "body", "")}
                event_id = getattr(event, "event_id", "")
                collected.append((event_id, content))
                seen[event_id] = content
    return collected


async def _peer_find_body(
    peer_client: Any, room_id: str, body: str, attempts: int = 8
) -> list[str]:
    """Recipient-side observation: native event IDs whose decrypted body matches."""
    for _ in range(attempts):
        await _peer_timeline(peer_client, room_id, 1)
        seen = peer_client._d2_seen
        matches = [
            event_id
            for event_id, content in seen.items()
            if content.get("body") == body
        ]
        if matches:
            return matches
    return []


async def _await_bot_sees_peer_device(rig: D2Rig, peer_user_id: str) -> None:
    """Wait until the adapter's device store holds the recipient device keys."""

    def _peer_known() -> bool:
        session = rig.adapter._session
        client = getattr(session, "_client", None)
        device_store = getattr(client, "device_store", None)
        if device_store is None:
            return False
        try:
            return bool(list(device_store.active_user_devices(peer_user_id)))
        except Exception:
            return False

    assert await wait_until(
        _peer_known, timeout=60.0, interval=0.5
    ), "bot device store never learned the peer device keys"


async def _await_olm_session_with_peer(rig: D2Rig, peer_user_id: str) -> None:
    """Wait until the bot holds an established Olm session with the peer device.

    The pinned SDK forks a fresh Megolm outbound session on every room send
    and silently skips key shares to devices without an Olm session, which
    would make the send undecryptable for the recipient. Establishing the
    Olm session first is a readiness gate, not a behavior change.
    """

    def _olm_ready() -> bool:
        session = rig.adapter._session
        client = getattr(session, "_client", None)
        device_store = getattr(client, "device_store", None)
        session_store = getattr(getattr(client, "olm", None), "session_store", None)
        if device_store is None or session_store is None:
            return False
        try:
            for device in device_store.active_user_devices(peer_user_id):
                key = getattr(device, "curve25519", None)
                if key and list(session_store.get(key)):
                    return True
        except Exception:
            return False
        return False

    assert await wait_until(
        _olm_ready, timeout=90.0, interval=0.5
    ), "bot never established an Olm session with the peer device"


def _write_verdict(tmp_path: Path, name: str, verdict: dict[str, Any]) -> None:
    """Persist a sanitized verdict summary beside the other pytest evidence."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / name).write_text(json.dumps(verdict, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Response-cut proxy: deterministic transport interruption below the SDK
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Durable production-shape rig: admission, outbox, retry worker, adapter
# ---------------------------------------------------------------------------


def _make_pipeline_config(
    storage: SQLiteStorage,
    target_room_id: str,
    target_adapter_id: str,
    matrix_adapter: MatrixAdapter,
) -> PipelineConfig:
    rendering = RenderingPipeline()
    rendering.register(MatrixRenderer(), priority=50)
    rendering.register(TextRenderer(), priority=100)
    route = Route(
        id="d2-qual-route",
        source=RouteSource(
            adapter=_SOURCE_ADAPTER,
            event_kinds=("message.created",),
            channel=_SOURCE_CHANNEL,
        ),
        targets=[RouteTarget(adapter=target_adapter_id, channel=target_room_id)],
    )
    return PipelineConfig(
        storage=storage,  # type: ignore[arg-type]
        router=Router(routes=[route]),
        fallback_resolver=FallbackResolver(),
        relation_resolver=RelationResolver(storage=storage),
        adapters={target_adapter_id: matrix_adapter},
        rendering_pipeline=rendering,
        runtime_accounting=RuntimeAccounting(),
        event_bus=EventBus(),
        # Without a route retry policy the pipeline dead-letters the first
        # transient delivery failure instead of scheduling a durable retry,
        # and the uncertain-send phases would have no retained work to
        # recover. Deterministic backoff keeps the observation windows tight.
        route_retry_policies={route.id: RetryPolicy(jitter=False)},
    )


def _make_ctx(
    adapter_id: str, runner: PipelineRunner, storage: SQLiteStorage
) -> AdapterContext:
    async def _publish(event: Any) -> None:
        await runner.handle_ingress(event)

    async def _admit(event: Any, provenance: Any, attachment: Any = None) -> Any:
        return await runner.admit_ingress(event, provenance, attachment=attachment)

    async def _load_checkpoint(stream: str) -> Any:
        return await storage.get_adapter_checkpoint(adapter_id, stream)

    async def _commit_checkpoint(stream: str, cursor: str, metadata_json: str) -> None:
        await storage.put_adapter_checkpoint(
            adapter_id, stream, cursor, metadata_json=metadata_json
        )

    return AdapterContext(
        adapter_id=adapter_id,
        publish_inbound=_publish,
        admit_inbound=_admit,
        load_checkpoint=_load_checkpoint,
        commit_checkpoint=_commit_checkpoint,
        logger=logging.getLogger(f"test.d2.{adapter_id}"),
        clock=lambda: datetime.now(UTC),
        shutdown_event=asyncio.Event(),
    )


@dataclass
class D2Rig:
    """One started production-shape stack around one real MatrixAdapter."""

    adapter: MatrixAdapter
    runner: PipelineRunner
    worker: DurableIngressWorker
    retry_worker: RetryWorker | None
    storage: SQLiteStorage
    db_path: str
    adapter_id: str
    store_path: str
    target_room_id: str
    homeserver_url: str
    retired: bool = False


def _make_matrix_config(
    env: SynapseEnv,
    *,
    adapter_id: str,
    store_path: str,
    homeserver: str | None,
    room_ids: tuple[str, ...],
    verification_policy: str | None,
    encryption_mode: str = "e2ee_required",
    auto_join_rooms: tuple[str, ...] | None = None,
    bot_access_token: str | None = None,
    bot_device_id: str | None = None,
) -> MatrixConfig:
    if auto_join_rooms is None:
        auto_join_rooms = room_ids
    kwargs: dict[str, Any] = {
        "adapter_id": adapter_id,
        "homeserver": homeserver or env.base_url,
        "user_id": env.bot_user_id,
        "access_token": bot_access_token or env.bot_access_token,
        "device_id": bot_device_id or env.bot_device_id,
        "room_allowlist": set(room_ids) if room_ids else None,
        "encryption_mode": encryption_mode,
        "auto_join_rooms": auto_join_rooms,
        "store_path": store_path,
    }
    if verification_policy is not None:
        kwargs["verification_policy"] = verification_policy
    return MatrixConfig(**kwargs).validate()


def _start_retry_worker(
    rig_storage: SQLiteStorage,
    runner: PipelineRunner,
    target_adapter_id: str,
    *,
    enabled: bool,
    initial_dispatch: bool,
    interval_seconds: float = 0.5,
    max_attempts: int = 30,
) -> RetryWorker:
    return RetryWorker(
        rig_storage,
        runner,
        None,
        retry_config=RetryConfig(
            enabled=enabled,
            interval_seconds=interval_seconds,
            max_attempts=max_attempts,
        ),
        enabled=enabled,
        initial_dispatch=initial_dispatch,
        interval_seconds=interval_seconds,
        max_attempts=max_attempts,
        available_target_adapters={target_adapter_id},
    )


async def _start_rig(
    env: SynapseEnv,
    *,
    adapter_id: str,
    store_path: str,
    db_path: str,
    target_room_id: str | None = None,
    homeserver: str | None = None,
    verification_policy: str | None = None,
    encryption_mode: str = "e2ee_required",
    auto_join_rooms: tuple[str, ...] | None = None,
    with_retry_worker: bool = True,
    retry_interval_seconds: float = 0.5,
    bot_access_token: str | None = None,
    bot_device_id: str | None = None,
) -> D2Rig:
    """Start one durable production-shape rig on real durable state.

    ``target_room_id`` defaults to the encrypted qualification room. Each rig
    owns its own nio store and MEDRE database; adapters never share one device
    identity across stores — a rig on a fresh store passes ``bot_device_id``
    from its own login. The adapter is fully started before the retry worker
    starts so a retained row claimed at startup can never meet an
    uninitialized session.
    """
    room_id = target_room_id or env.encrypted_room_id
    async with contextlib.AsyncExitStack() as resources:
        storage = SQLiteStorage(db_path=db_path)
        resources.push_async_callback(storage.close)
        await storage.initialize()
        config = _make_matrix_config(
            env,
            adapter_id=adapter_id,
            store_path=store_path,
            homeserver=homeserver,
            room_ids=(room_id,),
            verification_policy=verification_policy,
            encryption_mode=encryption_mode,
            auto_join_rooms=auto_join_rooms,
            bot_access_token=bot_access_token,
            bot_device_id=bot_device_id,
        )
        adapter = MatrixAdapter(config)
        runner = PipelineRunner(
            _make_pipeline_config(storage, room_id, adapter_id, adapter)
        )
        worker = DurableIngressWorker(storage=storage, pipeline=runner)
        retry_worker: RetryWorker | None = None
        if with_retry_worker:
            retry_worker = _start_retry_worker(
                storage,
                runner,
                adapter_id,
                enabled=True,
                initial_dispatch=True,
                interval_seconds=retry_interval_seconds,
            )
        resources.push_async_callback(runner.stop)
        await runner.start()
        resources.push_async_callback(adapter.stop)
        resources.push_async_callback(worker.stop, grace_seconds=5.0)
        await worker.start()
        await adapter.start(_make_ctx(adapter_id, runner, storage))
        if retry_worker is not None:
            resources.push_async_callback(retry_worker.stop)
            await retry_worker.start()
        rig = D2Rig(
            adapter=adapter,
            runner=runner,
            worker=worker,
            retry_worker=retry_worker,
            storage=storage,
            db_path=db_path,
            adapter_id=adapter_id,
            store_path=store_path,
            target_room_id=room_id,
            homeserver_url=homeserver or env.base_url,
        )
        resources.pop_all()
        return rig


async def _stop_retry_worker(rig: D2Rig) -> None:
    if rig.retry_worker is not None:
        await rig.retry_worker.stop()
        assert not rig.retry_worker.has_retained_work
        rig.retry_worker = None


@pytest.fixture
async def d2_resources():
    async with contextlib.AsyncExitStack() as resources:
        yield resources


async def _managed_rig(
    resources: contextlib.AsyncExitStack, *args: Any, **kwargs: Any
) -> D2Rig:
    rig = await _start_rig(*args, **kwargs)
    resources.push_async_callback(_stop_rig, rig)
    return rig


async def _stop_rig(rig: D2Rig) -> None:
    """Tear one rig down via the production stop paths.

    The nio store close must precede any store reopen by a restart phase.
    """
    if rig.retired:
        return
    rig.retired = True
    # End ingress and dispatch before retiring the native adapter. Do not
    # force-close the SDK's database on a successful restart path: that would
    # mask a production ownership/close defect in the qualification evidence.
    async with contextlib.AsyncExitStack() as resources:
        resources.push_async_callback(rig.storage.close)
        resources.push_async_callback(rig.runner.stop)
        resources.push_async_callback(rig.adapter.stop)
        resources.push_async_callback(_stop_retry_worker, rig)
        resources.push_async_callback(rig.worker.stop, grace_seconds=5.0)


async def _admit_message(
    rig: D2Rig,
    event_id: str,
    text: str,
    *,
    relations: tuple[EventRelation, ...] = (),
) -> CanonicalEvent:
    """Admit one message.created event through durable ingress."""
    event = CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(UTC),
        source_adapter=_SOURCE_ADAPTER,
        source_transport_id="node-1",
        source_channel_id=_SOURCE_CHANNEL,
        parent_event_id=None,
        lineage=(),
        relations=relations,
        payload={"text": text},
        metadata=EventMetadata(),
    )
    await rig.runner.admit_ingress(event, "live")
    return event


async def _outbox_rows(rig: D2Rig, event_id: str) -> list[Any]:
    return await rig.storage.list_outbox_items_for_event(event_id)


async def _await_outbox_terminal(rig: D2Rig, event_id: str, status: str) -> Any:
    """Wait for every outbox row of *event_id* to reach *status*."""

    async def _condition() -> bool:
        rows = await _outbox_rows(rig, event_id)
        return bool(rows) and all(row.status == status for row in rows)

    assert await wait_until(
        _condition, timeout=120.0, interval=0.25
    ), f"outbox rows for {event_id} never reached {status}"
    rows = await _outbox_rows(rig, event_id)
    assert rows
    return rows[0]


async def _await_outbox_status(rig: D2Rig, event_id: str, status: str) -> Any:
    """Wait until at least one outbox row of *event_id* reaches *status*."""

    matched: Any = None

    async def _condition() -> bool:
        nonlocal matched
        rows = await _outbox_rows(rig, event_id)
        matched = next((row for row in rows if row.status == status), None)
        return matched is not None

    assert await wait_until(
        _condition, timeout=120.0, interval=0.25
    ), f"no outbox row for {event_id} reached {status}"
    return matched


async def _await_native_ref(rig: D2Rig, event_id: str) -> str:
    """Wait for the outbound native ref and return the native message ID."""

    async def _condition() -> bool:
        refs = await rig.storage.list_native_refs_for_event(event_id)
        return any(ref.native_message_id for ref in refs)

    assert await wait_until(
        _condition, timeout=120.0, interval=0.25
    ), f"no outbound native ref recorded for {event_id}"
    refs = await rig.storage.list_native_refs_for_event(event_id)
    return refs[0].native_message_id


async def _matrix_native_refs_async(rig: D2Rig, event_id: str) -> list[Any]:
    refs = await rig.storage.list_native_refs_for_event(event_id)
    return [ref for ref in refs if ref.adapter == rig.adapter_id]


# ---------------------------------------------------------------------------
# Scenario 1 — routed outbound observed by an independent recipient
# ---------------------------------------------------------------------------


async def test_d2_routed_outbound_recipient_decrypts_content_room_and_relation(
    d2_env: SynapseEnv, tmp_path: Path, d2_resources: contextlib.AsyncExitStack
) -> None:
    """Target 1: the routed outbound is encrypted, and an independently
    controlled recipient decrypts the intended content in the intended
    conversation with the intended relation scope — recipient-side
    observation only."""
    env = d2_env
    room_id = env.encrypted_room_id
    rig_store = str(tmp_path / "rig-store")
    peer_store = str(tmp_path / "peer-store")
    db_path = str(tmp_path / "rig-storage.db")
    for store_dir in (rig_store, peer_store):
        Path(store_dir).mkdir(parents=True, exist_ok=True)

    peer_client = await _init_peer_client(
        env, _PEER_LOCALPART, _PEER_PASSWORD, peer_store
    )
    d2_resources.push_async_callback(peer_client.close)
    rig: D2Rig | None = None
    verdict: dict[str, Any] = {
        "transport": "matrix",
        "evidence_level": "docker_encrypted_room_qualification",
        "scenario": "routed_outbound_recipient_decryption",
        "synapse": _synapse_image_digest(),
    }
    try:
        rig = await _managed_rig(
            d2_resources,
            env,
            adapter_id="d2-matrix-target",
            store_path=rig_store,
            db_path=db_path,
        )
        await _await_bot_sees_peer_device(rig, env.peer_user_id)

        # Parent message: routed through durable admission, the durable
        # outbox and the retry worker to the real encrypted send path.
        parent_body = "d2 routed parent 2026-11-17"
        await _admit_message(rig, "d2-evt-parent", parent_body)
        await _await_outbox_terminal(rig, "d2-evt-parent", "sent")
        parent_native_id = await _await_native_ref(rig, "d2-evt-parent")
        assert parent_native_id.startswith("$")
        verdict["parent_native_id_redacted"] = _redact(parent_native_id)

        # Reply message: a canonical reply relation to the parent, resolved
        # per destination by core relation binding at delivery time.
        reply_body = "d2 routed reply relation scope"
        await _admit_message(
            rig,
            "d2-evt-reply",
            reply_body,
            relations=(
                EventRelation(
                    relation_type="reply",
                    target_event_id="d2-evt-parent",
                    target_native_ref=None,
                    key=None,
                    fallback_text=None,
                ),
            ),
        )
        await _await_outbox_terminal(rig, "d2-evt-reply", "sent")
        reply_native_id = await _await_native_ref(rig, "d2-evt-reply")

        # Recipient-side decryption and scope verification.
        parent_seen = await _peer_find_body(peer_client, room_id, parent_body)
        assert parent_seen, "recipient never decrypted the routed parent message"
        assert parent_native_id in parent_seen, (
            "recipient-observed parent event ID must equal the homeserver-"
            f"assigned ID recorded in the native ref: {parent_seen} vs "
            f"{parent_native_id}"
        )

        reply_seen = await _peer_find_body(peer_client, room_id, reply_body)
        assert reply_seen, "recipient never decrypted the routed reply message"
        assert reply_native_id in reply_seen

        reply_content = None
        for _ in range(8):
            seen: dict = getattr(peer_client, "_d2_seen", None) or {}
            for _event_id, content in list(seen.items()) + await _peer_timeline(
                peer_client, room_id, 1
            ):
                if (
                    _event_id == reply_native_id
                    and content.get("body") == reply_body
                    and "m.relates_to" in content
                ):
                    reply_content = content
            if reply_content is not None:
                break
        assert reply_content is not None, (
            "recipient never observed the reply relation binding on the "
            "decrypted reply event"
        )
        in_reply_to = (
            reply_content.get("m.relates_to", {})
            .get("m.in_reply_to", {})
            .get("event_id")
        )
        assert in_reply_to == parent_native_id, (
            f"reply relation scope mismatch: m.in_reply_to={in_reply_to!r}, "
            f"expected the parent native event {parent_native_id!r}"
        )
        verdict["reply_relation_bound"] = True

        # Conversation scope: the recipient synced exactly the intended room,
        # and the stored native refs agree on that channel.
        for event_id in ("d2-evt-parent", "d2-evt-reply"):
            matrix_refs = await _matrix_native_refs_async(rig, event_id)
            assert matrix_refs, f"no matrix native ref for {event_id}"
            assert all(
                ref.native_channel_id == room_id for ref in matrix_refs
            ), f"native ref channel scope mismatch for {event_id}"
        verdict["conversation_scope_room_redacted"] = _redact(room_id)
    finally:
        if rig is not None:
            await _stop_rig(rig)
    _write_verdict(tmp_path, "d2-scenario1-routed-outbound-verdict.json", verdict)


# ---------------------------------------------------------------------------
# Scenario 2 — restart identity preservation under the durable contracts
# ---------------------------------------------------------------------------


async def test_d2_restart_preserves_device_and_checkpoint_identities(
    d2_env: SynapseEnv,
    tmp_path: Path,
    d2_resources: contextlib.AsyncExitStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Target 2: restart the runtime/session on durable state and preserve

    * the device identity — the durable sync contract binds the SDK store to
      the account AND device and requires reopening to preserve device crypto
      (``docs/spec/durable-sync.md``; the durable database is located exactly
      where the installed SDK puts it: ``{user_id}_{device_id}.db``);
    * the durable sync consumer identity — ``open_sync`` MUST reuse the
      already-adopted consumer binding and MUST NOT rewrite SDK consumer IDs;
    * the application checkpoint identity — the adapter commits its durable
      sync cursor through the host checkpoint contract (``nio_durable``
      stream) and the restarted session reloads it;
    The transaction identity of retained work is qualified by the separate
    interrupted-send scenario; this is an in-process session reopen."""
    env = d2_env
    rig_store = str(tmp_path / "rig-store")
    peer_store = str(tmp_path / "peer-store")
    db_path = str(tmp_path / "rig-storage.db")
    for store_dir in (rig_store, peer_store):
        Path(store_dir).mkdir(parents=True, exist_ok=True)

    peer_client = await _init_peer_client(
        env, _PEER_LOCALPART, _PEER_PASSWORD, peer_store
    )
    d2_resources.push_async_callback(peer_client.close)
    verdict: dict[str, Any] = {
        "transport": "matrix",
        "evidence_level": "docker_encrypted_room_qualification",
        "scenario": "restart_identity_preservation",
    }
    rig = await _managed_rig(
        d2_resources,
        env,
        adapter_id="d2-restart-target",
        store_path=rig_store,
        db_path=db_path,
    )
    try:
        await _await_bot_sees_peer_device(rig, env.peer_user_id)

        # One delivered message before restart (delivered identity evidence).
        await _admit_message(rig, "d2-evt-pre-restart", "d2 pre-restart body")
        await _await_outbox_terminal(rig, "d2-evt-pre-restart", "sent")
        pre_native = await _await_native_ref(rig, "d2-evt-pre-restart")
        seen_pre = await _peer_find_body(
            peer_client, rig.target_room_id, "d2 pre-restart body"
        )
        assert (
            seen_pre and pre_native in seen_pre
        ), "recipient never decrypted the pre-restart delivery"

        # Identity snapshot 1: device keys, durable DB name, consumer UUID,
        # application checkpoint stream.
        session = rig.adapter._session
        client = session._client
        device_id = client.device_id
        keys_before = dict(client.olm.account.identity_keys)
        durable_db = Path(rig_store) / _durable_database_name(
            env.bot_user_id, device_id
        )
        assert durable_db.is_file(), f"durable database missing: {durable_db.name}"
        consumer_before = _existing_consumer_id(
            Path(rig_store), env.bot_user_id, device_id
        )
        assert consumer_before is not None, "durable consumer binding not adopted"

        async def _checkpoint() -> Any:
            return await rig.storage.get_adapter_checkpoint(
                "d2-restart-target", "nio_durable"
            )

        assert await wait_until(
            lambda: _checkpoint(), timeout=60.0, interval=0.5
        ), "durable sync checkpoint was never committed"
        checkpoint_before = await _checkpoint()
        assert checkpoint_before.cursor, "checkpoint cursor must be non-empty"
        verdict["device_id_redacted"] = _redact(device_id)
        verdict["checkpoint_stream"] = "nio_durable"

        # Observe the restored flag before the new owner can accept a batch.
        original_load = MatrixSession._load_classic_checkpoint
        restored: list[bool] = []

        async def observe_reload(session: MatrixSession) -> None:
            await original_load(session)
            restored.append(session.diagnostics().committed_checkpoint_present)

        monkeypatch.setattr(MatrixSession, "_load_classic_checkpoint", observe_reload)

        # Restart through the production stop paths, then reopen the same
        # durable state.
        await _stop_rig(rig)
        rig = await _managed_rig(
            d2_resources,
            env,
            adapter_id="d2-restart-target",
            store_path=rig_store,
            db_path=db_path,
        )
        await _await_bot_sees_peer_device(rig, env.peer_user_id)

        # Identity snapshot 2 and contract-tied equality assertions.
        client_after = rig.adapter._session._client
        assert (
            client_after.device_id == device_id
        ), "device identity changed across restart"
        keys_after = dict(client_after.olm.account.identity_keys)
        assert keys_after == keys_before, (
            "olm account identity keys changed across restart; the durable "
            "store did not preserve device crypto"
        )
        consumer_after = _existing_consumer_id(
            Path(rig_store), env.bot_user_id, device_id
        )
        assert consumer_after == consumer_before, (
            "durable sync consumer identity was rewritten across restart; "
            "open_sync must re-adopt the stored binding"
        )
        checkpoint_after = await _checkpoint()
        assert (
            checkpoint_after is not None
        ), "application checkpoint stream missing after restart"
        assert checkpoint_after.cursor, "checkpoint cursor empty after restart"
        stream_before, seq_before = checkpoint_before.cursor.rsplit(":", 1)
        stream_after, seq_after = checkpoint_after.cursor.rsplit(":", 1)
        assert stream_before == stream_after and int(seq_after) >= int(seq_before)
        diagnostics = rig.adapter.diagnostics()
        assert (
            diagnostics.get("checkpoint_owned_by_medre") is False
        ), "the SDK retains upstream cursor ownership across restart"
        assert (
            diagnostics.get("committed_checkpoint_present") is True
        ), "restarted session must report the committed checkpoint it reloaded"
        assert (
            restored and restored[0]
        ), "checkpoint evidence was not restored before sync"
        verdict["restart_scope"] = "same_process_session_reopen"
        verdict["consumer_id_stable"] = True
        verdict["device_keys_stable"] = True
        verdict["checkpoint_reloaded"] = True

        # Post-restart delivery: the reopened device encrypts for the same
        # room and the independent recipient decrypts it.
        post_body = "d2 post-restart body"
        await _admit_message(rig, "d2-evt-post-restart", post_body)
        await _await_outbox_terminal(rig, "d2-evt-post-restart", "sent")
        post_native = await _await_native_ref(rig, "d2-evt-post-restart")
        seen_post = await _peer_find_body(peer_client, rig.target_room_id, post_body)
        assert (
            seen_post and post_native in seen_post
        ), "recipient never decrypted the post-restart delivery"
        verdict["post_restart_delivery"] = "delivered_and_decrypted"
    finally:
        await _stop_rig(rig)
    _write_verdict(tmp_path, "d2-scenario2-restart-identity-verdict.json", verdict)


# ---------------------------------------------------------------------------
# Scenario 3 — interrupted send trichotomy
# ---------------------------------------------------------------------------


async def test_d2_interrupted_send_trichotomy(
    d2_env: SynapseEnv,
    tmp_path: Path,
    d2_resources: contextlib.AsyncExitStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Target 3: distinguish accepted / proven-unsent / uncertain facts for
    interrupted sends, reuse accepted identity where supported, never infer
    nonacceptance from silence, and never authorize a fresh send merely
    because a session restarted.

    Proven-unsent: the homeserver itself refuses the submission (M_FORBIDDEN,
    the room owner strips the bot's send permission). The pinned SDK refuses
    any room send for a room absent from its synced room map before the
    request leaves the process, so non-membership alone can never produce
    server evidence: the harness joins the bot itself while the adapter
    config keeps auto-join empty, and the refusal is produced server-side.
    No event is created; the durable outbox dead-letters with a permanent
    failure and no native ref.

    Uncertain: while the response-cut proxy is armed, every room-send request
    is fully forwarded and its response is destroyed. The adapter surfaces a
    transient failure and the pipeline records no native ref — from the
    client's viewpoint acceptance is UNKNOWN. Independent recipient
    observation then proves the homeserver actually accepted the send.

    Accepted via identity reuse: after an in-process session reopen (same durable state)
    the retry worker re-dispatches the retained uncertain row; the
    re-rendered delivery reuses the same deterministic transaction identity,
    the homeserver deduplicates it, and the recipient observes exactly one
    copy whose native event ID equals the recovered native ref.
    """
    env = d2_env
    room_id = env.encrypted_room_id
    rig_store = str(tmp_path / "rig-store")
    peer_store = str(tmp_path / "peer-store")
    db_path = str(tmp_path / "rig-storage.db")
    for store_dir in (rig_store, peer_store):
        Path(store_dir).mkdir(parents=True, exist_ok=True)

    peer_client = await _init_peer_client(
        env, _PEER_LOCALPART, _PEER_PASSWORD, peer_store
    )
    d2_resources.push_async_callback(peer_client.close)
    port = urlsplit(env.base_url).port
    assert port is not None
    proxy = SendResponseCutProxy("127.0.0.1", port)
    d2_resources.push_async_callback(proxy.close)
    proxy_port = await proxy.start()
    verdict: dict[str, Any] = {
        "transport": "matrix",
        "evidence_level": "docker_encrypted_room_qualification",
        "scenario": "interrupted_send_trichotomy",
        "phases": {},
        "limitations": [
            "The response-cut interruption is deterministic fault injection "
            "below the SDK; adapter, SDK and Synapse behavior around it is "
            "unmodified.",
            "Recipient absence after finite syncs corroborates but never "
            "proves nonacceptance; the proven-unsent phase rests on the "
            "server's own refusal response.",
        ],
    }

    sends: list[tuple[str, str | None, Any]] = []
    original_send = MatrixSession.room_send

    async def observe_send(session, *args, **kwargs):
        slot = len(sends)
        sends.append((kwargs["room_id"], kwargs.get("tx_id"), None))
        response = await original_send(session, *args, **kwargs)
        sends[slot] = (kwargs["room_id"], kwargs.get("tx_id"), response)
        return response

    monkeypatch.setattr(MatrixSession, "room_send", observe_send)

    # Phase 1: proven-unsent via server-directed refusal (M_FORBIDDEN).
    # The harness joins the bot to the peer-owned plaintext room — the
    # adapter config keeps auto_join_rooms empty and must never join on its
    # own — then the peer owner strips the bot's send permission with a
    # raised events_default, so the submission reaches Synapse and is
    # refused there.
    _http_json(
        f"{env.base_url}/_matrix/client/v3/join/{env.plain_room_id}",
        {},
        token=env.bot_access_token,
        method="POST",
    )
    _http_json(
        f"{env.base_url}/_matrix/client/v3/rooms/{env.plain_room_id}"
        "/state/m.room.power_levels",
        {
            # Room versions forbid listing the creator in content.users
            # (the creator's power level is implicit); pin the bot at 0 and
            # raise events_default so the bot's send is refused server-side.
            "users": {env.bot_user_id: 0},
            "users_default": 0,
            "events_default": 50,
        },
        token=env.peer_access_token,
        method="PUT",
    )
    # The refusal rig runs on its own bot device identity: one-time-key
    # namespaces belong to the device, and a fresh local store mints keys
    # from index 0, so a second store on the interrupt rig's device would
    # conflict on the homeserver (400: one-time key already exists).
    refusal_login = _login(env.base_url, _BOT_LOCALPART, _BOT_PASSWORD)
    refusal_rig = await _managed_rig(
        d2_resources,
        env,
        adapter_id="d2-refusal-target",
        store_path=str(tmp_path / "refusal-store"),
        db_path=str(tmp_path / "refusal-storage.db"),
        target_room_id=env.plain_room_id,
        verification_policy=None,
        encryption_mode="e2ee_optional",
        auto_join_rooms=(),
        with_retry_worker=False,
        bot_access_token=refusal_login["access_token"],
        bot_device_id=refusal_login.get("device_id", ""),
    )
    try:

        def _refusal_room_known() -> bool:
            session = refusal_rig.adapter._session
            client = getattr(session, "_client", None)
            rooms = getattr(client, "rooms", None)
            return bool(rooms) and env.plain_room_id in rooms

        assert await wait_until(
            _refusal_room_known, timeout=60.0, interval=0.5
        ), "bot session never synced the refusal target room"
        await _admit_message(refusal_rig, "d2-evt-refused", "d2 proven-unsent attempt")
        recovery_worker = _start_retry_worker(
            refusal_rig.storage,
            refusal_rig.runner,
            refusal_rig.adapter_id,
            enabled=False,
            initial_dispatch=True,
            interval_seconds=30.0,
        )
        try:
            await recovery_worker.start()
            await _await_outbox_terminal(refusal_rig, "d2-evt-refused", "dead_lettered")
        finally:
            await recovery_worker.stop()
            assert not recovery_worker.has_retained_work
        rows = await _outbox_rows(refusal_rig, "d2-evt-refused")
        row = rows[0]
        matrix_refs = await _matrix_native_refs_async(refusal_rig, "d2-evt-refused")
        assert not matrix_refs, "a server-refused send must not record a native ref"
        row_failure = row.failure_kind or ""
        assert (
            "permanent" in row_failure
        ), f"server refusal must be classified permanent, got {row_failure!r}"
        assert any(
            room == env.plain_room_id
            and getattr(response, "status_code", None) == "M_FORBIDDEN"
            and getattr(getattr(response, "transport_response", None), "status", None)
            == 403
            for room, _, response in sends
        ), "no native M_FORBIDDEN response"
        verdict["phases"]["proven_unsent_server_refusal"] = {
            "native_error": "M_FORBIDDEN",
            "outcome": "dead_lettered",
            "failure_kind": row_failure,
            "native_ref": None,
        }
    finally:
        await _stop_rig(refusal_rig)

    # Phase 2: initial dispatch only; no retries may race the observation.
    rig = await _managed_rig(
        d2_resources,
        env,
        adapter_id="d2-interrupt-target",
        store_path=rig_store,
        db_path=db_path,
        homeserver=f"http://127.0.0.1:{proxy_port}",
        verification_policy=None,
        with_retry_worker=False,
    )
    server_event_id: str | None = None
    try:
        await _await_bot_sees_peer_device(rig, env.peer_user_id)
        interrupted_body = "d2 interrupted send body"
        await _admit_message(rig, "d2-evt-interrupted", interrupted_body)
        dispatch_worker = _start_retry_worker(
            rig.storage,
            rig.runner,
            rig.adapter_id,
            enabled=False,
            initial_dispatch=True,
            interval_seconds=30.0,
        )
        try:
            await dispatch_worker.start()
            await _await_outbox_status(rig, "d2-evt-interrupted", "retry_wait")
        finally:
            await dispatch_worker.stop()
            assert not dispatch_worker.has_retained_work
        row = await _await_outbox_status(rig, "d2-evt-interrupted", "retry_wait")
        matrix_refs = await _matrix_native_refs_async(rig, "d2-evt-interrupted")
        assert (
            not matrix_refs
        ), "an uncertain attempt must not record a native message ref"
        assert proxy.cuts >= 1, "the proxy never cut a send response"
        assert "transient" in (row.failure_kind or ""), (
            f"interrupted send must be classified transient, got "
            f"{row.failure_kind!r}"
        )
        verdict["phases"]["uncertain_after_interruption"] = {
            "outcome": "retry_wait",
            "proxy_cuts": proxy.cuts,
            "failure_kind": row.failure_kind,
            "native_ref": None,
        }

        # Independent recipient observation: the homeserver ACTUALLY accepted
        # the interrupted send. The client could not have known this.
        accepted_ids = await _peer_find_body(
            peer_client, room_id, interrupted_body, attempts=10
        )
        assert accepted_ids, (
            "the interrupted send was never observed by the recipient; the "
            "scenario requires a genuinely accepted-but-unreported send"
        )
        assert (
            len(accepted_ids) == 1
        ), f"expected exactly one accepted copy, observed {accepted_ids}"
        server_event_id = accepted_ids[0]
        verdict["phases"]["uncertain_after_interruption"][
            "server_accepted_event_id_redacted"
        ] = _redact(server_event_id)
    finally:
        await _stop_rig(rig)

    assert server_event_id is not None
    interrupted_tx = {txn for room, txn, _ in sends if room == room_id}
    assert len(interrupted_tx) == 1 and None not in interrupted_tx
    sends.clear()

    # Phase 3: restart on the same durable state with the transport
    # restored; recovery re-dispatches the retained uncertain row.
    proxy.armed = False
    rig = await _managed_rig(
        d2_resources,
        env,
        adapter_id="d2-interrupt-target",
        store_path=rig_store,
        db_path=db_path,
        homeserver=f"http://127.0.0.1:{proxy_port}",
        verification_policy=None,
        with_retry_worker=False,
    )
    try:
        await _await_bot_sees_peer_device(rig, env.peer_user_id)
        recovery_worker = _start_retry_worker(
            rig.storage,
            rig.runner,
            rig.adapter_id,
            enabled=True,
            initial_dispatch=False,
            interval_seconds=0.5,
            max_attempts=30,
        )
        try:
            await recovery_worker.start()
            await _await_outbox_terminal(rig, "d2-evt-interrupted", "sent")
        finally:
            await recovery_worker.stop()
            assert not recovery_worker.has_retained_work
        recovered_native = await _await_native_ref(rig, "d2-evt-interrupted")

        # The homeserver deduplicated the reused transaction identity: the
        # recovered native ref IS the event accepted during the interruption.
        assert recovered_native == server_event_id, (
            f"recovery must reuse the accepted identity: recovered "
            f"{recovered_native!r}, server accepted {server_event_id!r}"
        )
        # Successful recipient syncs after recovery, with distinct native IDs
        # including undecryptable Megolm events from the fixture sender.
        await _peer_timeline(peer_client, room_id, attempts=3)
        final_ids = {
            event_id
            for event_id, sender in peer_client._d2_message_events.items()
            if sender == env.bot_user_id
        }
        assert final_ids == {
            server_event_id
        }, "recipient observed additional sender message identities"
        assert peer_client._d2_seen[server_event_id]["body"] == interrupted_body
        recovered_tx = {txn for room, txn, _ in sends if room == room_id}
        assert (
            recovered_tx == interrupted_tx
        ), "retained delivery changed transaction identity"
        verdict["phases"]["accepted_via_identity_reuse"] = {
            "outcome": "sent",
            "reused_server_event_id": True,
            "recipient_copies": 1,
        }

        verdict["transaction_identity"] = "observed_same_session_reopen_send_id"
        verdict["restart_scope"] = "same_process_session_reopen"
    finally:
        await _stop_rig(rig)

    assert set(verdict["phases"]) == {
        "proven_unsent_server_refusal",
        "uncertain_after_interruption",
        "accepted_via_identity_reuse",
    }
    _write_verdict(tmp_path, "d2-scenario3-trichotomy-verdict.json", verdict)


# ---------------------------------------------------------------------------
# Scenario 4 — server-directed cooldown and bounded unavailable server
# ---------------------------------------------------------------------------


async def test_d2_native_cooldown_and_operator_view(
    d2_rate_limit_env: SynapseEnv,
    d2_resources: contextlib.AsyncExitStack,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Target 4a: a genuine server-directed rate limit (M_LIMIT_EXCEEDED from
    Synapse's own message bucket — not a fake retry) drives the adapter
    cooldown, the wire-safe deferral gate and the durable retry schedule,
    while the operator view reports the actual known work."""
    env = d2_rate_limit_env
    room_id = env.encrypted_room_id
    rig_store = str(tmp_path / "rig-store")
    peer_store = str(tmp_path / "peer-store")
    db_path = str(tmp_path / "rig-storage.db")
    for store_dir in (rig_store, peer_store):
        Path(store_dir).mkdir(parents=True, exist_ok=True)

    peer_client = await _init_peer_client(
        env, _RL_PEER_LOCALPART, _RL_PEER_PASSWORD, peer_store
    )
    d2_resources.push_async_callback(peer_client.close)
    verdict: dict[str, Any] = {
        "transport": "matrix",
        "evidence_level": "docker_encrypted_room_qualification",
        "scenario": "native_server_directed_cooldown",
        "phases": {},
    }
    rig = await _managed_rig(
        d2_resources,
        env,
        adapter_id="d2-cooldown-target",
        store_path=rig_store,
        db_path=db_path,
    )
    try:
        await _await_bot_sees_peer_device(rig, env.peer_user_id)

        native_limits: list[tuple[datetime, int]] = []
        original_send = MatrixSession.room_send

        async def observe_rate_limit(session, *args, **kwargs):
            response = await original_send(session, *args, **kwargs)
            if getattr(response, "status_code", None) == "M_LIMIT_EXCEEDED":
                assert response.transport_response.status == 429
                hint = response.retry_after_ms
                assert isinstance(hint, int) and hint > 0
                native_limits.append((datetime.now(UTC), hint))
            return response

        monkeypatch.setattr(MatrixSession, "room_send", observe_rate_limit)
        # First send: the bucket has its burst token; delivery succeeds.
        await _admit_message(rig, "d2-evt-rl-first", "d2 cooldown first message")
        await _await_outbox_terminal(rig, "d2-evt-rl-first", "sent")

        # Second send immediately after: the homeserver's own message bucket
        # emits M_LIMIT_EXCEEDED with a retry_after hint.
        await _admit_message(rig, "d2-evt-rl-second", "d2 cooldown limited message")
        limited_row = await _await_outbox_status(rig, "d2-evt-rl-second", "retry_wait")
        assert "transient" in (
            limited_row.failure_kind or ""
        ), f"server rate limit must be transient, got {limited_row.failure_kind!r}"
        assert native_limits, "native rate-limit response was not observed"
        observed_at, retry_ms = native_limits[0]
        assert datetime.fromisoformat(
            limited_row.next_attempt_at
        ) >= observed_at + timedelta(milliseconds=retry_ms)
        diagnostics = rig.adapter.diagnostics()
        assert (
            diagnostics.get("outbound_rate_limit_events", 0) >= 1
        ), "adapter never observed a server-directed rate limit"
        remaining = diagnostics.get("outbound_cooldown_remaining_seconds", 0.0)
        assert (
            remaining > 0.0
        ), f"server-directed cooldown must be armed, remaining={remaining!r}"
        verdict["phases"]["server_rate_limit"] = {
            "outcome": "retry_wait",
            "failure_kind": limited_row.failure_kind,
            "cooldown_remaining_seconds": remaining,
            "next_attempt_at": limited_row.next_attempt_at,
            "native_retry_after_ms": retry_ms,
        }

        # While the cooldown is active, a further delivery is deferred without
        # touching the wire. The room-send seam is observed at the class
        # boundary (the session uses __slots__; instance patching is invalid).
        room_send_calls = 0
        original_room_send = MatrixSession.room_send

        async def _counting_room_send(self: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal room_send_calls
            room_send_calls += 1
            return await original_room_send(self, *args, **kwargs)

        monkeypatch.setattr(MatrixSession, "room_send", _counting_room_send)
        calls_before = room_send_calls
        with pytest.raises(AdapterSendError, match="cooldown"):
            await rig.adapter.deliver(
                _probe_result("d2-cooldown-gate", rig.adapter_id, room_id)
            )
        assert (
            room_send_calls == calls_before
        ), "cooldown-deferred delivery must not touch the wire"
        assert rig.adapter.diagnostics().get("outbound_cooldown_deferrals", 0) >= 1
        verdict["phases"]["cooldown_gate"] = "deferred_without_wire"

        seen = await _peer_find_body(peer_client, room_id, "d2 cooldown first message")
        assert seen, "recipient never decrypted the first send"
        verdict["phases"]["first_send"] = "delivered"

        # Cooldown expiry and durable recovery: the retained row recovers
        # through the retry worker once the server hint window passes.
        async def _cooldown_expired() -> bool:
            return (
                rig.adapter.diagnostics().get("outbound_cooldown_remaining_seconds", 0)
                <= 0.0
            )

        assert await wait_until(
            _cooldown_expired, timeout=120.0, interval=0.5
        ), "server-directed cooldown never expired"
        await _await_outbox_terminal(rig, "d2-evt-rl-second", "sent")
        second_native = await _await_native_ref(rig, "d2-evt-rl-second")
        second_seen = await _peer_find_body(
            peer_client, room_id, "d2 cooldown limited message", attempts=10
        )
        assert (
            second_seen and second_native in second_seen
        ), "the rate-limited message never recovered and reached the recipient"
        verdict["phases"]["cooldown_recovery"] = "delivered"
    finally:
        await _stop_rig(rig)
    _write_verdict(tmp_path, "d2-scenario4a-cooldown-verdict.json", verdict)


def _probe_result(event_id: str, target_adapter: str, room_id: str) -> RenderingResult:
    """A hand-built closed Matrix send envelope for the cooldown gate probe."""
    operation = MatrixOutboundOperation.send_event(
        "m.room.message",
        {"msgtype": "m.text", "body": "d2 cooldown gate probe"},
    )
    return RenderingResult(
        event_id=event_id,
        target_adapter=target_adapter,
        target_channel=room_id,
        payload=operation.to_payload(),
        metadata={"renderer": "matrix"},
    )


async def test_d2_bounded_unavailable_server_and_operator_view(
    d2_env: SynapseEnv,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    d2_resources: contextlib.AsyncExitStack,
) -> None:
    """Target 4b: with the homeserver genuinely stopped, the send path is
    bounded (no unbounded hammering), the durable outbox reports the actual
    known work, and recovery after the server returns completes without
    minting a fresh send identity for the retained work."""
    env = d2_env
    room_id = env.encrypted_room_id
    rig_store = str(tmp_path / "rig-store")
    peer_store = str(tmp_path / "peer-store")
    db_path = str(tmp_path / "rig-storage.db")
    for store_dir in (rig_store, peer_store):
        Path(store_dir).mkdir(parents=True, exist_ok=True)

    peer_client = await _init_peer_client(
        env, _PEER_LOCALPART, _PEER_PASSWORD, peer_store
    )
    d2_resources.push_async_callback(peer_client.close)
    verdict: dict[str, Any] = {
        "transport": "matrix",
        "evidence_level": "docker_encrypted_room_qualification",
        "scenario": "bounded_unavailable_server",
        "limitations": [
            "Session calls are counted; encryption preparation may fail before a room-send HTTP request."
        ],
        "phases": {},
    }
    rig = await _managed_rig(
        d2_resources,
        env,
        adapter_id="d2-unavailable-target",
        store_path=rig_store,
        db_path=db_path,
        with_retry_worker=False,
    )
    try:
        await _await_bot_sees_peer_device(rig, env.peer_user_id)

        # Healthy baseline while the server is up (single-shot dispatch).
        await _admit_message(rig, "d2-evt-up-baseline", "d2 unavailable baseline")
        baseline_worker = _start_retry_worker(
            rig.storage,
            rig.runner,
            rig.adapter_id,
            enabled=False,
            initial_dispatch=True,
            interval_seconds=30.0,
        )
        try:
            await baseline_worker.start()
            await _await_outbox_terminal(rig, "d2-evt-up-baseline", "sent")
        finally:
            await baseline_worker.stop()
            assert not baseline_worker.has_retained_work
        seen = await _peer_find_body(peer_client, room_id, "d2 unavailable baseline")
        assert seen, "recipient never decrypted the baseline send"
        verdict["phases"]["baseline"] = "delivered"

        # Stop the homeserver container: a genuinely unavailable server.
        _docker("stop", env.container, timeout=90)
        unavailable_body = "d2 sent while server unavailable"
        await _admit_message(rig, "d2-evt-down", unavailable_body)

        room_send_calls = 0
        transaction_ids: list[str | None] = []
        original_room_send = MatrixSession.room_send

        async def _counting_room_send(self: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal room_send_calls
            room_send_calls += 1
            transaction_ids.append(kwargs.get("tx_id"))
            return await original_room_send(self, *args, **kwargs)

        monkeypatch.setattr(MatrixSession, "room_send", _counting_room_send)

        down_worker = _start_retry_worker(
            rig.storage,
            rig.runner,
            rig.adapter_id,
            enabled=False,
            initial_dispatch=True,
            interval_seconds=30.0,
        )
        try:
            await down_worker.start()
            await _await_outbox_status(rig, "d2-evt-down", "retry_wait")
        finally:
            await down_worker.stop()
            assert not down_worker.has_retained_work
        down_row = await _await_outbox_status(rig, "d2-evt-down", "retry_wait")
        # The in-adapter retry policy is bounded: exactly the configured
        # attempts for this pipeline attempt, then the failure is recorded.
        assert (
            0 < room_send_calls <= 3
        ), f"unavailable-server session calls must be bounded, saw {room_send_calls}"
        assert "transient" in (
            down_row.failure_kind or ""
        ), f"unavailable server must be transient, got {down_row.failure_kind!r}"
        assert rig.adapter._transient_delivery_failures >= 1
        # No native ref may exist for work whose acceptance is unknown.
        matrix_refs = await _matrix_native_refs_async(rig, "d2-evt-down")
        assert (
            not matrix_refs
        ), "uncertain unavailable-server work must not claim acceptance"
        verdict["phases"]["unavailable_server"] = {
            "outcome": "retry_wait",
            "session_room_send_calls": room_send_calls,
            "failure_kind": down_row.failure_kind,
            "native_ref": None,
        }

        down_tx = set(transaction_ids)
        assert len(down_tx) == 1 and None not in down_tx
        transaction_ids.clear()
        # Server returns; the recovery owner re-dispatches the retained row.
        _docker("start", env.container, timeout=90)
        assert (
            "http://" + _docker("port", env.container, "8008/tcp", timeout=15).strip()
            == env.base_url
        )
        _wait_ready(env.base_url)
        recovery_worker = _start_retry_worker(
            rig.storage,
            rig.runner,
            rig.adapter_id,
            enabled=True,
            initial_dispatch=False,
            interval_seconds=0.5,
            max_attempts=30,
        )
        try:
            await recovery_worker.start()
            await _await_outbox_terminal(rig, "d2-evt-down", "sent")
        finally:
            await recovery_worker.stop()
            assert not recovery_worker.has_retained_work
        assert (
            set(transaction_ids) == down_tx
        ), "outage recovery changed transaction identity"
        recovered_native = await _await_native_ref(rig, "d2-evt-down")
        recovered_seen = await _peer_find_body(
            peer_client, room_id, unavailable_body, attempts=10
        )
        assert (
            recovered_seen and recovered_native in recovered_seen
        ), "recovered delivery never reached the independent recipient"
        verdict["phases"]["recovery_after_return"] = {
            "outcome": "sent",
            "native_ref_redacted": _redact(recovered_native),
        }
    finally:
        with contextlib.suppress(subprocess.SubprocessError, OSError):
            _docker("start", env.container, timeout=90)
        await _stop_rig(rig)
    _write_verdict(tmp_path, "d2-scenario4b-unavailable-verdict.json", verdict)
