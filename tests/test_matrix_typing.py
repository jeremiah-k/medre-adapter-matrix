"""Transient native typing is scoped separately from canonical message delivery."""

import asyncio
from datetime import datetime, timezone
import logging
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.session import MatrixSession
from medre.adapters.matrix.errors import MatrixConnectionError
from medre.config.adapters.matrix import MatrixConfig
from medre.core.contracts.adapter import AdapterContext, AdapterPermanentError
from medre.core.contracts.typing import TypingSnapshot, TypingTarget, TypingUpdate

ROOM = "!room:example.org"


def adapter():
    """Bind a configured relay to independent message and activity collectors."""
    config = MatrixConfig(
        adapter_id="matrix",
        homeserver="https://example.org",
        user_id="@bot:example.org",
        access_token="test-only",
        encryption_mode="plaintext",
        room_allowlist=[ROOM],
        auto_join_rooms=(ROOM,),
    )
    value = MatrixAdapter(config)
    publish, messages = AsyncMock(return_value=True), AsyncMock()
    value.ctx = AdapterContext(
        "matrix",
        messages,
        logging.getLogger("typing-test"),
        lambda: datetime.now(timezone.utc),
        asyncio.Event(),
        publish_typing=publish,
    )
    value._started = True
    return value, publish, messages


async def test_native_replacement_list_filters_own_identity_and_keeps_empty_stops():
    """The core receives whole native membership, with no provider-owned actor cache."""
    value, publish, messages = adapter()
    await value._on_room_typing(
        ROOM, ["@bot:example.org", "@alice:example.org", "@bob:example.org"]
    )
    assert publish.call_args.args[0] == TypingSnapshot(
        ROOM, ("@alice:example.org", "@bob:example.org")
    )
    await value._on_room_typing(ROOM, [])
    assert publish.call_args.args[0] == TypingSnapshot(ROOM, ())
    assert not messages.called


@pytest.mark.parametrize(
    "case", ["foreign_room", "duplicate", "invalid_actor", "startup", "disabled"]
)
async def test_optional_typing_cannot_bypass_native_scope_or_publish_history(case):
    """Malformed or unconfigured optional state does not affect ordinary ingress."""
    value, publish, messages = adapter()
    room, users = ROOM, ["@alice:example.org"]
    if case == "foreign_room":
        room = "!elsewhere:example.org"
    elif case == "duplicate":
        users *= 2
    elif case == "invalid_actor":
        users = ["bad\nidentity"]
    elif case == "startup":
        value._starting = True
    else:
        value.ctx.publish_typing = None
    await value._on_room_typing(room, users)
    assert not publish.called and not messages.called


async def test_sender_uses_guarded_native_session_without_message_rendering():
    """Desired state is scoped to this adapter and an allowed joined room."""
    value, _, messages = adapter()
    session = SimpleNamespace(
        closed=False,
        is_room_member=lambda _: True,
        is_room_encrypted=lambda _: False,
        crypto_enabled=False,
        room_typing=AsyncMock(),
    )
    value._session = session
    await value.send_typing(TypingUpdate(TypingTarget("matrix", ROOM), 10))
    session.room_typing.assert_awaited_once_with(ROOM, True, 10000)
    assert not messages.called
    with pytest.raises(AdapterPermanentError):
        await value.send_typing(TypingUpdate(TypingTarget("other", ROOM), 10))
    with pytest.raises(AdapterPermanentError):
        await value.send_typing(
            TypingUpdate(TypingTarget("matrix", "!wrong:example.org"), 10)
        )


async def test_session_owns_typing_request_and_refuses_native_errors(monkeypatch):
    """A transport response error cannot become a fake successful message handoff."""
    value, _, _ = adapter()

    class Response:
        """Independent native response shape used without importing an SDK."""

    monkeypatch.setitem(
        sys.modules, "nio", SimpleNamespace(RoomTypingResponse=Response)
    )
    session = MatrixSession(value._config)
    session._closed = False
    session._client = SimpleNamespace(room_typing=AsyncMock(return_value=Response()))
    await session.room_typing(ROOM, True, 10000)
    session._client.room_typing.assert_awaited_once_with(
        ROOM, typing_state=True, timeout=10000
    )
    session._client.room_typing.return_value = object()
    with pytest.raises(MatrixConnectionError, match="typing request failed"):
        await session.room_typing(ROOM, False, 1)


async def test_live_ephemeral_callback_forwards_only_the_native_actor_list():
    """Durable timeline dispatch never supplies activity to this callback."""
    callback = AsyncMock()
    value, _, _ = adapter()
    session = MatrixSession(value._config, typing_callback=callback)
    await session._on_typing(
        SimpleNamespace(room_id=ROOM), SimpleNamespace(users=["@peer:example.org"])
    )
    callback.assert_awaited_once_with(ROOM, ["@peer:example.org"])


async def test_fake_activity_uses_optional_sink_and_separate_bounded_history():
    """SDK-free fake contracts mirror native capability without message evidence."""
    import logging
    from datetime import datetime, timezone
    from medre.core.contracts.adapter import AdapterContext
    from medre.core.contracts.typing import TypingActivity
    from medre.adapters.fakes.matrix import FakeMatrixAdapter

    fake = FakeMatrixAdapter("fake")
    activity = TypingActivity("source", "actor", True)
    assert not await fake.simulate_typing(activity)
    sink = AsyncMock(return_value=True)
    ctx = AdapterContext(
        "fake",
        AsyncMock(),
        logging.getLogger("fake-typing"),
        lambda: datetime.now(timezone.utc),
        asyncio.Event(),
        publish_typing=sink,
    )
    await fake.start(ctx)
    assert (await fake.health_check()).capabilities.typing
    assert await fake.simulate_typing(activity)
    sink.assert_awaited_once_with(activity)
    update = TypingUpdate(TypingTarget("fake", "destination"), 5)
    for _ in range(1001):
        await fake.send_typing(update)
    assert len(fake.typing_updates) == 1000
    assert not fake.delivered_payloads
    assert not ctx.publish_inbound.called
    await fake.stop()
    assert not await fake.simulate_typing(activity)
