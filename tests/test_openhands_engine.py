"""OpenHandsEngine offline (TODO 1C3a): fake gateway (HTTP) + fake sandbox, real git."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from conftest import git
from test_gateway import MASTER, RUN_KEY, FakeLiteLLM

from grafter.charter import Charter, load_charter
from grafter.config import Config
from grafter.engines.base import Task
from grafter.engines.openhands import (
    ARCHIVE_PATH,
    KEEP_EOL,
    PATCH_PATH,
    AgentLimits,
    AgentOutcome,
    ExecResult,
    LlmSpec,
    OpenHandsEngine,
    Sandbox,
    coder_prompt,
    planner_messages,
    related_tests,
)
from grafter.finder import find_test_tasks
from grafter.gateway import GatewayClient
from grafter.gitops import GitRepo

PLAN = "1. tests/test_grading.py: test_grade_rejects_low_brix asserts REJECT below 7 brix."


@pytest.fixture
def fake() -> Iterator[FakeLiteLLM]:
    f = FakeLiteLLM()
    f.chat_response = {
        "choices": [{"message": {"content": PLAN}}],
        "usage": {"prompt_tokens": 900, "completion_tokens": 100},
    }
    f.key_info = {"info": {"spend": 0.21}}
    yield f
    f.server.shutdown()


@pytest.fixture
def workspace(tmp_path: Path, sandbox_origin: Path) -> GitRepo:
    return GitRepo.clone_or_fetch(str(sandbox_origin), tmp_path / "work")


@pytest.fixture
def charter(workspace: GitRepo) -> Charter:
    return load_charter(workspace.path / "grafter.charter.yaml")


@pytest.fixture
def cfg(make_cfg: Callable[..., Config], fake: FakeLiteLLM) -> Config:
    return make_cfg(
        engine="openhands",
        llm_gateway_url=fake.url,
        limits={
            "max_prs_per_night": 2,
            "usd_budget_per_night": 1.0,
            "token_budget_per_night": 50_000,
        },
    )


def task_for(cfg: Config, workspace: GitRepo, charter: Charter, target: str) -> Task:
    cov = {target: 10.0}
    found = find_test_tasks(cfg.repos[0], charter, cov, limit=1)
    assert found and found[0].target == target
    return found[0].task


def patch_from(workspace: GitRepo, tmp_path: Path, edit: Callable[[Path], None]) -> bytes:
    other = tmp_path / "container-copy"
    git(tmp_path, "clone", "-q", str(workspace.path), str(other))
    edit(other)
    git(other, "add", "-A")
    return subprocess.run(
        ["git", "-c", "core.autocrlf=false", "diff", "--cached", "--binary"],
        cwd=other,
        check=True,
        capture_output=True,
    ).stdout


def add_test(d: Path) -> None:
    p = d / "tests" / "test_grading_more.py"
    p.write_bytes(
        b"from acme_harvest.grading import Grade, grade\n\n\n"
        b"def test_grade_rejects_low_brix() -> None:\n"
        b"    assert grade(defect_pct=0, brix=6.9) == Grade.REJECT\n"
    )


@dataclass
class FakeSandbox:
    patch: bytes = b""
    outcome: AgentOutcome = field(
        default_factory=lambda: AgentOutcome("finished", 3000, 400, cost_estimate=0.2)
    )
    prep_exit: int = 0
    raise_on_agent: Exception | None = None
    calls: list[tuple[str, Any]] = field(default_factory=list)
    uploads: dict[str, bytes] = field(default_factory=dict)
    opened: int = 0
    closed: int = 0

    def factory(self) -> Callable[[], Any]:
        @contextmanager
        def open_box() -> Iterator[Sandbox]:
            self.opened += 1
            try:
                yield self
            finally:
                self.closed += 1

        return open_box

    def upload(self, local: Path, remote: str) -> None:
        self.uploads[remote] = local.read_bytes()
        self.calls.append(("upload", remote))

    def execute(self, command: str, timeout: float) -> ExecResult:
        self.calls.append(("execute", command))
        if "tar -xzf" in command:
            return ExecResult(self.prep_exit, "" if self.prep_exit == 0 else "tar: boom")
        return ExecResult(0, "")

    def download(self, remote: str, local: Path) -> None:
        self.calls.append(("download", remote))
        local.write_bytes(self.patch)

    def run_agent(self, prompt: str, llm: LlmSpec, limits: AgentLimits) -> AgentOutcome:
        self.calls.append(("agent", (prompt, llm, limits)))
        if self.raise_on_agent:
            raise self.raise_on_agent
        return self.outcome


def engine(cfg: Config, fake: FakeLiteLLM, box: FakeSandbox) -> OpenHandsEngine:
    return OpenHandsEngine(cfg, GatewayClient(fake.url, MASTER), box.factory())


def paths(fake: FakeLiteLLM) -> list[str]:
    return [c[1] for c in fake.calls]


# --- happy path --------------------------------------------------------------------------


def test_run_applies_patch_and_reports_gateway_cost(
    cfg: Config, fake: FakeLiteLLM, workspace: GitRepo, charter: Charter, tmp_path: Path
) -> None:
    box = FakeSandbox(patch=patch_from(workspace, tmp_path, add_test))
    task = task_for(cfg, workspace, charter, "src/acme_harvest/grading.py")
    result = engine(cfg, fake, box).run(task, workspace.path, charter)

    assert result.success, result.summary
    assert result.changed_files == ["tests/test_grading_more.py"]
    assert result.cost_usd == 0.21  # gateway spend, not the SDK's 0.2
    assert result.tokens_used == 900 + 100 + 3000 + 400
    assert PLAN in result.summary and "domain: grading" in result.summary
    assert (workspace.path / "tests/test_grading_more.py").is_file()

    # One run key for both models, capped at this task's share; deleted at the end.
    assert paths(fake) == ["/key/generate", "/v1/chat/completions", "/key/info", "/key/delete"]
    gen = fake.calls[0][3]
    assert gen["models"] == ["claude-opus-5-5", "claude-sonnet-5-5"]
    assert gen["max_budget"] == 0.5 and gen["duration"] == "1h"
    assert fake.calls[0][2] == f"Bearer {MASTER}"
    assert fake.calls[1][2] == f"Bearer {RUN_KEY}"  # planner uses the run key
    assert fake.calls[1][3]["model"] == "claude-opus-5-5"
    assert fake.calls[3][3] == {"keys": [RUN_KEY]}

    # The sandbox got an archive, ran the agent with the run key only, and gave a patch back.
    assert box.opened == box.closed == 1
    kinds = [c[0] for c in box.calls]
    assert kinds == ["upload", "execute", "agent", "execute", "execute", "download"]
    assert "GRAFTER_EOL" in box.calls[3][1]
    assert ARCHIVE_PATH in box.uploads and box.uploads[ARCHIVE_PATH][:2] == b"\x1f\x8b"
    prompt, llm, limits = box.calls[2][1]
    assert llm == LlmSpec("litellm_proxy/claude-sonnet-5-5", "http://litellm:4000", RUN_KEY)
    assert limits == AgentLimits(60, 0.5, 25_000 - 1000, 1800.0)
    assert PLAN in prompt and "tests/**" in prompt
    assert box.calls[5][1] == PATCH_PATH


def test_no_changes_is_success_with_empty_list(
    cfg: Config, fake: FakeLiteLLM, workspace: GitRepo, charter: Charter
) -> None:
    box = FakeSandbox(patch=b"")
    task = task_for(cfg, workspace, charter, "src/acme_harvest/grading.py")
    result = engine(cfg, fake, box).run(task, workspace.path, charter)
    assert result.success and result.changed_files == []


# --- failures -----------------------------------------------------------------------------


def test_patch_outside_repo_is_refused(
    cfg: Config, fake: FakeLiteLLM, workspace: GitRepo, charter: Charter, tmp_path: Path
) -> None:
    evil = (
        b"diff --git a/../escape.txt b/../escape.txt\nnew file mode 100644\n"
        b"--- /dev/null\n+++ b/../escape.txt\n@@ -0,0 +1 @@\n+pwned\n"
    )
    box = FakeSandbox(patch=evil)
    task = task_for(cfg, workspace, charter, "src/acme_harvest/grading.py")
    result = engine(cfg, fake, box).run(task, workspace.path, charter)
    assert not result.success and result.summary.startswith("patch refused")
    assert not (tmp_path / "escape.txt").exists()
    assert workspace.changed_files() == []
    assert paths(fake)[-1] == "/key/delete"


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("token_cap", "token budget exceeded"),
        ("timeout", "agent timed out after 1800s"),
        ("error", "agent ended with an error: status stuck"),
    ],
)
def test_agent_not_finished_fails_without_patch(
    cfg: Config,
    fake: FakeLiteLLM,
    workspace: GitRepo,
    charter: Charter,
    status: Any,
    reason: str,
) -> None:
    detail = "status stuck" if status == "error" else ""
    box = FakeSandbox(outcome=AgentOutcome(status, 30_000, 1_000, 0.4, detail))
    task = task_for(cfg, workspace, charter, "src/acme_harvest/grading.py")
    result = engine(cfg, fake, box).run(task, workspace.path, charter)
    assert not result.success and result.summary.startswith(reason)
    assert result.tokens_used == 1000 + 31_000 and result.cost_usd == 0.21
    assert "download" not in [c[0] for c in box.calls]
    assert paths(fake)[-1] == "/key/delete"


def test_planner_failure_deletes_key(
    cfg: Config, fake: FakeLiteLLM, workspace: GitRepo, charter: Charter
) -> None:
    fake.fail["/v1/chat/completions"] = (400, '{"error": "budget exceeded"}')
    box = FakeSandbox()
    task = task_for(cfg, workspace, charter, "src/acme_harvest/grading.py")
    result = engine(cfg, fake, box).run(task, workspace.path, charter)
    assert not result.success and result.summary.startswith("planner failed")
    assert box.opened == 0
    assert paths(fake)[-1] == "/key/delete"


def test_empty_plan_fails(
    cfg: Config, fake: FakeLiteLLM, workspace: GitRepo, charter: Charter
) -> None:
    fake.chat_response = {"choices": [{"message": {"content": "  "}}]}
    box = FakeSandbox()
    task = task_for(cfg, workspace, charter, "src/acme_harvest/grading.py")
    result = engine(cfg, fake, box).run(task, workspace.path, charter)
    assert not result.success and "empty plan" in result.summary and box.opened == 0


def test_sandbox_error_is_reported_and_key_deleted(
    cfg: Config, fake: FakeLiteLLM, workspace: GitRepo, charter: Charter
) -> None:
    box = FakeSandbox(raise_on_agent=RuntimeError("docker went away"))
    task = task_for(cfg, workspace, charter, "src/acme_harvest/grading.py")
    result = engine(cfg, fake, box).run(task, workspace.path, charter)
    assert not result.success
    assert result.summary == "sandbox error: RuntimeError: docker went away"
    assert box.closed == 1 and paths(fake)[-1] == "/key/delete"


def test_workspace_setup_failure(
    cfg: Config, fake: FakeLiteLLM, workspace: GitRepo, charter: Charter
) -> None:
    box = FakeSandbox(prep_exit=2)
    task = task_for(cfg, workspace, charter, "src/acme_harvest/grading.py")
    result = engine(cfg, fake, box).run(task, workspace.path, charter)
    assert not result.success and "workspace setup failed" in result.summary
    assert "agent" not in [c[0] for c in box.calls]


def test_key_creation_failure(
    cfg: Config, fake: FakeLiteLLM, workspace: GitRepo, charter: Charter
) -> None:
    fake.fail["/key/generate"] = (500, "down")
    task = task_for(cfg, workspace, charter, "src/acme_harvest/grading.py")
    result = engine(cfg, fake, FakeSandbox()).run(task, workspace.path, charter)
    assert not result.success and result.summary.startswith("gateway:")
    assert paths(fake) == ["/key/generate"]


def test_spend_unknown_falls_back_to_estimate(
    cfg: Config, fake: FakeLiteLLM, workspace: GitRepo, charter: Charter, tmp_path: Path
) -> None:
    fake.key_info = {}
    fake.fail["/key/delete"] = (500, "down")  # delete failing must not break the result
    box = FakeSandbox(patch=patch_from(workspace, tmp_path, add_test))
    task = task_for(cfg, workspace, charter, "src/acme_harvest/grading.py")
    result = engine(cfg, fake, box).run(task, workspace.path, charter)
    assert result.success and result.cost_usd == 0.2
    assert any("gateway spend unknown" in r for r in result.risks)


# --- prompts ------------------------------------------------------------------------------


def test_planner_prompt_has_charter_rules_target_and_tests(
    cfg: Config, workspace: GitRepo, charter: Charter
) -> None:
    task = task_for(cfg, workspace, charter, "src/acme_harvest/grading.py")
    system, user = planner_messages(task, charter, cfg.repos[0], workspace.path)
    assert system["role"] == "system" and "ONE" in system["content"]
    body = user["content"]
    assert "Cited domain: grading" in body
    assert "traceability chain must never break" in body
    assert "# AGENTS.md" in body
    assert "# Target file: src/acme_harvest/grading.py" in body and "def grade(" in body
    assert "# Existing tests: tests/test_grading.py" in body


def test_prompt_parts_are_capped(cfg: Config, workspace: GitRepo, charter: Charter) -> None:
    (workspace.path / "src/acme_harvest/grading.py").write_text("x = 1\n" * 10_000, "utf-8")
    task = task_for(cfg, workspace, charter, "src/acme_harvest/grading.py")
    body = planner_messages(task, charter, cfg.repos[0], workspace.path)[1]["content"]
    assert "[truncated, 60000 chars]" in body and len(body) < 60_000


def test_related_tests_only_under_test_paths(cfg: Config, workspace: GitRepo) -> None:
    rels = related_tests(workspace.path, cfg.repos[0], "src/acme_harvest/grading.py")
    assert rels == ["tests/test_grading.py"]
    assert related_tests(workspace.path, cfg.repos[0], "") == []


def test_coder_prompt_rules(cfg: Config, workspace: GitRepo, charter: Charter) -> None:
    task = task_for(cfg, workspace, charter, "src/acme_harvest/grading.py")
    prompt = coder_prompt(task, cfg.repos[0], PLAN)
    assert PLAN in prompt
    assert "ONLY under: tests/**" in prompt
    assert "one behaviour each" in prompt
    assert "pytest -q -p no:cacheprovider" in prompt
    assert "Do not commit" in prompt


# --- registry -----------------------------------------------------------------------------


def test_get_engine_openhands(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util

    from grafter.credentials import SecretError
    from grafter.engines import get_engine

    with pytest.raises(ValueError, match="needs the config"):
        get_engine("openhands")
    real_find = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a: object() if name == "openhands" else real_find(name, *a),
    )
    monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)
    monkeypatch.delenv("GRAFTER_LITELLM_MASTER_KEY", raising=False)
    monkeypatch.delenv("GRAFTER_LITELLM_MASTER_KEY_FILE", raising=False)
    with pytest.raises(SecretError, match="LITELLM_MASTER_KEY"):
        get_engine("openhands", cfg)
    monkeypatch.setenv("LITELLM_MASTER_KEY", MASTER)
    eng = get_engine("openhands", cfg)
    assert isinstance(eng, OpenHandsEngine) and eng.name == "openhands"
    assert MASTER not in repr(eng.gateway)


def test_get_engine_openhands_without_extra(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util

    from grafter.engines import get_engine

    real_find = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a: None if name == "openhands" else real_find(name, *a),
    )
    with pytest.raises(ImportError, match=r"grafter\[openhands\]"):
        get_engine("openhands", cfg)


# --- line endings -------------------------------------------------------------------------


def test_keep_eol_restores_crlf_on_modified_files_only(tmp_path: Path, git_isolation: None) -> None:
    import sys

    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "crlf.py").write_bytes(b"a = 1\r\nb = 2\r\n")
    (repo / "lf.py").write_bytes(b"x = 1\n")
    (repo / "still_crlf.py").write_bytes(b"keep\r\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "b")
    # What an editor in the container does: rewrite with LF.
    (repo / "crlf.py").write_bytes(b"a = 1\nb = 2\nc = 3\n")
    (repo / "lf.py").write_bytes(b"x = 1\ny = 2\n")
    (repo / "still_crlf.py").write_bytes(b"new\nkeep\r\nmore\n")  # editor mixed: fixed
    (repo / "added.py").write_bytes(b"new\n")  # new file: left as written
    subprocess.run([sys.executable, "-c", KEEP_EOL], cwd=repo, check=True)
    assert (repo / "crlf.py").read_bytes() == b"a = 1\r\nb = 2\r\nc = 3\r\n"
    assert (repo / "lf.py").read_bytes() == b"x = 1\ny = 2\n"
    assert (repo / "still_crlf.py").read_bytes() == b"new\r\nkeep\r\nmore\r\n"
    assert (repo / "added.py").read_bytes() == b"new\n"
    numstat = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "diff", "--numstat", "--", "crlf.py"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert numstat[:2] == ["1", "0"]  # one line added, nothing rewritten
