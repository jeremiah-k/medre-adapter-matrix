"""Review regressions for per-room diagnostics and SDK trust ownership."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from medre.adapters.matrix.adapter import (
    _MAX_PEER_TRUST_LOG_ROOMS,
    MatrixAdapter,
)
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


async def test_warning_rate_limit_is_per_room_and_uses_injected_clock(caplog) -> None:
    adapter = _adapter()
    logger = logging.getLogger("review.matrix.trust")
    _trust_session(adapter, logger)
    now = [100.0]
    adapter._clock = lambda: now[0]
    first, second = "!first:example.org", "!second:example.org"
    with caplog.at_level(logging.WARNING, logger=logger.name):
        await adapter._check_peer_device_trust(first, room_encrypted=True)
        await adapter._check_peer_device_trust(first, room_encrypted=True)
        await adapter._check_peer_device_trust(second, room_encrypted=True)
        now[0] += 61.0
        await adapter._check_peer_device_trust(first, room_encrypted=True)
    warnings = [record.getMessage() for record in caplog.records]
    assert len(warnings) == 3
    assert first in warnings[0] and second in warnings[1] and first in warnings[2]
    assert adapter._peer_device_trust_warnings == 4


async def test_warning_history_is_bounded_and_allows_new_rooms() -> None:
    adapter = _adapter()
    logger = logging.getLogger("review.matrix.trust.silent")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    _trust_session(adapter, logger)
    adapter._clock = lambda: 100.0
    for index in range(_MAX_PEER_TRUST_LOG_ROOMS + 1):
        await adapter._check_peer_device_trust(
            f"!room-{index}:example.org", room_encrypted=True
        )
    assert len(adapter._peer_device_trust_last_log_by_room) == _MAX_PEER_TRUST_LOG_ROOMS
    assert "!room-0:example.org" not in adapter._peer_device_trust_last_log_by_room
    await adapter._check_peer_device_trust("!room-0:example.org", room_encrypted=True)
    assert "!room-0:example.org" in adapter._peer_device_trust_last_log_by_room
    assert len(adapter._peer_device_trust_last_log_by_room) == _MAX_PEER_TRUST_LOG_ROOMS
    assert adapter._peer_device_trust_warnings == _MAX_PEER_TRUST_LOG_ROOMS + 2
