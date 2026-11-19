"""Shared fixtures for the offline end-to-end tests (pipeline, CLI run, doctor)."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from grafter.config import Config
from grafter.runner import CmdResult, CommandRunner, LocalRunner

ROOT = Path(__file__).parent.parent
SANDBOX = ROOT / "examples/sandbox-repo"
GITHUB_REPO = "acme/grafter-sandbox"


def git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "-c", "core.autocrlf=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return out.stdout.strip()


def bare_refs(bare: Path) -> dict[str, str]:
    out = git(bare, "for-each-ref", "--format=%(refname) %(objectname)")
    refs = (line.split(" ", 1) for line in out.splitlines() if line)
    return {name: sha for name, sha in refs}


@pytest.fixture
def git_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the user's global/system git config out of the tests."""
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[init]\n\tdefaultBranch = main\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture
def sandbox_origin(tmp_path: Path, git_isolation: None) -> Path:
    """Bare origin holding one commit of examples/sandbox-repo on main."""
    seed = tmp_path / "seed"
    shutil.copytree(
        SANDBOX,
        seed,
        ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", ".pytest_cache", ".coverage", "coverage.json"
        ),
    )
    git(seed, "init", "-q", "-b", "main")
    git(seed, "add", "-A")
    git(
        seed,
        "-c",
        "user.name=Seed",
        "-c",
        "user.email=seed@example.invalid",
        "commit",
        "-qm",
        "init",
    )
    bare = tmp_path / "origin.git"
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    return bare


def config_data(tmp_path: Path, origin: Path, **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "home": str(tmp_path / "home"),
        "repos": [
            {
                "name": "api",
                "github": GITHUB_REPO,
                "language": "python",
                "test_command": "pytest -q -p no:cacheprovider",
                "coverage_command": (
                    "pytest -q -p no:cacheprovider --cov --cov-report=json:coverage.json"
                ),
                "coverage_report": "coverage.json",
                "coverage_format": "coverage-json",
                "test_paths": ["tests/**"],
                "clone_url": str(origin),
            }
        ],
        "runner": "local",
        "engine": "noop",
        "kill_switch": {"file": str(tmp_path / "STOP"), "env": "GRAFTER_TEST_STOP"},
        "github_app": {"app_id": None, "installation_id": None},
    }
    data.update(overrides)
    return data


@pytest.fixture
def make_cfg(tmp_path: Path, sandbox_origin: Path) -> Callable[..., Config]:
    def make(**overrides: Any) -> Config:
        return Config.model_validate(config_data(tmp_path, sandbox_origin, **overrides))

    return make


_SKIP_PARTS = frozenset({".git", "__pycache__", ".pytest_cache", ".coverage", "coverage.json"})
_RUN_CACHE: dict[tuple[str, str], tuple[CmdResult, bytes | None]] = {}


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if _SKIP_PARTS.intersection(rel.parts) or not path.is_file():
            continue
        h.update(rel.as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return h.hexdigest()


class CachingRunner:
    """`LocalRunner` that runs each (command, work tree content) once per test session and
    replays the result (and the coverage.json it wrote) afterwards. Real pytest still runs
    for every distinct tree; it just isn't repeated across tests."""

    def __init__(self, inner: CommandRunner | None = None, report: str = "coverage.json") -> None:
        self.inner = inner or LocalRunner()
        self.report = report

    def run(self, cmd: str, cwd: Path, timeout: float = 900) -> CmdResult:
        key = (cmd, tree_hash(cwd))
        hit = _RUN_CACHE.get(key)
        report = cwd / self.report
        if hit is None:
            res = self.inner.run(cmd, cwd, timeout)
            hit = (res, report.read_bytes() if report.is_file() else None)
            _RUN_CACHE[key] = hit
        elif hit[1] is not None:
            report.write_bytes(hit[1])
        return hit[0]
