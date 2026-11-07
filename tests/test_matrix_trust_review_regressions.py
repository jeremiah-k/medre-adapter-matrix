"""Review regressions for opt-in trust ownership and a quiet default."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.device_trust import (
    PeerDeviceTrustReport,
    PeerRecipient,
    describe_peer_device_trust,
)
from medre.adapters.matrix.session import MatrixSession
from medre.config.adapters.matrix import MatrixConfig
from medre.config.adapters.matrix_errors import MatrixConfigError
from medre.core.contracts.adapter import AdapterContext


def _adapter() -> MatrixAdapter:
    return MatrixAdapter(
        MatrixConfig(
            adapter_id="test",
            homeserver="https://example.org",
            user_id="@bot:example.org",
            access_token="token",
            encryption_mode="e2ee_required",
        )
    )


def _trust_session(adapter: MatrixAdapter, logger: logging.Logger) -> None:
    adapter.ctx = AdapterContext(
        adapter_id="test",
        publish_inbound=AsyncMock(),
        logger=logger,
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )
    adapter._session = SimpleNamespace(
        crypto_enabled=True,
        peer_device_trust_report=AsyncMock(
            side_effect=lambda room_id: PeerDeviceTrustReport(
                room_id=room_id,
                recipients=(PeerRecipient(user_id="@peer:example.org", devices=()),),
            )
        ),
    )


@pytest.mark.parametrize("value", [["strict"], {"strict": True}])
def test_unhashable_verification_policy_has_typed_error(value) -> None:
    config = MatrixConfig(
        adapter_id="test",
        homeserver="https://example.org",
        user_id="@bot:example.org",
        access_token="token",
        verification_policy=value,
    )
    with pytest.raises(MatrixConfigError, match="verification_policy") as failure:
        config.validate()
    assert "strict" not in str(failure.value)


def test_verified_enum_alone_without_verified_property_is_not_trusted() -> None:
    reported = describe_peer_device_trust(
        "@peer:example.org",
        "DEVICE",
        SimpleNamespace(trust_state=SimpleNamespace(name="verified")),
    )
    assert reported.trust_state == "verified"
    assert reported.verified is False


def test_unreadable_device_properties_are_unverified() -> None:
    class UnreadableDevice:
        @property
        def trust_state(self):
            raise RuntimeError("unavailable crypto state")

    reported = describe_peer_device_trust(
        "@peer:example.org", "DEVICE", UnreadableDevice()
    )
    assert reported.trust_state == "unknown"
    assert reported.verified is False


async def test_durable_session_without_owner_has_unavailable_trust_view() -> None:
    session = MatrixSession(_adapter()._config)
    session._client = SimpleNamespace(rooms={})
    session._durable_sync_enabled = True
    report = await session.peer_device_trust_report("!room:example.org")
    assert report.trust_view_available is False


async def test_default_is_quiet_even_with_unavailable_peer_trust_view(caplog) -> None:
    adapter = _adapter()
    logger = logging.getLogger("review.matrix.trust")
    _trust_session(adapter, logger)
    with caplog.at_level(logging.WARNING, logger=logger.name):
        for room in ("!first:example.org", "!second:example.org"):
            await adapter._check_peer_device_trust(room, room_encrypted=True)
    assert not caplog.records
    adapter._session.peer_device_trust_report.assert_not_awaited()
    assert not hasattr(adapter, "_peer_device_trust_warnings")
    assert not hasattr(adapter, "_peer_device_trust_last_log_by_room")
