"""Runtime configuration (grafter.yaml)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from grafter.charter import REPO_NAME_PATTERN, ChangeType, normalise_patterns


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
    test_paths: list[str] = Field(min_length=1)  # globs; a `test` change may touch only these
    sandbox_image: str = ""  # Docker image; empty = engine default

    @field_validator("test_paths")
    @classmethod
    def _norm_test_paths(cls, v: list[str]) -> list[str]:
        return normalise_patterns(v)


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    home: str = Field(default="~/.grafter", min_length=1)  # runs, lock, db, work; ~ and $VARS ok
    repos: list[RepoConfig] = Field(min_length=1)
    branches: Branches = Branches()
    limits: Limits = Limits()
    engine: str = "openhands"
    tier0_auto_merge: bool = False
    llm_gateway_url: str = "http://localhost:4000"
    models: Models = Models()
    dry_run: bool = False
    kill_switch: KillSwitch = KillSwitch()
    nightly_focus: list[ChangeType] = Field(default_factory=lambda: [ChangeType.TEST], min_length=1)
    github_app: GitHubApp = GitHubApp()

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
        return self

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
