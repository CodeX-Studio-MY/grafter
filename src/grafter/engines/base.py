"""Engine adapter: any coding agent that can run a task in a workspace."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from grafter.charter import ChangeType, Charter


@dataclass(frozen=True)
class Task:
    id: str
    repo: str
    change_type: ChangeType
    charter_ref: str
    instruction: str


@dataclass
class RunResult:
    success: bool
    changed_files: list[str] = field(default_factory=list)
    summary: str = ""
    risks: list[str] = field(default_factory=list)
    tokens_used: int = 0


class AgentEngine(Protocol):
    name: str

    def run(self, task: Task, workspace: Path, charter: Charter) -> RunResult: ...
