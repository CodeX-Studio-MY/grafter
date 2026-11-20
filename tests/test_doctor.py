"""`grafter doctor` (TODO 1B5a)."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from test_github import APP_ID, INSTALLATION_ID, Clock, FakeGitHub
from typer.testing import CliRunner

from grafter import doctor
from grafter.cli import app
from grafter.doctor import Check, run_checks

NO_SECRET = {"GRAFTER_GITHUB_APP_PRIVATE_KEY": "", "GITHUB_APP_PRIVATE_KEY": ""}


class _Health(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        ok = self.path == "/health/liveliness"
        self.send_response(200 if ok else 404)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


@pytest.fixture(scope="module")
def gateway() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Health)
    threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def docker_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "docker_info", lambda: (True, "server 27.0.0"))


GATEWAY: list[str] = []
DEAD_GATEWAY = "http://127.0.0.1:9"


@pytest.fixture(autouse=True)
def _live_gateway(gateway: str) -> None:
    GATEWAY[:] = [gateway]


def write_cfg(tmp_path: Path, **overrides: Any) -> Path:
    data: dict[str, Any] = {
        "home": str(tmp_path / "home"),
        "repos": [
            {
                "name": "api",
                "github": "acme/grafter-sandbox",
                "language": "python",
                "test_command": "pytest",
                "test_paths": ["tests/**"],
            }
        ],
        "runner": "local",
        "engine": "noop",
        "kill_switch": {"file": str(tmp_path / "STOP"), "env": "GRAFTER_TEST_STOP"},
        "llm_gateway_url": GATEWAY[0],
    }
    data.update(overrides)
    path = tmp_path / "grafter.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def by_name(checks: list[Check]) -> dict[str, Check]:
    return {c.name: c for c in checks}


def test_ok_ish_config_exits_0(tmp_path: Path, gateway: str) -> None:
    cfg = write_cfg(tmp_path, llm_gateway_url=gateway)
    result = CliRunner().invoke(app, ["doctor", "--config", str(cfg)], env=NO_SECRET)
    assert result.exit_code == 0, result.output
    out = result.output
    for name in ("config", "home", "kill switch", "lock", "database", "git", "docker"):
        assert any(line.startswith("ok") and name in line for line in out.splitlines()), name
    assert "local: repo commands run on this host" in out
    assert "GitHub App not configured" in out
    assert "ok (2 warning(s))" in out
    assert (tmp_path / "home/grafter.db").is_file()
    assert not list((tmp_path / "home").glob(".doctor-*"))


def test_broken_config_exits_1(tmp_path: Path) -> None:
    bad = tmp_path / "grafter.yaml"
    bad.write_text("repos: []\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["doctor", "--config", str(bad)])
    assert result.exit_code == 1
    assert result.output.startswith("fail")
    assert "1 check(s) failed: config" in result.output
    missing = CliRunner().invoke(app, ["doctor", "--config", str(tmp_path / "nope.yaml")])
    assert missing.exit_code == 1


def test_openhands_engine_fails(tmp_path: Path) -> None:
    cfg = write_cfg(tmp_path, engine="openhands", runner="docker")
    result = CliRunner().invoke(app, ["doctor", "--config", str(cfg)], env=NO_SECRET)
    assert result.exit_code == 1
    assert "1 check(s) failed: engine" in result.output
    assert "docker runner lands in 1D2b" in result.output
    assert "check(s) failed: engine" in result.output


def test_warnings_kill_switch_lock_gateway_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor, "docker_info", lambda: (False, "docker not on PATH"))
    cfg = write_cfg(tmp_path, llm_gateway_url=DEAD_GATEWAY)
    (tmp_path / "STOP").write_text("", encoding="utf-8")
    (tmp_path / "home").mkdir()
    lock = tmp_path / "home/grafter.lock"
    lock.write_text(json.dumps({"pid": os.getpid(), "started_at": "2026-11-20T02:00:00+00:00"}))
    checks = by_name(run_checks(cfg, environ=NO_SECRET))
    assert checks["kill switch"].status == "warn" and "engaged" in checks["kill switch"].detail
    assert checks["lock"] == Check(
        "lock", "warn", f"held by pid {os.getpid()} since 2026-11-20T02:00:00+00:00"
    )
    assert checks["gateway"].status == "warn"
    assert checks["docker"] == Check("docker", "warn", "docker not on PATH")

    cfg = write_cfg(tmp_path)  # live gateway from here on: refused connects are slow on Windows
    lock.write_text(json.dumps({"pid": 0, "started_at": "2026-11-20T02:00:00+00:00"}))
    assert "stale lock" in by_name(run_checks(cfg, environ=NO_SECRET))["lock"].detail
    lock.write_text("garbage")
    assert "unreadable" in by_name(run_checks(cfg, environ=NO_SECRET))["lock"].detail


def test_git_missing_and_unknown_engine_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    checks = by_name(run_checks(write_cfg(tmp_path, engine="nope"), environ=NO_SECRET))
    assert checks["git"] == Check("git", "fail", "git not on PATH")
    assert checks["engine"].status == "fail" and "unknown engine" in checks["engine"].detail


def test_home_and_db_failures(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    checks = by_name(run_checks(write_cfg(tmp_path, home=str(blocker / "home")), environ={}))
    assert checks["home"].status == "fail"
    assert checks["database"].status == "fail"


def test_docker_info(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()  # the real function, not the autouse stub
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    assert doctor.docker_info() == (False, "docker not on PATH")

    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/docker")
    replies: list[Any] = [
        subprocess.CompletedProcess([], 0, "27.3.1 ", ""),
        subprocess.CompletedProcess([], 1, "", "Cannot connect to the Docker daemon "),
        subprocess.TimeoutExpired("docker", 20),
    ]

    def fake_run(*args: Any, **kwargs: Any) -> Any:
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    assert doctor.docker_info() == (True, "server 27.3.1")
    failed = "docker info failed: Cannot connect to the Docker daemon"
    assert doctor.docker_info() == (False, failed)
    assert doctor.docker_info() == (False, "docker info failed: TimeoutExpired")


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
    yield fake
    fake.server.shutdown()
    fake.server.server_close()


def test_github_app_lists_repos(
    tmp_path: Path, rsa_keys: tuple[str, str], fake_github: FakeGitHub, gateway: str
) -> None:
    fake_github.repos = ["acme/grafter-sandbox", "acme/other"]
    ids = {"app_id": APP_ID, "installation_id": INSTALLATION_ID, "api_url": fake_github.url}
    cfg = write_cfg(tmp_path, github_app=ids, llm_gateway_url=gateway)
    env = {"GRAFTER_GITHUB_APP_PRIVATE_KEY": rsa_keys[0]}
    result = CliRunner().invoke(app, ["doctor", "--config", str(cfg)], env=env)
    assert result.exit_code == 0, result.output
    assert "App sees acme/grafter-sandbox, acme/other" in result.output
    assert "BEGIN PRIVATE KEY" not in result.output
    assert "ghs_" not in result.output

    fake_github.repos = ["acme/other"]
    checks = by_name(run_checks(cfg, environ=env))
    assert checks["github"].status == "warn"
    assert "cannot see acme/grafter-sandbox" in checks["github"].detail


def test_github_app_rejected_fails(
    tmp_path: Path, rsa_keys: tuple[str, str], fake_github: FakeGitHub
) -> None:
    ids = {"app_id": APP_ID + 1, "installation_id": INSTALLATION_ID, "api_url": fake_github.url}
    cfg = write_cfg(tmp_path, github_app=ids)
    checks = by_name(run_checks(cfg, environ={"GRAFTER_GITHUB_APP_PRIVATE_KEY": rsa_keys[0]}))
    assert checks["github"].status == "fail"
    assert "HTTP 401" in checks["github"].detail
