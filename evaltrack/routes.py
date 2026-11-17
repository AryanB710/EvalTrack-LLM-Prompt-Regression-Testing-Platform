import csv
import difflib
import io
import json

from flask import (Blueprint, Response, abort, flash, redirect, render_template, request,
                   url_for)

from . import get_db
from .compare import compare_runs, final_verdict, suggest_baseline
from .datasets import import_dataset, parse_dataset
from .db import all_rows, now, one
from .providers import PROVIDERS, ProviderError
from .runner import execute_run
from .seed import create_prompt_version

bp = Blueprint("main", __name__)

RUN_SELECT = """
    SELECT r.*, pv.name AS prompt_name, pv.version, pv.model, pv.provider, d.name AS dataset_name,
           SUM(CASE WHEN res.review_status = 'approved' THEN 1
                    WHEN res.review_status = 'rejected' THEN 0
                    ELSE res.passed END) AS reviewed_passed,
           SUM(CASE WHEN res.review_status = 'pending' THEN 1 ELSE 0 END) AS pending
    FROM runs r
    JOIN prompt_versions pv ON pv.id = r.prompt_version_id
    JOIN datasets d ON d.id = r.dataset_id
    LEFT JOIN results res ON res.run_id = r.id
"""


def get_run(run_id):
    run = one(get_db(), RUN_SELECT + " WHERE r.id = ? GROUP BY r.id", (run_id,))
    if run is None:
        abort(404)
    return run


@bp.app_context_processor
def nav_counts():
    row = one(get_db(), "SELECT COUNT(*) AS n FROM results WHERE review_status = 'pending'")
    return {"pending_reviews": row["n"]}


# ---------------------------------------------------------------- dashboard

@bp.route("/")
def dashboard():
    db = get_db()
    datasets = all_rows(db, "SELECT * FROM datasets ORDER BY name")
    dataset_id = request.args.get("dataset", type=int) or (datasets[0]["id"] if datasets else None)

    latest = []
    if dataset_id:
        latest = all_rows(
            db,
            RUN_SELECT + """
            WHERE r.id IN (SELECT MAX(id) FROM runs
                           WHERE status = 'completed' AND dataset_id = ?
                           GROUP BY prompt_version_id)
            GROUP BY r.id ORDER BY pv.name, pv.version""",
            (dataset_id,),
        )

    chart = {
        "labels": [f"{r['prompt_name']} v{r['version']}" for r in latest],
        "pass_rate": [round(r["passed"] / r["total"] * 100, 1) for r in latest],
        "reviewed_rate": [round(r["reviewed_passed"] / r["total"] * 100, 1) for r in latest],
        "avg_latency": [round(r["avg_latency_ms"] or 0) for r in latest],
        "p95_latency": [round(r["p95_latency_ms"] or 0) for r in latest],
        "cost_per_1k": [round(r["cost_usd"] / r["total"] * 1000, 4) for r in latest],
        "tokens_per_case": [round((r["prompt_tokens"] + r["completion_tokens"]) / r["total"])
                            for r in latest],
    }
    recent = all_rows(db, RUN_SELECT + " GROUP BY r.id ORDER BY r.id DESC LIMIT 10")
    return render_template("dashboard.html", datasets=datasets, dataset_id=dataset_id,
                           latest=latest, chart=chart, recent=recent)


# ---------------------------------------------------------------- datasets

@bp.route("/datasets", methods=["GET", "POST"])
def datasets():
    db = get_db()
    if request.method == "POST":
        upload = request.files.get("file")
        name = request.form.get("name", "").strip()
        if not upload or not upload.filename:
            flash("Choose a .json or .csv file to upload.", "error")
            return redirect(url_for(".datasets"))
        try:
            text = upload.read().decode("utf-8")
            cases = parse_dataset(text, upload.filename)
            if not name:
                name = upload.filename.rsplit(".", 1)[0]
            dataset_id = import_dataset(db, name, cases, request.form.get("description", ""))
        except (ValueError, UnicodeDecodeError) as exc:
            flash(f"Couldn't import that file: {exc}", "error")
            return redirect(url_for(".datasets"))
        flash(f"Imported {len(cases)} cases into '{name}'.", "ok")
        return redirect(url_for(".dataset_detail", dataset_id=dataset_id))

    rows = all_rows(db, """
        SELECT d.*, COUNT(tc.id) AS n_cases,
               (SELECT COUNT(*) FROM runs WHERE dataset_id = d.id) AS n_runs
        FROM datasets d LEFT JOIN test_cases tc ON tc.dataset_id = d.id
        GROUP BY d.id ORDER BY d.name""")
    return render_template("datasets.html", datasets=rows)


@bp.route("/datasets/<int:dataset_id>")
def dataset_detail(dataset_id):
    db = get_db()
    dataset = one(db, "SELECT * FROM datasets WHERE id = ?", (dataset_id,))
    if dataset is None:
        abort(404)
    cases = all_rows(db, "SELECT * FROM test_cases WHERE dataset_id = ? ORDER BY case_key",
                     (dataset_id,))
    return render_template("dataset_detail.html", dataset=dataset, cases=cases)


@bp.route("/datasets/<int:dataset_id>/export.json")
def dataset_export(dataset_id):
    db = get_db()
    dataset = one(db, "SELECT * FROM datasets WHERE id = ?", (dataset_id,))
    if dataset is None:
        abort(404)
    cases = all_rows(db, "SELECT * FROM test_cases WHERE dataset_id = ? ORDER BY case_key",
                     (dataset_id,))
    body = {
        "name": dataset["name"],
        "description": dataset["description"],
        "cases": [{
            "key": c["case_key"], "input": c["input"], "expected": c["expected"],
            "checks": json.loads(c["checks"]),
            "tags": [t for t in c["tags"].split(",") if t],
        } for c in cases],
    }
    return Response(json.dumps(body, indent=2), mimetype="application/json",
                    headers={"Content-Disposition": f"attachment; filename={dataset['name']}.json"})


# ---------------------------------------------------------------- prompts

@bp.route("/prompts")
def prompts():
    rows = all_rows(get_db(), """
        SELECT pv.*, (SELECT COUNT(*) FROM runs WHERE prompt_version_id = pv.id) AS n_runs
        FROM prompt_versions pv ORDER BY pv.name, pv.version DESC""")
    grouped = {}
    for r in rows:
        grouped.setdefault(r["name"], []).append(r)
    return render_template("prompts.html", grouped=grouped)


@bp.route("/prompts/new", methods=["GET", "POST"])
def prompt_new():
    db = get_db()
    if request.method == "POST":
        f = request.form
        name = f.get("name", "").strip()
        template = f.get("template", "")
        if not name or "{{input}}" not in template:
            flash("A prompt needs a name and a template containing {{input}}.", "error")
            return render_template("prompt_form.html", base=f, providers=_provider_names())
        try:
            temperature = float(f.get("temperature") or 0)
            max_tokens = int(f.get("max_tokens") or 300)
        except ValueError:
            flash("Temperature and max tokens must be numbers.", "error")
            return render_template("prompt_form.html", base=f, providers=_provider_names())
        pv_id = create_prompt_version(
            db, name, f.get("system_prompt", ""), template,
            provider=f.get("provider", "mock"), model=f.get("model", "").strip() or "mock-small",
            temperature=temperature, max_tokens=max_tokens, notes=f.get("notes", ""),
        )
        flash("Saved new prompt version.", "ok")
        return redirect(url_for(".prompt_detail", pv_id=pv_id))

    base = None
    if request.args.get("from"):
        base = one(db, "SELECT * FROM prompt_versions WHERE id = ?",
                   (request.args.get("from", type=int),))
    return render_template("prompt_form.html", base=base, providers=_provider_names())


@bp.route("/prompts/<int:pv_id>")
def prompt_detail(pv_id):
    db = get_db()
    pv = one(db, "SELECT * FROM prompt_versions WHERE id = ?", (pv_id,))
    if pv is None:
        abort(404)
    prev = one(db, """SELECT * FROM prompt_versions WHERE name = ? AND version < ?
                      ORDER BY version DESC LIMIT 1""", (pv["name"], pv["version"]))
    diff = None
    if prev:
        def as_text(p):
            return f"[system]\n{p['system_prompt']}\n\n[template]\n{p['template']}\n\n" \
                   f"[model] {p['provider']}/{p['model']}  temp={p['temperature']}\n"
        diff = list(difflib.unified_diff(
            as_text(prev).splitlines(), as_text(pv).splitlines(),
            fromfile=f"v{prev['version']}", tofile=f"v{pv['version']}", lineterm="",
        ))
    runs = all_rows(db, RUN_SELECT + " WHERE r.prompt_version_id = ? GROUP BY r.id ORDER BY r.id DESC",
                    (pv_id,))
    return render_template("prompt_detail.html", pv=pv, prev=prev, diff=diff, runs=runs)


def _provider_names():
    return ["mock", *PROVIDERS.keys()]


# ---------------------------------------------------------------- runs

@bp.route("/runs")
def runs():
    rows = all_rows(get_db(), RUN_SELECT + " GROUP BY r.id ORDER BY r.id DESC")
    return render_template("runs.html", runs=rows)


@bp.route("/runs/new", methods=["GET", "POST"])
def run_new():
    db = get_db()
    if request.method == "POST":
        pv_id = request.form.get("prompt_version_id", type=int)
        dataset_id = request.form.get("dataset_id", type=int)
        baseline = request.form.get("baseline", "auto")
        if not pv_id or not dataset_id:
            flash("Pick a prompt version and a dataset.", "error")
            return redirect(url_for(".run_new"))

        if baseline == "auto":
            baseline_id = suggest_baseline(db, pv_id, dataset_id)
        elif baseline == "none":
            baseline_id = None
        else:
            baseline_id = int(baseline)

        try:
            run_id = execute_run(db, pv_id, dataset_id, baseline_run_id=baseline_id,
                                 workers=request.form.get("workers", 4, type=int))
        except (ProviderError, ValueError) as exc:
            flash(f"Run failed: {exc}", "error")
            return redirect(url_for(".run_new", prompt_version_id=pv_id, dataset_id=dataset_id))
        return redirect(url_for(".run_detail", run_id=run_id))

    versions = all_rows(db, "SELECT * FROM prompt_versions ORDER BY name, version DESC")
    datasets_ = all_rows(db, "SELECT * FROM datasets ORDER BY name")
    past_runs = all_rows(db, RUN_SELECT + " WHERE r.status = 'completed' GROUP BY r.id ORDER BY r.id DESC")
    return render_template(
        "run_new.html", versions=versions, datasets=datasets_, past_runs=past_runs,
        selected_pv=request.args.get("prompt_version_id", type=int),
        selected_ds=request.args.get("dataset_id", type=int),
    )


@bp.route("/runs/<int:run_id>")
def run_detail(run_id):
    db = get_db()
    run = get_run(run_id)
    show = request.args.get("show", "all")

    results = all_rows(db, """
        SELECT res.*, tc.case_key, tc.input, tc.expected, tc.tags
        FROM results res JOIN test_cases tc ON tc.id = res.test_case_id
        WHERE res.run_id = ? ORDER BY res.is_regression DESC, res.passed, tc.case_key""",
        (run_id,))
    filters = {
        "all": lambda r: True,
        "failed": lambda r: not final_verdict(r),
        "regressions": lambda r: r["is_regression"],
        "pending": lambda r: r["review_status"] == "pending",
        "retried": lambda r: r["attempts"] > 1,
    }
    shown = [r for r in results if filters.get(show, filters["all"])(r)]

    comparison, baseline = None, None
    if run["baseline_run_id"]:
        baseline = get_run(run["baseline_run_id"])
        comparison = compare_runs(db, run["baseline_run_id"], run_id)

    tag_stats = {}
    for r in results:
        for tag in filter(None, (r["tags"] or "").split(",")):
            s = tag_stats.setdefault(tag, [0, 0])
            s[0] += final_verdict(r)
            s[1] += 1

    return render_template("run_detail.html", run=run, results=shown, all_count=len(results),
                           show=show, baseline=baseline, comparison=comparison,
                           tag_stats=sorted(tag_stats.items()))


@bp.route("/runs/<int:run_id>/export.csv")
def run_export(run_id):
    run = get_run(run_id)
    rows = all_rows(get_db(), """
        SELECT tc.case_key, tc.input, res.output, res.error, res.passed, res.is_regression,
               res.review_status, res.review_note, res.latency_ms, res.attempts,
               res.prompt_tokens, res.completion_tokens, res.cost_usd, res.check_results
        FROM results res JOIN test_cases tc ON tc.id = res.test_case_id
        WHERE res.run_id = ? ORDER BY tc.case_key""", (run_id,))
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(rows[0].keys() if rows else ["case_key"])
    for r in rows:
        writer.writerow(list(r))
    filename = f"run-{run_id}-{run['prompt_name']}-v{run['version']}.csv"
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@bp.route("/compare")
def compare():
    db = get_db()
    base_id = request.args.get("base", type=int)
    cand_id = request.args.get("cand", type=int)
    all_runs = all_rows(db, RUN_SELECT + " WHERE r.status = 'completed' GROUP BY r.id ORDER BY r.id DESC")
    if not (base_id and cand_id):
        return render_template("compare.html", runs=all_runs, result=None)
    base, cand = get_run(base_id), get_run(cand_id)
    result = compare_runs(db, base_id, cand_id)
    only_changed = request.args.get("changed") == "1"
    rows = [r for r in result["rows"] if not only_changed or r["status"] in ("regression", "fixed")]
    return render_template("compare.html", runs=all_runs, result=result, rows=rows,
                           base=base, cand=cand, only_changed=only_changed)


# ---------------------------------------------------------------- review

@bp.route("/review")
def review_queue():
    items = all_rows(get_db(), """
        SELECT res.*, tc.case_key, tc.input, tc.expected, r.id AS run_id,
               pv.name AS prompt_name, pv.version, d.name AS dataset_name
        FROM results res
        JOIN test_cases tc ON tc.id = res.test_case_id
        JOIN runs r ON r.id = res.run_id
        JOIN prompt_versions pv ON pv.id = r.prompt_version_id
        JOIN datasets d ON d.id = r.dataset_id
        WHERE res.review_status = 'pending'
        ORDER BY res.is_regression DESC, r.id DESC, tc.case_key""")
    return render_template("review.html", items=items)


@bp.route("/results/<int:result_id>/review", methods=["POST"])
def review_result(result_id):
    db = get_db()
    action = request.form.get("action")
    status = {"approve": "approved", "reject": "rejected", "flag": "pending", "reset": "none"}.get(action)
    if status is None:
        abort(400)
    if one(db, "SELECT id FROM results WHERE id = ?", (result_id,)) is None:
        abort(404)
    db.execute(
        "UPDATE results SET review_status = ?, review_note = ?, reviewed_at = ? WHERE id = ?",
        (status, request.form.get("note", "").strip(), now() if status in ("approved", "rejected") else None,
         result_id),
    )
    db.commit()
    nxt = request.form.get("next") or url_for(".review_queue")
    if not nxt.startswith("/") or nxt.startswith("//"):  # don't redirect off-site
        nxt = url_for(".review_queue")
    return redirect(nxt)
