import sqlite3
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from grafter.cli import app
from grafter.store import (
    TRANSITIONS,
    ChangeStatus,
    InvalidTransition,
    Store,
    migrations,
)

S = ChangeStatus
runner = CliRunner()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store(tmp_path / "state" / "grafter.db") as s:
        s.migrate()
        yield s


def new_change(store: Store, run_id: str = "r1", fingerprint: str = "fp1", **kw: object) -> str:
    if store.conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone() is None:
        store.start_run(run_id, dry_run=False)
    args: dict[str, object] = {
        "repo": "api",
        "branch": "ai/tests-1",
        "base_sha": "abc123",
        "category": "tests",
        "charter_ref": "core_domains[0]",
        "fingerprint": fingerprint,
        "summary": "Add tests for parser",
        "risk_tier": 0,
    }
    args.update(kw)
    return store.add_change(run_id, **args)  # type: ignore[arg-type]


def count(store: Store, table: str) -> int:
    return int(store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_migrations_found_in_package() -> None:
    found = migrations()
    assert found[0][0] == 1 and found[0][1] == "0001_init.sql"
    assert "CREATE TABLE changes" in found[0][2]


def test_migrate_from_empty(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "dir" / "g.db"
    with Store(db) as s:
        assert s.version() == 0
        assert s.migrate() == 2
        tables = {
            r[0] for r in s.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert db.exists()
    assert {
        "schema_version",
        "runs",
        "changes",
        "change_deps",
        "change_events",
        "review_feedback",
    } <= tables


def test_migrate_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "g.db"
    with Store(db) as s:
        assert s.migrate() == 2
        assert s.migrate() == 2
        assert count(s, "schema_version") == 2
    with Store(db) as s:
        assert s.migrate() == 2
        row = s.conn.execute("SELECT version, applied_at FROM schema_version").fetchone()
    assert row[0] == 1
    assert datetime.fromisoformat(row[1]).utcoffset() is not None


def test_failed_migration_rolls_back(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    import grafter.store as mod

    latest = migrations()[-1][0]
    bad = [*migrations(), (latest + 1, "9999_bad.sql", "CREATE TABLE extra(x TEXT);\nNOT SQL;")]
    monkeypatch.setattr(mod, "migrations", lambda: bad)
    with pytest.raises(sqlite3.Error):
        store.migrate()
    assert store.version() == latest
    names = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master")}
    assert "extra" not in names


def test_pragmas(store: Store) -> None:
    c = store.conn
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_runs(store: Store) -> None:
    store.start_run("r1", dry_run=True)
    store.finish_run("r1", "ok", prs_opened=2, tokens=1234, cost_usd=0.42, note="fine")
    row = dict(store.conn.execute("SELECT * FROM runs WHERE id = 'r1'").fetchone())
    assert row["dry_run"] == 1 and row["status"] == "ok" and row["prs_opened"] == 2
    assert row["tokens"] == 1234 and row["cost_usd"] == pytest.approx(0.42)
    assert row["note"] == "fine" and row["finished_at"] >= row["started_at"]
    with pytest.raises(KeyError):
        store.finish_run("nope", "ok", 0, 0, 0.0)


def test_add_and_get_change_json_roundtrip(store: Store) -> None:
    results = {"passed": 12, "failed": 0, "suites": {"unit": [1, 2]}, "ok": True}
    cid = new_change(store, risks=["touches parser", "ünïcode"], test_results=results)
    assert len(cid) == 32
    ch = store.get_change(cid)
    assert ch["status"] == S.PROPOSED
    assert ch["risks"] == ["touches parser", "ünïcode"]
    assert ch["test_results"] == results
    assert ch["run_id"] == "r1" and ch["fingerprint"] == "fp1" and ch["risk_tier"] == 0
    assert ch["head_sha"] is None and ch["pr_number"] is None
    assert datetime.fromisoformat(ch["created_at"]).utcoffset() is not None
    ev = store.events(cid)
    assert len(ev) == 1
    assert ev[0]["from_status"] is None and ev[0]["to_status"] == "proposed"


def test_defaults_are_not_shared(store: Store) -> None:
    a = new_change(store, fingerprint="a")
    assert store.get_change(a)["risks"] == [] and store.get_change(a)["test_results"] == {}


def test_get_change_missing(store: Store) -> None:
    with pytest.raises(KeyError):
        store.get_change("missing")


def test_add_change_requires_run(store: Store) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.add_change("no-run", "api", "ai/x", "abc", "tests", "c", "fp", "s", 0)


def test_full_lifecycle_with_events(store: Store) -> None:
    cid = new_change(store)
    path = [
        S.BUILDING,
        S.TESTING,
        S.PENDING_REVIEW,
        S.REWORK,
        S.TESTING,
        S.PENDING_REVIEW,
        S.STALE,
        S.TESTING,
        S.PENDING_REVIEW,
        S.BLOCKED,
        S.PENDING_REVIEW,
        S.APPROVED,
        S.MERGED,
    ]
    for to in path:
        store.set_status(cid, to, actor="grafter", note=f"-> {to}")
    ch = store.get_change(cid)
    assert ch["status"] == "merged"
    assert ch["decided_by"] == "grafter" and ch["decided_at"] is not None
    ev = store.events(cid)
    assert [e["to_status"] for e in ev] == ["proposed", *path]
    assert [e["from_status"] for e in ev] == [None, "proposed", *path[:-1]]
    assert ev[-1]["actor"] == "grafter" and ev[-1]["note"] == "-> merged"


def test_reject_records_decision(store: Store) -> None:
    cid = new_change(store)
    for to in (S.BUILDING, S.TESTING, S.PENDING_REVIEW):
        store.set_status(cid, to, "grafter")
    store.set_status(cid, S.REJECTED, "alice", "out of scope")
    ch = store.get_change(cid)
    assert ch["status"] == "rejected" and ch["decided_by"] == "alice"


@pytest.mark.parametrize(
    ("path", "bad"),
    [
        ([], S.APPROVED),
        ([], S.MERGED),
        ([S.BUILDING, S.TESTING, S.PENDING_REVIEW], S.MERGED),
        ([S.BUILDING, S.REJECTED], S.BUILDING),
    ],
)
def test_invalid_transition_raises_and_writes_nothing(
    store: Store, path: list[ChangeStatus], bad: ChangeStatus
) -> None:
    cid = new_change(store)
    for to in path:
        store.set_status(cid, to, "grafter")
    before = store.get_change(cid)
    n_events = count(store, "change_events")
    with pytest.raises(InvalidTransition):
        store.set_status(cid, bad, "mallory")
    assert store.get_change(cid) == before
    assert count(store, "change_events") == n_events
    assert not store.conn.in_transaction


def test_set_status_unknown_change_and_value(store: Store) -> None:
    with pytest.raises(KeyError):
        store.set_status("missing", S.BUILDING, "grafter")
    cid = new_change(store)
    with pytest.raises(ValueError):
        store.set_status(cid, "bogus", "grafter")  # type: ignore[arg-type]
    store.set_status(cid, "building", "grafter")  # type: ignore[arg-type]
    assert store.get_change(cid)["status"] == "building"


def test_transition_table_covers_every_status() -> None:
    assert set(TRANSITIONS) == set(ChangeStatus)
    assert TRANSITIONS[S.MERGED] == frozenset() and TRANSITIONS[S.REJECTED] == frozenset()


def test_open_change_for(store: Store) -> None:
    assert store.open_change_for("api", "fp1") is None
    cid = new_change(store)
    found = store.open_change_for("api", "fp1")
    assert found is not None and found["id"] == cid
    assert store.open_change_for("web", "fp1") is None
    store.set_status(cid, S.BUILDING, "grafter")
    store.set_status(cid, S.REJECTED, "grafter")
    assert store.open_change_for("api", "fp1") is None


def test_update_change(store: Store) -> None:
    cid = new_change(store)
    before = store.get_change(cid)
    store.update_change(
        cid, head_sha="def456", pr_number=7, risks=["new"], test_results={"passed": 3}
    )
    ch = store.get_change(cid)
    assert ch["head_sha"] == "def456" and ch["pr_number"] == 7
    assert ch["risks"] == ["new"] and ch["test_results"] == {"passed": 3}
    assert ch["updated_at"] >= before["updated_at"]
    store.update_change(cid)  # no-op
    with pytest.raises(KeyError):
        store.update_change("missing", head_sha="x")


@pytest.mark.parametrize(
    "field", ["status", "id", "created_at", "nope", "summary = 'x', status", "head_sha; DROP"]
)
def test_update_change_rejects_unknown_column(store: Store, field: str) -> None:
    cid = new_change(store)
    before = store.get_change(cid)
    with pytest.raises(ValueError, match="not updatable"):
        store.update_change(cid, **{field: "x"})
    assert store.get_change(cid) == before


def test_deps(store: Store) -> None:
    a = new_change(store, fingerprint="a")
    b = new_change(store, fingerprint="b")
    store.add_dep(b, a)
    store.add_dep(b, a)  # idempotent
    assert store.deps(b) == [a] and store.deps(a) == []
    with pytest.raises(ValueError):
        store.add_dep(a, a)
    with pytest.raises(sqlite3.IntegrityError):
        store.add_dep(a, "missing")


def test_concurrent_reader_during_write(tmp_path: Path) -> None:
    db = tmp_path / "g.db"
    with Store(db) as writer:
        writer.migrate()
        writer.start_run("r0", dry_run=False)
        writer.conn.execute("BEGIN IMMEDIATE")
        writer.conn.execute("INSERT INTO runs(id, started_at, status) VALUES ('r1', 'now', 'x')")
        with Store(db) as reader:
            rows = [r[0] for r in reader.conn.execute("SELECT id FROM runs ORDER BY id")]
            assert rows == ["r0"]
        writer.conn.execute("COMMIT")
        with Store(db) as reader:
            assert count(reader, "runs") == 2


def test_backup(store: Store, tmp_path: Path) -> None:
    cid = new_change(store)
    store.set_status(cid, S.BUILDING, "grafter")
    dest = store.backup(tmp_path / "backups" / "copy.db")
    assert dest.exists()
    with Store(dest) as copy:
        assert copy.version() == 2
        assert copy.get_change(cid) == store.get_change(cid)
        assert copy.events(cid) == store.events(cid)


def test_close_is_idempotent(tmp_path: Path) -> None:
    s = Store(tmp_path / "g.db")
    s.close()
    s.close()
    with pytest.raises(RuntimeError, match="closed"):
        s.version()


# --- CLI ---


def write_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "grafter.yaml"
    cfg.write_text(
        "repos:\n"
        "  - {name: api, github: acme/api, language: python, test_command: pytest,"
        " test_paths: [tests/**]}\n",
        encoding="utf-8",
    )
    return cfg


def test_cli_migrate_and_backup(tmp_path: Path) -> None:
    cfg = str(write_config(tmp_path))
    db = tmp_path / "data" / "g.db"
    result = runner.invoke(app, ["db", "migrate", "--config", cfg, "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "schema version 2" in result.output and db.exists()
    dest = tmp_path / "bk" / "g.bak.db"
    result = runner.invoke(app, ["db", "backup", str(dest), "--config", cfg, "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "g.bak.db" in result.output
    with Store(dest) as copy:
        assert copy.version() == 2


def test_cli_default_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    result = runner.invoke(app, ["db", "migrate", "--config", str(write_config(tmp_path))])
    assert result.exit_code == 0, result.output
    assert (home / ".grafter" / "grafter.db").exists()


def test_cli_backup_missing_db(tmp_path: Path) -> None:
    cfg = str(write_config(tmp_path))
    result = runner.invoke(
        app,
        ["db", "backup", str(tmp_path / "out.db"), "--config", cfg, "--db", str(tmp_path / "x")],
    )
    assert result.exit_code == 1
    assert "no database" in result.output
    assert not (tmp_path / "x").exists()


def test_cli_bad_config(tmp_path: Path) -> None:
    result = runner.invoke(app, ["db", "migrate", "--config", str(tmp_path / "missing.yaml")])
    assert result.exit_code == 1
    assert "invalid config" in result.output


# --- findings (TODO 1E2) -----------------------------------------------------------------


def _f(rule: str, path: str = "requirements.txt", sev: str = "high", line: int | None = 1) -> Any:
    from grafter.findings import Finding

    return Finding("trivy", rule, sev, path, line, f"message {rule}")  # type: ignore[arg-type]


def test_upsert_findings_new_open_gone_reopen(store: Store) -> None:
    c1 = store.upsert_findings("api", "trivy", "r1", [_f("CVE-1"), _f("CVE-2")])
    assert (c1.new, c1.open, c1.gone) == (2, 2, 0)
    # Same two again, CVE-1 on another line: nothing new.
    c2 = store.upsert_findings("api", "trivy", "r2", [_f("CVE-1", line=9), _f("CVE-2")])
    assert (c2.new, c2.open, c2.gone) == (0, 2, 0)
    # CVE-2 fixed.
    c3 = store.upsert_findings("api", "trivy", "r3", [_f("CVE-1")])
    assert (c3.new, c3.open, c3.gone) == (0, 1, 1)
    gone = store.list_findings(status="gone")
    assert [g["rule_id"] for g in gone] == ["CVE-2"] and gone[0]["last_seen_run"] == "r2"
    # CVE-2 comes back: open again, first_seen kept.
    c4 = store.upsert_findings("api", "trivy", "r4", [_f("CVE-1"), _f("CVE-2")])
    assert (c4.new, c4.open, c4.gone) == (0, 2, 0)
    rows = {r["rule_id"]: r for r in store.list_findings()}
    assert rows["CVE-2"]["first_seen_run"] == "r1" and rows["CVE-2"]["last_seen_run"] == "r4"
    assert rows["CVE-1"]["line"] == 1  # latest line kept


def test_upsert_findings_is_scoped_by_repo_and_tool(store: Store) -> None:
    store.upsert_findings("api", "trivy", "r1", [_f("CVE-1")])
    store.upsert_findings("web", "trivy", "r1", [_f("CVE-1")])
    c = store.upsert_findings("api", "osv", "r1", [])
    assert (c.new, c.open, c.gone) == (0, 0, 0)
    assert len(store.list_findings()) == 2  # osv's empty scan closed nothing of trivy's


def test_list_findings_filters_and_order(store: Store) -> None:
    store.upsert_findings(
        "api", "trivy", "r1", [_f("A", sev="low"), _f("B", sev="critical"), _f("C", sev="medium")]
    )
    store.upsert_findings("web", "osv", "r1", [_f("D", sev="high")])
    assert [r["rule_id"] for r in store.list_findings()] == ["B", "D", "C", "A"]
    assert [r["rule_id"] for r in store.list_findings(min_severity="high")] == ["B", "D"]
    assert [r["rule_id"] for r in store.list_findings(repo="web")] == ["D"]
    assert [r["rule_id"] for r in store.list_findings(tool="trivy", repo="api")] == ["B", "C", "A"]
    assert store.list_findings(status="all", repo="nope") == []


def test_migrate_from_v1_keeps_data(tmp_path: Path) -> None:
    import sqlite3 as sq

    from grafter.store import migrations

    db = tmp_path / "old.db"
    conn = sq.connect(db)
    conn.execute(
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    v1 = next(sql for v, _n, sql in migrations() if v == 1)
    conn.executescript(v1)
    conn.execute("INSERT INTO schema_version VALUES (1, 'then')")
    conn.execute("INSERT INTO runs (id, started_at, status) VALUES ('old', 'then', 'ok')")
    conn.commit()
    conn.close()
    with Store(db) as s:
        assert s.migrate() == 2
        assert s.conn.execute("SELECT id FROM runs").fetchone()[0] == "old"
        assert s.list_findings() == []
