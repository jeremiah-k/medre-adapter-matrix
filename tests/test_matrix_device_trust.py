"""Peer-device verification policy: strict refusal and permissive diagnostics.

Covers the ``verification_policy`` configuration binding at the encrypted-send
authorization point: mixed verified/unverified device sets, trust refresh
between sends, device resets, runtime restart re-reading policy and trust,
unknown recipients, and trust-view unavailability.  Refusals assert the SDK
send call never happens, so an independent encrypted-peer observer could
never see the delivery.  Scope boundaries (plaintext rooms, inactive crypto,
ephemeral typing) prove the policy binds only to encrypted sends.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from medre.adapters.matrix.adapter import MatrixAdapter
from dataclasses import replace as dc_replace

from medre.adapters.matrix.device_trust import (
    PeerDevice,
    PeerDeviceTrustReport,
    PeerRecipient,
    evaluate_peer_device_trust,
    format_peer_device_trust_refusal,
    format_peer_device_trust_warning,
)
from medre.adapters.matrix.session import MatrixSession
from medre.config.adapters.matrix import MatrixConfig
from medre.core.contracts.adapter import (
    AdapterContext,
    AdapterPermanentError,
    AdapterSendError,
)
from medre.core.contracts.typing import TypingTarget, TypingUpdate
from medre.core.rendering.renderer import RenderingResult
from medre_matrix_tests.helpers.matrix import matrix_send_payload
from medre_matrix_tests.helpers.matrix_adapter import wire_mock_session

ROOM = "!trust-room:example.com"
OWN_USER = "@bot:example.com"
ALICE = "@alice:example.com"
CAROL = "@carol:example.com"
DAVE = "@dave:example.com"


def _make_config(**overrides: Any) -> MatrixConfig:
    defaults: dict[str, Any] = {
        "adapter_id": "matrix-test",
        "homeserver": "https://matrix.example.com",
        "user_id": OWN_USER,
        "access_token": "tok",
        "encryption_mode": "e2ee_required",
    }
    defaults.update(overrides)
    return MatrixConfig(**defaults)


def _make_ctx() -> AdapterContext:
    return AdapterContext(
        adapter_id="matrix-test",
        publish_inbound=AsyncMock(),
        logger=logging.getLogger("test.matrix-device-trust"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


class _FakeDevice:
    """Duck-typed SDK device: trust_state enum name plus verified bool."""

    def __init__(self, verified: bool, state: str) -> None:
        self.verified = verified
        self.trust_state = SimpleNamespace(name=state)


def _make_result(room_id: str = ROOM, body: str = "trust hello") -> RenderingResult:
    return RenderingResult(
        event_id="evt-trust-1",
        target_adapter="matrix-test",
        target_channel=room_id,
        payload=matrix_send_payload({"msgtype": "m.text", "body": body}),
        metadata={"renderer": "matrix"},
    )


def _stub_client(
    room_users: dict[str, Any],
    device_view: dict[str, dict[str, _FakeDevice]] | object,
) -> MagicMock:
    """Build a duck-typed nio client with room membership and a device view."""
    client = MagicMock(name="nio_client")
    client.room_send = AsyncMock(return_value=SimpleNamespace(event_id="$evt-trust-ok"))
    client.rooms = {ROOM: SimpleNamespace(users=room_users)}
    client.room_devices = MagicMock(return_value=device_view)
    return client


def _pd(user_id: str, device_id: str, verified: bool, state: str) -> PeerDevice:
    """Build an adapter-owned peer device for pure policy tests."""
    return PeerDevice(
        user_id=user_id, device_id=device_id, trust_state=state, verified=verified
    )


def _mixed_device_view() -> dict[str, dict[str, _FakeDevice]]:
    """Alice has one verified device; Carol's device is never verified."""
    return {
        ALICE: {"ALICEDEV": _FakeDevice(True, "verified")},
        CAROL: {"CAROLDEV": _FakeDevice(False, "unset")},
    }


def _mixed_room_users() -> dict[str, Any]:
    return {ALICE: {}, CAROL: {}}


def _wire_encrypted_session(
    adapter: MatrixAdapter, client: MagicMock, config: MatrixConfig
) -> MatrixSession:
    """Wire a real session around the stub client with active crypto and an
    established encrypted room, so the trust stage actually runs."""
    session = wire_mock_session(adapter, client, config=config)
    session._crypto_enabled = True
    session._room_states[ROOM] = "encrypted"
    return session


# ===================================================================
# Pure policy evaluation
# ===================================================================


class TestEvaluatePeerDeviceTrust:
    """The pure evaluation over adapter-owned trust reports."""

    def test_verified_device_lists_pass(self) -> None:
        report = PeerDeviceTrustReport(
            room_id=ROOM,
            recipients=(
                PeerRecipient(
                    user_id=ALICE,
                    devices=(
                        _pd(ALICE, "ALICEDEV", True, "verified"),
                        _pd(ALICE, "ALICEDEV-2", True, "verified"),
                    ),
                ),
            ),
        )
        assert evaluate_peer_device_trust(report) is None

    def test_empty_recipient_room_passes(self) -> None:
        report = PeerDeviceTrustReport(room_id=ROOM, recipients=())
        assert evaluate_peer_device_trust(report) is None

    def test_unverified_device_is_a_violation_with_state_named(self) -> None:
        report = PeerDeviceTrustReport(
            room_id=ROOM,
            recipients=(
                PeerRecipient(
                    user_id=CAROL,
                    devices=(_pd(CAROL, "CAROLDEV", False, "unset"),),
                ),
            ),
        )
        violation = evaluate_peer_device_trust(report)
        assert violation is not None
        assert violation.room_id == ROOM
        assert len(violation.unverified_devices) == 1
        assert violation.unverified_devices[0].user_id == CAROL
        assert violation.unverified_devices[0].device_id == "CAROLDEV"
        assert violation.unknown_recipients == ()
        assert violation.trust_view_available is True

    def test_blacklisted_and_ignored_devices_are_not_verified(self) -> None:
        report = PeerDeviceTrustReport(
            room_id=ROOM,
            recipients=(
                PeerRecipient(
                    user_id=ALICE,
                    devices=(
                        _pd(ALICE, "ALICEDEV", False, "blacklisted"),
                        _pd(ALICE, "ALICEDEV-2", False, "ignored"),
                    ),
                ),
            ),
        )
        violation = evaluate_peer_device_trust(report)
        assert violation is not None
        assert len(violation.unverified_devices) == 2

    def test_unknown_recipient_has_no_device_list(self) -> None:
        report = PeerDeviceTrustReport(
            room_id=ROOM,
            recipients=(PeerRecipient(user_id=DAVE, devices=()),),
        )
        violation = evaluate_peer_device_trust(report)
        assert violation is not None
        assert violation.unknown_recipients == (DAVE,)
        assert violation.unverified_devices == ()

    def test_missing_trust_view_fails_closed(self) -> None:
        report = PeerDeviceTrustReport(
            room_id=ROOM, recipients=(), trust_view_available=False
        )
        violation = evaluate_peer_device_trust(report)
        assert violation is not None
        assert violation.trust_view_available is False
        assert violation.unverified_devices == ()
        assert violation.unknown_recipients == ()


class TestTrustMessageFormatting:
    """Refusal and warning strings identify room, devices, and remedy."""

    def test_refusal_names_room_devices_and_remedy(self) -> None:
        report = PeerDeviceTrustReport(
            room_id=ROOM,
            recipients=(
                PeerRecipient(user_id=ALICE, devices=()),
                PeerRecipient(
                    user_id=CAROL,
                    devices=(_pd(CAROL, "CAROLDEV", False, "unset"),),
                ),
            ),
        )
        violation = evaluate_peer_device_trust(report)
        assert violation is not None
        message = format_peer_device_trust_refusal(violation)
        assert "verification_policy='strict'" in message
        assert ROOM in message
        assert f"{CAROL}/CAROLDEV (trust state: unset)" in message
        assert f"no known device list: {ALICE}" in message
        assert "Verify the listed devices" in message
        assert "never auto-verifies" in message
        assert "never falls back to a weaker transport" in message

    def test_refusal_names_unavailable_trust_view(self) -> None:
        report = PeerDeviceTrustReport(
            room_id=ROOM, recipients=(), trust_view_available=False
        )
        violation = evaluate_peer_device_trust(report)
        assert violation is not None
        message = format_peer_device_trust_refusal(violation)
        assert "trust view is unavailable" in message

    def test_warning_states_permissive_delivery(self) -> None:
        report = PeerDeviceTrustReport(
            room_id=ROOM,
            recipients=(
                PeerRecipient(
                    user_id=CAROL,
                    devices=(_pd(CAROL, "CAROLDEV", False, "unset"),),
                ),
            ),
        )
        violation = evaluate_peer_device_trust(report)
        assert violation is not None
        message = format_peer_device_trust_warning(violation)
        assert ROOM in message
        assert f"{CAROL}/CAROLDEV" in message
        assert "verification_policy='permissive'" in message
        assert "delivery proceeds" in message


# ===================================================================
# Session trust report collection
# ===================================================================


class TestSessionTrustReport:
    """The session maps duck-typed SDK device state into adapter-owned values."""

    def _session_with_client(
        self,
        room_users: dict[str, Any],
        device_view: dict[str, dict[str, _FakeDevice]],
        config: MatrixConfig | None = None,
    ) -> MatrixSession:
        session = MatrixSession(config or _make_config())
        session._client = _stub_client(room_users, device_view)
        return session

    def test_report_excludes_own_user_from_peer_trust(self) -> None:
        session = self._session_with_client(
            {ALICE: {}, OWN_USER: {}},
            {
                ALICE: {"ALICEDEV": _FakeDevice(True, "verified")},
                OWN_USER: {"BOTDEV": _FakeDevice(False, "unset")},
            },
        )
        report = session.peer_device_trust_report(ROOM)
        assert report.room_id == ROOM
        assert report.trust_view_available is True
        assert [recipient.user_id for recipient in report.recipients] == [ALICE]

    def test_member_without_device_entry_is_unknown_recipient(self) -> None:
        session = self._session_with_client(
            {ALICE: {}, DAVE: {}},
            {ALICE: {"ALICEDEV": _FakeDevice(True, "verified")}},
        )
        report = session.peer_device_trust_report(ROOM)
        assert [recipient.user_id for recipient in report.recipients] == [ALICE, DAVE]
        assert report.recipients[1].devices == ()

    def test_device_without_readable_state_maps_to_unknown(self) -> None:
        session = self._session_with_client(
            {ALICE: {}},
            {ALICE: {"ALICEDEV": SimpleNamespace()}},
        )
        report = session.peer_device_trust_report(ROOM)
        device = report.recipients[0].devices[0]
        assert device.trust_state == "unknown"
        assert device.verified is False

    def test_missing_client_fails_closed(self) -> None:
        session = MatrixSession(_make_config())
        report = session.peer_device_trust_report(ROOM)
        assert report.trust_view_available is False


# ===================================================================
# Strict mode: refusal at the encrypted-send authorization point
# ===================================================================


class TestStrictVerificationRefusal:
    """Strict mode refuses encrypted sends with trust gaps before any SDK call."""

    async def test_mixed_verified_and_unverified_devices_refuse_send(self) -> None:
        config = _make_config(verification_policy="strict")
        adapter = MatrixAdapter(config)
        adapter.ctx = _make_ctx()
        client = _stub_client(_mixed_room_users(), _mixed_device_view())
        _wire_encrypted_session(adapter, client, config)

        with pytest.raises(AdapterSendError) as exc_info:
            await adapter.deliver(_make_result())

        # The refusal is retryable so operator verification lets durable
        # delivery complete; the delivery itself never reached the SDK.
        assert exc_info.value.transient is True
        message = str(exc_info.value)
        assert "verification_policy='strict'" in message
        assert f"{CAROL}/CAROLDEV (trust state: unset)" in message
        assert "never auto-verifies" in message
        assert client.room_send.await_count == 0

    async def test_unknown_recipient_refuses_send(self) -> None:
        config = _make_config(verification_policy="strict")
        adapter = MatrixAdapter(config)
        adapter.ctx = _make_ctx()
        client = _stub_client(
            {ALICE: {}, DAVE: {}},
            {ALICE: {"ALICEDEV": _FakeDevice(True, "verified")}},
        )
        _wire_encrypted_session(adapter, client, config)

        with pytest.raises(AdapterSendError) as exc_info:
            await adapter.deliver(_make_result())

        assert f"no known device list: {DAVE}" in str(exc_info.value)
        assert client.room_send.await_count == 0

    async def test_unavailable_trust_view_fails_closed(self) -> None:
        config = _make_config(verification_policy="strict")
        adapter = MatrixAdapter(config)
        adapter.ctx = _make_ctx()
        client = _stub_client(_mixed_room_users(), object())
        _wire_encrypted_session(adapter, client, config)

        with pytest.raises(AdapterSendError) as exc_info:
            await adapter.deliver(_make_result())

        assert "trust view is unavailable" in str(exc_info.value)
        assert client.room_send.await_count == 0

    async def test_all_devices_verified_delivers(self) -> None:
        config = _make_config(verification_policy="strict")
        adapter = MatrixAdapter(config)
        adapter.ctx = _make_ctx()
        client = _stub_client(
            _mixed_room_users(),
            {
                ALICE: {"ALICEDEV": _FakeDevice(True, "verified")},
                CAROL: {"CAROLDEV": _FakeDevice(True, "verified")},
            },
        )
        _wire_encrypted_session(adapter, client, config)

        delivery = await adapter.deliver(_make_result())

        assert client.room_send.await_count == 1
        assert delivery.native_message_id == "$evt-trust-ok"
        assert adapter.diagnostics()["peer_device_trust_strict_refusals"] == 0

    async def test_trust_refresh_between_sends_allows_delivery(self) -> None:
        """Devices verified between attempts are seen on the next attempt."""
        config = _make_config(verification_policy="strict")
        adapter = MatrixAdapter(config)
        adapter.ctx = _make_ctx()
        device_view = _mixed_device_view()
        client = _stub_client(_mixed_room_users(), device_view)
        _wire_encrypted_session(adapter, client, config)

        with pytest.raises(AdapterSendError):
            await adapter.deliver(_make_result())
        assert client.room_send.await_count == 0

        # The operator verifies Carol's device in a Matrix client; the SDK
        # device store now reports it verified.
        verified = device_view[CAROL]["CAROLDEV"]
        verified.verified = True
        verified.trust_state = SimpleNamespace(name="verified")

        delivery = await adapter.deliver(_make_result())

        assert client.room_send.await_count == 1
        assert delivery.native_message_id == "$evt-trust-ok"

    async def test_device_reset_refuses_previously_verified_device(self) -> None:
        """A verified device replaced by an unverified one blocks later sends."""
        config = _make_config(verification_policy="strict")
        adapter = MatrixAdapter(config)
        adapter.ctx = _make_ctx()
        device_view = _mixed_device_view()
        device_view[CAROL] = {"CAROLDEV": _FakeDevice(True, "verified")}
        client = _stub_client(_mixed_room_users(), device_view)
        _wire_encrypted_session(adapter, client, config)

        await adapter.deliver(_make_result())
        assert client.room_send.await_count == 1

        # Carol's device resets and re-logs in as a fresh unverified device.
        device_view[CAROL] = {"CAROL-NEW": _FakeDevice(False, "unset")}

        with pytest.raises(AdapterSendError) as exc_info:
            await adapter.deliver(_make_result())

        assert f"{CAROL}/CAROL-NEW (trust state: unset)" in str(exc_info.value)
        assert client.room_send.await_count == 1
        assert adapter.diagnostics()["peer_device_trust_strict_refusals"] == 1

    async def test_strict_refusal_counter_is_observable(self) -> None:
        config = _make_config(verification_policy="strict")
        adapter = MatrixAdapter(config)
        adapter.ctx = _make_ctx()
        client = _stub_client(_mixed_room_users(), _mixed_device_view())
        _wire_encrypted_session(adapter, client, config)

        with pytest.raises(AdapterSendError):
            await adapter.deliver(_make_result())

        diagnostics = adapter.diagnostics()
        assert diagnostics["peer_device_trust_strict_refusals"] == 1
        assert diagnostics["peer_device_trust_warnings"] == 0
        assert diagnostics["verification_policy"] == "strict"


# ===================================================================
# Permissive mode (default): delivery proceeds with loud diagnostics
# ===================================================================


class TestPermissiveDiagnostics:
    """The default mode keeps delivery behavior and makes trust gaps loud."""

    async def test_mixed_devices_deliver_with_warning_and_counter(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        config = _make_config()
        adapter = MatrixAdapter(config)
        adapter.ctx = _make_ctx()
        client = _stub_client(_mixed_room_users(), _mixed_device_view())
        _wire_encrypted_session(adapter, client, config)

        with caplog.at_level(logging.WARNING, logger="test.matrix-device-trust"):
            delivery = await adapter.deliver(_make_result())

        assert client.room_send.await_count == 1
        assert delivery.native_message_id == "$evt-trust-ok"
        messages = " ".join(record.getMessage() for record in caplog.records)
        assert ROOM in messages
        assert f"{CAROL}/CAROLDEV" in messages
        assert "verification_policy='permissive'" in messages

        diagnostics = adapter.diagnostics()
        assert diagnostics["peer_device_trust_warnings"] == 1
        assert diagnostics["peer_device_trust_strict_refusals"] == 0
        assert diagnostics["verification_policy"] == "permissive"

    async def test_verified_devices_deliver_without_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        config = _make_config()
        adapter = MatrixAdapter(config)
        adapter.ctx = _make_ctx()
        client = _stub_client(
            _mixed_room_users(),
            {
                ALICE: {"ALICEDEV": _FakeDevice(True, "verified")},
                CAROL: {"CAROLDEV": _FakeDevice(True, "verified")},
            },
        )
        _wire_encrypted_session(adapter, client, config)

        with caplog.at_level(logging.WARNING, logger="test.matrix-device-trust"):
            await adapter.deliver(_make_result())

        assert client.room_send.await_count == 1
        assert not caplog.records
        assert adapter.diagnostics()["peer_device_trust_warnings"] == 0

    async def test_unknown_recipient_warns_but_delivers(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        config = _make_config()
        adapter = MatrixAdapter(config)
        adapter.ctx = _make_ctx()
        client = _stub_client(
            {ALICE: {}, DAVE: {}},
            {ALICE: {"ALICEDEV": _FakeDevice(True, "verified")}},
        )
        _wire_encrypted_session(adapter, client, config)

        with caplog.at_level(logging.WARNING, logger="test.matrix-device-trust"):
            await adapter.deliver(_make_result())

        assert client.room_send.await_count == 1
        messages = " ".join(record.getMessage() for record in caplog.records)
        assert f"no known device list: {DAVE}" in messages


# ===================================================================
# Policy scope: binds only to encrypted sends
# ===================================================================


class TestPolicyScope:
    """Plaintext rooms, inactive crypto, and typing are outside the policy."""

    async def test_strict_policy_does_not_bind_plaintext_rooms(self) -> None:
        config = _make_config(verification_policy="strict")
        adapter = MatrixAdapter(config)
        adapter.ctx = _make_ctx()
        client = _stub_client(_mixed_room_users(), _mixed_device_view())
        session = wire_mock_session(adapter, client, config=config)
        session._crypto_enabled = True
        session._room_states[ROOM] = "plaintext"

        delivery = await adapter.deliver(_make_result())

        assert client.room_send.await_count == 1
        assert delivery.native_message_id == "$evt-trust-ok"
        assert adapter.diagnostics()["peer_device_trust_strict_refusals"] == 0
        assert adapter.diagnostics()["peer_device_trust_warnings"] == 0

    async def test_inactive_crypto_stays_outside_trust_policy(self) -> None:
        """Without crypto the existing encrypted-room safeguard classifies, not trust."""
        config = _make_config(verification_policy="strict")
        adapter = MatrixAdapter(config)
        adapter.ctx = _make_ctx()
        client = _stub_client(_mixed_room_users(), _mixed_device_view())
        session = wire_mock_session(adapter, client, config=config)
        session._crypto_enabled = False
        session._room_states[ROOM] = "encrypted"

        with pytest.raises(AdapterPermanentError) as exc_info:
            await adapter.deliver(_make_result())

        assert "E2EE crypto is not active" in str(exc_info.value)
        assert client.room_send.await_count == 0
        assert adapter.diagnostics()["peer_device_trust_strict_refusals"] == 0

    async def test_typing_is_outside_the_verification_policy(self) -> None:
        """Ephemeral typing carries no encrypted content and proceeds regardless."""
        config = _make_config(verification_policy="strict", room_allowlist=[ROOM])
        adapter = MatrixAdapter(config)
        adapter.ctx = _make_ctx()
        adapter._started = True
        session = SimpleNamespace(
            closed=False,
            is_room_member=lambda _: True,
            is_room_encrypted=lambda _: True,
            crypto_enabled=True,
            room_typing=AsyncMock(),
        )
        adapter._session = session  # type: ignore[assignment]

        await adapter.send_typing(TypingUpdate(TypingTarget("matrix-test", ROOM), 10))

        session.room_typing.assert_awaited_once_with(ROOM, True, 10000)


# ===================================================================
# Runtime restart: policy and trust state are re-read, never cached
# ===================================================================


class TestRuntimeRestart:
    """A rebuilt adapter and session re-read the policy and the device view."""

    async def test_restart_after_verification_delivers_under_strict(self) -> None:
        config = _make_config(verification_policy="strict")
        adapter = MatrixAdapter(config)
        adapter.ctx = _make_ctx()
        device_view = _mixed_device_view()
        client = _stub_client(_mixed_room_users(), device_view)
        _wire_encrypted_session(adapter, client, config)

        with pytest.raises(AdapterSendError):
            await adapter.deliver(_make_result())
        assert client.room_send.await_count == 0

        # Restart with the same strict config: the fresh session reads the
        # SDK device store again, which now reports Carol's device verified.
        verified = device_view[CAROL]["CAROLDEV"]
        verified.verified = True
        verified.trust_state = SimpleNamespace(name="verified")
        restarted = MatrixAdapter(config)
        restarted.ctx = _make_ctx()
        restarted_client = _stub_client(_mixed_room_users(), device_view)
        _wire_encrypted_session(restarted, restarted_client, config)

        delivery = await restarted.deliver(_make_result())

        assert restarted_client.room_send.await_count == 1
        assert delivery.native_message_id == "$evt-trust-ok"

    async def test_restart_rebuilds_policy_from_config(self) -> None:
        """A restart selects the configured mode, not a cached one."""
        permissive_config = _make_config()
        device_view = _mixed_device_view()

        strict_adapter = MatrixAdapter(
            dc_replace(permissive_config, verification_policy="strict")
        )
        strict_adapter.ctx = _make_ctx()
        strict_client = _stub_client(_mixed_room_users(), device_view)
        _wire_encrypted_session(
            strict_adapter,
            strict_client,
            dc_replace(permissive_config, verification_policy="strict"),
        )
        with pytest.raises(AdapterSendError):
            await strict_adapter.deliver(_make_result())
        assert strict_client.room_send.await_count == 0

        permissive_adapter = MatrixAdapter(permissive_config)
        permissive_adapter.ctx = _make_ctx()
        permissive_client = _stub_client(_mixed_room_users(), device_view)
        _wire_encrypted_session(
            permissive_adapter, permissive_client, permissive_config
        )
        await permissive_adapter.deliver(_make_result())
        assert permissive_client.room_send.await_count == 1


# ===================================================================
# Delivery flag is orthogonal to the verification policy
# ===================================================================


def test_delivery_flag_unchanged_by_verification_policy() -> None:
    """The nio send flag keeps its E2EE behavior under both policy states."""
    strict = MatrixAdapter(_make_config(verification_policy="strict"))
    permissive = MatrixAdapter(_make_config(verification_policy="permissive"))
    assert strict._should_ignore_unverified_devices() is True
    assert permissive._should_ignore_unverified_devices() is True
