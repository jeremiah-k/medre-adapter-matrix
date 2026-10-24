"""Regression boundaries for durable admission and consumer identity."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from medre.adapters.matrix.durable_sync import (
    MatrixBatchConsumer,
    _canonical_homeserver,
)
from medre.adapters.matrix.errors import MatrixAttachmentFetchDeferredError
from medre.core.ingress.types import DurableIngressDeferredError


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://EXAMPLE.org.:443/", "https://example.org"),
        ("http://h:443/", "http://h:443"),
        ("http://h:0/", "http://h:0"),
        ("http://[2001:db8::1]:80/", "http://[2001:db8::1]"),
        ("https://[2001:db8::1]:8448/prefix/", "https://[2001:db8::1]:8448/prefix"),
    ],
)
def test_canonical_origin_preserves_address_port_and_path(url, expected):
    """Equivalent spellings collapse while distinct endpoints remain distinct."""
    assert _canonical_homeserver(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://u:p@h",
        "https://@h",
        "https://h/?x=1",
        "https://h/#part",
        "ftp://h",
        "http://./",
    ],
)
def test_canonical_origin_rejects_non_server_urls(url):
    """Credentials and routing suffixes cannot alter an identity binding."""
    with pytest.raises(ValueError):
        _canonical_homeserver(url)


async def test_media_deferrals_have_backoff_and_capacity_does_not_consume_it(
    monkeypatch,
):
    """Media retries wait 15/30 seconds and reset for the next native record."""
    import medre.adapters.matrix.durable_sync as module

    records = [
        SimpleNamespace(kind="timeline", name=name) for name in ("first", "second")
    ]
    counts = {item.name: 0 for item in records}
    delays = []

    async def dispatch(item):
        counts[item.name] += 1
        if counts[item.name] == 1:
            raise DurableIngressDeferredError("event", ("capacity_full",))
        if counts[item.name] < 4:
            raise MatrixAttachmentFetchDeferredError("event", ("network",))

    async def sleep(delay):
        delays.append(delay)

    owner = SimpleNamespace(
        next_batch=AsyncMock(return_value=SimpleNamespace(records=records, sequence=1)),
        dispatch=dispatch,
        ack=AsyncMock(side_effect=asyncio.CancelledError),
    )
    monkeypatch.setattr(
        module, "asyncio", SimpleNamespace(**(vars(asyncio) | {"sleep": sleep}))
    )
    consumer = MatrixBatchConsumer(owner, accept_loss=AsyncMock(), accepted=AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await consumer.consume()
    assert delays == [0.25, 15.0, 30.0, 0.25, 15.0, 30.0]
    assert counts == {"first": 4, "second": 4}


@pytest.mark.matrix_sdk
async def test_owned_oauth_binding_survives_token_rotation_and_canonical_alias(
    tmp_path, monkeypatch
):
    """Rotating secrets do not invalidate a verified account/device binding."""
    import nio
    import medre.adapters.matrix.durable_sync as durable
    import medre.config.adapters.matrix_credentials as credentials
    from medre_matrix_tests.helpers.oauth import make_session

    user, device = "@relay:example", "MEDRE_device"
    configured, actual = "https://ALIAS.example:443/", "https://server.example"

    def fresh(server):
        """Restore a client before loading either SDK store format."""
        client = nio.AsyncClient(server, user, device, store_path=None)
        client.restore_login(user, device, "startup-token")
        return client

    monkeypatch.setattr(credentials, "load_credentials_json", lambda: None)
    client = fresh(configured)
    owner = durable.open_sync(
        client,
        adapter_id="matrix",
        homeserver=configured,
        store_path=tmp_path,
        sync_timeout_ms=1000,
        legacy_cursor=None,
    )
    await owner.close()
    await client.close()
    old_id = durable._existing_consumer_id(tmp_path, user, device)
    # The sidecar and owned manager rotate independently of the startup token.
    managed = make_session(homeserver=actual, access_token="rotated-token")
    monkeypatch.setattr(
        credentials, "load_credentials_json", lambda: managed.credentials()
    )
    client = fresh(configured)
    owner = durable.open_sync(
        client,
        adapter_id="matrix",
        homeserver=configured,
        store_path=tmp_path,
        sync_timeout_ms=1000,
        legacy_cursor=None,
        managed_session=managed,
    )
    try:
        assert durable._existing_consumer_id(tmp_path, user, device) == old_id
    finally:
        await owner.close()
        await client.close()


@pytest.mark.matrix_sdk
async def test_owned_oauth_binding_rejects_another_device(tmp_path):
    """A manager for another device cannot authorize alias ownership."""
    import nio
    from nio.exceptions import LocalProtocolError
    from medre.adapters.matrix.durable_sync import open_sync
    from medre_matrix_tests.helpers.oauth import make_session

    session = make_session()
    client = nio.AsyncClient(
        session.homeserver, session.user_id, "OTHER", store_path=None
    )
    client.restore_login(session.user_id, "OTHER", "startup-token")
    try:
        with pytest.raises(LocalProtocolError, match="managed OAuth identity"):
            open_sync(
                client,
                adapter_id="matrix",
                homeserver=session.homeserver,
                store_path=tmp_path,
                sync_timeout_ms=1000,
                legacy_cursor=None,
                managed_session=session,
            )
    finally:
        await client.close()
