"""Dependency, secret and misconfiguration scanners (TODO 1E2, D20): Trivy + OSV-Scanner.

Two phases, so repo content never meets the network:
1. `update()`: one container per tool refreshes its vulnerability databases into a named
   Docker volume. It has a network and **no repo content**.
2. `scan(workdir)`: each tool runs in a `DockerRunner` container (`--network none`, no
   capabilities, uid 10001) with the repo copied in and the cache volume **read-only**. Each
   writes SARIF, copied back to a temp dir (never into the work tree) and parsed.

Findings are candidate tasks only; phase 1 never acts on them.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from grafter.config import Scanning
from grafter.findings import Finding, parse_sarif
from grafter.runner import DockerRunner

CACHE = "/cache"
SCAN_ROOT = "/tmp/workspace"  # scanner images have no /workspace owned by uid 10001
OUT_DIR = ".grafter-scan"
OSV_DB_URL = "https://osv-vulnerabilities.storage.googleapis.com/{eco}/all.zip"
UPDATE_TIMEOUT_S = 1800
HARDENING = ("--cap-drop", "ALL", "--security-opt", "no-new-privileges")


@dataclass
class ToolResult:
    tool: str
    findings: list[Finding] = field(default_factory=list)
    error: str = ""  # set when the tool failed; findings are then not trusted


class Scanner(Protocol):
    def update(self) -> list[str]:
        """Refresh databases. Returns one message per tool that failed (scans still run
        on the cached databases, if any)."""
        ...

    def scan(self, workdir: Path) -> list[ToolResult]: ...


def trivy_update_script() -> str:
    return (
        f"trivy fs --download-db-only --cache-dir {CACHE}/trivy --no-progress --quiet"
        f" && chmod -R a+rX {CACHE}/trivy"
    )


def osv_update_script(ecosystems: Sequence[str], refresh_hours: int) -> str:
    """Download each ecosystem's DB unless it is fresh; atomic rename; world-readable."""
    minutes = refresh_hours * 60
    parts = ["set -e"]
    for eco in ecosystems:
        d = f"{CACHE}/osv/osv-scalibr/{eco}"
        f = f"{d}/all.zip"
        url = OSV_DB_URL.format(eco=eco)
        parts.append(
            f"mkdir -p {shlex.quote(d)}; "
            f'if [ -z "$(find {shlex.quote(f)} -mmin -{minutes} 2>/dev/null)" ]; then '
            f"wget -q -O {shlex.quote(f + '.tmp')} {shlex.quote(url)} && "
            f"mv {shlex.quote(f + '.tmp')} {shlex.quote(f)}; fi"
        )
    parts.append(f"chmod -R a+rX {CACHE}/osv")
    return "\n".join(parts)


TRIVY_SCAN = (
    f"mkdir -p {OUT_DIR} && trivy fs --cache-dir {CACHE}/trivy --skip-db-update"
    " --skip-java-db-update --skip-check-update --offline-scan --skip-version-check"
    " --disable-telemetry --no-progress --quiet --scanners vuln,secret,misconfig"
    f" --skip-dirs {OUT_DIR} --format sarif -o {OUT_DIR}/trivy.sarif ."
)
# OSV exits 1 when it finds vulnerabilities and 128 when there is nothing to scan.
OSV_SCAN = (
    f"mkdir -p {OUT_DIR} && /osv-scanner scan source --offline -r --format sarif"
    f" --output-file {OUT_DIR}/osv.sarif .; rc=$?; "
    f"if [ $rc -eq 128 ]; then echo '{{\"runs\": []}}' > {OUT_DIR}/osv.sarif; rc=0; fi; "
    "[ $rc -eq 0 ] || [ $rc -eq 1 ]"
)


class DockerScanner:
    def __init__(self, settings: Scanning, docker: Sequence[str] | None = None) -> None:
        self.s = settings
        self.docker = list(docker) if docker else [shutil.which("docker") or "docker"]

    def _images(self) -> dict[str, str]:
        return {"trivy": self.s.trivy_image, "osv": self.s.osv_image}

    def update(self) -> list[str]:
        scripts = {
            "trivy": trivy_update_script(),
            "osv": osv_update_script(self.s.osv_ecosystems, self.s.refresh_hours),
        }
        failures = []
        for tool in self.s.tools:
            cmd = [
                *self.docker,
                "run",
                "--rm",
                *HARDENING,
                "-v",
                f"{self.s.cache_volume}:{CACHE}",
                "--entrypoint",
                "sh",
                self._images()[tool],
                "-c",
                scripts[tool],
            ]
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=UPDATE_TIMEOUT_S, check=False
                )
            except subprocess.TimeoutExpired:
                failures.append(f"{tool}: database update timed out")
                continue
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout).strip()[-300:]
                failures.append(f"{tool}: database update failed (exit {proc.returncode}): {tail}")
        return failures

    def scan(self, workdir: Path) -> list[ToolResult]:
        commands = {
            "trivy": (TRIVY_SCAN, {}),
            "osv": (OSV_SCAN, {"OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY": f"{CACHE}/osv"}),
        }
        results = []
        for tool in self.s.tools:
            script, env = commands[tool]
            runner = DockerRunner(
                self._images()[tool],
                docker=self.docker,
                volumes=[(self.s.cache_volume, CACHE)],
                env=env,
                workdir=SCAN_ROOT,
            )
            out = f"{OUT_DIR}/{tool}.sarif"
            with tempfile.TemporaryDirectory(prefix="grafter-scan-") as tmp:
                res = runner.run(
                    f"sh -c {shlex.quote(script)}",
                    workdir,
                    timeout=self.s.timeout_s,
                    outputs=[out],
                    out_dir=Path(tmp),
                )
                report = Path(tmp) / out
                if res.exit_code != 0 or not report.is_file():
                    tail = (res.stderr or res.stdout).strip()[-300:]
                    results.append(ToolResult(tool, error=f"exit {res.exit_code}: {tail}"))
                    continue
                try:
                    data = json.loads(report.read_text("utf-8"))
                except (OSError, ValueError) as e:
                    results.append(ToolResult(tool, error=f"unreadable SARIF: {e}"))
                    continue
            results.append(ToolResult(tool, parse_sarif(data, tool, root=SCAN_ROOT)))
        return results
