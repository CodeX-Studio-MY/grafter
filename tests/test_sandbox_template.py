"""The sandbox template (examples/sandbox-repo) must stay valid for Grafter (TODO 0D5)."""

from pathlib import Path

from typer.testing import CliRunner

from grafter.charter import ChangeType, load_charter
from grafter.cli import app
from grafter.config import load_config
from grafter.engines.base import RunResult, Task
from grafter.guard import check

ROOT = Path(__file__).parent.parent
SANDBOX = ROOT / "examples/sandbox-repo"
CHARTER = SANDBOX / "grafter.charter.yaml"
CONFIG = ROOT / "deploy/grafter.sandbox.yaml"


def test_charter_validates_against_sandbox_config() -> None:
    result = CliRunner().invoke(app, ["validate", str(CHARTER), "--config", str(CONFIG)])
    assert result.exit_code == 0, result.output
    assert "repos: 1 (api)" in result.output


def test_sandbox_config_is_safe_by_default() -> None:
    cfg = load_config(CONFIG)
    assert cfg.dry_run is True
    assert cfg.limits.max_prs_per_night == 1
    assert cfg.tier0_auto_merge is False


def _task() -> Task:
    return Task(
        id="t1",
        repo="api",
        change_type=ChangeType.TEST,
        charter_ref="domain: grading",
        instruction="add tests",
    )


def test_guard_allows_test_only_change_in_sandbox() -> None:
    charter, cfg = load_charter(CHARTER), load_config(CONFIG)
    result = RunResult(success=True, changed_files=["tests/test_traceability.py"], lines_added=40)
    v = check(_task(), result, charter, cfg.limits, cfg.repo("api"))
    assert v.allowed, v.reasons


def test_guard_blocks_protected_invoices_module() -> None:
    charter, cfg = load_charter(CHARTER), load_config(CONFIG)
    result = RunResult(success=True, changed_files=["src/acme_harvest/invoices.py"])
    v = check(_task(), result, charter, cfg.limits, cfg.repo("api"))
    assert not v.allowed
    assert any("protected" in r for r in v.reasons)
