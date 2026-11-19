-- 0001: initial schema (D17). Portable SQL types only; JSON columns hold JSON text.
-- Timestamps are ISO-8601 UTC text.

CREATE TABLE runs (
    id          TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    dry_run     INTEGER NOT NULL DEFAULT 0,
    prs_opened  INTEGER NOT NULL DEFAULT 0,
    tokens      INTEGER NOT NULL DEFAULT 0,
    cost_usd    REAL NOT NULL DEFAULT 0,
    note        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE changes (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES runs(id),
    repo         TEXT NOT NULL,
    branch       TEXT NOT NULL,
    base_sha     TEXT NOT NULL,
    head_sha     TEXT,
    status       TEXT NOT NULL,
    risk_tier    INTEGER NOT NULL,
    category     TEXT NOT NULL,
    charter_ref  TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    summary      TEXT NOT NULL,
    risks        TEXT NOT NULL DEFAULT '[]',
    test_results TEXT NOT NULL DEFAULT '{}',
    group_id     TEXT,
    pr_number    INTEGER,
    decided_by   TEXT,
    decided_at   TEXT,
    merged_sha   TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE INDEX ix_changes_repo_fingerprint_status ON changes (repo, fingerprint, status);

CREATE TABLE change_deps (
    change_id  TEXT NOT NULL REFERENCES changes(id),
    depends_on TEXT NOT NULL REFERENCES changes(id),
    PRIMARY KEY (change_id, depends_on)
);

CREATE TABLE change_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    change_id   TEXT NOT NULL REFERENCES changes(id),
    from_status TEXT,
    to_status   TEXT NOT NULL,
    actor       TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    at          TEXT NOT NULL
);

CREATE INDEX ix_change_events_change ON change_events (change_id);

CREATE TABLE review_feedback (
    id        INTEGER PRIMARY KEY,
    change_id TEXT NOT NULL REFERENCES changes(id),
    author    TEXT NOT NULL,
    comment   TEXT NOT NULL,
    at        TEXT NOT NULL
);
