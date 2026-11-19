"""Solution Charter: the human-owned rules every change must serve."""

from __future__ import annotations

import re
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

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


# Locked for every charter, whatever its protected_paths say (appended, deduped).
ALWAYS_PROTECTED = ("grafter.charter.yaml", "grafter.yaml", "**/AGENTS.md", ".github/", ".grafter/")


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
        self.protected_paths = _dedupe(
            [_normalise_pattern(p) for p in [*self.protected_paths, *ALWAYS_PROTECTED]]
        )
        self.review_paths = _dedupe([_normalise_pattern(p) for p in self.review_paths])
        overlap = {p.casefold() for p in self.protected_paths} & {
            p.casefold() for p in self.review_paths
        }
        if overlap:
            raise ValueError(f"paths cannot be both protected and review: {sorted(overlap)}")
        return self

    def is_protected(self, path: str) -> bool:
        """True if `path` is protected. Unsafe paths fail closed (True)."""
        return _matches(path, self.protected_paths)

    def needs_review(self, path: str) -> bool:
        """True if `path` needs human review. Unsafe paths fail closed (True)."""
        return _matches(path, self.review_paths)


class UnsafePathError(ValueError):
    """A path that is empty, absolute, escapes the repo root, or is ambiguous on some OS."""


_DRIVE = re.compile(r"^[A-Za-z]:")


def normalise_path(path: str) -> str:
    """Return a repo-relative POSIX path, resolved lexically.

    Backslashes become `/`, repeated `/` collapse, `.` segments drop and `..` resolves.
    Raises UnsafePathError for empty, absolute, drive-letter, UNC, NUL-containing or
    root-escaping paths, and for segments Windows would silently rewrite (trailing
    `.` or space) or treat as a stream (`:`). Callers compare case-insensitively.
    """
    if "\x00" in path:
        raise UnsafePathError("path contains NUL byte")
    p = path.replace("\\", "/")
    if not p.strip():
        raise UnsafePathError("empty path")
    if p.startswith("/"):
        raise UnsafePathError(f"absolute or UNC path: {path!r}")
    if _DRIVE.match(p):
        raise UnsafePathError(f"drive-letter path: {path!r}")
    out: list[str] = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if not out:
                raise UnsafePathError(f"path escapes the root: {path!r}")
            out.pop()
            continue
        if ":" in seg or seg[-1] in ". ":
            raise UnsafePathError(f"ambiguous path segment {seg!r} in {path!r}")
        out.append(seg)
    if not out:
        raise UnsafePathError(f"path resolves to the root: {path!r}")
    return "/".join(out)


def _normalise_pattern(pattern: str) -> str:
    is_dir = pattern.replace("\\", "/").endswith("/")
    norm = normalise_path(pattern)
    return norm + "/" if is_dir else norm


def _dedupe(patterns: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for p in patterns:
        if p.casefold() not in seen:
            seen.add(p.casefold())
            out.append(p)
    return out


@lru_cache(maxsize=1024)
def _compile(pattern: str) -> re.Pattern[str]:
    """Glob -> regex. `*` and `?` stay inside one segment, `**` spans zero or more
    segments, a trailing `/` matches that directory, its bare name and everything under
    it. A pattern without glob chars or trailing `/` is an exact path."""
    is_dir = pattern.endswith("/")
    segs = pattern.rstrip("/").split("/")
    rx = ""
    for i, seg in enumerate(segs):
        last = i == len(segs) - 1
        if seg == "**":
            rx += ".+" if last else "(?:[^/]+/)*"
            continue
        for ch in seg:
            rx += "[^/]*" if ch == "*" else "[^/]" if ch == "?" else re.escape(ch)
        if not last:
            rx += "/"
    if is_dir:
        rx += "(?:/.*)?"
    return re.compile(rx, re.IGNORECASE | re.DOTALL)


def _matches(path: str, patterns: list[str]) -> bool:
    try:
        norm = normalise_path(path)
    except UnsafePathError:
        return True
    return any(_compile(p).fullmatch(norm) for p in patterns)


def load_charter(path: str | Path) -> Charter:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("charter must be a YAML mapping")
    return Charter.model_validate(data)
