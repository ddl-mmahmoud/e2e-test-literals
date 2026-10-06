from changed_literals.languages import resolve_adapter
from changed_literals.languages.twirl import mask_scala
from changed_literals.walker import collect_findings

ALL_LINES = set(range(1, 1000))


def _findings(source: str):
    adapter = resolve_adapter("views/example.scala.html")
    assert adapter is not None
    return collect_findings(source.encode(), adapter, ALL_LINES, "added", "views/example.scala.html", min_confidence=0.0)


def _by_text(findings, text):
    matches = [f for f in findings if f.text == text]
    assert matches, f"no finding with text={text!r} among {[f.text for f in findings]}"
    return matches[0]


def test_mask_scala_preserves_line_count_and_length():
    src = b'@import a.b.C\n@(x: String)\n<p>Hello</p>\n'
    masked = mask_scala(src)
    assert len(masked) == len(src)
    assert masked.count(b"\n") == src.count(b"\n")


def test_plain_html_text_is_found():
    src = '<div><p>Exports let you share environment variables and files.</p></div>'
    f = _by_text(_findings(src), "Exports let you share environment variables and files.")
    assert f.context == "html-text"
    assert f.confidence >= 0.8


def test_allow_listed_attribute_is_high_confidence():
    src = '<input placeholder="Type here" />'
    f = _by_text(_findings(src), "Type here")
    assert f.context == "html-attr:placeholder"
    assert f.confidence >= 0.7


def test_deny_listed_attribute_is_low_confidence():
    src = '<div class="panel-heading-row"></div>'
    findings = _findings(src)
    matches = [f for f in findings if f.text == "panel-heading-row"]
    assert matches
    assert matches[0].confidence < 0.1


def test_import_and_param_directives_produce_no_real_findings():
    src = (
        "@import domino.common.{AnalyticProject, DataSet}\n"
        "@(project: Project)(implicit request: RequestHeader)\n"
        "<div><p>Real visible sentence here.</p></div>\n"
    )
    findings = [f for f in _findings(src) if f.confidence >= 0.3]
    assert any(f.text == "Real visible sentence here." for f in findings)
    assert not any("AnalyticProject" in f.text or "DataSet" in f.text for f in findings)


def test_helper_block_arg_text_is_found_like_plain_text():
    src = '<span>@helpLinkTemplate("url", true, true, "id"){Learn more about Environment variables}</span>'
    f = _by_text(_findings(src), "Learn more about Environment variables")
    assert f.context == "html-text"


def test_helper_call_mid_sentence_does_not_glue_the_two_sentences_together():
    # Regression: a masked helper call inline in a longer sentence used to
    # produce one merged "text" node containing both sentences plus a run
    # of blank padding and stray braces -- see CHANGED-LITERALS-PLAN.md.
    src = (
        '<p>You can control which results are shown. '
        '@helpLinkTemplate("url", true, true, "id"){Learn more}.</p>'
    )
    findings = _findings(src)
    texts = {f.text for f in findings}
    assert "You can control which results are shown." in texts
    assert "Learn more" in texts
    assert not any("shown." in t and "Learn more" in t for t in texts)


def test_match_case_fragments_are_low_confidence_not_missing_real_text():
    src = (
        "<div>\n"
        "@project.projectType match {\n"
        '    case DataSet => {\n'
        "        <p>Exports let you share environment variables and files between data sets.</p>\n"
        "    }\n"
        "    case _ => {\n"
        "        <p>Imports and exports let you share environment variables and files between projects.</p>\n"
        "    }\n"
        "}\n"
        "</div>\n"
    )
    findings = _findings(src)
    high_confidence_texts = {f.text for f in findings if f.confidence >= 0.3}
    assert "Exports let you share environment variables and files between data sets." in high_confidence_texts
    assert (
        "Imports and exports let you share environment variables and files between projects."
        in high_confidence_texts
    )
