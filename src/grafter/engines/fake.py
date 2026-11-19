"""Deterministic offline engine for tests and CI (TODO 1C4).

Not registered in `get_engine` (it needs its file contents up front); construct it
directly: `FakeEngine({"tests/test_x.py": "..."}).run(task, workspace, charter)`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path

from grafter.charter import Charter, normalise_path
from grafter.engines.base import RunResult, Task


class FakeEngine:
    """Writes the given repo-relative files into the workspace and reports them."""

    name = "fake"

    def __init__(
        self,
        files: Mapping[str, str],
        summary: str = "fake change",
        risks: Iterable[str] = (),
        tokens: int = 0,
        success: bool = True,
    ) -> None:
        self.files = dict(files)
        self.summary = summary
        self.risks = list(risks)
        self.tokens = tokens
        self.success = success

    def run(self, task: Task, workspace: Path, charter: Charter) -> RunResult:
        # Validate every path before writing anything: UnsafePathError on the first bad one.
        planned = {normalise_path(p): content for p, content in self.files.items()}
        for rel, content in planned.items():
            dest = workspace / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("w", encoding="utf-8", newline="\n") as fh:
                fh.write(content)
        return RunResult(
            success=self.success,
            changed_files=sorted(planned),
            summary=self.summary,
            risks=list(self.risks),
            tokens_used=self.tokens,
        )
