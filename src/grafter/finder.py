"""Find work: `test` tasks for the lowest-covered files (TODO 1D1)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from grafter.charter import ChangeType, Charter, matches_any
from grafter.config import RepoConfig
from grafter.engines.base import Task
from grafter.runner import CommandRunner

CoverageFormat = Literal["coverage-json", "lcov"]


class CoverageError(RuntimeError):
    """The coverage command failed or its report is missing or unreadable."""


@dataclass
class CoverageSnapshot:
    total: float
    files: dict[str, float] = field(default_factory=dict)


def _norm_key(path: str) -> str:
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _pct(hit: int, found: int) -> float:
    return 100.0 if found == 0 else 100.0 * hit / found


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise CoverageError(f"cannot read coverage report {path}: {e}") from e
    if not isinstance(data, dict):
        raise CoverageError(f"coverage report {path} is not a JSON object")
    return data


def _lcov_records(path: Path) -> dict[str, tuple[int, int]]:
    """Per file (LH, LF) from an lcov tracefile. Records for the same file are summed."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise CoverageError(f"cannot read coverage report {path}: {e}") from e
    out: dict[str, tuple[int, int]] = {}
    current: str | None = None
    lh = lf = 0
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("SF:"):
            current, lh, lf = _norm_key(line[3:]), 0, 0
        elif line.startswith("LH:") and current is not None:
            lh = int(line[3:])
        elif line.startswith("LF:") and current is not None:
            lf = int(line[3:])
        elif line == "end_of_record" and current is not None:
            old_lh, old_lf = out.get(current, (0, 0))
            out[current] = (old_lh + lh, old_lf + lf)
            current = None
    return out


def read_coverage(path: Path, fmt: CoverageFormat) -> dict[str, float]:
    """Per-file percent covered, keyed by repo-relative POSIX path."""
    if fmt == "lcov":
        return {f: _pct(h, n) for f, (h, n) in _lcov_records(path).items()}
    try:
        files = _load_json(path)["files"]
        return {_norm_key(f): float(v["summary"]["percent_covered"]) for f, v in files.items()}
    except (KeyError, TypeError, AttributeError) as e:
        raise CoverageError(f"unexpected coverage-json layout in {path}: {e}") from e


def read_total(path: Path, fmt: CoverageFormat) -> float:
    """Overall percent covered (lcov: sum of LH / sum of LF)."""
    if fmt == "lcov":
        recs = _lcov_records(path).values()
        return _pct(sum(h for h, _ in recs), sum(n for _, n in recs))
    try:
        return float(_load_json(path)["totals"]["percent_covered"])
    except (KeyError, TypeError) as e:
        raise CoverageError(f"unexpected coverage-json layout in {path}: {e}") from e


def read_snapshot(path: Path, fmt: CoverageFormat) -> CoverageSnapshot:
    return CoverageSnapshot(total=read_total(path, fmt), files=read_coverage(path, fmt))


def measure_coverage(
    runner: CommandRunner, workdir: Path, repo: RepoConfig, timeout: float = 900
) -> CoverageSnapshot:
    """Run `repo.coverage_command` in `workdir` and read `repo.coverage_report`.

    A stale report is deleted first, so a command that writes nothing is caught.
    Raises CoverageError if coverage is not configured, the command fails or the
    report is missing.
    """
    if not repo.coverage_command or not repo.coverage_report:
        raise CoverageError(f"repo {repo.name!r}: coverage_command and coverage_report needed")
    report = workdir / repo.coverage_report
    report.unlink(missing_ok=True)
    res = runner.run(repo.coverage_command, workdir, timeout)
    if res.exit_code != 0:
        tail = (res.stdout + res.stderr).strip()[-1000:]
        raise CoverageError(f"coverage command failed (exit {res.exit_code}): {tail}")
    if not report.is_file():
        raise CoverageError(f"coverage report not written: {repo.coverage_report}")
    return read_snapshot(report, repo.coverage_format)


def fingerprint(repo: str, change_type: ChangeType | str, target: str) -> str:
    """Stable id for one change: the same work found twice gets the same id."""
    key = f"{repo}|{ChangeType(change_type).value}|{target}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


_WORD = re.compile(r"[a-z0-9]+")
_SUFFIXES = ("ing", "es", "s")


def _stem(word: str) -> str:
    for suf in _SUFFIXES:
        if word.endswith(suf) and len(word) - len(suf) >= 3:
            return word[: -len(suf)]
    return word


def _stems(text: str, min_len: int = 4) -> set[str]:
    """Stems of words with at least `min_len` letters: `lots` -> `lot`, `invoicing` -> `invoic`."""
    return {_stem(w) for w in _WORD.findall(text.lower()) if len(w) >= min_len}


def charter_domain(charter: Charter, path: str) -> str:
    """The charter core domain a file serves.

    First domain sharing a word stem with the file name, else with its directories,
    else the first core domain.
    """
    parts = path.split("/")
    name = parts[-1].rsplit(".", 1)[0]
    for haystack in (name, " ".join(parts[:-1])):
        words = _stems(haystack)
        for domain in charter.core_domains:
            if _stems(domain, min_len=3) & words:  # file words need 4+ letters
                return domain
    return charter.core_domains[0]


@dataclass(frozen=True)
class FoundTask:
    task: Task
    target: str  # repo-relative file the task is about
    coverage: float  # its percent covered when found


def find_test_tasks(
    repo: RepoConfig,
    charter: Charter,
    coverage: dict[str, float],
    limit: int = 5,
    min_gain_below: float = 100.0,
) -> list[FoundTask]:
    """`test` tasks for the lowest-covered files, lowest first (ties by path).

    Skips files at or above `min_gain_below` percent, protected or review files for
    `repo` (unsafe paths count as protected) and files matching `repo.test_paths`.
    """
    candidates = sorted(
        (pct, f)
        for f, pct in coverage.items()
        if pct < min_gain_below
        and not charter.is_protected(f, repo.name)
        and not charter.needs_review(f, repo.name)
        and not matches_any(f, repo.test_paths)
    )
    found: list[FoundTask] = []
    for pct, target in candidates[: max(limit, 0)]:
        domain = charter_domain(charter, target)
        instruction = (
            f"Add tests for {target} (now {pct:.0f}% line coverage). "
            f"Create or change files only under the test paths ({', '.join(repo.test_paths)}); "
            "do not change source files. Do not skip, delete or weaken existing tests. "
            f"Cite the charter domain '{domain}' in the summary."
        )
        task = Task(
            id=fingerprint(repo.name, ChangeType.TEST, target),
            repo=repo.name,
            change_type=ChangeType.TEST,
            charter_ref=f"domain: {domain}",
            instruction=instruction,
        )
        found.append(FoundTask(task=task, target=target, coverage=pct))
    return found
