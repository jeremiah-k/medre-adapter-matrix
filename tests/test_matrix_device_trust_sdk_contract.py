"""Pinned mindroom-nio local device-trust surface, not peer verification UX.

This deliberately runs only in the opt-in matrix_sdk tier.  It qualifies
interfaces used by the local strict gate, not cross-signing, remote trust,
or receipt of an encrypted message by another device.
"""

from __future__ import annotations

import inspect

import pytest

pytestmark = pytest.mark.matrix_sdk


def test_pinned_nio_exposes_local_trust_and_sdk_refusal() -> None:
    import nio
    from nio.crypto import OlmDevice, TrustState

    device = OlmDevice(
        user_id="@peer:example.org",
        device_id="DEVICE",
        keys={"ed25519": "example", "curve25519": "example"},
    )
    assert device.trust_state is TrustState.unset
    assert device.verified is False
    device.trust_state = TrustState.verified
    assert device.verified is True
    device.trust_state = TrustState.blacklisted
    assert device.verified is False
    refusal = nio.OlmUnverifiedDeviceError(device)
    assert refusal.device.id == "DEVICE"
    assert refusal.device.user_id == "@peer:example.org"

    assert callable(getattr(nio.AsyncClient, "room_devices", None))
    assert callable(getattr(nio.AsyncClient, "verify_device", None))
    assert (
        "ignore_unverified_devices"
        in inspect.signature(nio.AsyncClient.room_send).parameters
    )
    assert (
        "ignore_unverified_devices"
        in inspect.signature(nio.AsyncClient.share_group_session).parameters
    )


def test_pinned_nio_room_membership_starts_incomplete() -> None:
    import nio

    room = nio.MatrixRoom("!room:example.org", "@bot:example.org", encrypted=True)
    assert room.members_synced is False
    assert room.users == {}


async def test_classic_joined_members_completes_the_preflight_view(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    import nio
    from medre.adapters.matrix.session import MatrixSession
    from medre.config.adapters.matrix import MatrixConfig

    room_id, own, peer = "!room:example.org", "@bot:example.org", "@peer:example.org"
    client = nio.AsyncClient(
        "https://example.org", config=nio.AsyncClientConfig(encryption_enabled=False)
    )
    client.restore_login(own, "BOT", "test-token")
    client.rooms[room_id] = nio.MatrixRoom(room_id, own, encrypted=True)

    async def receive_members(*args, **kwargs):
        response = nio.JoinedMembersResponse.from_dict(
            {"joined": {own: {}, peer: {}}}, room_id
        )
        await client.receive_response(response)
        return response

    request = AsyncMock(side_effect=receive_members)
    monkeypatch.setattr(client, "_send", request)
    session = MatrixSession(
        MatrixConfig(adapter_id="test", homeserver="https://example.org", user_id=own)
    )
    session._client = client
    try:
        report = await session.peer_device_trust_report(room_id)
        assert request.await_count == 1
        assert client.rooms[room_id].members_synced is True
        assert report.trust_view_available is True
        # Membership alone never turns an unknown device list into trust.
        assert [recipient.user_id for recipient in report.recipients] == [peer]
        assert report.recipients[0].devices == ()
    finally:
        await client.close()


async def test_durable_joined_members_populates_cache_not_room_projection() -> None:
    import asyncio
    import json
    from contextlib import nullcontext
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    import nio
    from nio.crypto import OlmDevice, TrustState
    from nio.crypto.device import DeviceStore
    from nio.durable.outbound import OutboundCrypto
    from medre.adapters.matrix.session import MatrixSession
    from medre.config.adapters.matrix import MatrixConfig

    room_id, own, peer = "!room:example.org", "@bot:example.org", "@peer:example.org"
    room = nio.MatrixRoom(room_id, own, encrypted=True)
    room.add_member(own, "Bot", None)
    devices = DeviceStore()
    device = OlmDevice(peer, "PEER", {"ed25519": "example", "curve25519": "example"})
    device.trust_state = TrustState.verified
    devices.add(device)
    client = SimpleNamespace(
        rooms={room_id: room},
        encrypted_rooms={room_id},
        device_store=devices,
        device_id="BOT",
        should_query_keys=False,
        invalidate_outbound_session=Mock(),
    )
    owner = SimpleNamespace(
        client=client,
        _crypto_lock=asyncio.Lock(),
        _assert_active=lambda: None,
        _store=SimpleNamespace(transaction=nullcontext),
        _crypto=SimpleNamespace(
            olm=SimpleNamespace(
                tracked_users={own, peer},
                users_for_key_query=set(),
                wedged_devices=(),
                key_request_devices_no_session=(),
            ),
            capture=lambda: None,
        ),
        _transport=SimpleNamespace(
            request=AsyncMock(
                return_value=json.dumps({"joined": {own: {}, peer: {}}}).encode()
            )
        ),
    )
    owner._outbound = OutboundCrypto(owner)
    client._durable_session = owner
    client.joined_members = owner._outbound.joined_members
    session = MatrixSession(
        MatrixConfig(adapter_id="test", homeserver="https://example.org", user_id=own)
    )
    session._client = client
    session._durable_sync_enabled = True
    entered, release = asyncio.Event(), asyncio.Event()
    response_body = json.dumps({"joined": {own: {}, peer: {}}}).encode()

    async def membership_response(*args):
        entered.set()
        await release.wait()
        return response_body

    owner._transport.request.side_effect = membership_response
    reports = [
        asyncio.create_task(session.peer_device_trust_report(room_id)) for _ in range(2)
    ]
    try:
        await asyncio.wait_for(entered.wait(), 5)
    finally:
        release.set()
    first, second = await asyncio.wait_for(asyncio.gather(*reports), 5)
    assert first == second
    report = first
    owner._transport.request.assert_awaited_once()
    assert room.members_synced is False and peer not in room.users
    assert owner._outbound.member_cache[room_id] == [own, peer]
    assert report.trust_view_available is True
    assert report.recipients[0].user_id == peer
    assert report.recipients[0].devices[0].verified is True


def test_pinned_nio_keys_query_delegates_to_durable_locked_transaction() -> None:
    """Review the real pinned SDK path, not an invented owner method."""
    import inspect

    import nio
    from nio.durable.outbound import OutboundCrypto

    entrypoint = inspect.getsource(nio.AsyncClient.keys_query)
    locked_query = inspect.getsource(OutboundCrypto.keys_query)
    queued_query = inspect.getsource(OutboundCrypto._keys_query)
    assert "self._durable_session._outbound.keys_query" in entrypoint
    assert "async with self.session._crypto_lock:" in locked_query
    assert "await self._keys_query(user_set)" in locked_query
    assert "with self.transaction():" in queued_query
