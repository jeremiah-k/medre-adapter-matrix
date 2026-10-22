"""Optional encryption remains visible when a connected session lacks crypto."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.session import MatrixSession
from medre.config.adapters.matrix import MatrixConfig


def config(mode="e2ee_optional"):
    """Build an SDK-free configuration with no native credentials or state."""
    return MatrixConfig(
        adapter_id="matrix",
        homeserver="https://matrix.example.invalid",
        user_id="@bridge:example.invalid",
        access_token="test-only",
        encryption_mode=mode,
        sync_stale_timeout_seconds=0,
    )


@pytest.mark.parametrize(
    ("mode", "enabled", "expected"),
    [
        ("e2ee_optional", False, "degraded"),
        ("e2ee_optional", True, "healthy"),
        ("plaintext", False, "healthy"),
        ("e2ee_required", False, "degraded"),
    ],
)
async def test_optional_crypto_unavailable_is_degraded_even_after_successful_sync(
    mode, enabled, expected
):
    """Authentication and sync cannot satisfy configured encryption readiness."""
    adapter = MatrixAdapter(config(mode))
    adapter._session = SimpleNamespace(
        connected=True,
        last_sync_error=None,
        last_successful_sync=1,
        reconnecting=False,
        is_logged_in=lambda: True,
        crypto_enabled=enabled,
    )
    assert (await adapter.health_check()).health == expected


async def test_missing_optional_dependencies_explain_the_install_command(
    monkeypatch, caplog
):
    """Missing extras are an observable limitation before any delivery fails."""
    session = MatrixSession(config())
    monkeypatch.setattr("medre.adapters.matrix.compat.HAS_E2EE", False)
    plaintext = AsyncMock()
    monkeypatch.setattr(MatrixSession, "_start_plaintext", plaintext)
    with caplog.at_level(logging.WARNING):
        await session._start_e2ee_optional()
    plaintext.assert_awaited_once()
    assert "matrix[e2e]" in (session.last_crypto_error or "")
    assert "matrix[e2e]" in caplog.text
    assert not session.crypto_enabled


async def test_completed_crypto_setup_without_active_crypto_is_reported(
    monkeypatch, caplog
):
    """A setup call returning normally cannot conceal an unusable Olm/store pair."""
    session = MatrixSession(config())
    monkeypatch.setattr("medre.adapters.matrix.compat.HAS_E2EE", True)
    monkeypatch.setattr(MatrixSession, "_start_e2ee_required", AsyncMock())
    with caplog.at_level(logging.WARNING):
        await session._start_e2ee_optional()
    assert "did not activate crypto" in session.last_crypto_error
    assert "only unencrypted rooms" in caplog.text


async def test_optional_setup_exception_retains_reason_and_plain_room_operation(
    monkeypatch, caplog
):
    """Unavailable crypto records its reason while keeping the safety guard intact."""
    session = MatrixSession(config())
    monkeypatch.setattr("medre.adapters.matrix.compat.HAS_E2EE", True)
    monkeypatch.setattr(
        MatrixSession,
        "_start_e2ee_required",
        AsyncMock(side_effect=RuntimeError("test crypto initialization failed")),
    )
    plaintext = AsyncMock()
    monkeypatch.setattr(MatrixSession, "_start_plaintext", plaintext)
    with caplog.at_level(logging.WARNING):
        await session._start_e2ee_optional()
    plaintext.assert_awaited_once()
    assert "test crypto initialization failed" in session.last_crypto_error
    assert "only unencrypted rooms" in caplog.text
    assert not session.crypto_enabled
