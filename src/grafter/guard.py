"""Pre-review checks a change must pass before it becomes a PR."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntEnum

from grafter.charter import ChangeType, Charter, matches_any
from grafter.config import Limits, RepoConfig
from grafter.engines.base import RunResult, Task
from grafter.secret_scan import scan_diff


class Tier(IntEnum):
    AUTO = 0
    APPROVE = 1
    REVIEW = 2


@dataclass
class Verdict:
    allowed: bool
    tier: Tier = Tier.APPROVE
    reasons: list[str] = field(default_factory=list)


# Lines that declare a test. A `test` change may not remove them (diff lines with `-`).
# `(?<![\w.])` keeps `m.group(` / `submit(` from matching.
TEST_DECLARATIONS = tuple(
    re.compile(p)
    for p in (
        r"^\s*(?:async\s+)?def\s+test_",  # Python
        r"\[\s*(?:Fact|Theory|Test|TestMethod)\b",  # C# xUnit / NUnit / MSTest
        r"(?<![\w.])(?:it|test|describe)\s*\(",  # JS/TS (Jest, Vitest, Mocha)
        r"(?<![\w.])(?:testWidgets|group)\s*\(",  # Dart (`test(` is covered above)
    )
)

# Lines that skip tests. A `test` change may not add them (diff lines with `+`).
TEST_SKIPS = tuple(
    re.compile(p)
    for p in (
        r"@pytest\.mark\.skip",  # Python (also skipif)
        r"\bpytest\.skip\s*\(",
        r"(?<![\w.])(?:it|test|describe)\.skip\s*\(",  # JS/TS
        r"(?<![\w.])x(?:it|test|describe)\s*\(",
        r"\bSkip\s*=",  # C# [Fact(Skip = "...")]
        r"\bskip\s*:\s*true\b",  # Dart
    )
)


def _diff_lines(diff: str, sign: str) -> list[str]:
    header = sign * 3
    return [ln[1:] for ln in diff.splitlines() if ln.startswith(sign) and not ln.startswith(header)]


def _test_change_reasons(result: RunResult, repo: RepoConfig | None) -> list[str]:
    if repo is None:
        return ["test change needs repo config"]
    reasons: list[str] = []
    outside = [f for f in result.changed_files if not matches_any(f, repo.test_paths)]
    if outside:
        reasons.append(f"non-test files in test change: {outside}")
    if result.deleted_files:
        reasons.append(f"test changes may not delete files: {result.deleted_files}")
    removed = _diff_lines(result.diff, "-")
    if any(rx.search(ln) for ln in removed for rx in TEST_DECLARATIONS):
        reasons.append("removes test declarations")
    added = _diff_lines(result.diff, "+")
    if any(rx.search(ln) for ln in added for rx in TEST_SKIPS):
        reasons.append("skips tests")
    return reasons


def check(
    task: Task,
    result: RunResult,
    charter: Charter,
    limits: Limits | None = None,
    repo: RepoConfig | None = None,
) -> Verdict:
    limits = limits or Limits()
    reasons: list[str] = []
    touched = [*result.changed_files, *result.deleted_files]

    if not task.charter_ref.strip():
        reasons.append("no charter citation")
    if task.change_type not in charter.allowed_change_types:
        reasons.append(f"change type not allowed: {task.change_type}")

    touched_protected = [f for f in touched if charter.is_protected(f, task.repo)]
    if touched_protected:
        reasons.append(f"touches protected paths: {touched_protected}")

    if len(touched) > limits.max_files_per_change:
        reasons.append(f"too many files: {len(touched)} > {limits.max_files_per_change}")
    lines = result.lines_added + result.lines_removed
    if lines > limits.max_lines_per_change:
        reasons.append(f"diff too large: {lines} > {limits.max_lines_per_change} lines")

    if repo is not None and repo.name != task.repo:
        reasons.append(f"repo config mismatch: task repo {task.repo!r}, config {repo.name!r}")
    if task.change_type == ChangeType.TEST:
        reasons.extend(_test_change_reasons(result, repo))

    for finding in scan_diff(result.diff):
        reasons.append(f"secret detected: {finding.rule} (line {finding.line_no})")

    if reasons:
        return Verdict(allowed=False, reasons=reasons)

    if any(charter.needs_review(f, task.repo) for f in touched):
        return Verdict(allowed=True, tier=Tier.REVIEW, reasons=["touches review paths"])
    # Tier 0 is only a patch-level dependency bump touching at most 2 files.
    # Security changes are never Tier 0 (see docs/autonomy.md).
    if (
        task.change_type == ChangeType.DEPENDENCY
        and task.version_bump == "patch"
        and len(touched) <= 2
    ):
        return Verdict(allowed=True, tier=Tier.AUTO)
    return Verdict(allowed=True, tier=Tier.APPROVE)
