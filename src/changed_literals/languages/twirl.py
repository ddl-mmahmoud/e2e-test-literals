"""Play/Twirl view-template adapter (`*.scala.html`).

There's no tree-sitter grammar for Twirl (confirmed: no tree-sitter-twirl
project exists), and a Twirl file is fundamentally an HTML document with
embedded Scala via `@`-prefixed expressions, not Scala with embedded
markup. So this adapter doesn't parse Twirl directly: it masks out the
`@`-Scala regions first (replacing them with spaces of the same length, so
line/column offsets -- and therefore diff line-overlap matching -- stay
correct), then parses what's left as plain HTML and walks that tree with
an HTML-flavored classify(), analogous to the JSX one in ecmascript.py.

Known limitations, accepted as a first pass (see CHANGED-LITERALS-PLAN.md
Q6): a `@x match { case A => { ... } }` block only has the `@x match {`
header masked -- the `case A => {` fragments inside aren't `@`-expressions
themselves, so they leak through as stray low-confidence text nodes.
Real prose that happens to contain an `@`-shaped substring (e.g. an email
address like "user@example.com") can also be mis-masked, since the masker
has no way to distinguish "code position" from "template body position"
without a real Twirl tokenizer. Both are accepted tradeoffs for avoiding
a from-scratch Twirl parser; tighten only if the resulting noise (or,
worse, missed strings from the email case) turns out to matter in
practice.
"""

from __future__ import annotations

import re

from tree_sitter import Node

from ..scoring import linguistic_score
from .base import ClassifyResult

ALLOW_ATTRS = {"placeholder", "aria-label", "alt", "title", "label"}
DENY_ATTRS = {"class", "id", "href", "src", "name", "type", "style", "rel", "target", "for"}

_PAIR = {"(": ")", "[": "]"}


def _skip_string(text: str, i: int) -> int:
    """i is at an opening '"'. Handles triple-quoted and \\-escaped strings."""
    n = len(text)
    if text[i : i + 3] == '"""':
        end = text.find('"""', i + 3)
        return end + 3 if end != -1 else n
    i += 1
    while i < n:
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == '"':
            return i + 1
        i += 1
    return n


def _skip_identifier(text: str, i: int) -> int:
    n = len(text)
    while i < n and (text[i].isalnum() or text[i] == "_"):
        i += 1
    return i


def _skip_balanced(text: str, i: int) -> int:
    """text[i] is '(' or '['. Returns the index just past the matching close."""
    open_ch = text[i]
    close_ch = _PAIR[open_ch]
    depth = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            i = _skip_string(text, i)
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _mask_expression_header(text: str, at: int) -> int:
    """text[at] is '@' (not '@@' or '@*'). Returns the index just past the
    part of the `@`-expression that should be masked -- the `@`, any
    identifier/call chain, and any `(...)`/`[...]` groups, but never a
    trailing `{ ... }` block body (that stays as literal template content)."""
    n = len(text)
    i = at + 1

    if i < n and text[i] == "{":
        return i  # bare `@{ expr }`: only the `@` itself is code-shaped.

    if i < n and text[i] == "(":
        while i < n and text[i] in "([":
            i = _skip_balanced(text, i)
        return i

    if i < n and (text[i].isalpha() or text[i] == "_"):
        ident_start = i
        i = _skip_identifier(text, i)
        if text[ident_start:i] == "import":
            # `@import a.b.C` always runs to end of line in Twirl.
            end_of_line = text.find("\n", i)
            return end_of_line if end_of_line != -1 else n
        while i < n:
            if text[i] in "([":
                i = _skip_balanced(text, i)
            elif text[i] == "." and i + 1 < n and (text[i + 1].isalpha() or text[i + 1] == "_"):
                i = _skip_identifier(text, i + 1)
            else:
                break
        return i

    return i  # bare '@' followed by something unexpected -- mask just it.


def mask_scala(source: bytes) -> bytes:
    text = source.decode("utf-8", "replace")
    out = list(text)
    i = 0
    n = len(text)

    def blank(start: int, end: int) -> None:
        for j in range(start, end):
            if out[j] != "\n":
                out[j] = " "

    while i < n:
        if text[i] != "@":
            i += 1
            continue
        if i + 1 < n and text[i + 1] == "@":
            i += 2  # `@@` is an escaped literal '@' -- leave it as content.
            continue
        if i + 1 < n and text[i + 1] == "*":
            end = text.find("*@", i + 2)
            end = end + 2 if end != -1 else n
            blank(i, end)
            i = end
            continue
        end = _mask_expression_header(text, i)
        blank(i, end)
        i = end

    return "".join(out).encode("utf-8")


def _decode(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


# Masking replaces a `@`-expression header with spaces, so a run of 2+
# spaces (real hand-authored HTML prose essentially never has that) or a
# stray `{`/`}` left over from a `@helper(...){ ... }`/`@if(...) { ... }`
# boundary is treated as a fragment separator, not part of the text. A
# single flat HTML `text` node can otherwise glue two unrelated sentences
# together around a masked expression (see CHANGED-LITERALS-PLAN.md).
_FRAGMENT_SPLIT_RE = re.compile(r"[{}]|  +")


def literal_texts_of(node: Node) -> list[str] | None:
    if node.type == "text":
        raw = _decode(node)
        fragments = (segment.strip() for segment in _FRAGMENT_SPLIT_RE.split(raw))
        return [f for f in fragments if f]
    if node.type == "attribute_value":
        text = _decode(node)
        return [text] if text else []
    return None


def _html_attr_name(attribute_value_node: Node) -> str | None:
    quoted = attribute_value_node.parent
    attribute = quoted.parent if quoted is not None else None
    if attribute is None:
        return None
    for child in attribute.children:
        if child.type == "attribute_name":
            return _decode(child)
    return None


def classify(node: Node, raw_text: str) -> ClassifyResult | None:
    trimmed = raw_text.strip()
    if not trimmed:
        return None

    if node.type == "text":
        if len(trimmed) < 2:
            return None
        return ClassifyResult("html-text", 0.9)

    if node.type == "attribute_value":
        attr_name = _html_attr_name(node) or "?"
        if attr_name in ALLOW_ATTRS:
            return ClassifyResult(f"html-attr:{attr_name}", 0.8)
        if attr_name in DENY_ATTRS:
            return ClassifyResult(f"html-attr:{attr_name}", 0.03)
        return ClassifyResult(f"html-attr:{attr_name}", linguistic_score(trimmed))

    return None
