"""Run repo commands (test_command, coverage_command) without a shell.

`LocalRunner` runs on this host: offline testing only. `DockerRunner` (TODO 1D2b) runs each
command in a fresh, offline, hardened sandbox container; the work tree is copied in (never
mounted) and only the files a caller names in `outputs` are copied back.
"""

from __future__ import annotations

import io
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
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
    def run(
        self, cmd: str, cwd: Path, timeout: float = 900, outputs: Sequence[str] = ()
    ) -> CmdResult:
        """Run `cmd` in `cwd`. `outputs`: repo-relative files the caller reads afterwards
        (e.g. the coverage report); a runner that works on a copy must bring them back."""
        ...


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

    def run(
        self, cmd: str, cwd: Path, timeout: float = 900, outputs: Sequence[str] = ()
    ) -> CmdResult:
        args = resolve_args(cmd)  # outputs: written in place already
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


# --- Docker -------------------------------------------------------------------------------

SANDBOX_USER = "10001:10001"  # the `sandbox` user in sandboxes/*/Dockerfile
WORKDIR = "/workspace"
DOCKER_TIMEOUT_S = 120  # create / cp / rm


# Extract the tar on stdin as the sandbox user, then exec the command (no shell parsing of it).
def entry(workdir: str) -> list[str]:
    """Extract the tar on stdin into `workdir` (created if missing), then exec the command."""
    return [
        "sh",
        "-c",
        f'mkdir -p {workdir} && tar -xf - -C {workdir} && cd {workdir} && exec "$@"',
        "sh",
    ]


ENTRY = entry(WORKDIR)
HARDENING = (
    "--network",
    "none",
    "--cap-drop",
    "ALL",
    "--security-opt",
    "no-new-privileges",
    "--pids-limit",
    "512",
    "--memory",
    "2g",
    "--cpus",
    "2",
)


_VOLUME_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")  # named volumes only, never host paths
_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")


class RunnerError(RuntimeError):
    pass


def default_image(language: str) -> str:
    return f"grafter-sandbox-{language}:latest"


def tree_tar(root: Path) -> bytes:
    """The work tree minus `.git/` as an uncompressed tar (links kept as links)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root)
            if rel.parts[0] == ".git":
                continue
            info = tar.gettarinfo(str(path), arcname=rel.as_posix())
            info.uid = info.gid = 10001
            info.uname = info.gname = "sandbox"
            if info.isfile():
                with path.open("rb") as fh:
                    tar.addfile(info, fh)
            else:
                tar.addfile(info)
    return buf.getvalue()


def _safe_output(name: str) -> PurePosixPath:
    rel = PurePosixPath(name.replace("\\", "/"))
    if rel.is_absolute() or not rel.parts or ".." in rel.parts:
        raise RunnerError(f"unsafe output path: {name!r}")
    return rel


def extract_output(tar_bytes: bytes, name: str, cwd: Path) -> bool:
    """Write the single regular file `name` from a `docker cp ... -` tar into `cwd`.
    Returns False when the tar is empty; raises on anything but exactly that one file."""
    rel = _safe_output(name)
    if not tar_bytes:
        return False
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tar:
        members = tar.getmembers()
        if len(members) != 1 or members[0].name != rel.name or not members[0].isfile():
            got = [(m.name, m.type) for m in members]
            raise RunnerError(f"output {name!r}: expected one regular file, got {got}")
        src = tar.extractfile(members[0])
        assert src is not None
        data = src.read()
    dest = cwd.joinpath(*rel.parts)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return True


class DockerRunner:
    """Each `run` is a new container from `image`: no network, no capabilities, non-root,
    resource limits, no host mounts. The tree goes in as a tar on stdin."""

    def __init__(
        self,
        image: str,
        docker: Sequence[str] | None = None,
        volumes: Sequence[tuple[str, str]] = (),
        env: Mapping[str, str] | None = None,
        workdir: str = WORKDIR,
    ) -> None:
        """`volumes`: (named volume, container path) pairs, always mounted read-only (1E2's
        scanner cache). `env`: plain, non-secret settings for the container."""
        if not image:
            raise RunnerError("no sandbox image")
        for volume, target in volumes:
            if not _VOLUME_NAME.fullmatch(volume) or not target.startswith("/") or ":" in target:
                raise RunnerError(f"bad volume mount: {volume!r} -> {target!r}")
        for key in env or {}:
            if not _ENV_NAME.fullmatch(key):
                raise RunnerError(f"bad env name: {key!r}")
        if not workdir.startswith("/") or " " in workdir:
            raise RunnerError(f"bad workdir: {workdir!r}")
        self.image = image
        self.workdir = workdir
        self.volumes = list(volumes)
        self.env = dict(env or {})
        exe = shutil.which("docker")
        self.docker = list(docker) if docker else [exe or "docker"]

    def _docker(
        self, *args: str, input: bytes | None = None, timeout: float = DOCKER_TIMEOUT_S
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [*self.docker, *args],
            input=input,
            capture_output=True,
            timeout=timeout,
            shell=False,
            check=False,
        )

    def run(
        self,
        cmd: str,
        cwd: Path,
        timeout: float = 900,
        outputs: Sequence[str] = (),
        out_dir: Path | None = None,
    ) -> CmdResult:
        """`out_dir`: where `outputs` land (default `cwd`); scanners keep them out of the tree."""
        args = shlex.split(cmd, posix=True)
        if not args:
            raise ValueError("empty command")
        for name in outputs:
            _safe_output(name)
        payload = tree_tar(cwd)
        name = f"grafter-run-{uuid.uuid4().hex[:12]}"
        start = time.monotonic()
        try:
            created = self._docker(
                "create",
                "-i",
                "--name",
                name,
                *HARDENING,
                "--user",
                SANDBOX_USER,
                *(f for v, t in self.volumes for f in ("-v", f"{v}:{t}:ro")),
                *(f for k, v in self.env.items() for f in ("-e", f"{k}={v}")),
                # No -w: Docker would create the dir as root; the entry script makes it.
                "--entrypoint",
                "sh",
                self.image,
                *entry(self.workdir)[1:],
                *args,
            )
            if created.returncode != 0:
                err = _tail(created.stderr)
                return CmdResult(NOT_FOUND_EXIT, "", f"docker create failed: {err}", 0.0)
            try:
                proc = self._docker("start", "-ai", name, input=payload, timeout=timeout)
            except subprocess.TimeoutExpired as e:
                self._docker("kill", name)
                return CmdResult(TIMEOUT_EXIT, _tail(e.stdout), "timeout", time.monotonic() - start)
            result = CmdResult(
                proc.returncode, _tail(proc.stdout), _tail(proc.stderr), time.monotonic() - start
            )
            for out in outputs:
                rel = _safe_output(out)
                copied = self._docker("cp", f"{name}:{self.workdir}/{rel.as_posix()}", "-")
                if copied.returncode == 0:
                    extract_output(copied.stdout, out, out_dir or cwd)
            return result
        finally:
            self._docker("rm", "-f", name)
