from changed_literals.languages import resolve_adapter
from changed_literals.walker import collect_findings

ALL_LINES = set(range(1, 1000))


def _findings(path: str, source: str):
    adapter = resolve_adapter(path)
    assert adapter is not None, f"no adapter resolved for {path}"
    return collect_findings(source.encode(), adapter, ALL_LINES, "added", path, min_confidence=0.0)


def _by_text(findings, text):
    matches = [f for f in findings if f.text == text]
    assert matches, f"no finding with text={text!r} among {[f.text for f in findings]}"
    return matches[0]


def test_jsx_text_is_high_confidence():
    src = 'function F() { return <div>Hello there</div>; }'
    f = _by_text(_findings("Widget.tsx", src), "Hello there")
    assert f.context == "jsx-text"
    assert f.confidence >= 0.9


def test_allow_listed_jsx_attribute_is_high_confidence():
    src = 'function F() { return <Button label="Save changes" />; }'
    f = _by_text(_findings("Widget.tsx", src), "Save changes")
    assert f.context == "jsx-attr:label"
    assert f.confidence >= 0.8


def test_deny_listed_jsx_attribute_is_low_confidence():
    src = 'function F() { return <div className="save-changes-btn" />; }'
    f = _by_text(_findings("Widget.tsx", src), "save-changes-btn")
    assert f.context == "jsx-attr:className"
    assert f.confidence < 0.1


def test_i18n_call_is_high_confidence():
    src = 'const x = t("translated text");'
    f = _by_text(_findings("strings.ts", src), "translated text")
    assert f.context == "call:t"
    assert f.confidence >= 0.85


def test_console_call_is_denied():
    src = 'console.log("debug details");'
    f = _by_text(_findings("debug.ts", src), "debug details")
    assert f.context == "call:console.log"
    assert f.confidence < 0.1


def test_allow_listed_object_key_is_high_confidence():
    src = 'const opts = { title: "My Title" };'
    f = _by_text(_findings("opts.ts", src), "My Title")
    assert f.context == "prop:title"
    assert f.confidence >= 0.7


def test_deny_pattern_variable_name_is_low_confidence():
    src = 'const className = "some-class-name";'
    f = _by_text(_findings("styles.ts", src), "some-class-name")
    assert f.context == "var:className"
    assert f.confidence < 0.1


def test_no_substitution_template_literal_is_scored():
    src = "const msg = `Please try again`;"
    f = _by_text(_findings("msg.ts", src), "Please try again")
    assert f.context.startswith("var:msg")


def test_template_literal_with_substitution_is_skipped_but_nested_string_found():
    src = 'const msg = `prefix ${cond ? "yes" : "no"} suffix`;'
    findings = _findings("msg.ts", src)
    texts = {f.text for f in findings}
    assert "yes" in texts and "no" in texts


def test_plain_js_jsx_uses_same_adapter_as_tsx():
    src = 'function F() { return <div>Hello there</div>; }'
    f = _by_text(_findings("Widget.jsx", src), "Hello there")
    assert f.context == "jsx-text"
