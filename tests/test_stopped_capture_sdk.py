"""The pinned Matrix SDK and stopped capture share one native ownership lease."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from medre.adapters.matrix import stopped_declaration as declaration
from medre.core.events import CanonicalEvent, EventMetadata, NativeMessageRef
from medre.core.events.delivery import DeliveryAttemptProvenance
from medre.core.rendering.radio_text import plan_radio_text
from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.sqlite.storage import SQLiteStorage
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
    from nio.durable import DurableSyncConfig, open_durable_sync

    consumer = uuid4()
    sync = None
    pending_batch = None
    committed_sequence = None
    durable_cursor = None
    progress_generation = None
    if store_kind == "durable":
        client.user_id = config.user_id
        client.access_token = "test-credential"
        sync = open_durable_sync(
            client,
            consumer_id=consumer,
            store_path=store,
            config=DurableSyncConfig(max_batch_records=1),
        )
    else:
        client.restore_login(config.user_id, config.device_id, "test-credential")
    stream_id = sync.stream_id if sync is not None else None
    identity = client.olm.account.identity_keys
    provider = declaration.declaration()
    try:
        if sync is not None:
            # One real response publishes distinct bounded batches. Commit the
            # first message and leave the second pending at the same cursor.
            await sync._accept_response(
                json.dumps(
                    {
                        "next_batch": "restore-position",
                        "rooms": {
                            "join": {
                                "!room:example.org": {
                                    "state": {
                                        "events": [
                                            {
                                                "type": "m.room.member",
                                                "state_key": config.user_id,
                                                "sender": config.user_id,
                                                "event_id": "$joined",
                                                "origin_server_ts": 0,
                                                "content": {"membership": "join"},
                                            }
                                        ]
                                    },
                                    "timeline": {
                                        "events": [
                                            {
                                                "type": "m.room.message",
                                                "event_id": event_id,
                                                "sender": "@alice:example.org",
                                                "origin_server_ts": 1,
                                                "content": {
                                                    "msgtype": "m.text",
                                                    "body": event_id,
                                                },
                                            }
                                            for event_id in (
                                                "$committed-before-restore",
                                                "$pending-before-restore",
                                            )
                                        ]
                                    },
                                }
                            }
                        },
                    }
                ).encode()
            )
            committed_message = False
            batch = await sync.next_batch()
            while batch is not None:
                if any(
                    r.source.get("event_id") == "$pending-before-restore"
                    for r in batch.records
                ):
                    pending_batch = batch
                    break
                committed_message |= any(
                    r.source.get("event_id") == "$committed-before-restore"
                    for r in batch.records
                )
                await sync.ack(batch)
                batch = await sync.next_batch()
            assert committed_message and pending_batch is not None
            committed_sequence = sync._store.acked_sequence
            assert committed_sequence > 0
            assert pending_batch.sequence > committed_sequence
            durable_cursor = sync.cursor
            assert durable_cursor == "restore-position"
            progress_generation = sync.progress_generation
        with pytest.raises(maintenance.ExclusionRefused):
            provider.local_exclusion(config, adapter_id="bridge")
    finally:
        if sync is not None:
            await sync.close()
        await client.close()
        if not client.store.database.is_closed():
            client.store.database.close()
    # Real core operational state and native Matrix identity are captured as
    # one stopped installation. The radio checkpoint is local evidence, not
    # attached device firmware or permission for automatic retransmission.
    database = root / "core.sqlite"
    core = SQLiteStorage(str(database))
    await core.initialize()
    event = CanonicalEvent(
        event_id="mixed-native-event",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="matrix",
        source_transport_id="@relay:example.org",
        source_channel_id="!room:example.org",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "reply correlation survives restore"},
        metadata=EventMetadata(),
    )
    await core.append(event)
    await core.store_native_ref(
        NativeMessageRef(
            id="matrix-mixed-ref",
            event_id=event.event_id,
            adapter="matrix",
            native_channel_id="!room:example.org",
            native_message_id="$native-target",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
    )
    await core.put_adapter_checkpoint(
        "bridge", "radio_adapter_cursor", "radio-receipt-marker"
    )
    outbox = await core.create_outbox_item(
        DeliveryOutboxItem(
            outbox_id="mixed-radio-obligation",
            event_id=event.event_id,
            route_id="matrix-to-radio",
            delivery_plan_id="radio-plan",
            target_adapter="meshtastic",
            target_channel="0",
            attempt_number=0,
            status="pending",
        )
    )
    claimed = await core.claim_due_outbox_items(
        datetime.now(timezone.utc).isoformat(), "interrupted-native-owner"
    )
    assert [row.outbox_id for row in claimed] == [outbox.outbox_id]
    assert (
        await core.reserve_outbox_attempt(
            outbox.outbox_id, "interrupted-native-owner", 0
        )
        == 1
    )
    provenance = DeliveryAttemptProvenance(
        event.event_id, "radio-plan", "meshtastic", "0", outbox.outbox_id, 1, "live"
    )
    radio_plan = plan_radio_text("uncertain before shutdown", max_text_bytes=72)
    bound = await core.bind_radio_attempt(
        provenance, "interrupted-native-owner", radio_plan
    )
    assert bound is not None
    uncertain = await core.begin_radio_part(bound, "interrupted-native-owner", 1)
    assert uncertain is not None
    assert uncertain.progress.parts[0].state == "submitting"
    saved_row = await core.get_outbox_item(outbox.outbox_id)
    await core.close()
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
            assert resumed_sync.cursor == durable_cursor
            assert resumed_sync._store.acked_sequence == committed_sequence
            assert resumed_sync.progress_generation == progress_generation
            # Compare the complete retained batch, including its sequence and
            # records; stable stream identity alone cannot prove work survived.
            replay = await resumed_sync.next_batch()
            assert replay == pending_batch
            assert replay.sequence > committed_sequence
            await resumed_sync.ack(replay)
            assert await resumed_sync.next_batch() is None
        else:
            resumed.restore_login(config.user_id, config.device_id, "test-credential")
        assert resumed.olm.account.identity_keys == identity
        recovered = SQLiteStorage(str(restored / "core.sqlite"))
        await recovered.initialize()
        try:
            assert (
                await recovered.resolve_native_ref(
                    "matrix", "!room:example.org", "$native-target"
                )
                == event.event_id
            )
            assert (
                await recovered.get_adapter_checkpoint("bridge", "radio_adapter_cursor")
            ).cursor == "radio-receipt-marker"
            assert await recovered.get_outbox_item(outbox.outbox_id) == saved_row
            assert await recovered.get_radio_attempt(outbox.outbox_id, 1) == uncertain
            assert (
                await recovered.bind_radio_attempt(
                    provenance, "interrupted-native-owner", radio_plan
                )
            ) is None, "restored uncertain radio work must not grant a resend"
        finally:
            await recovered.close()
    finally:
        if resumed_sync is not None:
            await resumed_sync.close()
        await resumed.close()
        if resumed.store is not None and not resumed.store.database.is_closed():
            resumed.store.database.close()
