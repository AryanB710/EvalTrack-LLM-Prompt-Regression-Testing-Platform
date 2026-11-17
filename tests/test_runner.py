import json

import pytest

from evaltrack.compare import compare_runs, final_verdict, suggest_baseline
from evaltrack.db import all_rows, one
from evaltrack.providers import MockProvider, ProviderError, RetryPolicy
from evaltrack.runner import execute_run, p95, render_prompt


def results(conn, run_id):
    return {r["case_key"]: r for r in all_rows(conn, """
        SELECT res.*, tc.case_key FROM results res
        JOIN test_cases tc ON tc.id = res.test_case_id WHERE run_id = ?""", (run_id,))}


def test_render_prompt_leaves_other_braces_alone():
    assert render_prompt('Return {"a": 1} for {{input}}', {"input": "x"}) == 'Return {"a": 1} for x'


def test_p95_nearest_rank():
    assert p95([]) is None
    assert p95([5]) == 5
    assert p95(list(range(1, 101))) == 95


def test_run_records_results_and_aggregates(conn, small_dataset, make_version, echo, no_sleep):
    run_id = execute_run(conn, make_version(), small_dataset, provider=echo(), policy=no_sleep)
    run = one(conn, "SELECT * FROM runs WHERE id = ?", (run_id,))
    assert run["status"] == "completed"
    assert (run["total"], run["passed"], run["failed"], run["errored"]) == (2, 2, 0, 0)
    assert run["avg_latency_ms"] == 100.0
    assert run["prompt_tokens"] == 10 and run["completion_tokens"] == 6
    stored = results(conn, run_id)
    assert stored["a"]["output"] == "hello"
    assert json.loads(stored["a"]["check_results"])[0]["passed"] is True
    assert stored["a"]["review_status"] == "none"


def test_failures_go_to_review_queue(conn, small_dataset, make_version, echo, no_sleep):
    run_id = execute_run(conn, make_version(), small_dataset, provider=echo(broken={"bye"}),
                         policy=no_sleep)
    stored = results(conn, run_id)
    assert stored["b"]["passed"] == 0
    assert stored["b"]["review_status"] == "pending"


def test_regressions_are_flagged_against_baseline(conn, small_dataset, make_version, echo, no_sleep):
    v1 = make_version()
    base = execute_run(conn, v1, small_dataset, provider=echo(), policy=no_sleep)
    v2 = make_version("shorter")
    cand = execute_run(conn, v2, small_dataset, baseline_run_id=base,
                       provider=echo(broken={"hello"}), policy=no_sleep)

    run = one(conn, "SELECT * FROM runs WHERE id = ?", (cand,))
    assert run["regressions"] == 1 and run["fixes"] == 0
    stored = results(conn, cand)
    assert stored["a"]["is_regression"] == 1
    assert stored["b"]["is_regression"] == 0


def test_fixes_are_counted(conn, small_dataset, make_version, echo, no_sleep):
    base = execute_run(conn, make_version(), small_dataset, provider=echo(broken={"bye"}),
                       policy=no_sleep)
    cand = execute_run(conn, make_version(), small_dataset, baseline_run_id=base,
                       provider=echo(), policy=no_sleep)
    diff = compare_runs(conn, base, cand)
    assert [r["case_key"] for r in diff["fixes"]] == ["b"]
    assert diff["counts"] == {"passing": 1, "fixed": 1}


def test_human_review_overrides_comparison(conn, small_dataset, make_version, echo, no_sleep):
    base = execute_run(conn, make_version(), small_dataset, provider=echo(), policy=no_sleep)
    cand = execute_run(conn, make_version(), small_dataset, baseline_run_id=base,
                       provider=echo(broken={"hello"}), policy=no_sleep)
    regressed = results(conn, cand)["a"]
    conn.execute("UPDATE results SET review_status = 'approved' WHERE id = ?", (regressed["id"],))
    conn.commit()
    assert compare_runs(conn, base, cand)["regressions"] == []


def test_final_verdict():
    assert final_verdict({"passed": 0, "review_status": "approved"}) is True
    assert final_verdict({"passed": 1, "review_status": "rejected"}) is False
    assert final_verdict({"passed": 1, "review_status": "pending"}) is True


def test_provider_errors_are_recorded_per_case(conn, small_dataset, make_version):
    class AlwaysDown:
        def complete(self, *a, **k):
            raise ProviderError("HTTP 401: invalid key")

    run_id = execute_run(conn, make_version(), small_dataset, provider=AlwaysDown(),
                         policy=RetryPolicy(sleep=lambda s: None))
    run = one(conn, "SELECT * FROM runs WHERE id = ?", (run_id,))
    assert run["status"] == "completed"
    assert run["errored"] == 2 and run["failed"] == 0
    assert run["avg_latency_ms"] is None
    assert "invalid key" in results(conn, run_id)["a"]["error"]


def test_transient_failures_are_retried(conn, small_dataset, make_version):
    responder = lambda system, prompt: prompt.replace("say ", "")
    provider = MockProvider(responder=responder, failure_rate=0.5, seed=1)
    run_id = execute_run(conn, make_version(), small_dataset, provider=provider,
                         policy=RetryPolicy(max_attempts=10, sleep=lambda s: None))
    stored = results(conn, run_id)
    assert all(r["passed"] for r in stored.values())
    assert any(r["attempts"] > 1 for r in stored.values())


def test_missing_api_key_fails_before_creating_run(conn, small_dataset, monkeypatch):
    from evaltrack.seed import create_prompt_version
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    pv = create_prompt_version(conn, "real", "", "{{input}}", provider="groq",
                               model="llama-3.1-8b-instant")
    with pytest.raises(ProviderError):
        execute_run(conn, pv, small_dataset)
    assert one(conn, "SELECT COUNT(*) AS n FROM runs")["n"] == 0


def test_empty_dataset_is_rejected(conn, make_version, echo):
    conn.execute("INSERT INTO datasets (name, created_at) VALUES ('empty', 'now')")
    ds = one(conn, "SELECT id FROM datasets WHERE name = 'empty'")["id"]
    with pytest.raises(ValueError, match="no test cases"):
        execute_run(conn, make_version(), ds, provider=echo())


def test_suggest_baseline_picks_latest_other_version(conn, small_dataset, make_version, echo, no_sleep):
    v1, v2 = make_version(), make_version()
    assert suggest_baseline(conn, v2, small_dataset) is None
    r1 = execute_run(conn, v1, small_dataset, provider=echo(), policy=no_sleep)
    execute_run(conn, v2, small_dataset, provider=echo(), policy=no_sleep)
    assert suggest_baseline(conn, v2, small_dataset) == r1
