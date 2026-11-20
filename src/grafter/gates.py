"""Verification gates after the engine ran (TODO 1D2).

The full test suite must pass, overall coverage must not drop below the baseline and,
for a targeted `test` change, the target file's coverage must improve. `guard.check`
runs separately. A failed gate means the change is rejected with these reasons, no PR.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from grafter.config import RepoConfig
from grafter.finder import CoverageError, CoverageSnapshot, measure_coverage
from grafter.runner import CommandRunner

COVERAGE_TOLERANCE = 0.01  # percentage points; absorbs float noise
OUTPUT_TAIL = 1000  # chars of command output kept in a failure reason


@dataclass
class GateResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    tests_summary: str = ""
    coverage_before: float | None = None
    coverage_after: float | None = None
    target_before: float | None = None
    target_after: float | None = None


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def run_gates(
    runner: CommandRunner,
    workdir: Path,
    repo: RepoConfig,
    baseline: CoverageSnapshot,
    target: str | None,
    timeout: float = 900,
) -> GateResult:
    """Run the gates in `workdir` (the engine's changed checkout)."""
    target_before = baseline.files.get(target, 0.0) if target else None
    result = GateResult(
        passed=False,
        coverage_before=baseline.total,
        target_before=target_before,
    )

    tests = runner.run(repo.test_command, workdir, timeout)
    result.tests_summary = _last_line(tests.stdout) or _last_line(tests.stderr)
    if tests.exit_code != 0:
        tail = (tests.stdout + tests.stderr).strip()[-OUTPUT_TAIL:]
        result.reasons.append(f"tests failed (exit {tests.exit_code}): {tail}")
        return result

    try:
        after = measure_coverage(runner, workdir, repo, timeout)
    except CoverageError as e:
        result.reasons.append(str(e))
        return result
    result.coverage_after = after.total

    if after.total < baseline.total - COVERAGE_TOLERANCE:
        result.reasons.append(f"coverage dropped: {baseline.total:.2f}% -> {after.total:.2f}%")
    if target:
        result.target_after = after.files.get(target)
        if result.target_after is None:
            result.reasons.append(f"target file missing from coverage report: {target}")
        elif target_before is not None and result.target_after <= target_before:
            result.reasons.append(
                f"target file coverage did not improve: {target} "
                f"{target_before:.2f}% -> {result.target_after:.2f}%"
            )

    result.passed = not result.reasons
    return result
