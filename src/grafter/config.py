"""Runtime configuration (grafter.yaml)."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from grafter.charter import REPO_NAME_PATTERN, ChangeType, normalise_path, normalise_patterns


class Branches(BaseModel):
    model_config = ConfigDict(extra="forbid")

    production: str = "main"
    preview: str = "ai/preview"
    fix_prefix: str = "ai/"


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_prs_per_night: int = Field(default=3, ge=1, le=10)
    token_budget_per_night: int = Field(default=500_000, gt=0)
    max_files_per_change: int = Field(default=20, ge=1)
    max_lines_per_change: int = Field(default=400, ge=1)
    usd_budget_per_night: float = Field(default=5.0, gt=0)  # hard cap on the LiteLLM key


class Models(BaseModel):
    """LiteLLM model name per agent role (D15). Reasoning roles use Opus, coding uses Sonnet."""

    model_config = ConfigDict(extra="forbid")

    planner: str = Field(default="claude-opus-5-5", min_length=1)  # find work, plan the change
    coder: str = Field(default="claude-sonnet-5-5", min_length=1)  # write code and tests
    reviewer: str = Field(default="claude-opus-5-5", min_length=1)  # review the diff
    judge: str = Field(default="claude-opus-5-5", min_length=1)  # charter alignment


_TRUTHY = frozenset({"1", "true", "yes", "on"})
_SECRET_PARTS = ("private_key", "secret", "password", "token")
_SECRET_ALLOW = frozenset({"token_budget_per_night"})
_URL_USERINFO = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/@]*@")


# Agent sandbox image (TODO 1D2c): the OpenHands agent server pinned by digest (SDK 1.60.1,
# spike 1C2) plus test tools; built by `scripts/build-sandbox.sh agent-python` from
# sandboxes/agent-python/Dockerfile. Bump its FROM together with the `openhands` extra.
OPENHANDS_IMAGE = "grafter-sandbox-agent-python:latest"


class OpenHands(BaseModel):
    """`engine: openhands` settings (TODO 1C3). The container reaches the gateway at
    `gateway_url` over `network`; it never sees the master or provider key."""

    model_config = ConfigDict(extra="forbid")

    image: str = Field(default=OPENHANDS_IMAGE, min_length=1)
    network: str = Field(default="grafter-gw", min_length=1)
    gateway_url: str = Field(default="http://litellm:4000", min_length=1)
    max_iterations: int = Field(default=60, ge=1, le=500)
    timeout_s: int = Field(default=1800, ge=60)
    plan_max_output: int = Field(default=2000, ge=100)  # planner reply cap, in tokens
    coder_max_output: int = Field(default=8192, ge=256)  # per agent call, in tokens
    # 1D2c: the agent server sits only on the internal `network`; this relay publishes it.
    relay_image: str = Field(default="python:3.12.15-slim-bookworm", min_length=1)
    memory: str = Field(default="4g", min_length=1)
    cpus: str = Field(default="2", min_length=1)
    health_timeout_s: int = Field(default=120, ge=5)


class KillSwitch(BaseModel):
    """Stops all runs when the file exists or the env var is truthy."""

    model_config = ConfigDict(extra="forbid")

    file: str = "~/.grafter/STOP"
    env: str = "GRAFTER_STOP"

    def is_engaged(self, environ: Mapping[str, str] | None = None) -> bool:
        env = os.environ if environ is None else environ
        if env.get(self.env, "").strip().lower() in _TRUTHY:
            return True
        return Path(os.path.expandvars(self.file)).expanduser().exists()


class GitHubApp(BaseModel):
    """GitHub App ids only; the private key comes from the environment."""

    model_config = ConfigDict(extra="forbid")

    app_id: int | None = None
    installation_id: int | None = None
    api_url: str = Field(default="https://api.github.com", pattern=r"^https?://")


def _is_secret_key(key: str) -> bool:
    k = key.lower()
    if k in _SECRET_ALLOW:
        return False
    return k.endswith("_key") or any(part in k for part in _SECRET_PARTS)


def _find_secret_keys(data: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(data, Mapping):
        for key, value in data.items():
            here = f"{path}.{key}" if path else str(key)
            if isinstance(key, str) and _is_secret_key(key):
                found.append(here)
            found.extend(_find_secret_keys(value, here))
    elif isinstance(data, list | tuple):
        for i, item in enumerate(data):
            found.extend(_find_secret_keys(item, f"{path}[{i}]"))
    return found


class RepoConfig(BaseModel):
    """One repo of the solution and how to test it."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=REPO_NAME_PATTERN)  # short id, e.g. `api`; charter `repos:` key
    github: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")  # owner/repo
    default_branch: str = Field(default="main", min_length=1)
    language: str = Field(min_length=1)  # free text: dotnet, node, flutter, python, ...
    test_command: str = Field(min_length=1)
    coverage_command: str = ""
    coverage_report: str = ""  # repo-relative file coverage_command writes; empty = none
    coverage_format: Literal["coverage-json", "lcov"] = "coverage-json"
    test_paths: list[str] = Field(min_length=1)  # globs; a `test` change may touch only these
    sandbox_image: str = ""  # Docker image; empty = engine default
    # Clone from here instead of github.com (local path or URL without credentials).
    # For offline testing; real runs leave it empty and clone with the App token.
    clone_url: str = ""

    @field_validator("clone_url")
    @classmethod
    def _no_credentials(cls, v: str) -> str:
        if _URL_USERINFO.search(v):
            raise ValueError("clone_url must not contain credentials (user:token@)")
        return v

    @field_validator("test_paths")
    @classmethod
    def _norm_test_paths(cls, v: list[str]) -> list[str]:
        return normalise_patterns(v)

    @field_validator("coverage_report")
    @classmethod
    def _norm_coverage_report(cls, v: str) -> str:
        return normalise_path(v) if v else v


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    home: str = Field(default="~/.grafter", min_length=1)  # runs, lock, db, work; ~ and $VARS ok
    repos: list[RepoConfig] = Field(min_length=1)
    branches: Branches = Branches()
    limits: Limits = Limits()
    engine: str = "openhands"
    openhands: OpenHands = OpenHands()
    tier0_auto_merge: bool = False
    llm_gateway_url: str = "http://localhost:4000"
    models: Models = Models()
    dry_run: bool = False
    kill_switch: KillSwitch = KillSwitch()
    nightly_focus: list[ChangeType] = Field(default_factory=lambda: [ChangeType.TEST], min_length=1)
    github_app: GitHubApp = GitHubApp()
    charter_repo: str = ""  # repo `name` whose work tree holds the charter; empty = first repo
    charter_file: str = "grafter.charter.yaml"  # path inside the charter repo
    runner: Literal["docker", "local"] = "docker"  # local = repo commands on this host (tests)
    git_author_name: str = Field(default="Grafter", min_length=1)
    git_author_email: str = Field(default="grafter-bot@users.noreply.github.com", min_length=3)

    @field_validator("charter_file")
    @classmethod
    def _norm_charter_file(cls, v: str) -> str:
        return normalise_path(v)

    @model_validator(mode="before")
    @classmethod
    def _reject_secrets(cls, data: Any) -> Any:
        found = _find_secret_keys(data)
        if found:
            raise ValueError(
                f"secrets are not allowed in config (keys: {', '.join(found)}); "
                "pass them via environment variables instead"
            )
        return data

    @model_validator(mode="after")
    def _unique_repo_names(self) -> Config:
        names = [r.name for r in self.repos]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate repo names: {dupes}")
        if self.charter_repo and self.charter_repo not in names:
            raise ValueError(
                f"charter_repo {self.charter_repo!r} is not a configured repo ({names})"
            )
        return self

    def charter_repo_config(self) -> RepoConfig:
        """The repo whose work tree holds the charter (`charter_repo`, else the first)."""
        return self.repo(self.charter_repo) if self.charter_repo else self.repos[0]

    def repo(self, name: str) -> RepoConfig:
        for r in self.repos:
            if r.name == name:
                return r
        known = ", ".join(r.name for r in self.repos)
        raise KeyError(f"unknown repo {name!r}; configured repos: {known}")

    def focus_for(self, day: date) -> ChangeType:
        return self.nightly_focus[day.toordinal() % len(self.nightly_focus)]

    def paths(self) -> Paths:
        return Paths.under(self.home)


@dataclass(frozen=True)
class Paths:
    """Grafter's local state, all under `Config.home` (`~` and env vars expanded)."""

    home: Path
    runs_dir: Path
    lock_file: Path
    db_file: Path
    work_dir: Path

    @classmethod
    def under(cls, home: str | Path) -> Paths:
        root = Path(os.path.expandvars(str(home))).expanduser()
        return cls(
            home=root,
            runs_dir=root / "runs",
            lock_file=root / "grafter.lock",
            db_file=root / "grafter.db",
            work_dir=root / "work",
        )


def load_config(path: str | Path) -> Config:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return Config.model_validate(data)
