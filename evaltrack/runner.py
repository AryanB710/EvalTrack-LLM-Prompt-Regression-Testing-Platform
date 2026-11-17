import json
import math
from concurrent.futures import ThreadPoolExecutor

from .checks import run_checks
from .compare import compare_runs
from .db import all_rows, now, one
from .pricing import cost_usd
from .providers import ProviderError, RetryPolicy, call_with_retry, get_provider


def render_prompt(template, case):
    # Plain replace instead of str.format so prompts can contain JSON braces.
    return template.replace("{{input}}", case["input"])


def p95(values):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]  # nearest-rank


def _evaluate(case, pv, provider, policy):
    prompt = render_prompt(pv["template"], case)
    specs = json.loads(case["checks"] or "[]")
    try:
        completion, attempts = call_with_retry(
            lambda: provider.complete(
                pv["system_prompt"], prompt, pv["model"], pv["temperature"], pv["max_tokens"]
            ),
            policy,
        )
    except ProviderError as exc:
        return {
            "case": case, "output": None, "error": str(exc), "attempts": exc.attempts,
            "latency_ms": None, "prompt_tokens": 0, "completion_tokens": 0,
            "passed": False, "checks": [],
        }

    checks = run_checks(completion.text, case["expected"], specs)
    return {
        "case": case,
        "output": completion.text,
        "error": None,
        "attempts": attempts,
        "latency_ms": completion.latency_ms,
        "prompt_tokens": completion.prompt_tokens,
        "completion_tokens": completion.completion_tokens,
        "passed": all(c.passed for c in checks),
        "checks": checks,
    }


def execute_run(conn, prompt_version_id, dataset_id, baseline_run_id=None,
                provider=None, policy=None, workers=4, triggered_by="web"):
    pv = one(conn, "SELECT * FROM prompt_versions WHERE id = ?", (prompt_version_id,))
    if pv is None:
        raise ValueError(f"prompt version {prompt_version_id} not found")
    cases = all_rows(conn, "SELECT * FROM test_cases WHERE dataset_id = ? ORDER BY case_key",
                     (dataset_id,))
    if not cases:
        raise ValueError("dataset has no test cases")

    # Resolve the provider before creating the run so a missing API key is
    # reported to the user instead of leaving a half-finished run behind.
    provider = provider or get_provider(pv["provider"])
    policy = policy or RetryPolicy()

    cur = conn.execute(
        """INSERT INTO runs (prompt_version_id, dataset_id, baseline_run_id, status,
                             started_at, triggered_by)
           VALUES (?, ?, ?, 'running', ?, ?)""",
        (prompt_version_id, dataset_id, baseline_run_id, now(), triggered_by),
    )
    run_id = cur.lastrowid
    conn.commit()

    try:
        # Calls are I/O bound, so threads are enough. Only the main thread
        # touches SQLite.
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            outcomes = list(pool.map(lambda c: _evaluate(c, pv, provider, policy), cases))

        for o in outcomes:
            needs_review = not o["passed"]
            conn.execute(
                """INSERT INTO results (run_id, test_case_id, output, error, attempts, latency_ms,
                       prompt_tokens, completion_tokens, cost_usd, passed, check_results, review_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id, o["case"]["id"], o["output"], o["error"], o["attempts"],
                    o["latency_ms"], o["prompt_tokens"], o["completion_tokens"],
                    cost_usd(pv["model"], o["prompt_tokens"], o["completion_tokens"]),
                    int(o["passed"]), json.dumps([c.to_dict() for c in o["checks"]]),
                    "pending" if needs_review else "none",
                ),
            )

        latencies = [o["latency_ms"] for o in outcomes if o["latency_ms"] is not None]
        prompt_tokens = sum(o["prompt_tokens"] for o in outcomes)
        completion_tokens = sum(o["completion_tokens"] for o in outcomes)
        conn.execute(
            """UPDATE runs SET total = ?, passed = ?, failed = ?, errored = ?,
                   avg_latency_ms = ?, p95_latency_ms = ?, prompt_tokens = ?,
                   completion_tokens = ?, cost_usd = ?
               WHERE id = ?""",
            (
                len(outcomes),
                sum(o["passed"] for o in outcomes),
                sum(1 for o in outcomes if not o["passed"] and not o["error"]),
                sum(1 for o in outcomes if o["error"]),
                sum(latencies) / len(latencies) if latencies else None,
                p95(latencies),
                prompt_tokens,
                completion_tokens,
                cost_usd(pv["model"], prompt_tokens, completion_tokens),
                run_id,
            ),
        )
        conn.commit()

        if baseline_run_id:
            _mark_regressions(conn, run_id, baseline_run_id)

        conn.execute("UPDATE runs SET status = 'completed', finished_at = ? WHERE id = ?",
                     (now(), run_id))
        conn.commit()
    except Exception as exc:
        conn.rollback()
        conn.execute("UPDATE runs SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
                     (str(exc)[:500], now(), run_id))
        conn.commit()
        raise

    return run_id


def _mark_regressions(conn, run_id, baseline_run_id):
    diff = compare_runs(conn, baseline_run_id, run_id)
    for row in diff["regressions"]:
        # A regression always goes to a human, even if it was already pending.
        conn.execute(
            "UPDATE results SET is_regression = 1, review_status = 'pending' WHERE id = ?",
            (row["candidate"]["id"],),
        )
    conn.execute("UPDATE runs SET regressions = ?, fixes = ? WHERE id = ?",
                 (len(diff["regressions"]), len(diff["fixes"]), run_id))
    conn.commit()
