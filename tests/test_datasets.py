import json

import pytest

from evaltrack.datasets import import_dataset, parse_dataset
from evaltrack.db import all_rows
from evaltrack.demo import SAMPLE_DATASET


def test_sample_dataset_parses():
    cases = parse_dataset(SAMPLE_DATASET.read_text(), SAMPLE_DATASET.name)
    assert len(cases) == 12
    assert all(c["checks"] for c in cases)


def test_csv_import():
    text = ('key,input,expected,checks,tags\n'
            'q1,"How much?",,"[{""type"": ""contains"", ""value"": ""Rs""}]",billing\n'
            'q2,Hello,,,\n')
    cases = parse_dataset(text, "cases.csv")
    assert cases[0]["checks"] == [{"type": "contains", "value": "Rs"}]
    assert cases[1]["checks"] == []


def test_keys_are_generated_when_missing():
    cases = parse_dataset(json.dumps([{"input": "a"}, {"input": "b"}]), "x.json")
    assert [c["key"] for c in cases] == ["case-001", "case-002"]


@pytest.mark.parametrize("payload, message", [
    ([{"key": "a", "input": ""}], "has no input"),
    ([{"key": "a", "input": "x"}, {"key": "a", "input": "y"}], "duplicate case key"),
    ([{"key": "a", "input": "x", "checks": [{"type": "nope"}]}], "case 'a': unknown check type"),
    ([], "no test cases"),
])
def test_invalid_datasets_are_rejected(payload, message):
    with pytest.raises(ValueError, match=message):
        parse_dataset(json.dumps(payload), "x.json")


def test_unsupported_extension():
    with pytest.raises(ValueError, match=".json or .csv"):
        parse_dataset("", "x.txt")


def test_reimport_updates_by_key(conn):
    first = [{"key": "a", "input": "old", "expected": "", "checks": [], "tags": ""}]
    ds = import_dataset(conn, "d", first)
    second = [{"key": "a", "input": "new", "expected": "", "checks": [], "tags": ""},
              {"key": "b", "input": "added", "expected": "", "checks": [], "tags": ""}]
    assert import_dataset(conn, "d", second) == ds
    rows = all_rows(conn, "SELECT case_key, input FROM test_cases ORDER BY case_key")
    assert [tuple(r) for r in rows] == [("a", "new"), ("b", "added")]
