from pathlib import Path

import pytest

from grafter.charter import ChangeType, Charter, load_charter
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
