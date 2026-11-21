"""Offline end-to-end tests of one nightly run (TODO 1D3): real git against a bare origin,
real pytest on the sandbox template, FakeEngine for the agent, a fake GitHub in-process."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from conftest import GITHUB_REPO, CachingRunner, bare_refs, git

from grafter.charter import Charter
from grafter.config import Config
from grafter.engines.base import AgentEngine, RunResult, Task
from grafter.engines.fake import FakeEngine
from grafter.engines.noop import NoopEngine
from grafter.finder import FoundTask
from grafter.github import GitHubError
from grafter.gitops import GitRepo
from grafter.lock import KillSwitchEngaged, LockHeld, RunLock
from grafter.pipeline import (
    STOP_BUDGET,
    STOP_DONE,
    STOP_KILL,
    STOP_MAX_PRS,
    RunDeps,
    RunSummary,
    authed,
    run_night,
    strip_credentials,
)
from grafter.prbody import marker
from grafter.runner import CmdResult
from grafter.store import Store

TRACE = "src/acme_harvest/traceability.py"
LOTS = "src/acme_harvest/lots.py"
GRADING = "src/acme_harvest/grading.py"

TRACE_TEST = """\
from datetime import date

import pytest

from acme_harvest.lots import Lot
from acme_harvest.traceability import BrokenChainError, chain, origin

D = date(2026, 11, 18)


def lot(tid: str, parent: str | None = None) -> Lot:
    return Lot(tid, "Green Valley", "mango", 10.0, D, parent)


def test_chain_walks_to_origin() -> None:
    lots = {"a": lot("a"), "a.1": lot("a.1", "a")}
    assert [x.trace_id for x in chain(lots, "a.1")] == ["a.1", "a"]
    assert origin(lots, "a.1").trace_id == "a"


def test_missing_parent_breaks_chain() -> None:
    with pytest.raises(BrokenChainError):
        chain({"a.1": lot("a.1", "a")}, "a.1")


def test_loop_breaks_chain() -> None:
    with pytest.raises(BrokenChainError):
        chain({"a": lot("a", "b"), "b": lot("b", "a")}, "a")
"""

LOTS_TEST = """\
from datetime import date

import pytest

from acme_harvest.lots import IntakeError, intake, split

D = date(2026, 11, 18)


def test_split_keeps_parent_link() -> None:
    parent = intake("Green Valley", "mango", 10.0, D, 1, D)
    a, b = split(parent, [4.0, 6.0])
    assert a.parent == parent.trace_id
    assert b.trace_id == parent.trace_id + ".2"


def test_split_rejects_bad_weights() -> None:
    parent = intake("Green Valley", "mango", 10.0, D, 1, D)
    with pytest.raises(IntakeError):
        split(parent, [10.0])
    with pytest.raises(IntakeError):
        split(parent, [5.0, 4.0])
"""

GRADING_TEST = """\
import pytest

from acme_harvest.grading import Grade, grade


def test_middle_grades() -> None:
    assert grade(defect_pct=10, brix=12) == Grade.B
    assert grade(defect_pct=20, brix=12) == Grade.C


def test_bad_defect_share() -> None:
    with pytest.raises(ValueError):
        grade(defect_pct=101, brix=12)
"""

GOOD = {
    TRACE: {"tests/test_traceability.py": TRACE_TEST},
    LOTS: {"tests/test_more_lots.py": LOTS_TEST},
    GRADING: {"tests/test_more_grading.py": GRADING_TEST},
}

SKIPPING_TEST = TRACE_TEST + '\n\n@pytest.mark.skip("later")\ndef test_later() -> None:\n    pass\n'


class FakeGitHub:
    """In-process stand-in for `GitHubClient` (create_pr / ensure_labels / find_open_pr)."""

    def __init__(self) -> None:
        self.pulls: list[dict[str, Any]] = []
        self.labels: set[str] = set()
        self.fail_create: Exception | None = None
        self.comments: list[tuple[str, int, str]] = []
        self.fail_comment: Exception | None = None

    def ensure_labels(self, repo: str, labels: dict[str, str]) -> list[str]:
        assert repo == GITHUB_REPO
        new = sorted(set(labels) - self.labels)
        self.labels |= set(labels)
        return new

    def create_pr(
        self, repo: str, head: str, base: str, title: str, body: str, labels: list[str]
    ) -> dict[str, Any]:
        if self.fail_create is not None:
            raise self.fail_create
        n = len(self.pulls) + 1
        pr = {
            "number": n,
            "repo": repo,
            "head": head,
            "base": base,
            "title": title,
            "body": body,
            "labels": labels,
            "html_url": f"https://github.example/{repo}/pull/{n}",
        }
        self.pulls.append(pr)
        return pr

    def find_open_pr(self, repo: str, fingerprint: str) -> dict[str, Any] | None:
        return next((p for p in self.pulls if marker(fingerprint) in p["body"]), None)

    def ensure_report_issue(self, repo: str) -> int:
        assert repo == GITHUB_REPO
        return 7

    def comment(self, repo: str, number: int, body: str) -> str:
        if self.fail_comment is not None:
            raise self.fail_comment
        self.comments.append((repo, number, body))
        return f"https://github.example/{repo}/issues/{number}#c{len(self.comments)}"


class Engines:
    """Engine factory: per-target files, else no-op. Records the targets it was asked for."""

    def __init__(self, files: Mapping[str, Mapping[str, str]], **kw: Any) -> None:
        self.files, self.kw = files, kw
        self.targets: list[str] = []

    def __call__(self, found: FoundTask) -> AgentEngine:
        self.targets.append(found.target)
        if found.target in self.files:
            return FakeEngine(
                self.files[found.target], summary=f"Add tests for {found.target}", **self.kw
            )
        return NoopEngine()


class Boom:
    name = "boom"

    def run(self, task: Task, workspace: Path, charter: Charter) -> RunResult:
        raise RuntimeError(
            "agent crashed: https://x-access-token:ghs_SECRETSECRET1234@github.com/x"
        )


def deps_for(
    origin: Path,
    engine: Callable[[FoundTask], AgentEngine],
    github: FakeGitHub | None = None,
    runner: Any = None,
) -> RunDeps:
    return RunDeps(
        engine=engine,
        runner=runner or CachingRunner(),
        github=github,
        origin_url=lambda repo: str(origin),
        environ={},
        today=lambda: date(2026, 11, 20),
    )


def changes(cfg: Config) -> list[dict[str, Any]]:
    with Store(cfg.paths().db_file) as s:
        return [dict(r) for r in s.conn.execute("SELECT * FROM changes ORDER BY created_at")]


def run_row(cfg: Config, run_id: str) -> dict[str, Any]:
    with Store(cfg.paths().db_file) as s:
        return dict(s.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())


def events(cfg: Config, summary: RunSummary) -> list[str]:
    import json

    path = cfg.paths().runs_dir / summary.run_id / "run.jsonl"
    return [json.loads(line)["kind"] for line in path.read_text(encoding="utf-8").splitlines()]


# --- happy path + idempotency ----------------------------------------------------------


def test_good_change_opens_one_pr(make_cfg: Callable[..., Config], sandbox_origin: Path) -> None:
    cfg = make_cfg(limits={"max_prs_per_night": 1}, git_author_name="Grafter Bot")
    gh = FakeGitHub()
    main_before = bare_refs(sandbox_origin)["refs/heads/main"]

    s = run_night(cfg, deps_for(sandbox_origin, Engines(GOOD), gh), dry_run=False)

    assert s.rejected == [] and s.errors == []
    assert len(s.prs) == 1
    pr = s.prs[0]
    assert pr["target"] == TRACE and pr["number"] == 1 and pr["dry_run"] is False
    assert pr["url"] == f"https://github.example/{GITHUB_REPO}/pull/1"
    assert s.stopped_reason == STOP_MAX_PRS
    branch = f"ai/test-{pr['fingerprint']}"
    assert pr["branch"] == branch

    refs = bare_refs(sandbox_origin)
    assert refs["refs/heads/main"] == main_before
    assert f"refs/heads/{branch}" in refs
    assert set(refs) == {"refs/heads/main", f"refs/heads/{branch}"}
    files = git(sandbox_origin, "diff", "--name-only", "main", branch)
    assert files == "tests/test_traceability.py"
    author = git(sandbox_origin, "log", "-1", "--format=%an <%ae>", branch)
    assert author == "Grafter Bot <grafter-bot@users.noreply.github.com>"

    (created,) = gh.pulls
    assert created["head"] == branch and created["base"] == "main"
    assert marker(pr["fingerprint"]) in created["body"]
    assert created["labels"] == ["grafter", "tier-1", "type-test"]
    assert {"grafter", "tier-1", "type-test"} <= gh.labels

    (change,) = changes(cfg)
    assert change["status"] == "pending_review"
    assert change["pr_number"] == 1
    assert change["head_sha"] == refs[f"refs/heads/{branch}"]
    assert change["fingerprint"] == pr["fingerprint"]
    assert run_row(cfg, s.run_id)["prs_opened"] == 1
    assert run_row(cfg, s.run_id)["status"] == "ok"

    report = Path(s.report_path)
    assert report == cfg.paths().runs_dir / s.run_id / "report.md"
    md = report.read_text(encoding="utf-8")
    assert f"[#1](https://github.example/{GITHUB_REPO}/pull/1)" in md
    assert "- PRs opened: 1 (limit 1)" in md

    kinds = events(cfg, s)
    assert kinds[0] == "run_started" and kinds[-1] == "run_finished"
    assert "pr_opened" in kinds and "gates" in kinds and "guard" in kinds
    # The work clone's origin never keeps credentials.
    assert "x-access-token" not in (cfg.paths().work_dir / "api/.git/config").read_text("utf-8")

    # Second night: the open change is skipped, no new PR, nothing pushed.
    only_trace = Engines({TRACE: GOOD[TRACE]})
    s2 = run_night(cfg, deps_for(sandbox_origin, only_trace, gh), dry_run=False)
    assert s2.prs == []
    assert [x["target"] for x in s2.skipped][:1] == [TRACE]
    assert s2.skipped[0]["reason"].startswith("open change")
    assert TRACE not in only_trace.targets
    assert len(gh.pulls) == 1
    assert bare_refs(sandbox_origin) == refs


def test_open_pr_or_origin_branch_skips_even_without_db(
    make_cfg: Callable[..., Config], sandbox_origin: Path, tmp_path: Path
) -> None:
    cfg = make_cfg(limits={"max_prs_per_night": 1})
    gh = FakeGitHub()
    first = run_night(cfg, deps_for(sandbox_origin, Engines(GOOD), gh), dry_run=False)
    assert len(first.prs) == 1

    # Fresh state DB, same GitHub: the PR marker finds the open PR.
    cfg2 = cfg.model_copy(update={"home": str(tmp_path / "home2")})
    s = run_night(cfg2, deps_for(sandbox_origin, Engines(GOOD), gh), dry_run=False)
    assert s.skipped[0]["target"] == TRACE
    assert s.skipped[0]["reason"] == "open PR #1"

    # Fresh DB and no PR (e.g. closed): the branch left on origin blocks a retry.
    cfg3 = cfg.model_copy(update={"home": str(tmp_path / "home3")})
    s = run_night(cfg3, deps_for(sandbox_origin, Engines(GOOD), FakeGitHub()), dry_run=False)
    assert s.skipped[0]["target"] == TRACE
    assert "already on origin" in s.skipped[0]["reason"]


# --- rejections ------------------------------------------------------------------------


def test_protected_file_rejected_nothing_pushed(
    make_cfg: Callable[..., Config], sandbox_origin: Path
) -> None:
    cfg = make_cfg(limits={"max_prs_per_night": 1})
    refs = bare_refs(sandbox_origin)
    bad = {TRACE: {"src/acme_harvest/invoices.py": "# rewritten\n"}}
    gh = FakeGitHub()
    s = run_night(cfg, deps_for(sandbox_origin, Engines(bad), gh), dry_run=False)
    rej = next(r for r in s.rejected if r["target"] == TRACE)
    assert any("protected" in r for r in rej["reasons"])
    assert "touches protected paths" in Path(s.report_path).read_text(encoding="utf-8")
    assert s.prs == [] and gh.pulls == []
    assert bare_refs(sandbox_origin) == refs
    statuses = {c["fingerprint"]: (c["status"], c["decided_by"]) for c in changes(cfg)}
    assert statuses[rej["fingerprint"]] == ("rejected", "grafter")


def test_skipping_test_rejected(make_cfg: Callable[..., Config], sandbox_origin: Path) -> None:
    cfg = make_cfg(limits={"max_prs_per_night": 1})
    bad = {TRACE: {"tests/test_traceability.py": SKIPPING_TEST}}
    s = run_night(cfg, deps_for(sandbox_origin, Engines(bad), FakeGitHub()), dry_run=False)
    rej = next(r for r in s.rejected if r["target"] == TRACE)
    assert "skips tests" in rej["reasons"]


def test_noop_engine_rejected_with_no_changes(
    make_cfg: Callable[..., Config], sandbox_origin: Path
) -> None:
    cfg = make_cfg()
    s = run_night(cfg, deps_for(sandbox_origin, Engines({})), dry_run=True)
    assert [r["target"] for r in s.rejected] == [TRACE, LOTS, GRADING]
    assert s.rejected[0]["reasons"] == [
        "engine reported failure: noop engine: no changes",
        "engine made no changes",
    ]
    assert s.stopped_reason == STOP_DONE
    assert {c["status"] for c in changes(cfg)} == {"rejected"}


def test_failing_gate_rejected(make_cfg: Callable[..., Config], sandbox_origin: Path) -> None:
    cfg = make_cfg(limits={"max_prs_per_night": 1})
    failing = {TRACE: {"tests/test_traceability.py": "def test_x() -> None:\n    assert False\n"}}
    s = run_night(cfg, deps_for(sandbox_origin, Engines(failing), FakeGitHub()), dry_run=False)
    rej = next(r for r in s.rejected if r["target"] == TRACE)
    assert rej["reasons"][0].startswith("tests failed")
    change = next(c for c in changes(cfg) if c["fingerprint"] == rej["fingerprint"])
    assert change["status"] == "rejected"
    assert '"passed": false' in change["test_results"]


def test_symlink_rejected(make_cfg: Callable[..., Config], sandbox_origin: Path) -> None:
    class LinkEngine:
        name = "link"

        def run(self, task: Task, workspace: Path, charter: Charter) -> RunResult:
            os.symlink("../src/acme_harvest/invoices.py", workspace / "tests/link.py")
            return RunResult(success=True)

    probe = sandbox_origin.parent / "probe-link"
    try:
        os.symlink("x", probe)
    except OSError:
        pytest.skip("symlinks need privileges on this OS (POSIX-only check)")
    cfg = make_cfg(limits={"max_prs_per_night": 1})
    git_cfg = sandbox_origin.parent / "gitconfig"
    git_cfg.write_text(git_cfg.read_text("utf-8") + "[core]\n\tsymlinks = true\n", "utf-8")
    s = run_night(cfg, deps_for(sandbox_origin, lambda f: LinkEngine()), dry_run=True)
    assert any(r.startswith("symlinks not allowed") for r in s.rejected[0]["reasons"])


def test_engine_error_rejected_and_run_continues(
    make_cfg: Callable[..., Config], sandbox_origin: Path
) -> None:
    cfg = make_cfg(limits={"max_prs_per_night": 1})
    good = Engines(GOOD)

    def factory(found: FoundTask) -> AgentEngine:
        return Boom() if found.target == TRACE else good(found)

    gh = FakeGitHub()
    s = run_night(cfg, deps_for(sandbox_origin, factory, gh), dry_run=False)
    (rej,) = s.rejected
    assert rej["target"] == TRACE
    assert rej["reasons"][0].startswith("error: RuntimeError: agent crashed")
    assert "SECRETSECRET" not in rej["reasons"][0]
    assert [p["target"] for p in s.prs] == [LOTS]
    assert len(gh.pulls) == 1


def test_pr_creation_failure_rejected_and_scrubbed(
    make_cfg: Callable[..., Config], sandbox_origin: Path
) -> None:
    cfg = make_cfg(limits={"max_prs_per_night": 1})
    gh = FakeGitHub()
    gh.fail_create = GitHubError("POST /pulls -> HTTP 422: token ghs_abcdefghijklmnop1234 bad")
    s = run_night(cfg, deps_for(sandbox_origin, Engines({TRACE: GOOD[TRACE]}), gh), dry_run=False)
    rej = s.rejected[0]
    assert rej["target"] == TRACE
    assert "HTTP 422" in rej["reasons"][0] and "ghs_abcdefghijklmnop1234" not in rej["reasons"][0]
    assert s.prs == []
    # The branch was pushed before the PR failed; a later run reports it instead of retrying.
    assert f"refs/heads/{rej['branch']}" in bare_refs(sandbox_origin)


class StrayRunner(CachingRunner):
    """Coverage run leaves an untracked file behind (build output that is not gitignored)."""

    def run(
        self, cmd: str, cwd: Path, timeout: float = 900, outputs: Sequence[str] = ()
    ) -> CmdResult:
        res = super().run(cmd, cwd, timeout, outputs)
        if "--cov" in cmd and (cwd / "tests/test_traceability.py").exists():
            (cwd / "stray.log").write_text("x\n", encoding="utf-8")
        return res


def test_work_tree_changed_by_gates_rejected(
    make_cfg: Callable[..., Config], sandbox_origin: Path
) -> None:
    cfg = make_cfg(limits={"max_prs_per_night": 1})
    engines = Engines({TRACE: GOOD[TRACE]})
    s = run_night(
        cfg, deps_for(sandbox_origin, engines, FakeGitHub(), StrayRunner()), dry_run=False
    )
    rej = s.rejected[0]
    assert rej["reasons"] == [
        "work tree changed while gates ran (gitignore build output?): ['stray.log']"
    ]


# --- dry run, limits, stops ------------------------------------------------------------


def test_dry_run_pushes_nothing(make_cfg: Callable[..., Config], sandbox_origin: Path) -> None:
    cfg = make_cfg(limits={"max_prs_per_night": 1})
    refs = bare_refs(sandbox_origin)
    s = run_night(cfg, deps_for(sandbox_origin, Engines(GOOD)), dry_run=True)
    assert bare_refs(sandbox_origin) == refs
    (pr,) = s.prs
    assert pr["dry_run"] is True and pr["number"] is None and pr["target"] == TRACE
    (change,) = changes(cfg)
    assert change["status"] == "rejected" and change["decided_by"] == "dry-run"
    assert run_row(cfg, s.run_id)["prs_opened"] == 0
    assert run_row(cfg, s.run_id)["dry_run"] == 1
    assert "would_open_pr" in events(cfg, s)
    md = Path(s.report_path).read_text(encoding="utf-8")
    assert "## Would open (dry run)" in md and "would open PR" in md

    # The dry-run record does not block the next run.
    s2 = run_night(cfg, deps_for(sandbox_origin, Engines(GOOD)), dry_run=True)
    assert [p["target"] for p in s2.prs] == [TRACE]


def test_real_run_needs_github(make_cfg: Callable[..., Config], sandbox_origin: Path) -> None:
    with pytest.raises(ValueError, match="GitHub client"):
        run_night(make_cfg(), deps_for(sandbox_origin, Engines({})), dry_run=False)


def test_max_prs_per_night(make_cfg: Callable[..., Config], sandbox_origin: Path) -> None:
    cfg = make_cfg(limits={"max_prs_per_night": 1})
    gh = FakeGitHub()
    engines = Engines(GOOD)
    s = run_night(cfg, deps_for(sandbox_origin, engines, gh), dry_run=False)
    assert len(s.prs) == 1 and len(gh.pulls) == 1
    assert engines.targets == [TRACE]
    assert s.stopped_reason == STOP_MAX_PRS
    assert len([r for r in bare_refs(sandbox_origin) if r.startswith("refs/heads/ai/")]) == 1


def test_two_prs_then_done(make_cfg: Callable[..., Config], sandbox_origin: Path) -> None:
    cfg = make_cfg(limits={"max_prs_per_night": 3})
    gh = FakeGitHub()
    s = run_night(
        cfg,
        deps_for(sandbox_origin, Engines({TRACE: GOOD[TRACE], LOTS: GOOD[LOTS]}), gh),
        dry_run=False,
    )
    assert [p["target"] for p in s.prs] == [TRACE, LOTS]
    assert [r["target"] for r in s.rejected] == [GRADING]
    assert s.stopped_reason == STOP_DONE
    # Each branch holds only its own change, cut from main.
    for pr in s.prs:
        files = git(sandbox_origin, "diff", "--name-only", "main", pr["branch"])
        assert files == next(iter(GOOD[pr["target"]]))


def test_token_budget_stops_run(make_cfg: Callable[..., Config], sandbox_origin: Path) -> None:
    cfg = make_cfg(limits={"max_prs_per_night": 3, "token_budget_per_night": 1000})
    bad = {TRACE: {"src/acme_harvest/invoices.py": "# x\n"}}
    engines = Engines(bad, tokens=1000)
    s = run_night(cfg, deps_for(sandbox_origin, engines), dry_run=True)
    assert s.tokens == 1000
    assert s.stopped_reason == STOP_BUDGET
    assert engines.targets == [TRACE]
    assert run_row(cfg, s.run_id)["tokens"] == 1000


def test_kill_switch_before_start(make_cfg: Callable[..., Config], sandbox_origin: Path) -> None:
    cfg = make_cfg()
    Path(cfg.kill_switch.file).write_text("", encoding="utf-8")
    with pytest.raises(KillSwitchEngaged):
        run_night(cfg, deps_for(sandbox_origin, Engines(GOOD)), dry_run=True)
    assert not cfg.paths().work_dir.exists()
    assert not cfg.paths().db_file.exists()
    assert not cfg.paths().lock_file.exists()


def test_kill_switch_between_tasks(make_cfg: Callable[..., Config], sandbox_origin: Path) -> None:
    cfg = make_cfg()
    engines = Engines({})

    def factory(found: FoundTask) -> AgentEngine:
        Path(cfg.kill_switch.file).write_text("", encoding="utf-8")
        return engines(found)

    s = run_night(cfg, deps_for(sandbox_origin, factory), dry_run=True)
    assert s.stopped_reason == STOP_KILL and s.kill_switch
    assert engines.targets == [TRACE]
    assert run_row(cfg, s.run_id)["status"] == "stopped"


def test_lock_held(make_cfg: Callable[..., Config], sandbox_origin: Path) -> None:
    cfg = make_cfg()
    with RunLock(cfg.paths().lock_file), pytest.raises(LockHeld):
        run_night(cfg, deps_for(sandbox_origin, Engines(GOOD)), dry_run=True)
    assert not cfg.paths().work_dir.exists()


def test_other_focus_skips_night(make_cfg: Callable[..., Config], sandbox_origin: Path) -> None:
    cfg = make_cfg(nightly_focus=["security"])
    engines = Engines(GOOD)
    s = run_night(cfg, deps_for(sandbox_origin, engines), dry_run=True)
    assert "not supported" in s.stopped_reason
    assert engines.targets == []
    assert not cfg.paths().work_dir.exists()


def test_repo_error_recorded_and_run_finishes(
    make_cfg: Callable[..., Config], sandbox_origin: Path
) -> None:
    cfg = make_cfg()
    broken = cfg.repos[0].model_copy(update={"coverage_command": 'python -c "exit(3)"'})
    cfg = cfg.model_copy(update={"repos": [broken]})
    s = run_night(cfg, deps_for(sandbox_origin, Engines(GOOD)), dry_run=True)
    (err,) = s.errors
    assert err["repo"] == "api" and "coverage command failed (exit 3)" in err["error"]
    assert s.stopped_reason == STOP_DONE


def test_missing_charter_fails_run(make_cfg: Callable[..., Config], sandbox_origin: Path) -> None:
    cfg = make_cfg(charter_file="docs/nope.yaml")
    s = run_night(cfg, deps_for(sandbox_origin, Engines(GOOD)), dry_run=True)
    assert s.failed and "FileNotFoundError" in s.stopped_reason
    assert run_row(cfg, s.run_id)["status"] == "failed"
    assert "FileNotFoundError" in Path(s.report_path).read_text(encoding="utf-8")
    assert not cfg.paths().lock_file.exists()


# --- helpers ---------------------------------------------------------------------------


def test_strip_credentials() -> None:
    assert strip_credentials("https://x-access-token:t@github.com/o/r.git") == (
        "https://github.com/o/r.git"
    )
    assert strip_credentials("/srv/git/r.git") == "/srv/git/r.git"


def test_authed_restores_clean_origin(tmp_path: Path, git_isolation: None) -> None:
    git(tmp_path, "init", "-q", "w")
    repo = GitRepo(tmp_path / "w")
    git(repo.path, "remote", "add", "origin", "https://github.com/o/r.git")
    url = "https://x-access-token:ghs_tok@github.com/o/r.git"
    with authed(repo, url):
        assert git(repo.path, "remote", "get-url", "origin") == url
    assert git(repo.path, "remote", "get-url", "origin") == "https://github.com/o/r.git"
    with pytest.raises(RuntimeError), authed(repo, url):
        raise RuntimeError("boom")
    assert git(repo.path, "remote", "get-url", "origin") == "https://github.com/o/r.git"


class Costly:
    """Wraps an engine factory and books `usd` per task, like the OpenHands engine does."""

    def __init__(self, inner: Engines, usd: float) -> None:
        self.inner, self.usd = inner, usd

    def __call__(self, found: FoundTask) -> AgentEngine:
        engine, usd = self.inner(found), self.usd

        class Wrapped:
            name = engine.name

            def run(self, task: Task, workspace: Path, charter: Charter) -> RunResult:
                result = engine.run(task, workspace, charter)
                result.cost_usd = usd
                return result

        return Wrapped()


def test_usd_budget_stops_run_and_cost_is_recorded(
    make_cfg: Callable[..., Config], sandbox_origin: Path
) -> None:
    from grafter.pipeline import STOP_USD

    cfg = make_cfg(limits={"max_prs_per_night": 3, "usd_budget_per_night": 0.5})
    bad = {TRACE: {"src/acme_harvest/invoices.py": "# x\n"}}
    engines = Engines(bad)
    s = run_night(cfg, deps_for(sandbox_origin, Costly(engines, 0.5)), dry_run=True)
    assert s.cost_usd == 0.5
    assert s.stopped_reason == STOP_USD
    assert engines.targets == [TRACE]
    assert run_row(cfg, s.run_id)["cost_usd"] == 0.5
    report = Path(s.report_path).read_text("utf-8")
    assert "- Cost: $0.50 (budget $0.50)" in report


# --- report as an issue comment (TODO 1D4b) --------------------------------------------


def test_real_run_posts_report_comment(
    make_cfg: Callable[..., Config], sandbox_origin: Path
) -> None:
    cfg = make_cfg(limits={"max_prs_per_night": 1})
    gh = FakeGitHub()
    s = run_night(cfg, deps_for(sandbox_origin, Engines(GOOD), gh), dry_run=False)
    assert len(gh.comments) == 1
    repo, number, body = gh.comments[0]
    assert (repo, number) == (GITHUB_REPO, 7)
    assert s.run_id in body and "pull/1" in body
    assert body == Path(s.report_path).read_text("utf-8")
    assert s.report_url == f"https://github.example/{GITHUB_REPO}/issues/7#c1"
    assert "report_posted" in events(cfg, s)


def test_dry_run_posts_no_comment(make_cfg: Callable[..., Config], sandbox_origin: Path) -> None:
    cfg = make_cfg(limits={"max_prs_per_night": 1})
    gh = FakeGitHub()
    s = run_night(cfg, deps_for(sandbox_origin, Engines(GOOD), gh), dry_run=True)
    assert gh.comments == [] and s.report_url == ""


def test_report_post_failure_does_not_fail_run(
    make_cfg: Callable[..., Config], sandbox_origin: Path
) -> None:
    from grafter.github import GitHubError

    cfg = make_cfg(limits={"max_prs_per_night": 1})
    gh = FakeGitHub()
    gh.fail_comment = GitHubError(
        "POST x -> HTTP 502: https://x-access-token:ghs_SECRETSECRET1234@github.com"
    )
    s = run_night(cfg, deps_for(sandbox_origin, Engines(GOOD), gh), dry_run=False)
    assert s.stopped_reason == STOP_MAX_PRS and not s.failed
    assert s.report_url == ""
    assert run_row(cfg, s.run_id)["status"] == "ok"
    log = (Path(s.report_path).parent / "run.jsonl").read_text("utf-8")
    assert "report_post_failed" in log and "ghs_SECRET" not in log


def test_comment_body_is_capped() -> None:
    from grafter.report import COMMENT_MAX, comment_body

    assert comment_body("short", "/r/report.md") == "short"
    long = comment_body("x" * (COMMENT_MAX + 500), "/r/report.md")
    assert len(long) <= COMMENT_MAX
    assert long.endswith("Truncated: the full report is at `/r/report.md` on the Grafter host.\n")


def test_report_has_one_cost_line(make_cfg: Callable[..., Config], sandbox_origin: Path) -> None:
    cfg = make_cfg(limits={"max_prs_per_night": 1})
    s = run_night(cfg, deps_for(sandbox_origin, Engines(GOOD), FakeGitHub()), dry_run=True)
    lines = [ln for ln in Path(s.report_path).read_text("utf-8").splitlines() if "Cost" in ln]
    assert lines == ["- Cost: $0.00 (budget $5.00)"]
