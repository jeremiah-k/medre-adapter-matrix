"""Peer-device trust reporting and evaluation for encrypted sends.

Matrix adapter-owned policy types.  The session collects the device trust
the pinned SDK exposes for a room's recipients, the adapter evaluates it at
the encrypted-send authorization point.  Policy evaluation works only on
the adapter-owned value objects below, so it never imports ``nio`` types
and stays testable against stub clients.

A peer device counts as trusted only when the SDK affirmatively marks it
verified.  ``unset``, ``blacklisted``, and ``ignored`` trust states, a
missing trust attribute, and a recipient with no known device list are all
trust gaps: they never auto-verify and never fall back to a weaker
transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "PeerDevice",
    "PeerDeviceTrustReport",
    "PeerDeviceTrustViolation",
    "PeerRecipient",
    "describe_peer_device_trust",
    "describe_peer_device_trust_violation",
    "evaluate_peer_device_trust",
    "format_peer_device_trust_refusal",
    "format_peer_device_trust_warning",
]


@dataclass(frozen=True)
class PeerDevice:
    """One peer device as reported by the session's SDK client.

    Attributes
    ----------
    user_id:
        Owning recipient's fully-qualified Matrix user ID.
    device_id:
        SDK-reported device identifier.
    trust_state:
        Adapter-owned trust-state name: ``"verified"``, ``"unset"``,
        ``"blacklisted"``, ``"ignored"``, or ``"unknown"`` when the SDK
        exposes no readable state.
    verified:
        ``True`` only when the SDK affirmatively marks the device verified.
    """

    user_id: str
    device_id: str
    trust_state: str
    verified: bool


@dataclass(frozen=True)
class PeerRecipient:
    """One recipient of an encrypted send with its known devices.

    ``devices`` is empty when the SDK exposes no device list for the
    recipient: the recipient is unknown to the device store, which is
    itself a trust gap.
    """

    user_id: str
    devices: tuple[PeerDevice, ...]


@dataclass(frozen=True)
class PeerDeviceTrustReport:
    """Room-scoped peer-device trust snapshot consumed by the policy.

    Attributes
    ----------
    room_id:
        Target room of the encrypted send.
    recipients:
        Every room recipient device other than the adapter's current
        device, in stable order. Sibling devices of the adapter's account
        remain recipients and require affirmative verification.
    trust_view_available:
        ``False`` when the SDK exposed no device view at all: trust cannot
        be established for any recipient and the policy must fail closed.
    """

    room_id: str
    recipients: tuple[PeerRecipient, ...]
    trust_view_available: bool = True


@dataclass(frozen=True)
class PeerDeviceTrustViolation:
    """Trust gaps found while authorizing one encrypted send."""

    room_id: str
    unverified_devices: tuple[PeerDevice, ...]
    unknown_recipients: tuple[str, ...]
    trust_view_available: bool


def describe_peer_device_trust(user_id: str, device_id: str, device: Any) -> PeerDevice:
    """Map one duck-typed SDK device to the adapter-owned trust value.

    The pinned SDK exposes ``trust_state`` (an enum with ``name``) and a
    ``verified`` bool property.  Both accesses are duck-typed so stub
    clients work; an unreadable state maps to ``"unknown"`` and stays
    unverified.
    """
    try:
        trust_state = getattr(device, "trust_state", None)
        name = getattr(trust_state, "name", None)
        verified = getattr(device, "verified", None)
    except Exception:
        # Some SDK compatibility objects expose properties that raise while
        # their backing store is unavailable. Treat that as an unreadable
        # trust view, never as affirmative verification.
        name, verified = "unknown", False
    if not isinstance(name, str) or not name:
        name = "unknown"
    # Do not let a contradictory duck-typed SDK view bypass a revoked,
    # blacklisted or ignored trust state.  In mindroom-nio both are derived
    # from the same enum, but tests and compatibility clients need this fence.
    verified = name == "verified" and verified is True
    return PeerDevice(
        user_id=user_id,
        device_id=str(device_id),
        trust_state=name,
        verified=verified,
    )


def evaluate_peer_device_trust(
    report: PeerDeviceTrustReport,
) -> PeerDeviceTrustViolation | None:
    """Evaluate a trust report against the peer-device trust policy.

    Returns ``None`` when every recipient has an affirmatively verified
    device list.  Any other outcome is a violation: devices that are not
    affirmatively verified, recipients with no known device list, or a
    missing device view.
    """
    if not report.trust_view_available:
        return PeerDeviceTrustViolation(
            room_id=report.room_id,
            unverified_devices=(),
            unknown_recipients=(),
            trust_view_available=False,
        )
    unverified: list[PeerDevice] = []
    unknown: list[str] = []
    for recipient in report.recipients:
        if not recipient.devices:
            unknown.append(recipient.user_id)
            continue
        unverified.extend(device for device in recipient.devices if not device.verified)
    if not unverified and not unknown:
        return None
    return PeerDeviceTrustViolation(
        room_id=report.room_id,
        unverified_devices=tuple(unverified),
        unknown_recipients=tuple(unknown),
        trust_view_available=True,
    )


def _display_identifier(value: str) -> str:
    """Escape terminal controls while preserving the native identity in policy."""
    return ascii(value)[1:-1]


def describe_peer_device_trust_violation(violation: PeerDeviceTrustViolation) -> str:
    """Describe the trust gaps of a violation in one operator-readable string."""
    parts: list[str] = []
    if violation.unverified_devices:
        parts.append(
            "unverified devices: "
            + ", ".join(
                f"{_display_identifier(device.user_id)}/{_display_identifier(device.device_id)} "
                f"(trust state: {_display_identifier(device.trust_state)})"
                for device in violation.unverified_devices
            )
        )
    if violation.unknown_recipients:
        parts.append(
            "recipients with no known device list: "
            + ", ".join(
                _display_identifier(user)
                for user in sorted(violation.unknown_recipients)
            )
        )
    if not violation.trust_view_available:
        parts.append("the peer-device trust view is unavailable")
    return "; ".join(parts)


def format_peer_device_trust_warning(violation: PeerDeviceTrustViolation) -> str:
    """Format the permissive-mode diagnostic for one delivery with trust gaps."""
    return (
        "encrypted send to Matrix room "
        f"{_display_identifier(violation.room_id)} addresses peer-device trust gaps "
        f"({describe_peer_device_trust_violation(violation)}); delivery "
        "proceeds under verification_policy='permissive'"
    )


def format_peer_device_trust_refusal(violation: PeerDeviceTrustViolation) -> str:
    """Format the strict-mode refusal with the operator remedy.

    The message identifies the room, every offending recipient and device,
    and the verification remedy.  It never implies that the adapter can or
    will verify devices on the operator's behalf.
    """
    if not violation.trust_view_available:
        return (
            f"verification_policy='strict' refused the encrypted send to Matrix "
            f"room {_display_identifier(violation.room_id)}: the peer-device trust view is unavailable. "
            "Room membership and device keys must load successfully before "
            "retrying; check homeserver reachability and session sync. "
            "MEDRE never auto-verifies devices or falls back to plaintext."
        )
    return (
        f"verification_policy='strict' refused the encrypted send to Matrix "
        f"room {_display_identifier(violation.room_id)}: "
        f"{describe_peer_device_trust_violation(violation)}. Verify the "
        "listed devices in MEDRE's own nio device store before retrying. "
        "Verification performed in another Matrix client does not automatically "
        "mark MEDRE's local devices trusted; MEDRE does not currently provide "
        "an operator peer-verification workflow. Use permissive mode when "
        "that local trust cannot be established. MEDRE never auto-verifies "
        "devices or falls back to plaintext. A retry remains blocked until "
        "the local trust view is complete and verified."
    )
