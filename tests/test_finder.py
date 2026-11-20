"""Task finder for `test` changes (TODO 1D1), run for real on a copy of the sandbox repo."""

import json
import shutil
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError

from grafter.charter import ChangeType, Charter, UnsafePathError, load_charter
from grafter.config import RepoConfig, load_config
from grafter.finder import (
    CoverageError,
    CoverageSnapshot,
    FoundTask,
    charter_domain,
    find_test_tasks,
    fingerprint,
    measure_coverage,
    read_coverage,
    read_snapshot,
    read_total,
)
from grafter.runner import CmdResult, LocalRunner

ROOT = Path(__file__).parent.parent
SANDBOX = ROOT / "examples/sandbox-repo"
CONFIG = ROOT / "deploy/grafter.sandbox.yaml"


def sandbox_copy(dest: Path) -> Path:
    repo = dest / "sandbox"
    shutil.copytree(SANDBOX, repo, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".*"))
    return repo


@pytest.fixture(scope="module")
def sandbox_coverage(tmp_path_factory: pytest.TempPathFactory) -> CoverageSnapshot:
    repo = sandbox_copy(tmp_path_factory.mktemp("finder"))
    return measure_coverage(LocalRunner(), repo, load_config(CONFIG).repo("api"))


@pytest.fixture
def charter() -> Charter:
    return load_charter(SANDBOX / "grafter.charter.yaml")


def repo_cfg(**kw: object) -> RepoConfig:
    data: dict[str, object] = {
        "name": "api",
        "github": "o/r",
        "language": "python",
        "test_command": "pytest",
        "test_paths": ["tests/**"],
    }
    data.update(kw)
    return RepoConfig.model_validate(data)


# --- RepoConfig coverage fields ---


def test_repo_coverage_defaults() -> None:
    r = repo_cfg()
    assert r.coverage_report == ""
    assert r.coverage_format == "coverage-json"


def test_repo_coverage_report_normalised() -> None:
    r = repo_cfg(coverage_report=".\\build\\cov.json", coverage_format="lcov")
    assert r.coverage_report == "build/cov.json"
    assert r.coverage_format == "lcov"


@pytest.mark.parametrize("bad", ["../cov.json", "/abs/cov.json", "C:/cov.json"])
def test_repo_coverage_report_unsafe_rejected(bad: str) -> None:
    with pytest.raises((ValidationError, UnsafePathError)):
        repo_cfg(coverage_report=bad)


def test_repo_coverage_format_rejected() -> None:
    with pytest.raises(ValidationError):
        repo_cfg(coverage_format="cobertura")


def test_sandbox_config_has_coverage_report() -> None:
    r = load_config(CONFIG).repo("api")
    assert r.coverage_report == "coverage.json"
    assert r.coverage_format == "coverage-json"


# --- report readers ---


def test_read_coverage_json(tmp_path: Path) -> None:
    p = tmp_path / "c.json"
    p.write_text(
        json.dumps(
            {
                "totals": {"percent_covered": 55.5},
                "files": {
                    "src\\a.py": {"summary": {"percent_covered": 10.0}},
                    "./src/b.py": {"summary": {"percent_covered": 90}},
                },
            }
        ),
        encoding="utf-8",
    )
    assert read_coverage(p, "coverage-json") == {"src/a.py": 10.0, "src/b.py": 90.0}
    assert read_total(p, "coverage-json") == 55.5
    assert read_snapshot(p, "coverage-json").total == 55.5


LCOV = """TN:
SF:./lib/a.dart
DA:1,1
LF:4
LH:1
end_of_record
SF:lib\\b.dart
LF:0
LH:0
end_of_record
SF:lib/a.dart
LF:4
LH:3
end_of_record
LH:9
"""


def test_read_lcov(tmp_path: Path) -> None:
    p = tmp_path / "lcov.info"
    p.write_text(LCOV, encoding="utf-8")
    assert read_coverage(p, "lcov") == {"lib/a.dart": 50.0, "lib/b.dart": 100.0}
    assert read_total(p, "lcov") == 50.0


def test_read_lcov_empty_is_full(tmp_path: Path) -> None:
    p = tmp_path / "lcov.info"
    p.write_text("", encoding="utf-8")
    assert read_snapshot(p, "lcov") == CoverageSnapshot(100.0, {})


@pytest.mark.parametrize(
    "content", ["not json", "[1]", '{"files": {"a": 1}}', '{"totals": 1, "files": 1}']
)
def test_read_coverage_json_bad(tmp_path: Path, content: str) -> None:
    p = tmp_path / "c.json"
    p.write_text(content, encoding="utf-8")
    with pytest.raises(CoverageError):
        read_snapshot(p, "coverage-json")
    with pytest.raises(CoverageError):
        read_coverage(p, "coverage-json")


def test_read_missing_report(tmp_path: Path) -> None:
    with pytest.raises(CoverageError):
        read_total(tmp_path / "nope.json", "coverage-json")
    with pytest.raises(CoverageError):
        read_coverage(tmp_path / "lcov.info", "lcov")


# --- measure_coverage ---


class StubRunner:
    def __init__(self, result: CmdResult, write: tuple[Path, str] | None = None) -> None:
        self.result, self.write = result, write
        self.calls: list[str] = []

    def run(
        self, cmd: str, cwd: Path, timeout: float = 900, outputs: Sequence[str] = ()
    ) -> CmdResult:
        self.calls.append(cmd)
        if self.write:
            self.write[0].write_text(self.write[1], encoding="utf-8")
        return self.result


def test_measure_requires_config(tmp_path: Path) -> None:
    with pytest.raises(CoverageError, match="coverage_command and coverage_report"):
        measure_coverage(StubRunner(CmdResult(0, "", "", 0)), tmp_path, repo_cfg())


def test_measure_command_failure(tmp_path: Path) -> None:
    repo = repo_cfg(coverage_command="cov", coverage_report="c.json")
    with pytest.raises(CoverageError, match="exit 2"):
        measure_coverage(StubRunner(CmdResult(2, "", "boom", 0)), tmp_path, repo)


def test_measure_deletes_stale_report(tmp_path: Path) -> None:
    (tmp_path / "c.json").write_text('{"totals": {"percent_covered": 1}}', encoding="utf-8")
    repo = repo_cfg(coverage_command="cov", coverage_report="c.json")
    with pytest.raises(CoverageError, match="not written"):
        measure_coverage(StubRunner(CmdResult(0, "", "", 0)), tmp_path, repo)


def test_measure_reads_lcov(tmp_path: Path) -> None:
    repo = repo_cfg(coverage_command="cov", coverage_report="out/lcov.info", coverage_format="lcov")
    (tmp_path / "out").mkdir()
    runner = StubRunner(CmdResult(0, "", "", 0), (tmp_path / "out/lcov.info", LCOV))
    snap = measure_coverage(runner, tmp_path, repo)
    assert snap.total == 50.0
    assert runner.calls == ["cov"]


# --- fingerprint and domains ---


def test_fingerprint_stable_and_distinct() -> None:
    a = fingerprint("api", ChangeType.TEST, "src/a.py")
    assert a == fingerprint("api", "test", "src/a.py")
    assert len(a) == 16
    assert int(a, 16) >= 0
    assert a != fingerprint("web", ChangeType.TEST, "src/a.py")
    assert a != fingerprint("api", ChangeType.BUGFIX, "src/a.py")
    assert a != fingerprint("api", ChangeType.TEST, "src/b.py")


@pytest.mark.parametrize(
    ("path", "domain"),
    [
        ("src/acme_harvest/traceability.py", "traceability"),
        ("src/acme_harvest/grading.py", "grading"),
        ("src/acme_harvest/invoices.py", "invoicing"),
        ("lib/packs/Packing.Service.cs", "packing"),
        ("src/acme_harvest/lots.py", "harvest intake"),  # no name match: directory
        ("src/misc/util.py", "harvest intake"),  # no match: first domain
    ],
)
def test_charter_domain(charter: Charter, path: str, domain: str) -> None:
    assert charter_domain(charter, path) == domain


def test_charter_domain_singular_plural() -> None:
    c = Charter(
        solution="S",
        purpose="p",
        core_domains=["billing", "lot tracking"],
        allowed_change_types=[ChangeType.TEST],
    )
    assert charter_domain(c, "src/Lots.ts") == "lot tracking"
    assert charter_domain(c, "src/abc.ts") == "billing"  # short words never match


# --- find_test_tasks ---


def test_find_ranks_and_filters_synthetic(charter: Charter) -> None:
    cov = {
        "src/acme_harvest/grading.py": 40.0,
        "src/acme_harvest/b.py": 10.0,
        "src/acme_harvest/a.py": 10.0,
        "src/acme_harvest/invoices.py": 0.0,  # protected in repo api
        "tests/test_x.py": 0.0,  # test path
        "src/acme_harvest/full.py": 100.0,
        "../escape.py": 0.0,  # unsafe: fails closed
    }
    found = find_test_tasks(repo_cfg(), charter, cov)
    assert [f.target for f in found] == [
        "src/acme_harvest/a.py",
        "src/acme_harvest/b.py",
        "src/acme_harvest/grading.py",
    ]
    assert [f.target for f in find_test_tasks(repo_cfg(), charter, cov, limit=1)] == [
        "src/acme_harvest/a.py"
    ]
    assert find_test_tasks(repo_cfg(), charter, cov, limit=0) == []
    assert [f.target for f in find_test_tasks(repo_cfg(), charter, cov, min_gain_below=20)] == [
        "src/acme_harvest/a.py",
        "src/acme_harvest/b.py",
    ]


def test_find_skips_review_paths() -> None:
    c = Charter(
        solution="S",
        purpose="p",
        core_domains=["grading"],
        review_paths=["src/auth/"],
        allowed_change_types=[ChangeType.TEST],
    )
    found = find_test_tasks(repo_cfg(), c, {"src/auth/login.py": 0.0, "src/grading.py": 5.0})
    assert [f.target for f in found] == ["src/grading.py"]


def test_find_on_sandbox_repo(sandbox_coverage: CoverageSnapshot, charter: Charter) -> None:
    repo = load_config(CONFIG).repo("api")
    found = find_test_tasks(repo, charter, sandbox_coverage.files)
    targets = [f.target for f in found]
    assert targets[0] == "src/acme_harvest/traceability.py"
    assert set(targets) == {
        "src/acme_harvest/traceability.py",
        "src/acme_harvest/lots.py",
        "src/acme_harvest/grading.py",
    }
    assert [f.coverage for f in found] == sorted(f.coverage for f in found)
    assert "src/acme_harvest/invoices.py" not in targets
    assert not any(t.startswith("tests/") for t in targets)

    by_target = {f.target: f for f in found}
    trace = by_target["src/acme_harvest/traceability.py"]
    assert isinstance(trace, FoundTask)
    assert trace.coverage == 0.0
    assert trace.task.charter_ref == "domain: traceability"
    assert trace.task.change_type is ChangeType.TEST
    assert trace.task.repo == "api"
    assert trace.task.id == fingerprint("api", ChangeType.TEST, trace.target)
    assert "src/acme_harvest/traceability.py" in trace.task.instruction
    assert "tests/**" in trace.task.instruction
    assert "traceability" in trace.task.instruction
    assert by_target["src/acme_harvest/grading.py"].task.charter_ref == "domain: grading"
