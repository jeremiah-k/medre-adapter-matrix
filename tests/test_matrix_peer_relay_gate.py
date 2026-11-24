"""Peer-relay trust gate: ingress rules, warnings, and diagnostics.

The adapter trusts no other relays or bridges unless their exact Matrix
user IDs are listed in ``MatrixConfig.trusted_relays``.  These tests pin
the four gate rules, the rate-limited warnings, the diagnostics counters,
and the bounded warning limiter.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from medre_matrix_tests.helpers.matrix_adapter import (
    make_adapter_context as _make_adapter_context,
)
from medre_matrix_tests.helpers.matrix_adapter import (
    make_fake_nio_event as _make_fake_nio_event,
)
from medre_matrix_tests.helpers.matrix_adapter import make_fake_room as _make_fake_room
from medre_matrix_tests.helpers.matrix_adapter import (
    make_matrix_config as _make_matrix_config,
)
from medre_matrix_tests.helpers.matrix_adapter import to_event_dict as _to_event_dict

from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.metadata import MatrixMetadataEnvelope
from medre.adapters.matrix.peer_relay import (
    PeerRelayWarningLimiter,
    carries_medre_content,
)

_RELAY = "@relay-bot:example.org"
_OTHER = "@alice:example.com"

# A claim field value that must never appear in a drop log record.
_CLAIM_MARKER = "evt-claim-marker"


def _coherent_content(body: str = "relayed provenance") -> dict[str, Any]:
    """Content carrying a coherent MEDRE envelope."""
    envelope = MatrixMetadataEnvelope(
        source_adapter="peer-relay",
        canonical_event_id=_CLAIM_MARKER,
    )
    return {"msgtype": "m.text", "body": body, **envelope.to_content()}


def _minimal_content(body: str = "minimal relay") -> dict[str, Any]:
    """Content carrying a coherent minimal-mode envelope."""
    return {
        "msgtype": "m.text",
        "body": body,
        "medre": {
            "envelope": {
                "schema_version": 1,
                "canonical_event_id": _CLAIM_MARKER,
                "source_transport_id": "node-1",
                "metadata_mode": "minimal",
            }
        },
    }


def _malformed_content(body: str = "broken claim") -> dict[str, Any]:
    """Content carrying the reserved namespace with a corrupt envelope."""
    return {
        "msgtype": "m.text",
        "body": body,
        "medre": {"envelope": "not a dict"},
    }


def _unsupported_content(body: str = "future claim") -> dict[str, Any]:
    """Content carrying an envelope with an unsupported schema version."""
    return {
        "msgtype": "m.text",
        "body": body,
        "medre": {
            "envelope": {
                "schema_version": 99,
                "canonical_event_id": _CLAIM_MARKER,
                "metadata_mode": "safe",
            }
        },
    }


def _make_gate_adapter(
    trusted_relays: tuple[str, ...],
    clock: Any = None,
) -> tuple[MatrixAdapter, list[Any], Any]:
    """Build an adapter with the given trust list and a collecting context."""
    config = _make_matrix_config(
        adapter_id="matrix-gate",
        user_id="@bot:example.com",
        trusted_relays=trusted_relays,
    )
    adapter = MatrixAdapter(config)
    if clock is not None:
        adapter._clock = clock
    published, ctx = _make_adapter_context(adapter_id="matrix-gate")
    adapter.ctx = ctx
    adapter._started = True
    return adapter, published, ctx


async def _deliver(
    adapter: MatrixAdapter, sender: str, content: dict[str, Any]
) -> None:
    event = _make_fake_nio_event(sender=sender, content=content)
    await adapter._on_room_message(_to_event_dict(_make_fake_room(), event))


# ===================================================================
# Namespace detection
# ===================================================================


class TestCarriesMedreContent:
    """Raw namespace detection never parses claim contents."""

    def test_dict_with_key(self) -> None:
        assert carries_medre_content({"medre": {"envelope": {}}}) is True

    def test_dict_without_key(self) -> None:
        assert carries_medre_content({"msgtype": "m.text", "body": "hi"}) is False

    def test_none_content(self) -> None:
        assert carries_medre_content(None) is False

    def test_non_dict_content(self) -> None:
        # A plain string must not match by substring.
        assert carries_medre_content("medre in plain text") is False


# ===================================================================
# Warning limiter
# ===================================================================


class TestPeerRelayWarningLimiter:
    """Bounded once-per-interval warning cadence."""

    def test_first_observation_warns(self) -> None:
        limiter = PeerRelayWarningLimiter()
        assert limiter.should_warn("@s:example.org", "untrusted", 0.0) is True

    def test_repeat_inside_interval_is_silent(self) -> None:
        limiter = PeerRelayWarningLimiter()
        assert limiter.should_warn("@s:example.org", "untrusted", 0.0) is True
        assert limiter.should_warn("@s:example.org", "untrusted", 299.9) is False

    def test_repeat_at_interval_boundary_warns_again(self) -> None:
        limiter = PeerRelayWarningLimiter()
        assert limiter.should_warn("@s:example.org", "untrusted", 0.0) is True
        assert limiter.should_warn("@s:example.org", "untrusted", 299.9) is False
        assert limiter.should_warn("@s:example.org", "untrusted", 300.0) is True

    def test_reasons_are_throttled_independently(self) -> None:
        limiter = PeerRelayWarningLimiter()
        assert limiter.should_warn("@s:example.org", "untrusted", 0.0) is True
        assert limiter.should_warn("@s:example.org", "invalid", 1.0) is True

    def test_senders_are_throttled_independently(self) -> None:
        limiter = PeerRelayWarningLimiter()
        assert limiter.should_warn("@a:example.org", "untrusted", 0.0) is True
        assert limiter.should_warn("@b:example.org", "untrusted", 1.0) is True

    def test_entries_are_bounded(self) -> None:
        limiter = PeerRelayWarningLimiter(max_entries=3)
        for index in range(5):
            limiter.should_warn(f"@s{index}:example.org", "untrusted", float(index))
        assert len(limiter._last) == 3
        # The oldest entry was evicted, so its sender warns again.
        assert limiter.should_warn("@s0:example.org", "untrusted", 100.0) is True


# ===================================================================
# Ingress gate rules
# ===================================================================


class TestPeerRelayTrustGate:
    """The four gate rules in ``_on_room_message``."""

    async def test_untrusted_sender_envelope_dropped(self) -> None:
        """An unlisted sender's envelope-carrying message is dropped."""
        adapter, published, _ = _make_gate_adapter(trusted_relays=())
        await _deliver(adapter, _OTHER, _coherent_content())

        assert len(published) == 0
        assert adapter._inbound_filtered_untrusted_relay == 1
        assert adapter._inbound_filtered_invalid_relay_envelope == 0
        assert adapter._inbound_suppressed_envelope == 0

    async def test_empty_trust_list_drops_every_envelope(self) -> None:
        """The default configuration trusts no relay at all."""
        adapter, published, _ = _make_gate_adapter(trusted_relays=())
        await _deliver(adapter, _RELAY, _coherent_content())
        await _deliver(adapter, _OTHER, _minimal_content())

        assert len(published) == 0
        assert adapter._inbound_filtered_untrusted_relay == 2

    async def test_ordinary_message_from_unlisted_sender_relayed(self) -> None:
        """Regular Matrix messages are never gated, in any configuration."""
        adapter, published, _ = _make_gate_adapter(
            trusted_relays=(_RELAY,),
        )
        await _deliver(
            adapter,
            _OTHER,
            {"msgtype": "m.text", "body": "just chatting"},
        )

        assert len(published) == 1
        assert adapter._inbound_filtered_untrusted_relay == 0

    async def test_trusted_sender_coherent_envelope_relayed(self) -> None:
        """A listed sender's coherent envelope is processed as today."""
        adapter, published, _ = _make_gate_adapter(
            trusted_relays=(_RELAY,),
        )
        await _deliver(adapter, _RELAY, _coherent_content())
        await _deliver(adapter, _RELAY, _minimal_content())

        assert len(published) == 2
        assert adapter._inbound_filtered_untrusted_relay == 0
        assert adapter._inbound_filtered_invalid_relay_envelope == 0

    async def test_trusted_sender_malformed_envelope_dropped(self) -> None:
        """A listed sender's corrupt envelope is dropped, never downgraded."""
        adapter, published, _ = _make_gate_adapter(
            trusted_relays=(_RELAY,),
        )
        await _deliver(adapter, _RELAY, _malformed_content())

        assert len(published) == 0
        assert adapter._inbound_filtered_invalid_relay_envelope == 1
        assert adapter._inbound_filtered_untrusted_relay == 0

    async def test_trusted_sender_unsupported_version_dropped(self) -> None:
        """A listed sender's future-version envelope is dropped."""
        adapter, published, _ = _make_gate_adapter(
            trusted_relays=(_RELAY,),
        )
        await _deliver(adapter, _RELAY, _unsupported_content())

        assert len(published) == 0
        assert adapter._inbound_filtered_invalid_relay_envelope == 1

    async def test_untrusted_sender_checked_before_claim_parsing(self) -> None:
        """The trust decision precedes claim parsing: shape never matters."""
        adapter, published, _ = _make_gate_adapter(
            trusted_relays=(_RELAY,),
        )
        await _deliver(adapter, _OTHER, _malformed_content())
        await _deliver(adapter, _OTHER, _unsupported_content())

        assert len(published) == 0
        assert adapter._inbound_filtered_untrusted_relay == 2
        assert adapter._inbound_filtered_invalid_relay_envelope == 0

    async def test_null_medre_value_from_trusted_sender_dropped(self) -> None:
        """A reserved namespace of JSON null is an invalid claim."""
        adapter, published, _ = _make_gate_adapter(
            trusted_relays=(_RELAY,),
        )
        await _deliver(
            adapter,
            _RELAY,
            {"msgtype": "m.text", "body": "null claim", "medre": None},
        )

        assert len(published) == 0
        assert adapter._inbound_filtered_invalid_relay_envelope == 1

    async def test_self_sender_still_suppressed_before_the_gate(self) -> None:
        """Own-account events stay self-suppressed, not gate-filtered."""
        adapter, published, _ = _make_gate_adapter(trusted_relays=())
        await _deliver(adapter, "@bot:example.com", _coherent_content())

        assert len(published) == 0
        assert adapter._inbound_suppressed_self == 1
        assert adapter._inbound_filtered_untrusted_relay == 0


# ===================================================================
# Warnings
# ===================================================================


class TestPeerRelayGateWarnings:
    """Drop warnings name the remedy and never log claim contents."""

    @pytest.fixture(autouse=True)
    def _capture_logs(self, caplog: pytest.LogCaptureFixture) -> Any:
        caplog.set_level(logging.DEBUG, logger="test.matrix-gate")
        self._caplog = caplog
        return caplog

    def _records(self) -> list[logging.LogRecord]:
        return [
            record
            for record in self._caplog.records
            if record.name == "test.matrix-gate"
        ]

    async def test_untrusted_drop_warns_with_remedy(self) -> None:
        """The untrusted warning names sender, room, event, and the remedy."""
        adapter, _published, _ = _make_gate_adapter(trusted_relays=())
        await _deliver(adapter, _OTHER, _coherent_content())

        warnings = [
            record for record in self._records() if record.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert _OTHER in message
        assert "!room:server" in message
        assert "$evt-001" in message
        assert "trusted_relays" in message
        assert "not authenticated" in message

    async def test_untrusted_warning_never_logs_claim_contents(self) -> None:
        """No log record exposes the claim's envelope fields."""
        adapter, _published, _ = _make_gate_adapter(trusted_relays=())
        await _deliver(adapter, _OTHER, _coherent_content())
        await _deliver(adapter, _RELAY, _malformed_content())

        for record in self._records():
            assert _CLAIM_MARKER not in record.getMessage()
            assert "peer-relay" not in record.getMessage()

    async def test_invalid_drop_warns_about_peer_version(self) -> None:
        """The invalid warning tells the operator to check the peer's version."""
        adapter, _published, _ = _make_gate_adapter(
            trusted_relays=(_RELAY,),
        )
        await _deliver(adapter, _RELAY, _malformed_content())

        warnings = [
            record for record in self._records() if record.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert _RELAY in message
        assert "malformed or unsupported" in message
        assert "peer relay's version" in message

    async def test_repeat_drop_inside_window_is_debug_only(self) -> None:
        """A repeat inside the window stays silent at WARNING; counter moves."""
        clock = {"now": 0.0}
        adapter, _published, _ = _make_gate_adapter(
            trusted_relays=(), clock=lambda: clock["now"]
        )
        await _deliver(adapter, _OTHER, _coherent_content())
        clock["now"] = 10.0
        await _deliver(adapter, _OTHER, _coherent_content())

        warnings = [
            record for record in self._records() if record.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        debugs = [
            record
            for record in self._records()
            if record.levelno == logging.DEBUG
            and "repeated untrusted" in record.getMessage()
        ]
        assert len(debugs) == 1
        assert adapter._inbound_filtered_untrusted_relay == 2

    async def test_repeat_drop_after_window_warns_again(self) -> None:
        """The next drop at or past the interval warns again."""
        clock = {"now": 0.0}
        adapter, _published, _ = _make_gate_adapter(
            trusted_relays=(), clock=lambda: clock["now"]
        )
        await _deliver(adapter, _OTHER, _coherent_content())
        clock["now"] = 10.0
        await _deliver(adapter, _OTHER, _coherent_content())
        clock["now"] = 310.0
        await _deliver(adapter, _OTHER, _coherent_content())

        warnings = [
            record for record in self._records() if record.levelno == logging.WARNING
        ]
        assert len(warnings) == 2
        assert adapter._inbound_filtered_untrusted_relay == 3

    async def test_distinct_senders_warn_independently(self) -> None:
        """The throttle is per sender: a second sender warns in the window."""
        adapter, _published, _ = _make_gate_adapter(trusted_relays=())
        await _deliver(adapter, _OTHER, _coherent_content())
        await _deliver(adapter, _RELAY, _coherent_content())

        warnings = [
            record for record in self._records() if record.levelno == logging.WARNING
        ]
        assert len(warnings) == 2


# ===================================================================
# Diagnostics
# ===================================================================


class TestPeerRelayGateDiagnostics:
    """Diagnostics expose the configured count and observed drops."""

    async def test_disconnected_diagnostics_expose_counts(self) -> None:
        """The no-session branch exposes the count and drop counters."""
        config = _make_matrix_config(
            trusted_relays=(_RELAY, "@second:example.org"),
        )
        adapter = MatrixAdapter(config)
        diag = adapter.diagnostics()

        assert diag["trusted_relay_count"] == 2
        assert diag["inbound_filtered_untrusted_relay"] == 0
        assert diag["inbound_filtered_invalid_relay_envelope"] == 0
        # Counts only — never the configured relay IDs.
        import json

        assert _RELAY not in json.dumps(diag, default=str)

    async def test_diagnostics_reflect_observed_drops(self) -> None:
        """Observed drops surface under both drop counters."""
        adapter, _published, _ = _make_gate_adapter(trusted_relays=())
        await _deliver(adapter, _OTHER, _coherent_content())

        diag = adapter.diagnostics()
        assert diag["trusted_relay_count"] == 0
        assert diag["inbound_filtered_untrusted_relay"] == 1
        assert diag["inbound_filtered_invalid_relay_envelope"] == 0
