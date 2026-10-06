from changed_literals.scoring import linguistic_score


def test_empty_string_scores_zero():
    assert linguistic_score("   ") == 0.0


def test_identifier_like_text_scores_low():
    assert linguistic_score("my-css-class") == 0.15
    assert linguistic_score("userId") == 0.15


def test_prose_scores_high():
    score = linguistic_score("Please enter your email address.")
    assert score >= 0.7


def test_single_bare_word_matches_identifier_fast_path():
    # A single alphabetic word is indistinguishable from an identifier/slug
    # by this heuristic, same as the original TS implementation -- multi-word
    # phrases are what the linguistic scoring is actually meant to catch.
    assert linguistic_score("Save") == 0.15


def test_short_multiword_phrase_scores_above_identifier_floor():
    score = linguistic_score("Save changes")
    assert 0.5 <= score < 0.9
