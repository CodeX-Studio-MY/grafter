"""Nightly run report as markdown (TODO 1D4): `<runs_dir>/<run_id>/report.md`."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from grafter.config import Config

if TYPE_CHECKING:  # pragma: no cover
    from grafter.pipeline import RunSummary

REPORT_FILE = "report.md"
COMMENT_MAX = 60_000  # GitHub's comment limit is 65,536 characters


def comment_body(text: str, path: str) -> str:
    """The report as an issue comment, cut to `COMMENT_MAX` with a pointer to the full file."""
    if len(text) <= COMMENT_MAX:
        return text
    note = f"\n\n---\nTruncated: the full report is at `{path}` on the Grafter host.\n"
    return text[: COMMENT_MAX - len(note)] + note


def _cell(text: Any) -> str:
    """One markdown table cell: no newlines, no column breaks."""
    return str(text).replace("\r", " ").replace("\n", " ").replace("|", "\\|").strip()


def _where(item: dict[str, Any]) -> str:
    return f"`{_cell(item.get('repo', ''))}` `{_cell(item.get('target', ''))}`"


def render_report(summary: RunSummary, cfg: Config) -> str:
    real = [p for p in summary.prs if not p.get("dry_run")]
    would = [p for p in summary.prs if p.get("dry_run")]
    lines = [
        f"# Grafter run {summary.run_id}",
        "",
        f"- Dry run: {'yes' if summary.dry_run else 'no'}",
        f"- Stopped: {_cell(summary.stopped_reason)}",
        f"- PRs opened: {len(real)} (limit {cfg.limits.max_prs_per_night})",
        f"- Tokens: {summary.tokens:,} (budget {cfg.limits.token_budget_per_night:,})",
        f"- Cost: ${summary.cost_usd:.2f} (budget ${cfg.limits.usd_budget_per_night:.2f})",
        f"- Repos: {', '.join(r.name for r in cfg.repos)}",
        "",
        "## Pull requests",
        "",
    ]
    if real:
        for p in real:
            link = p.get("url") or f"#{p.get('number')}"
            lines.append(
                f"- [#{p.get('number')}]({link}) {_cell(p.get('title', ''))} "
                f"({_where(p)}, tier {p.get('tier')}, branch `{_cell(p.get('branch', ''))}`)"
            )
    else:
        lines.append("None.")
    if would:
        lines += ["", "## Would open (dry run)", ""]
        for p in would:
            lines.append(
                f"- would open PR: {_cell(p.get('title', ''))} ({_where(p)}, tier {p.get('tier')}, "
                f"branch `{_cell(p.get('branch', ''))}`)"
            )
    lines += ["", "## Rejected", ""]
    if summary.rejected:
        for r in summary.rejected:
            lines.append(f"- {_where(r)}")
            lines += [f"  - {_cell(reason)[:500]}" for reason in r.get("reasons", [])]
    else:
        lines.append("None.")
    lines += ["", "## Skipped", ""]
    if summary.skipped:
        lines += [f"- {_where(s)}: {_cell(s.get('reason', ''))}" for s in summary.skipped]
    else:
        lines.append("None.")
    if summary.errors:
        lines += ["", "## Errors", ""]
        lines += [
            f"- `{_cell(e.get('repo', ''))}`: {_cell(e.get('error', ''))[:500]}"
            for e in summary.errors
        ]
    lines.append("")
    return "\n".join(lines)
