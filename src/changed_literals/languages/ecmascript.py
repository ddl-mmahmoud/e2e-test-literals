"""Shared TS/TSX/JS/JSX adapter.

Direct port of classify()/literalTextOf() from
frontend/extract-ui-shared.ts in the frontend-web-ui-service repo. The
tree-sitter grammars for typescript/tsx/javascript share the same node
shapes for everything this cares about, so one adapter covers all four
extensions -- only the grammar name passed to tree_sitter_language_pack
differs (see languages/__init__.py).

Grammar shapes this relies on (verified against tree-sitter-language-pack
1.20.0's typescript/tsx/javascript grammars):
  - string literal: `string` node, text lives in `string_fragment` children.
  - no-substitution template literal: `template_string` node with no
    `template_substitution` child (same string_fragment extraction).
  - JSX text: `jsx_text` node, use its own .text.
  - JSX attribute: `jsx_attribute` node; named_children are
    [name (property_identifier), value?] -- no field names for either.
  - call: `call_expression` has fields "function" (callee) and "arguments"
    (an `arguments` node whose children are the actual argument nodes).
  - object property: `pair` node has fields "key" and "value".
  - variable init: `variable_declarator` node has fields "name" and "value".
"""

from __future__ import annotations

import re

from tree_sitter import Node

from ..scoring import linguistic_score
from .base import ClassifyResult

ALLOW_ATTRS = {
    "label",
    "title",
    "placeholder",
    "alt",
    "aria-label",
    "helperText",
    "description",
    "tooltip",
    "errorText",
    "subtitle",
    "heading",
    "buttonText",
    "message",
    "confirmText",
    "cancelText",
}

DENY_ATTRS = {
    "className",
    "class",
    "style",
    "id",
    "key",
    "href",
    "src",
    "name",
    "type",
    "variant",
    "size",
    "role",
    "testId",
    "data-testid",
    "htmlFor",
    "rel",
    "target",
    "for",
}

I18N_CALLS = {"t", "formatMessage", "translate"}
DENY_CALLS = {
    "console.log",
    "console.error",
    "console.warn",
    "console.info",
    "require",
    "describe",
    "it",
    "test",
    "expect",
}

ALLOW_KEYS = {
    "label",
    "title",
    "header",
    "caption",
    "message",
    "description",
    "text",
    "placeholder",
    "tooltip",
    "subtitle",
    "heading",
}

DENY_NAME_PATTERN = re.compile(
    r"^(id|key|type|variant|size|role|slug|className|testId|dataTestId|href|src|url|path|name)$",
    re.IGNORECASE,
)


def _decode(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _string_text(node: Node) -> str:
    return "".join(_decode(c) for c in node.children if c.type == "string_fragment")


def literal_texts_of(node: Node) -> list[str] | None:
    if node.type == "string":
        return [_string_text(node)]
    if node.type == "template_string":
        # Extension point: TemplateExpression (interpolated template
        # literals) isn't handled -- only the no-substitution case, to
        # match extract-ui-shared.ts's literalTextOf(). Returning None (not
        # []) here lets the walker keep descending to find nested literals
        # inside the substitution expression.
        if any(c.type == "template_substitution" for c in node.children):
            return None
        return [_string_text(node)]
    if node.type == "jsx_text":
        return [_decode(node)]
    return None


def _jsx_attr_name(attr_node: Node) -> str | None:
    for child in attr_node.named_children:
        if child.type in ("property_identifier", "identifier"):
            return _decode(child)
    return None


def _call_callee_text(call_node: Node) -> str | None:
    callee = call_node.child_by_field_name("function")
    return _decode(callee) if callee is not None else None


def classify(node: Node, raw_text: str) -> ClassifyResult | None:
    trimmed = raw_text.strip()
    if not trimmed:
        return None

    if node.type == "jsx_text":
        if len(trimmed) < 2:
            return None
        return ClassifyResult("jsx-text", 0.95)

    parent = node.parent
    if parent is None:
        return ClassifyResult("unknown", linguistic_score(trimmed))

    if parent.type == "jsx_attribute":
        attr_name = _jsx_attr_name(parent) or "?"
        if attr_name in ALLOW_ATTRS:
            return ClassifyResult(f"jsx-attr:{attr_name}", 0.85)
        if attr_name in DENY_ATTRS:
            return ClassifyResult(f"jsx-attr:{attr_name}", 0.03)
        return ClassifyResult(f"jsx-attr:{attr_name}", linguistic_score(trimmed))

    # A call argument's direct parent is the grammar's `arguments` node,
    # one level below the call_expression itself.
    if parent.type == "arguments" and parent.parent is not None and parent.parent.type == "call_expression":
        call = parent.parent
        callee = _call_callee_text(call) or "?"
        short = callee.split(".")[-1]
        if callee in I18N_CALLS or short in I18N_CALLS:
            return ClassifyResult(f"call:{callee}", 0.9)
        if callee in DENY_CALLS or short in DENY_CALLS:
            return ClassifyResult(f"call:{callee}", 0.02)
        return ClassifyResult(f"call:{callee}", linguistic_score(trimmed))

    if parent.type == "pair":
        key_node = parent.child_by_field_name("key")
        key_name = _decode(key_node).strip("'\"") if key_node is not None else "?"
        if key_name in ALLOW_KEYS:
            return ClassifyResult(f"prop:{key_name}", 0.75)
        return ClassifyResult(f"prop:{key_name}", linguistic_score(trimmed))

    if parent.type == "variable_declarator":
        name_node = parent.child_by_field_name("name")
        var_name = _decode(name_node) if name_node is not None else "?"
        if DENY_NAME_PATTERN.match(var_name):
            return ClassifyResult(f"var:{var_name}", 0.05)
        return ClassifyResult(f"var:{var_name}", linguistic_score(trimmed))

    return ClassifyResult(parent.type, linguistic_score(trimmed))
