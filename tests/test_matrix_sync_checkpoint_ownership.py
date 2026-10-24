"""Application admission, migration evidence and bounded attachment retry state."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from medre.adapters.matrix.errors import (
    MATRIX_ATTACHMENT_FETCH_DEFERRAL_COUNT_KEY,
    MatrixAttachmentFetchDeferredError,
)
from medre.adapters.matrix.session import (
    _ATTACHMENT_FETCH_DEFERRALS_MAX,
    MatrixSession,
)
from medre.core.ingress.types import AdapterCheckpoint
from medre_matrix_tests.helpers.matrix_session import make_matrix_config


def _durable_session(**overrides: object) -> MatrixSession:
    """Supply all three host-owned ingress and checkpoint callbacks."""

    async def admit(_event: dict[str, object], _provenance: str) -> None:
        """Accept the event without changing the test evidence."""
        return None

    async def load(_stream: str) -> AdapterCheckpoint | None:
        """Return only the checkpoint selected by this test."""
        return None

    async def commit(_stream: str, _cursor: str, _metadata: str) -> None:
        """Capture application checkpoint acceptance."""
        return None

    params: dict[str, object] = {
        "admission_callback": admit,
        "checkpoint_loader": load,
        "checkpoint_committer": commit,
    }
    params.update(overrides)
    return MatrixSession(make_matrix_config(), **params)


def test_durable_client_config_disables_ordinary_token_writes() -> None:
    """Durable ownership excludes ordinary SDK sync-token persistence."""
    captured: dict[str, object] = {}

    def config_factory(**kwargs: object) -> object:
        """Record the SDK settings selected by the provider."""
        captured.update(kwargs)
        return object()

    session = _durable_session()
    nio = SimpleNamespace(AsyncClientConfig=config_factory)
    session._build_client_config(nio, encryption_enabled=False)

    assert captured == {
        "encryption_enabled": False,
        "max_timeouts": 0,
        "store_sync_tokens": False,
    }


def test_standalone_client_config_enables_sdk_token_writes() -> None:
    """Callback-only sessions retain the ordinary SDK token policy."""
    captured: dict[str, object] = {}

    def config_factory(**kwargs: object) -> object:
        """Record the SDK settings selected by the provider."""
        captured.update(kwargs)
        return object()

    session = MatrixSession(make_matrix_config())
    nio = SimpleNamespace(AsyncClientConfig=config_factory)
    session._build_client_config(nio, encryption_enabled=False)

    assert captured == {
        "encryption_enabled": False,
        "max_timeouts": 0,
        "store_sync_tokens": True,
    }


async def test_unsupported_admission_provenance_propagates_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Programming-error path: unknown provenance raises ValueError that
    propagates unchanged for the consumer to retain and report.
    """

    admission = AsyncMock()
    session = _durable_session(admission_callback=admission)
    room = SimpleNamespace(room_id="!room:example.org")
    event = SimpleNamespace(
        sender="@alice:example.org",
        event_id="$event",
        body="hello",
        source={
            "event_id": "$event",
            "sender": "@alice:example.org",
            "type": "m.room.message",
            "content": {"msgtype": "m.text", "body": "hello"},
        },
    )

    with pytest.raises(ValueError, match="unsupported Matrix ingress provenance"):
        await session._on_nio_admission(
            room, event, SimpleNamespace(value="unsupported")
        )

    admission.assert_not_awaited()


async def test_durable_deferred_admission_preserves_host_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The batch consumer receives the original capacity deferral signal."""
    from medre.core.ingress import DurableIngressDeferredError

    async def defer(_event: dict[str, object], _provenance: str) -> None:
        """Retain the native event through an explicit admission deferral."""
        raise DurableIngressDeferredError("$event", ("capacity_full",))

    session = _durable_session(admission_callback=defer)
    room = SimpleNamespace(room_id="!room:example.org")
    event = SimpleNamespace(
        sender="@alice:example.org",
        event_id="$event",
        body="hello",
        source={
            "event_id": "$event",
            "sender": "@alice:example.org",
            "type": "m.room.message",
            "content": {"msgtype": "m.text", "body": "hello"},
        },
    )

    with pytest.raises(DurableIngressDeferredError, match="capacity_full"):
        await session._on_nio_admission(room, event, SimpleNamespace(value="live"))


async def test_attachment_fetch_deferral_count_survives_restart_and_clears_on_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retain media retry counts across restart and clear them only after acceptance."""
    checkpoints: dict[str, AdapterCheckpoint] = {}

    async def load(stream: str) -> AdapterCheckpoint | None:
        """Return only the checkpoint selected by this test."""
        return checkpoints.get(stream)

    async def commit(stream: str, cursor: str, metadata: str) -> None:
        """Capture application checkpoint acceptance."""
        checkpoints[stream] = AdapterCheckpoint(
            adapter_id="matrix-test",
            stream=stream,
            cursor=cursor,
            metadata_json=metadata,
            updated_at="2026-09-26T00:00:00Z",
        )

    room = SimpleNamespace(room_id="!room:example.org")
    event = SimpleNamespace(
        sender="@alice:example.org",
        event_id="$media",
        body="photo.png",
        source={
            "event_id": "$media",
            "sender": "@alice:example.org",
            "type": "m.room.message",
            "content": {"msgtype": "m.image", "body": "photo.png"},
        },
    )

    seen_counts: list[int] = []

    async def defer(normalized: dict[str, object], _provenance: str) -> None:
        """Retain the native event through an explicit admission deferral."""
        seen_counts.append(
            int(normalized.get(MATRIX_ATTACHMENT_FETCH_DEFERRAL_COUNT_KEY, 0))
        )
        raise MatrixAttachmentFetchDeferredError("generated-canonical", ("network",))

    first = _durable_session(
        admission_callback=defer,
        checkpoint_loader=load,
        checkpoint_committer=commit,
    )
    await first._load_attachment_fetch_deferrals()
    with pytest.raises(MatrixAttachmentFetchDeferredError):
        await first._on_nio_admission(room, event, SimpleNamespace(value="live"))
    assert seen_counts == [0]

    accepted_counts: list[int] = []

    async def accept(normalized: dict[str, object], _provenance: str) -> None:
        """Capture the retry count observed at successful admission."""
        accepted_counts.append(
            int(normalized.get(MATRIX_ATTACHMENT_FETCH_DEFERRAL_COUNT_KEY, 0))
        )

    restarted = _durable_session(
        admission_callback=accept,
        checkpoint_loader=load,
        checkpoint_committer=commit,
    )
    await restarted._load_attachment_fetch_deferrals()
    await restarted._on_nio_admission(room, event, SimpleNamespace(value="live"))

    assert accepted_counts == [1]
    saved = checkpoints["matrix_attachment_fetch_deferrals"]
    assert json.loads(saved.metadata_json) == {"attempts": {}}


async def test_attachment_fetch_checkpoint_failure_keeps_native_event_pending(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed retry-state write cannot acknowledge the native event."""

    async def fail_commit(_stream: str, _cursor: str, _metadata: str) -> None:
        """Model an unavailable application evidence store."""
        raise OSError("disk full")

    async def defer(_event: dict[str, object], _provenance: str) -> None:
        """Retain the native event through an explicit admission deferral."""
        raise MatrixAttachmentFetchDeferredError("generated-canonical", ("network",))

    session = _durable_session(
        admission_callback=defer,
        checkpoint_committer=fail_commit,
    )
    room = SimpleNamespace(room_id="!room:example.org")
    event = SimpleNamespace(
        sender="@alice:example.org",
        event_id="$media",
        body="photo.png",
        source={
            "event_id": "$media",
            "sender": "@alice:example.org",
            "type": "m.room.message",
            "content": {"msgtype": "m.image", "body": "photo.png"},
        },
    )

    caplog.set_level("ERROR", logger="medre.adapters.matrix.session")
    with pytest.raises(MatrixAttachmentFetchDeferredError):
        await session._on_nio_admission(room, event, SimpleNamespace(value="live"))

    assert list(session._attachment_fetch_deferrals.values()) == [1]
    assert any(
        "Failed to persist Matrix attachment fetch deferral" in record.message
        for record in caplog.records
    )


async def test_attachment_fetch_deferral_map_is_bounded_with_oldest_eviction() -> None:
    """The persisted deferral map cannot grow without limit.

    Entries normally clear on durable admission, but events nio consumes
    without a further dispatch leave theirs behind; recording past the cap
    evicts the oldest identity first so the checkpoint stays bounded.
    """
    session = _durable_session()
    overflow = 5
    total = _ATTACHMENT_FETCH_DEFERRALS_MAX + overflow
    for index in range(total):
        await session._record_attachment_fetch_deferral(f"{index:064x}")

    assert len(session._attachment_fetch_deferrals) == _ATTACHMENT_FETCH_DEFERRALS_MAX
    assert f"{0:064x}" not in session._attachment_fetch_deferrals
    assert f"{overflow:064x}" in session._attachment_fetch_deferrals
    newest = f"{total - 1:064x}"
    assert session._attachment_fetch_deferrals[newest] == 1

    # Re-recording an existing identity refreshes its recency, so a still
    # failing fetch keeps its budget instead of being evicted as stale.
    await session._record_attachment_fetch_deferral(f"{overflow:064x}")
    await session._record_attachment_fetch_deferral(f"{total:064x}")
    assert f"{overflow + 1:064x}" not in session._attachment_fetch_deferrals
    assert f"{overflow:064x}" in session._attachment_fetch_deferrals
    assert session._attachment_fetch_deferrals[f"{overflow:064x}"] == 2


async def test_unexpected_admission_error_preserves_attachment_retry_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed committed record retains its original acquisition retry state."""

    calls = {"count": 0}

    async def flaky_admission(_event: dict[str, object], _provenance: str) -> None:
        """Defer attachment acquisition before surfacing an application failure."""
        calls["count"] += 1
        if calls["count"] == 1:
            raise MatrixAttachmentFetchDeferredError(
                "generated-canonical", ("network",)
            )
        raise RuntimeError("boom")

    session = _durable_session(admission_callback=flaky_admission)
    room = SimpleNamespace(room_id="!room:example.org")
    event = SimpleNamespace(
        sender="@alice:example.org",
        event_id="$media",
        body="photo.png",
        source={
            "event_id": "$media",
            "sender": "@alice:example.org",
            "type": "m.room.message",
            "content": {"msgtype": "m.image", "body": "photo.png"},
        },
    )

    with pytest.raises(MatrixAttachmentFetchDeferredError):
        await session._on_nio_admission(room, event, SimpleNamespace(value="live"))
    assert list(session._attachment_fetch_deferrals.values()) == [1]

    with pytest.raises(RuntimeError, match="boom"):
        await session._on_nio_admission(room, event, SimpleNamespace(value="live"))
    assert list(session._attachment_fetch_deferrals.values()) == [1]


@pytest.mark.parametrize(
    "metadata_json",
    [
        "[]",
        '{"attempts":[]}',
        '{"attempts":{"not-a-digest":1}}',
        '{"attempts":{"' + "a" * 64 + '":0}}',
        '{"attempts":{"' + "a" * 64 + '":3}}',
        '{"attempts":{"' + "a" * 64 + '":true}}',
    ],
)
async def test_load_attachment_fetch_checkpoint_discards_malformed_metadata(
    metadata_json: str,
) -> None:
    checkpoint = AdapterCheckpoint(
        adapter_id="matrix-test",
        stream="matrix_attachment_fetch_deferrals",
        cursor="v1",
        metadata_json=metadata_json,
        updated_at="2026-09-26T00:00:00Z",
    )

    async def load(_stream: str) -> AdapterCheckpoint:
        """Return only the checkpoint selected by this test."""
        return checkpoint

    logger = MagicMock()
    session = _durable_session(checkpoint_loader=load, logger=logger)

    await session._load_attachment_fetch_deferrals()

    assert session._attachment_fetch_deferrals == {}
    logger.warning.assert_called_once_with(
        "Ignoring malformed Matrix attachment fetch deferral checkpoint"
    )


async def test_unexpected_runtime_error_propagates_and_is_logged(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Programming/storage errors must NOT masquerade as deferral — nio
    sees the original ``RuntimeError`` and wraps it in
    ``_LiveCallbackError(accepted=True)`` (live) or logs+drops (recovered),
    so supervision can stop admission while preserving the committed record.

    The session also logs ``MATRIX_ADMISSION_UNEXPECTED_ERROR`` so the
    operator can correlate the failure.
    """

    original = RuntimeError("sqlite write failed")

    async def reject(_event: dict[str, object], _provenance: str) -> None:
        """Surface the original admission failure without SDK translation."""
        raise original

    session = _durable_session(admission_callback=reject)
    room = SimpleNamespace(room_id="!room:example.org")
    event = SimpleNamespace(
        sender="@alice:example.org",
        event_id="$event",
        body="hello",
        source={
            "event_id": "$event",
            "sender": "@alice:example.org",
            "type": "m.room.message",
            "content": {"msgtype": "m.text", "body": "hello"},
        },
    )

    caplog.set_level("ERROR", logger="medre.adapters.matrix.session")
    with pytest.raises(RuntimeError, match="sqlite write failed") as caught:
        await session._on_nio_admission(room, event, SimpleNamespace(value="recovered"))

    assert caught.value is original
    assert any(
        "MATRIX_ADMISSION_UNEXPECTED_ERROR" in rec.message for rec in caplog.records
    ), [rec.message for rec in caplog.records]
    assert session._recovered_event_count == 1


async def test_admission_deferral_does_not_require_sdk_error_translation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When nio is not importable at all, deferral signals also propagate
    unchanged so nio never sees a translated class it cannot handle.
    """
    from medre.core.ingress import DurableIngressDeferredError

    async def defer(_event: dict[str, object], _provenance: str) -> None:
        """Retain the native event through an explicit admission deferral."""
        raise DurableIngressDeferredError("$event", ("deferred",))

    session = _durable_session(admission_callback=defer)
    monkeypatch.setitem(sys.modules, "nio", SimpleNamespace())
    monkeypatch.delitem(sys.modules, "nio.exceptions", raising=False)

    room = SimpleNamespace(room_id="!room:example.org")
    event = SimpleNamespace(
        sender="@alice:example.org",
        event_id="$event",
        body="hello",
        source={
            "event_id": "$event",
            "sender": "@alice:example.org",
            "type": "m.room.message",
            "content": {"msgtype": "m.text", "body": "hello"},
        },
    )

    with pytest.raises(DurableIngressDeferredError, match="deferred"):
        await session._on_nio_admission(room, event, SimpleNamespace(value="live"))


async def test_stopping_session_does_not_commit_or_ack_sync_response() -> None:
    commits = AsyncMock()
    session = _durable_session(checkpoint_committer=commits)
    client = MagicMock()
    session._client = client
    session._stop_requested = True

    await session._on_sync_response(
        SimpleNamespace(next_batch="s-stop", abandoned_rooms={})
    )

    commits.assert_not_awaited()
    client.acknowledge_classic_sync.assert_not_called()


async def test_load_checkpoint_restores_cursor_without_discarding_sdk_recovery() -> (
    None
):
    """Read legacy cursor evidence without erasing pending SDK recovery."""
    checkpoint = AdapterCheckpoint(
        adapter_id="matrix-test",
        stream="classic_sync",
        cursor="s41",
        metadata_json='{"abandoned_rooms":{"!lost:example.org":["fetch_failed"]}}',
        updated_at="2026-08-18T00:00:00Z",
    )

    async def load(stream: str) -> AdapterCheckpoint | None:
        """Return only the checkpoint selected by this test."""
        if stream in ("matrix_attachment_fetch_deferrals", "nio_durable"):
            return None
        assert stream == "classic_sync"
        return checkpoint

    session = _durable_session(checkpoint_loader=load)
    client = MagicMock()
    client.store = object()
    client.clear_persisted_sync_recovery = MagicMock()
    session._client = client

    await session._load_classic_checkpoint()

    assert session._committed_sync_token == "s41"
    assert session._recovery_abandoned_rooms == {"!lost:example.org": ("fetch_failed",)}
    assert json.loads(session._recovery_last_abandonment or "{}") == {
        "causes": {"fetch_failed": 1},
        "room_count": 1,
    }
    client.clear_persisted_sync_recovery.assert_not_called()


@pytest.mark.parametrize(
    "metadata_json",
    [
        "[]",
        '{"abandoned_rooms":[]}',
        '{"abandoned_rooms":{"!room:example.org":"fetch_failed"}}',
        '{"abandoned_rooms":{"!room:example.org":[1]}}',
    ],
)
async def test_load_checkpoint_warns_and_discards_malformed_nested_metadata(
    metadata_json: str,
) -> None:
    """Malformed legacy loss metadata cannot fabricate recovery facts."""
    checkpoint = AdapterCheckpoint(
        adapter_id="matrix-test",
        stream="classic_sync",
        cursor="s41",
        metadata_json=metadata_json,
        updated_at="2026-08-18T00:00:00Z",
    )

    async def load(stream: str) -> AdapterCheckpoint | None:
        """Return only the checkpoint selected by this test."""
        if stream != "classic_sync":
            return None
        return checkpoint

    logger = MagicMock()
    session = _durable_session(checkpoint_loader=load, logger=logger)
    session._client = MagicMock(store=None)

    await session._load_classic_checkpoint()

    assert session._recovery_abandoned_rooms == {}
    assert session._recovery_last_abandonment is None
    logger.warning.assert_called_once_with(
        "Ignoring malformed Matrix checkpoint recovery metadata"
    )
