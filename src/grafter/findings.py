"""Scanner findings from SARIF 2.1.0 (TODO 1E2). Stored as candidate tasks; not acted on in
phase 1. Trivy and OSV-Scanner both write SARIF, so one parser serves both.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Literal

from grafter.charter import UnsafePathError, normalise_path
from grafter.secret_scan import RULES

Severity = Literal["low", "medium", "high", "critical"]
SEVERITIES: tuple[Severity, ...] = ("low", "medium", "high", "critical")
MESSAGE_MAX = 500
_SPACE = re.compile(r"\s+")
_LEVEL: dict[str, Severity] = {"error": "high", "warning": "medium", "note": "low", "none": "low"}


@dataclass(frozen=True)
class Finding:
    tool: str
    rule_id: str
    severity: Severity
    path: str  # repo-relative POSIX
    line: int | None
    message: str

    @property
    def fingerprint(self) -> str:
        """Same issue, same id across scans: tool, rule, file and message; not the line."""
        key = f"{self.tool}|{self.rule_id}|{self.path}|{self.message}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def severity_at_least(severity: str, minimum: str) -> bool:
    order: tuple[str, ...] = SEVERITIES
    return order.index(severity) >= order.index(minimum)


def _clean_message(text: str) -> str:
    """One line, at most MESSAGE_MAX chars, with anything secret-shaped masked."""
    flat = _SPACE.sub(" ", text).strip()
    for pattern in RULES.values():
        flat = pattern.sub(lambda m: m.group(0)[:4] + "***", flat)
    return flat[:MESSAGE_MAX]


def _severity(result: dict[str, Any], rule: dict[str, Any]) -> Severity:
    """CVSS `security-severity` when present (GitHub code scanning's mapping), else level."""
    props = rule.get("properties") if isinstance(rule.get("properties"), dict) else {}
    raw = props.get("security-severity") if isinstance(props, dict) else None
    try:
        score = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        score = None
    if score is not None:
        if score >= 9.0:
            return "critical"
        if score >= 7.0:
            return "high"
        if score >= 4.0:
            return "medium"
        return "low"
    level = result.get("level")
    if not isinstance(level, str):
        dc = rule.get("defaultConfiguration")
        level = dc.get("level") if isinstance(dc, dict) else None
    return _LEVEL.get(level or "", "medium")


def _repo_path(uri: str, root: str) -> str | None:
    """Repo-relative path for a SARIF artifact URI, or None if it points outside the repo."""
    path = uri.removeprefix("file://")
    root = root.rstrip("/") + "/"
    if path.startswith(root):
        path = path[len(root) :]
    try:
        return normalise_path(path)
    except UnsafePathError:
        return None


def parse_sarif(data: Any, tool: str, root: str = "/workspace") -> list[Finding]:
    """Findings from one SARIF log, de-duplicated by fingerprint. Results without a usable
    in-repo location are dropped; malformed parts are skipped, never raised."""
    out: dict[str, Finding] = {}
    runs = data.get("runs") if isinstance(data, dict) else None
    for run in runs if isinstance(runs, list) else []:
        if not isinstance(run, dict):
            continue
        driver = (run.get("tool") or {}).get("driver") or {}
        rules = {
            r["id"]: r
            for r in driver.get("rules") or []
            if isinstance(r, dict) and isinstance(r.get("id"), str)
        }
        for res in run.get("results") or []:
            if not isinstance(res, dict) or not isinstance(res.get("ruleId"), str):
                continue
            locs = res.get("locations") or []
            phys = locs[0].get("physicalLocation") if locs and isinstance(locs[0], dict) else None
            art = phys.get("artifactLocation") if isinstance(phys, dict) else None
            uri = art.get("uri") if isinstance(art, dict) else None
            path = _repo_path(uri, root) if isinstance(uri, str) else None
            if path is None:
                continue
            region = phys.get("region") if isinstance(phys, dict) else None
            line = region.get("startLine") if isinstance(region, dict) else None
            msg = res.get("message")
            text = msg.get("text") if isinstance(msg, dict) else None
            finding = Finding(
                tool=tool,
                rule_id=res["ruleId"],
                severity=_severity(res, rules.get(res["ruleId"], {})),
                path=path,
                line=line if isinstance(line, int) and line > 0 else None,
                message=_clean_message(text if isinstance(text, str) else res["ruleId"]),
            )
            out.setdefault(finding.fingerprint, finding)
    return list(out.values())
