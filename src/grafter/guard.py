"""Pre-review checks a change must pass before it becomes a PR."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

from grafter.charter import ChangeType, Charter
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


def check(task: Task, result: RunResult, charter: Charter) -> Verdict:
    reasons: list[str] = []

    if not task.charter_ref.strip():
        reasons.append("no charter citation")
    if task.change_type not in charter.allowed_change_types:
        reasons.append(f"change type not allowed: {task.change_type}")

    touched_protected = [f for f in result.changed_files if charter.is_protected(f)]
    if touched_protected:
        reasons.append(f"touches protected paths: {touched_protected}")

    if reasons:
        return Verdict(allowed=False, reasons=reasons)

    if any(charter.needs_review(f) for f in result.changed_files):
        return Verdict(allowed=True, tier=Tier.REVIEW, reasons=["touches review paths"])
    if task.change_type in (ChangeType.DEPENDENCY, ChangeType.SECURITY) and len(
        result.changed_files
    ) <= 2:
        return Verdict(allowed=True, tier=Tier.AUTO)
    return Verdict(allowed=True, tier=Tier.APPROVE)
