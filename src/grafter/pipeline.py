"""One nightly run (TODO 1D3).

lock -> kill switch -> charter -> per repo: baseline coverage -> `test` tasks -> per task:
engine -> real diff -> guard -> gates -> commit -> push `ai/*` -> PR -> record.
Stops at `max_prs_per_night`, the token budget or the kill switch. A dry run does
everything except commit, push and PR. One failing task never stops the run.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from grafter.charter import ChangeType, Charter, load_charter
from grafter.config import Config, Paths, RepoConfig
from grafter.engines.base import AgentEngine, RunResult
from grafter.finder import CoverageSnapshot, FoundTask, find_test_tasks, measure_coverage
from grafter.gates import run_gates
from grafter.github import scrub as scrub_tokens
from grafter.gitops import GitError, GitRepo
from grafter.gitops import scrub as scrub_urls
from grafter.guard import Tier, check
from grafter.lock import RunLock, check_kill_switch
from grafter.prbody import LABEL_COLORS, fingerprint_for, render_pr
from grafter.report import REPORT_FILE, comment_body, render_report
from grafter.runlog import RunLog, new_run_id
from grafter.runner import CommandRunner
from grafter.store import TRANSITIONS, ChangeStatus, Store

ACTOR = "grafter"
DRY_RUN_ACTOR = "dry-run"
SCRATCH_BRANCH = "grafter/scratch"  # local only: baseline coverage and charter checkout
EXTRA_TASKS = 2  # candidates per repo beyond max_prs_per_night, to absorb rejections
NOTE_MAX = 2000

STOP_DONE = "completed"
STOP_MAX_PRS = "max_prs_per_night reached"
STOP_BUDGET = "token budget reached"
STOP_USD = "USD budget reached"
STOP_KILL = "kill switch engaged"

_USERINFO = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*://)[^/@]*@")


class PullRequestApi(Protocol):
    """The `GitHubClient` calls the pipeline needs (a fake in tests)."""

    def ensure_labels(self, repo: str, labels: dict[str, str]) -> list[str]: ...

    def create_pr(
        self, repo: str, head: str, base: str, title: str, body: str, labels: list[str]
    ) -> dict[str, Any]: ...

    def find_open_pr(self, repo: str, fingerprint: str) -> dict[str, Any] | None: ...

    def ensure_report_issue(self, repo: str) -> int: ...

    def comment(self, repo: str, number: int, body: str) -> str: ...


@dataclass
class RunDeps:
    engine: Callable[[FoundTask], AgentEngine]
    runner: CommandRunner
    github: PullRequestApi | None
    # Clone/push URL per repo. Real: https://x-access-token:<token>@github.com/<owner>/<repo>.git.
    # The token is written to .git/config only while git talks to origin, never while the
    # engine or repo commands run.
    origin_url: Callable[[RepoConfig], str]
    environ: Mapping[str, str] | None = None
    today: Callable[[], date] = date.today
    # Per-repo runner (DockerRunner picks the repo's sandbox image); default: `runner`.
    runner_for: Callable[[RepoConfig], CommandRunner] | None = None

    def runner_of(self, repo: RepoConfig) -> CommandRunner:
        return self.runner_for(repo) if self.runner_for else self.runner


@dataclass
class RunSummary:
    run_id: str
    dry_run: bool
    prs: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    tokens: int = 0
    cost_usd: float = 0.0  # gateway spend reported by the engine, summed per task
    stopped_reason: str = ""
    errors: list[dict[str, Any]] = field(default_factory=list)  # repo-level failures
    report_path: str = ""
    report_url: str = ""  # the report's issue comment (real runs only)

    @property
    def kill_switch(self) -> bool:
        return self.stopped_reason == STOP_KILL

    @property
    def failed(self) -> bool:
        return self.stopped_reason.startswith("error")


def scrub(text: str) -> str:
    """Remove URL credentials and GitHub tokens from `text`."""
    return scrub_tokens(scrub_urls(text))


def strip_credentials(url: str) -> str:
    return _USERINFO.sub(r"\1", url)


def _error(e: BaseException) -> str:
    return f"{type(e).__name__}: {scrub(str(e))}"[:NOTE_MAX]


def _check_url(url: str) -> str:
    if url.startswith("-"):
        raise GitError("git remote", "unsafe url (starts with '-')")
    return url


@contextmanager
def authed(repo: GitRepo, url: str) -> Iterator[None]:
    """Point origin at the credentialed `url` for one network step, then strip it again."""
    clean = strip_credentials(url)
    repo.git("remote", "set-url", "origin", _check_url(url))
    if clean == url:  # nothing to strip afterwards
        yield
        return
    try:
        yield
    finally:
        repo.git("remote", "set-url", "origin", clean)


def sync_repo(url: str, dest: Path) -> GitRepo:
    """Clone or fetch `dest` from `url`; origin is left without credentials."""
    _check_url(url)
    if (dest / ".git").exists():
        repo = GitRepo(dest)
        with authed(repo, url):
            GitRepo.clone_or_fetch(url, dest)
        return repo
    try:
        return GitRepo.clone_or_fetch(url, dest)
    finally:
        if (dest / ".git").exists():
            GitRepo(dest).git("remote", "set-url", "origin", strip_credentials(url))


def _post_report(
    cfg: Config, github: PullRequestApi, summary: RunSummary, report: Path, log: RunLog
) -> None:
    """Comment the report on the solution's report issue (1D4b). Never fails the run."""
    repo = cfg.charter_repo_config().github
    try:
        body = comment_body(report.read_text("utf-8"), str(report))
        number = github.ensure_report_issue(repo)
        summary.report_url = github.comment(repo, number, body)
        log.event("report_posted", repo=repo, issue=number, url=summary.report_url)
    except Exception as e:  # GitHub down, permissions, ...: the report stays local
        log.event("report_post_failed", repo=repo, error=scrub(_error(e))[:NOTE_MAX])


def run_night(cfg: Config, deps: RunDeps, dry_run: bool, run_id: str | None = None) -> RunSummary:
    """Run one night. Raises `LockHeld` or `KillSwitchEngaged` before anything else happens;
    every later failure ends up in the summary (`stopped_reason`, `rejected`, `errors`)."""
    if not dry_run and deps.github is None:
        raise ValueError("a real run needs a GitHub client; use a dry run without one")
    paths = cfg.paths()
    run_id = run_id or new_run_id()
    with RunLock(paths.lock_file):
        check_kill_switch(cfg.kill_switch, deps.environ)
        with Store(paths.db_file) as store:
            store.migrate()
            store.start_run(run_id, dry_run)
            log = RunLog(paths.runs_dir / run_id, run_id)
            summary = RunSummary(run_id=run_id, dry_run=dry_run)
            status = "failed"
            try:
                _Night(cfg, deps, paths, store, log, summary).run()
                status = "stopped" if summary.kill_switch else "ok"
            except Exception as e:
                summary.stopped_reason = f"error: {_error(e)}"
                log.event("run_error", error=summary.stopped_reason)
            finally:
                if not summary.stopped_reason:
                    summary.stopped_reason = "interrupted"
                opened = 0 if dry_run else len(summary.prs)
                store.finish_run(
                    run_id, status, opened, summary.tokens, summary.cost_usd, summary.stopped_reason
                )
                report = log.run_dir / REPORT_FILE
                with report.open("w", encoding="utf-8", newline="\n") as fh:
                    fh.write(render_report(summary, cfg))
                summary.report_path = str(report)
                if not dry_run and deps.github is not None:
                    _post_report(cfg, deps.github, summary, report, log)
                log.event(
                    "run_finished",
                    report=str(report),
                    status=status,
                    stopped_reason=summary.stopped_reason,
                    prs=len(summary.prs),
                    rejected=len(summary.rejected),
                    skipped=len(summary.skipped),
                    tokens=summary.tokens,
                    cost_usd=round(summary.cost_usd, 4),
                )
    return summary


class _Night:
    def __init__(
        self,
        cfg: Config,
        deps: RunDeps,
        paths: Paths,
        store: Store,
        log: RunLog,
        summary: RunSummary,
    ) -> None:
        self.cfg, self.deps, self.paths = cfg, deps, paths
        self.store, self.log, self.summary = store, log, summary

    # --- run / repo ----------------------------------------------------------------------

    def run(self) -> None:
        cfg, log = self.cfg, self.log
        focus = cfg.focus_for(self.deps.today())
        log.event(
            "run_started",
            dry_run=self.summary.dry_run,
            focus=focus.value,
            repos=[r.name for r in cfg.repos],
            max_prs=cfg.limits.max_prs_per_night,
            token_budget=cfg.limits.token_budget_per_night,
        )
        if focus != ChangeType.TEST:
            self.summary.stopped_reason = (
                f"nightly focus {focus.value!r} not supported in phase 1 (test only)"
            )
            log.event("focus_skipped", focus=focus.value)
            return

        charter_repo = cfg.charter_repo_config()
        charter_git = self._sync(charter_repo)
        self._fresh(charter_git, charter_repo, SCRATCH_BRANCH)
        charter = load_charter(charter_git.path / cfg.charter_file)
        log.event("charter_loaded", repo=charter_repo.name, file=cfg.charter_file)
        unknown = sorted(set(charter.repos) - {r.name for r in cfg.repos})
        if unknown:
            log.event("charter_unknown_repos", repos=unknown)

        for repo in cfg.repos:
            if self._should_stop():
                return
            try:
                git = charter_git if repo.name == charter_repo.name else self._sync(repo)
                self._repo(repo, git, charter)
            except Exception as e:
                self.summary.errors.append({"repo": repo.name, "error": _error(e)})
                log.event("repo_error", repo=repo.name, error=_error(e))
        if not self.summary.stopped_reason:
            full = len(self.summary.prs) >= cfg.limits.max_prs_per_night
            self.summary.stopped_reason = STOP_MAX_PRS if full else STOP_DONE

    def _repo(self, repo: RepoConfig, git: GitRepo, charter: Charter) -> None:
        self._fresh(git, repo, SCRATCH_BRANCH)
        baseline = measure_coverage(self.deps.runner_of(repo), git.path, repo)
        found = find_test_tasks(
            repo, charter, baseline.files, limit=self.cfg.limits.max_prs_per_night + EXTRA_TASKS
        )
        self.log.event(
            "tasks_found",
            repo=repo.name,
            coverage=round(baseline.total, 2),
            targets=[f.target for f in found],
        )
        for task in found:
            if self._should_stop():
                return
            self._task(repo, git, charter, baseline, task)

    def _should_stop(self) -> bool:
        s, limits = self.summary, self.cfg.limits
        if s.stopped_reason:
            return True
        if self.cfg.kill_switch.is_engaged(self.deps.environ):
            s.stopped_reason = STOP_KILL
        elif len(s.prs) >= limits.max_prs_per_night:
            s.stopped_reason = STOP_MAX_PRS
        elif s.tokens >= limits.token_budget_per_night:
            s.stopped_reason = STOP_BUDGET
        elif s.cost_usd >= limits.usd_budget_per_night:
            s.stopped_reason = STOP_USD
        else:
            return False
        self.log.event("run_stopping", reason=s.stopped_reason)
        return True

    # --- git helpers ---------------------------------------------------------------------

    def _sync(self, repo: RepoConfig) -> GitRepo:
        git = sync_repo(self.deps.origin_url(repo), self.paths.work_dir / repo.name)
        self.log.event("repo_synced", repo=repo.name)
        return git

    def _fresh(self, git: GitRepo, repo: RepoConfig, branch: str) -> str:
        with authed(git, self.deps.origin_url(repo)):
            return git.fresh_branch(branch, repo.default_branch)

    @staticmethod
    def _on_origin(git: GitRepo, branch: str) -> bool:
        try:
            git.git("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}")
        except GitError:
            return False
        return True

    # --- one task ------------------------------------------------------------------------

    def _skip(self, entry: dict[str, Any], reason: str) -> None:
        self.summary.skipped.append({**entry, "reason": reason})
        self.log.event("task_skipped", **entry, reason=reason)

    def _reject(
        self, change_id: str | None, entry: dict[str, Any], reasons: list[str], actor: str = ACTOR
    ) -> None:
        reasons = [scrub(r) for r in reasons]
        if change_id is not None:
            current = ChangeStatus(self.store.get_change(change_id)["status"])
            if ChangeStatus.REJECTED in TRANSITIONS[current]:
                note = "; ".join(reasons)[:NOTE_MAX]
                self.store.set_status(change_id, ChangeStatus.REJECTED, actor, note)
        self.summary.rejected.append({**entry, "reasons": reasons})
        self.log.event("task_rejected", **entry, reasons=reasons)

    def _task(
        self,
        repo: RepoConfig,
        git: GitRepo,
        charter: Charter,
        baseline: CoverageSnapshot,
        found: FoundTask,
    ) -> None:
        cfg, store, log = self.cfg, self.store, self.log
        task = found.task
        fp = fingerprint_for(task, found.target)
        branch = f"{cfg.branches.fix_prefix}test-{fp}"
        entry: dict[str, Any] = {
            "repo": repo.name,
            "target": found.target,
            "fingerprint": fp,
            "branch": branch,
        }
        change_id: str | None = None
        try:
            existing = store.open_change_for(repo.name, fp)
            if existing is not None:
                self._skip(entry, f"open change {existing['id']} ({existing['status']})")
                return
            gh = self.deps.github
            if gh is not None and not self.summary.dry_run:
                pr = gh.find_open_pr(repo.github, fp)
                if pr is not None:
                    self._skip(entry, f"open PR #{pr.get('number')}")
                    return
            if self._on_origin(git, branch):
                self._skip(entry, f"branch {branch} already on origin; delete it to retry")
                return

            base_sha = self._fresh(git, repo, branch)
            change_id = store.add_change(
                self.summary.run_id,
                repo.name,
                branch,
                base_sha,
                category=task.change_type.value,
                charter_ref=task.charter_ref,
                fingerprint=fp,
                summary=task.instruction,
                risk_tier=int(Tier.APPROVE),
            )
            store.set_status(change_id, ChangeStatus.BUILDING, ACTOR)
            log.event("task_started", **entry, change_id=change_id, coverage=found.coverage)

            engine = self.deps.engine(found)
            result = engine.run(task, git.path, charter)
            self.summary.tokens += max(result.tokens_used, 0)
            self.summary.cost_usd += max(result.cost_usd, 0.0)
            log.event(
                "engine_finished",
                **entry,
                engine=engine.name,
                success=result.success,
                tokens=result.tokens_used,
                cost_usd=result.cost_usd,
                claimed_files=result.changed_files,
            )

            stats = git.diff_stats()
            reasons: list[str] = []
            if not result.success:
                reasons.append(f"engine reported failure: {result.summary}"[:NOTE_MAX])
            if stats.symlinks:
                reasons.append(f"symlinks not allowed: {stats.symlinks}")
            if stats.binary_files:
                reasons.append(f"binary files not allowed: {stats.binary_files}")
            if not stats.changed_files:
                reasons.append("engine made no changes")
            if reasons:
                self._reject(change_id, entry, reasons)
                return

            deleted = set(stats.deleted_files)
            real = RunResult(
                success=True,
                changed_files=[f for f in stats.changed_files if f not in deleted],
                deleted_files=stats.deleted_files,
                lines_added=stats.added,
                lines_removed=stats.removed,
                diff=git.diff(),
                summary=result.summary,
                risks=result.risks,
                tokens_used=result.tokens_used,
                cost_usd=result.cost_usd,
            )
            store.set_status(change_id, ChangeStatus.TESTING, ACTOR)
            log.event(
                "diff",
                **entry,
                files=real.changed_files,
                deleted=real.deleted_files,
                added=real.lines_added,
                removed=real.lines_removed,
            )

            # Guard first: a change that may not land is never executed by the gates.
            verdict = check(task, real, charter, cfg.limits, repo)
            log.event(
                "guard",
                **entry,
                allowed=verdict.allowed,
                tier=int(verdict.tier),
                reasons=verdict.reasons,
            )
            if not verdict.allowed:
                self._reject(change_id, entry, verdict.reasons)
                return

            gates = run_gates(self.deps.runner_of(repo), git.path, repo, baseline, found.target)
            test_results = {
                "passed": gates.passed,
                "tests": gates.tests_summary,
                "coverage_before": gates.coverage_before,
                "coverage_after": gates.coverage_after,
                "target_before": gates.target_before,
                "target_after": gates.target_after,
            }
            store.update_change(
                change_id,
                risk_tier=int(verdict.tier),
                summary=result.summary or task.instruction,
                risks=result.risks,
                test_results=test_results,
            )
            log.event("gates", **entry, reasons=gates.reasons, **test_results)
            if not gates.passed:
                self._reject(change_id, entry, gates.reasons)
                return

            after = git.diff_stats()
            if (sorted(after.changed_files), after.added, after.removed) != (
                sorted(stats.changed_files),
                stats.added,
                stats.removed,
            ):
                extra = sorted(set(after.changed_files) - set(stats.changed_files))
                self._reject(
                    change_id,
                    entry,
                    [f"work tree changed while gates ran (gitignore build output?): {extra}"],
                )
                return

            draft = render_pr(
                task,
                verdict.tier,
                summary=result.summary,
                tests_summary=gates.tests_summary,
                coverage_before=gates.coverage_before,
                coverage_after=gates.coverage_after,
                risks=result.risks,
                tokens=result.tokens_used,
                cost_usd=result.cost_usd,
                fingerprint=fp,
            )
            pr_entry = {**entry, "title": draft.title, "tier": int(verdict.tier)}

            if self.summary.dry_run:
                store.set_status(
                    change_id, ChangeStatus.REJECTED, DRY_RUN_ACTOR, "dry run: PR not opened"
                )
                self.summary.prs.append({**pr_entry, "dry_run": True, "number": None, "url": None})
                log.event("would_open_pr", **pr_entry)
                return

            assert gh is not None  # checked in run_night
            message = f"{draft.title}\n\nGrafter-Fingerprint: {fp}\nCharter: {task.charter_ref}\n"
            head_sha = git.commit(message, cfg.git_author_name, cfg.git_author_email)
            store.update_change(change_id, head_sha=head_sha)
            with authed(git, self.deps.origin_url(repo)):
                git.push(branch, cfg.branches.fix_prefix, cfg.branches.production)
            log.event("pushed", **entry, head_sha=head_sha)

            gh.ensure_labels(
                repo.github, {lb: LABEL_COLORS.get(lb, "ededed") for lb in draft.labels}
            )
            pr = gh.create_pr(
                repo.github, branch, repo.default_branch, draft.title, draft.body, draft.labels
            )
            number = int(pr["number"])
            store.update_change(change_id, pr_number=number)
            store.set_status(change_id, ChangeStatus.PENDING_REVIEW, ACTOR, f"PR #{number}")
            url = pr.get("html_url")
            self.summary.prs.append({**pr_entry, "dry_run": False, "number": number, "url": url})
            log.event("pr_opened", **entry, number=number, url=url)
        except Exception as e:
            self._reject(change_id, entry, [f"error: {_error(e)}"])
