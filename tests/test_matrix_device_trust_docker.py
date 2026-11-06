"""Docker qualification for the peer-device verification policy (strict gate).

One ordered scenario on a real Synapse homeserver with real Megolm crypto
proves the merge gate carried in the ``verification_policy`` PR body: a
real encrypted room with verified and unverified recipient devices,
permissive delivery, correct strict refusal, and local trust maintained
across restarts — plus the fresh-startup strict send whose first trust
report must prepare room membership rather than report an unavailable
view.

Everything runs loopback: the Synapse container is session-scoped, both
recipients are local accounts, and trust is local nio-store state set by
the scenario itself.  The scenario drives the real adapter delivery path
(``MatrixAdapter.deliver`` with a closed outbound envelope), never raw
nio sends, so it qualifies the adapter's own gate.

Evidence classification: ``docker_encrypted_room_qualification``.  The
verdict JSON records each phase's outcome with redacted device IDs.

Running locally::

    .venv/bin/python -m pytest tests/test_matrix_device_trust_docker.py \\
        -m docker -v

Requires Docker and ``mindroom-nio[e2e]`` (``HAS_E2EE``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from medre.adapters.fakes.matrix import FakeMatrixAdapter
from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.compat import HAS_E2EE
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.config.adapters.matrix import MatrixConfig
from medre.core.contracts.adapter import AdapterContext, AdapterSendError
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events.bus import EventBus
from medre.core.ingress import DurableIngressWorker
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.rendering.renderer import RenderingPipeline, RenderingResult
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.core.supervision.accounting import RuntimeAccounting
from medre_matrix_tests.helpers.async_utils import wait_until
from medre_matrix_tests.helpers.matrix import matrix_send_payload

pytestmark = pytest.mark.docker

logger = logging.getLogger("medre.matrix39.docker")

_SYNAPSE_IMAGE = os.environ.get("MEDRE_SYNAPSE_IMAGE", "matrixdotorg/synapse:v1.164.2")
_SYNAPSE_SERVER_NAME = "medre-matrix39.local"
_SYNAPSE_PORT = int(os.environ.get("MEDRE_MATRIX39_SYNAPSE_PORT", "38108"))
_READY_TIMEOUT = 90
_CONTAINER = "medre-matrix39-synapse"

_BOT_LOCALPART = "medre39-bot"
_BOT_PASSWORD = "medre39-bot-password"
_ALT_BOT_LOCALPART = "medre39-bot2"
_ALT_BOT_PASSWORD = "medre39-bot2-password"
_TEST_LOCALPART = "medre39-peer"
_TEST_PASSWORD = "medre39-peer-password"


# ---------------------------------------------------------------------------
# Docker bootstrap (trimmed port of the core integration harness)
# ---------------------------------------------------------------------------


def _docker(*args: str, timeout: int = 60) -> str:
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


def _http_json(
    url: str,
    payload: dict[str, Any] | None = None,
    *,
    token: str | None = None,
    method: str = "GET",
    timeout: int = 15,
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(
            req, timeout=timeout
        ) as resp:  # nosec: localhost test container
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"HTTP {exc.code} for {method} {url}: {exc.read().decode()}"
        ) from exc


def _wait_ready(base_url: str) -> None:
    import time

    deadline = time.monotonic() + _READY_TIMEOUT
    while time.monotonic() < deadline:
        try:
            _http_json(f"{base_url}/_matrix/client/versions", timeout=3)
            return
        except (RuntimeError, OSError):
            time.sleep(1.0)
    raise RuntimeError(f"Synapse did not become ready within {_READY_TIMEOUT}s")


@dataclass
class SynapseDockerEnv:
    """Connection details for the running qualification homeserver."""

    base_url: str
    container: str
    bot_user_id: str
    bot_access_token: str
    bot_device_id: str
    alt_bot_user_id: str
    alt_bot_access_token: str
    alt_bot_device_id: str
    test_user_id: str
    test_access_token: str
    encrypted_room_id: str


def _login(base_url: str, localpart: str, password: str) -> dict[str, Any]:
    return _http_json(
        f"{base_url}/_matrix/client/v3/login",
        {"type": "m.login.password", "user": localpart, "password": password},
        method="POST",
    )


@pytest.fixture(scope="session")
def synapse39_env() -> Any:
    """Boot one Synapse container with an encrypted room for the scenario."""
    if not _docker_available():
        pytest.skip("Docker is not available")
    if not HAS_E2EE:
        pytest.skip("mindroom-nio[e2e] is required for the device-trust scenario")

    import tempfile

    _docker("rm", "-f", _CONTAINER, timeout=30)
    base_url = f"http://localhost:{_SYNAPSE_PORT}"
    # The bind root must exist before generate and be host-owned with the
    # execute bit set (mkdtemp-style 700 dirs lose traversal after the
    # chmod below: a+rw never adds x).
    parent = Path(tempfile.gettempdir()) / "medre-matrix39-synapse"
    parent.mkdir(parents=True, exist_ok=True)
    data_dir = parent / "synapse"
    if data_dir.exists():
        shutil.rmtree(data_dir, ignore_errors=True)
        if data_dir.exists():
            with suppress(PermissionError):
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
                    timeout=30,
                )
    data_dir.mkdir(parents=True, exist_ok=True)

    # Generate the homeserver config into the bind-mounted data dir, then
    # relax the image's UID-991 ownership so the host can read and append.
    _docker(
        "run",
        "--rm",
        "-e",
        f"SYNAPSE_SERVER_NAME={_SYNAPSE_SERVER_NAME}",
        "-e",
        "SYNAPSE_REPORT_STATS=no",
        "-v",
        f"{data_dir}:/data",
        _SYNAPSE_IMAGE,
        "generate",
        timeout=60,
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
        "a+rw",
        "/data",
        timeout=30,
    )

    # CI-friendly overrides: open registration, shared secret, relaxed
    # message and login rate limits for the multi-login scenario.
    homeserver_yaml = data_dir / "homeserver.yaml"
    with open(homeserver_yaml, "a", encoding="utf-8") as fh:
        fh.write("\n# matrix39 qualification overrides\n")
        fh.write("enable_registration: true\n")
        fh.write("enable_registration_without_verification: true\n")
        fh.write("registration_shared_secret: medre-matrix39-shared-secret\n")
        fh.write("rc_message:\n  per_second: 25\n  burst_count: 100\n")
        fh.write(
            "rc_login:\n"
            "  account:\n    per_second: 5\n    burst_count: 100\n"
            "  address:\n    per_second: 5\n    burst_count: 100\n"
        )

    _docker(
        "run",
        "-d",
        "--name",
        _CONTAINER,
        "-e",
        f"SYNAPSE_SERVER_NAME={_SYNAPSE_SERVER_NAME}",
        "-e",
        "SYNAPSE_REPORT_STATS=no",
        "-p",
        f"{_SYNAPSE_PORT}:8008",
        "-v",
        f"{data_dir}:/data",
        _SYNAPSE_IMAGE,
        timeout=60,
    )
    try:
        _wait_ready(base_url)

        for localpart, password, admin in (
            (_BOT_LOCALPART, _BOT_PASSWORD, True),
            (_ALT_BOT_LOCALPART, _ALT_BOT_PASSWORD, False),
            (_TEST_LOCALPART, _TEST_PASSWORD, False),
        ):
            _docker(
                "exec",
                _CONTAINER,
                "register_new_matrix_user",
                "-u",
                localpart,
                "-p",
                password,
                *(("-a",) if admin else ("--no-admin",)),
                "-c",
                "/data/homeserver.yaml",
                "http://localhost:8008",
                timeout=30,
            )

        bot_login = _login(base_url, _BOT_LOCALPART, _BOT_PASSWORD)
        alt_bot_login = _login(base_url, _ALT_BOT_LOCALPART, _ALT_BOT_PASSWORD)
        test_login = _login(base_url, _TEST_LOCALPART, _TEST_PASSWORD)

        room = _http_json(
            f"{base_url}/_matrix/client/v3/createRoom",
            {"name": "MEDRE Matrix39 Qualification", "preset": "public_chat"},
            token=bot_login["access_token"],
            method="POST",
        )
        room_id = room["room_id"]
        _http_json(
            f"{base_url}/_matrix/client/v3/join/{room_id}",
            {},
            token=test_login["access_token"],
            method="POST",
        )
        _http_json(
            f"{base_url}/_matrix/client/v3/rooms/{room_id}" f"/state/m.room.encryption",
            {"algorithm": "m.megolm.v1.aes-sha2"},
            token=bot_login["access_token"],
            method="PUT",
        )

        env = SynapseDockerEnv(
            base_url=base_url,
            container=_CONTAINER,
            bot_user_id=bot_login["user_id"],
            bot_access_token=bot_login["access_token"],
            bot_device_id=bot_login.get("device_id", ""),
            alt_bot_user_id=alt_bot_login["user_id"],
            alt_bot_access_token=alt_bot_login["access_token"],
            alt_bot_device_id=alt_bot_login.get("device_id", ""),
            test_user_id=test_login["user_id"],
            test_access_token=test_login["access_token"],
            encrypted_room_id=room_id,
        )
        logger.info(
            "Matrix39 Synapse ready: bot=%s peer=%s room=%s",
            env.bot_user_id,
            env.test_user_id,
            env.encrypted_room_id,
        )
        yield env
    finally:
        with suppress(subprocess.SubprocessError, OSError):
            _docker("rm", "-f", _CONTAINER, timeout=30)


# ---------------------------------------------------------------------------
# Scenario rig: real adapter + durable pipeline wiring
# ---------------------------------------------------------------------------


def _redact_device_id(device_id: str | None) -> str:
    if not device_id:
        return "<none>"
    if len(device_id) <= 4:
        return "****"
    return device_id[:2] + "****" + device_id[-2:]


def _close_nio_store(client: Any) -> None:
    """Close the nio store's SQLite connection the session stop misses.

    The pinned nio surface closes the client but leaves the store's
    SQLite connection to the garbage collector; a restart phase that
    reopens the same store must not race that connection.
    """
    store = getattr(client, "store", None)
    database = getattr(store, "database", None)
    if database is None:
        return
    try:
        from playhouse.sqliteq import SqliteQueueDatabase

        if isinstance(database, SqliteQueueDatabase) and not database.is_stopped():
            database.stop()
    except ImportError:
        pass
    with suppress(Exception):
        if not database.is_closed():
            database.close()


def _make_config(
    env: SynapseDockerEnv,
    *,
    adapter_id: str,
    store_path: str,
    verification_policy: str | None,
    identity: str = "primary",
) -> MatrixConfig:
    """Build one bot config; ``identity`` selects the bot account.

    Each adapter instance owns its own Matrix account and device: sharing
    one device identity across two nio stores forks durable key
    maintenance and the second session's crypto requests fail.
    """
    if identity == "primary":
        user_id, access_token, device_id = (
            env.bot_user_id,
            env.bot_access_token,
            env.bot_device_id,
        )
    else:
        user_id, access_token, device_id = (
            env.alt_bot_user_id,
            env.alt_bot_access_token,
            env.alt_bot_device_id,
        )
    kwargs: dict[str, Any] = {
        "adapter_id": adapter_id,
        "homeserver": env.base_url,
        "user_id": user_id,
        "access_token": access_token,
        "device_id": device_id,
        "room_allowlist": {env.encrypted_room_id},
        "encryption_mode": "e2ee_required",
        "auto_join_rooms": (env.encrypted_room_id,),
        "store_path": store_path,
    }
    if verification_policy is not None:
        kwargs["verification_policy"] = verification_policy
    return MatrixConfig(**kwargs).validate()


def _make_pipeline(storage: SQLiteStorage, env: SynapseDockerEnv) -> PipelineConfig:
    rendering = RenderingPipeline()
    rendering.register(MatrixRenderer(), priority=50)
    rendering.register(TextRenderer(), priority=100)
    route = Route(
        id="matrix39-qual-route",
        source=RouteSource(
            adapter="matrix39-strict-bot",
            event_kinds=("message.created",),
            channel=env.encrypted_room_id,
        ),
        targets=[RouteTarget(adapter="matrix39-fake-out")],
    )
    return PipelineConfig(
        storage=storage,  # type: ignore[arg-type]
        router=Router(routes=[route]),
        fallback_resolver=FallbackResolver(),
        relation_resolver=RelationResolver(storage=storage),
        adapters={},
        rendering_pipeline=rendering,
        runtime_accounting=RuntimeAccounting(),
        event_bus=EventBus(),
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
        logger=logging.getLogger(f"test.matrix39.{adapter_id}"),
        clock=lambda: datetime.now(UTC),
        shutdown_event=asyncio.Event(),
    )


@dataclass
class Rig:
    """One started MatrixAdapter plus its pipeline scaffolding."""

    adapter: MatrixAdapter
    runner: PipelineRunner
    worker: DurableIngressWorker
    fake: FakeMatrixAdapter
    storage: SQLiteStorage
    db_path: str


async def _start_rig(
    env: SynapseDockerEnv,
    *,
    adapter_id: str,
    store_path: str,
    db_path: str,
    verification_policy: str | None = "strict",
    identity: str = "primary",
) -> Rig:
    """Start one adapter with durable sync wired (the production shape)."""
    storage = SQLiteStorage(db_path=db_path)
    await storage.initialize()
    fake = FakeMatrixAdapter("matrix39-fake-out", channel="ch-0")
    runner = PipelineRunner(_make_pipeline(storage, env))
    worker = DurableIngressWorker(storage=storage, pipeline=runner)
    await runner.start()
    await worker.start()
    config = _make_config(
        env,
        adapter_id=adapter_id,
        store_path=store_path,
        verification_policy=verification_policy,
        identity=identity,
    )
    adapter = MatrixAdapter(config)
    await adapter.start(_make_ctx(adapter_id, runner, storage))
    return Rig(
        adapter=adapter,
        runner=runner,
        worker=worker,
        fake=fake,
        storage=storage,
        db_path=db_path,
    )


async def _stop_rig(rig: Rig) -> None:
    """Tear one rig down; the nio store close must precede any store reopen."""
    session = getattr(rig.adapter, "_session", None)
    client = getattr(session, "_client", None) if session is not None else None
    for name, stop_coro in (
        ("matrix39-adapter", rig.adapter.stop()),
        ("matrix39-worker", rig.worker.stop(grace_seconds=5.0)),
        ("matrix39-fake", rig.fake.stop()),
        ("matrix39-runner", rig.runner.stop()),
    ):
        try:
            await stop_coro
        except Exception:
            logger.debug("%s cleanup error", name, exc_info=True)
    if client is not None:
        _close_nio_store(client)
    with suppress(Exception):
        await rig.storage.close()


def _make_result(room_id: str, body: str, seq: int) -> RenderingResult:
    operation_payload = matrix_send_payload(
        {"msgtype": "m.text", "body": body},
    )
    return RenderingResult(
        event_id=f"matrix39-evt-{seq}",
        target_adapter="matrix39-strict-bot",
        target_channel=room_id,
        payload=operation_payload,
        metadata={"renderer": "matrix"},
    )


async def _await_bot_sees_peer_device(rig: Rig, peer_user_id: str) -> None:
    """Poll until the adapter's device store holds the peer's device keys."""

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


def _peer_device(rig: Rig, peer_user_id: str, peer_device_id: str) -> Any:
    client = rig.adapter._session._client
    matches = [
        device
        for device in client.device_store.active_user_devices(peer_user_id)
        if getattr(device, "id", None) == peer_device_id
    ]
    assert matches, f"peer device {peer_device_id} not in bot store"
    return matches[0]


async def _peer_decrypts(
    test_client: Any, room_id: str, body: str, attempts: int = 4
) -> bool:
    """Sync the peer client and look for the decrypted body in the batch.

    The pinned nio fork exposes decrypted timeline events on the sync
    response (``rooms.join[room_id].timeline.events``), not on the room
    projection.
    """
    for _ in range(attempts):
        response = await test_client.sync(timeout=8000)
        join = getattr(getattr(response, "rooms", None), "join", None) or {}
        info = join.get(room_id)
        timeline = getattr(info, "timeline", None) if info is not None else None
        for event in getattr(timeline, "events", None) or []:
            # A Megolm event only surfaces as RoomMessageText once the
            # peer's olm machine genuinely decrypted it.
            if (
                type(event).__name__ == "RoomMessageText"
                and getattr(event, "body", "") == body
            ):
                return True
    return False


async def _init_peer_client(env: SynapseDockerEnv, store_path: str) -> Any:
    """Create the peer nio client with genuine Megolm crypto enabled."""
    import nio

    test_login = _login(env.base_url, _TEST_LOCALPART, _TEST_PASSWORD)
    client = nio.AsyncClient(
        homeserver=env.base_url,
        user=test_login["user_id"],
        device_id=test_login["device_id"],
        store_path=store_path,
        config=nio.AsyncClientConfig(encryption_enabled=True),
    )
    try:
        client.restore_login(
            user_id=test_login["user_id"],
            device_id=test_login["device_id"],
            access_token=test_login["access_token"],
        )
        await client.sync(full_state=True)
        if client.should_upload_keys:
            await client.keys_upload()
        if client.should_query_keys:
            await client.keys_query()
        if client.should_claim_keys:
            with suppress(Exception):
                users = client.get_users_for_key_claiming()
                if users:
                    await client.keys_claim(users)
        await client.send_to_device_messages()
    except BaseException:
        await client.close()
        raise
    return client


# ---------------------------------------------------------------------------
# The qualification scenario
# ---------------------------------------------------------------------------


async def test_peer_device_verification_policy_docker_qualification(
    synapse39_env: SynapseDockerEnv,
    tmp_path: Path,
) -> None:
    """Prove the strict peer-device gate on a real encrypted room.

    Phase A: the FIRST strict delivery on a fresh session is refused for
    the unverified peer device — membership preparation must succeed (the
    refusal must name devices, never report an unavailable trust view).
    Phase B: verifying the peer device in MEDRE's own store lets the same
    strict delivery through, and the peer genuinely decrypts the Megolm
    payload.  Phase C: unverifying refuses again on the next send.
    Phase D: after an adapter restart on the same store, strict delivers
    without re-verification — local trust survives restarts.  Phase E: a
    default (permissive) adapter delivers despite trust gaps and records
    the diagnostics counter.
    """
    env = synapse39_env
    room_id = env.encrypted_room_id
    seq = 0
    verdict: dict[str, Any] = {
        "transport": "matrix",
        "evidence_level": "docker_encrypted_room_qualification",
        "encrypted_room_id": room_id,
        "bot_device_id_redacted": "<set-by-fixture-login>",
        "phases": {},
        "limitations": [
            "Docker loopback only — no federation, external network, or "
            "cross-signing ceremony proof.",
            "Trust is local nio-store state; no interactive verification UX.",
            "Peer decryption proves Megolm delivery, not recipient-side "
            "user action or read receipts.",
        ],
    }

    peer_store = str(tmp_path / "peer-store")
    strict_store = str(tmp_path / "strict-store")
    strict_db = str(tmp_path / "strict-storage.db")
    permissive_store = str(tmp_path / "permissive-store")
    permissive_db = str(tmp_path / "permissive-storage.db")
    # nio's sqlite store lease requires the store directory to exist.
    for store_dir in (peer_store, strict_store, permissive_store):
        Path(store_dir).mkdir(parents=True, exist_ok=True)

    test_client = await _init_peer_client(env, peer_store)
    peer_device_id = test_client.device_id
    verdict["peer_device_id_redacted"] = _redact_device_id(peer_device_id)

    strict_rig: Rig | None = None
    permissive_rig: Rig | None = None
    try:
        # Key warm-up under strict: the bot must know the peer device keys
        # before the fresh-startup send so the refusal names the device.
        strict_rig = await _start_rig(
            env,
            adapter_id="matrix39-strict-bot",
            store_path=strict_store,
            db_path=strict_db,
        )
        await _await_bot_sees_peer_device(strict_rig, env.test_user_id)
        session = strict_rig.adapter._session
        verdict["bot_device_id_redacted"] = _redact_device_id(
            getattr(session._client, "device_id", None)
        )

        # Phase A — fresh-startup strict refusal (first delivery ever).
        seq += 1
        body_a = f"matrix39 phase A refused body {seq}"
        with pytest.raises(AdapterSendError) as excinfo:
            await strict_rig.adapter.deliver(_make_result(room_id, body_a, seq))
        refusal = str(excinfo.value)
        assert excinfo.value.transient is True, "strict refusal must be retryable"
        assert "verification_policy='strict' refused" in refusal
        assert "unverified devices:" in refusal, refusal
        assert env.test_user_id in refusal
        assert peer_device_id in refusal
        assert "trust view is unavailable" not in refusal, (
            "fresh-startup trust report must prepare membership, "
            f"not report an unavailable view: {refusal}"
        )
        assert "no known device list" not in refusal
        assert strict_rig.adapter._peer_device_trust_strict_refusals == 1
        verdict["phases"]["fresh_startup_strict_refusal"] = {
            "outcome": "refused",
            "transient": True,
        }

        # Phase B — local verification lets strict deliver; peer decrypts.
        device = _peer_device(strict_rig, env.test_user_id, peer_device_id)
        assert strict_rig.adapter._session._client.verify_device(device) is True
        seq += 1
        body_b = f"matrix39 phase B verified delivery {seq}"
        delivered = await strict_rig.adapter.deliver(_make_result(room_id, body_b, seq))
        assert delivered.disposition == "transport_handoff"
        assert delivered.confirmation_level == "remote_service"
        assert delivered.native_message_id
        assert delivered.native_message_id.startswith("$")
        assert await _peer_decrypts(
            test_client, room_id, body_b
        ), "peer client never decrypted the verified strict delivery"
        verdict["phases"]["verified_strict_delivers"] = {
            "outcome": "delivered",
            "event_id_redacted": delivered.native_message_id[:6] + "****",
        }

        # Phase C — trust reset refuses the next send (policy re-reads).
        assert strict_rig.adapter._session._client.unverify_device(device) is True
        seq += 1
        body_c = f"matrix39 phase C unverified again {seq}"
        with pytest.raises(AdapterSendError) as excinfo_c:
            await strict_rig.adapter.deliver(_make_result(room_id, body_c, seq))
        assert "verification_policy='strict' refused" in str(excinfo_c.value)
        assert "unverified devices:" in str(excinfo_c.value)
        verdict["phases"]["unverified_refuses_again"] = {"outcome": "refused"}

        # Phase D — restart on the same store; verified trust survives.
        assert strict_rig.adapter._session._client.verify_device(device) is True
        await _stop_rig(strict_rig)
        strict_rig = None
        strict_rig = await _start_rig(
            env,
            adapter_id="matrix39-strict-bot",
            store_path=strict_store,
            db_path=strict_db,
        )
        seq += 1
        body_d = f"matrix39 phase D restart delivery {seq}"
        delivered_d = await strict_rig.adapter.deliver(
            _make_result(room_id, body_d, seq)
        )
        assert delivered_d.native_message_id
        assert delivered_d.native_message_id.startswith("$")
        assert delivered_d.confirmation_level == "remote_service"
        assert strict_rig.adapter._peer_device_trust_strict_refusals == 0
        verdict["phases"]["trust_survives_restart"] = {
            "outcome": "delivered",
            "event_id_redacted": delivered_d.native_message_id[:6] + "****",
        }

        # Phase E — permissive default delivers despite trust gaps.  The
        # adapter runs as its own bot account so the two nio stores never
        # share one device identity.
        permissive_rig = await _start_rig(
            env,
            adapter_id="matrix39-permissive-bot",
            store_path=permissive_store,
            db_path=permissive_db,
            verification_policy=None,
            identity="alt",
        )
        await _await_bot_sees_peer_device(permissive_rig, env.test_user_id)
        seq += 1
        body_e = f"matrix39 phase E permissive delivery {seq}"
        delivered_e = await permissive_rig.adapter.deliver(
            _make_result(room_id, body_e, seq)
        )
        assert delivered_e.native_message_id
        assert delivered_e.native_message_id.startswith("$")
        assert permissive_rig.adapter._peer_device_trust_warnings >= 1
        assert permissive_rig.adapter._peer_device_trust_strict_refusals == 0
        verdict["phases"]["permissive_default_delivers"] = {
            "outcome": "delivered",
            "warnings_recorded": permissive_rig.adapter._peer_device_trust_warnings,
        }
    finally:
        if strict_rig is not None:
            await _stop_rig(strict_rig)
        if permissive_rig is not None:
            await _stop_rig(permissive_rig)
        with suppress(Exception):
            await test_client.close()

    assert set(verdict["phases"]) == {
        "fresh_startup_strict_refusal",
        "verified_strict_delivers",
        "unverified_refuses_again",
        "trust_survives_restart",
        "permissive_default_delivers",
    }

    artifact_dir = os.environ.get("MEDRE_DOCKER_ARTIFACT_RUN_DIR")
    target_dir = Path(artifact_dir) if artifact_dir else tmp_path
    target_dir.mkdir(parents=True, exist_ok=True)
    verdict_path = target_dir / "matrix39-verification-policy-verdict.json"
    verdict_path.write_text(json.dumps(verdict, indent=2), encoding="utf-8")
    logger.info("Matrix39 qualification verdict written to %s", verdict_path)
