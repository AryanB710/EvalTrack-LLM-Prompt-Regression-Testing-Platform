import pytest

from evaltrack.checks import run_checks, validate_checks


def verdicts(output, specs, expected=""):
    return [r.passed for r in run_checks(output, expected, specs)]


def test_contains_is_case_insensitive_by_default():
    assert verdicts("Refunds take 5-7 Business Days", [{"type": "contains", "value": "business days"}]) == [True]


def test_contains_case_sensitive_flag():
    spec = [{"type": "contains", "value": "UPI", "case_sensitive": True}]
    assert verdicts("we accept upi", spec) == [False]


def test_not_contains_catches_leaked_code():
    assert verdicts("use FREE100", [{"type": "not_contains", "value": "free100"}]) == [False]


def test_contains_any_and_all():
    assert verdicts("cash only", [{"type": "contains_any", "values": ["upi", "cash"]}]) == [True]
    assert verdicts("cash only", [{"type": "contains_all", "values": ["upi", "cash"]}]) == [False]


def test_regex():
    assert verdicts("I can't do that", [{"type": "regex", "pattern": r"can'?t|cannot"}]) == [True]


def test_word_and_char_limits():
    text = "one two three four"
    assert verdicts(text, [{"type": "max_words", "value": 3}]) == [False]
    assert verdicts(text, [{"type": "min_words", "value": 3}]) == [True]
    assert verdicts(text, [{"type": "max_chars", "value": 100}]) == [True]


def test_exact_uses_expected_when_no_value():
    assert verdicts(" yes ", [{"type": "exact"}], expected="yes") == [True]


def test_similarity_needs_expected():
    result = run_checks("anything", "", [{"type": "similarity", "min": 0.5}])[0]
    assert not result.passed
    assert "no expected" in result.detail


def test_similarity_threshold():
    ref = "Refunds are processed within 5-7 business days."
    assert verdicts(ref, [{"type": "similarity", "min": 0.9}], expected=ref) == [True]
    assert verdicts("No.", [{"type": "similarity", "min": 0.5}], expected=ref) == [False]


def test_json_checks():
    assert verdicts('{"answer": "x"}', [{"type": "json_valid"}]) == [True]
    assert verdicts("not json", [{"type": "json_valid"}]) == [False]
    assert verdicts('{"answer": "x"}', [{"type": "json_has_keys", "keys": ["answer", "escalate"]}]) == [False]
    assert verdicts('[1, 2]', [{"type": "json_has_keys", "keys": ["a"]}]) == [False]


def test_bad_regex_fails_the_check_instead_of_crashing():
    result = run_checks("text", "", [{"type": "regex", "pattern": "("}])[0]
    assert not result.passed
    assert "crashed" in result.detail


def test_none_output_is_treated_as_empty():
    assert verdicts(None, [{"type": "contains", "value": "x"}]) == [False]


@pytest.mark.parametrize("specs, message", [
    ("nope", "must be a list"),
    ([{"value": "x"}], "needs a 'type'"),
    ([{"type": "smells_good"}], "unknown check type"),
    ([{"type": "contains"}], "is missing"),
])
def test_validate_checks_rejects_bad_specs(specs, message):
    with pytest.raises(ValueError, match=message):
        validate_checks(specs)
