"""Native Matrix progress publishes readiness without an explicit health probe."""

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.session import MatrixSession
from medre.config.adapters.matrix import MatrixConfig
from medre.core.contracts.adapter import AdapterContext
from medre_matrix_tests.helpers.async_utils import wait_until


@pytest.fixture
def native_adapter(monkeypatch):
    """Keep real session health transitions while replacing network lifecycle only."""
    monkeypatch.setattr("medre.adapters.matrix.adapter.HAS_NIO", True)

    async def start(session):
        """Authentication alone leaves the real first-ack readiness condition pending."""
        session._closed = False
        session._stop_requested = False
        session._client = SimpleNamespace(logged_in=True)

    async def stop(session, timeout=5.0):
        """Retire the session without opening a real native client."""
        session._stop_requested = True
        session._closed = True
        session._client = None

    monkeypatch.setattr(MatrixSession, "start", start)
    monkeypatch.setattr(MatrixSession, "stop", stop)
    return MatrixAdapter(
        MatrixConfig(
            adapter_id="matrix",
            homeserver="https://example.com",
            user_id="@bot:example.com",
            access_token="test-token",
            encryption_mode="plaintext",
        )
    )


def context(reports):
    """Use the public context sink while retaining compatibility with older core."""
    ctx = AdapterContext(
        "matrix",
        AsyncMock(),
        logging.getLogger("native-health"),
        lambda: datetime.now(timezone.utc),
        asyncio.Event(),
    )
    ctx.report_health = reports.append
    return ctx


async def test_durable_ack_publishes_recovery_without_probe(native_adapter):
    """An acknowledged batch clears the exact waiting reason seen at startup."""
    reports = []
    adapter = native_adapter
    await adapter.start(context(reports))
    try:
        assert reports[-1].health == "degraded"
        assert adapter._health_reasons == ("waiting_initial_sync_ack",)
        adapter._session._on_batch_acknowledged(SimpleNamespace(completes_sync=True))
        assert reports[-1].health == "healthy"
        assert adapter.diagnostics()["health"] == "healthy"
        count = len(reports)
        adapter._session._on_batch_acknowledged(SimpleNamespace(completes_sync=True))
        assert len(reports) == count
    finally:
        await adapter.stop()


async def test_classic_sync_publishes_recovery(native_adapter):
    """Standalone ordinary sync retains the same acknowledged-progress health contract."""
    reports = []
    adapter = native_adapter
    await adapter.start(context(reports))
    try:
        await adapter._session._on_sync_response(SimpleNamespace(next_batch="cursor"))
        assert reports[-1].health == "healthy"
    finally:
        await adapter.stop()


async def test_reconnect_attempt_stays_degraded_until_native_ack(
    native_adapter, monkeypatch
):
    """Beginning another request cannot erase a recorded transport failure."""
    reports = []
    adapter = native_adapter
    await adapter.start(context(reports))
    session = adapter._session
    session._on_batch_acknowledged(SimpleNamespace(completes_sync=True))
    attempts = 0
    entered = asyncio.Event()

    async def attempt(owner):
        """Fail once, then retain the next native request before any acknowledgement."""
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("transport unavailable")
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(MatrixSession, "_run_sync_forever_attempt", attempt)
    monkeypatch.setattr("medre.adapters.matrix.session._BACKOFF_BASE", 0)
    monkeypatch.setattr("medre.adapters.matrix.session._BACKOFF_CAP", 0)
    task = asyncio.create_task(session._sync_with_reconnect())
    try:
        assert await wait_until(entered.is_set)
        assert reports[-1].health == "degraded"
        assert session.reconnecting
        session._on_batch_acknowledged(SimpleNamespace(completes_sync=True))
        assert reports[-1].health == "healthy"
        assert not session.reconnecting
    finally:
        session._stop_requested = True
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await adapter.stop()


async def test_terminal_sync_failure_is_published(native_adapter, monkeypatch):
    """Fatal sync failure is a health observation while adapter ownership remains started."""
    reports = []
    adapter = native_adapter
    await adapter.start(context(reports))

    async def fail(session):
        """Model a non-retryable native failure."""
        raise ValueError("invalid sync state")

    monkeypatch.setattr(MatrixSession, "_run_sync_forever_attempt", fail)
    try:
        await adapter._session._sync_with_reconnect()
        assert reports[-1].health == "failed"
    finally:
        await adapter.stop()


async def test_retired_session_cannot_publish_for_replacement(native_adapter):
    """A callback captured by an earlier start cannot claim the replacement's health."""
    reports = []
    adapter = native_adapter
    await adapter.start(context(reports))
    old = adapter._session._health_callback
    await adapter.stop()
    await adapter.start(context(reports))
    try:
        count = len(reports)
        old()
        assert len(reports) == count
        assert adapter.diagnostics()["health"] == "degraded"
    finally:
        await adapter.stop()


async def test_observer_failure_cannot_interrupt_native_ack(native_adapter):
    """Diagnostics never own sync admission or acknowledgement."""
    adapter = native_adapter
    ctx = context([])

    def refused(info):
        """Model an unavailable optional observer."""
        raise RuntimeError("observer unavailable")

    ctx.report_health = refused
    await adapter.start(ctx)
    try:
        adapter._session._on_batch_acknowledged(SimpleNamespace(completes_sync=True))
        assert adapter.diagnostics()["health"] == "healthy"
        assert adapter._session.last_successful_sync is not None
    finally:
        await adapter.stop()


async def test_probe_and_failed_observer_cannot_suppress_native_report(native_adapter):
    """Only successful notification delivery participates in report deduplication."""
    reports = []
    adapter = native_adapter
    ctx = context(reports)

    def refused(info):
        """Model a temporarily unavailable diagnostic consumer."""
        raise RuntimeError("observer unavailable")

    ctx.report_health = refused
    await adapter.start(ctx)
    try:
        adapter._session._on_batch_acknowledged(SimpleNamespace(completes_sync=True))
        assert (await adapter.health_check()).health == "healthy"
        ctx.report_health = reports.append
        adapter._session._on_batch_acknowledged(SimpleNamespace(completes_sync=True))
        assert reports[-1].health == "healthy"
    finally:
        await adapter.stop()


async def test_unexpected_sync_cancellation_reports_failed(native_adapter, monkeypatch):
    """A cancelled sync owner cannot leave an authenticated adapter apparently healthy."""
    reports = []
    adapter = native_adapter
    await adapter.start(context(reports))
    adapter._session._on_batch_acknowledged(SimpleNamespace(completes_sync=True))

    async def cancelled(session):
        """Model native cancellation that was not requested by adapter shutdown."""
        raise asyncio.CancelledError

    monkeypatch.setattr(MatrixSession, "_run_sync_forever_attempt", cancelled)
    try:
        with pytest.raises(asyncio.CancelledError):
            await adapter._session._sync_with_reconnect()
        assert reports[-1].health == "failed"
    finally:
        await adapter.stop()


async def test_backoff_cancellation_reports_failed(native_adapter, monkeypatch):
    """Cancellation during the reconnect backoff cannot strand a reconnecting state."""
    reports = []
    adapter = native_adapter
    await adapter.start(context(reports))
    adapter._session._on_batch_acknowledged(SimpleNamespace(completes_sync=True))

    async def fail(session):
        raise RuntimeError("sync dropped")

    real_sleep = asyncio.sleep

    async def cancelled_backoff(delay):
        """Model task cancellation arriving while the backoff wait is pending."""
        if isinstance(delay, (int, float)) and delay > 0:
            raise asyncio.CancelledError
        await real_sleep(delay)

    monkeypatch.setattr(MatrixSession, "_run_sync_forever_attempt", fail)
    monkeypatch.setattr(asyncio, "sleep", cancelled_backoff)
    try:
        with pytest.raises(asyncio.CancelledError):
            await adapter._session._sync_with_reconnect()
        assert reports[-1].health == "failed"
        assert adapter._session._reconnecting is False
    finally:
        await adapter.stop()


async def test_durable_reopen_cancellation_reports_failed(native_adapter, monkeypatch):
    """Cancellation while reopening a durable generation cannot strand reconnecting."""
    reports = []
    adapter = native_adapter
    await adapter.start(context(reports))
    adapter._session._on_batch_acknowledged(SimpleNamespace(completes_sync=True))
    session = adapter._session
    session._durable_sync_enabled = True

    class RetiredOwner:
        @property
        def cursor(self):
            raise RuntimeError("durable owner retired")

    session._durable_owner = RetiredOwner()
    session._client = SimpleNamespace(device_id="DEVICE", logged_in=True)

    async def fail(session):
        raise RuntimeError("sync dropped")

    async def cancelled_reopen(session, **kwargs):
        """Model task cancellation arriving during the durable reopen sequence."""
        raise asyncio.CancelledError

    monkeypatch.setattr(MatrixSession, "_run_sync_forever_attempt", fail)
    monkeypatch.setattr(MatrixSession, "_cancel_tasks_with_deadline", AsyncMock())
    monkeypatch.setattr(MatrixSession, "_close_client", AsyncMock())
    monkeypatch.setattr(MatrixSession, "_start_durable", cancelled_reopen)
    try:
        with pytest.raises(asyncio.CancelledError):
            await session._sync_with_reconnect()
        assert reports[-1].health == "failed"
        assert session._reconnecting is False
    finally:
        await adapter.stop()
