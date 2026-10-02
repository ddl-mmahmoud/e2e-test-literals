"""Unit tests for service/impact.py -- changed-literals and the test revision build are
both faked out (Q7 of CHANGED-LITERALS-IMPACT-PLAN.md: assume the documented HTTP
contract and mock it, no real changed-literals instance needed), covering the
filter/substring-match orchestration logic itself."""

from __future__ import annotations

import pytest

from e2e_test_literals.db import write_sqlite
from e2e_test_literals.service import changed_literals_client as cl_client
from e2e_test_literals.service import config, impact


def _write_test_db(sha: str, *, case_id: int = 1, value: str = "Saved successfully", tags=("smoke",)) -> None:
    cases = {
        case_id: {
            "scenarios": [
                {
                    "feature_file": "tests/ui/features/example.feature",
                    "scenario_name": "Save a widget",
                    "tags": list(tags),
                }
            ],
            "literals": [
                {
                    "value": value,
                    "kind": "step_arg",
                    "dynamic": False,
                    "source_kind": "direct",
                    "source_file": "tests/ui/features/example.feature",
                    "source_line": 5,
                    "step_text": f'Then I click the button "{value}"',
                }
            ],
            "patterns": [],
        }
    }
    write_sqlite(cases, config.DB_DIR / f"{sha}.sqlite")


@pytest.fixture(autouse=True)
def _impact_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_DIR", tmp_path / "db")
    config.DB_DIR.mkdir(parents=True)


def _finding(**overrides) -> dict:
    base = {
        "file": "src/Widget.tsx",
        "line": 10,
        "column": 2,
        "text": "Saved successfully",
        "context": "jsx-text",
        "confidence": 0.95,
        "change": "removed",
    }
    base.update(overrides)
    return base


def _stub_changed_literals(monkeypatch, findings: list[dict]) -> None:
    monkeypatch.setattr(cl_client, "create_job", lambda repo, base, updated: {"job_id": "job-1"})
    monkeypatch.setattr(cl_client, "get_job_status", lambda job_id: {"status": "done"})
    monkeypatch.setattr(
        cl_client,
        "get_result_page",
        lambda job_id, *, offset=0, limit=2000: {
            "offset": offset,
            "limit": limit,
            "total": len(findings),
            "findings": findings if offset == 0 else [],
        },
    )


def test_matches_removed_literal_above_confidence(monkeypatch):
    monkeypatch.setattr(impact, "resolve_and_generate", lambda repo, ref: "deadbeef")
    _write_test_db("deadbeef")
    _stub_changed_literals(monkeypatch, [_finding()])

    result = impact.compute_impact("test-repo", "main", "product-repo", "base", "updated")

    assert result["test_sha"] == "deadbeef"
    assert result["min_removal_confidence"] == impact.DEFAULT_MIN_REMOVAL_CONFIDENCE
    assert result["total"] == 1
    finding = result["findings"][0]
    assert finding["change"] == "removed"
    assert len(finding["matched_test_literals"]) == 1
    match = finding["matched_test_literals"][0]
    assert match["value"] == "Saved successfully"
    assert match["scenario_name"] == "Save a widget"
    assert match["source_file"] == "tests/ui/features/example.feature"
    assert match["tags"] == ["smoke"]


def test_drops_findings_below_min_confidence(monkeypatch):
    monkeypatch.setattr(impact, "resolve_and_generate", lambda repo, ref: "deadbeef")
    _write_test_db("deadbeef")
    _stub_changed_literals(monkeypatch, [_finding(confidence=0.5)])

    result = impact.compute_impact("test-repo", "main", "product-repo", "base", "updated")

    assert result["findings"] == []


def test_drops_added_findings(monkeypatch):
    monkeypatch.setattr(impact, "resolve_and_generate", lambda repo, ref: "deadbeef")
    _write_test_db("deadbeef")
    _stub_changed_literals(monkeypatch, [_finding(change="added")])

    result = impact.compute_impact("test-repo", "main", "product-repo", "base", "updated")

    assert result["findings"] == []


def test_removed_finding_with_no_substring_match_has_empty_match_list(monkeypatch):
    monkeypatch.setattr(impact, "resolve_and_generate", lambda repo, ref: "deadbeef")
    _write_test_db("deadbeef")
    _stub_changed_literals(monkeypatch, [_finding(text="Something unrelated")])

    result = impact.compute_impact("test-repo", "main", "product-repo", "base", "updated")

    assert result["total"] == 1
    assert result["findings"][0]["matched_test_literals"] == []


def test_respects_custom_min_removal_confidence(monkeypatch):
    monkeypatch.setattr(impact, "resolve_and_generate", lambda repo, ref: "deadbeef")
    _write_test_db("deadbeef")
    _stub_changed_literals(monkeypatch, [_finding(confidence=0.6)])

    result = impact.compute_impact("test-repo", "main", "product-repo", "base", "updated", min_removal_confidence=0.5)

    assert result["total"] == 1


def test_match_is_case_sensitive_substring(monkeypatch):
    monkeypatch.setattr(impact, "resolve_and_generate", lambda repo, ref: "deadbeef")
    _write_test_db("deadbeef", value="Saved successfully")
    _stub_changed_literals(monkeypatch, [_finding(text="saved successfully (lowercase, different text)")])

    result = impact.compute_impact("test-repo", "main", "product-repo", "base", "updated")

    assert result["findings"][0]["matched_test_literals"] == []


def test_raises_on_remote_job_error(monkeypatch):
    monkeypatch.setattr(impact, "resolve_and_generate", lambda repo, ref: "deadbeef")
    _write_test_db("deadbeef")
    monkeypatch.setattr(cl_client, "create_job", lambda repo, base, updated: {"job_id": "job-1"})
    monkeypatch.setattr(cl_client, "get_job_status", lambda job_id: {"status": "error", "error": "boom"})

    with pytest.raises(cl_client.ChangedLiteralsError, match="boom"):
        impact.compute_impact("test-repo", "main", "product-repo", "base", "updated")


def test_fetch_all_findings_follows_pagination(monkeypatch):
    page_one = [_finding(text=f"finding-{i}") for i in range(2)]
    page_two = [_finding(text=f"finding-{i}") for i in range(2, 3)]

    def _get_result_page(job_id, *, offset=0, limit=2000):
        if offset == 0:
            return {"offset": 0, "limit": 2, "total": 3, "findings": page_one}
        return {"offset": 2, "limit": 2, "total": 3, "findings": page_two}

    monkeypatch.setattr(cl_client, "get_result_page", _get_result_page)
    monkeypatch.setattr(impact, "_RESULT_PAGE_SIZE", 2)

    all_findings = impact._fetch_all_findings("job-1")

    assert [f["text"] for f in all_findings] == ["finding-0", "finding-1", "finding-2"]
