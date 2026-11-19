"""Shape held Matrix account facts for the startup block."""

from __future__ import annotations

from urllib.parse import urlsplit


def _distinct_homeserver(homeserver: str, user_id: str | None) -> str | None:
    """Return an endpoint that adds information beyond the user's server name."""
    try:
        endpoint = urlsplit(homeserver)
        if not endpoint.hostname or endpoint.username is not None:
            return None
    except ValueError:
        return None
    if endpoint.query or endpoint.fragment:
        return None

    server_name = user_id.partition(":")[2] if user_id else ""
    if (
        endpoint.scheme == "https"
        and endpoint.path in {"", "/"}
        and endpoint.netloc.casefold().removesuffix(":443")
        == server_name.casefold().removesuffix(":443")
    ):
        return None
    return homeserver


def build_startup_info(
    *, homeserver: str, user_id: str | None = None, device_id: str | None = None
) -> dict[str, object]:
    """Use the held user and device IDs and a distinct configured endpoint."""
    rows: list[list[dict[str, str]]] = []
    identity: list[dict[str, str]] = []
    if user_id:
        identity.append({"name": "user_id", "value": user_id})
    if device_id:
        identity.append({"name": "device_id", "value": device_id, "label": "device"})
    if identity:
        rows.append(identity)

    endpoint = _distinct_homeserver(homeserver, user_id)
    if endpoint:
        rows.append([{"name": "homeserver", "value": endpoint, "label": "homeserver"}])
    return {"rows": rows}
