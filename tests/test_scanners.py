"""DockerScanner (TODO 1E2) with a fake `docker`: update has a network but no repo content;
scans are offline, read-only cache, uid 10001, SARIF back outside the work tree."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from grafter.config import Scanning
from grafter.scanners import (
    CACHE,
    OSV_SCAN,
    SCAN_ROOT,
    TRIVY_SCAN,
    DockerScanner,
    osv_update_script,
    trivy_update_script,
)

FIX = Path(__file__).parent / "fixtures" / "sarif"

FAKE = r"""
import io, json, os, sys, tarfile
args = sys.argv[1:]
entry = {"args": args}
if args[0] == "start":
    entry["stdin_bytes"] = len(sys.stdin.buffer.read())
with open(os.environ["FAKE_DOCKER_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(entry) + "\n")
mode = os.environ.get("FAKE_MODE", "ok")
if args[0] == "run":  # update phase
    sys.exit(3 if mode == "update_fail" else 0)
if args[0] == "create":
    print("cid"); sys.exit(0)
if args[0] == "start":
    sys.exit(2 if mode == "scan_fail" else 0)
if args[0] == "cp":
    src = args[1].split(":", 1)[1]
    name = src.rsplit("/", 1)[1]
    path = os.path.join(os.environ["FAKE_SARIF_DIR"], name)
    if mode == "no_sarif" or not os.path.exists(path):
        sys.exit(1)
    data = open(path, "rb").read()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name); info.size = len(data); tar.addfile(info, io.BytesIO(data))
    sys.stdout.buffer.write(buf.getvalue()); sys.exit(0)
sys.exit(0)
"""


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], Path]:
    script = tmp_path / "fake_docker.py"
    script.write_text(FAKE, encoding="utf-8")
    log = tmp_path / "docker.log"
    sarif = tmp_path / "sarif"
    sarif.mkdir()
    for name in ("trivy.sarif", "osv.sarif"):
        text = (FIX / name).read_text("utf-8").replace("/workspace", SCAN_ROOT)
        (sarif / name).write_text(text, encoding="utf-8")
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    monkeypatch.setenv("FAKE_SARIF_DIR", str(sarif))
    monkeypatch.delenv("FAKE_MODE", raising=False)
    return [sys.executable, str(script)], log


def calls(log: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log.read_text("utf-8").splitlines()]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "config").write_text("token=ghs_secret\n", encoding="utf-8")
    (root / "requirements.txt").write_text("requests==2.19.0\n", encoding="utf-8")
    return root


def test_update_has_network_but_no_repo_content(fake: tuple[list[str], Path]) -> None:
    docker, log = fake
    assert DockerScanner(Scanning(), docker=docker).update() == []
    runs = [c["args"] for c in calls(log)]
    assert [a[0] for a in runs] == ["run", "run"]
    for args in runs:
        joined = " ".join(args)
        assert "--network" not in args  # default bridge: it downloads databases
        assert "-i" not in args and "stdin_bytes" not in json.dumps(args)  # no repo tar
        assert f"grafter-scan-cache:{CACHE}" in joined and ":ro" not in joined
        assert "--cap-drop ALL" in joined and "no-new-privileges" in joined
    assert "--download-db-only" in runs[0][-1]
    assert "osv-vulnerabilities.storage.googleapis.com/PyPI/all.zip" in runs[1][-1]


def test_update_failure_is_reported_per_tool(
    fake: tuple[list[str], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    docker, _ = fake
    monkeypatch.setenv("FAKE_MODE", "update_fail")
    failures = DockerScanner(Scanning(), docker=docker).update()
    assert [f.split(":")[0] for f in failures] == ["trivy", "osv"]
    assert "exit 3" in failures[0]


def test_scan_is_offline_read_only_and_parses_sarif(
    fake: tuple[list[str], Path], repo: Path
) -> None:
    docker, log = fake
    results = DockerScanner(Scanning(), docker=docker).scan(repo)
    assert [r.tool for r in results] == ["trivy", "osv"] and not any(r.error for r in results)
    assert {f.rule_id for f in results[0].findings} == {
        "CVE-2018-18074",
        "CVE-2024-35195",
        "github-pat",
    }
    assert all(f.path in ("requirements.txt", "src/settings.py") for f in results[0].findings)
    assert len(results[1].findings) == 2
    creates = [c["args"] for c in calls(log) if c["args"][0] == "create"]
    assert len(creates) == 2
    for args in creates:
        joined = " ".join(args)
        assert "--network none" in joined and "--user 10001:10001" in joined
        assert f"grafter-scan-cache:{CACHE}:ro" in joined
    assert "OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY=/cache/osv" in " ".join(creates[1])
    starts = [c for c in calls(log) if c["args"][0] == "start"]
    assert all(c["stdin_bytes"] > 0 for c in starts)  # the repo went in as a tar
    assert sorted(p.name for p in repo.iterdir()) == [".git", "requirements.txt"]  # untouched


@pytest.mark.parametrize("mode", ["scan_fail", "no_sarif"])
def test_scan_failure_is_a_tool_error(
    fake: tuple[list[str], Path], repo: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    docker, _ = fake
    monkeypatch.setenv("FAKE_MODE", mode)
    results = DockerScanner(Scanning(tools=["osv"]), docker=docker).scan(repo)
    assert len(results) == 1 and results[0].error and results[0].findings == []


def test_unreadable_sarif(fake: tuple[list[str], Path], repo: Path, tmp_path: Path) -> None:
    docker, _ = fake
    (tmp_path / "sarif" / "trivy.sarif").write_text("not json", encoding="utf-8")
    (res,) = DockerScanner(Scanning(tools=["trivy"]), docker=docker).scan(repo)
    assert res.error.startswith("unreadable SARIF")


def test_scripts_are_offline_and_bounded() -> None:
    assert "--skip-db-update" in TRIVY_SCAN and "--offline-scan" in TRIVY_SCAN
    assert "--skip-dirs .grafter-scan" in TRIVY_SCAN
    assert "--offline" in OSV_SCAN and "128" in OSV_SCAN
    assert "chmod -R a+rX /cache/trivy" in trivy_update_script()
    script = osv_update_script(["PyPI", "crates.io"], 12)
    assert "-mmin -720" in script and "crates.io/all.zip" in script
    assert script.count("mv ") == 2  # atomic: download to .tmp, then rename


def test_bad_ecosystem_names_rejected() -> None:
    with pytest.raises(ValueError, match="ecosystem"):
        Scanning(osv_ecosystems=["PyPI; rm -rf /"])
