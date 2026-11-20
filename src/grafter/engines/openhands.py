"""OpenHands engine (TODO 1C3, D4, D15). MIT packages only; never OpenHands `enterprise/`.

One task, two steps:
1. **Planner** (`models.planner`), one chat call made from this host: task, charter, the
   repo's `AGENTS.md`, the target file and its current tests -> a short test plan.
2. **Coder** (`models.coder`), an OpenHands agent inside a fresh agent-server container that
   holds an archive of the work tree. It follows the plan, then the container's diff comes
   back as a patch and is applied here with `git apply` (which refuses paths outside the repo).

Nothing the agent writes runs on this host. Both steps use one gateway run key, limited to
the two models and capped at this task's share of the nightly USD budget (D16); the key is
always deleted. Cost is the gateway's own spend for that key, not an SDK estimate.

The SDK sits behind `SandboxFactory` (real adapter: `grafter.engines.openhands_sdk`), so this
module is tested offline with fakes and does not import `openhands`.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from grafter.charter import Charter, matches_any
from grafter.config import Config, RepoConfig
from grafter.engines.base import RunResult, Task
from grafter.gateway import GatewayClient, GatewayError
from grafter.gitops import GitError, GitRepo

WORKDIR = "/workspace/project"
PATCH_PATH = "/tmp/grafter.patch"
ARCHIVE_PATH = "/tmp/grafter-repo.tgz"
KEY_DURATION = "1h"
SETUP_TIMEOUT_S = 600.0
CMD_TIMEOUT_S = 120.0

# Size caps for prompt parts (characters).
CAP_AGENTS_MD = 6_000
CAP_TARGET = 16_000
CAP_TEST_FILE = 8_000
CAP_TESTS_TOTAL = 24_000
MAX_TEST_FILES = 4

# Best-effort dependency install per language before the agent starts.
SETUP = {
    "python": "python -m pip install -q -e . pytest pytest-cov 2>&1 | tail -3 || true",
}

AgentStatus = Literal["finished", "token_cap", "error", "timeout"]


@dataclass(frozen=True)
class LlmSpec:
    model: str  # LiteLLM proxy model name, e.g. "litellm_proxy/claude-sonnet-5-5"
    base_url: str  # as seen from inside the container
    api_key: str  # the run key; never the master or provider key


@dataclass(frozen=True)
class AgentLimits:
    max_iterations: int
    max_usd: float
    max_tokens: int
    timeout_s: float


@dataclass
class AgentOutcome:
    status: AgentStatus
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_estimate: float = 0.0  # SDK's own estimate; for the log only
    detail: str = ""

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str


class Sandbox(Protocol):
    def upload(self, local: Path, remote: str) -> None: ...

    def execute(self, command: str, timeout: float) -> ExecResult: ...

    def download(self, remote: str, local: Path) -> None: ...

    def run_agent(self, prompt: str, llm: LlmSpec, limits: AgentLimits) -> AgentOutcome: ...


SandboxFactory = Callable[[], AbstractContextManager[Sandbox]]


# --- prompts ---------------------------------------------------------------------------


def _clip(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[:cap] + f"\n... [truncated, {len(text)} chars]"


def _read(path: Path, cap: int) -> str:
    try:
        return _clip(path.read_text(encoding="utf-8", errors="replace"), cap)
    except OSError:
        return ""


def related_tests(workspace: Path, repo: RepoConfig, target: str) -> list[str]:
    """Test files (repo-relative, POSIX) under the test paths that mention the target module."""
    stem = PurePosixPath(target).stem
    if not stem:
        return []
    hits: list[str] = []
    for p in sorted(workspace.rglob("*")):
        if not p.is_file() or ".git" in p.parts:
            continue
        rel = p.relative_to(workspace).as_posix()
        if matches_any(rel, repo.test_paths) and stem in _read(p, CAP_TEST_FILE):
            hits.append(rel)
            if len(hits) >= MAX_TEST_FILES:
                break
    return hits


def planner_messages(
    task: Task, charter: Charter, repo: RepoConfig, workspace: Path
) -> list[dict[str, str]]:
    domain = task.charter_ref.removeprefix("domain: ")
    parts = [
        f"# Task\n{task.instruction}",
        "# Solution charter\n"
        f"Solution: {charter.solution}\nPurpose: {charter.purpose}\n"
        f"Cited domain: {domain}\n"
        "Invariants (never break these):\n"
        + "\n".join(f"- {i}" for i in charter.invariants or ["(none listed)"]),
    ]
    agents = _read(workspace / "AGENTS.md", CAP_AGENTS_MD)
    if agents:
        parts.append(f"# AGENTS.md (repo rules)\n{agents}")
    if task.target:
        parts.append(
            f"# Target file: {task.target}\n```\n{_read(workspace / task.target, CAP_TARGET)}\n```"
        )
    budget = CAP_TESTS_TOTAL
    for rel in related_tests(workspace, repo, task.target):
        body = _read(workspace / rel, min(CAP_TEST_FILE, budget))
        budget -= len(body)
        parts.append(f"# Existing tests: {rel}\n```\n{body}\n```")
        if budget <= 0:
            break
    system = (
        "You plan test-only changes for a coding agent. Reply with a short numbered plan: "
        "which test file to change or create, and 2-6 small test functions, each checking ONE "
        "behaviour of the target (name + what it asserts). Only behaviour the target code "
        "really has. No source changes, no new dependencies, no skips. Keep it under 300 words."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(parts)}]


def coder_prompt(task: Task, repo: RepoConfig, plan: str) -> str:
    return (
        f"You are working in the git repo at {WORKDIR}.\n\n"
        f"Task: {task.instruction}\n\n"
        f"Plan to follow:\n{plan.strip()}\n\n"
        "Rules (a change that breaks one is rejected):\n"
        f"- Create or change files ONLY under: {', '.join(repo.test_paths)}.\n"
        "- Never change source files, config, CI, AGENTS.md or the charter.\n"
        "- Never delete, skip, xfail or weaken an existing test.\n"
        "- Write small, focused test functions: one behaviour each, a descriptive name, "
        "a few asserts. Not one big test with many asserts.\n"
        "- Do not commit; leave the changes in the work tree.\n\n"
        f"When done, run `{repo.test_command}` in {WORKDIR} and make sure the whole suite "
        "passes. Finish when it is green."
    )


# --- engine ----------------------------------------------------------------------------


def _fail(
    summary: str, tokens: int = 0, cost: float = 0.0, risks: list[str] | None = None
) -> RunResult:
    return RunResult(
        success=False, summary=summary[:500], tokens_used=tokens, cost_usd=cost, risks=risks or []
    )


class OpenHandsEngine:
    name = "openhands"

    def __init__(self, cfg: Config, gateway: GatewayClient, sandbox: SandboxFactory) -> None:
        self.cfg = cfg
        self.gateway = gateway
        self.sandbox = sandbox

    @property
    def usd_per_task(self) -> float:
        lim = self.cfg.limits
        return round(lim.usd_budget_per_night / lim.max_prs_per_night, 4)

    @property
    def tokens_per_task(self) -> int:
        lim = self.cfg.limits
        return lim.token_budget_per_night // lim.max_prs_per_night

    def run(self, task: Task, workspace: Path, charter: Charter) -> RunResult:
        repo = self.cfg.repo(task.repo)
        models = self.cfg.models
        try:
            key = self.gateway.create_run_key(
                f"{task.id}", [models.planner, models.coder], self.usd_per_task, KEY_DURATION
            )
        except GatewayError as e:
            return _fail(f"gateway: {e}")
        try:
            return self._run(task, workspace, charter, repo, key)
        finally:
            try:
                self.gateway.delete_key(key)
            except GatewayError:
                pass  # expires on its own after KEY_DURATION

    def _spend(self, key: str, fallback: float, risks: list[str]) -> float:
        try:
            return self.gateway.key_spend(key)
        except GatewayError as e:
            risks.append(f"gateway spend unknown ({e}); cost is the SDK estimate")
            return fallback

    def _run(
        self, task: Task, workspace: Path, charter: Charter, repo: RepoConfig, key: str
    ) -> RunResult:
        oh = self.cfg.openhands
        risks: list[str] = []
        try:
            plan = self.gateway.complete(
                key,
                self.cfg.models.planner,
                planner_messages(task, charter, repo, workspace),
                oh.plan_max_output,
            )
        except GatewayError as e:
            return _fail(f"planner failed: {e}", cost=self._spend(key, 0.0, risks), risks=risks)
        plan_tokens = plan.prompt_tokens + plan.completion_tokens
        if not plan.text.strip():
            return _fail(
                "planner returned an empty plan", plan_tokens, self._spend(key, 0.0, risks)
            )

        git = GitRepo(workspace)
        limits = AgentLimits(
            max_iterations=oh.max_iterations,
            max_usd=self.usd_per_task,
            max_tokens=max(self.tokens_per_task - plan_tokens, 0),
            timeout_s=float(oh.timeout_s),
        )
        llm = LlmSpec(f"litellm_proxy/{self.cfg.models.coder}", oh.gateway_url, key)
        with tempfile.TemporaryDirectory(prefix="grafter-oh-") as tmp:
            tmpdir = Path(tmp)
            patch = tmpdir / "change.patch"
            try:
                outcome = self._in_sandbox(task, repo, plan.text, git, tmpdir, patch, llm, limits)
            except Exception as e:  # SDK/container errors: report, never crash the night
                cost = self._spend(key, 0.0, risks)
                return _fail(f"sandbox error: {type(e).__name__}: {e}", plan_tokens, cost, risks)
            tokens = plan_tokens + outcome.tokens
            cost = self._spend(key, outcome.cost_estimate, risks)
            if outcome.status != "finished":
                reason = {
                    "token_cap": f"token budget exceeded ({tokens} > {self.tokens_per_task})",
                    "timeout": f"agent timed out after {oh.timeout_s}s",
                    "error": "agent ended with an error",
                }[outcome.status]
                detail = f": {outcome.detail}" if outcome.detail else ""
                return _fail(reason + detail, tokens, cost, risks)
            try:
                git.apply_patch(patch)
            except GitError as e:
                return _fail(f"patch refused: {e}", tokens, cost, risks)
        return RunResult(
            success=True,
            changed_files=git.changed_files(),
            summary=f"{task.charter_ref}. Plan:\n{plan.text.strip()}"[:2000],
            risks=risks,
            tokens_used=tokens,
            cost_usd=cost,
        )

    def _in_sandbox(
        self,
        task: Task,
        repo: RepoConfig,
        plan: str,
        git: GitRepo,
        tmpdir: Path,
        patch: Path,
        llm: LlmSpec,
        limits: AgentLimits,
    ) -> AgentOutcome:
        archive = git.archive(tmpdir / "repo.tgz")
        with self.sandbox() as box:
            box.upload(archive, ARCHIVE_PATH)
            prep = box.execute(
                f"mkdir -p {WORKDIR} && tar -xzf {ARCHIVE_PATH} -C {WORKDIR} && cd {WORKDIR} && "
                "git init -q && git add -A && "
                "git -c user.name=grafter -c user.email=grafter@example.invalid "
                "commit -q -m base",
                CMD_TIMEOUT_S,
            )
            if prep.exit_code != 0:
                raise RuntimeError(f"workspace setup failed: {prep.stdout[-300:]}")
            setup = SETUP.get(repo.language)
            if setup:
                box.execute(f"cd {WORKDIR} && {setup}", SETUP_TIMEOUT_S)
            outcome = box.run_agent(coder_prompt(task, repo, plan), llm, limits)
            if outcome.status == "finished":
                out = box.execute(
                    f"cd {WORKDIR} && git add -A && git diff --cached --binary > {PATCH_PATH}",
                    CMD_TIMEOUT_S,
                )
                if out.exit_code != 0:
                    raise RuntimeError(f"collecting the diff failed: {out.stdout[-300:]}")
                box.download(PATCH_PATH, patch)
        return outcome
