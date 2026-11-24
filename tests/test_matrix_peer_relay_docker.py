"""Docker qualification for the trusted peer-relay gate.

One ordered scenario on a real Synapse homeserver proves the ingress gate
carried by ``MatrixConfig.trusted_relays``: an unlisted relay account's
MEDRE envelope is dropped and counted with the remedy warning, the same
account after being listed is relayed, a listed account's malformed
envelope is dropped under the invalid-reason counter, and an ordinary
message from an unlisted account is relayed untouched.

Everything runs loopback in a plaintext room, so no E2EE backend is
required.  The scenario drives the real adapter ingress path —
``MatrixAdapter.start`` with the session's sync loop feeding
``_on_room_message`` — never raw event injection, and observes the gate
through published events, diagnostics counters, and captured log records.

Evidence classification: ``docker_peer_relay_gate_qualification``.

Running locally::

    .venv/bin/python -m pytest tests/test_matrix_peer_relay_docker.py \\
        -m docker -v

Requires Docker.
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
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from medre.core.contracts.adapter import AdapterContext
from medre.core.events import CanonicalEvent
from medre_matrix_tests.helpers.async_utils import wait_until

from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.compat import HAS_NIO
from medre.config.adapters.matrix import MatrixConfig

pytestmark = pytest.mark.docker

logger = logging.getLogger("medre.matrix-trust.docker")

_SYNAPSE_IMAGE = os.environ.get("MEDRE_SYNAPSE_IMAGE", "matrixdotorg/synapse:v1.164.2")
_SYNAPSE_SERVER_NAME = "medre-trust.local"
_SYNAPSE_PORT = int(os.environ.get("MEDRE_TRUST_SYNAPSE_PORT", "38471"))
_READY_TIMEOUT = 90
_CONTAINER = "medre-trust-synapse"

_BOT_LOCALPART = "medre-trust-bot"
_BOT_PASSWORD = "medre-trust-bot-password"
_RELAY_LOCALPART = "medre-trust-relay"
_RELAY_PASSWORD = "medre-trust-relay-password"
_OTHER_LOCALPART = "medre-trust-other"
_OTHER_PASSWORD = "medre-trust-other-password"

_WAIT_PUBLISHED = 30.0


# ---------------------------------------------------------------------------
# Docker bootstrap (trimmed port of the device-trust harness)
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
class SynapseTrustEnv:
    """Connection details for the qualification homeserver."""

    base_url: str
    container: str
    bot_user_id: str
    bot_access_token: str
    relay_user_id: str
    relay_access_token: str
    other_user_id: str
    other_access_token: str
    room_id: str


def _login(base_url: str, localpart: str, password: str) -> dict[str, Any]:
    return _http_json(
        f"{base_url}/_matrix/client/v3/login",
        {"type": "m.login.password", "user": localpart, "password": password},
        method="POST",
    )


@pytest.fixture(scope="session")
def trust_env() -> Any:
    """Boot one Synapse container with a plaintext shared room."""
    if not _docker_available():
        pytest.skip("Docker is not available")
    if not HAS_NIO:
        pytest.skip("mindroom-nio is required for the peer-relay scenario")

    import tempfile

    _docker("rm", "-f", "-v", _CONTAINER, timeout=30)
    base_url = f"http://localhost:{_SYNAPSE_PORT}"
    # Host-owned bind dir: the image's UID-991 files must stay readable and
    # writable by later root-side config appends on the host.
    parent = Path(tempfile.gettempdir()) / "medre-trust-synapse"
    parent.mkdir(parents=True, exist_ok=True)
    data_dir = parent / "synapse"
    shutil.rmtree(data_dir, ignore_errors=True)
    data_dir.mkdir(parents=True, exist_ok=True)

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

    # CI-friendly overrides: open registration and relaxed message/login
    # rate limits for the multi-account scenario.
    homeserver_yaml = data_dir / "homeserver.yaml"
    with open(homeserver_yaml, "a", encoding="utf-8") as fh:
        fh.write("\n# medre-trust qualification overrides\n")
        fh.write("enable_registration: true\n")
        fh.write("enable_registration_without_verification: true\n")
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

        for localpart, password in (
            (_BOT_LOCALPART, _BOT_PASSWORD),
            (_RELAY_LOCALPART, _RELAY_PASSWORD),
            (_OTHER_LOCALPART, _OTHER_PASSWORD),
        ):
            _docker(
                "exec",
                _CONTAINER,
                "register_new_matrix_user",
                "-u",
                localpart,
                "-p",
                password,
                "--no-admin",
                "-c",
                "/data/homeserver.yaml",
                "http://localhost:8008",
                timeout=30,
            )

        bot_login = _login(base_url, _BOT_LOCALPART, _BOT_PASSWORD)
        relay_login = _login(base_url, _RELAY_LOCALPART, _RELAY_PASSWORD)
        other_login = _login(base_url, _OTHER_LOCALPART, _OTHER_PASSWORD)

        room = _http_json(
            f"{base_url}/_matrix/client/v3/createRoom",
            {"name": "MEDRE Trust Qualification", "preset": "public_chat"},
            token=relay_login["access_token"],
            method="POST",
        )
        room_id = room["room_id"]
        _http_json(
            f"{base_url}/_matrix/client/v3/join/{room_id}",
            {},
            token=other_login["access_token"],
            method="POST",
        )

        env = SynapseTrustEnv(
            base_url=base_url,
            container=_CONTAINER,
            bot_user_id=bot_login["user_id"],
            bot_access_token=bot_login["access_token"],
            relay_user_id=relay_login["user_id"],
            relay_access_token=relay_login["access_token"],
            other_user_id=other_login["user_id"],
            other_access_token=other_login["access_token"],
            room_id=room_id,
        )
        logger.info(
            "Trust Synapse ready: bot=%s relay=%s room=%s",
            env.bot_user_id,
            env.relay_user_id,
            env.room_id,
        )
        yield env
    finally:
        with suppress(subprocess.SubprocessError, OSError):
            _docker("rm", "-f", "-v", _CONTAINER, timeout=30)


# ---------------------------------------------------------------------------
# Scenario rig
# ---------------------------------------------------------------------------


class _RecordingHandler(logging.Handler):
    """Capture log records for warning assertions."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self) -> list[str]:
        return [record.getMessage() for record in self.records]


@dataclass
class _PhaseRig:
    """One adapter phase: collecting context, capture handler, store dir."""

    adapter: MatrixAdapter
    published: list[CanonicalEvent]
    handler: _RecordingHandler
    capture_logger: logging.Logger
    store_path: Path
    _started: bool = field(default=False)

    async def start(self) -> None:
        ctx = AdapterContext(
            adapter_id=self.adapter.adapter_id,
            publish_inbound=self._publish,
            logger=self.capture_logger,
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )
        await self.adapter.start(ctx)
        self._started = True

    async def _publish(self, event: CanonicalEvent) -> None:
        self.published.append(event)

    async def stop(self) -> None:
        if self._started:
            await self.adapter.stop()
            self._started = False
        self.capture_logger.removeHandler(self.handler)


def _send_message(
    env: SynapseTrustEnv, sender_token: str, content: dict[str, Any]
) -> str:
    """Send one room message as *sender_token*'s account; return its event ID."""
    txn = uuid.uuid4().hex
    response = _http_json(
        f"{env.base_url}/_matrix/client/v3/rooms/{env.room_id}"
        f"/send/m.room.message/{txn}",
        content,
        token=sender_token,
        method="PUT",
    )
    return str(response["event_id"])


def _coherent_envelope_content() -> dict[str, Any]:
    return {
        "msgtype": "m.text",
        "body": "relayed provenance payload",
        "medre": {
            "envelope": {
                "schema_version": 1,
                "canonical_event_id": "evt-claim-marker",
                "source_adapter": "peer-relay",
                "source_channel": "!elsewhere:example.org",
                "metadata_mode": "safe",
            }
        },
    }


def _malformed_envelope_content() -> dict[str, Any]:
    return {
        "msgtype": "m.text",
        "body": "malformed claim payload",
        "medre": {"envelope": "not a dict"},
    }


async def _make_rig(
    env: SynapseTrustEnv,
    tmp_path: Path,
    *,
    adapter_id: str,
    trusted_relays: tuple[str, ...],
) -> _PhaseRig:
    config = MatrixConfig(
        adapter_id=adapter_id,
        homeserver=env.base_url,
        user_id=env.bot_user_id,
        access_token=env.bot_access_token,
        room_allowlist={env.room_id},
        trusted_relays=trusted_relays,
        encryption_mode="plaintext",
        sync_timeout_ms=5000,
        auto_join_rooms=(env.room_id,),
        store_path=str(tmp_path / f"store-{adapter_id}"),
    ).validate()
    adapter = MatrixAdapter(config)
    capture_logger = logging.getLogger(f"medre.matrix-trust.{adapter_id}")
    handler = _RecordingHandler()
    capture_logger.addHandler(handler)
    capture_logger.setLevel(logging.DEBUG)
    return _PhaseRig(
        adapter=adapter,
        published=[],
        handler=handler,
        capture_logger=capture_logger,
        store_path=tmp_path / f"store-{adapter_id}",
    )


# ---------------------------------------------------------------------------
# Scenario
# ---------------------------------------------------------------------------


async def test_peer_relay_gate_qualification(trust_env: Any, tmp_path: Path) -> None:
    """Untrusted envelope dropped, listed relay admitted, malformed dropped,
    ordinary message never gated — on a real homeserver."""
    env = trust_env
    _CLAIM_MARKER = "evt-claim-marker"

    # -- Phase A: empty trust list trusts nobody -------------------------
    untrusted_rig = await _make_rig(
        env,
        tmp_path,
        adapter_id="trust-unlisted",
        trusted_relays=(),
    )
    try:
        await untrusted_rig.start()

        # Case 1: an unlisted relay account's MEDRE envelope is dropped.
        _send_message(env, env.relay_access_token, _coherent_envelope_content())
        assert await wait_until(
            lambda: untrusted_rig.adapter.diagnostics()[
                "inbound_filtered_untrusted_relay"
            ]
            >= 1,
            timeout=_WAIT_PUBLISHED,
        )
        assert untrusted_rig.published == []
        assert (
            untrusted_rig.adapter.diagnostics()[
                "inbound_filtered_invalid_relay_envelope"
            ]
            == 0
        )
        remedy = [
            message
            for message in untrusted_rig.handler.messages()
            if "trusted_relays" in message and "not authenticated" in message
        ]
        assert remedy, "expected the remedy warning for the untrusted drop"
        assert env.relay_user_id in remedy[0]
        assert _CLAIM_MARKER not in "".join(untrusted_rig.handler.messages())

        # Case 4: an ordinary message from an unlisted account is relayed.
        _send_message(
            env,
            env.other_access_token,
            {"msgtype": "m.text", "body": "ordinary room chatter"},
        )
        assert await wait_until(
            lambda: len(untrusted_rig.published) >= 1,
            timeout=_WAIT_PUBLISHED,
        )
        assert (
            untrusted_rig.adapter.diagnostics()["inbound_filtered_untrusted_relay"] == 1
        )
    finally:
        await untrusted_rig.stop()

    # -- Phase B: the relay account is listed ----------------------------
    listed_rig = await _make_rig(
        env,
        tmp_path,
        adapter_id="trust-listed",
        trusted_relays=(env.relay_user_id,),
    )
    try:
        await listed_rig.start()
        diag = listed_rig.adapter.diagnostics()
        assert diag["trusted_relay_count"] == 1

        # Case 2: the same account, now listed, is relayed.
        _send_message(env, env.relay_access_token, _coherent_envelope_content())
        assert await wait_until(
            lambda: len(listed_rig.published) >= 1,
            timeout=_WAIT_PUBLISHED,
        )
        assert listed_rig.adapter.diagnostics()["inbound_filtered_untrusted_relay"] == 0
        assert (
            listed_rig.adapter.diagnostics()["inbound_filtered_invalid_relay_envelope"]
            == 0
        )

        # Case 3: the listed account's malformed envelope is dropped.
        _send_message(env, env.relay_access_token, _malformed_envelope_content())
        assert await wait_until(
            lambda: listed_rig.adapter.diagnostics()[
                "inbound_filtered_invalid_relay_envelope"
            ]
            >= 1,
            timeout=_WAIT_PUBLISHED,
        )
        assert len(listed_rig.published) == 1
        version_warning = [
            message
            for message in listed_rig.handler.messages()
            if "peer relay's version" in message
        ]
        assert version_warning, "expected the version-check warning"
        assert env.relay_user_id in version_warning[0]
    finally:
        await listed_rig.stop()
