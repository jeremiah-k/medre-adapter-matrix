"""Final text budgets and unrepresented relations across native Matrix operations."""

from datetime import UTC, datetime

import pytest

from medre.adapters.matrix.outbound import MatrixOutboundOperation
from medre.adapters.matrix.renderer import MatrixNativeMutationError, MatrixRenderer
from medre.core.events import CanonicalEvent, EventMetadata, EventRelation, NativeRef
from medre.core.events.attachments import ATTACHMENT_PAYLOAD_KEY, AttachmentDescriptor
from medre.core.events.canonical import RelationTargetFact
from medre.core.rendering.renderer import RenderingContext


def event(kind, relations, body="caption"):
    payload = {"body": body}
    if kind == "message.file":
        payload[ATTACHMENT_PAYLOAD_KEY] = AttachmentDescriptor(
            kind="image",
            filename="photo.png",
            mime_type="image/png",
            size_bytes=11,
            content_ref="sha256:" + "ab" * 32,
        ).to_payload()
    return CanonicalEvent(
        event_id="boundary",
        event_kind=kind,
        schema_version=1,
        timestamp=datetime.now(UTC),
        source_adapter="source",
        source_transport_id="sender",
        source_channel_id="source-room",
        parent_event_id=None,
        lineage=(),
        relations=relations,
        metadata=EventMetadata(),
        payload=payload,
    )


def relation(kind, native_id=None, key=None):
    return EventRelation(
        kind,
        None,
        NativeRef("target", "!room:example.org", native_id) if native_id else None,
        key,
        None,
        target_fact=(
            RelationTargetFact(
                status="bound_owned",
                adapter="target",
                native_channel_id="!room:example.org",
                native_message_id=native_id,
            )
            if native_id
            else None
        ),
    )


def context(**limits):
    return RenderingContext("direct", "target", "!room:example.org", "matrix", **limits)


async def test_media_preserves_only_unrepresented_relation_context():
    result = await MatrixRenderer().render(
        event(
            "message.file",
            (relation("reply", "$parent"), relation("reaction", key="👍")),
        ),
        context(),
    )
    operation = MatrixOutboundOperation.from_payload(result.payload)
    assert operation.content["body"] == ("reacted 👍 to an earlier message caption")
    assert operation.content["m.relates_to"] == {
        "m.in_reply_to": {"event_id": "$parent"}
    }
    assert operation.content_ref == "sha256:" + "ab" * 32


@pytest.mark.parametrize("limits", [{"max_text_bytes": 7}, {"max_text_chars": 3}])
async def test_media_caption_obeys_final_budget(limits):
    result = await MatrixRenderer().render(
        event("message.file", (), body="ééééé"), context(**limits)
    )
    operation = MatrixOutboundOperation.from_payload(result.payload)
    assert operation.content["body"] == "ééé"
    assert result.truncated
    assert result.metadata["rendered_text_bytes"] == 6
    assert operation.content["filename"] == "photo.png"


async def test_edit_retains_secondary_relation_without_changing_native_target():
    result = await MatrixRenderer().render(
        event(
            "message.edited",
            (relation("edit", "$original"), relation("reaction", key="👍")),
            body="replacement",
        ),
        context(),
    )
    operation = MatrixOutboundOperation.from_payload(result.payload)
    content = operation.content
    assert content["m.new_content"]["body"] == (
        "reacted 👍 to an earlier message replacement"
    )
    assert content["body"] == "* " + content["m.new_content"]["body"]
    assert content["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$original"}


@pytest.mark.parametrize("limit", ["max_text_bytes", "max_text_chars"])
async def test_edit_checks_fallback_body_budget_without_clipping(limit):
    original = event("message.edited", (relation("edit", "$original"),), body="hello")
    renderer = MatrixRenderer()
    with pytest.raises(ValueError, match="edit exceeds"):
        await renderer.render(original, context(**{limit: 6}))
    result = await renderer.render(original, context(**{limit: 7}))
    operation = MatrixOutboundOperation.from_payload(result.payload)
    assert operation.content["body"] == "* hello"
    assert operation.content["m.new_content"]["body"] == "hello"


@pytest.mark.parametrize("second", ["edit", "delete"])
async def test_native_mutation_rejects_multiple_targets(second):
    with pytest.raises(MatrixNativeMutationError, match="multiple mutation"):
        await MatrixRenderer().render(
            event(
                "message.edited",
                (relation("edit", "$one"), relation(second, "$two")),
            ),
            context(),
        )


@pytest.mark.parametrize("strategy", ["direct", "fallback_text"])
async def test_single_reaction_on_authored_message_keeps_body(strategy):
    from dataclasses import replace

    original = event(
        "message.created",
        (relation("reaction", "$parent", "👍"),),
        body="Please check this",
    )
    result = await MatrixRenderer().render(
        original, replace(context(), delivery_strategy=strategy)
    )
    content = MatrixOutboundOperation.from_payload(result.payload).content
    assert content["msgtype"] == "m.text"
    assert content["body"] == "reacted 👍 to an earlier message Please check this"
    assert "m.relates_to" not in content
    assert original.payload["body"] == "Please check this"


@pytest.mark.parametrize("strategy", ["direct", "fallback_text"])
async def test_unbound_reply_description_has_budget_precedence(strategy):
    from dataclasses import replace

    original = event(
        "message.created",
        (relation("reaction", key="👍"), relation("reply")),
        body="answer",
    )
    result = await MatrixRenderer().render(
        original, replace(context(max_text_chars=31), delivery_strategy=strategy)
    )
    content = MatrixOutboundOperation.from_payload(result.payload).content
    assert content["body"].startswith("[replying to: unknown message]")
    assert result.truncated
    assert original.relations[0].relation_type == "reaction"


@pytest.mark.parametrize(
    "kind,mutation",
    [
        ("message.created", "edit"),
        ("message.created", "delete"),
        ("message.edited", "delete"),
        ("message.deleted", "edit"),
        ("message.edited", None),
        ("message.deleted", None),
    ],
)
async def test_native_mutation_requires_matching_kind_and_relation(kind, mutation):
    """Even owned targets cannot turn another event kind into a native mutation."""
    original = event(
        kind, (relation(mutation, "$owned"),) if mutation else (), body="authored"
    )
    with pytest.raises(MatrixNativeMutationError, match="event kind"):
        await MatrixRenderer().render(original, context())
    assert original.payload["body"] == "authored"


@pytest.mark.parametrize("strategy", ["direct", "fallback_text"])
@pytest.mark.parametrize("styled", [False, True])
async def test_relation_free_reaction_never_forwards_payload_body(strategy, styled):
    from dataclasses import replace

    original = event("message.reacted", (), body="authored")
    if styled:
        from msgspec.structs import replace as replace_event
        from medre.core.events.text import StyledText, TextSpan

        original = replace_event(
            original,
            payload=StyledText("authored", (TextSpan(0, 8, "strong"),)).to_payload(),
        )
    result = await MatrixRenderer().render(
        original, replace(context(), delivery_strategy=strategy)
    )
    content = MatrixOutboundOperation.from_payload(result.payload).content
    assert content["body"] == "[reacted]"
    assert "authored" not in content.get("formatted_body", "")
    assert "m.relates_to" not in content


@pytest.mark.parametrize("thread_id", [None, "$root"])
@pytest.mark.parametrize("kind", ["message.created", "message.file"])
async def test_unbound_reply_visible_in_thread_and_media_budget(thread_id, kind):
    original = event(
        kind,
        (
            relation("reaction", key="👍"),
            relation("thread", thread_id),
            relation("reply"),
        ),
        body="answer",
    )
    result = await MatrixRenderer().render(original, context(max_text_chars=31))
    content = MatrixOutboundOperation.from_payload(result.payload).content
    assert content["body"].startswith("[replying to: unknown message]")
    assert result.truncated
    if thread_id:
        assert content["m.relates_to"]["event_id"] == thread_id
    else:
        assert "m.relates_to" not in content
    assert tuple(rel.relation_type for rel in original.relations) == (
        "reaction",
        "thread",
        "reply",
    )


@pytest.mark.parametrize("thread_id", [None, "$root"])
async def test_reaction_marker_never_reuses_suppressed_body_spans(thread_id):
    from msgspec.structs import replace as replace_event
    from medre.core.events.text import StyledText, TextSpan

    relations = (relation("thread", thread_id),) if thread_id else ()
    original = replace_event(
        event("message.reacted", relations),
        payload=StyledText("[reacted]", (TextSpan(0, 9, "strong"),)).to_payload(),
    )
    result = await MatrixRenderer().render(original, context())
    content = MatrixOutboundOperation.from_payload(result.payload).content
    assert content["body"] == "[reacted]"
    assert "<strong>" not in content["formatted_body"]
