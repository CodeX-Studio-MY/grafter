"""Nightly run report (TODO 1D4)."""

from grafter.config import Config
from grafter.pipeline import RunSummary
from grafter.report import render_report

CFG = Config.model_validate(
    {
        "repos": [
            {
                "name": "api",
                "github": "o/r",
                "language": "python",
                "test_command": "pytest",
                "test_paths": ["tests/**"],
            }
        ],
        "limits": {"max_prs_per_night": 2, "token_budget_per_night": 1000},
    }
)
PR = {
    "repo": "api",
    "target": "src/a.py",
    "fingerprint": "f1",
    "branch": "ai/test-f1",
    "title": "[grafter] test: Add tests",
    "tier": 1,
}


def test_report_lists_prs_rejections_skips_and_tokens() -> None:
    s = RunSummary(
        run_id="20261120T020000Z-abc123",
        dry_run=False,
        prs=[{**PR, "dry_run": False, "number": 7, "url": "https://github.example/o/r/pull/7"}],
        rejected=[{**PR, "target": "src/b.py", "reasons": ["skips tests", "multi\nline | pipe"]}],
        skipped=[{**PR, "target": "src/c.py", "reason": "open PR #3"}],
        tokens=1234,
        stopped_reason="max_prs_per_night reached",
        errors=[{"repo": "web", "error": "GitError: clone failed"}],
    )
    md = render_report(s, CFG)
    assert md.startswith("# Grafter run 20261120T020000Z-abc123\n")
    assert "- Dry run: no" in md
    assert "- Stopped: max_prs_per_night reached" in md
    assert "- PRs opened: 1 (limit 2)" in md
    assert "- Tokens: 1,234 (budget 1,000)" in md
    assert "[#7](https://github.example/o/r/pull/7) [grafter] test: Add tests" in md
    assert "`api` `src/b.py`" in md
    assert "  - skips tests" in md
    assert "  - multi line \\| pipe" in md
    assert "`api` `src/c.py`: open PR #3" in md
    assert "## Errors" in md and "`web`: GitError: clone failed" in md
    assert "Would open" not in md


def test_report_dry_run_and_empty_sections() -> None:
    s = RunSummary(
        run_id="r1",
        dry_run=True,
        prs=[{**PR, "dry_run": True, "number": None, "url": None}],
        stopped_reason="completed",
    )
    md = render_report(s, CFG)
    assert "- Dry run: yes" in md
    assert "- PRs opened: 0 (limit 2)" in md
    assert "## Would open (dry run)" in md
    assert "would open PR: [grafter] test: Add tests" in md
    assert md.count("None.") == 3  # PRs, rejected, skipped
    assert "## Errors" not in md
