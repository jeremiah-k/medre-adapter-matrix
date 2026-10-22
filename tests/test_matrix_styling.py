"""Semantic formatting preserves authoritative text and escapes output."""

from datetime import datetime, timezone

import pytest

from medre.adapters.matrix.codec import MatrixCodec
from medre.config.adapters.matrix import MatrixConfig
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.adapters.matrix.styling import from_html, render_payload, to_html
from medre.core.events import CanonicalEvent, EventMetadata, EventRelation, NativeRef
from medre.core.events.text import StyledText, TextSpan
from medre.core.rendering.renderer import RenderingContext


def content(markup):
    return {"format": "org.matrix.custom.html", "formatted_body": markup}


@pytest.mark.parametrize(
    "tag,style",
    [
        ("b", "strong"),
        ("em", "emphasis"),
        ("del", "strike"),
        ("code", "code"),
        ("pre", "pre"),
        ("blockquote", "quote"),
    ],
)
def test_supported_styles_roundtrip(tag, style):
    value = from_html("café 🦉", content(f"<{tag}>café 🦉</{tag}>"))
    assert value == StyledText("café 🦉", (TextSpan(0, 6, style),))
    assert from_html(value.body, content(to_html(value))) == value


def test_blocks_entities_links_and_opaque_pre():
    value = from_html(
        "one & two\ncode",
        content(
            '<p>one &amp; <a href="https://例え.test/文?a=1&amp;b=2">two</a></p><pre><code>code</code></pre>'
        ),
    )
    assert value.spans == (
        TextSpan(6, 9, "link", "https://例え.test/文?a=1&b=2"),
        TextSpan(10, 14, "pre"),
    )
    assert from_html(value.body, content(to_html(value))) == value


@pytest.mark.parametrize(
    "markup",
    [
        "<b>different</b>",
        "<script>safe</script>",
        "<img src=x>",
        "<b><em>safe</b></em>",
        "<b>safe",
        '<a href="javascript:alert(1)">safe</a>',
        '<a href="https://example.org" href="https://evil.example">safe</a>',
        "<mx-reply>safe</mx-reply>",
        "<!DOCTYPE html><b>safe</b>",
        "<b>safe</b><!--hidden-->",
        "<b>safe</b><?ignored processing?>",
        "<span>" * 20 + "safe" + "</span>" * 20,
    ],
)
def test_unsupported_malformed_and_unsafe_html_keeps_plain_body(markup):
    assert from_html("safe", content(markup)) == StyledText("safe")


def test_reply_fallback_is_removed_only_with_relation_context():
    markup = "<mx-reply><blockquote>parent</blockquote></mx-reply><b>answer</b>"
    assert not from_html("answer", content(markup)).spans
    assert from_html("answer", content(markup), reply=True).spans == (
        TextSpan(0, 6, "strong"),
    )


def test_html_input_and_output_have_independent_content_and_attribute_escaping():
    value = StyledText(
        '<&" 🦉', (TextSpan(0, 5, "link", "https://example.org/?a=1&b=2"),)
    )
    markup = to_html(value, prefix="<prefix> ")
    assert "&lt;prefix&gt;" in markup and "&lt;&amp;" in markup
    assert 'href="https://example.org/?a=1&amp;b=2"' in markup
    assert "<prefix>" not in markup
    assert render_payload(value.to_payload(), "changed") is None
    assert render_payload(value.to_payload(), value.body[:-1]) is None


def event(payload, relations=()):
    return CanonicalEvent(
        event_id="styled",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="source",
        source_transport_id="alice",
        source_channel_id="room",
        parent_event_id=None,
        lineage=(),
        relations=relations,
        payload=payload,
        metadata=EventMetadata(),
    )


@pytest.mark.parametrize(
    "relations",
    [
        (),
        (
            EventRelation(
                "reply",
                "parent",
                NativeRef("matrix", "!room:test", "$parent"),
                None,
                "parent",
            ),
        ),
        (
            EventRelation(
                "thread", "root", NativeRef("matrix", "!room:test", "$root"), None, None
            ),
        ),
    ],
)
async def test_renderer_styles_content_without_styling_or_replacing_prefix(relations):
    payload = StyledText("hello 🦉", (TextSpan(0, 5, "strong"),)).to_payload()
    payload["text"] = "incorrect legacy alias"
    source = event(payload, relations)
    result = await MatrixRenderer().render(
        source, RenderingContext("direct", "matrix", "!room:test", "matrix")
    )
    rendered = result.payload["_matrix_operation"]["content"]
    assert rendered["body"].endswith("hello 🦉")
    assert "<strong>hello</strong>" in rendered["formatted_body"]
    assert "incorrect legacy alias" not in rendered["body"]
    assert source.payload["body"] == "hello 🦉"


async def test_degraded_truncated_reply_never_reuses_source_span_offsets():
    source = event(
        StyledText("answer", (TextSpan(0, 6, "strong"),)).to_payload(),
        (EventRelation("reply", "missing", None, None, "parent"),),
    )
    result = await MatrixRenderer().render(
        source,
        RenderingContext(
            "fallback_text", "matrix", "!room:test", "matrix", max_text_chars=12
        ),
    )
    rendered = result.payload["_matrix_operation"]["content"]
    assert "<strong>" not in rendered["formatted_body"]
    assert len(rendered["body"]) <= 12


def test_codec_records_formatting_from_effective_edit_content():
    codec = MatrixCodec(
        "matrix",
        MatrixConfig(
            adapter_id="matrix",
            homeserver="https://example.org",
            user_id="@relay:example.org",
            access_token="token",
        ),
    )
    original = {
        "body": "* previous",
        "msgtype": "m.text",
        "m.new_content": {
            "body": "hello",
            "msgtype": "m.text",
            **content("<b>hello</b>"),
        },
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
    }
    native = {
        "event_id": "$edit",
        "sender": "@alice:example.org",
        "type": "m.room.message",
        "content": original,
    }
    result = codec.decode({"source": native}, room_id="!room:test")
    assert StyledText.from_payload(result.payload) == StyledText(
        "hello", (TextSpan(0, 5, "strong"),)
    )


def test_inline_prefix_cannot_turn_complete_line_blocks_into_partial_blocks():
    value = StyledText("quote", (TextSpan(0, 5, "quote"),))
    assert render_payload(value.to_payload(), "[alice] quote") is None
    assert "<blockquote>quote</blockquote>" in render_payload(
        value.to_payload(), "[alice]\nquote"
    )


async def test_relation_fallback_uses_styled_body_not_stale_legacy_text_alias():
    payload = StyledText("correct 🦉", (TextSpan(0, 7, "strong"),)).to_payload()
    payload["text"] = "stale legacy alias"
    source = event(payload, (EventRelation("reply", "missing", None, None, "parent"),))
    result = await MatrixRenderer().render(
        source, RenderingContext("fallback_text", "matrix", "!room:test", "matrix")
    )
    content = result.payload["_matrix_operation"]["content"]
    assert "correct 🦉" in content["body"]
    assert "stale legacy alias" not in content["body"]
    assert "<strong>" not in content["formatted_body"]
