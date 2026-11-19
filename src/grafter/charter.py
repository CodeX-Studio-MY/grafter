"""Solution Charter: the human-owned rules every change must serve."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path, PurePosixPath

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ChangeType(StrEnum):
    BUGFIX = "bugfix"
    SECURITY = "security"
    PERFORMANCE = "performance"
    TEST = "test"
    REFACTOR = "refactor"
    DOCS = "docs"
    DEPENDENCY = "dependency"


class FeaturePolicy(StrEnum):
    PROPOSE_ONLY = "propose_only"


ALWAYS_PROTECTED = ("grafter.charter.yaml",)


class Charter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    solution: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    core_domains: list[str] = Field(min_length=1)
    users: list[str] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)
    invariants: list[str] = Field(default_factory=list)
    protected_paths: list[str] = Field(default_factory=list)
    review_paths: list[str] = Field(default_factory=list)
    allowed_change_types: list[ChangeType] = Field(min_length=1)
    feature_changes: FeaturePolicy = FeaturePolicy.PROPOSE_ONLY

    @model_validator(mode="after")
    def _paths(self) -> Charter:
        for p in ALWAYS_PROTECTED:
            if p not in self.protected_paths:
                self.protected_paths.append(p)
        overlap = set(self.protected_paths) & set(self.review_paths)
        if overlap:
            raise ValueError(f"paths cannot be both protected and review: {sorted(overlap)}")
        return self

    def is_protected(self, path: str) -> bool:
        return _matches(path, self.protected_paths)

    def needs_review(self, path: str) -> bool:
        return _matches(path, self.review_paths)


def _matches(path: str, prefixes: list[str]) -> bool:
    p = PurePosixPath(path)
    for prefix in prefixes:
        if prefix.endswith("/"):
            if str(p).startswith(prefix) or str(p) + "/" == prefix:
                return True
        elif str(p) == prefix:
            return True
    return False


def load_charter(path: str | Path) -> Charter:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("charter must be a YAML mapping")
    return Charter.model_validate(data)
