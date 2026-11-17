# EvalTrack

Regression testing for LLM prompts. You keep a dataset of test inputs with checks
attached, run each prompt version against it, and EvalTrack tells you which cases
broke, what it costs, and how fast it is, before the new prompt goes live.

I built this after running into a problem many times: you change a prompt to fix one
answer and three other answers quietly get worse. Unit tests catch this for normal code.
For prompts, people mostly eyeball a few outputs. EvalTrack makes that repeatable.

![Run page](docs/run.png)

## What it does

- **Datasets.** Reusable sets of test cases, imported from JSON or CSV. Each case has an
  input, an optional reference answer, tags, and a list of checks.
- **Prompt versions.** Prompts are never edited in place. Every change creates a new
  version, so every run points at the exact text it used. The prompt page shows a diff
  against the previous version.
- **Runs.** Runs every case through the model in parallel, scores the outputs and records
  latency, token usage and cost for each case.
- **Regression detection.** Each run can be compared with a baseline run. A case that
  passed in the baseline and fails now is flagged as a regression; the reverse is
  counted as a fix.
- **Human review.** Failed and regressed outputs go into a review queue. A reviewer can
  approve an output (the check was too strict) or confirm the failure. Their decision
  overrides the automated verdict everywhere: pass rates, comparisons and later regressions.
- **Dashboards.** Quality, latency (avg and p95) and cost per 1,000 cases for each prompt
  version, plus a per-tag breakdown and side-by-side output comparison.
- **Reliability.** Retries with exponential backoff and jitter on 429s, 5xx errors and
  timeouts, and respects `Retry-After`. Permanent errors (bad key, bad request) fail fast.
  One failed case is recorded as an error and never kills the whole run.
- **CI gate.** `python -m evaltrack run ... --fail-on-regression` exits with code 1 when
  quality drops, so a prompt change can block a merge just like a failing test.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python -m evaltrack seed      # demo dataset, 3 prompt versions, 3 runs
python run.py                 # http://127.0.0.1:5000
```

The demo runs fully offline with a mock provider, so no API key is needed.

## The demo story

The demo is a support bot for Tiffinly, a made-up food delivery app, with 12 test cases.

| Version | Change | Result |
|---|---|---|
| v1 | Friendly, complete answers, has a prompt-injection guardrail | 12/12, the baseline |
| v2 | "Be concise. Answer in one sentence." Guardrail line trimmed to save tokens | 56% fewer tokens, 49% faster, but **4 regressions** |
| v3 | Two sentences allowed, guardrail restored | 12/12, still cheaper than v1 |

v2's regressions are what you'd see in real life: the refund answer loses the
"5-7 business days" detail, the allergy answer loses "we cannot guarantee", and with the
guardrail gone the bot hands out a discount code to a prompt-injection attempt. The
dashboard shows v2 as the cheapest and fastest version, which is exactly why you need the
regression check next to the cost numbers.

The mock provider reacts to the system prompt roughly the way a real model would, so the
demo shows real regressions rather than hard-coded ones. See `evaltrack/demo.py`.

## Using a real model

Any OpenAI-compatible `/chat/completions` API works. Groq and OpenAI are built in:

```bash
export GROQ_API_KEY=...        # or OPENAI_API_KEY
```

Then create a prompt version with provider `groq` and a model such as
`llama-3.1-8b-instant`. Prices used for cost tracking live in `evaltrack/pricing.py`; check
them against the provider's current pricing page.

## Dataset format

```json
{
  "name": "tiffinly-support",
  "cases": [
    {
      "key": "refund-missing-item",
      "input": "One item was missing from my order. How do I get a refund?",
      "expected": "Refunds ... are processed within 5-7 business days ...",
      "checks": [
        {"type": "contains", "value": "5-7 business days"},
        {"type": "similarity", "min": 0.6},
        {"type": "max_words", "value": 60}
      ],
      "tags": ["billing"]
    }
  ]
}
```

CSV also works, with columns `key,input,expected,checks,tags` (`checks` holds a JSON string).
Checks are validated at import time, so a typo shows up immediately instead of as a
confusing failed run.

| Check | Passes when |
|---|---|
| `contains` / `not_contains` | substring present / absent (case-insensitive unless `"case_sensitive": true`) |
| `contains_any` / `contains_all` | any / all of `values` present |
| `regex` | `pattern` matches |
| `max_words`, `min_words`, `max_chars` | length limits |
| `exact` | output equals `value`, or the reference answer |
| `similarity` | difflib ratio against the reference answer is at least `min` |
| `json_valid`, `json_has_keys` | output is JSON / has the given keys |

Adding a check means writing one function and decorating it with `@register` in
`evaltrack/checks.py`.

## CLI and CI

```bash
python -m evaltrack import path/to/cases.json --name my-dataset
python -m evaltrack run --prompt support-bot --dataset tiffinly-support \
    --baseline auto --fail-on-regression --min-pass-rate 0.9
python -m evaltrack runs
```

```
Running support-bot v2 on tiffinly-support (baseline run #1) ...
run #4: 8/12 passed (67%), 0 errors, avg 286 ms, p95 348 ms, $0.000036
  REGRESSION  late-order
  REGRESSION  peanut-allergy
  REGRESSION  prompt-injection
  REGRESSION  refund-missing-item
gate failed: 4 regression(s)
```

Exit codes: `0` passed, `1` gate failed, `2` error. `.github/workflows/ci.yml` runs the
tests and the gate on every push.

## How it's put together

```
evaltrack/
  providers.py   OpenAI-compatible client, mock provider, retry policy
  checks.py      check registry and validation
  runner.py      runs a prompt version over a dataset, stores results and aggregates
  compare.py     case-by-case diff between two runs, baseline suggestion
  datasets.py    JSON/CSV parsing and import
  routes.py      Flask views
  cli.py         command line and CI gate
  schema.sql     SQLite schema
```

A few decisions worth explaining:

- **Runs are compared by `case_key`, not database id.** Re-importing a dataset doesn't
  break comparisons with older runs.
- **Human review overrides the automated verdict.** String checks are crude. Some
  "failures" are fine answers worded differently, and some passes are wrong. The reviewer
  gets the final say, and regressions are computed from final verdicts.
- **Latency is measured on the successful attempt only.** Backoff time is tracked as an
  attempt count instead, so a rate-limited run doesn't make the model look slow.
- **Threads for parallel calls, one thread for SQLite.** API calls are I/O bound, so a
  thread pool is enough. Workers only call the model; the main thread does all the writes.
- **The provider is resolved before the run row is created.** A missing API key gives a
  clear error instead of leaving a half-finished run in the history.
- **Prompt templates use `{{input}}` with plain string replacement**, not `str.format`, so
  prompts can include JSON examples with curly braces.
- **Charts are plain HTML/CSS.** No JS chart library, so the dashboard works offline.

## Tests

```bash
pytest -q
```

88 tests covering the check library, retry and backoff behaviour (with a fake HTTP
session that plays back 429s, 503s and timeouts), the runner, regression and fix
detection, review overrides, dataset import and validation, the CLI exit codes and every
web page.

## Limitations and what I'd add next

- String checks can't judge meaning. Next step: an LLM-as-judge check with its own
  rubric, plus embedding similarity with sentence-transformers.
- Runs execute inside the web request, which is fine for datasets of a few hundred cases.
  Bigger datasets would need a background worker.
- Each case runs once. Running each case several times would show how stable the answers
  are at temperature > 0.
- SQLite with a single user. Multi-user review would need authentication and Postgres.

## Screenshots

| Dashboard | Side-by-side compare |
|---|---|
| ![](docs/dashboard.png) | ![](docs/compare.png) |
| **Review queue** | **Prompt diff** |
| ![](docs/review.png) | ![](docs/prompt_diff.png) |
