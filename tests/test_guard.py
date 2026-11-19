from pathlib import Path

import pytest
from pydantic import ValidationError

from grafter.charter import ChangeType, Charter, load_charter
from grafter.config import Limits
from grafter.engines.base import RunResult, Task
from grafter.guard import Tier, check

ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / "examples/acme-harvest/grafter.charter.yaml"


@pytest.fixture
def charter() -> Charter:
    return load_charter(EXAMPLE)


def task(
    t: ChangeType = ChangeType.BUGFIX,
    ref: str = "invariant: traceability",
    bump: str | None = None,
) -> Task:
    return Task(
        id="1", repo="api", change_type=t, charter_ref=ref, instruction="x", version_bump=bump
    )


def result(*files: str) -> RunResult:
    return RunResult(success=True, changed_files=list(files))


def test_guard_rejects_protected_path(charter):
    v = check(task(), result("auth/x.cs"), charter)
    assert not v.allowed


def test_guard_rejects_missing_citation(charter):
    v = check(task(ref=" "), result("src/a.cs"), charter)
    assert not v.allowed


def test_guard_review_paths_are_tier2(charter):
    v = check(task(), result("db/migrations/2.sql"), charter)
    assert v.allowed and v.tier == Tier.REVIEW


def test_patch_dependency_bump_is_tier0(charter):
    v = check(task(ChangeType.DEPENDENCY, bump="patch"), result("a.csproj"), charter)
    assert v.allowed and v.tier == Tier.AUTO


def test_minor_dependency_bump_is_tier1(charter):
    v = check(task(ChangeType.DEPENDENCY, bump="minor"), result("a.csproj"), charter)
    assert v.allowed and v.tier == Tier.APPROVE


def test_dependency_bump_without_version_is_tier1(charter):
    v = check(task(ChangeType.DEPENDENCY), result("a.csproj"), charter)
    assert v.allowed and v.tier == Tier.APPROVE


def test_patch_bump_touching_many_files_is_tier1(charter):
    v = check(task(ChangeType.DEPENDENCY, bump="patch"), result("a", "b", "c"), charter)
    assert v.allowed and v.tier == Tier.APPROVE


def test_patch_bump_in_review_path_is_tier2(charter):
    v = check(task(ChangeType.DEPENDENCY, bump="patch"), result("db/migrations/x"), charter)
    assert v.allowed and v.tier == Tier.REVIEW


def test_small_security_change_is_never_tier0(charter):
    v = check(task(ChangeType.SECURITY, bump="patch"), result("a.csproj"), charter)
    assert v.allowed and v.tier == Tier.APPROVE


def test_default_limits():
    lim = Limits()
    assert lim.max_files_per_change == 20
    assert lim.max_lines_per_change == 400


def test_limits_must_be_positive():
    with pytest.raises(ValidationError):
        Limits(max_files_per_change=0)
    with pytest.raises(ValidationError):
        Limits(max_lines_per_change=0)


def test_rejects_too_many_files(charter):
    r = RunResult(success=True, changed_files=["a", "b"], deleted_files=["c"])
    v = check(task(), r, charter, Limits(max_files_per_change=2))
    assert not v.allowed
    assert any("too many files: 3 > 2" in x for x in v.reasons)


def test_file_limit_is_inclusive(charter):
    r = RunResult(success=True, changed_files=["a"], deleted_files=["c"])
    v = check(task(), r, charter, Limits(max_files_per_change=2))
    assert v.allowed


def test_rejects_too_many_lines(charter):
    r = RunResult(success=True, changed_files=["a"], lines_added=300, lines_removed=101)
    v = check(task(), r, charter)
    assert not v.allowed
    assert any("diff too large: 401 > 400 lines" in x for x in v.reasons)


def test_line_limit_is_inclusive(charter):
    r = RunResult(success=True, changed_files=["a"], lines_added=300, lines_removed=100)
    assert check(task(), r, charter).allowed


def test_default_limits_used_when_none(charter):
    r = RunResult(success=True, changed_files=[f"f{i}" for i in range(21)])
    assert not check(task(), r, charter).allowed


def test_rejects_deleting_protected_file(charter):
    r = RunResult(success=True, changed_files=["src/a.cs"], deleted_files=["auth/login.cs"])
    v = check(task(), r, charter)
    assert not v.allowed
    assert any("auth/login.cs" in x for x in v.reasons)


def test_deleting_review_file_is_tier2(charter):
    r = RunResult(success=True, deleted_files=["db/migrations/1.sql"])
    v = check(task(), r, charter)
    assert v.allowed and v.tier == Tier.REVIEW


def test_deleted_files_count_against_tier0(charter):
    r = RunResult(success=True, changed_files=["a.csproj"], deleted_files=["b", "c"])
    v = check(task(ChangeType.DEPENDENCY, bump="patch"), r, charter)
    assert v.allowed and v.tier == Tier.APPROVE
