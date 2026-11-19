"""Run repo commands (test_command, coverage_command) without a shell.

`LocalRunner` runs on this host. A Docker runner (phase 1 sandbox) implements the same
`CommandRunner` protocol.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

MAX_OUTPUT = 20_000  # chars kept from the end of stdout / stderr
TIMEOUT_EXIT = 124  # like coreutils `timeout`
NOT_FOUND_EXIT = 127  # like a POSIX shell


@dataclass
class CmdResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float


class CommandRunner(Protocol):
    def run(self, cmd: str, cwd: Path, timeout: float = 900) -> CmdResult: ...


def _tail(text: str | bytes | None) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    return text[-MAX_OUTPUT:]


def resolve_args(cmd: str) -> list[str]:
    """Split `cmd` POSIX-style and resolve the executable.

    `pytest` -> `[sys.executable, "-m", "pytest"]` and `python` -> `[sys.executable]`, so
    runs stay inside Grafter's interpreter (its venv) on every OS. Other executables are
    resolved with `shutil.which` (finds `npm.cmd` etc. on Windows); unknown ones are kept.
    """
    args = shlex.split(cmd, posix=True)
    if not args:
        raise ValueError("empty command")
    head, rest = args[0], args[1:]
    if head == "pytest":
        return [sys.executable, "-m", "pytest", *rest]
    if head == "python":
        return [sys.executable, *rest]
    return [shutil.which(head) or head, *rest]


class LocalRunner:
    """Runs commands on this host with `shell=False`."""

    def run(self, cmd: str, cwd: Path, timeout: float = 900) -> CmdResult:
        args = resolve_args(cmd)
        start = time.monotonic()
        try:
            proc = subprocess.run(
                args,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            return CmdResult(TIMEOUT_EXIT, _tail(e.stdout), "timeout", time.monotonic() - start)
        except FileNotFoundError:
            return CmdResult(
                NOT_FOUND_EXIT, "", f"command not found: {args[0]}", time.monotonic() - start
            )
        return CmdResult(
            proc.returncode, _tail(proc.stdout), _tail(proc.stderr), time.monotonic() - start
        )
