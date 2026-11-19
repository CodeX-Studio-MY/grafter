"""Minimal secret scanner for unified diffs (no external dependency).

Only added lines are scanned. Findings never carry the full secret.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

RULES: dict[str, re.Pattern[str]] = {
    "aws": re.compile(r"AKIA[0-9A-Z]{16}"),
    "github": re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    "github_pat": re.compile(r"github_pat_[A-Za-z0-9_]{22,}"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "slack": re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    "sk": re.compile(r"sk-(ant-)?[A-Za-z0-9_-]{20,}"),
    "google": re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    "generic": re.compile(
        r"(password|passwd|secret|api_key|apikey|token)\s*[:=]\s*[\"'][^\"'\s]{12,}[\"']",
        re.IGNORECASE,
    ),
}


@dataclass(frozen=True)
class Finding:
    rule: str
    line_no: int  # 1-based line number within the diff text
    redacted: str


def redact(value: str) -> str:
    return value[:4] + "***"


def scan_diff(diff: str) -> list[Finding]:
    findings: list[Finding] = []
    for line_no, line in enumerate(diff.splitlines(), start=1):
        if not line.startswith("+") or line.startswith("+++"):
            continue
        added = line[1:]
        for rule, pattern in RULES.items():
            m = pattern.search(added)
            if m:
                findings.append(Finding(rule=rule, line_no=line_no, redacted=redact(m.group(0))))
    return findings
