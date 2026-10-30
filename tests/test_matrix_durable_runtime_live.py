"""Opt-in native sync through the real runtime, durable storage and a fake sink.

Set MATRIX_HOMESERVER, MATRIX_USER_ID, MATRIX_ACCESS_TOKEN, MATRIX_ROOM_ID and
MATRIX_PEER_ACCESS_TOKEN to independent local-homeserver accounts. Set
MATRIX_ENCRYPTED=1 for the genuine Megolm variant with fresh account/device stores. The existing
core Synapse starter can supply these values. This test never injects callbacks
when native sync fails: missing real ingress is a failure.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
import sqlite3
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import pytest
from medre.config.loader import load_config
from medre.runtime.builder import RuntimeBuilder
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre_matrix_tests.helpers.async_utils import wait_until

pytestmark = [pytest.mark.local_integration, pytest.mark.matrix_sdk]


@asynccontextmanager
async def native_peer(http, homeserver, token, room, store_path, *, encrypted):
    """Own an independent sender; encrypted runs use genuine native Megolm."""
    if not encrypted:
        yield None
        return
    import nio
    from medre.adapters.matrix.client_lifecycle import close_matrix_client

    async with http.get(
        f"{homeserver}/_matrix/client/v3/account/whoami",
        headers={"Authorization": f"Bearer {token}"},
    ) as response:
        assert response.status == 200
        identity = await response.json()
    store_path.mkdir()
    client = nio.AsyncClient(
        homeserver,
        identity["user_id"],
        identity["device_id"],
        store_path=str(store_path),
        config=nio.AsyncClientConfig(encryption_enabled=True, max_timeouts=0),
    )
    task = None
    try:
        client.restore_login(identity["user_id"], identity["device_id"], token)
        await client.keys_upload()
        await client.sync(timeout=0, full_state=True)
        assert client.rooms[room].encrypted
        task = asyncio.create_task(client.sync_forever(timeout=1000))
        yield client
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await close_matrix_client(client)


async def test_durable_runtime_native_ingress_restart_and_device_continuity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native events route once through durable core admission across a restart."""
    required = (
        "MATRIX_HOMESERVER",
        "MATRIX_USER_ID",
        "MATRIX_ACCESS_TOKEN",
        "MATRIX_ROOM_ID",
        "MATRIX_PEER_ACCESS_TOKEN",
    )
    if not all(os.environ.get(name) for name in required):
        pytest.skip("explicit local Matrix accounts are required")
    import aiohttp
    import yaml

    homeserver, user, token, room, peer_token = (os.environ[name] for name in required)
    encrypted = os.environ.get("MATRIX_ENCRYPTED") == "1"
    reject_encrypted = encrypted and os.environ.get("MATRIX_PLAINTEXT_POLICY") == "1"
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path / "home"))
    config_file = tmp_path / "bridge.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "runtime": {"name": "durable-sdk-qualification"},
                "storage": {"backend": "sqlite"},
                "adapters": {
                    "matrix": {
                        "native": {
                            "adapter_kind": "real",
                            "homeserver": homeserver,
                            "user_id": user,
                            "access_token": token,
                            "room_allowlist": [room],
                            "encryption_mode": (
                                "e2ee_required"
                                if encrypted and not reject_encrypted
                                else "plaintext"
                            ),
                            "sync_timeout_ms": 1000,
                        },
                        "sink": {
                            "adapter_kind": "fake",
                            "homeserver": "https://unused.invalid",
                            "user_id": "@witness:example.org",
                            "access_token": "fake",
                        },
                    }
                },
                "routes": {
                    "native_to_witness": {
                        "source_adapters": ["native"],
                        "dest_adapters": ["sink"],
                        "directionality": "source_to_dest",
                        "context_map": {room: {"dest_context": "!sink:example.org"}},
                    }
                },
            }
        )
    )
    config_file.chmod(0o600)
    config, _, paths = load_config(config_file)
    native_ids = []
    keys = None
    async with aiohttp.ClientSession() as http:
        if encrypted:
            async with http.put(
                f"{homeserver}/_matrix/client/v3/rooms/{quote(room, safe='')}/state/m.room.encryption",
                headers={"Authorization": f"Bearer {token}"},
                json={"algorithm": "m.megolm.v1.aes-sha2"},
            ) as response:
                assert response.status == 200
        async with native_peer(
            http,
            homeserver,
            peer_token,
            room,
            tmp_path / "peer-store",
            encrypted=encrypted,
        ) as peer:
            for generation in range(2):
                app = RuntimeBuilder(config, paths).build()
                try:
                    await asyncio.wait_for(app.start(), 30)
                    session = app.adapters["native"]._session
                    assert await wait_until(
                        lambda session=session: session.is_live
                        or session.last_sync_error is not None,
                        timeout=30,
                    )
                    assert session.last_sync_error is None
                    assert session.is_live
                    assert not session.diagnostics().checkpoint_owned_by_medre
                    actual_keys = session._client.olm.account.identity_keys
                    if keys is None:
                        keys = actual_keys
                    else:
                        assert actual_keys == keys
                    marker = f"durable-sync-{generation}-{uuid4().hex}"
                    url = f"{homeserver}/_matrix/client/v3/rooms/{quote(room, safe='')}/send/m.room.message/{uuid4().hex}"
                    if peer is not None:
                        import nio

                        result = await peer.room_send(
                            room,
                            "m.room.message",
                            {"msgtype": "m.text", "body": marker},
                            ignore_unverified_devices=True,
                        )
                        assert isinstance(result, nio.RoomSendResponse)
                        native_ids.append(result.event_id)
                    else:
                        async with http.put(
                            url,
                            headers={"Authorization": f"Bearer {peer_token}"},
                            json={"msgtype": "m.text", "body": marker},
                        ) as response:
                            assert response.status == 200
                            native_ids.append((await response.json())["event_id"])
                    sink = app.adapters["sink"]
                    if reject_encrypted:
                        assert await wait_until(
                            lambda adapter=app.adapters[
                                "native"
                            ], generation=generation: adapter._inbound_filtered_encryption_policy
                            >= 1,
                            timeout=30,
                        )
                        assert sink.delivered_payloads == []
                    else:
                        assert await wait_until(
                            lambda sink=sink, session=session: len(
                                sink.delivered_payloads
                            )
                            >= 1
                            or session.last_sync_error is not None,
                            timeout=30,
                        )
                        assert session.last_sync_error is None
                        assert len(sink.delivered_payloads) == 1
                        assert marker in repr(sink.delivered_payloads[0].payload)
                finally:
                    await asyncio.wait_for(app.stop(), 15)
                storage = SQLiteStorage(str(paths.database_path))
                await storage.initialize()
                try:
                    with closing(sqlite3.connect(paths.database_path)) as database:
                        for native_id in native_ids:
                            rows = database.execute(
                                "SELECT event_id FROM canonical_events WHERE source_native_message_id=?",
                                (native_id,),
                            ).fetchall()
                            if reject_encrypted:
                                assert rows == []
                                continue
                            assert len(rows) == 1
                            read = await storage.read_event_for_replay(
                                rows[0][0], now=datetime.now(timezone.utc)
                            )
                            assert read.status == "retained"
                            event = read.event
                            assert event is not None
                            assert "durable-sync-" in event.payload["body"]
                            assert (
                                database.execute(
                                    "SELECT COUNT(*) FROM delivery_receipts WHERE event_id=? AND target_adapter='sink'",
                                    (rows[0][0],),
                                ).fetchone()[0]
                                >= 1
                            )
                finally:
                    await storage.close()


async def test_delegated_login_reopens_same_native_device_store(tmp_path, monkeypatch):
    """Real TLS discovery/login and alias runtime reuse one device and consumer."""
    required = ("MATRIX_ALIAS_HOMESERVER", "MATRIX_LOGIN_USER", "MATRIX_LOGIN_PASSWORD")
    if not all(os.environ.get(name) for name in required):
        pytest.skip("explicit local delegated-login accounts are required")
    from urllib.parse import urlsplit
    from unittest.mock import AsyncMock
    import nio
    from medre.adapters.matrix.auth import (
        discover_well_known,
        matrix_login,
        save_credentials_json,
    )
    from medre.adapters.matrix.durable_sync import open_sync, _existing_consumer_id
    from medre.adapters.matrix.session import MatrixSession
    from medre.config.adapters.matrix import MatrixConfig

    monkeypatch.setenv("MEDRE_HOME", str(tmp_path / "home"))
    alias = os.environ["MATRIX_ALIAS_HOMESERVER"]
    discovered = await asyncio.to_thread(discover_well_known, urlsplit(alias).netloc)
    assert discovered and discovered != alias
    login = await asyncio.to_thread(
        matrix_login,
        discovered,
        os.environ["MATRIX_LOGIN_USER"],
        os.environ["MATRIX_LOGIN_PASSWORD"],
    )
    save_credentials_json(login)
    client = nio.AsyncClient(
        login.homeserver, login.user_id, login.device_id, store_path=None
    )
    client.restore_login(login.user_id, login.device_id, login.access_token)
    store = tmp_path / "store"
    owner = open_sync(
        client,
        adapter_id="native",
        homeserver=login.homeserver,
        store_path=store,
        sync_timeout_ms=1000,
        legacy_cursor=None,
    )
    try:
        keys = client.olm.account.identity_keys
        consumer_id = _existing_consumer_id(store, login.user_id, login.device_id)
    finally:
        await owner.close()
        await client.close()
    config = MatrixConfig(
        adapter_id="native",
        homeserver=alias,
        user_id=login.user_id,
        device_id=login.device_id,
        access_token=login.access_token,
        store_path=str(store),
        encryption_mode="plaintext",
        sync_timeout_ms=1000,
    )
    for _ in range(2):
        session = MatrixSession(
            config,
            admission_callback=AsyncMock(),
            checkpoint_loader=AsyncMock(return_value=None),
            checkpoint_committer=AsyncMock(),
        )
        try:
            await session.start()
            assert await wait_until(
                lambda session=session: session.is_live
                or session.last_sync_error is not None,
                timeout=30,
            )
            assert session.last_sync_error is None
            identity = await session._client.whoami()
            assert (identity.user_id, identity.device_id) == (
                login.user_id,
                login.device_id,
            )
            assert session._client.olm.account.identity_keys == keys
            assert (
                _existing_consumer_id(store, login.user_id, login.device_id)
                == consumer_id
            )
        finally:
            await session.stop()
