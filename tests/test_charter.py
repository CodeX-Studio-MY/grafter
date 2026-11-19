from pathlib import Path

import pytest
from pydantic import ValidationError

from grafter.charter import ChangeType, Charter, load_charter
from grafter.config import load_config
from grafter.engines.base import RunResult, Task
from grafter.guard import Tier, check

ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / "examples/acme-harvest/grafter.charter.yaml"


@pytest.fixture
def charter() -> Charter:
    return load_charter(EXAMPLE)


def task(t: ChangeType = ChangeType.BUGFIX, ref: str = "invariant: traceability") -> Task:
    return Task(id="1", repo="api", change_type=t, charter_ref=ref, instruction="x")


def test_example_charter_loads(charter):
    assert charter.solution == "AcmeHarvest"
    assert charter.is_protected("grafter.charter.yaml")
    assert charter.is_protected("auth/login.cs")
    assert charter.needs_review("db/migrations/001.sql")
    assert not charter.is_protected("src/lots.cs")


def test_charter_always_protects_itself():
    c = Charter(solution="x", purpose="y", core_domains=["a"], allowed_change_types=["bugfix"])
    assert c.is_protected("grafter.charter.yaml")


def test_path_cannot_be_protected_and_review():
    with pytest.raises(ValidationError):
        Charter(
            solution="x",
            purpose="y",
            core_domains=["a"],
            allowed_change_types=["bugfix"],
            protected_paths=["db/"],
            review_paths=["db/"],
        )


def test_unknown_field_rejected():
    with pytest.raises(ValidationError):
        Charter(
            solution="x",
            purpose="y",
            core_domains=["a"],
            allowed_change_types=["bugfix"],
            features="yes",
        )


def test_guard_rejects_protected_path(charter):
    v = check(task(), RunResult(success=True, changed_files=["auth/x.cs"]), charter)
    assert not v.allowed


def test_guard_rejects_missing_citation(charter):
    v = check(task(ref=" "), RunResult(success=True, changed_files=["src/a.cs"]), charter)
    assert not v.allowed


def test_guard_review_paths_are_tier2(charter):
    v = check(task(), RunResult(success=True, changed_files=["db/migrations/2.sql"]), charter)
    assert v.allowed and v.tier == Tier.REVIEW


def test_guard_small_dependency_bump_is_tier0(charter):
    v = check(
        task(ChangeType.DEPENDENCY), RunResult(success=True, changed_files=["a.csproj"]), charter
    )
    assert v.allowed and v.tier == Tier.AUTO


def test_example_config_defaults():
    cfg = load_config(ROOT / "grafter.example.yaml")
    assert cfg.tier0_auto_merge is False
    assert cfg.branches.preview == "ai/preview"
