"""SARIF parsing (TODO 1E2), on real-shaped output from Trivy 0.76.2 and OSV-Scanner 2.7.0."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from grafter.findings import Finding, parse_sarif, severity_at_least

FIX = Path(__file__).parent / "fixtures" / "sarif"


def load(name: str) -> Any:
    return json.loads((FIX / name).read_text("utf-8"))


def test_trivy_vulns_and_secret() -> None:
    found = {f.rule_id: f for f in parse_sarif(load("trivy.sarif"), "trivy")}
    assert set(found) == {"CVE-2018-18074", "CVE-2024-35195", "github-pat"}
    cve = found["CVE-2018-18074"]
    assert (cve.path, cve.line, cve.severity) == ("requirements.txt", 1, "high")
    assert cve.message.startswith("Package: requests Installed Version: 2.19.0")
    assert found["CVE-2024-35195"].severity == "medium"
    secret = found["github-pat"]
    assert secret.path == "src/settings.py" and secret.severity in ("high", "critical")
    assert "ghp_" not in secret.message


def test_osv_absolute_uris_and_duplicates() -> None:
    found = parse_sarif(load("osv.sarif"), "osv")
    assert sorted(f.rule_id for f in found) == ["CVE-2018-18074", "CVE-2024-35195"]  # 4 -> 2
    by_id = {f.rule_id: f for f in found}
    assert by_id["CVE-2018-18074"].path == "requirements.txt"
    assert by_id["CVE-2018-18074"].severity == "high"  # security-severity 7.5
    assert by_id["CVE-2024-35195"].severity == "medium"  # 5.6
    assert by_id["CVE-2018-18074"].line is None


def sarif(uri: str, **result: Any) -> dict[str, Any]:
    res = {
        "ruleId": "R1",
        "message": {"text": "m"},
        "locations": [
            {"physicalLocation": {"artifactLocation": {"uri": uri}, "region": {"startLine": 3}}}
        ],
    }
    res.update(result)
    return {"runs": [{"tool": {"driver": {"name": "x", "rules": []}}, "results": [res]}]}


def test_paths_outside_repo_are_dropped_and_backslashes_normalised() -> None:
    assert parse_sarif(sarif("file:///etc/passwd"), "t") == []
    assert parse_sarif(sarif("../escape.txt"), "t") == []
    (f,) = parse_sarif(sarif("src\\app\\x.py"), "t")
    assert f.path == "src/app/x.py" and f.line == 3
    (g,) = parse_sarif(sarif("file:///workspace/a/b.py"), "t")
    assert g.path == "a/b.py"


def test_level_fallback_and_secret_masking() -> None:
    token = "ghp_" + "A1b2C3d4" * 5
    (f,) = parse_sarif(sarif("a.py", level="note", message={"text": f"leaked {token}\n x"}), "t")
    assert f.severity == "low"
    assert token not in f.message and f.message == "leaked ghp_*** x"
    (g,) = parse_sarif(sarif("a.py", level="bogus"), "t")
    assert g.severity == "medium"


def test_malformed_input_is_skipped() -> None:
    for bad in (None, [], {"runs": "x"}, {"runs": [1, {"results": [1, {"ruleId": 3}]}]}):
        assert parse_sarif(bad, "t") == []
    no_loc = sarif("a.py")
    no_loc["runs"][0]["results"][0]["locations"] = []
    assert parse_sarif(no_loc, "t") == []


def test_fingerprint_ignores_line() -> None:
    a = Finding("trivy", "R", "high", "a.py", 1, "m")
    b = Finding("trivy", "R", "high", "a.py", 40, "m")
    c = Finding("trivy", "R", "high", "b.py", 1, "m")
    assert a.fingerprint == b.fingerprint != c.fingerprint
    assert len(a.fingerprint) == 16


def test_severity_at_least() -> None:
    assert severity_at_least("high", "medium") and severity_at_least("low", "low")
    assert not severity_at_least("low", "high")
