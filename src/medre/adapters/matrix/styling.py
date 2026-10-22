"""Bounded HTML conversion; plain body remains the content authority."""

from __future__ import annotations

from collections.abc import Mapping
from html import escape
from html.parser import HTMLParser

from medre.core.events.text import (
    MAX_STYLED_TEXT_LENGTH,
    MAX_TEXT_SPANS,
    MAX_TEXT_STYLE_DEPTH,
    StyledText,
    TextSpan,
    TextStyle,
)

_TAGS: dict[str, TextStyle] = {
    "b": "strong",
    "strong": "strong",
    "i": "emphasis",
    "em": "emphasis",
    "s": "strike",
    "strike": "strike",
    "del": "strike",
    "code": "code",
    "pre": "pre",
    "blockquote": "quote",
    "a": "link",
}
_HTML = {
    "strong": "strong",
    "emphasis": "em",
    "strike": "del",
    "code": "code",
    "pre": "pre",
    "quote": "blockquote",
    "link": "a",
}
_BLOCKS = {"p", "div", "blockquote", "pre"}


class _PresentationParser(HTMLParser):
    def __init__(self, *, reply: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.reply = reply
        self.parts: list[str] = []
        self.length = 0
        self.stack: list[tuple[str, int, str | None]] = []
        self.spans: list[TextSpan] = []
        self.skipping = False
        self.nodes = 0

    def _append(self, data: str) -> None:
        if self.skipping:
            return
        self.length += len(data)
        if self.length > MAX_STYLED_TEXT_LENGTH + 1:
            raise ValueError("HTML text exceeds presentation budget")
        self.parts.append(data)

    def _line(self) -> None:
        if self.parts and not self.parts[-1].endswith("\n"):
            self._append("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.nodes += 1
        if self.nodes > MAX_TEXT_SPANS * 4 or len(self.stack) >= MAX_TEXT_STYLE_DEPTH:
            raise ValueError("HTML exceeds presentation budget")
        if tag == "br":
            self._append("\n")
            return
        if tag not in _TAGS and tag not in {"p", "div", "span", "font", "mx-reply"}:
            raise ValueError("Unsupported HTML element")
        if tag == "mx-reply":
            if not self.reply or self.stack or self.length:
                raise ValueError("Unexpected HTML reply fallback")
            self.skipping = True
        if tag in _BLOCKS:
            self._line()
        hrefs = [value for key, value in attrs if key == "href"]
        if tag == "a" and len(hrefs) != 1:
            raise ValueError("Ambiguous HTML link")
        self.stack.append((tag, self.length, hrefs[0] if hrefs else None))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag != "br":
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1][0] != tag:
            raise ValueError("Unbalanced HTML elements")
        _, start, href = self.stack.pop()
        if tag == "mx-reply":
            self.skipping = False
            return
        if self.skipping:
            return
        if tag in _TAGS and self.length > start:
            # Matrix commonly wraps preformatted content in both pre and code.
            # The canonical pre span is already opaque.
            if not (tag == "code" and any(t == "pre" for t, _, _ in self.stack)):
                self.spans.append(
                    TextSpan(
                        start, self.length, _TAGS[tag], href if tag == "a" else None
                    )
                )
                if len(self.spans) > MAX_TEXT_SPANS:
                    raise ValueError("Too many HTML spans")
        if tag in _BLOCKS:
            self._line()

    def handle_data(self, data: str) -> None:
        self._append(data)

    def handle_comment(self, data: str) -> None:
        raise ValueError("Unsupported HTML comment")

    def handle_pi(self, data: str) -> None:
        raise ValueError("Unsupported HTML processing instruction")

    def handle_decl(self, decl: str) -> None:
        raise ValueError("Unsupported HTML declaration")

    def unknown_decl(self, data: str) -> None:
        raise ValueError("Unsupported HTML declaration")


def from_html(
    body: str, content: Mapping[str, object], *, reply: bool = False
) -> StyledText:
    """Accept supported HTML only when its visible text equals the plain body."""
    plain = StyledText(body)
    markup = content.get("formatted_body")
    if (
        content.get("format") != "org.matrix.custom.html"
        or not isinstance(markup, str)
        or len(markup) > MAX_STYLED_TEXT_LENGTH * 4
        or len(body) > MAX_STYLED_TEXT_LENGTH
    ):
        return plain
    parser = _PresentationParser(reply=reply)
    try:
        parser.feed(markup)
        parser.close()
        if parser.stack:
            return plain
        visible = "".join(parser.parts)
        if visible != body and visible == body + "\n":
            visible = body  # A final block boundary is not message content.
        if visible != body:
            return plain
        spans = tuple(
            TextSpan(s.start, min(s.end, len(body)), s.style, s.href)
            for s in parser.spans
        )
        return StyledText(body, spans)
    except (ValueError, AssertionError):
        return plain


def to_html(value: StyledText, *, prefix: str = "") -> str:
    """Escape all content and attributes; prefixes stay outside source styling."""
    result = [escape(prefix, quote=False).replace("\n", "<br/>")]
    stack: list[TextSpan] = []
    position = 0
    for span in value.spans:
        while stack and stack[-1].end <= span.start:
            parent = stack.pop()
            result.append(
                escape(value.body[position : parent.end], quote=False).replace(
                    "\n", "<br/>"
                )
            )
            result.append(f"</{_HTML[parent.style]}>")
            position = parent.end
        result.append(
            escape(value.body[position : span.start], quote=False).replace(
                "\n", "<br/>"
            )
        )
        tag = _HTML[span.style]
        attr = (
            f' href="{escape(span.href, quote=True)}"' if span.href is not None else ""
        )
        result.append(f"<{tag}{attr}>")
        stack.append(span)
        position = span.start
    while stack:
        parent = stack.pop()
        result.append(
            escape(value.body[position : parent.end], quote=False).replace(
                "\n", "<br/>"
            )
        )
        result.append(f"</{_HTML[parent.style]}>")
        position = parent.end
    result.append(escape(value.body[position:], quote=False).replace("\n", "<br/>"))
    return "".join(result)


def render_payload(payload: Mapping[str, object], rendered_body: str) -> str | None:
    """Render unchanged source content with an independently formatted prefix.

    Relation degradation, truncation, and conflicting legacy text aliases must
    not cause stale offsets to decorate different content.
    """
    value = StyledText.from_payload(payload)
    if not value.spans or not value.body or not rendered_body.endswith(value.body):
        return None
    offset = len(rendered_body) - len(value.body)
    try:
        prefixed = StyledText(
            rendered_body,
            tuple(
                TextSpan(span.start + offset, span.end + offset, span.style, span.href)
                for span in value.spans
            ),
        )
    except ValueError:
        return None
    return to_html(prefixed)


def source_text(payload: Mapping[str, object]) -> str:
    """Bind optional formatting to body; retain the legacy plain alias contract."""
    if "formatting" in payload:
        return StyledText.from_payload(payload).body
    return str(payload.get("text", payload.get("body", "")))
