"""Command line entry point.

    python -m evaltrack seed
    python -m evaltrack import sample_data/tiffinly_support.json
    python -m evaltrack run --prompt support-bot --dataset tiffinly-support --fail-on-regression

`run` exits with code 1 when the quality gate fails, so it can block a merge
in CI the same way a failing unit test does.
"""
import argparse
import os
import sys
from pathlib import Path

from .compare import compare_runs, suggest_baseline
from .datasets import import_dataset, parse_dataset
from .db import all_rows, connect, init_db, one
from .providers import ProviderError
from .runner import execute_run
from .seed import seed_demo

EXIT_OK, EXIT_GATE_FAILED, EXIT_ERROR = 0, 1, 2


def _conn(args):
    conn = connect(args.db)
    init_db(conn)
    return conn


def cmd_seed(args):
    conn = _conn(args)
    if seed_demo(conn):
        print("Loaded demo dataset, 3 prompt versions and 3 runs.")
    else:
        print("Demo data already present, nothing to do.")
    return EXIT_OK


def cmd_import(args):
    path = Path(args.file)
    cases = parse_dataset(path.read_text(encoding="utf-8"), path.name)
    name = args.name or path.stem
    import_dataset(_conn(args), name, cases, args.description or "")
    print(f"Imported {len(cases)} cases into '{name}'.")
    return EXIT_OK


def cmd_run(args):
    conn = _conn(args)
    if args.version:
        pv = one(conn, "SELECT * FROM prompt_versions WHERE name = ? AND version = ?",
                 (args.prompt, args.version))
    else:
        pv = one(conn, "SELECT * FROM prompt_versions WHERE name = ? ORDER BY version DESC LIMIT 1",
                 (args.prompt,))
    ds = one(conn, "SELECT * FROM datasets WHERE name = ?", (args.dataset,))
    if pv is None or ds is None:
        print("error: prompt or dataset not found", file=sys.stderr)
        return EXIT_ERROR

    if args.baseline == "auto":
        baseline = suggest_baseline(conn, pv["id"], ds["id"])
    elif args.baseline == "none":
        baseline = None
    else:
        baseline = int(args.baseline)

    print(f"Running {pv['name']} v{pv['version']} on {ds['name']}"
          + (f" (baseline run #{baseline})" if baseline else "") + " ...")
    run_id = execute_run(conn, pv["id"], ds["id"], baseline_run_id=baseline,
                         workers=args.workers, triggered_by="cli")
    run = one(conn, "SELECT * FROM runs WHERE id = ?", (run_id,))

    pass_rate = run["passed"] / run["total"]
    print(f"run #{run_id}: {run['passed']}/{run['total']} passed ({pass_rate:.0%}), "
          f"{run['errored']} errors, avg {run['avg_latency_ms']:.0f} ms, "
          f"p95 {run['p95_latency_ms']:.0f} ms, ${run['cost_usd']:.6f}")

    failed_gate = False
    if baseline:
        diff = compare_runs(conn, baseline, run_id)
        for r in diff["regressions"]:
            print(f"  REGRESSION  {r['case_key']}")
        for r in diff["fixes"]:
            print(f"  fixed       {r['case_key']}")
        if args.fail_on_regression and diff["regressions"]:
            print(f"gate failed: {len(diff['regressions'])} regression(s)")
            failed_gate = True
    if args.min_pass_rate is not None and pass_rate < args.min_pass_rate:
        print(f"gate failed: pass rate {pass_rate:.0%} is below {args.min_pass_rate:.0%}")
        failed_gate = True

    return EXIT_GATE_FAILED if failed_gate else EXIT_OK


def cmd_runs(args):
    rows = all_rows(_conn(args), """
        SELECT r.id, pv.name, pv.version, d.name AS dataset, r.status, r.passed, r.total,
               r.regressions, r.baseline_run_id
        FROM runs r JOIN prompt_versions pv ON pv.id = r.prompt_version_id
        JOIN datasets d ON d.id = r.dataset_id ORDER BY r.id DESC LIMIT 20""")
    for r in rows:
        reg = f"{r['regressions']} regressions" if r["baseline_run_id"] else "no baseline"
        print(f"#{r['id']:<4} {r['name']} v{r['version']:<3} {r['dataset']:<20} "
              f"{r['status']:<10} {r['passed']}/{r['total']}  {reg}")
    return EXIT_OK


def build_parser():
    p = argparse.ArgumentParser(prog="evaltrack", description="LLM prompt regression testing")
    p.add_argument("--db", default=os.environ.get("EVALTRACK_DB", "evaltrack.db"))
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("seed", help="load demo data").set_defaults(fn=cmd_seed)

    imp = sub.add_parser("import", help="import a dataset from .json or .csv")
    imp.add_argument("file")
    imp.add_argument("--name")
    imp.add_argument("--description")
    imp.set_defaults(fn=cmd_import)

    run = sub.add_parser("run", help="run a prompt version against a dataset")
    run.add_argument("--prompt", required=True, help="prompt name")
    run.add_argument("--version", type=int, help="defaults to the latest version")
    run.add_argument("--dataset", required=True)
    run.add_argument("--baseline", default="auto", help="auto | none | <run id>")
    run.add_argument("--workers", type=int, default=4)
    run.add_argument("--fail-on-regression", action="store_true")
    run.add_argument("--min-pass-rate", type=float, help="e.g. 0.9")
    run.set_defaults(fn=cmd_run)

    sub.add_parser("runs", help="list recent runs").set_defaults(fn=cmd_runs)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (ProviderError, ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
