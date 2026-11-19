"""`grafter run` end to end (TODO 1D3, 1A1): CLI -> pipeline against a local bare origin."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import config_data
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from test_github import APP_ID, INSTALLATION_ID, Clock, FakeGitHub
from typer.testing import CliRunner

from grafter.cli import app
from grafter.lock import RunLock


@pytest.fixture(scope="module")
def rsa_keys() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private, public


@pytest.fixture
def fake_github(rsa_keys: tuple[str, str]) -> Iterator[FakeGitHub]:
    fake = FakeGitHub(rsa_keys[1], Clock(time.time()))
    fake.clock.now = time.time()
    yield fake
    fake.server.shutdown()
    fake.server.server_close()


def write_cfg(tmp_path: Path, origin: Path, **overrides: Any) -> Path:
    path = tmp_path / "grafter.yaml"
    path.write_text(yaml.safe_dump(config_data(tmp_path, origin, **overrides)), encoding="utf-8")
    return path


def invoke(*args: str, env: dict[str, str] | None = None) -> Any:
    return CliRunner().invoke(app, list(args), env=env)


def test_dry_run_writes_log_and_report(tmp_path: Path, sandbox_origin: Path) -> None:
    cfg = write_cfg(tmp_path, sandbox_origin)
    result = invoke("run", "--dry-run", "--config", str(cfg))
    assert result.exit_code == 0, result.output
    assert "dry run " in result.output and "3 rejected" in result.output
    runs = list((tmp_path / "home/runs").iterdir())
    assert len(runs) == 1
    log = runs[0] / "run.jsonl"
    kinds = [json.loads(line)["kind"] for line in log.read_text("utf-8").splitlines()]
    assert kinds[0] == "run_started" and kinds[-1] == "run_finished"
    assert all(
        json.loads(line)["run_id"] == runs[0].name for line in log.read_text("utf-8").splitlines()
    )
    report = runs[0] / "report.md"
    assert report.is_file()
    assert f"report: {report}" in result.output
    assert "engine made no changes" in report.read_text("utf-8")


def test_config_dry_run_flag_is_honoured(tmp_path: Path, sandbox_origin: Path) -> None:
    cfg = write_cfg(tmp_path, sandbox_origin, dry_run=True)
    result = invoke("run", "--config", str(cfg))
    assert result.exit_code == 0, result.output
    assert result.output.startswith("dry run ") or "\ndry run " in result.output


def test_refuses_docker_runner(tmp_path: Path, sandbox_origin: Path) -> None:
    cfg = write_cfg(tmp_path, sandbox_origin, runner="docker")
    result = invoke("run", "--dry-run", "--config", str(cfg))
    assert result.exit_code == 2
    assert "docker runner lands in 1D2b" in result.output
    assert not (tmp_path / "home").exists()


def test_refuses_openhands_and_unknown_engine(tmp_path: Path, sandbox_origin: Path) -> None:
    cfg = write_cfg(tmp_path, sandbox_origin, engine="openhands")
    result = invoke("run", "--dry-run", "--config", str(cfg))
    assert result.exit_code == 2
    assert "engine 'openhands' lands in 1C3; use engine: noop" in result.output
    cfg = write_cfg(tmp_path, sandbox_origin, engine="nope")
    assert invoke("run", "--dry-run", "--config", str(cfg)).exit_code == 2


def test_invalid_config_exit_1(tmp_path: Path) -> None:
    bad = tmp_path / "grafter.yaml"
    bad.write_text("repos: []\n", encoding="utf-8")
    assert invoke("run", "--config", str(bad)).exit_code == 1


def test_kill_switch_exit_3(tmp_path: Path, sandbox_origin: Path) -> None:
    cfg = write_cfg(tmp_path, sandbox_origin)
    (tmp_path / "STOP").write_text("", encoding="utf-8")
    result = invoke("run", "--dry-run", "--config", str(cfg))
    assert result.exit_code == 3
    assert "kill switch engaged" in result.output
    assert not (tmp_path / "home/work").exists()


def test_kill_switch_env_exit_3(tmp_path: Path, sandbox_origin: Path) -> None:
    cfg = write_cfg(tmp_path, sandbox_origin)
    result = invoke("run", "--dry-run", "--config", str(cfg), env={"GRAFTER_TEST_STOP": "1"})
    assert result.exit_code == 3


def test_lock_held_exit_4(tmp_path: Path, sandbox_origin: Path) -> None:
    cfg = write_cfg(tmp_path, sandbox_origin)
    with RunLock(tmp_path / "home/grafter.lock"):
        result = invoke("run", "--dry-run", "--config", str(cfg))
    assert result.exit_code == 4
    assert f"held by pid {os.getpid()}" in result.output


def test_failed_run_exit_1(tmp_path: Path, sandbox_origin: Path) -> None:
    cfg = write_cfg(tmp_path, sandbox_origin, charter_file="missing.yaml")
    result = invoke("run", "--dry-run", "--config", str(cfg))
    assert result.exit_code == 1
    assert "stopped: error: FileNotFoundError" in result.output


def test_real_run_needs_app_ids(tmp_path: Path, sandbox_origin: Path) -> None:
    cfg = write_cfg(tmp_path, sandbox_origin)
    result = invoke("run", "--config", str(cfg))
    assert result.exit_code == 1
    assert "github_app.app_id and github_app.installation_id are not set" in result.output


def test_real_run_needs_private_key(tmp_path: Path, sandbox_origin: Path) -> None:
    ids = {"app_id": APP_ID, "installation_id": INSTALLATION_ID}
    cfg = write_cfg(tmp_path, sandbox_origin, github_app=ids)
    env = {"GRAFTER_GITHUB_APP_PRIVATE_KEY": "", "GITHUB_APP_PRIVATE_KEY": ""}
    result = invoke("run", "--config", str(cfg), env=env)
    assert result.exit_code == 1
    assert "secret GITHUB_APP_PRIVATE_KEY is not set" in result.output


def test_real_run_against_fake_github(
    tmp_path: Path,
    sandbox_origin: Path,
    rsa_keys: tuple[str, str],
    fake_github: FakeGitHub,
) -> None:
    """Real (non-dry) run wiring: App token, GitHubClient, find_open_pr per task."""
    ids = {"app_id": APP_ID, "installation_id": INSTALLATION_ID, "api_url": fake_github.url}
    cfg = write_cfg(tmp_path, sandbox_origin, github_app=ids)
    env = {"GRAFTER_GITHUB_APP_PRIVATE_KEY": rsa_keys[0]}
    result = invoke("run", "--config", str(cfg), env=env)
    assert result.exit_code == 0, result.output
    assert result.output.startswith("run ") or "\nrun " in result.output
    pulls = [c for c in fake_github.calls if c[:2] == ("GET", "/repos/acme/grafter-sandbox/pulls")]
    assert len(pulls) == 3  # one open-PR check per candidate task
    assert fake_github.issued  # the App token was used


def test_summary_printing_and_kill_switch_mid_run(
    tmp_path: Path, sandbox_origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from grafter import cli
    from grafter.pipeline import STOP_KILL, RunSummary

    def fake_run_night(cfg: Any, deps: Any, dry_run: bool) -> RunSummary:
        return RunSummary(
            run_id="r1",
            dry_run=dry_run,
            prs=[{"branch": "ai/test-x", "target": "src/a.py", "url": None}],
            errors=[{"repo": "api", "error": "GitError: boom"}],
            stopped_reason=STOP_KILL,
            report_path="r.md",
        )

    monkeypatch.setattr(cli, "run_night", fake_run_night)
    result = invoke("run", "--dry-run", "--config", str(write_cfg(tmp_path, sandbox_origin)))
    assert result.exit_code == 3
    assert "PR ai/test-x: src/a.py" in result.output
    assert "error api: GitError: boom" in result.output
    assert "stopped: kill switch engaged" in result.output
