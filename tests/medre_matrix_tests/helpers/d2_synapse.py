"""Disposable loopback Synapse homeservers and D2 recipient observations."""

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
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

from medre.adapters.matrix.compat import HAS_E2EE

logger = logging.getLogger("medre.matrix.d2")

_SYNAPSE_IMAGE = "matrixdotorg/synapse:v1.164.2"
_SERVER_NAME = "medre-d2.local"
_CONTAINER = "medre-d2-synapse"
_RL_CONTAINER = "medre-d2-ratelimit-synapse"
_READY_TIMEOUT = 180

BOT_LOCALPART = "d2-bot"
BOT_PASSWORD = "d2-bot-password"
PEER_LOCALPART = "d2-peer"
PEER_PASSWORD = "d2-peer-password"
_RL_BOT_LOCALPART = "d2rl-bot"
_RL_BOT_PASSWORD = "d2rl-bot-password"
RL_PEER_LOCALPART = "d2rl-peer"
RL_PEER_PASSWORD = "d2rl-peer-password"


def docker(*args: str, timeout: int = 120) -> str:
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
        docker("info", "--format", "{{.ServerVersion}}", timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def synapse_image_digest() -> str:
    with contextlib.suppress(subprocess.SubprocessError, OSError, IndexError):
        return docker(
            "image",
            "inspect",
            "--format",
            "{{index .RepoDigests 0}}",
            _SYNAPSE_IMAGE,
            timeout=15,
        ).strip()
    return "<unavailable>"


def http_json(
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


def wait_ready(base_url: str, timeout: int = _READY_TIMEOUT) -> None:
    """Bounded readiness wait on the versions endpoint (service bootstrap)."""
    monotonic_deadline = time.monotonic() + timeout
    while time.monotonic() < monotonic_deadline:
        try:
            http_json(f"{base_url}/_matrix/client/versions", timeout=3)
            return
        except (RuntimeError, OSError):
            time.sleep(1.0)
    raise RuntimeError(f"Synapse did not become ready within {timeout}s")


def login(base_url: str, localpart: str, password: str) -> dict[str, Any]:
    return http_json(
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
    docker(
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
) -> Generator[SynapseEnv, None, None]:
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
        docker(
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
        docker(
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

        docker(
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

        docker(
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
        base_url = "http://" + docker("port", container, "8008/tcp", timeout=15).strip()
        assert urlsplit(base_url).hostname == "127.0.0.1"
        wait_ready(base_url)

        for localpart, password in (
            (bot_localpart, bot_password),
            (peer_localpart, peer_password),
        ):
            docker(
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

        bot_login = login(base_url, bot_localpart, bot_password)
        peer_login = login(base_url, peer_localpart, peer_password)

        encrypted_room = http_json(
            f"{base_url}/_matrix/client/v3/createRoom",
            {"name": "MEDRE D2 encrypted", "preset": "public_chat"},
            token=bot_login["access_token"],
            method="POST",
        )
        encrypted_room_id = encrypted_room["room_id"]
        http_json(
            f"{base_url}/_matrix/client/v3/join/{encrypted_room_id}",
            {},
            token=peer_login["access_token"],
            method="POST",
        )
        http_json(
            f"{base_url}/_matrix/client/v3/rooms/{encrypted_room_id}"
            "/state/m.room.encryption",
            {"algorithm": "m.megolm.v1.aes-sha2"},
            token=bot_login["access_token"],
            method="PUT",
        )

        # Peer-owned plaintext room for the server-directed refusal scenario;
        # the bot's membership and send-permission setup lives in that
        # scenario, never in this shared boot helper.
        plain_room = http_json(
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
            docker("rm", "-f", container, timeout=90)
        try:
            shutil.rmtree(data_dir)
        except PermissionError:
            _clear_root_owned(data_dir)
            shutil.rmtree(data_dir)
        parent.rmdir()


@pytest.fixture
def d2_env() -> Generator[SynapseEnv, None, None]:
    """Main qualification homeserver with ordinary rate limits."""
    if not _docker_available():
        pytest.skip("Docker is not available")
    if not HAS_E2EE:
        pytest.skip("mindroom-nio[e2e] is required for the D2 encrypted qualification")
    yield from _boot_synapse(
        _CONTAINER,
        bot_localpart=BOT_LOCALPART,
        bot_password=BOT_PASSWORD,
        peer_localpart=PEER_LOCALPART,
        peer_password=PEER_PASSWORD,
        rc_message=None,
    )


@pytest.fixture
def d2_rate_limit_env() -> Generator[SynapseEnv, None, None]:
    """Homeserver whose message bucket emits genuine M_LIMIT_EXCEEDED."""
    if not _docker_available():
        pytest.skip("Docker is not available")
    if not HAS_E2EE:
        pytest.skip("mindroom-nio[e2e] is required for the D2 encrypted qualification")
    yield from _boot_synapse(
        _RL_CONTAINER,
        bot_localpart=_RL_BOT_LOCALPART,
        bot_password=_RL_BOT_PASSWORD,
        peer_localpart=RL_PEER_LOCALPART,
        peer_password=RL_PEER_PASSWORD,
        rc_message={"per_second": 0.05, "burst_count": 1},
    )


async def init_peer_client(
    env: SynapseEnv, localpart: str, password: str, store_path: str
) -> Any:
    """Create the recipient nio client with genuine Megolm crypto enabled."""
    import nio

    peer_login = await asyncio.to_thread(login, env.base_url, localpart, password)
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


async def peer_timeline(
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


async def peer_find_body(
    peer_client: Any, room_id: str, body: str, attempts: int = 8
) -> list[str]:
    """Recipient-side observation: native event IDs whose decrypted body matches."""
    for _ in range(attempts):
        await peer_timeline(peer_client, room_id, 1)
        seen = peer_client._d2_seen
        matches = [
            event_id
            for event_id, content in seen.items()
            if content.get("body") == body
        ]
        if matches:
            return matches
    return []
