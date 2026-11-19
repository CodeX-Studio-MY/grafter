"""Runtime configuration (grafter.yaml)."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class Branches(BaseModel):
    model_config = ConfigDict(extra="forbid")

    production: str = "main"
    preview: str = "ai/preview"
    fix_prefix: str = "ai/"


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_prs_per_night: int = Field(default=3, ge=1, le=10)
    token_budget_per_night: int = Field(default=500_000, gt=0)


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repos: list[str] = Field(min_length=1)
    branches: Branches = Branches()
    limits: Limits = Limits()
    engine: str = "openhands"
    tier0_auto_merge: bool = False
    llm_gateway_url: str = "http://localhost:4000"


def load_config(path: str | Path) -> Config:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return Config.model_validate(data)
