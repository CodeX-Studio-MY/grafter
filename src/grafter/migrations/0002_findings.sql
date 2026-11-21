-- 0002: scanner findings (TODO 1E2). Candidate tasks only; nothing acts on them in phase 1.
-- One row per (repo, tool, fingerprint); the fingerprint ignores the line number.

CREATE TABLE findings (
    id              TEXT PRIMARY KEY,
    repo            TEXT NOT NULL,
    tool            TEXT NOT NULL,
    fingerprint     TEXT NOT NULL,
    rule_id         TEXT NOT NULL,
    severity        TEXT NOT NULL CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    path            TEXT NOT NULL,
    line            INTEGER,
    message         TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('open', 'gone')),
    first_seen_run  TEXT NOT NULL,
    last_seen_run   TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    UNIQUE (repo, tool, fingerprint)
);

CREATE INDEX findings_by_status ON findings (status, repo, tool);
