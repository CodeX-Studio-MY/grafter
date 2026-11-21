"""Grafter state store: one SQLite file on the host (D17).

Stdlib `sqlite3` in autocommit mode with explicit transactions. WAL + busy timeout so a
reader (dashboard, CLI) never blocks the nightly writer. Schema comes from numbered
`grafter.migrations/NNNN_*.sql` files; SQL stays portable (TEXT/INTEGER/REAL, JSON as text).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from types import TracebackType
from typing import Any, Self
from uuid import uuid4

from grafter.findings import SEVERITIES, Finding

BUSY_TIMEOUT_MS = 5000
_MIGRATION_NAME = re.compile(r"^(\d{4})_[A-Za-z0-9_]+\.sql$")


class ChangeStatus(StrEnum):
    PROPOSED = "proposed"
    BUILDING = "building"
    TESTING = "testing"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    MERGED = "merged"
    REJECTED = "rejected"
    REWORK = "rework"
    STALE = "stale"
    BLOCKED = "blocked"


_S = ChangeStatus

# Lifecycle from docs/branch-model.md. stale -> testing is the rebase step.
TRANSITIONS: Mapping[ChangeStatus, frozenset[ChangeStatus]] = {
    _S.PROPOSED: frozenset({_S.BUILDING}),
    _S.BUILDING: frozenset({_S.TESTING, _S.REJECTED}),
    _S.TESTING: frozenset({_S.PENDING_REVIEW, _S.REJECTED}),
    _S.PENDING_REVIEW: frozenset({_S.APPROVED, _S.REJECTED, _S.REWORK, _S.STALE, _S.BLOCKED}),
    _S.APPROVED: frozenset({_S.MERGED, _S.STALE}),
    _S.REWORK: frozenset({_S.TESTING}),
    _S.STALE: frozenset({_S.TESTING}),
    _S.BLOCKED: frozenset({_S.PENDING_REVIEW, _S.REJECTED}),
    _S.MERGED: frozenset(),
    _S.REJECTED: frozenset(),
}

CLOSED = frozenset({_S.MERGED, _S.REJECTED})
_DECISIONS = frozenset({_S.APPROVED, _S.REJECTED})

_JSON_COLUMNS = frozenset({"risks", "test_results"})
# Columns `update_change` may set. Status changes go through `set_status` only.
UPDATABLE = frozenset(
    {
        "branch",
        "base_sha",
        "head_sha",
        "risk_tier",
        "category",
        "charter_ref",
        "summary",
        "risks",
        "test_results",
        "group_id",
        "pr_number",
        "decided_by",
        "decided_at",
        "merged_sha",
    }
)


class InvalidTransition(ValueError):
    """A status change not allowed by the lifecycle."""


@dataclass(frozen=True)
class FindingCounts:
    new: int
    open: int
    gone: int


def now() -> str:
    """Current time, ISO-8601 UTC."""
    return datetime.now(UTC).isoformat()


def migrations() -> list[tuple[int, str, str]]:
    """(version, filename, sql) for every packaged migration, in version order."""
    found = []
    for entry in files("grafter.migrations").iterdir():
        m = _MIGRATION_NAME.match(entry.name)
        if m:
            found.append((int(m.group(1)), entry.name, entry.read_text(encoding="utf-8")))
    found.sort()
    return found


class Store:
    """Grafter's SQLite state. Single writer (the Grafter process); readers welcome."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = sqlite3.connect(
            self.path, isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        self._conn.execute("PRAGMA foreign_keys=ON")

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("store is closed")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    # --- schema ---

    def version(self) -> int:
        """Current schema version (0 = empty database)."""
        exists = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
        ).fetchone()
        if exists is None:
            return 0
        row = self.conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return int(row[0] or 0)

    def migrate(self) -> int:
        """Apply pending migrations in order, each in one transaction. Returns the version."""
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version"
            " (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        current = self.version()
        for version, _name, sql in migrations():
            if version <= current:
                continue
            # executescript cannot bind parameters; both values are generated here.
            script = (
                f"BEGIN IMMEDIATE;\n{sql}\n;"
                f"INSERT INTO schema_version (version, applied_at) VALUES ({version:d}, '{now()}');"
                "\nCOMMIT;"
            )
            try:
                self.conn.executescript(script)
            except BaseException:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise
            current = version
        return current

    # --- runs ---

    def start_run(self, run_id: str, dry_run: bool) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO runs (id, started_at, status, dry_run) VALUES (?, ?, 'running', ?)",
                (run_id, now(), int(dry_run)),
            )

    def finish_run(
        self,
        run_id: str,
        status: str,
        prs_opened: int,
        tokens: int,
        cost_usd: float,
        note: str = "",
    ) -> None:
        with self._tx() as c:
            cur = c.execute(
                "UPDATE runs SET finished_at = ?, status = ?, prs_opened = ?, tokens = ?,"
                " cost_usd = ?, note = ? WHERE id = ?",
                (now(), status, prs_opened, tokens, cost_usd, note, run_id),
            )
            if cur.rowcount == 0:
                raise KeyError(run_id)

    # --- changes ---

    def add_change(
        self,
        run_id: str,
        repo: str,
        branch: str,
        base_sha: str,
        category: str,
        charter_ref: str,
        fingerprint: str,
        summary: str,
        risk_tier: int,
        risks: list[str] | None = None,
        test_results: dict[str, Any] | None = None,
    ) -> str:
        """Record a new change in status `proposed`. Returns its id."""
        change_id = uuid4().hex
        ts = now()
        with self._tx() as c:
            c.execute(
                "INSERT INTO changes (id, run_id, repo, branch, base_sha, status, risk_tier,"
                " category, charter_ref, fingerprint, summary, risks, test_results,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    change_id,
                    run_id,
                    repo,
                    branch,
                    base_sha,
                    ChangeStatus.PROPOSED.value,
                    risk_tier,
                    category,
                    charter_ref,
                    fingerprint,
                    summary,
                    json.dumps(risks or []),
                    json.dumps(test_results or {}),
                    ts,
                    ts,
                ),
            )
            self._event(c, change_id, None, ChangeStatus.PROPOSED, "grafter", "", ts)
        return change_id

    def get_change(self, change_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM changes WHERE id = ?", (change_id,)).fetchone()
        if row is None:
            raise KeyError(change_id)
        return _decode(row)

    def open_change_for(self, repo: str, fingerprint: str) -> dict[str, Any] | None:
        """The not-yet-closed change for this finding, if any (dedupe across nights)."""
        row = self.conn.execute(
            "SELECT * FROM changes WHERE repo = ? AND fingerprint = ? AND status NOT IN (?, ?)"
            " ORDER BY created_at DESC LIMIT 1",
            (repo, fingerprint, *sorted(CLOSED)),
        ).fetchone()
        return None if row is None else _decode(row)

    def set_status(self, change_id: str, to: ChangeStatus, actor: str, note: str = "") -> None:
        """Move a change along the lifecycle; writes a change_events row atomically."""
        target = ChangeStatus(to)
        ts = now()
        with self._tx() as c:
            row = c.execute("SELECT status FROM changes WHERE id = ?", (change_id,)).fetchone()
            if row is None:
                raise KeyError(change_id)
            current = ChangeStatus(row[0])
            if target not in TRANSITIONS[current]:
                raise InvalidTransition(f"{change_id}: {current} -> {target} not allowed")
            if target in _DECISIONS:
                c.execute(
                    "UPDATE changes SET status = ?, decided_by = ?, decided_at = ?,"
                    " updated_at = ? WHERE id = ?",
                    (target.value, actor, ts, ts, change_id),
                )
            else:
                c.execute(
                    "UPDATE changes SET status = ?, updated_at = ? WHERE id = ?",
                    (target.value, ts, change_id),
                )
            self._event(c, change_id, current, target, actor, note, ts)

    def update_change(self, change_id: str, **fields: Any) -> None:
        """Set allow-listed columns (see `UPDATABLE`). JSON columns take Python values."""
        unknown = sorted(set(fields) - UPDATABLE)
        if unknown:
            raise ValueError(f"not updatable: {unknown}")
        # Column names come from the UPDATABLE constant, never from caller input.
        cols = sorted(UPDATABLE & set(fields))
        if not cols:
            return
        values = [json.dumps(fields[k]) if k in _JSON_COLUMNS else fields[k] for k in cols]
        assignments = ", ".join(f"{k} = ?" for k in cols)
        with self._tx() as c:
            cur = c.execute(
                f"UPDATE changes SET {assignments}, updated_at = ? WHERE id = ?",
                (*values, now(), change_id),
            )
            if cur.rowcount == 0:
                raise KeyError(change_id)

    def add_dep(self, change_id: str, depends_on: str) -> None:
        """`change_id` cannot be approved before `depends_on`. Idempotent."""
        if change_id == depends_on:
            raise ValueError("a change cannot depend on itself")
        with self._tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO change_deps (change_id, depends_on) VALUES (?, ?)",
                (change_id, depends_on),
            )

    def deps(self, change_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT depends_on FROM change_deps WHERE change_id = ? ORDER BY depends_on",
            (change_id,),
        )
        return [r[0] for r in rows]

    def events(self, change_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM change_events WHERE change_id = ? ORDER BY id", (change_id,)
        )
        return [dict(r) for r in rows]

    # --- maintenance ---

    # --- findings (1E2) ---

    def upsert_findings(
        self, repo: str, tool: str, run_id: str, findings: Sequence[Finding]
    ) -> FindingCounts:
        """Record one full scan of `repo` by `tool`: new findings are added, seen ones
        refreshed (and reopened if gone), open ones missing from this scan become `gone`."""
        ts = now()
        seen = {f.fingerprint: f for f in findings}
        with self._tx() as c:
            rows = c.execute(
                "SELECT fingerprint, status FROM findings WHERE repo = ? AND tool = ?",
                (repo, tool),
            ).fetchall()
            known = {r["fingerprint"]: r["status"] for r in rows}
            new = 0
            for fp, f in seen.items():
                if fp in known:
                    c.execute(
                        "UPDATE findings SET status = 'open', severity = ?, line = ?, message = ?,"
                        " last_seen_run = ?, last_seen_at = ?"
                        " WHERE repo = ? AND tool = ? AND fingerprint = ?",
                        (f.severity, f.line, f.message, run_id, ts, repo, tool, fp),
                    )
                else:
                    new += 1
                    c.execute(
                        "INSERT INTO findings (id, repo, tool, fingerprint, rule_id, severity,"
                        " path, line, message, status, first_seen_run, last_seen_run,"
                        " first_seen_at, last_seen_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)",
                        (
                            uuid4().hex,
                            repo,
                            tool,
                            fp,
                            f.rule_id,
                            f.severity,
                            f.path,
                            f.line,
                            f.message,
                            run_id,
                            run_id,
                            ts,
                            ts,
                        ),
                    )
            gone = [fp for fp, status in known.items() if status == "open" and fp not in seen]
            for fp in gone:
                c.execute(
                    "UPDATE findings SET status = 'gone' WHERE repo = ? AND tool = ?"
                    " AND fingerprint = ?",
                    (repo, tool, fp),
                )
        return FindingCounts(new=new, open=len(seen), gone=len(gone))

    def list_findings(
        self,
        status: str = "open",
        repo: str | None = None,
        tool: str | None = None,
        min_severity: str = "low",
    ) -> list[dict[str, Any]]:
        """Findings, most severe first. `status`: open, gone or all."""
        where, args = [], []
        if status != "all":
            where.append("status = ?")
            args.append(status)
        if repo:
            where.append("repo = ?")
            args.append(repo)
        if tool:
            where.append("tool = ?")
            args.append(tool)
        order: tuple[str, ...] = SEVERITIES
        allowed = order[order.index(min_severity) :]
        where.append(f"severity IN ({', '.join('?' for _ in allowed)})")
        args.extend(allowed)
        rank = " ".join(f"WHEN '{s}' THEN {i}" for i, s in enumerate(SEVERITIES))
        sql = (
            "SELECT * FROM findings WHERE "
            + " AND ".join(where)
            + f" ORDER BY CASE severity {rank} END DESC, repo, tool, path, rule_id"
        )
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def backup(self, dest: Path) -> Path:
        """Consistent online copy via the SQLite backup API."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(dest)
        try:
            self.conn.backup(target)
        finally:
            target.close()
        return dest

    @staticmethod
    def _event(
        c: sqlite3.Connection,
        change_id: str,
        from_status: ChangeStatus | None,
        to_status: ChangeStatus,
        actor: str,
        note: str,
        at: str,
    ) -> None:
        c.execute(
            "INSERT INTO change_events (change_id, from_status, to_status, actor, note, at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                change_id,
                None if from_status is None else from_status.value,
                to_status.value,
                actor,
                note,
                at,
            ),
        )


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for k in _JSON_COLUMNS:
        d[k] = json.loads(d[k])
    return d
