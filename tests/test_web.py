import io
import json

import pytest

from evaltrack import get_db
from evaltrack.db import one


@pytest.mark.parametrize("url", [
    "/", "/runs", "/runs/1", "/runs/2?show=regressions", "/runs/new", "/prompts",
    "/prompts/2", "/prompts/new?from=1", "/datasets", "/datasets/1", "/compare",
    "/compare?base=1&cand=2&changed=1", "/review",
])
def test_pages_render(seeded_client, url):
    assert seeded_client.get(url).status_code == 200


def test_empty_app_renders(app):
    page = app.test_client().get("/")
    assert page.status_code == 200
    assert b"No runs yet" in page.data


def test_missing_run_is_404(seeded_client):
    assert seeded_client.get("/runs/999").status_code == 404


def test_run_page_shows_regressions(seeded_client):
    html = seeded_client.get("/runs/2").get_data(as_text=True)
    assert "passed in the baseline and fail now" in html
    assert "prompt-injection" in html


def test_prompt_diff_is_shown(seeded_client):
    html = seeded_client.get("/prompts/2").get_data(as_text=True)
    assert "Changes since v1" in html
    assert "Answer in one sentence" in html


def test_review_approve_clears_queue_item(seeded_client, app):
    with app.app_context():
        pending = one(get_db(), "SELECT id FROM results WHERE review_status = 'pending' LIMIT 1")
    resp = seeded_client.post(f"/results/{pending['id']}/review",
                              data={"action": "approve", "note": "wording is fine", "next": "/review"})
    assert resp.status_code == 302
    with app.app_context():
        row = one(get_db(), "SELECT * FROM results WHERE id = ?", (pending["id"],))
    assert row["review_status"] == "approved"
    assert row["review_note"] == "wording is fine"
    assert row["reviewed_at"]


def test_review_rejects_unknown_action_and_offsite_redirect(seeded_client):
    assert seeded_client.post("/results/1/review", data={"action": "delete"}).status_code == 400
    resp = seeded_client.post("/results/1/review",
                              data={"action": "flag", "next": "//evil.example.com"})
    assert resp.location.endswith("/review")


def test_upload_dataset_and_run_it(seeded_client):
    payload = json.dumps([{"key": "q", "input": "Do you deliver to Udupi?",
                           "checks": [{"type": "contains", "value": "Udupi"}]}])
    resp = seeded_client.post("/datasets", data={
        "file": (io.BytesIO(payload.encode()), "areas.json"), "name": "areas",
    }, content_type="multipart/form-data")
    assert resp.status_code == 302

    resp = seeded_client.post("/runs/new", data={
        "prompt_version_id": 3, "dataset_id": 2, "baseline": "none", "workers": 1,
    })
    assert resp.status_code == 302
    html = seeded_client.get(resp.location).get_data(as_text=True)
    assert "areas" in html


def test_bad_upload_shows_error(seeded_client):
    resp = seeded_client.post("/datasets", data={
        "file": (io.BytesIO(b"[]"), "empty.json"),
    }, content_type="multipart/form-data", follow_redirects=True)
    assert b"no test cases found" in resp.data


def test_new_prompt_requires_input_placeholder(seeded_client):
    resp = seeded_client.post("/prompts/new", data={"name": "x", "template": "no placeholder"})
    assert b"containing {{input}}" in resp.data


def test_new_prompt_version_increments(seeded_client, app):
    seeded_client.post("/prompts/new", data={
        "name": "support-bot", "template": "{{input}}", "provider": "mock", "model": "mock-small",
    })
    with app.app_context():
        row = one(get_db(), "SELECT MAX(version) AS v FROM prompt_versions WHERE name = 'support-bot'")
    assert row["v"] == 4


def test_run_with_missing_key_shows_flash(seeded_client, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    seeded_client.post("/prompts/new", data={
        "name": "real", "template": "{{input}}", "provider": "groq", "model": "llama-3.1-8b-instant",
    })
    resp = seeded_client.post("/runs/new", data={"prompt_version_id": 4, "dataset_id": 1},
                              follow_redirects=True)
    assert b"GROQ_API_KEY is not set" in resp.data


def test_csv_export(seeded_client):
    resp = seeded_client.get("/runs/2/export.csv")
    assert resp.mimetype == "text/csv"
    assert resp.get_data(as_text=True).startswith("case_key,input,output")
