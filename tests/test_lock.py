import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from grafter import lock as lock_mod
from grafter.config import KillSwitch
from grafter.lock import KillSwitchEngaged, LockHeld, RunLock, check_kill_switch, pid_alive


@pytest.fixture(scope="module")
def dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def write_lock(path: Path, pid: int, started: datetime | None = None) -> None:
    started = started or datetime.now(UTC)
    path.write_text(json.dumps({"pid": pid, "started_at": started.isoformat()}), encoding="utf-8")


# --- pid_alive --------------------------------------------------------------


def test_pid_alive(dead_pid: int) -> None:
    assert pid_alive(os.getpid())
    assert not pid_alive(dead_pid)
    assert not pid_alive(0)
    assert not pid_alive(-5)
    assert not pid_alive(2**40)


@pytest.mark.parametrize(
    ("exc", "alive"), [(None, True), (ProcessLookupError, False), (PermissionError, True)]
)
def test_posix_pid_alive(
    monkeypatch: pytest.MonkeyPatch, exc: type[OSError] | None, alive: bool
) -> None:
    calls: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        calls.append((pid, sig))
        if exc is not None:
            raise exc

    monkeypatch.setattr(lock_mod.os, "kill", fake_kill)
    assert lock_mod._posix_pid_alive(1234) is alive
    assert calls == [(1234, 0)]


# --- RunLock ----------------------------------------------------------------


def test_acquire_and_release(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "grafter.lock"
    with RunLock(path) as lock:
        assert lock.held
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["pid"] == os.getpid()
        datetime.fromisoformat(data["started_at"])
    assert not path.exists()
    assert not lock.held


def test_second_acquire_raises(tmp_path: Path) -> None:
    path = tmp_path / "grafter.lock"
    with RunLock(path):
        with pytest.raises(LockHeld, match=f"pid {os.getpid()}") as err:
            with RunLock(path):
                pass  # pragma: no cover
        assert err.value.pid == os.getpid()
        assert err.value.started_at
        assert path.exists()
    assert not path.exists()


def test_held_by_other_live_process(tmp_path: Path) -> None:
    path = tmp_path / "grafter.lock"
    write_lock(path, os.getpid(), datetime.now(UTC) - timedelta(hours=1))
    with pytest.raises(LockHeld):
        RunLock(path).acquire()


def test_stale_by_age_is_taken_over(tmp_path: Path) -> None:
    path = tmp_path / "grafter.lock"
    write_lock(path, os.getpid(), datetime.now(UTC) - timedelta(hours=13))
    with RunLock(path) as lock:
        assert lock.held
    assert not path.exists()


def test_custom_stale_after(tmp_path: Path) -> None:
    path = tmp_path / "grafter.lock"
    write_lock(path, os.getpid(), datetime.now(UTC) - timedelta(minutes=10))
    with RunLock(path, stale_after=timedelta(minutes=5)):
        pass


def test_stale_by_dead_pid(tmp_path: Path, dead_pid: int) -> None:
    path = tmp_path / "grafter.lock"
    write_lock(path, dead_pid)
    with RunLock(path):
        assert json.loads(path.read_text(encoding="utf-8"))["pid"] == os.getpid()


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not json",
        "[1, 2]",
        '{"pid": "x", "started_at": "2026-01-01T00:00:00+00:00"}',
        '{"pid": 1}',
        '{"pid": 1, "started_at": "yesterday"}',
        '{"pid": true, "started_at": "2026-01-01T00:00:00+00:00"}',
    ],
)
def test_corrupt_lock_is_taken_over(tmp_path: Path, content: str) -> None:
    path = tmp_path / "grafter.lock"
    path.write_text(content, encoding="utf-8")
    with RunLock(path) as lock:
        assert lock.held


def test_naive_started_at_treated_as_utc(tmp_path: Path) -> None:
    path = tmp_path / "grafter.lock"
    old = (datetime.now(UTC) - timedelta(hours=20)).replace(tzinfo=None)
    path.write_text(
        json.dumps({"pid": os.getpid(), "started_at": old.isoformat()}), encoding="utf-8"
    )
    with RunLock(path):
        pass


def test_release_keeps_foreign_lock(tmp_path: Path, dead_pid: int) -> None:
    path = tmp_path / "grafter.lock"
    lock = RunLock(path)
    lock.acquire()
    write_lock(path, dead_pid)  # someone else took it over
    lock.release()
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["pid"] == dead_pid


def test_release_tolerates_missing_or_corrupt(tmp_path: Path) -> None:
    path = tmp_path / "grafter.lock"
    lock = RunLock(path)
    lock.release()  # not held: no-op
    lock.acquire()
    path.unlink()
    lock.release()
    lock.acquire()
    path.write_text("garbage", encoding="utf-8")
    lock.release()
    assert path.read_text(encoding="utf-8") == "garbage"


def test_lost_race_after_takeover(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "grafter.lock"
    write_lock(path, os.getpid(), datetime.now(UTC) - timedelta(days=2))
    lock = RunLock(path)
    real_remove = lock._remove_stale

    def remove_then_race() -> None:
        real_remove()
        write_lock(path, os.getpid())  # another process grabbed it first

    monkeypatch.setattr(lock, "_remove_stale", remove_then_race)
    with pytest.raises(LockHeld):
        lock.acquire()


def test_lock_vanishes_between_create_and_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "grafter.lock"
    write_lock(path, os.getpid())
    lock = RunLock(path)

    def gone() -> None:
        path.unlink()
        return None

    monkeypatch.setattr(lock, "_read", gone)
    lock.acquire()
    assert lock.held
    lock.release()


# --- kill switch ------------------------------------------------------------


def test_kill_switch_off(tmp_path: Path) -> None:
    check_kill_switch(KillSwitch(file=str(tmp_path / "STOP")), environ={})


def test_kill_switch_env(tmp_path: Path) -> None:
    ks = KillSwitch(file=str(tmp_path / "STOP"), env="X_STOP")
    with pytest.raises(KillSwitchEngaged, match="X_STOP"):
        check_kill_switch(ks, environ={"X_STOP": "1"})


def test_kill_switch_file(tmp_path: Path) -> None:
    stop = tmp_path / "STOP"
    stop.touch()
    with pytest.raises(KillSwitchEngaged, match="STOP"):
        check_kill_switch(KillSwitch(file=str(stop)), environ={})


def test_kill_switch_default_environ(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X_STOP2", "yes")
    with pytest.raises(KillSwitchEngaged):
        check_kill_switch(KillSwitch(file=str(tmp_path / "STOP"), env="X_STOP2"))
