"""A thread context and an explicit reply have independent native semantics."""

import pytest

from medre.adapters.matrix.codec import MatrixCodec
from medre.config.adapters.matrix import MatrixConfig


def decode(body, relation):
    return MatrixCodec(
        "matrix", MatrixConfig(adapter_id="matrix", homeserver="", user_id="")
    ).decode(
        {
            "sender": "@alice:example.org",
            "event_id": "$incoming",
            "source": {
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.text",
                    "body": body,
                    "m.relates_to": relation,
                },
            },
        },
        room_id="!room:example.org",
    )


def test_thread_without_reply_marker_preserves_authored_quote():
    event = decode("> authored quote", {"rel_type": "m.thread", "event_id": "$root"})
    assert event.payload["body"] == "> authored quote"
    assert [r.relation_type for r in event.relations] == ["thread"]


@pytest.mark.parametrize("fallback", [False, True, None])
def test_thread_explicit_reply_is_distinct_from_compatibility_fallback(fallback):
    relation = {
        "rel_type": "m.thread",
        "event_id": "$root",
        "m.in_reply_to": {"event_id": "$parent"},
    }
    if fallback is not None:
        relation["is_falling_back"] = fallback
    event = decode("> <@bob:example.org> preview\n\n> authored quote\nanswer", relation)
    assert event.payload["body"] == "> authored quote\nanswer"
    replies = [r for r in event.relations if r.relation_type == "reply"]
    assert len(replies) == (0 if fallback is True else 1)
    if replies:
        assert replies[0].target_native_ref.native_message_id == "$parent"
