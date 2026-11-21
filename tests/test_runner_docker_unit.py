"""DockerRunner without Docker (TODO 1D2b): a fake `docker` script records every call."""

from __future__ import annotations

import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

from grafter.runner import (
    NOT_FOUND_EXIT,
    TIMEOUT_EXIT,
    DockerRunner,
    LocalRunner,
    RunnerError,
    default_image,
    extract_output,
    tree_tar,
)

FAKE = r"""
import json, os, sys, time
log = os.environ["FAKE_DOCKER_LOG"]
args = sys.argv[1:]
entry = {"args": args}
if args[0] == "start":
    data = sys.stdin.buffer.read()
    open(os.environ["FAKE_DOCKER_STDIN"], "wb").write(data)
    entry["stdin_bytes"] = len(data)
with open(log, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(entry) + "\n")
if args[0] == "create":
    if os.environ.get("FAKE_CREATE_FAIL"):
        sys.stderr.write("Unable to find image\n"); sys.exit(125)
    print("cid123"); sys.exit(0)
if args[0] == "start":
    time.sleep(float(os.environ.get("FAKE_START_SLEEP", "0")))
    print("3 passed"); sys.stderr.write("warn\n")
    sys.exit(int(os.environ.get("FAKE_START_EXIT", "0")))
if args[0] == "cp":
    path = os.environ.get("FAKE_CP_TAR", "")
    if not path:
        sys.stderr.write("Could not find the file\n"); sys.exit(1)
    sys.stdout.buffer.write(open(path, "rb").read()); sys.exit(0)
sys.exit(0)
"""


@pytest.fixture
def fake_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], Path]:
    script = tmp_path / "fake_docker.py"
    script.write_text(FAKE, encoding="utf-8")
    log = tmp_path / "docker.log"
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    monkeypatch.setenv("FAKE_DOCKER_STDIN", str(tmp_path / "stdin.tar"))
    for var in ("FAKE_CREATE_FAIL", "FAKE_START_SLEEP", "FAKE_START_EXIT", "FAKE_CP_TAR"):
        monkeypatch.delenv(var, raising=False)
    return [sys.executable, str(script)], log


def calls(log: Path) -> list[list[str]]:
    return [json.loads(line)["args"] for line in log.read_text("utf-8").splitlines()]


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "config").write_text("[remote] url=https://x-access-token:SECRET@x\n")
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("a = 1\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_a.py").write_text("def test_a(): pass\n", encoding="utf-8")
    return root


def one_file_tar(name: str, data: bytes, kind: bytes = tarfile.REGTYPE) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name)
        info.type = kind
        if kind == tarfile.REGTYPE:
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        else:
            info.linkname = "/etc/passwd"
            tar.addfile(info)
    return buf.getvalue()


def test_run_creates_hardened_offline_container_and_cleans_up(
    fake_docker: tuple[list[str], Path], tree: Path, tmp_path: Path
) -> None:
    docker, log = fake_docker
    res = DockerRunner("img:1", docker=docker).run("pytest -q -k 'a b'", tree, timeout=30)
    assert (res.exit_code, res.stdout.strip(), res.stderr.strip()) == (0, "3 passed", "warn")
    create, start, rm = calls(log)
    assert create[0] == "create" and create[1] == "-i"
    name = create[create.index("--name") + 1]
    assert name.startswith("grafter-run-")
    flags = " ".join(create)
    for flag in (
        "--network none",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--pids-limit 512",
        "--memory 2g",
        "--user 10001:10001",
    ):
        assert flag in flags
    assert "-w" not in create  # Docker would create the dir as root; the entry script does it
    assert "mkdir -p /workspace && tar -xf - -C /workspace" in flags
    assert "-v" not in create and "--volume" not in create and "--mount" not in create
    image_at = create.index("img:1")
    assert create[image_at + 1 : image_at + 3] == ["-c", create[image_at + 2]]
    assert create[image_at + 4 :] == ["pytest", "-q", "-k", "a b"]  # no shell parsing
    assert start == ["start", "-ai", name]
    assert rm == ["rm", "-f", name]


def test_tree_tar_excludes_git_and_sets_owner(tree: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(tree_tar(tree))) as tar:
        names = sorted(m.name for m in tar.getmembers())
        owners = {(m.uid, m.gid) for m in tar.getmembers()}
    assert names == ["src", "src/a.py", "tests", "tests/test_a.py"]
    assert owners == {(10001, 10001)}


def test_stdin_carries_the_tree(
    fake_docker: tuple[list[str], Path], tree: Path, tmp_path: Path
) -> None:
    docker, _ = fake_docker
    DockerRunner("img", docker=docker).run("true", tree)
    with tarfile.open(tmp_path / "stdin.tar") as tar:
        assert "tests/test_a.py" in tar.getnames()
        assert not any(n.startswith(".git") for n in tar.getnames())


def test_output_copied_back(
    fake_docker: tuple[list[str], Path], tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker, log = fake_docker
    report = tmp_path / "cp.tar"
    report.write_bytes(one_file_tar("coverage.json", b'{"totals": {}}'))
    monkeypatch.setenv("FAKE_CP_TAR", str(report))
    DockerRunner("img", docker=docker).run("pytest --cov", tree, outputs=["coverage.json"])
    assert (tree / "coverage.json").read_bytes() == b'{"totals": {}}'
    cp = calls(log)[2]
    assert cp[0] == "cp" and cp[1].endswith(":/workspace/coverage.json") and cp[2] == "-"
    assert calls(log)[-1][0] == "rm"


def test_missing_output_writes_nothing(fake_docker: tuple[list[str], Path], tree: Path) -> None:
    docker, _ = fake_docker
    DockerRunner("img", docker=docker).run("pytest", tree, outputs=["coverage.json"])
    assert not (tree / "coverage.json").exists()


def test_nonzero_exit_is_reported(
    fake_docker: tuple[list[str], Path], tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker, log = fake_docker
    monkeypatch.setenv("FAKE_START_EXIT", "1")
    assert DockerRunner("img", docker=docker).run("pytest", tree).exit_code == 1
    assert calls(log)[-1][0] == "rm"


def test_timeout_kills_and_removes(
    fake_docker: tuple[list[str], Path], tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker, log = fake_docker
    monkeypatch.setenv("FAKE_START_SLEEP", "5")
    res = DockerRunner("img", docker=docker).run("pytest", tree, timeout=1)
    assert res.exit_code == TIMEOUT_EXIT and res.stderr == "timeout"
    assert [c[0] for c in calls(log)][-2:] == ["kill", "rm"]


def test_create_failure(
    fake_docker: tuple[list[str], Path], tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker, log = fake_docker
    monkeypatch.setenv("FAKE_CREATE_FAIL", "1")
    res = DockerRunner("img", docker=docker).run("pytest", tree)
    assert res.exit_code == NOT_FOUND_EXIT and "Unable to find image" in res.stderr
    assert [c[0] for c in calls(log)] == ["create", "rm"]


def test_rejects_bad_inputs(fake_docker: tuple[list[str], Path], tree: Path) -> None:
    docker, log = fake_docker
    runner = DockerRunner("img", docker=docker)
    with pytest.raises(ValueError, match="empty"):
        runner.run("  ", tree)
    for bad in ("../x", "/etc/passwd", ""):
        with pytest.raises(RunnerError, match="unsafe output"):
            runner.run("pytest", tree, outputs=[bad])
    assert not log.exists()  # nothing reached docker
    with pytest.raises(RunnerError, match="no sandbox image"):
        DockerRunner("")


def test_extract_output_refuses_links_and_extra_members(tmp_path: Path) -> None:
    link = one_file_tar("coverage.json", b"", kind=tarfile.SYMTYPE)
    with pytest.raises(RunnerError, match="one regular file"):
        extract_output(link, "coverage.json", tmp_path)
    other = one_file_tar("evil.json", b"x")
    with pytest.raises(RunnerError, match="one regular file"):
        extract_output(other, "coverage.json", tmp_path)
    assert extract_output(b"", "coverage.json", tmp_path) is False
    assert extract_output(one_file_tar("c.json", b"1"), "out/c.json", tmp_path)
    assert (tmp_path / "out" / "c.json").read_bytes() == b"1"
    assert list(tmp_path.iterdir()) == [tmp_path / "out"]


def test_default_image_and_local_runner_accepts_outputs(tmp_path: Path) -> None:
    assert default_image("python") == "grafter-sandbox-python:latest"
    res = LocalRunner().run("python -c \"print('hi')\"", tmp_path, outputs=["x.json"])
    assert res.exit_code == 0 and res.stdout.strip() == "hi"


def test_read_only_volumes_env_and_out_dir(
    fake_docker: tuple[list[str], Path], tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker, log = fake_docker
    report = tmp_path / "cp.tar"
    report.write_bytes(one_file_tar("t.sarif", b"{}"))
    monkeypatch.setenv("FAKE_CP_TAR", str(report))
    out = tmp_path / "out"
    runner = DockerRunner(
        "img", docker=docker, volumes=[("grafter-scan-cache", "/cache")], env={"A_B": "/cache/x"}
    )
    runner.run("trivy fs .", tree, outputs=[".grafter-scan/t.sarif"], out_dir=out)
    create = calls(log)[0]
    assert create[create.index("-v") + 1] == "grafter-scan-cache:/cache:ro"
    assert create[create.index("-e") + 1] == "A_B=/cache/x"
    assert "--network" in create and create[create.index("--network") + 1] == "none"
    assert (out / ".grafter-scan" / "t.sarif").read_bytes() == b"{}"
    assert not (tree / ".grafter-scan").exists()  # nothing written into the work tree


@pytest.mark.parametrize(
    ("volumes", "env"),
    [
        ([("C:/host/dir", "/cache")], {}),  # a host path, not a named volume
        ([("/etc", "/cache")], {}),
        ([("cache", "relative")], {}),
        ([("cache", "/c:rw")], {}),
        ([], {"bad-name": "x"}),
    ],
)
def test_rejects_bad_mounts_and_env(volumes: list[tuple[str, str]], env: dict[str, str]) -> None:
    with pytest.raises(RunnerError, match="bad"):
        DockerRunner("img", volumes=volumes, env=env)
