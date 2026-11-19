import json
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from grafter.config import Config, Paths
from grafter.runlog import RunLog, new_run_id

REPOS = {
    "repos": [
        {
            "name": "api",
            "github": "org/api",
            "language": "python",
            "test_command": "pytest",
            "test_paths": ["tests/**"],
        }
    ]
}


def lines(log: RunLog) -> list[dict[str, object]]:
    return [json.loads(x) for x in log.path.read_text(encoding="utf-8").splitlines()]


# --- new_run_id -------------------------------------------------------------


def test_run_id_format_and_utc() -> None:
    sgt = timezone(timedelta(hours=8))
    rid = new_run_id(datetime(2026, 11, 21, 6, 0, 0, tzinfo=sgt))
    assert re.fullmatch(r"20261120T220000Z-[0-9a-f]{6}", rid)


def test_run_id_naive_is_utc_and_default_now() -> None:
    assert new_run_id(datetime(2026, 1, 2, 3, 4, 5)).startswith("20260102T030405Z-")
    rid = new_run_id()
    assert re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{6}", rid)
    assert rid != new_run_id()


# --- RunLog -----------------------------------------------------------------


def test_creates_dir_and_appends_lines(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "r1"
    log = RunLog(run_dir, "r1")
    assert run_dir.is_dir()
    assert log.path == run_dir / "run.jsonl"
    log.event("start", repo="api", n=3)
    log.event("end", ok=True, extra=None, ratio=0.5)
    first, second = lines(log)
    assert first["run_id"] == "r1" and first["kind"] == "start"
    assert first["repo"] == "api" and first["n"] == 3
    assert second == {**second, "kind": "end", "ok": True, "extra": None, "ratio": 0.5}
    ts = datetime.fromisoformat(str(first["ts"]))
    assert ts.utcoffset() == timedelta(0)


def test_appends_across_instances(tmp_path: Path) -> None:
    RunLog(tmp_path, "r").event("a")
    log = RunLog(tmp_path, "r")
    log.event("b")
    assert [x["kind"] for x in lines(log)] == ["a", "b"]


def test_each_line_flushed(tmp_path: Path) -> None:
    log = RunLog(tmp_path, "r")
    log.event("a")
    # readable before close
    assert len(lines(log)) == 1
    log.close()


def test_context_manager_closes(tmp_path: Path) -> None:
    with RunLog(tmp_path, "r") as log:
        log.event("a")
    log.close()  # idempotent
    log.event("after")  # reopens lazily
    assert [x["kind"] for x in lines(log)] == ["a", "after"]


@pytest.mark.parametrize(
    "secret",
    [
        "sk-1234",
        "ghp_" + "a" * 36,
        "token AKIA" + "A" * 16 + " leaked",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----",
        "xoxb-" + "1" * 12,
    ],
)
def test_secrets_redacted(tmp_path: Path, secret: str) -> None:
    log = RunLog(tmp_path, "r")
    log.event("x", value=secret, nested={"deep": [secret]})
    (rec,) = lines(log)
    raw = log.path.read_text(encoding="utf-8")
    assert secret not in raw
    assert rec["value"] == secret[:4] + "***"
    assert rec["nested"] == {"deep": [secret[:4] + "***"]}


def test_non_json_values_stringified_and_redacted(tmp_path: Path) -> None:
    class Leaky:
        def __str__(self) -> str:
            return "sk-ant-" + "x" * 30

    log = RunLog(tmp_path, "r")
    log.event(
        "x",
        path=Path("a/b"),
        when=datetime(2026, 1, 1, tzinfo=UTC),
        obj=Leaky(),
        items=(1, "two"),
        keys={1: "one"},
    )
    (rec,) = lines(log)
    assert rec["path"] == str(Path("a/b"))
    assert rec["when"] == "2026-01-01 00:00:00+00:00"
    assert rec["obj"] == "sk-a***"
    assert rec["items"] == [1, "two"]
    assert rec["keys"] == {"1": "one"}


def test_plain_strings_kept(tmp_path: Path) -> None:
    log = RunLog(tmp_path, "r")
    log.event("x", msg="opened PR #4 on ai/test-123")
    assert lines(log)[0]["msg"] == "opened PR #4 on ai/test-123"


@pytest.mark.parametrize("field", ["ts", "run_id"])
def test_reserved_fields_rejected(tmp_path: Path, field: str) -> None:
    with pytest.raises(ValueError, match="reserved"):
        RunLog(tmp_path, "r").event("x", **{field: "nope"})


# --- Config.paths -----------------------------------------------------------


def test_paths_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    c = Config.model_validate(REPOS)
    assert c.home == "~/.grafter"
    p = c.paths()
    home = tmp_path / ".grafter"
    assert p == Paths(
        home=home,
        runs_dir=home / "runs",
        lock_file=home / "grafter.lock",
        db_file=home / "grafter.db",
        work_dir=home / "work",
    )


def test_paths_env_vars_expanded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GRAFTER_TEST_HOME", str(tmp_path))
    c = Config.model_validate({**REPOS, "home": "$GRAFTER_TEST_HOME/g"})
    p = c.paths()
    assert p.home == tmp_path / "g"
    assert p.runs_dir == tmp_path / "g" / "runs"
    with pytest.raises(AttributeError):
        p.home = tmp_path  # type: ignore[misc]


def test_example_config_mentions_home() -> None:
    text = (Path(__file__).parent.parent / "grafter.example.yaml").read_text(encoding="utf-8")
    assert "# home: ~/.grafter" in text
