"""Target-selection rule tests for MatrixRenderer.

Locks the current target-selection contracts:

* MatrixRenderer uses ``relations[0]`` only — subsequent relations are ignored.
* Reply target: ``_matrix_target_event_id`` returns the native event ID only
  when ``target_native_ref.adapter == target_adapter`` and
  ``native_message_id`` is non-empty.  Cross-adapter refs are rejected.
* Reaction target: true ``m.reaction`` when a Matrix-native target exists;
  ``m.emote`` fallback otherwise.
* Missing target (empty relations): renders plain ``m.text`` with no
  ``m.relates_to``.
* Stale / non-existent native target: the renderer emits the ID without
  pre-validation — it trusts the ``target_native_ref`` blindly.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TARGET = "chat-instance"


def _ctx(
    target_adapter: str = _TARGET,
    delivery_strategy: DeliveryStrategyMethod = "direct",
) -> RenderingContext:
    return RenderingContext(
        delivery_strategy=delivery_strategy,
        target_adapter=target_adapter,
        target_platform="matrix",
    )


def _event(
    relations: tuple[EventRelation, ...] = (),
    payload: dict[str, object] | None = None,
    source_adapter: str = "transport-1",
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id="evt-001",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(UTC),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=relations,
        payload=payload or {"body": "hello"},
        metadata=EventMetadata(),
    )


def _reply_rel(
    adapter: str = _TARGET,
    native_message_id: str = "$mx-ev-42",
) -> EventRelation:
    return EventRelation(
        relation_type="reply",
        target_event_id="canonical-42",
        target_native_ref=NativeRef(
            adapter=adapter,
            native_channel_id="!room:server",
            native_message_id=native_message_id,
        ),
        key=None,
        fallback_text=None,
    )


def _reaction_rel(
    adapter: str = _TARGET,
    native_message_id: str = "$mx-ev-99",
    key: str = "👍",
) -> EventRelation:
    return EventRelation(
        relation_type="reaction",
        target_event_id="canonical-99",
        target_native_ref=NativeRef(
            adapter=adapter,
            native_channel_id="!room:server",
            native_message_id=native_message_id,
        ),
        key=key,
        fallback_text=None,
    )


# ---------------------------------------------------------------------------
# Reply target selection
# ---------------------------------------------------------------------------


async def test_reply_uses_native_ref_owned_by_target_adapter() -> None:
    """Reply renders m.in_reply_to when native ref belongs to target."""
    renderer = MatrixRenderer()
    event = _event(relations=(_reply_rel(adapter=_TARGET, native_message_id="$mx-1"),))
    result = await renderer.render(event, _ctx())

    relates = _payload_content(result).get("m.relates_to")
    assert relates is not None
    assert relates["m.in_reply_to"]["event_id"] == "$mx-1"  # type: ignore[index]


async def test_reply_ignores_cross_adapter_native_ref() -> None:
    """Reply with a native ref from a different adapter produces no m.in_reply_to."""
    renderer = MatrixRenderer()
    event = _event(
        relations=(_reply_rel(adapter="other-radio", native_message_id="$mx-2"),)
    )
    result = await renderer.render(event, _ctx())

    assert _payload_content(result).get("m.relates_to") is None


async def test_reply_without_native_ref_no_relates_to() -> None:
    """Reply relation with no target_native_ref produces no m.relates_to."""
    renderer = MatrixRenderer()
    rel = EventRelation(
        relation_type="reply",
        target_event_id="canonical-42",
        target_native_ref=None,
        key=None,
        fallback_text=None,
    )
    event = _event(relations=(rel,))
    result = await renderer.render(event, _ctx())

    assert _payload_content(result).get("m.relates_to") is None


async def test_reply_with_empty_native_message_id_no_relates_to() -> None:
    """Reply whose native_message_id is empty produces no m.relates_to."""
    renderer = MatrixRenderer()
    event = _event(relations=(_reply_rel(adapter=_TARGET, native_message_id=""),))
    result = await renderer.render(event, _ctx())

    assert _payload_content(result).get("m.relates_to") is None


# ---------------------------------------------------------------------------
# Reaction target selection
# ---------------------------------------------------------------------------


async def test_reaction_true_m_reaction_when_native_target_exists() -> None:
    """Reaction renders true m.reaction when Matrix-native target exists."""
    renderer = MatrixRenderer()
    event = _event(
        relations=(_reaction_rel(adapter=_TARGET, native_message_id="$mx-r1"),),
        payload={"body": "👍"},
    )
    result = await renderer.render(event, _ctx())

    assert result.payload["_matrix_operation"]["event_type"] == "m.reaction"
    relates = _payload_content(result)["m.relates_to"]
    assert relates["rel_type"] == "m.annotation"  # type: ignore[index]
    assert relates["event_id"] == "$mx-r1"  # type: ignore[index]


async def test_reaction_emote_fallback_when_no_native_target() -> None:
    """Reaction falls back to m.emote when native ref is from a different adapter."""
    renderer = MatrixRenderer()
    event = _event(
        relations=(_reaction_rel(adapter="other-radio", native_message_id="$mx-r2"),),
        payload={"body": "❤️"},
    )
    result = await renderer.render(event, _ctx())

    # No true m.reaction; should be emote fallback
    assert result.payload["_matrix_operation"]["event_type"] == "m.room.message"
    assert _payload_content(result)["msgtype"] == "m.emote"


async def test_reaction_emote_fallback_when_no_native_ref_at_all() -> None:
    """Reaction with no target_native_ref falls back to m.emote."""
    renderer = MatrixRenderer()
    rel = EventRelation(
        relation_type="reaction",
        target_event_id="canonical-99",
        target_native_ref=None,
        key="👍",
        fallback_text=None,
    )
    event = _event(relations=(rel,), payload={"body": "👍"})
    result = await renderer.render(event, _ctx())

    assert _payload_content(result)["msgtype"] == "m.emote"


# ---------------------------------------------------------------------------
# Reaction emote fallback shaping
# ---------------------------------------------------------------------------


def _reaction(
    fallback_text: str | None = None,
    metadata: dict[str, object] | None = None,
    key: str = "🎉",
) -> EventRelation:
    return EventRelation(
        relation_type="reaction",
        target_event_id="canonical-99",
        target_native_ref=None,
        key=key,
        fallback_text=fallback_text,
        metadata=metadata or {},
    )


async def test_reaction_emote_body_has_exactly_one_leading_newline() -> None:
    """One intentional newline separates the emote prefix from the wording.

    The plain body carries exactly one leading newline (``* Relay`` then one
    blank line), and the HTML formatted body encodes that same break exactly
    once via its ``<p>`` boundary — never a stacked ``<br/>``.
    """
    renderer = MatrixRenderer()
    event = _event(relations=(_reaction(),), payload={"body": "🎉"})
    result = await renderer.render(event, _ctx())

    content = _payload_content(result)
    assert content["msgtype"] == "m.emote"
    body = content["body"]
    assert body.startswith("\n reacted 🎉 to an earlier message")
    assert len(body) - len(body.lstrip("\n")) == 1
    assert content["formatted_body"] == "<p>reacted 🎉 to an earlier message</p>"


async def test_reaction_emote_quotes_enriched_target_text() -> None:
    """The fallback quotes the canonical target preview, never empty quotes."""
    renderer = MatrixRenderer()
    event = _event(
        relations=(_reaction(fallback_text="Ok on medre now. How do things look?"),),
        payload={"body": "🎉"},
    )
    result = await renderer.render(event, _ctx())

    body = _payload_content(result)["body"]
    assert body.startswith("\n reacted 🎉 to")
    assert len(body) - len(body.lstrip("\n")) == 1
    assert 'to "Ok on medre now. How do things look?"' in body


@pytest.mark.parametrize(
    ("template", "expected_body", "expected_html"),
    [
        (
            "[RelayA]: ",
            '\n [RelayA]: reacted 🎉 to "preview"',
            '<p>[RelayA]: reacted 🎉 to "preview"</p>',
        ),
        (
            "[RelayA]",
            '\n [RelayA] reacted 🎉 to "preview"',
            '<p>[RelayA] reacted 🎉 to "preview"</p>',
        ),
        (
            "\n[RelayA]: ",
            '\n [RelayA]: reacted 🎉 to "preview"',
            '<p>[RelayA]: reacted 🎉 to "preview"</p>',
        ),
        (
            "[RelayA]:\n",
            '\n [RelayA]: reacted 🎉 to "preview"',
            '<p>[RelayA]: reacted 🎉 to "preview"</p>',
        ),
        (
            "\n",
            '\n reacted 🎉 to "preview"',
            '<p>reacted 🎉 to "preview"</p>',
        ),
    ],
)
async def test_reaction_emote_prefixed_body_has_exactly_one_leading_newline(
    template, expected_body, expected_html
) -> None:
    """Prefixed emote fallbacks keep the one-newline design in both bodies.

    The visible prefix attribution is unchanged apart from line breaks, which
    a configured prefix may not contribute: newline-prefixed, newline-terminated,
    and whitespace-only templates all yield exactly one leading newline in the
    plain body and the ``<p>`` boundary as the single break in the HTML body,
    with no derived ``<br/>`` stacked after it.
    """
    renderer = MatrixRenderer(
        configs={
            _TARGET: MatrixConfig(_TARGET, "https://example.org", "@relay:example.org")
        }
    )
    renderer._configs[_TARGET] = replace(
        renderer._configs[_TARGET], relay_prefix=template
    )
    event = _event(
        relations=(_reaction(fallback_text="preview"),), payload={"body": "🎉"}
    )
    result = await renderer.render(event, _ctx())

    content = _payload_content(result)
    assert content["msgtype"] == "m.emote"
    assert content["body"] == expected_body
    assert content["formatted_body"] == expected_html
    assert "<br/>" not in content["formatted_body"]


async def test_reaction_emote_prefers_enrichment_original_text_metadata() -> None:
    """``metadata['original_text']`` from core enrichment wins over nothing."""
    renderer = MatrixRenderer()
    event = _event(
        relations=(_reaction(metadata={"original_text": "preview from enrichment"}),),
        payload={"body": "🎉"},
    )
    result = await renderer.render(event, _ctx())

    assert 'reacted 🎉 to "preview from enrichment"' in _payload_content(result)["body"]


async def test_reaction_emote_truncates_long_target_text() -> None:
    """Long target previews truncate with the house ellipsis convention."""
    renderer = MatrixRenderer()
    long_text = "x" * 60
    event = _event(
        relations=(_reaction(fallback_text=long_text),), payload={"body": "🎉"}
    )
    result = await renderer.render(event, _ctx())

    body = _payload_content(result)["body"]
    assert f'to "{"x" * 40}..."' in body
    assert long_text not in body


# ---------------------------------------------------------------------------
# Missing target (no relations)
# ---------------------------------------------------------------------------


async def test_no_relations_renders_plain_text() -> None:
    """Event with no relations renders as plain m.text."""
    renderer = MatrixRenderer()
    event = _event()
    result = await renderer.render(event, _ctx())

    assert _payload_content(result)["msgtype"] == "m.text"
    assert _payload_content(result)["body"] == "hello"
    assert "m.relates_to" not in _payload_content(result)
    assert result.payload["_matrix_operation"]["event_type"] == "m.room.message"


# ---------------------------------------------------------------------------
# Stale target / no pre-validation
# ---------------------------------------------------------------------------


async def test_stale_native_target_emitted_without_validation() -> None:
    """Renderer emits the native event ID even if it no longer exists on the server.

    The renderer trusts the ``target_native_ref`` blindly — it does not
    validate whether the Matrix event still exists.  This is the current
    contract: pre-validation is the adapter's responsibility, not the
    renderer's.
    """
    renderer = MatrixRenderer()
    event = _event(
        relations=(_reply_rel(adapter=_TARGET, native_message_id="$mx-deleted-event"),),
    )
    result = await renderer.render(event, _ctx())

    relates = _payload_content(result)["m.relates_to"]
    assert relates["m.in_reply_to"]["event_id"] == "$mx-deleted-event"  # type: ignore[index]


# ---------------------------------------------------------------------------
# Multiple relations — semantic selection is order-independent
# ---------------------------------------------------------------------------


async def test_reply_precedence_when_reply_is_first() -> None:
    """Reply wins over reaction regardless of tuple position."""
    renderer = MatrixRenderer()
    rel1 = _reply_rel(adapter=_TARGET, native_message_id="$mx-first")
    rel2 = _reaction_rel(adapter=_TARGET, native_message_id="$mx-second")
    event = _event(relations=(rel1, rel2))
    result = await renderer.render(event, _ctx())

    # Reply is selected semantically, not because it is first.
    relates = _payload_content(result)["m.relates_to"]
    assert "m.in_reply_to" in relates  # type: ignore[operator]
    assert relates["m.in_reply_to"]["event_id"] == "$mx-first"  # type: ignore[index]
    # No reaction annotation from rel2
    assert result.payload["_matrix_operation"]["event_type"] == "m.room.message"


async def test_reply_precedence_when_reaction_is_first() -> None:
    """Reply still wins when the reaction appears first."""
    renderer = MatrixRenderer()
    rel1 = _reaction_rel(adapter=_TARGET, native_message_id="$mx-r-first", key="🔥")
    rel2 = _reply_rel(adapter=_TARGET, native_message_id="$mx-reply-second")
    event = _event(relations=(rel1, rel2), payload={"body": "🔥"})
    result = await renderer.render(event, _ctx())

    assert result.payload["_matrix_operation"]["event_type"] == "m.room.message"
    relates = _payload_content(result)["m.relates_to"]
    assert "m.in_reply_to" in relates  # type: ignore[operator]
    assert relates["m.in_reply_to"]["event_id"] == "$mx-reply-second"  # type: ignore[index]


# ---------------------------------------------------------------------------
# Fallback-text strategy
# ---------------------------------------------------------------------------


async def test_fallback_text_strategy_no_native_relates_to() -> None:
    """Under fallback_text strategy, no native m.relates_to is emitted."""
    renderer = MatrixRenderer()
    event = _event(relations=(_reply_rel(adapter=_TARGET, native_message_id="$mx-fb"),))
    result = await renderer.render(event, _ctx(delivery_strategy="fallback_text"))

    assert "m.relates_to" not in _payload_content(result)
    assert result.payload["_matrix_operation"]["event_type"] == "m.room.message"
    assert result.fallback_applied == "strategy_fallback_text"
