import json

from .datasets import import_dataset, parse_dataset
from .db import now, one
from .demo import PROMPT_VERSIONS, SAMPLE_DATASET, TEMPLATE
from .providers import MockProvider, RetryPolicy
from .runner import execute_run


def create_prompt_version(conn, name, system_prompt, template, provider="mock",
                          model="mock-small", temperature=0.0, max_tokens=300, notes=""):
    row = one(conn, "SELECT MAX(version) AS v FROM prompt_versions WHERE name = ?", (name,))
    version = (row["v"] or 0) + 1
    cur = conn.execute(
        """INSERT INTO prompt_versions (name, version, system_prompt, template, provider,
               model, temperature, max_tokens, notes, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (name, version, system_prompt, template, provider, model, temperature,
         max_tokens, notes, now()),
    )
    conn.commit()
    return cur.lastrowid


def seed_demo(conn):
    """Load the sample dataset, three prompt versions and one run each.
    v2 and v3 are compared against v1, which plays the production baseline."""
    if one(conn, "SELECT id FROM datasets WHERE name = 'tiffinly-support'"):
        return False

    raw = json.loads(SAMPLE_DATASET.read_text())
    cases = parse_dataset(SAMPLE_DATASET.read_text(), SAMPLE_DATASET.name)
    dataset_id = import_dataset(conn, raw["name"], cases, raw.get("description", ""))

    # A few simulated 503s so the attempts column has something to show.
    provider = MockProvider(failure_rate=0.15, seed=42)
    policy = RetryPolicy(sleep=lambda s: None)

    baseline = None
    for spec in PROMPT_VERSIONS:
        pv_id = create_prompt_version(conn, "support-bot", spec["system_prompt"], TEMPLATE,
                                      notes=spec["notes"])
        run_id = execute_run(conn, pv_id, dataset_id, baseline_run_id=baseline,
                             provider=provider, policy=policy, triggered_by="seed")
        baseline = baseline or run_id
    return True
