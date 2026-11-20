"""Verification gates (TODO 1D2), run for real on a copy of the sandbox repo."""

import shutil
from collections.abc import Sequence
from pathlib import Path

import pytest

from grafter.charter import ChangeType, load_charter
from grafter.config import RepoConfig, load_config
from grafter.engines.base import Task
from grafter.engines.fake import FakeEngine
from grafter.finder import CoverageSnapshot, measure_coverage
from grafter.gates import GateResult, run_gates
from grafter.runner import CmdResult, LocalRunner

ROOT = Path(__file__).parent.parent
SANDBOX = ROOT / "examples/sandbox-repo"
CONFIG = ROOT / "deploy/grafter.sandbox.yaml"
TARGET = "src/acme_harvest/traceability.py"
TASK = Task(
    id="t1",
    repo="api",
    change_type=ChangeType.TEST,
    charter_ref="domain: traceability",
    instruction="add tests",
)

GOOD_TEST = """\
from datetime import date

import pytest

from acme_harvest.lots import Lot
from acme_harvest.traceability import BrokenChainError, chain, origin

D = date(2026, 11, 18)


def lot(tid: str, parent: str | None = None) -> Lot:
    return Lot(tid, "Green Valley", "mango", 10.0, D, parent)


def test_chain_walks_to_origin() -> None:
    lots = {"a": lot("a"), "a.1": lot("a.1", "a")}
    assert [x.trace_id for x in chain(lots, "a.1")] == ["a.1", "a"]
    assert origin(lots, "a.1").trace_id == "a"


def test_missing_parent_breaks_chain() -> None:
    with pytest.raises(BrokenChainError):
        chain({"a.1": lot("a.1", "a")}, "a.1")


def test_loop_breaks_chain() -> None:
    with pytest.raises(BrokenChainError):
        chain({"a": lot("a", "b"), "b": lot("b", "a")}, "a")
"""

FAILING_TEST = """\
def test_broken() -> None:
    assert 1 == 2
"""

UNRELATED_TEST = """\
from acme_harvest.grading import Grade, grade


def test_reject() -> None:
    assert grade(defect_pct=50, brix=12) == Grade.REJECT
"""

UNCOVERED_SOURCE = "\n".join(f"def f{i}() -> int:\n    return {i}\n" for i in range(40))


@pytest.fixture(scope="module")
def repo() -> RepoConfig:
    return load_config(CONFIG).repo("api")


@pytest.fixture(scope="module")
def baseline(tmp_path_factory: pytest.TempPathFactory, repo: RepoConfig) -> CoverageSnapshot:
    work = tmp_path_factory.mktemp("baseline") / "sandbox"
    shutil.copytree(SANDBOX, work)
    return measure_coverage(LocalRunner(), work, repo)


def apply(tmp_path: Path, files: dict[str, str]) -> Path:
    work = tmp_path / "sandbox"
    shutil.copytree(SANDBOX, work, ignore=shutil.ignore_patterns("__pycache__", ".*"))
    charter = load_charter(work / "grafter.charter.yaml")
    assert FakeEngine(files).run(TASK, work, charter).success
    return work


def test_good_test_passes_and_target_improves(
    tmp_path: Path, repo: RepoConfig, baseline: CoverageSnapshot
) -> None:
    work = apply(tmp_path, {"tests/test_traceability.py": GOOD_TEST})
    g = run_gates(LocalRunner(), work, repo, baseline, TARGET)
    assert g.passed, g.reasons
    assert g.reasons == []
    assert "passed" in g.tests_summary
    assert g.coverage_before == baseline.total
    assert g.coverage_after is not None and g.coverage_after > baseline.total
    assert g.target_before == 0.0
    assert g.target_after == 100.0


def test_failing_test_rejected(
    tmp_path: Path, repo: RepoConfig, baseline: CoverageSnapshot
) -> None:
    work = apply(tmp_path, {"tests/test_traceability.py": GOOD_TEST + "\n\n" + FAILING_TEST})
    g = run_gates(LocalRunner(), work, repo, baseline, TARGET)
    assert not g.passed
    assert len(g.reasons) == 1
    assert g.reasons[0].startswith("tests failed (exit 1)")
    assert "test_broken" in g.reasons[0]
    assert "failed" in g.tests_summary
    assert g.coverage_after is None


def test_test_that_misses_target_rejected(
    tmp_path: Path, repo: RepoConfig, baseline: CoverageSnapshot
) -> None:
    work = apply(tmp_path, {"tests/test_more_grading.py": UNRELATED_TEST})
    g = run_gates(LocalRunner(), work, repo, baseline, TARGET)
    assert not g.passed
    assert g.reasons == [f"target file coverage did not improve: {TARGET} 0.00% -> 0.00%"]
    assert g.target_after == 0.0


def test_no_target_only_checks_suite_and_total(
    tmp_path: Path, repo: RepoConfig, baseline: CoverageSnapshot
) -> None:
    work = apply(tmp_path, {"tests/test_more_grading.py": UNRELATED_TEST})
    g = run_gates(LocalRunner(), work, repo, baseline, None)
    assert g.passed, g.reasons
    assert g.target_before is None and g.target_after is None


def test_coverage_drop_rejected(
    tmp_path: Path, repo: RepoConfig, baseline: CoverageSnapshot
) -> None:
    work = apply(tmp_path, {"src/acme_harvest/extra.py": UNCOVERED_SOURCE})
    g = run_gates(LocalRunner(), work, repo, baseline, None)
    assert not g.passed
    assert len(g.reasons) == 1
    assert g.reasons[0].startswith("coverage dropped:")


def test_missing_report_rejected(
    tmp_path: Path, repo: RepoConfig, baseline: CoverageSnapshot
) -> None:
    work = apply(tmp_path, {"tests/test_traceability.py": GOOD_TEST})
    bad = repo.model_copy(update={"coverage_report": "not-written.json"})
    g = run_gates(LocalRunner(), work, bad, baseline, TARGET)
    assert not g.passed
    assert g.reasons == ["coverage report not written: not-written.json"]
    assert g.coverage_after is None


class StubRunner:
    """Tests pass; the coverage command writes `report` (or nothing)."""

    def __init__(self, report: str | None, test_out: str = "", test_err: str = "") -> None:
        self.report, self.test_out, self.test_err = report, test_out, test_err

    def run(
        self, cmd: str, cwd: Path, timeout: float = 900, outputs: Sequence[str] = ()
    ) -> CmdResult:
        if "--cov" in cmd:
            if self.report is not None:
                (cwd / "coverage.json").write_text(self.report, encoding="utf-8")
            return CmdResult(0, "", "", 0.1)
        return CmdResult(0, self.test_out, self.test_err, 0.1)


def test_target_missing_from_report(tmp_path: Path, repo: RepoConfig) -> None:
    report = '{"totals": {"percent_covered": 50}, "files": {}}'
    g = run_gates(StubRunner(report), tmp_path, repo, CoverageSnapshot(50.0, {}), "src/new.py")
    assert g == GateResult(
        passed=False,
        reasons=["target file missing from coverage report: src/new.py"],
        tests_summary="",
        coverage_before=50.0,
        coverage_after=50.0,
        target_before=0.0,
        target_after=None,
    )


def test_summary_falls_back_to_stderr_and_tolerance(tmp_path: Path, repo: RepoConfig) -> None:
    report = '{"totals": {"percent_covered": 49.995}, "files": {}}'
    g = run_gates(
        StubRunner(report, test_err="\n3 passed\n\n"), tmp_path, repo, CoverageSnapshot(50.0), None
    )
    assert g.passed, g.reasons
    assert g.tests_summary == "3 passed"
