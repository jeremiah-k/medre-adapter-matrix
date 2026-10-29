"""Initial sync stays inside bounded startup without hiding native failures."""

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
def starting_adapter(monkeypatch):
    """Authenticate a real session without allowing sync progress implicitly."""
    monkeypatch.setattr("medre.adapters.matrix.adapter.HAS_NIO", True)
    monkeypatch.setattr("medre.adapters.matrix.compat.HAS_NIO", True)

    async def start(session):
        session._closed = False
        session._stop_requested = False
        session._client = SimpleNamespace(logged_in=True)

    async def stop(session, timeout=5.0):
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
        )
    )


def context(reports):
    """Record the same completed health observations consumed by the runtime."""
    return AdapterContext(
        "matrix",
        AsyncMock(),
        logging.getLogger("startup-grace"),
        lambda: datetime.now(timezone.utc),
        asyncio.Event(),
        report_health=reports.append,
    )


async def test_startup_waits_for_first_ack_without_degraded_report(
    starting_adapter, caplog
):
    """A resumed long poll is allowed to finish before startup health is published."""
    adapter = starting_adapter
    reports = []
    with caplog.at_level(logging.INFO):
        task = asyncio.create_task(adapter.start(context(reports)))
        try:
            assert await wait_until(
                lambda: adapter._session is not None and adapter._session.connected
            )
            assert (
                not task.done()
            ), "authentication must not settle initial-sync readiness"
            assert reports == []
            adapter._session._on_batch_acknowledged(
                SimpleNamespace(completes_sync=True)
            )
            await asyncio.wait_for(task, timeout=1)
            assert [info.health for info in reports] == ["healthy"]
            assert "health=degraded" not in caplog.text
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await adapter.stop()


async def test_grace_expiry_keeps_recovery_and_reports_real_health(starting_adapter):
    """An unacknowledged sync remains degraded and can recover after the budget."""
    from dataclasses import replace

    adapter = starting_adapter
    adapter._config = replace(adapter._config, initial_sync_timeout_seconds=0.01)
    reports = []
    try:
        await asyncio.wait_for(adapter.start(context(reports)), timeout=1)
        session = adapter._session
        assert [info.health for info in reports] == ["degraded"]
        assert adapter._health_reasons == ("waiting_initial_sync_ack",)
        assert session.connected
        session._on_batch_acknowledged(SimpleNamespace(completes_sync=True))
        assert [info.health for info in reports] == ["degraded", "healthy"]
    finally:
        await adapter.stop()


@pytest.mark.parametrize("failure", ["terminal", "reconnecting"])
async def test_native_failure_ends_wait_immediately(starting_adapter, failure):
    """Grace applies to ordinary first-sync latency, not a known native outage."""
    adapter = starting_adapter
    reports = []
    task = asyncio.create_task(adapter.start(context(reports)))
    try:
        assert await wait_until(
            lambda: adapter._session is not None and adapter._session.connected
        )
        session = adapter._session
        if failure == "terminal":
            session._sync_failure = ValueError("invalid native state")
        else:
            session._reconnecting = True
        session._notify_health_change()
        await asyncio.wait_for(task, timeout=1)
        assert reports[-1].health == ("failed" if failure == "terminal" else "degraded")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await adapter.stop()


async def test_disabled_grace_returns_pending_health_immediately(starting_adapter):
    """Explicit zero preserves immediate startup without promoting authentication."""
    from dataclasses import replace

    adapter = starting_adapter
    adapter._config = replace(adapter._config, initial_sync_timeout_seconds=0)
    reports = []
    try:
        await adapter.start(context(reports))
        assert [info.health for info in reports] == ["degraded"]
    finally:
        await adapter.stop()


async def test_cancellation_during_grace_retires_session(starting_adapter):
    """The runtime startup deadline owns cancellation and releases partial resources."""
    adapter = starting_adapter
    reports = []
    task = asyncio.create_task(adapter.start(context(reports)))
    assert await wait_until(
        lambda: adapter._session is not None and adapter._session.connected
    )
    session = adapter._session
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.closed
    assert adapter._session is None and adapter.ctx is None
    assert not adapter._started and not adapter._starting
    assert reports == []


async def test_ack_during_room_join_is_not_waited_twice(starting_adapter, monkeypatch):
    """A first batch accepted before the waiter starts satisfies the same readiness."""
    from dataclasses import replace

    adapter = starting_adapter
    adapter._config = replace(adapter._config, auto_join_rooms=("!room:example.com",))

    async def join(session, rooms):
        session._on_batch_acknowledged(SimpleNamespace(completes_sync=True))
        return {room: True for room in rooms}

    monkeypatch.setattr(MatrixSession, "ensure_joined_rooms", join)
    reports = []
    try:
        await asyncio.wait_for(adapter.start(context(reports)), timeout=1)
        assert [info.health for info in reports] == ["healthy"]
    finally:
        await adapter.stop()


@pytest.mark.parametrize("budget", [True, -1, float("nan"), float("inf"), "45"])
def test_invalid_grace_budget_is_rejected(budget):
    """Startup cannot silently accept nonfinite, negative or incorrectly typed waits."""
    with pytest.raises(ValueError, match="initial_sync_timeout_seconds"):
        MatrixConfig(
            adapter_id="matrix",
            homeserver="https://example.com",
            user_id="@bot:example.com",
            access_token="test-token",
            initial_sync_timeout_seconds=budget,
        ).validate()


async def test_runtime_classifies_initial_ack_as_healthy_startup(
    starting_adapter, tmp_path, monkeypatch, caplog
):
    """The public runtime startup barrier sees acknowledged native readiness."""
    from medre.config.loader import parse_runtime_config
    from medre.config.paths import resolve
    from medre.runtime.builder import RuntimeBuilder

    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    paths = resolve()
    config = parse_runtime_config(
        {
            "storage": {"backend": "memory"},
            "adapters": {
                "matrix": {
                    "matrix": {
                        "homeserver": "https://example.com",
                        "user_id": "@bot:example.com",
                        "access_token": "test-token",
                    }
                }
            },
            "routes": {},
        },
        paths,
    )
    app = RuntimeBuilder(config, paths).build()
    adapter = app.adapters["matrix"]
    with caplog.at_level(logging.INFO):
        task = asyncio.create_task(app.start())
        try:
            assert await wait_until(
                lambda: adapter._session is not None and adapter._session.connected
            )
            assert not task.done()
            adapter._session._on_batch_acknowledged(
                SimpleNamespace(completes_sync=True)
            )
            await asyncio.wait_for(task, timeout=2)
            assert app._health_state["runtime_health"] == "healthy"
            assert "health=degraded" not in caplog.text
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await app.stop()


@pytest.mark.parametrize("ack_before_stop", [False, True])
async def test_stop_retires_pending_start_without_readiness(
    starting_adapter, ack_before_stop, caplog
):
    """Stop wakes startup promptly and queued acknowledgements cannot resurrect it."""
    adapter = starting_adapter
    reports = []
    with caplog.at_level(logging.INFO):
        task = asyncio.create_task(adapter.start(context(reports)))
        try:
            assert await wait_until(
                lambda: adapter._session is not None and adapter._session.connected
            )
            session = adapter._session
            if ack_before_stop:
                session._on_batch_acknowledged(SimpleNamespace(completes_sync=True))
            await adapter.stop()
            assert await wait_until(
                task.done, timeout=0.2
            ), "stop must wake startup before its sync deadline"
            with pytest.raises(asyncio.CancelledError):
                await task
            assert session.closed
            assert adapter._session is None
            assert not adapter._started and not adapter._starting
            assert reports == []
            assert "MatrixAdapter matrix started" not in caplog.text
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await adapter.stop()


async def test_replacement_start_does_not_inherit_or_lose_retired_state(
    starting_adapter,
):
    """An earlier wait cannot publish readiness or clear the replacement session."""
    adapter = starting_adapter
    reports = []
    first = asyncio.create_task(adapter.start(context(reports)))
    second = None
    try:
        assert await wait_until(
            lambda: adapter._session is not None and adapter._session.connected
        )
        previous = adapter._session
        second = asyncio.create_task(adapter.start(context(reports)))
        assert await wait_until(lambda: adapter._session is not previous)
        assert await wait_until(first.done, timeout=0.2)
        with pytest.raises(asyncio.CancelledError):
            await first
        replacement = adapter._session
        assert replacement is not None and replacement.connected
        replacement._on_batch_acknowledged(SimpleNamespace(completes_sync=True))
        await asyncio.wait_for(second, timeout=1)
        assert adapter._session is replacement
        assert adapter._started and not adapter._starting
        assert [info.health for info in reports] == ["healthy"]
    finally:
        tasks = [task for task in (first, second) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await adapter.stop()
