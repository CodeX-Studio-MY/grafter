"""Trivy + OSV-Scanner for real (TODO 1E2 Done-when). Skipped unless docker runs and both
pinned images and the database cache exist (`grafter scan` creates the cache)."""

from __future__ import annotations

import secrets
import shutil
import string
import subprocess
from pathlib import Path

import pytest

from grafter.config import Scanning
from grafter.scanners import DockerScanner


def _ready() -> bool:
    exe = shutil.which("docker")
    if exe is None:
        return False
    s = Scanning()
    for args in (
        ["image", "inspect", s.trivy_image],
        ["image", "inspect", s.osv_image],
        ["volume", "inspect", s.cache_volume],
    ):
        try:
            if subprocess.run([exe, *args], capture_output=True, timeout=30).returncode != 0:
                return False
        except (OSError, subprocess.TimeoutExpired):
            return False
    return True


pytestmark = pytest.mark.skipif(not _ready(), reason="needs docker, scanner images and cache")


def test_both_tools_find_the_vulnerable_pin_and_trivy_the_secret(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "requirements.txt").write_text("requests==2.19.0\n", encoding="utf-8")
    # Generated here, never committed: a GitHub-token-shaped string for Trivy's secret scanner.
    token = "ghp_" + "".join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(36)
    )
    (repo / "src" / "settings.py").write_text(f'TOKEN = "{token}"\n', encoding="utf-8")

    results = {r.tool: r for r in DockerScanner(Scanning(osv_ecosystems=["PyPI"])).scan(repo)}
    assert not results["trivy"].error and not results["osv"].error
    trivy = {f.rule_id for f in results["trivy"].findings}
    osv = {f.rule_id for f in results["osv"].findings}
    assert "CVE-2018-18074" in trivy and "CVE-2018-18074" in osv
    assert "github-pat" in trivy
    assert all(token not in f.message for f in results["trivy"].findings)
    assert sorted(p.name for p in repo.rglob("*")) == ["requirements.txt", "settings.py", "src"]
