"""Matrix health explains first-sync waiting without treating login as readiness."""

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.session import MatrixSession
from medre.config.adapters.matrix import MatrixConfig


def config():
    """Use complete inert credentials so no operator sidecar is consulted."""
    return MatrixConfig(
        adapter_id="matrix",
        homeserver="https://example.com",
        user_id="@bot:example.com",
        access_token="test-token",
        encryption_mode="e2ee_optional",
    )


async def test_first_ack_clears_waiting_reason_and_health(caplog):
    """Only acknowledged sync progress makes a resumed, authenticated session ready."""
    adapter = MatrixAdapter(config())
    session = MagicMock()
    session.last_sync_error = None
    session.connected = True
    session.is_logged_in.return_value = True
    session.reconnecting = False
    session.last_successful_sync = None
    session.crypto_enabled = True
    adapter._session = session
    adapter._clock = lambda: 100.0
    with caplog.at_level(logging.INFO):
        assert (await adapter.health_check()).health == "degraded"
        assert adapter._health_reasons == ("waiting_initial_sync_ack",)
        session.last_successful_sync = 100.0
        assert (await adapter.health_check()).health == "healthy"
        assert adapter._health_reasons == ()
    assert "waiting_initial_sync_ack" in caplog.text
    assert "health=healthy reasons=none" in caplog.text


async def test_crypto_and_sync_wait_are_independent_reasons():
    """Missing crypto stays visible even while the first sync is pending."""
    adapter = MatrixAdapter(config())
    session = MagicMock()
    session.last_sync_error = None
    session.connected = True
    session.is_logged_in.return_value = True
    session.reconnecting = False
    session.last_successful_sync = None
    session.crypto_enabled = False
    adapter._session = session
    assert (await adapter.health_check()).health == "degraded"
    assert adapter._health_reasons == ("waiting_initial_sync_ack", "crypto_inactive")


def test_first_durable_ack_logs_progress_once(caplog):
    """The operator sees acknowledgement progress even after the startup summary."""
    session = MatrixSession(config())
    session._clock = lambda: 100.0
    with caplog.at_level(logging.INFO):
        session._on_batch_acknowledged(SimpleNamespace(completes_sync=True))
        session._on_batch_acknowledged(SimpleNamespace(completes_sync=True))
    assert session.last_successful_sync == 100.0
    assert caplog.text.count("acknowledged its first batch") == 1
