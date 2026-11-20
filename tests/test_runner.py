"""LocalRunner: shell-free command runs with timeout and output cap."""

import shutil
import sys
from pathlib import Path

import pytest

from grafter.runner import MAX_OUTPUT, CmdResult, CommandRunner, LocalRunner, _tail, resolve_args


def test_resolve_maps_pytest_and_python_to_this_interpreter() -> None:
    assert resolve_args("pytest -q --cov") == [sys.executable, "-m", "pytest", "-q", "--cov"]
    assert resolve_args("python -c 'print(1)'") == [sys.executable, "-c", "print(1)"]


def test_resolve_uses_which_and_keeps_unknown() -> None:
    git = shutil.which("git")
    assert git is not None
    assert resolve_args("git status") == [git, "status"]
    assert resolve_args("no-such-tool-xyz a") == ["no-such-tool-xyz", "a"]


def test_resolve_rejects_empty() -> None:
    with pytest.raises(ValueError):
        resolve_args("   ")


def test_run_captures_output_and_exit_code(tmp_path: Path) -> None:
    runner: CommandRunner = LocalRunner()
    code = "import os, sys; print(os.getcwd()); print('err', file=sys.stderr); sys.exit(3)"
    r = runner.run(f'python -c "{code}"', tmp_path)
    assert r.exit_code == 3
    assert Path(r.stdout.strip()).resolve() == tmp_path.resolve()
    assert r.stderr.strip() == "err"
    assert r.duration_s >= 0


def test_run_does_not_use_a_shell(tmp_path: Path) -> None:
    r = LocalRunner().run(
        "python -c \"import sys; print(sys.argv[1:])\" '&&' 'echo' '$HOME'", tmp_path
    )
    assert r.exit_code == 0
    assert r.stdout.strip() == "['&&', 'echo', '$HOME']"


def test_run_truncates_output_to_tail(tmp_path: Path) -> None:
    r = LocalRunner().run("python -c \"print('a' * 30000 + 'END')\"", tmp_path)
    assert len(r.stdout) == MAX_OUTPUT
    assert r.stdout.rstrip().endswith("END")


def test_run_timeout(tmp_path: Path) -> None:
    r = LocalRunner().run('python -c "import time; time.sleep(10)"', tmp_path, timeout=0.5)
    assert r == CmdResult(124, r.stdout, "timeout", r.duration_s)
    assert r.duration_s < 9


def test_run_missing_executable(tmp_path: Path) -> None:
    r = LocalRunner().run("no-such-tool-xyz --help", tmp_path)
    assert r.exit_code == 127
    assert "no-such-tool-xyz" in r.stderr


def test_tail_handles_none_and_bytes() -> None:
    assert _tail(None) == ""
    assert _tail(b"ok \xff") == "ok �"
    assert _tail("x" * (MAX_OUTPUT + 5)) == "x" * MAX_OUTPUT
