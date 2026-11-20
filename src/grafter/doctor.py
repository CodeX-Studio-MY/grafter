"""`grafter doctor` (TODO 1B5): is this host ready for a nightly run?

Each check is ok, warn or fail. Any fail means `grafter run` cannot work as configured.
Checks never print secrets: the App key and tokens stay inside `AppAuth`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from grafter.config import Config, load_config
from grafter.credentials import SecretError
from grafter.engines import get_engine
from grafter.gateway import GatewayClient, GatewayError
from grafter.github import GitHubClient, GitHubError, app_auth
from grafter.lock import pid_alive
from grafter.runner import default_image
from grafter.store import Store

Status = Literal["ok", "warn", "fail"]
DOCKER_TIMEOUT_S = 20
GATEWAY_TIMEOUT_S = 5


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str


def docker_info() -> tuple[bool, str]:
    """(works, server version or error) from `docker info`."""
    exe = shutil.which("docker")
    if exe is None:
        return False, "docker not on PATH"
    try:
        proc = subprocess.run(
            [exe, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=DOCKER_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"docker info failed: {type(e).__name__}"
    if proc.returncode != 0:
        return False, f"docker info failed: {(proc.stderr or proc.stdout).strip()[-200:]}"
    return True, f"server {proc.stdout.strip()}"


def _home(cfg: Config) -> Check:
    home = cfg.paths().home
    probe = home / f".doctor-{os.getpid()}"
    try:
        home.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        return Check("home", "fail", f"{home} not writable: {e.strerror or e}")
    return Check("home", "ok", str(home))


def _lock(cfg: Config) -> Check:
    path = cfg.paths().lock_file
    if not path.exists():
        return Check("lock", "ok", "free")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        pid, started = int(data["pid"]), str(data["started_at"])
    except (OSError, ValueError, TypeError, KeyError):
        return Check("lock", "warn", f"unreadable lock file {path}; the next run takes it over")
    if pid_alive(pid):
        return Check("lock", "warn", f"held by pid {pid} since {started}")
    return Check("lock", "warn", f"stale lock (pid {pid} not running); the next run takes it over")


def _db(cfg: Config) -> Check:
    path = cfg.paths().db_file
    try:
        with Store(path) as store:
            version = store.migrate()
    except Exception as e:
        return Check("database", "fail", f"{path}: {type(e).__name__}: {e}")
    return Check("database", "ok", f"{path} at schema version {version}")


def _engine(cfg: Config) -> Check:
    try:
        engine = get_engine(cfg.engine, cfg)
    except (ValueError, ImportError, SecretError) as e:
        return Check("engine", "fail", str(e))
    return Check("engine", "ok", engine.name)


def _runner(cfg: Config) -> Check:
    if cfg.runner == "local":
        return Check(
            "runner", "warn", "local: repo commands run on this host (offline testing only)"
        )
    docker = shutil.which("docker")
    if docker is None:
        return Check("runner", "fail", "docker: not on PATH")
    images = sorted({r.sandbox_image or default_image(r.language) for r in cfg.repos})
    missing = []
    for image in images:
        proc = subprocess.run(
            [docker, "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if proc.returncode != 0:
            missing.append(image)
    if missing:
        return Check(
            "runner",
            "fail",
            f"docker: sandbox image(s) missing: {', '.join(missing)}; "
            "build with scripts/build-sandbox.sh <language>",
        )
    return Check("runner", "ok", f"docker: {', '.join(images)}")


def _gateway(cfg: Config) -> Check:
    try:
        GatewayClient(cfg.llm_gateway_url, "", timeout=GATEWAY_TIMEOUT_S).health()
    except GatewayError as e:
        return Check("gateway", "warn", str(e))
    return Check("gateway", "ok", cfg.llm_gateway_url)


def _github(cfg: Config, environ: Mapping[str, str] | None) -> Check:
    try:
        auth = app_auth(cfg, environ)
    except SecretError as e:
        return Check("github", "warn", f"GitHub App not configured: {e}")
    try:
        visible = GitHubClient(auth, api_url=cfg.github_app.api_url).installation_repos()
    except GitHubError as e:
        return Check("github", "fail", str(e))
    missing = sorted({r.github for r in cfg.repos} - set(visible))
    listed = ", ".join(visible) or "none"
    if missing:
        return Check("github", "warn", f"App sees {listed}; cannot see {', '.join(missing)}")
    return Check("github", "ok", f"App sees {listed}")


def run_checks(config: Path, environ: Mapping[str, str] | None = None) -> list[Check]:
    try:
        cfg = load_config(config)
    except (ValidationError, ValueError, FileNotFoundError, OSError) as e:
        return [Check("config", "fail", f"{config}: {e}")]
    checks = [
        Check("config", "ok", f"{config} ({len(cfg.repos)} repo(s))"),
        _home(cfg),
    ]
    ks = cfg.kill_switch
    if ks.is_engaged(environ):
        detail = f"engaged (file {ks.file} or env {ks.env}); runs stop until it is cleared"
        checks.append(Check("kill switch", "warn", detail))
    else:
        checks.append(Check("kill switch", "ok", "off"))
    checks += [_lock(cfg), _db(cfg)]
    git = shutil.which("git")
    checks.append(Check("git", "ok", git) if git else Check("git", "fail", "git not on PATH"))
    works, detail = docker_info()
    checks.append(Check("docker", "ok" if works else "warn", detail))
    checks += [_runner(cfg), _engine(cfg), _gateway(cfg), _github(cfg, environ)]
    return checks
