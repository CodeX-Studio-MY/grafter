"""Engine that changes nothing: exercises the whole pipeline offline without an LLM."""

from __future__ import annotations

from pathlib import Path

from grafter.charter import Charter
from grafter.engines.base import RunResult, Task


class NoopEngine:
    name = "noop"

    def run(self, task: Task, workspace: Path, charter: Charter) -> RunResult:
        return RunResult(success=False, summary="noop engine: no changes")
