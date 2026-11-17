"""Automated checks run against every model output.

A test case carries a list of specs like
    [{"type": "contains", "value": "5-7 business days"},
     {"type": "max_words", "value": 60}]
Each check returns (passed, detail). The case passes only if all checks pass.
"""
import json
import re
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher

REGISTRY = {}


@dataclass
class CheckResult:
    type: str
    passed: bool
    detail: str

    def to_dict(self):
        return asdict(self)


def register(name, required=()):
    def wrap(fn):
        REGISTRY[name] = (fn, required)
        return fn
    return wrap


def _norm(text, spec):
    return text if spec.get("case_sensitive") else text.lower()


@register("contains", required=("value",))
def _contains(output, expected, spec):
    ok = _norm(spec["value"], spec) in _norm(output, spec)
    return ok, f"'{spec['value']}' {'found' if ok else 'missing'}"


@register("not_contains", required=("value",))
def _not_contains(output, expected, spec):
    ok = _norm(spec["value"], spec) not in _norm(output, spec)
    return ok, f"'{spec['value']}' {'absent' if ok else 'present but should not be'}"


@register("contains_any", required=("values",))
def _contains_any(output, expected, spec):
    hits = [v for v in spec["values"] if _norm(v, spec) in _norm(output, spec)]
    return bool(hits), f"matched {hits}" if hits else f"none of {spec['values']}"


@register("contains_all", required=("values",))
def _contains_all(output, expected, spec):
    missing = [v for v in spec["values"] if _norm(v, spec) not in _norm(output, spec)]
    return not missing, "all present" if not missing else f"missing {missing}"


@register("regex", required=("pattern",))
def _regex(output, expected, spec):
    flags = 0 if spec.get("case_sensitive") else re.IGNORECASE
    ok = re.search(spec["pattern"], output, flags) is not None
    return ok, f"/{spec['pattern']}/ {'matched' if ok else 'no match'}"


@register("max_words", required=("value",))
def _max_words(output, expected, spec):
    n = len(output.split())
    return n <= spec["value"], f"{n} words (max {spec['value']})"


@register("min_words", required=("value",))
def _min_words(output, expected, spec):
    n = len(output.split())
    return n >= spec["value"], f"{n} words (min {spec['value']})"


@register("max_chars", required=("value",))
def _max_chars(output, expected, spec):
    n = len(output)
    return n <= spec["value"], f"{n} chars (max {spec['value']})"


@register("exact")
def _exact(output, expected, spec):
    target = spec.get("value", expected)
    ok = output.strip() == (target or "").strip()
    return ok, "exact match" if ok else "differs from expected"


@register("similarity", required=("min",))
def _similarity(output, expected, spec):
    # difflib is crude but dependency-free and good enough to spot answers that
    # drifted a lot from a reference. Not a substitute for semantic scoring.
    if not expected:
        return False, "no expected answer to compare against"
    ratio = SequenceMatcher(None, output.lower(), expected.lower()).ratio()
    return ratio >= spec["min"], f"similarity {ratio:.2f} (min {spec['min']})"


@register("json_valid")
def _json_valid(output, expected, spec):
    try:
        json.loads(output)
        return True, "valid JSON"
    except ValueError as exc:
        return False, f"invalid JSON: {exc}"


@register("json_has_keys", required=("keys",))
def _json_has_keys(output, expected, spec):
    try:
        data = json.loads(output)
    except ValueError:
        return False, "not JSON"
    if not isinstance(data, dict):
        return False, "JSON is not an object"
    missing = [k for k in spec["keys"] if k not in data]
    return not missing, "all keys present" if not missing else f"missing keys {missing}"


def validate_checks(specs):
    """Raise ValueError for malformed specs. Used when importing datasets so a
    typo shows up at upload time instead of as a confusing failed run."""
    if not isinstance(specs, list):
        raise ValueError("checks must be a list")
    for i, spec in enumerate(specs):
        if not isinstance(spec, dict) or "type" not in spec:
            raise ValueError(f"check #{i + 1} needs a 'type'")
        if spec["type"] not in REGISTRY:
            raise ValueError(f"unknown check type '{spec['type']}'")
        _, required = REGISTRY[spec["type"]]
        missing = [f for f in required if f not in spec]
        if missing:
            raise ValueError(f"check '{spec['type']}' is missing {missing}")


def run_checks(output, expected, specs):
    output = output or ""
    results = []
    for spec in specs:
        entry = REGISTRY.get(spec.get("type"))
        if entry is None:
            results.append(CheckResult(spec.get("type", "?"), False, "unknown check type"))
            continue
        fn, _ = entry
        try:
            ok, detail = fn(output, expected or "", spec)
        except Exception as exc:  # a bad regex etc. shouldn't kill the run
            ok, detail = False, f"check crashed: {exc}"
        results.append(CheckResult(spec["type"], bool(ok), detail))
    return results
