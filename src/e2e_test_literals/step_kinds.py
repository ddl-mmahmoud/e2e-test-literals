"""What *kind* of string a step argument holds, and how a test literal of that kind is matched.

Two separate layers (see scratch/STEP-KIND-MATCHING-PLAN.md):

1. `classify_argument` / `TABLE_HEADER` -- at index time, each recorded literal gets a
   `string_kind`, read from the registered step pattern the argument came from. The kind
   is nearly always the noun directly before the quoted field (`the button "{name}"`,
   `the "{nth:nth}" link "{name}"`), occasionally the one right after it
   (`the "{action}" link for ...`). The arg *name* is rarely informative (`{name}` is
   everywhere), except for the unmistakably opaque ones (`username`, `token`, `*_id`, ...).
2. `POLICIES` -- at match time, `string_kind -> MatchPolicy`. Changing a policy needs no
   re-index, since only the kind is stored. Kinds without an entry (including `unknown`)
   get `DEFAULT_POLICY`, the lax algorithm, so a step nobody has classified is never
   silently dropped from matching.

Ambiguity deliberately leans lax: the point of the per-kind policy is to cut the match set
to a reasonable size, not to be perfect, so a kind is only made strict when button/link-style
labels (matched in their entirety by tests) are clearly what's being recorded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

BUTTON = "button"
LINK = "link"
TAB = "tab"
MENU = "menu"
MENU_ITEM = "menu_item"
CHECKBOX = "checkbox"
RADIO_BUTTON = "radio_button"
DROPDOWN = "dropdown"
OPTION = "option"
INPUT = "input"
TABLE_HEADER = "table_header"
IMAGE_ALT = "image_alt"
ELEMENT = "element"
TEXT = "text"
DATA = "data"
OPAQUE = "opaque"
UNKNOWN = "unknown"

EXACT = "exact"
FUZZY = "substring_and_partial_ratio"
NEVER = "never"


@dataclass(frozen=True)
class MatchPolicy:
    """`algorithm` is `EXACT` (equality after whitespace normalization; a match differing
    only by case is reported at a lower confidence, see matching.py), `FUZZY`
    (case-insensitive containment, graded by rapidfuzz's `partial_ratio`), or `NEVER`.
    `confidence` fixes the reported confidence of a successful case-sensitive `EXACT` match
    (default 1.0)."""

    algorithm: str
    confidence: float = 1.0


_STRICT = MatchPolicy(EXACT)

DEFAULT_POLICY = MatchPolicy(FUZZY)

POLICIES: dict[str, MatchPolicy] = {
    BUTTON: _STRICT,
    LINK: _STRICT,
    TAB: _STRICT,
    MENU: _STRICT,
    MENU_ITEM: _STRICT,
    CHECKBOX: _STRICT,
    RADIO_BUTTON: _STRICT,
    DROPDOWN: _STRICT,
    OPTION: _STRICT,
    INPUT: _STRICT,
    TABLE_HEADER: _STRICT,
    IMAGE_ALT: _STRICT,
    ELEMENT: DEFAULT_POLICY,
    TEXT: DEFAULT_POLICY,
    # Names of things a test creates or opens (projects, workspaces, ...): not product text,
    # but a seeded one could coincide with a product string, so only a low-confidence exact
    # match -- below the default `min_literal_match_confidence`, i.e. opt-in.
    DATA: MatchPolicy(EXACT, confidence=0.3),
    OPAQUE: MatchPolicy(NEVER),
    UNKNOWN: DEFAULT_POLICY,
}


def policy_for(string_kind: str) -> MatchPolicy:
    return POLICIES.get(string_kind, DEFAULT_POLICY)


# Nouns that name a UI control, matched against the words right before / after a field.
_CONTROL_NOUN_KINDS: dict[str, str] = {
    "button": BUTTON,
    "link": LINK,
    "tab": TAB,
    "menu": MENU,
    "menu item": MENU_ITEM,
    "checkbox": CHECKBOX,
    "radio button": RADIO_BUTTON,
    "radio": RADIO_BUTTON,
    "dropdown": DROPDOWN,
    "dropdown option": OPTION,
    "option": OPTION,
    "input": INPUT,
    "table header": TABLE_HEADER,
    "column": TABLE_HEADER,
    "image with the alt text": IMAGE_ALT,
    "element": ELEMENT,
    "text": TEXT,
}

# Nouns for things a test creates/opens by name -- only recognized right *before* a field.
_DATA_NOUNS = frozenset(
    {
        "project",
        "workspace",
        "volume",
        "dataset",
        "datasource",
        "repo",
        "repository",
        "branch",
        "user",
        "endpoint",
        "data plane",
        "compute cluster",
        "cluster",
        "namespace",
        "ticket",
        "revision",
        "async prediction",
        "model",
        "environment",
        "policy",
        "filesystem",
        "image",
    }
)

# A parse-field name containing one of these `_`-separated words never holds product text.
_OPAQUE_NAME_WORDS = frozenset(
    {
        "username",
        "password",
        "token",
        "id",
        "ids",
        "path",
        "filepath",
        "filename",
        "url",
        "command",
        "address",
        "email",
        "host",
        "arn",
        "uri",
        "sha",
        "secret",
        "seconds",
        "timeout",
        "count",
        "number",
        "percentage",
        "repeat",
        "level",
    }
)
_OPAQUE_NAMES = frozenset({"user_name", "time_out", "file_name", "exit_code"})

# "... via domino API", "... via Jira API", "... via the keycloak API"
_API_STEP = re.compile(r"\bvia (?:the )?(?:[\w-]+ )?API\b")

# Escape hatch for a (registered pattern, parse-field name) the noun rules misread.
OVERRIDES: dict[tuple[str, str], str] = {}

_CONTROL_NOUNS_LONGEST_FIRST = sorted(_CONTROL_NOUN_KINDS, key=len, reverse=True)
_DATA_NOUNS_LONGEST_FIRST = sorted(_DATA_NOUNS, key=len, reverse=True)


def _ends_with_noun(words: str, nouns: list[str]) -> str | None:
    return next((noun for noun in nouns if words == noun or words.endswith(f" {noun}")), None)


def _starts_with_noun(words: str, nouns: list[str]) -> str | None:
    return next((noun for noun in nouns if words == noun or words.startswith(f"{noun} ")), None)


def classify_argument(pattern: str, arg_name: str) -> str:
    """The `string_kind` of the value captured by parse-field `arg_name` of registered step `pattern`."""
    override = OVERRIDES.get((pattern, arg_name))
    if override is not None:
        return override

    if arg_name in _OPAQUE_NAMES or _OPAQUE_NAME_WORDS.intersection(arg_name.split("_")):
        return OPAQUE

    field = re.search(r"\{" + re.escape(arg_name) + r"(?::[^}]*)?\}", pattern)
    if field is None:
        return UNKNOWN
    before = pattern[: field.start()].rstrip('" ')
    after = pattern[field.end() :].lstrip('" ')

    noun = _ends_with_noun(before, _CONTROL_NOUNS_LONGEST_FIRST)
    if noun is not None:
        return _CONTROL_NOUN_KINDS[noun]
    noun = _starts_with_noun(after, _CONTROL_NOUNS_LONGEST_FIRST)
    if noun is not None:
        return _CONTROL_NOUN_KINDS[noun]

    if after.startswith("seconds"):
        return OPAQUE
    # A value typed into a field is test data, not something the product renders.
    if after.startswith("into ") or _ends_with_noun(before, _DATA_NOUNS_LONGEST_FIRST) is not None:
        return DATA
    # Whatever an API-driven step takes that isn't plainly something else is test data too.
    if _API_STEP.search(pattern):
        return DATA
    return UNKNOWN
