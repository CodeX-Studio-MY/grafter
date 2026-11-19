from pathlib import Path

import pytest
from pydantic import ValidationError

from grafter.charter import Charter, load_charter
from grafter.config import load_config

ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / "examples/acme-harvest/grafter.charter.yaml"


@pytest.fixture
def charter() -> Charter:
    return load_charter(EXAMPLE)


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


def test_example_config_defaults():
    cfg = load_config(ROOT / "grafter.example.yaml")
    assert cfg.tier0_auto_merge is False
    assert cfg.branches.preview == "ai/preview"
