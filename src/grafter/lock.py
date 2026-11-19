"""Single-run lock file and kill-switch check. Standard library only.

One Grafter run at a time per host: the lock is a JSON file `{"pid", "started_at"}` created
with O_EXCL. A lock whose process is gone, that is older than `stale_after`, or that cannot be
read is taken over once; otherwise `LockHeld` is raised.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType

from grafter.config import KillSwitch

_MAX_PID = 2**32 - 1


class LockHeld(RuntimeError):
    """Another live run holds the lock."""

    def __init__(self, path: Path, pid: int | None, started_at: str | None) -> None:
        self.path = path
        self.pid = pid
        self.started_at = started_at
        super().__init__(f"run lock {path} is held by pid {pid} since {started_at}")


class KillSwitchEngaged(RuntimeError):
    """The kill switch is on; no run may start."""


def check_kill_switch(ks: KillSwitch, environ: Mapping[str, str] | None = None) -> None:
    if ks.is_engaged(environ):
        raise KillSwitchEngaged(
            f"kill switch engaged (env {ks.env} is truthy or file {ks.file} exists); "
            "unset the variable and remove the file to resume"
        )


def _posix_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by someone else
        return True
    return True


def _windows_pid_alive(pid: int) -> bool:  # pragma: no cover - exercised on Windows only
    if sys.platform != "win32":
        raise OSError("not Windows")
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    process_query_limited_information, error_access_denied, still_active = 0x1000, 5, 259

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return bool(ctypes.get_last_error() == error_access_denied)
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True  # cannot tell: assume alive
        return bool(code.value == still_active)
    finally:
        kernel32.CloseHandle(handle)


def pid_alive(pid: int) -> bool:
    if pid <= 0 or pid > _MAX_PID:
        return False
    return _windows_pid_alive(pid) if os.name == "nt" else _posix_pid_alive(pid)


@dataclass(frozen=True)
class _Owner:
    pid: int
    started_at: datetime

    @property
    def started_iso(self) -> str:
        return self.started_at.isoformat()


class RunLock:
    def __init__(self, path: Path, stale_after: timedelta = timedelta(hours=12)) -> None:
        self.path = Path(path)
        self.stale_after = stale_after
        self._mine: _Owner | None = None

    @property
    def held(self) -> bool:
        return self._mine is not None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._try_create():
            return
        owner = self._read()
        if owner is not None and not self._is_stale(owner):
            raise LockHeld(self.path, owner.pid, owner.started_iso)
        self._remove_stale()
        if self._try_create():
            return
        owner = self._read()  # lost the race to another process
        raise LockHeld(
            self.path, owner.pid if owner else None, owner.started_iso if owner else None
        )

    def release(self) -> None:
        mine, self._mine = self._mine, None
        if mine is not None and self._read() == mine:
            self.path.unlink(missing_ok=True)

    def __enter__(self) -> RunLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    def _try_create(self) -> bool:
        me = _Owner(pid=os.getpid(), started_at=datetime.now(UTC))
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"pid": me.pid, "started_at": me.started_iso}, fh)
        self._mine = me
        return True

    def _read(self) -> _Owner | None:
        """The lock's owner, or None when the file is missing or unreadable."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            pid, started = data["pid"], datetime.fromisoformat(data["started_at"])
        except (OSError, ValueError, TypeError, KeyError):
            return None
        if not isinstance(pid, int) or isinstance(pid, bool):
            return None
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        return _Owner(pid=pid, started_at=started)

    def _is_stale(self, owner: _Owner) -> bool:
        too_old = datetime.now(UTC) - owner.started_at > self.stale_after
        return too_old or not pid_alive(owner.pid)

    def _remove_stale(self) -> None:
        self.path.unlink(missing_ok=True)
