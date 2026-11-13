"""Delivered reactions are always described emote messages.

Every reaction the Matrix adapter delivers crosses a network boundary
and is emitted through the bridge's own session identity, so a native
``m.reaction`` annotation is never attribution-correct: the annotation
would carry the bridge identity (the reacting mesh user's name vanishes)
and Matrix annotation idempotency would collapse same-key reactions from
different source users into one event.  These tests lock the
described-delivery contract for the relayed-message case — a reaction
whose target resolves to a Matrix event ID owned by the destination.
"""

from __future__ import annotations

from datetime import UTC, datetime
from msgspec.structs import replace

import pytest
from medre.adapters.fakes.matrix import FakeMatrixAdapter
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.config.adapters.matrix import MatrixConfig
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    EventRelation,
    NativeRef,
)
from medre.core.planning.delivery_plan import DeliveryStrategyMethod
from medre.core.rendering.renderer import RenderingContext
from medre_matrix_tests.helpers.matrix import matrix_payload_content as _payload_content

_TARGET = "chat-instance"
_ROOM = "!room:example.org"


def _renderer_with_auto_prefix() -> MatrixRenderer:
    """Renderer with a target config whose default relay_prefix selects
    automatic attribution (``[{sender}]: ``)."""
    return MatrixRenderer(
        configs={
            _TARGET: MatrixConfig(
                _TARGET, "https://example.org", "@relay:example.org"
            )
        }
    )


def _mesh_reaction(
    sender: str = "alice-mesh",
    *,
    key: str = "👍",
    target_adapter: str = _TARGET,
    target_message_id: str | None = "$mx-relayed-1",
    fallback_text: str | None = "hello mesh world",
    metadata: dict[str, object] | None = None,
) -> CanonicalEvent:
    """A mesh-source reaction to a message relayed to Matrix.

    By default the reaction's ``target_native_ref`` belongs to the
    Matrix target adapter — exactly the relayed-message case that used
    to render a native ``m.reaction`` annotation.
    """
    ref = (
        NativeRef(target_adapter, _ROOM, target_message_id)
        if target_message_id is not None
        else None
    )
    return CanonicalEvent(
        event_id=f"reaction-{sender}-{target_message_id}",
        event_kind="message.reacted",
        schema_version=1,
        timestamp=datetime.now(UTC),
        source_adapter="radio-east",
        source_transport_id=sender,
        source_channel_id="LongFast",
        parent_event_id=None,
        lineage=(),
        relations=(
            EventRelation(
                relation_type="reaction",
                target_event_id="canonical-relayed-1",
                target_native_ref=ref,
                key=key,
                fallback_text=fallback_text,
                metadata=metadata or {},
            ),
        ),
        payload={"body": key, "key": key},
        metadata=EventMetadata(),
    )


def _ctx(
    delivery_strategy: DeliveryStrategyMethod = "direct",
) -> RenderingContext:
    return RenderingContext(
        delivery_strategy=delivery_strategy,
        target_adapter=_TARGET,
        target_platform="matrix",
        target_channel=_ROOM,
    )


def _assert_described_emote(result, sender: str, key: str = "👍") -> None:
    """Assert the rendering is one described emote room message."""
    operation = result.payload["_matrix_operation"]
    assert operation["event_type"] == "m.room.message"
    content = _payload_content(result)
    assert "m.relates_to" not in content
    assert "m.annotation" not in str(content)
    assert content["msgtype"] == "m.emote"
    body = content["body"]
    # The attribution prefix names the reacting source user.
    assert body.startswith(f"\n [{sender}]: reacted {key} to")
    # Exactly one intentional leading newline.
    assert len(body) - len(body.lstrip("\n")) == 1
    # The bounded target preview is quoted.
    assert 'to "hello mesh world"' in body
    # The HTML body encodes the single break via its <p> boundary only.
    assert content["formatted_body"] == (
        f'<p>[{sender}]: reacted {key} to "hello mesh world"</p>'
    )


@pytest.mark.parametrize("delivery_strategy", ["direct", "fallback_text"])
async def test_mesh_reaction_to_relayed_message_is_one_described_emote(
    delivery_strategy,
) -> None:
    """A resolvable Matrix target renders one described emote, never an annotation."""
    renderer = _renderer_with_auto_prefix()
    event = _mesh_reaction("alice-mesh")
    result = await renderer.render(event, _ctx(delivery_strategy))
    _assert_described_emote(result, "alice-mesh")


async def test_deliver_path_never_emits_a_native_reaction_event(
    make_adapter_context,
) -> None:
    """The adapter deliver path sends only ``m.room.message`` operations."""
    renderer = _renderer_with_auto_prefix()
    event = _mesh_reaction("alice-mesh")
    result = await renderer.render(event, _ctx("direct"))

    adapter = FakeMatrixAdapter(_TARGET)
    await adapter.start(make_adapter_context(_TARGET))
    await adapter.deliver(result)

    assert len(adapter.sent_operations) == 1
    operation = adapter.sent_operations[0]
    assert operation.kind == "send_event"
    assert operation.event_type == "m.room.message"
    sent = adapter.delivered_payloads[0].payload["_matrix_operation"]["content"]
    assert "m.relates_to" not in sent
    assert sent["msgtype"] == "m.emote"


async def test_three_mesh_users_same_key_produce_three_distinct_messages() -> None:
    """Same key on the same target from three users yields three messages.

    Matrix ``m.reaction`` annotations are idempotent per (sender, key),
    so the native form collapsed the second and third reaction into
    no-ops.  Described deliveries are distinct room messages, each
    attributed to its own source user.
    """
    renderer = _renderer_with_auto_prefix()
    senders = ("alice-mesh", "bob-mesh", "carol-mesh")

    results = [
        await renderer.render(_mesh_reaction(sender), _ctx("direct"))
        for sender in senders
    ]

    bodies = []
    for sender, result in zip(senders, results, strict=True):
        _assert_described_emote(result, sender)
        bodies.append(result.payload["_matrix_operation"]["content"]["body"])

    # Three distinct delivered messages — no collapse.
    assert len(set(bodies)) == 3
    for sender, body in zip(senders, bodies, strict=True):
        assert body.startswith(f"\n [{sender}]: reacted 👍 to")


async def test_unobserved_target_without_enrichment_names_an_earlier_message() -> None:
    """Empty enrichment degrades to ``an earlier message``, never empty quotes."""
    renderer = _renderer_with_auto_prefix()
    event = _mesh_reaction(
        "alice-mesh",
        target_adapter="radio-east",
        target_message_id=None,
        fallback_text=None,
    )
    result = await renderer.render(event, _ctx("direct"))

    body = result.payload["_matrix_operation"]["content"]["body"]
    assert body.startswith("\n [alice-mesh]: reacted 👍 to an earlier message")
    assert '""' not in body


async def test_custom_exact_key_is_rendered_verbatim_with_resolved_target() -> None:
    """The explicit key is preserved verbatim even when the target resolves."""
    renderer = _renderer_with_auto_prefix()
    event = _mesh_reaction("alice-mesh", key=" custom key ")
    result = await renderer.render(event, _ctx("direct"))

    body = result.payload["_matrix_operation"]["content"]["body"]
    assert 'reacted  custom key  to "hello mesh world"' in body


async def test_enriched_original_text_is_quoted_with_resolved_target() -> None:
    """Core relation enrichment supplies the quoted preview when present."""
    renderer = _renderer_with_auto_prefix()
    event = _mesh_reaction(
        "alice-mesh",
        fallback_text=None,
        metadata={"original_text": "enriched target text"},
    )
    result = await renderer.render(event, _ctx("direct"))

    body = result.payload["_matrix_operation"]["content"]["body"]
    assert 'reacted 👍 to "enriched target text"' in body


async def test_target_resolution_does_not_leak_into_described_content() -> None:
    """The resolved Matrix event ID never becomes a wire relation target."""
    renderer = _renderer_with_auto_prefix()
    event = _mesh_reaction(
        "alice-mesh",
        target_message_id="$mx-relayed-1",
    )
    event = replace(event, event_id="reaction-under-test")
    result = await renderer.render(event, _ctx("direct"))

    operation = result.payload["_matrix_operation"]
    content = _payload_content(result)
    assert "m.relates_to" not in content
    assert "m.annotation" not in str(content)
    # The visible bodies quote the target text, never the target ID.
    assert "$mx-relayed-1" not in content["body"]
    assert "$mx-relayed-1" not in content["formatted_body"]
    assert operation["event_type"] == "m.room.message"
