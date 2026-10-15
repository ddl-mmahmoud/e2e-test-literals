"""The impact form page: that it serves, carries the API's own defaults, only ever requests
paths relative to its own URL (so it works under any mount prefix), and that its script
parses (under node). Its behaviour in a browser is not exercised here."""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from e2e_test_literals.service import app as app_module
from e2e_test_literals.service import impact
from e2e_test_literals.service.app import ImpactRequest
from e2e_test_literals.service.impact_form_page import render_form

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _script() -> str:
    return re.search(r"<script>(.*)</script>", render_form(), re.S).group(1)


def test_form_is_served_as_html():
    with TestClient(app_module.app) as client:
        resp = client.get("/changed-literals-impact-report")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "<form" in resp.text


def test_no_templating_placeholder_is_left_and_defaults_come_from_the_api():
    page = render_form()
    assert "__" not in re.sub(r"<script>.*</script>", "", page, flags=re.S)
    assert f'id="min_removal_confidence" min="0" max="1" step="any" value="{impact.DEFAULT_MIN_REMOVAL_CONFIDENCE}"' in page
    assert f'id="invocation_max_length" min="0" step="1" value="{impact.DEFAULT_INVOCATION_MAX_LENGTH}"' in page


def test_every_api_parameter_has_a_field():
    page = render_form()
    for name in ImpactRequest.model_fields:
        assert f'id="{name}"' in page, name


def test_literals_repo_sits_in_the_change_box_and_pr_url_is_the_default_mode():
    page = render_form()
    change_box = re.search(r"<legend>Change to analyze</legend>(.*?)</fieldset>", page, re.S).group(1)
    assert 'id="literals_repo"' in change_box
    assert 'id="pr_url"' in change_box
    assert re.search(r'value="pr_url" checked', page)
    repos_box = re.search(r"<legend>Repositories</legend>(.*?)</fieldset>", page, re.S).group(1)
    assert "literals_repo" not in repos_box


def test_page_requests_only_paths_relative_to_itself():
    script = _script()
    assert 'fetch("changed-literals-impact"' in script
    assert "`changed-literals-impact/jobs/" in script
    assert "/result?report=1" in script
    assert not re.search(r"""fetch\(["'`]/""", script)
    assert not re.search(r"""location\.assign\(["'`]/""", script)


@needs_node
def test_script_parses(tmp_path):
    path = tmp_path / "form.js"
    path.write_text(_script())
    subprocess.run(["node", "--check", str(path)], check=True, capture_output=True)
