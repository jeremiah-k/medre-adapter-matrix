"""Ordered batch acceptance, cancellation and real SDK store adoption."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from medre.adapters.matrix.durable_sync import (
    MatrixBatchAcceptanceError,
    MatrixBatchConsumer,
    current_record,
    open_sync,
)
from medre.adapters.matrix.session import MatrixSession
from medre.core.ingress.types import AdapterCheckpoint, DurableIngressDeferredError
from medre_matrix_tests.helpers.async_utils import wait_until
from medre_matrix_tests.helpers.matrix_session import make_matrix_config


class Owner:
    """In-memory committed queue with a cancellable SDK-shaped producer."""

    def __init__(self, records: tuple[object, ...]) -> None:
        """Keep one committed batch available until its acknowledgement succeeds."""
        self.batch = SimpleNamespace(stream_id=uuid4(), sequence=1, records=records)
        self.queue = deque([self.batch])
        self.dispatch = AsyncMock()
        self.ack = AsyncMock(side_effect=lambda _batch: self.queue.popleft())
        self.retired = asyncio.Event()

    async def run(self) -> None:
        """Model a source whose retirement can be observed after cancellation."""
        try:
            await asyncio.Event().wait()
        finally:
            self.retired.set()

    async def next_batch(self) -> object | None:
        """Return committed work without claiming or consuming it."""
        return self.queue[0] if self.queue else None

    async def wait_for_work(self) -> None:
        """Model an idle source that remains cancellable."""
        await asyncio.Event().wait()


def record(name: str = "one", kind: str = "timeline") -> SimpleNamespace:
    """Build a native record with independently declared provenance."""
    return SimpleNamespace(kind=kind, name=name, provenance="recovered")


async def cancel(task: asyncio.Task[None]) -> None:
    """Join cancelled test work so failures cannot leak tasks into later tests."""
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_deferred_record_blocks_ack_and_does_not_repeat_accepted_prefix() -> None:
    """Capacity backpressure retains the current record without replaying accepted siblings."""
    first, second = record("first"), record("second")
    owner = Owner((first, second))
    blocked, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def dispatch(item: object) -> None:
        """Reject the second record until the capacity owner releases it."""
        seen.append(item)
        if item is second and not release.is_set():
            blocked.set()
            raise DurableIngressDeferredError("event", ("capacity_full",))

    owner.dispatch.side_effect = dispatch
    accepted = AsyncMock()
    consumer = MatrixBatchConsumer(
        owner, accept_loss=AsyncMock(), accepted=accepted, retry_delay=0
    )
    task = asyncio.create_task(consumer.run())
    try:
        await asyncio.wait_for(blocked.wait(), 1)
        owner.ack.assert_not_awaited()
        accepted.assert_not_awaited()
        release.set()
        assert await wait_until(lambda: consumer.last_acked_sequence == 1)
        assert seen.count(first) == 1
        assert seen[-1] is second
        accepted.assert_awaited_once_with(owner.batch)
    finally:
        await cancel(task)
    assert owner.retired.is_set()


@pytest.mark.parametrize("failure_site", ["dispatch", "evidence"])
async def test_unexpected_failure_retains_committed_batch(failure_site: str) -> None:
    """Application failures stop consumption without losing committed native work."""
    owner = Owner((record(),))
    accepted = AsyncMock()
    original = ValueError("application failure")
    if failure_site == "dispatch":
        owner.dispatch.side_effect = original
    else:
        accepted.side_effect = original
    consumer = MatrixBatchConsumer(owner, accept_loss=AsyncMock(), accepted=accepted)
    with pytest.raises(MatrixBatchAcceptanceError) as raised:
        await consumer.run()
    assert raised.value.__cause__ is original
    owner.ack.assert_not_awaited()
    assert len(owner.queue) == 1
    assert owner.retired.is_set()
    assert current_record.get() is None


async def test_cancellation_during_admission_retains_batch_and_retires_source() -> None:
    """Cancellation joins both tasks while retaining the unacknowledged batch."""
    owner = Owner((record(),))
    entered, retired = asyncio.Event(), asyncio.Event()

    async def dispatch(item: object) -> None:
        """Expose record-local provenance while admission remains blocked."""
        assert current_record.get() is item
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            retired.set()

    owner.dispatch.side_effect = dispatch
    consumer = MatrixBatchConsumer(owner, accept_loss=AsyncMock(), accepted=AsyncMock())
    task = asyncio.create_task(consumer.run())
    await asyncio.wait_for(entered.wait(), 1)
    assert current_record.get() is None
    await cancel(task)
    owner.ack.assert_not_awaited()
    assert retired.is_set() and owner.retired.is_set()


async def test_loss_is_persisted_before_ack_and_restored_from_application_evidence() -> (
    None
):
    """Gap loss survives acknowledgement and application restart."""
    checkpoints = {}

    async def commit(stream: str, cursor: str, metadata: str) -> None:
        """Capture the exact checkpoint payload accepted by the host."""
        checkpoints[stream] = AdapterCheckpoint(
            "matrix", stream, cursor, metadata, "now"
        )

    session = MatrixSession(
        make_matrix_config(),
        admission_callback=AsyncMock(),
        checkpoint_loader=AsyncMock(return_value=None),
        checkpoint_committer=commit,
    )
    item = SimpleNamespace(
        kind="loss", room_id="!r:example.org", source={"reason": "page_limit"}
    )
    owner = Owner((item,))
    owner.batch.completes_sync = True
    consumer = MatrixBatchConsumer(
        owner,
        accept_loss=session._accept_sync_loss,
        accepted=session._accept_sync_batch,
    )
    task = asyncio.create_task(consumer.run())
    try:
        assert await wait_until(lambda: consumer.last_acked_sequence == 1)
    finally:
        await cancel(task)
    assert "page_limit" in checkpoints["nio_durable"].metadata_json
    session._checkpoint_loader = AsyncMock(
        side_effect=lambda stream: checkpoints.get(stream)
    )
    await session._load_classic_checkpoint()
    assert session._recovery_abandoned_rooms == {"!r:example.org": ("page_limit",)}


def sync_body() -> bytes:
    """One real SDK response with membership and an independently known message."""
    return json.dumps(
        {
            "next_batch": "position-2",
            "rooms": {
                "join": {
                    "!r:example.org": {
                        "state": {
                            "events": [
                                {
                                    "type": "m.room.member",
                                    "state_key": "@alice:example.org",
                                    "sender": "@alice:example.org",
                                    "event_id": "$join",
                                    "origin_server_ts": 1,
                                    "content": {"membership": "join"},
                                }
                            ]
                        },
                        "timeline": {
                            "events": [
                                {
                                    "type": "m.room.message",
                                    "sender": "@bob:example.org",
                                    "event_id": "$known",
                                    "origin_server_ts": 2,
                                    "content": {
                                        "msgtype": "m.text",
                                        "body": "known text",
                                    },
                                }
                            ]
                        },
                    }
                }
            },
        }
    ).encode()


@pytest.mark.matrix_sdk
async def test_real_sdk_adoption_preserves_keys_cursor_and_unacknowledged_records(
    tmp_path: Path,
) -> None:
    """SDK adoption retains device keys and replays the exact committed native batch."""
    import nio

    from medre.adapters.matrix.client_lifecycle import close_matrix_client

    user, device = "@alice:example.org", "DEVICE"
    ordinary = nio.AsyncClient(
        "https://example.org", user, device, store_path=str(tmp_path)
    )
    ordinary.restore_login(user, device, "test-token")
    keys = ordinary.olm.account.identity_keys
    await close_matrix_client(ordinary)

    def fresh() -> object:
        """Restore an authenticated client before opening its durable store."""
        client = nio.AsyncClient("https://example.org", user, device, store_path=None)
        client.restore_login(user, device, "test-token")
        return client

    def attach(client: object) -> object:
        """Open the stable instance binding using the saved legacy cursor."""
        return open_sync(
            client,
            adapter_id="matrix",
            homeserver="https://example.org",
            store_path=tmp_path,
            sync_timeout_ms=1000,
            legacy_cursor="position-1",
        )

    first = fresh()
    owner = attach(first)
    try:
        assert owner.cursor == "position-1"
        assert first.olm.account.identity_keys == keys
        await owner._accept_response(sync_body())
        batch = await owner.next_batch()
        while batch is not None and not any(
            r.source.get("event_id") == "$known" for r in batch.records
        ):
            await owner.ack(batch)
            batch = await owner.next_batch()
        assert batch is not None
        stream, sequence = batch.stream_id, batch.sequence
    finally:
        await owner.close()
        await first.close()

    second = fresh()
    owner = attach(second)
    try:
        assert second.olm.account.identity_keys == keys
        replay = await owner.next_batch()
        assert (replay.stream_id, replay.sequence) == (stream, sequence)
        assert replay.records == batch.records
        assert any(r.source.get("event_id") == "$known" for r in replay.records)
    finally:
        await owner.close()
        await second.close()


@pytest.mark.matrix_sdk
async def test_real_session_dispatch_preserves_provenance_and_admits_before_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real SDK parsing reaches admission with event-time provenance intact."""
    config = replace(
        make_matrix_config(),
        user_id="@alice:example.org",
        device_id="DEVICE",
        store_path=str(tmp_path),
        encryption_mode="plaintext",
    )
    admitted = AsyncMock()
    session = MatrixSession(
        config,
        admission_callback=admitted,
        checkpoint_loader=AsyncMock(return_value=None),
        checkpoint_committer=AsyncMock(),
    )
    # Exercise real construction, callback wiring, database and record parsing;
    # only network polling is held so a deterministic captured response can enter.
    monkeypatch.setattr(MatrixSession, "_run_sync", AsyncMock(side_effect=lambda: None))
    consumer_task = None
    try:
        await session._start_durable(start_sync=False)
        await session._durable_owner._accept_response(sync_body())
        consumer_task = asyncio.create_task(session._batch_consumer.consume())
        assert await wait_until(lambda: admitted.await_count == 1)
        event, provenance = admitted.await_args.args
        assert (event["event_id"], event["body"]) == ("$known", "known text")
        assert provenance == "history"
        assert (
            session._client.olm is not None
        )  # storage backend, even in plaintext policy
        assert not session.crypto_enabled
    finally:
        if consumer_task is not None:
            await cancel(consumer_task)
        await session.stop()


async def test_progress_callback_runs_only_after_successful_sdk_ack() -> None:
    """Persisted application evidence cannot stand in for an SDK acknowledgement."""
    owner = Owner((record(),))
    owner.ack.side_effect = OSError("ack commit failed")
    progress = []
    accepted = AsyncMock()
    consumer = MatrixBatchConsumer(
        owner,
        accept_loss=AsyncMock(),
        accepted=accepted,
        acknowledged=lambda batch: progress.append(batch.sequence),
    )
    with pytest.raises(OSError, match="ack commit failed"):
        await consumer.run()
    accepted.assert_awaited_once()
    assert progress == []
    assert consumer.last_acked_sequence is None


@pytest.mark.matrix_sdk
async def test_real_stop_during_admission_releases_lease_and_preserves_batch(
    tmp_path: Path,
) -> None:
    """Stopping a blocked consumer preserves the exact native work for restart."""
    entered = asyncio.Event()

    async def blocked(*_args: object) -> None:
        """Hold admission open so shutdown must retire an active consumer."""
        entered.set()
        await asyncio.Event().wait()

    config = replace(
        make_matrix_config(),
        user_id="@alice:example.org",
        device_id="DEVICE",
        store_path=str(tmp_path),
        encryption_mode="plaintext",
    )
    session = MatrixSession(
        config,
        admission_callback=blocked,
        checkpoint_loader=AsyncMock(return_value=None),
        checkpoint_committer=AsyncMock(),
    )
    restarted = None
    try:
        await session._start_durable(start_sync=False)
        keys = session._client.olm.account.identity_keys
        await session._durable_owner._accept_response(sync_body())
        session._sync_task = asyncio.create_task(session._batch_consumer.run())
        await asyncio.wait_for(entered.wait(), 1)
        pending = await session._durable_owner.next_batch()
        await asyncio.wait_for(session.stop(timeout=1), 2)
        assert session._client is None or session._client.store.database.is_closed()
        restarted = MatrixSession(
            config,
            admission_callback=AsyncMock(),
            checkpoint_loader=AsyncMock(return_value=None),
            checkpoint_committer=AsyncMock(),
        )
        await restarted._start_durable(start_sync=False)
        assert restarted._client.olm.account.identity_keys == keys
        replay = await restarted._durable_owner.next_batch()
        assert (replay.stream_id, replay.sequence, replay.records) == (
            pending.stream_id,
            pending.sequence,
            pending.records,
        )
    finally:
        await session.stop()
        if restarted is not None:
            await restarted.stop()


@pytest.mark.matrix_sdk
async def test_authenticated_setup_reopens_adopted_store_without_replacing_device_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated authenticated setup uses durable ownership after runtime adoption."""
    import medre.adapters.matrix.e2ee_bootstrap as bootstrap
    from medre.adapters.matrix.auth import MatrixLoginResult
    from medre.adapters.matrix.identity import MatrixCrossSigningDiagnostics

    config = replace(
        make_matrix_config(),
        user_id="@alice:example.org",
        device_id="DEVICE",
        store_path=str(tmp_path),
        encryption_mode="plaintext",
    )
    session = MatrixSession(
        config,
        admission_callback=AsyncMock(),
        checkpoint_loader=AsyncMock(return_value=None),
        checkpoint_committer=AsyncMock(),
    )
    await session._start_durable(start_sync=False)
    keys = session._client.olm.account.identity_keys
    await session.stop()
    observed = []

    class Service:
        """Hold network identity reconciliation at its existing tested seam."""

        def __init__(self, client: object, **_kwargs: object) -> None:
            """Observe the restored device account independently of signing policy."""
            observed.append(client.olm.account.identity_keys)

        async def reconcile(self, **kwargs: object) -> str:
            """Refuse an implicit reset while reporting an existing valid identity."""
            assert not kwargs["reset"]
            return "already_signed"

        def diagnostics(self) -> object:
            """Report the signing postcondition expected by authenticated setup."""
            return MatrixCrossSigningDiagnostics(chain_status="valid")

    monkeypatch.setattr(bootstrap, "MatrixCrossSigningService", Service)
    monkeypatch.setattr(bootstrap, "matrix_store_path_for_adapter", lambda _: tmp_path)
    login = MatrixLoginResult(config.homeserver, config.user_id, "DEVICE", "test-token")
    for _ in range(2):
        await bootstrap.bootstrap_login_cross_signing(
            login, "test-password", adapter_id=config.adapter_id
        )
    assert observed == [keys, keys]


@pytest.mark.matrix_sdk
async def test_live_typing_is_not_replayed_with_durable_batches(tmp_path: Path) -> None:
    """The SDK sends transient actor lists only for a fresh captured response."""
    config = replace(
        make_matrix_config(),
        user_id="@alice:example.org",
        device_id="DEVICE",
        store_path=str(tmp_path),
        encryption_mode="plaintext",
    )
    typing = AsyncMock()
    session = MatrixSession(
        config,
        admission_callback=AsyncMock(),
        typing_callback=typing,
        checkpoint_loader=AsyncMock(return_value=None),
        checkpoint_committer=AsyncMock(),
    )
    await session._start_durable(start_sync=False)
    body = json.loads(sync_body())
    body["rooms"]["join"]["!r:example.org"]["ephemeral"] = {
        "events": [{"type": "m.typing", "content": {"user_ids": ["@bob:example.org"]}}]
    }
    try:
        await session._durable_owner._accept_response(json.dumps(body).encode())
        typing.assert_awaited_once_with("!r:example.org", ["@bob:example.org"])
        batch = await session._durable_owner.next_batch()
        for item in batch.records:
            await session._durable_owner.dispatch(item)
        typing.assert_awaited_once()
    finally:
        await session.stop()


@pytest.mark.parametrize(
    "metadata", ["{}", '{"schema_version":true,"abandoned_rooms":{}}', "[]"]
)
async def test_corrupt_durable_evidence_fails_closed(metadata: str) -> None:
    """An accepted-loss checkpoint must not disappear behind a startup warning."""
    from medre.adapters.matrix.errors import MatrixConnectionError

    checkpoint = AdapterCheckpoint("matrix", "nio_durable", "stream:1", metadata, "now")
    loader = AsyncMock(
        side_effect=lambda stream: checkpoint if stream == "nio_durable" else None
    )
    session = MatrixSession(
        make_matrix_config(),
        admission_callback=AsyncMock(),
        checkpoint_loader=loader,
        checkpoint_committer=AsyncMock(),
    )
    with pytest.raises(MatrixConnectionError, match="checkpoint evidence"):
        await session._load_classic_checkpoint()


async def test_durable_checkpoint_example_and_written_metadata_match_schema() -> None:
    """The operator-facing evidence schema describes the actual committed payload."""
    import jsonschema

    root = Path(__file__).resolve().parents[1] / "docs" / "schemas"
    schema = json.loads((root / "matrix-durable-checkpoint.schema.json").read_text())
    example = json.loads(
        (root / "examples" / "matrix-durable-checkpoint-example.json").read_text()
    )
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(example, schema)
    committer = AsyncMock()
    session = MatrixSession(
        make_matrix_config(),
        admission_callback=AsyncMock(),
        checkpoint_loader=AsyncMock(),
        checkpoint_committer=committer,
    )
    await session._accept_sync_batch(SimpleNamespace(stream_id=uuid4(), sequence=1))
    jsonschema.validate(json.loads(committer.await_args.args[2]), schema)
    assert session.last_successful_sync is None


@pytest.mark.matrix_sdk
async def test_reopening_adopted_store_ignores_obsolete_application_cursor(
    tmp_path: Path,
) -> None:
    """A crash between SDK adoption and app evidence cannot rewind SDK state."""
    import nio

    def fresh():
        """Restore an authenticated client before opening its durable store."""
        client = nio.AsyncClient(
            "https://example.org", "@alice:example.org", "DEVICE", store_path=None
        )
        client.restore_login("@alice:example.org", "DEVICE", "test-token")
        return client

    def attach(client):
        """Open the stable instance binding using the saved legacy cursor."""
        return open_sync(
            client,
            adapter_id="matrix",
            homeserver="https://example.org",
            store_path=tmp_path,
            sync_timeout_ms=1000,
            legacy_cursor="obsolete",
        )

    client = fresh()
    owner = attach(client)
    try:
        # Seed independently committed SDK state without a live homeserver.
        with owner._store.transaction():
            owner._store.set_cursor("sdk-position")
    finally:
        await owner.close()
        await client.close()
    client = fresh()
    owner = attach(client)
    try:
        assert owner.cursor == "sdk-position"
    finally:
        await owner.close()
        await client.close()


@pytest.mark.matrix_sdk
async def test_legacy_pending_recovery_refuses_adoption_without_erasing_work(
    tmp_path: Path,
) -> None:
    """Legacy recovery is a migration blocker, not state the host may discard."""
    import nio
    from nio.exceptions import LocalProtocolError
    from nio.store import DefaultStore

    legacy = DefaultStore("@alice:example.org", "DEVICE", str(tmp_path))
    legacy.database.execute_sql(
        "CREATE TABLE pendingtimelineevents (generation INTEGER, admission_accepted INTEGER)"
    )
    legacy.database.execute_sql("INSERT INTO pendingtimelineevents VALUES (1,0)")
    legacy.database.close()
    client = nio.AsyncClient(
        "https://example.org", "@alice:example.org", "DEVICE", store_path=None
    )
    client.restore_login("@alice:example.org", "DEVICE", "test-token")
    try:
        with pytest.raises(LocalProtocolError, match="recovery to be drained"):
            open_sync(
                client,
                adapter_id="matrix",
                homeserver="https://example.org",
                store_path=tmp_path,
                sync_timeout_ms=1000,
                legacy_cursor="saved",
            )
    finally:
        await client.close()
    legacy = DefaultStore("@alice:example.org", "DEVICE", str(tmp_path))
    try:
        assert legacy.database.execute_sql(
            "SELECT generation,admission_accepted FROM pendingtimelineevents"
        ).fetchall() == [(1, 0)]
    finally:
        legacy.database.close()


@pytest.mark.parametrize("disposed", [False, True])
async def test_transport_retry_reuses_active_owner_and_reopens_disposed_owner(
    disposed, monkeypatch
):
    """An HTTP outage avoids signing setup; poisoned preparation gets a fresh owner."""
    import medre.adapters.matrix.session as module

    session = MatrixSession(
        make_matrix_config(),
        admission_callback=AsyncMock(),
        checkpoint_loader=AsyncMock(),
        checkpoint_committer=AsyncMock(),
    )
    session._client = SimpleNamespace(device_id="DEVICE")

    class Stream:
        @property
        def cursor(self):
            """Expose whether the SDK owner remains usable after the failed attempt."""
            if disposed:
                raise RuntimeError("client disposed during preparation")
            return "stored-position"

    session._durable_owner = Stream()
    attempts = []

    async def attempt():
        """Fail one source attempt before allowing supervision to stop."""
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("temporary connection failure")
        session._stop_requested = True

    reopen, close = AsyncMock(), AsyncMock()
    monkeypatch.setattr(
        MatrixSession, "_run_sync_forever_attempt", lambda self: attempt()
    )
    monkeypatch.setattr(MatrixSession, "_start_durable", reopen)
    monkeypatch.setattr(MatrixSession, "_close_client", close)
    real_sleep = asyncio.sleep

    async def yield_once(_delay):
        """Yield deterministically without waiting for reconnect backoff."""
        await real_sleep(0)

    monkeypatch.setattr(
        module, "asyncio", SimpleNamespace(**(vars(asyncio) | {"sleep": yield_once}))
    )
    await session._sync_with_reconnect()
    assert len(attempts) == 2
    if disposed:
        reopen.assert_awaited_once_with(start_sync=False, known_device_id="DEVICE")
        close.assert_awaited_once()
    else:
        reopen.assert_not_awaited()
        close.assert_not_awaited()


async def test_deferred_batch_evidence_retries_without_redispatch() -> None:
    """A deferred checkpoint must not discard its successfully admitted records."""
    owner = Owner((record(),))
    attempted = 0

    async def commit(_batch: object) -> None:
        """Defer evidence once after record admission has already succeeded."""
        nonlocal attempted
        attempted += 1
        if attempted == 1:
            raise DurableIngressDeferredError("checkpoint", ("capacity_full",))

    consumer = MatrixBatchConsumer(
        owner, accept_loss=AsyncMock(), accepted=commit, retry_delay=0
    )
    task = asyncio.create_task(consumer.consume())
    try:
        assert await wait_until(lambda: consumer.last_acked_sequence == 1)
        assert attempted == 2
        owner.dispatch.assert_awaited_once()
    finally:
        await cancel(task)


@pytest.mark.matrix_sdk
async def test_real_sdk_callback_deferral_preserves_committed_batch(
    tmp_path: Path,
) -> None:
    """The actual nio dispatch path must not wrap media admission deferrals."""
    import nio

    from medre.adapters.matrix.errors import MatrixAttachmentFetchDeferredError

    user, device = "@alice:example.org", "DEVICE"
    client = nio.AsyncClient("https://example.org", user, device, store_path=None)
    client.restore_login(user, device, "test-token")
    calls = []

    async def defer(_room: object, event: object) -> None:
        """Reject native admission without granting SDK acknowledgement."""
        calls.append(event)
        raise MatrixAttachmentFetchDeferredError("$known", ("temporary-media",))

    client.add_event_callback(defer, nio.RoomMessageText)
    owner = open_sync(
        client,
        adapter_id="matrix",
        homeserver="https://example.org",
        store_path=tmp_path,
        sync_timeout_ms=1000,
        legacy_cursor=None,
    )
    try:
        await owner._accept_response(sync_body())
        batch = await owner.next_batch()
        while batch is not None and not any(
            item.source.get("event_id") == "$known" for item in batch.records
        ):
            await owner.ack(batch)
            batch = await owner.next_batch()
        assert batch is not None
        message = next(
            item for item in batch.records if item.source.get("event_id") == "$known"
        )
        with pytest.raises(DurableIngressDeferredError):
            await owner.dispatch(message)
        assert len(calls) == 1
        assert (await owner.next_batch()).sequence == batch.sequence
    finally:
        await owner.close()
        await client.close()


@pytest.mark.matrix_sdk
async def test_idle_sdk_sync_produces_acknowledgeable_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A quiet sync must finish with a committed empty progress batch."""
    import nio

    user, device = "@alice:example.org", "DEVICE"
    client = nio.AsyncClient("https://example.org", user, device, store_path=None)
    client.restore_login(user, device, "test-token")
    owner = open_sync(
        client,
        adapter_id="matrix",
        homeserver="https://example.org",
        store_path=tmp_path,
        sync_timeout_ms=1000,
        legacy_cursor=None,
    )

    # The runner performs real network crypto maintenance between a quiet
    # response and its committed empty batch; the property under test is the
    # acknowledgeable progress itself, not key upload against a live server.
    async def _no_crypto_maintenance() -> None:
        """Isolate quiet source progress from unrelated network key maintenance."""
        return None

    monkeypatch.setattr(owner, "_maintain_crypto", _no_crypto_maintenance)
    runner = None
    try:
        owner._source_ready.clear()
        await owner._accept_response(b'{"next_batch":"idle-1","rooms":{}}')
        runner = asyncio.create_task(owner.run())
        assert await wait_until(lambda: owner._store.has_batches(), timeout=3)
        batch = await owner.next_batch()
        assert batch is not None
        assert batch.records == () and batch.completes_sync
        progress = []
        consumer = MatrixBatchConsumer(
            owner,
            accept_loss=AsyncMock(),
            accepted=AsyncMock(),
            acknowledged=lambda b: progress.append(b.sequence),
        )
        # The existing runner has already published the empty batch.
        task = asyncio.create_task(consumer.consume())
        try:
            assert await wait_until(lambda: len(progress) == 1)
            assert await owner.next_batch() is None
        finally:
            await cancel(task)
    finally:
        if runner is not None:
            await cancel(runner)
        await owner.close()
        await client.close()


@pytest.mark.matrix_sdk
async def test_consumer_id_canonicalizes_alias_and_reopens_legacy_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verified credential aliases must preserve the original SDK ownership."""
    import nio
    from nio.durable import DurableSyncConfig, open_durable_sync
    from nio.exceptions import LocalProtocolError
    from nio.store import DefaultStore
    from uuid import NAMESPACE_URL, uuid5

    import medre.adapters.matrix.durable_sync as durable

    user, device = "@alice:example.org", "DEVICE"
    raw = "https://DELEGATE.EXAMPLE.org:443/"
    configured = "https://alias.example.org"
    old_id = uuid5(
        NAMESPACE_URL, "medre:matrix:" + "\0".join((raw.rstrip("/"), user, "matrix"))
    )

    def fresh(server: str) -> object:
        """Restore an authenticated client before opening its durable store."""
        client = nio.AsyncClient(server, user, device, store_path=None)
        client.restore_login(user, device, "test-token")
        return client

    original = fresh(raw)
    owner = open_durable_sync(
        original,
        consumer_id=old_id,
        store_path=tmp_path,
        config=DurableSyncConfig(sync_timeout_ms=1000),
        source_store_class=DefaultStore,
    )
    await owner.close()
    await original.close()

    import medre.config.adapters.matrix_credentials as credentials

    monkeypatch.setattr(
        credentials,
        "load_credentials_json",
        lambda: {
            "homeserver": raw,
            "user_id": user,
            "device_id": device,
            "access_token": "test-token",
        },
    )
    client = fresh(configured)
    reopened = durable.open_sync(
        client,
        adapter_id="matrix",
        homeserver=configured,
        store_path=tmp_path,
        sync_timeout_ms=1000,
        legacy_cursor=None,
    )
    try:
        assert durable._existing_consumer_id(tmp_path, user, device) == old_id
    finally:
        await reopened.close()
        await client.close()

    # An unrelated credential must not authorize a different homeserver.
    monkeypatch.setattr(
        credentials,
        "load_credentials_json",
        lambda: {
            "homeserver": raw,
            "user_id": user,
            "device_id": device,
            "access_token": "a-different-token",
        },
    )
    other = fresh(configured)
    try:
        with pytest.raises(LocalProtocolError, match="consumer identity mismatch"):
            durable.open_sync(
                other,
                adapter_id="matrix",
                homeserver=configured,
                store_path=tmp_path,
                sync_timeout_ms=1000,
                legacy_cursor=None,
            )
    finally:
        await other.close()
