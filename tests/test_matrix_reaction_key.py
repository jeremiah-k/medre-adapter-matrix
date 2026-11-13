"""Canonical reactions require explicit keys independent of presentation text."""

from datetime import datetime, timezone

import pytest
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.core.events import CanonicalEvent, EventMetadata, EventRelation, NativeRef
from medre.core.rendering.renderer import RenderingContext


def reaction(key, bound):
    return CanonicalEvent(
        event_id="reaction",
        event_kind="message.reacted",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="radio",
        source_transport_id="alice",
        source_channel_id="0",
        parent_event_id=None,
        lineage=(),
        relations=(
            EventRelation(
                "reaction",
                None,
                NativeRef(
                    "matrix" if bound else "radio",
                    "!room:example.org" if bound else "0",
                    "$parent" if bound else "0",
                ),
                key,
                "preview",
            ),
        ),
        payload={"body": "🔥", "emoji": "🔥", "key": "🔥"},
        metadata=EventMetadata(),
    )


@pytest.mark.parametrize("key", [None, "", " \t\n"])
@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("strategy", ["direct", "fallback_text"])
async def test_missing_key_never_uses_body_or_payload_emoji(key, bound, strategy):
    with pytest.raises(ValueError, match="explicit nonempty key"):
        await MatrixRenderer().render(
            reaction(key, bound),
            RenderingContext(strategy, "matrix", "!room:example.org", "matrix"),
        )


@pytest.mark.parametrize("key", ["🔥", "❤️", "👍🏽", " custom key "])
@pytest.mark.parametrize("strategy", ["direct", "fallback_text"])
async def test_described_reaction_preserves_exact_explicit_key(key, strategy):
    result = await MatrixRenderer().render(
        reaction(key, True),
        RenderingContext(strategy, "matrix", "!room:example.org", "matrix"),
    )
    operation = result.payload["_matrix_operation"]
    assert operation["event_type"] == "m.room.message"
    content = operation["content"]
    assert "m.relates_to" not in content
    assert f"reacted {key} to" in content["body"]


@pytest.mark.parametrize("strategy", ["direct", "fallback_text"])
@pytest.mark.parametrize("reaction_first", [False, True])
async def test_reply_relation_does_not_license_a_missing_reaction_key(
    strategy, reaction_first
):
    from msgspec.structs import replace

    source = reaction(None, True)
    reply = EventRelation(
        "reply", None, NativeRef("matrix", "!room:example.org", "$reply"), None, None
    )
    relations = (
        (*source.relations, reply) if reaction_first else (reply, *source.relations)
    )
    with pytest.raises(ValueError, match="explicit nonempty key"):
        await MatrixRenderer().render(
            replace(source, relations=relations),
            RenderingContext(strategy, "matrix", "!room:example.org", "matrix"),
        )
