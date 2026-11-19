"""Runtime configuration (grafter.yaml)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from grafter.charter import ChangeType


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


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repos: list[str] = Field(min_length=1)
    branches: Branches = Branches()
    limits: Limits = Limits()
    engine: str = "openhands"
    tier0_auto_merge: bool = False
    llm_gateway_url: str = "http://localhost:4000"
    model: str = ""  # LiteLLM model name via the gateway; empty = gateway default
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

    def focus_for(self, day: date) -> ChangeType:
        return self.nightly_focus[day.toordinal() % len(self.nightly_focus)]


def load_config(path: str | Path) -> Config:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return Config.model_validate(data)
