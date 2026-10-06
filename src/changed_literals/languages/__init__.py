"""Extension/suffix -> LanguageAdapter registry.

To add a language: write one module with `literal_texts_of`/`classify`
(and an optional `preprocess`), then add one entry here (plus, if it needs
its own git pathspec, one line in PATHSPECS). Nothing else in the pipeline
changes.
"""

from __future__ import annotations

from collections.abc import Callable

from . import ecmascript, twirl
from .base import LanguageAdapter

_TS = LanguageAdapter("ts", "typescript", ecmascript.literal_texts_of, ecmascript.classify)
_TSX = LanguageAdapter("tsx", "tsx", ecmascript.literal_texts_of, ecmascript.classify)
_JS = LanguageAdapter("js", "javascript", ecmascript.literal_texts_of, ecmascript.classify)
_JSX = LanguageAdapter("jsx", "javascript", ecmascript.literal_texts_of, ecmascript.classify)
_TWIRL = LanguageAdapter(
    "twirl", "html", twirl.literal_texts_of, twirl.classify, preprocess=twirl.mask_scala
)

# Order matters: compound suffixes (`.scala.html`) must be checked before a
# bare extension match could shadow them.
_MATCHERS: list[tuple[str, Callable[[str], bool], LanguageAdapter]] = [
    ("twirl", lambda p: p.endswith(".scala.html"), _TWIRL),
    ("ts", lambda p: p.endswith(".ts") and not p.endswith(".d.ts"), _TS),
    ("tsx", lambda p: p.endswith(".tsx"), _TSX),
    ("js", lambda p: p.endswith(".js"), _JS),
    ("jsx", lambda p: p.endswith(".jsx"), _JSX),
]

PATHSPECS: dict[str, str] = {
    "twirl": "*.scala.html",
    "ts": "*.ts",
    "tsx": "*.tsx",
    "js": "*.js",
    "jsx": "*.jsx",
}


def resolve_adapter(path: str, allowed_names: set[str] | None = None) -> LanguageAdapter | None:
    for name, matches, adapter in _MATCHERS:
        if matches(path) and (allowed_names is None or name in allowed_names):
            return adapter
    return None


def pathspecs_for(allowed_names: set[str] | None = None) -> list[str]:
    if allowed_names is None:
        return list(PATHSPECS.values())
    return [spec for name, spec in PATHSPECS.items() if name in allowed_names]
