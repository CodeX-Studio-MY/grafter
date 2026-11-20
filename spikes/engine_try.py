"""TODO 1C3a Done-when: one real OpenHandsEngine run on the sandbox repo, then the guard.

Nothing agent-written runs on this host: the agent and its tests run in the container, the
diff comes back as a patch, and only git touches it here. Gates (which would execute the
tests) are NOT run; that needs the Docker runner (1D2b).

    .venv-oh/Scripts/python spikes/engine_try.py      # Windows; needs Docker + local gateway

Reads LITELLM_MASTER_KEY from deploy/litellm/.env unless it is already set.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")

from grafter.charter import load_charter  # noqa: E402
from grafter.config import Config  # noqa: E402
from grafter.engines import get_engine  # noqa: E402
from grafter.finder import find_test_tasks  # noqa: E402
from grafter.gitops import GitRepo  # noqa: E402
from grafter.guard import check  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = ROOT / "examples" / "sandbox-repo"
TARGET = "src/acme_harvest/grading.py"


def master_key() -> None:
    if os.environ.get("LITELLM_MASTER_KEY"):
        return
    for line in (ROOT / "deploy/litellm/.env").read_text(encoding="utf-8").splitlines():
        if line.startswith("LITELLM_MASTER_KEY="):
            os.environ["LITELLM_MASTER_KEY"] = line.split("=", 1)[1].strip()
            return
    raise SystemExit("LITELLM_MASTER_KEY not set and not in deploy/litellm/.env")


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=cwd, check=True)


def main() -> int:
    master_key()
    with tempfile.TemporaryDirectory(prefix="grafter-try-") as tmp:
        work = Path(tmp) / "work"
        shutil.copytree(
            SANDBOX,
            work,
            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".coverage"),
        )
        git(work, "init", "-q", "-b", "main")
        git(work, "add", "-A")
        git(
            work, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "base"
        )

        cfg = Config.model_validate(
            {
                "home": str(Path(tmp) / "home"),
                "repos": [
                    {
                        "name": "api",
                        "github": "acme/grafter-sandbox",
                        "language": "python",
                        "test_command": "python -m pytest -q -p no:cacheprovider",
                        "test_paths": ["tests/**"],
                    }
                ],
                "engine": "openhands",
                "llm_gateway_url": "http://127.0.0.1:4000",
                "limits": {"max_prs_per_night": 1, "usd_budget_per_night": 1.0},
            }
        )
        charter = load_charter(work / "grafter.charter.yaml")
        task = find_test_tasks(cfg.repos[0], charter, {TARGET: 40.0}, limit=1)[0].task
        print(f"task {task.id}: {task.instruction}\n")

        engine = get_engine("openhands", cfg)
        started = time.monotonic()
        result = engine.run(task, work, charter)
        print(
            f"engine: success={result.success} tokens={result.tokens_used} "
            f"cost=${result.cost_usd:.4f} ({time.monotonic() - started:.0f}s)"
        )
        print(f"summary:\n{result.summary}\n")
        if result.risks:
            print("risks:", result.risks)

        repo = GitRepo(work)
        stats = repo.diff_stats()
        diff = repo.diff()
        print(f"--- diff on the host ({stats.added}+ {stats.removed}-) ---\n{diff}")
        from grafter.engines.base import RunResult

        real = RunResult(
            success=result.success,
            changed_files=[f for f in stats.changed_files if f not in stats.deleted_files],
            deleted_files=stats.deleted_files,
            lines_added=stats.added,
            lines_removed=stats.removed,
            diff=diff,
            summary=result.summary,
        )
        verdict = check(task, real, charter, cfg.limits, cfg.repos[0])
        print(
            f"guard: allowed={verdict.allowed} tier={int(verdict.tier)} reasons={verdict.reasons}"
        )
        leftovers = subprocess.run(
            ["docker", "ps", "-a", "--filter", "name=agent-server", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        print("leftover containers:", leftovers or "none")
        ok = result.success and verdict.allowed and bool(stats.changed_files) and not leftovers
        print("DONE-WHEN:", "PASS" if ok else "FAIL")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
