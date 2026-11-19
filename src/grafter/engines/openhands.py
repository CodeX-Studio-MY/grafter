"""OpenHands Software Agent SDK adapter (MIT packages only)."""

from __future__ import annotations

from pathlib import Path

from grafter.charter import Charter
from grafter.engines.base import RunResult, Task


class OpenHandsEngine:
    name = "openhands"

    def run(self, task: Task, workspace: Path, charter: Charter) -> RunResult:
        # Phase 1: wire openhands.sdk Agent + DockerWorkspace here.
        raise NotImplementedError("OpenHands engine lands in phase 1")
