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
