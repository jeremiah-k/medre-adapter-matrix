"""Startup-info facts from state the Matrix adapter already holds.

Core's runtime duck-types an optional ``startup_info()`` callable on a
built adapter and renders the returned scalar facts at the adapter's
ready transition (see the adapter-startup-info specification in the
core distribution).  This module shapes the facts this adapter holds at
that transition — the configured homeserver, the authenticated client
identity (user ID and device ID, once the session's client holds them),
and the installed transport SDK version — into the plain mapping the
contract expects.

Pure read: the provider performs no homeserver I/O, acquires no
resources, and mutates no session state, so a synchronous callable is
safe to run on any worker thread.  Facts that are not held yet are
omitted rather than probed: no ``whoami`` or other request is ever
issued to fill a gap, and readiness is never gated or delayed for any
fact.  Access tokens and olm/identity key material are never offered.
This module imports nothing from ``medre.core``: the returned mapping
is plain JSON-like data, validated and redacted by core at the render
boundary.
"""

from __future__ import annotations

from importlib import metadata
from typing import Any

__all__ = ["build_startup_info"]

# The transport SDK distribution whose installed version is reported as
# a held version fact.  The version itself is only ever read from
# installed package metadata at runtime — never stated as a literal.
_SDK_DISTRIBUTION = "mindroom-nio"


def _sdk_version() -> str | None:
    """Return the installed transport SDK version, when held.

    Reads installed package metadata only; ``None`` when the
    distribution is absent so the fact is simply omitted.
    """
    try:
        return metadata.version(_SDK_DISTRIBUTION)
    except metadata.PackageNotFoundError:
        return None


def build_startup_info(
    *,
    homeserver: str,
    user_id: str | None = None,
    device_id: str | None = None,
) -> dict[str, Any]:
    """Build the startup-info mapping from held state.

    ``user_id`` and ``device_id`` are the client identity the adapter's
    logged-in session holds; they are omitted when unavailable.  The
    account identity forms the first row and the configured endpoint
    (plus the installed SDK version, when resolvable) the second, per
    the contract's row guidance.  ``homeserver`` is the validated
    configured endpoint and is always held by a constructed adapter.
    """
    rows: list[list[dict[str, Any]]] = []
    identity: list[dict[str, Any]] = []
    if user_id:
        identity.append({"name": "user_id", "value": user_id, "label": "user"})
    if device_id:
        identity.append({"name": "device_id", "value": device_id, "label": "device"})
    if identity:
        rows.append(identity)
    endpoint: list[dict[str, Any]] = []
    if homeserver:
        endpoint.append(
            {"name": "homeserver", "value": homeserver, "label": "homeserver"}
        )
    sdk_version = _sdk_version()
    if sdk_version:
        endpoint.append({"name": "sdk_version", "value": sdk_version, "label": "sdk"})
    if endpoint:
        rows.append(endpoint)
    return {"rows": rows}
