"""Engine adapter: any coding agent that can run a task in a workspace."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from grafter.charter import ChangeType, Charter


@dataclass(frozen=True)
class Task:
    id: str
    repo: str
    change_type: ChangeType
    charter_ref: str
    instruction: str
    version_bump: Literal["patch", "minor", "major"] | None = None
    target: str = ""  # repo-relative file the task is about, when there is one


@dataclass
class RunResult:
    success: bool
    changed_files: list[str] = field(default_factory=list)
    summary: str = ""
    risks: list[str] = field(default_factory=list)
    tokens_used: int = 0
    lines_added: int = 0
    lines_removed: int = 0
    deleted_files: list[str] = field(default_factory=list)
    diff: str = ""
    cost_usd: float = 0.0  # gateway spend for this task (D16), not an SDK estimate


class AgentEngine(Protocol):
    name: str

    def run(self, task: Task, workspace: Path, charter: Charter) -> RunResult: ...
