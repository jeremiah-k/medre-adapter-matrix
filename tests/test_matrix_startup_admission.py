"""Classic Sync must not acknowledge startup events without durable admission."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from medre.adapters.matrix import adapter as adapter_module
from medre.adapters.matrix.adapter import MatrixAdapter
from medre.core.ingress import AdmissionResult, DurableIngressDeferredError
from medre_matrix_tests.helpers.matrix_adapter import (
    make_adapter_context,
    make_matrix_config,
)

EVENT = {
    "room_id": "!room:example.com",
    "sender": "@alice:example.com",
    "body": "Arrived while MEDRE was offline",
    "event_id": "$offline",
    "msgtype": "m.text",
    "source": {
        "event_id": "$offline",
        "sender": "@alice:example.com",
        "type": "m.room.message",
        "content": {"msgtype": "m.text", "body": "Arrived while MEDRE was offline"},
    },
}


@pytest.mark.parametrize("provenance", ["live", "recovered", "history"])
async def test_startup_sync_durably_admits_before_start_returns(
    monkeypatch, provenance
):
    published, ctx = make_adapter_context()
    admit = AsyncMock(
        return_value=AdmissionResult(
            event_id="canonical",
            created=True,
            provenance=provenance,
            work_status="suppressed_history" if provenance == "history" else "pending",
        )
    )
    ctx.admit_inbound = admit

    class StartupSession:
        def is_room_encrypted(self, room_id: str) -> bool:
            """Startup admission uses an unencrypted native room."""
            return False

        closed = False
        connected = True
        last_sync_error = None
        last_successful_sync = None
        reconnecting = False
        crypto_enabled = True

        def is_logged_in(self) -> bool:
            return True

        def __init__(self, **kwargs):
            self.callback = kwargs["admission_callback"]

        async def start(self):
            await self.callback(dict(EVENT), provenance)

        async def stop(self, **kwargs):
            self.connected = False
            self.closed = True

    monkeypatch.setattr(adapter_module, "HAS_NIO", True)
    monkeypatch.setattr(adapter_module, "MatrixSession", StartupSession)
    adapter = MatrixAdapter(make_matrix_config())
    try:
        await adapter.start(ctx)
        admit.assert_awaited_once()
        event, actual_provenance = admit.await_args.args
        assert event.source_native_ref.native_message_id == "$offline"
        assert actual_provenance == provenance
        assert published == []
        assert adapter._started and not adapter._starting
        # A connected fake session has not yet acknowledged a native sync.
        assert (await adapter.health_check()).health == "degraded"
    finally:
        await adapter.stop()


@pytest.mark.parametrize("stopped", [False, True])
async def test_inactive_durable_callback_defers_instead_of_acknowledging(stopped):
    _, ctx = make_adapter_context()
    adapter = MatrixAdapter(make_matrix_config())
    if stopped:
        adapter.ctx = ctx
        await adapter.stop()
    with pytest.raises(DurableIngressDeferredError):
        await adapter._on_room_message(dict(EVENT), "live")


async def test_legacy_startup_callback_remains_suppressed(monkeypatch):
    published, ctx = make_adapter_context()

    class StartupSession:
        def is_room_encrypted(self, room_id: str) -> bool:
            """Startup admission uses an unencrypted native room."""
            return False

        closed = False
        connected = True
        last_sync_error = None
        last_successful_sync = None
        reconnecting = False
        crypto_enabled = True

        def is_logged_in(self) -> bool:
            return True

        def __init__(self, **kwargs):
            self.callback = kwargs["message_callback"]

        async def start(self):
            await self.callback(dict(EVENT))

        async def stop(self, **kwargs):
            self.connected = False
            self.closed = True

    monkeypatch.setattr(adapter_module, "HAS_NIO", True)
    monkeypatch.setattr(adapter_module, "MatrixSession", StartupSession)
    adapter = MatrixAdapter(make_matrix_config())
    try:
        await adapter.start(ctx)
        assert published == []
    finally:
        await adapter.stop()


@pytest.mark.parametrize(
    "error", [RuntimeError("join failed"), asyncio.CancelledError()]
)
async def test_auto_join_failure_closes_startup_admission(monkeypatch, error):
    _, ctx = make_adapter_context()
    session = AsyncMock()
    session.closed = False
    session.start = AsyncMock()
    session.ensure_joined_rooms = AsyncMock(side_effect=error)
    session.stop = AsyncMock()
    monkeypatch.setattr(adapter_module, "HAS_NIO", True)
    monkeypatch.setattr(adapter_module, "MatrixSession", lambda **kwargs: session)
    adapter = MatrixAdapter(make_matrix_config(auto_join_rooms=("!room:example.com",)))
    with pytest.raises(type(error)):
        await adapter.start(ctx)
    session.stop.assert_awaited_once()
    assert not adapter._started and not adapter._starting
    assert adapter.ctx is None and adapter._session is None
    with pytest.raises(DurableIngressDeferredError):
        await adapter._on_room_message(dict(EVENT), "live")
