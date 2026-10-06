"""Generic line-overlap AST walk, parameterized by a LanguageAdapter.

Port of collectFindings() from frontend/extract-ui-diff.ts in the
frontend-web-ui-service repo, generalized so it works for any tree-sitter
grammar rather than just ts-morph's TypeScript-specific node model.
"""

from __future__ import annotations

import tree_sitter_language_pack as tslp
from tree_sitter import Node

from .finding import Change, Finding
from .languages.base import LanguageAdapter

DEFAULT_MIN_CONFIDENCE = 0.3


def collect_findings(
    source: bytes,
    adapter: LanguageAdapter,
    target_lines: set[int],
    change: Change,
    report_path: str,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> list[Finding]:
    if adapter.preprocess is not None:
        source = adapter.preprocess(source)

    parser = tslp.get_parser(adapter.parser_name)
    tree = parser.parse(source)
    findings: list[Finding] = []

    def visit(node: Node) -> None:
        texts = adapter.literal_texts_of(node)
        if texts is not None:
            start_line = node.start_point[0] + 1
            end_line = node.end_point[0] + 1
            # Extension point: this only checks direct line overlap with
            # the diff, same as the original TS tool -- it doesn't trace a
            # literal defined outside the diff back to a changed usage site.
            touches_diff = any(line in target_lines for line in range(start_line, end_line + 1))
            if touches_diff:
                for raw_text in texts:
                    result = adapter.classify(node, raw_text)
                    if result is not None and result.confidence >= min_confidence:
                        findings.append(
                            Finding(
                                file=report_path,
                                line=start_line,
                                column=node.start_point[1] + 1,
                                text=raw_text.strip()[:200],
                                context=result.context,
                                confidence=round(result.confidence, 2),
                                change=change,
                            )
                        )
            # `texts` (even if empty) means this node is a literal-shaped
            # leaf for this adapter -- don't descend further.
            return

        for child in node.children:
            visit(child)

    visit(tree.root_node)
    return findings
