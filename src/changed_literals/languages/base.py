"""Shared types every language adapter plugs into.

A new language is: one module implementing `literal_texts_of` and
`classify`, plus one or two entries in `languages/__init__.py`'s registry.
Nothing else in the pipeline (walker, extractor, cli) needs to change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from tree_sitter import Node


@dataclass(frozen=True)
class ClassifyResult:
    context: str
    confidence: float


@dataclass(frozen=True)
class LanguageAdapter:
    name: str
    """Short identifier used by the --languages CLI filter, e.g. "tsx", "twirl"."""

    parser_name: str
    """Grammar name passed to tree_sitter_language_pack.get_parser()."""

    literal_texts_of: Callable[[Node], list[str] | None]
    """None if `node` isn't a literal-shaped leaf at all (walker keeps
    descending into its children). A list -- even an empty one -- means
    `node` IS such a leaf (walker stops descending), yielding zero or more
    text fragments to classify independently. More than one fragment
    happens when a single AST/DOM node's text had to be split after the
    fact (e.g. Twirl's masking can glue two separate sentences either side
    of a masked expression into one HTML text node) -- all fragments from
    one node share that node's line/column in the resulting Finding, which
    is an approximation when there's more than one."""

    classify: Callable[[Node, str], ClassifyResult | None]
    """None to drop the node entirely (e.g. blank text)."""

    preprocess: Callable[[bytes], bytes] | None = None
    """Optional source-level rewrite applied before parsing (must preserve
    byte length/line layout so diff line numbers still line up)."""
