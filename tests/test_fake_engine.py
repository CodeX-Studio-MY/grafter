"""FakeEngine: deterministic offline engine for pipeline tests (TODO 1C4)."""

from pathlib import Path

import pytest

from grafter.charter import ChangeType, Charter, UnsafePathError
from grafter.engines.base import AgentEngine, Task
from grafter.engines.fake import FakeEngine

CHARTER = Charter(
    solution="S", purpose="p", core_domains=["grading"], allowed_change_types=[ChangeType.TEST]
)
TASK = Task(
    id="t",
    repo="api",
    change_type=ChangeType.TEST,
    charter_ref="domain: grading",
    instruction="add tests",
)


def test_writes_files_and_reports_them(tmp_path: Path) -> None:
    eng = FakeEngine(
        {"tests/b/test_x.py": "x = 1\n", "tests\\test_a.py": "a = 2\n"},
        summary="add tests",
        risks=["none"],
        tokens=42,
    )
    r = eng.run(TASK, tmp_path, CHARTER)
    assert r.success is True
    assert r.changed_files == ["tests/b/test_x.py", "tests/test_a.py"]
    assert r.summary == "add tests"
    assert r.risks == ["none"]
    assert r.tokens_used == 42
    assert (tmp_path / "tests/b/test_x.py").read_text(encoding="utf-8") == "x = 1\n"
    assert (tmp_path / "tests/test_a.py").read_text(encoding="utf-8") == "a = 2\n"


def test_overwrites_existing_file(tmp_path: Path) -> None:
    (tmp_path / "f.py").write_text("old", encoding="utf-8")
    FakeEngine({"f.py": "new"}).run(TASK, tmp_path, CHARTER)
    assert (tmp_path / "f.py").read_text(encoding="utf-8") == "new"


def test_failure_result(tmp_path: Path) -> None:
    r = FakeEngine({}, success=False).run(TASK, tmp_path, CHARTER)
    assert r.success is False
    assert r.changed_files == []
    assert r.summary == "fake change"


@pytest.mark.parametrize("bad", ["../escape.py", "/abs.py", "C:/x.py", "a/../../b.py", ""])
def test_refuses_unsafe_paths_before_writing(tmp_path: Path, bad: str) -> None:
    eng = FakeEngine({"tests/ok.py": "ok", bad: "evil"})
    with pytest.raises(UnsafePathError):
        eng.run(TASK, tmp_path, CHARTER)
    assert not (tmp_path / "tests/ok.py").exists()


def test_is_an_agent_engine() -> None:
    eng: AgentEngine = FakeEngine({})
    assert eng.name == "fake"
