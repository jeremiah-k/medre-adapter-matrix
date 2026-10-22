"""Missing native parents retain readable context and reply precedence."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from medre.adapters.matrix.renderer import MatrixRenderer
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    EventRelation,
    NativeRef,
    NativeMetadata,
)
from medre.core.rendering.renderer import RenderingContext


@pytest.mark.parametrize("scope", ["missing", "adapter", "room", "empty"])
@pytest.mark.parametrize("reaction_first", [False, True])
async def test_unbound_reply_preserves_answer_and_context_without_native_relation(
    scope, reaction_first
):
    ref = (
        None
        if scope == "missing"
        else NativeRef(
            "other" if scope == "adapter" else "matrix",
            "!other:example.org" if scope == "room" else "!room:example.org",
            "" if scope == "empty" else "$parent",
        )
    )
    reply = EventRelation("reply", "parent", ref, None, "parent café 🦉")
    reaction = EventRelation("reaction", "different", None, "👍", "different")
    relations = (reaction, reply) if reaction_first else (reply,)
    event = CanonicalEvent(
        event_id="answer",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="source",
        source_transport_id="alice",
        source_channel_id="room@example.org",
        parent_event_id=None,
        lineage=(),
        relations=relations,
        payload={"body": "answer café 🦉"},
        metadata=EventMetadata(),
    )
    result = await MatrixRenderer().render(
        event, RenderingContext("direct", "matrix", "!room:example.org", "matrix")
    )
    content = result.payload["_matrix_operation"]["content"]
    assert content["msgtype"] == "m.text"
    assert "m.relates_to" not in content
    assert "parent café 🦉" in content["body"] and "answer café 🦉" in content["body"]
    assert "[replying to:" in content["body"]
    assert result.fallback_applied == "relation_reply"
    assert event.relations == relations and event.payload["body"] == "answer café 🦉"


@pytest.mark.parametrize("strategy", ["direct", "fallback_text"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    "relation_id, wire_id, expected",
    [(42, "55", "42"), (None, "55", "55"), ("", 0, "0"), (None, None, None)],
)
async def test_unbound_reply_keeps_opt_in_mmrelay_identity(
    strategy, enabled, relation_id, wire_id, expected
):
    reply = EventRelation(
        "reply",
        "canonical-parent",
        NativeRef("radio", "0", "unrelated-packet"),
        None,
        "parent café 🦉",
        metadata={"meshtastic_reply_id": relation_id},
    )
    event = CanonicalEvent(
        event_id="answer",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="radio",
        source_transport_id="node",
        source_channel_id="0",
        parent_event_id=None,
        lineage=(),
        relations=(reply,),
        payload={"body": "answer café 🦉"},
        metadata=EventMetadata(
            native=NativeMetadata(
                data={"interop": {"mmrelay": {"meshtastic_replyId": wire_id}}}
            )
        ),
    )
    renderer = MatrixRenderer(
        source_configs={"radio": SimpleNamespace(mmrelay_compatibility=enabled)}
    )
    result = await renderer.render(
        event, RenderingContext(strategy, "matrix", "!room:example.org", "matrix")
    )
    content = result.payload["_matrix_operation"]["content"]
    assert "m.relates_to" not in content
    assert content["body"] == "[replying to: parent café 🦉] answer café 🦉"
    if enabled and expected is not None:
        assert content["meshtastic_replyId"] == expected
    else:
        assert "meshtastic_replyId" not in content
    if enabled:
        assert content["meshtastic_text"] == "answer café 🦉"
    assert event.relations == (reply,) and event.payload["body"] == "answer café 🦉"
