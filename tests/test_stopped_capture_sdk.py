"""The pinned Matrix SDK and stopped capture share one native ownership lease."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from types import SimpleNamespace
from uuid import uuid4

import pytest

from medre.adapters.matrix import stopped_declaration as declaration
from medre.adapters.matrix.plugin import adapter_spec
from medre.maintenance import stopped_declare as maintenance
from medre.maintenance.stopped_snapshot import restore_stopped_snapshot

pytestmark = pytest.mark.matrix_sdk


@pytest.mark.parametrize("store_kind", ["ordinary", "durable"])
async def test_real_store_lease_capture_restore_preserves_identity(
    tmp_path, monkeypatch, store_kind
):
    if os.name == "nt":
        pytest.skip("native SDK lease and account filenames require POSIX")
    nio = pytest.importorskip("nio")
    crypto = pytest.importorskip("nio.crypto")
    if not crypto.ENCRYPTION_ENABLED:
        pytest.skip("Matrix E2EE extra is required")
    root = tmp_path / "installation"
    store = root / "matrix"
    store.mkdir(parents=True)
    credentials = root / "credentials" / "matrix.json"
    credentials.parent.mkdir()
    credentials.write_text(json.dumps({"access_token": "test-credential"}))
    monkeypatch.setattr(declaration, "_credentials_sidecar", lambda: credentials)
    config = SimpleNamespace(
        user_id="@relay:example.org", device_id="DEVICE", store_path=str(store)
    )
    client = nio.AsyncClient(
        "https://example.org",
        config.user_id,
        device_id=config.device_id,
        store_path=str(store),
        config=nio.AsyncClientConfig(encryption_enabled=True),
    )
    from nio.durable import open_durable_sync

    consumer = uuid4()
    sync = None
    if store_kind == "durable":
        client.user_id = config.user_id
        client.access_token = "test-credential"
        sync = open_durable_sync(client, consumer_id=consumer, store_path=store)
    else:
        client.restore_login(config.user_id, config.device_id, "test-credential")
    stream_id = sync.stream_id if sync is not None else None
    identity = client.olm.account.identity_keys
    provider = declaration.declaration()
    try:
        with pytest.raises(maintenance.ExclusionRefused):
            provider.local_exclusion(config, adapter_id="bridge")
    finally:
        if sync is not None:
            await sync.close()
        await client.close()
        if not client.store.database.is_closed():
            client.store.database.close()
    database = root / "core.sqlite"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE retained(value TEXT)")
        connection.execute("INSERT INTO retained VALUES ('delivery fence')")
    monkeypatch.setattr(
        maintenance,
        "_prepare_instances",
        lambda *_: iter([("matrix", "bridge", "real", adapter_spec(), config)]),
    )
    archive = tmp_path / "snapshot"
    plan = maintenance.capture_configured_stopped_state(
        SimpleNamespace(storage=SimpleNamespace(path=str(database))),
        SimpleNamespace(),
        common_root=root,
        snapshot=archive,
        runtime_stopped=True,
    )
    assert any(
        entry.path == "matrix/@relay:example.org_DEVICE.db" for entry in plan.entries
    )
    assert any(entry.path == "credentials/matrix.json" for entry in plan.entries)
    assert not any(entry.path.endswith(".ingest.lock") for entry in plan.entries)
    restored = tmp_path / "restored"
    restore_stopped_snapshot(archive, restored, runtime_stopped=True)
    assert (
        restored / "credentials/matrix.json"
    ).read_bytes() == credentials.read_bytes()
    resumed = nio.AsyncClient(
        "https://example.org",
        config.user_id,
        device_id=config.device_id,
        store_path=str(restored / "matrix"),
        config=nio.AsyncClientConfig(encryption_enabled=True),
    )
    resumed_sync = None
    try:
        if store_kind == "durable":
            resumed.user_id = config.user_id
            resumed.access_token = "test-credential"
            resumed_sync = open_durable_sync(
                resumed, consumer_id=consumer, store_path=restored / "matrix"
            )
            assert resumed_sync.stream_id == stream_id
        else:
            resumed.restore_login(config.user_id, config.device_id, "test-credential")
        assert resumed.olm.account.identity_keys == identity
        with closing(sqlite3.connect(restored / "core.sqlite")) as connection:
            assert connection.execute("SELECT value FROM retained").fetchone() == (
                "delivery fence",
            )
    finally:
        if resumed_sync is not None:
            await resumed_sync.close()
        await resumed.close()
        if resumed.store is not None and not resumed.store.database.is_closed():
            resumed.store.database.close()
