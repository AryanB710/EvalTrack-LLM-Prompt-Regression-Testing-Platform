"""Diff two runs case by case.

Cases are matched on case_key rather than test_case id, so a dataset that was
re-imported (new ids, same keys) can still be compared against old runs.
"""
from .db import all_rows, one


def final_verdict(row):
    """Human review overrides the automated checks."""
    if row["review_status"] == "approved":
        return True
    if row["review_status"] == "rejected":
        return False
    return bool(row["passed"])


def _results_by_key(conn, run_id):
    rows = all_rows(
        conn,
        """SELECT r.*, tc.case_key, tc.input, tc.expected, tc.tags
           FROM results r JOIN test_cases tc ON tc.id = r.test_case_id
           WHERE r.run_id = ? ORDER BY tc.case_key""",
        (run_id,),
    )
    return {row["case_key"]: row for row in rows}


def classify(base_pass, cand_pass):
    if base_pass is None:
        return "new"
    if cand_pass is None:
        return "removed"
    if base_pass and not cand_pass:
        return "regression"
    if not base_pass and cand_pass:
        return "fixed"
    return "passing" if cand_pass else "still_failing"


def compare_runs(conn, baseline_id, candidate_id):
    base = _results_by_key(conn, baseline_id)
    cand = _results_by_key(conn, candidate_id)

    rows = []
    for key in sorted(set(base) | set(cand)):
        b, c = base.get(key), cand.get(key)
        b_pass = final_verdict(b) if b else None
        c_pass = final_verdict(c) if c else None
        rows.append({
            "case_key": key,
            "input": (c or b)["input"],
            "baseline": b,
            "candidate": c,
            "base_pass": b_pass,
            "cand_pass": c_pass,
            "status": classify(b_pass, c_pass),
        })

    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    base_run = one(conn, "SELECT * FROM runs WHERE id = ?", (baseline_id,))
    cand_run = one(conn, "SELECT * FROM runs WHERE id = ?", (candidate_id,))
    return {
        "rows": rows,
        "counts": counts,
        "regressions": [r for r in rows if r["status"] == "regression"],
        "fixes": [r for r in rows if r["status"] == "fixed"],
        "deltas": metric_deltas(base_run, cand_run),
    }


def metric_deltas(base_run, cand_run):
    def rate(run):
        return run["passed"] / run["total"] if run["total"] else 0.0

    def pct_change(a, b):
        if not a:
            return None
        return (b - a) / a * 100

    return {
        "pass_rate": (rate(cand_run) - rate(base_run)) * 100,  # percentage points
        "avg_latency_pct": pct_change(base_run["avg_latency_ms"], cand_run["avg_latency_ms"]),
        "p95_latency_pct": pct_change(base_run["p95_latency_ms"], cand_run["p95_latency_ms"]),
        "cost_pct": pct_change(base_run["cost_usd"], cand_run["cost_usd"]),
        "tokens_pct": pct_change(
            base_run["prompt_tokens"] + base_run["completion_tokens"],
            cand_run["prompt_tokens"] + cand_run["completion_tokens"],
        ),
    }


def suggest_baseline(conn, prompt_version_id, dataset_id):
    """Latest completed run of a *different* version of the same prompt on the
    same dataset. That's almost always what you want to compare against."""
    row = one(
        conn,
        """SELECT r.id FROM runs r
           JOIN prompt_versions pv ON pv.id = r.prompt_version_id
           WHERE r.status = 'completed' AND r.dataset_id = ?
             AND pv.name = (SELECT name FROM prompt_versions WHERE id = ?)
             AND pv.id != ?
           ORDER BY r.id DESC LIMIT 1""",
        (dataset_id, prompt_version_id, prompt_version_id),
    )
    return row["id"] if row else None
