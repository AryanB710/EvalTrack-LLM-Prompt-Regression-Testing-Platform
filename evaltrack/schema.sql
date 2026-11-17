CREATE TABLE IF NOT EXISTS datasets (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS test_cases (
    id         INTEGER PRIMARY KEY,
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    case_key   TEXT NOT NULL,
    input      TEXT NOT NULL,
    expected   TEXT DEFAULT '',
    checks     TEXT NOT NULL DEFAULT '[]',   -- JSON list of check specs
    tags       TEXT DEFAULT '',
    UNIQUE (dataset_id, case_key)
);

CREATE TABLE IF NOT EXISTS prompt_versions (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    version       INTEGER NOT NULL,
    system_prompt TEXT DEFAULT '',
    template      TEXT NOT NULL,
    provider      TEXT NOT NULL DEFAULT 'mock',
    model         TEXT NOT NULL,
    temperature   REAL NOT NULL DEFAULT 0,
    max_tokens    INTEGER NOT NULL DEFAULT 300,
    notes         TEXT DEFAULT '',
    created_at    TEXT NOT NULL,
    UNIQUE (name, version)
);

CREATE TABLE IF NOT EXISTS runs (
    id                INTEGER PRIMARY KEY,
    prompt_version_id INTEGER NOT NULL REFERENCES prompt_versions(id),
    dataset_id        INTEGER NOT NULL REFERENCES datasets(id),
    baseline_run_id   INTEGER REFERENCES runs(id),
    status            TEXT NOT NULL,          -- running | completed | failed
    error             TEXT,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    total             INTEGER DEFAULT 0,
    passed            INTEGER DEFAULT 0,
    failed            INTEGER DEFAULT 0,
    errored           INTEGER DEFAULT 0,
    regressions       INTEGER DEFAULT 0,
    fixes             INTEGER DEFAULT 0,
    avg_latency_ms    REAL,
    p95_latency_ms    REAL,
    prompt_tokens     INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    cost_usd          REAL DEFAULT 0,
    triggered_by      TEXT DEFAULT 'web'
);

CREATE TABLE IF NOT EXISTS results (
    id                INTEGER PRIMARY KEY,
    run_id            INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    test_case_id      INTEGER NOT NULL REFERENCES test_cases(id),
    output            TEXT,
    error             TEXT,
    attempts          INTEGER DEFAULT 1,
    latency_ms        REAL,
    prompt_tokens     INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    cost_usd          REAL DEFAULT 0,
    passed            INTEGER NOT NULL DEFAULT 0,  -- automated verdict
    check_results     TEXT NOT NULL DEFAULT '[]',
    is_regression     INTEGER NOT NULL DEFAULT 0,
    review_status     TEXT NOT NULL DEFAULT 'none', -- none | pending | approved | rejected
    review_note       TEXT DEFAULT '',
    reviewed_at       TEXT
);

CREATE INDEX IF NOT EXISTS idx_results_run ON results(run_id);
CREATE INDEX IF NOT EXISTS idx_results_review ON results(review_status);
CREATE INDEX IF NOT EXISTS idx_runs_dataset ON runs(dataset_id, status);
