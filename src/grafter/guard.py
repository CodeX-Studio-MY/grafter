"""Pre-review checks a change must pass before it becomes a PR."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

from grafter.charter import ChangeType, Charter
from grafter.config import Limits
from grafter.engines.base import RunResult, Task


class Tier(IntEnum):
    AUTO = 0
    APPROVE = 1
    REVIEW = 2


@dataclass
class Verdict:
    allowed: bool
    tier: Tier = Tier.APPROVE
    reasons: list[str] = field(default_factory=list)


def check(task: Task, result: RunResult, charter: Charter, limits: Limits | None = None) -> Verdict:
    limits = limits or Limits()
    reasons: list[str] = []
    touched = [*result.changed_files, *result.deleted_files]

    if not task.charter_ref.strip():
        reasons.append("no charter citation")
    if task.change_type not in charter.allowed_change_types:
        reasons.append(f"change type not allowed: {task.change_type}")

    touched_protected = [f for f in touched if charter.is_protected(f)]
    if touched_protected:
        reasons.append(f"touches protected paths: {touched_protected}")

    if len(touched) > limits.max_files_per_change:
        reasons.append(f"too many files: {len(touched)} > {limits.max_files_per_change}")
    lines = result.lines_added + result.lines_removed
    if lines > limits.max_lines_per_change:
        reasons.append(f"diff too large: {lines} > {limits.max_lines_per_change} lines")

    if reasons:
        return Verdict(allowed=False, reasons=reasons)

    if any(charter.needs_review(f) for f in touched):
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
