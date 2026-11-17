import csv
import io
import json

from .checks import validate_checks
from .db import now, one


def parse_dataset(text, filename):
    """Accept either a JSON list of cases or a CSV with
    key,input,expected,checks,tags columns (checks is a JSON string)."""
    if filename.lower().endswith(".json"):
        data = json.loads(text)
        cases = data["cases"] if isinstance(data, dict) else data
    elif filename.lower().endswith(".csv"):
        cases = []
        for row in csv.DictReader(io.StringIO(text)):
            raw = (row.get("checks") or "").strip()
            cases.append({
                "key": row.get("key"),
                "input": row.get("input"),
                "expected": row.get("expected", ""),
                "checks": json.loads(raw) if raw else [],
                "tags": row.get("tags", ""),
            })
    else:
        raise ValueError("upload a .json or .csv file")

    cleaned, seen = [], set()
    for i, case in enumerate(cases, start=1):
        key = (case.get("key") or f"case-{i:03d}").strip()
        if not (case.get("input") or "").strip():
            raise ValueError(f"case '{key}' has no input")
        if key in seen:
            raise ValueError(f"duplicate case key '{key}'")
        seen.add(key)
        checks = case.get("checks") or []
        try:
            validate_checks(checks)
        except ValueError as exc:
            raise ValueError(f"case '{key}': {exc}") from exc
        tags = case.get("tags") or ""
        if isinstance(tags, list):
            tags = ",".join(tags)
        cleaned.append({
            "key": key,
            "input": case["input"].strip(),
            "expected": case.get("expected") or "",
            "checks": checks,
            "tags": tags,
        })
    if not cleaned:
        raise ValueError("no test cases found")
    return cleaned


def import_dataset(conn, name, cases, description=""):
    """Create the dataset, or merge into it if the name already exists.
    Cases are upserted on (dataset_id, case_key) instead of deleted and
    re-inserted, because old results reference test_case rows."""
    existing = one(conn, "SELECT id FROM datasets WHERE name = ?", (name,))
    if existing:
        dataset_id = existing["id"]
        if description:
            conn.execute("UPDATE datasets SET description = ? WHERE id = ?",
                         (description, dataset_id))
    else:
        dataset_id = conn.execute(
            "INSERT INTO datasets (name, description, created_at) VALUES (?, ?, ?)",
            (name, description, now()),
        ).lastrowid

    for c in cases:
        conn.execute(
            """INSERT INTO test_cases (dataset_id, case_key, input, expected, checks, tags)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT (dataset_id, case_key) DO UPDATE SET
                   input = excluded.input, expected = excluded.expected,
                   checks = excluded.checks, tags = excluded.tags""",
            (dataset_id, c["key"], c["input"], c["expected"], json.dumps(c["checks"]), c["tags"]),
        )
    conn.commit()
    return dataset_id
