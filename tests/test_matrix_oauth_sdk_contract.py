"""Pinned SDK transport and persistent own-device signing qualification."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from medre_matrix_tests.helpers.sdk_contract import (
    assert_installed_extra_matches_declared_pins,
)

pytestmark = pytest.mark.matrix_sdk


async def test_real_sdk_send_uses_fresh_bearer_without_query_credentials():
    nio = pytest.importorskip("nio")
    assert_installed_extra_matches_declared_pins("matrix", ("mindroom-nio",))
    from medre.adapters.matrix.oauth_session import attach_oauth
    from medre_matrix_tests.helpers.oauth import make_session

    saved = make_session()
    client = nio.AsyncClient(
        saved.homeserver,
        saved.user_id,
        config=nio.AsyncClientConfig(encryption_enabled=False),
    )
    http = SimpleNamespace(request=AsyncMock(return_value=object()), close=AsyncMock())
    client.client_session = http
    manager = SimpleNamespace(
        session=saved, access_token=AsyncMock(return_value="FRESH_TOKEN")
    )
    attach_oauth(client, manager)
    try:
        await client.send(
            "GET",
            "/_matrix/client/v3/sync?access_token=old",
            headers={"Authorization": "Bearer old"},
        )
        call = http.request.await_args
        assert call.args[:2] == ("GET", saved.homeserver + "/_matrix/client/v3/sync")
        assert call.kwargs["headers"]["Authorization"] == "Bearer FRESH_TOKEN"
    finally:
        await client.close()


async def test_durable_sdk_transport_uses_managed_oauth_refresh():
    """Durable sync must use the wrapped client.send, not a stale bearer token."""
    nio = pytest.importorskip("nio")
    assert_installed_extra_matches_declared_pins("matrix", ("mindroom-nio",))
    from nio.durable.transport import HttpError, Transport
    from medre.adapters.matrix.oauth_session import attach_oauth
    from medre_matrix_tests.helpers.oauth import make_session

    saved = make_session()
    client = nio.AsyncClient(
        saved.homeserver,
        saved.user_id,
        config=nio.AsyncClientConfig(encryption_enabled=False),
    )
    client.access_token = "EXPIRED_TOKEN"
    manager = SimpleNamespace(
        session=saved,
        access_token=AsyncMock(return_value="REFRESHED_TOKEN"),
    )
    observed = []
    statuses = [200]

    class Content:
        async def iter_chunked(self, _size: int):
            """Yield a bounded response without contacting an external server."""
            yield b'{"next_batch":"one"}'

    async def fake_send(_method, _path, _body=None, **kwargs):
        """Observe the refreshed bearer independently of the SDK response status."""
        observed.append(kwargs["headers"]["Authorization"])
        return SimpleNamespace(
            status=statuses[0],
            content=Content(),
            release=lambda: None,
            headers={},
        )

    client.send = fake_send
    attach_oauth(client, manager)
    try:
        result = await Transport(client, 10000).request(
            "GET", "/_matrix/client/v3/sync?access_token=expired"
        )
        assert result == b'{"next_batch":"one"}'
        manager.access_token.assert_awaited_once()
        assert observed == ["Bearer REFRESHED_TOKEN"]
        statuses[0] = 401
        with pytest.raises(HttpError) as rejected:
            await Transport(client, 10000).request("GET", "/_matrix/client/v3/sync")
        assert rejected.value.status == 401
        assert observed == ["Bearer REFRESHED_TOKEN"] * 2
    finally:
        await client.close()
