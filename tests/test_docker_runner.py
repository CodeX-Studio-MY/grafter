"""DockerRunner against real Docker (TODO 1D2b Done-when): the gates give the same verdicts in
a sandbox container as on the host. Skipped unless `docker` runs and the sandbox image exists
(`scripts/build-sandbox.sh python`); CI builds the image and runs this file.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import tree_hash
from test_gates import (
    CONFIG,
    FAILING_TEST,
    GOOD_TEST,
    SANDBOX,
    TARGET,
    UNCOVERED_SOURCE,
    UNRELATED_TEST,
    apply,
)

from grafter.config import RepoConfig, load_config
from grafter.finder import CoverageSnapshot, measure_coverage
from grafter.gates import GateResult, run_gates
from grafter.runner import DockerRunner, LocalRunner, default_image

IMAGE = default_image("python")


def _docker_ready() -> bool:
    exe = shutil.which("docker")
    if exe is None:
        return False
    try:
        proc = subprocess.run(
            [exe, "image", "inspect", IMAGE], capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


pytestmark = pytest.mark.skipif(not _docker_ready(), reason=f"needs docker and {IMAGE}")

NETWORK_TEST = """\
import urllib.request


def test_reaches_the_internet() -> None:
    urllib.request.urlopen("https://pypi.org/simple/", timeout=5)
"""

SCENARIOS = {
    "good test": ({"tests/test_traceability.py": GOOD_TEST}, TARGET),
    "failing test": ({"tests/test_traceability.py": GOOD_TEST + "\n\n" + FAILING_TEST}, TARGET),
    "misses target": ({"tests/test_more_grading.py": UNRELATED_TEST}, TARGET),
    "coverage drop": ({"src/acme_harvest/extra.py": UNCOVERED_SOURCE}, None),
}


@pytest.fixture(scope="module")
def repo() -> RepoConfig:
    return load_config(CONFIG).repo("api")


@pytest.fixture(scope="module")
def docker() -> DockerRunner:
    return DockerRunner(IMAGE)


@pytest.fixture(scope="module")
def baselines(
    tmp_path_factory: pytest.TempPathFactory, repo: RepoConfig, docker: DockerRunner
) -> dict[str, CoverageSnapshot]:
    out = {}
    for name, runner in (("local", LocalRunner()), ("docker", docker)):
        work = tmp_path_factory.mktemp(f"base-{name}") / "sandbox"
        shutil.copytree(SANDBOX, work, ignore=shutil.ignore_patterns("__pycache__", ".*"))
        out[name] = measure_coverage(runner, work, repo)
    return out


def _verdict(g: GateResult) -> tuple[bool, list[str], float | None, float | None]:
    # Reasons keep their first line only: output tails differ (paths, timings).
    return (g.passed, [r.splitlines()[0] for r in g.reasons], g.coverage_after, g.target_after)


def leftovers() -> list[str]:
    exe = shutil.which("docker") or "docker"
    out = subprocess.run(
        [exe, "ps", "-a", "--filter", "name=grafter-run-", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return out.split()


def test_baseline_coverage_matches(baselines: dict[str, CoverageSnapshot]) -> None:
    local, docker = baselines["local"], baselines["docker"]
    assert docker.total == local.total
    assert docker.files == local.files
    print(f"\nbaseline: local {local.total:.2f}%  docker {docker.total:.2f}%")


@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_gates_same_verdict_in_docker(
    scenario: str,
    tmp_path: Path,
    repo: RepoConfig,
    docker: DockerRunner,
    baselines: dict[str, CoverageSnapshot],
) -> None:
    files, target = SCENARIOS[scenario]
    local_work = apply(tmp_path / "local", files)
    docker_work = apply(tmp_path / "docker", files)
    before = tree_hash(docker_work)

    local = run_gates(LocalRunner(), local_work, repo, baselines["local"], target)
    in_docker = run_gates(docker, docker_work, repo, baselines["docker"], target)

    assert _verdict(in_docker) == _verdict(local)
    print(f"\n{scenario}:\n  local  {_verdict(local)}\n  docker {_verdict(in_docker)}")
    # The container got a copy: only the coverage report came back.
    assert tree_hash(docker_work) == before
    extra = sorted(
        p.relative_to(docker_work).as_posix()
        for p in docker_work.rglob("*")
        if p.is_file() and not (SANDBOX / p.relative_to(docker_work)).exists()
    )
    assert set(extra) <= {*files, "coverage.json"}
    assert leftovers() == []


def test_network_is_blocked_in_docker(
    tmp_path: Path, repo: RepoConfig, docker: DockerRunner
) -> None:
    work = apply(tmp_path, {"tests/test_network.py": NETWORK_TEST})
    res = docker.run(repo.test_command, work, timeout=120)
    assert res.exit_code == 1, res.stdout
    assert "test_reaches_the_internet" in res.stdout
    assert any(
        s in res.stdout for s in ("Temporary failure in name resolution", "URLError", "Errno")
    )


def test_container_cannot_touch_host_tree(tmp_path: Path, docker: DockerRunner) -> None:
    work = apply(tmp_path, {})
    before = tree_hash(work)
    res = docker.run("sh -c 'rm -rf /workspace/src && echo pwned > /workspace/x'", work)
    assert res.exit_code == 0
    assert tree_hash(work) == before and not (work / "x").exists()


def test_runs_as_sandbox_user_without_capabilities(tmp_path: Path, docker: DockerRunner) -> None:
    work = apply(tmp_path, {})
    res = docker.run("sh -c 'id -u; grep CapEff /proc/self/status'", work)
    uid, cap = res.stdout.split("\n")[:2]
    assert uid.strip() == "10001"
    assert cap.split()[-1] == "0000000000000000"
