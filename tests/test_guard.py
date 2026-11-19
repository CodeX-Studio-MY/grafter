from pathlib import Path

import pytest
from pydantic import ValidationError

from grafter.charter import ChangeType, Charter, load_charter
from grafter.config import Limits
from grafter.engines.base import RunResult, Task
from grafter.guard import Tier, check
from grafter.secret_scan import scan_diff

ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / "examples/acme-harvest/grafter.charter.yaml"


@pytest.fixture
def charter() -> Charter:
    return load_charter(EXAMPLE)


def task(
    t: ChangeType = ChangeType.BUGFIX,
    ref: str = "invariant: traceability",
    bump: str | None = None,
) -> Task:
    return Task(
        id="1", repo="api", change_type=t, charter_ref=ref, instruction="x", version_bump=bump
    )


def result(*files: str) -> RunResult:
    return RunResult(success=True, changed_files=list(files))


def test_guard_rejects_protected_path(charter):
    v = check(task(), result("auth/x.cs"), charter)
    assert not v.allowed


def test_guard_rejects_missing_citation(charter):
    v = check(task(ref=" "), result("src/a.cs"), charter)
    assert not v.allowed


def test_guard_review_paths_are_tier2(charter):
    v = check(task(), result("db/migrations/2.sql"), charter)
    assert v.allowed and v.tier == Tier.REVIEW


def test_guard_uses_task_repo_rules(charter):
    web = Task(id="1", repo="web", change_type=ChangeType.BUGFIX, charter_ref="x", instruction="x")
    mobile = Task(
        id="1", repo="mobile", change_type=ChangeType.BUGFIX, charter_ref="x", instruction="x"
    )
    assert check(web, result("db/migrations/2.sql"), charter).tier == Tier.APPROVE
    assert check(web, result("android/app/build.gradle"), charter).allowed
    assert not check(mobile, result("android/app/build.gradle"), charter).allowed


def test_patch_dependency_bump_is_tier0(charter):
    v = check(task(ChangeType.DEPENDENCY, bump="patch"), result("a.csproj"), charter)
    assert v.allowed and v.tier == Tier.AUTO


def test_minor_dependency_bump_is_tier1(charter):
    v = check(task(ChangeType.DEPENDENCY, bump="minor"), result("a.csproj"), charter)
    assert v.allowed and v.tier == Tier.APPROVE


def test_dependency_bump_without_version_is_tier1(charter):
    v = check(task(ChangeType.DEPENDENCY), result("a.csproj"), charter)
    assert v.allowed and v.tier == Tier.APPROVE


def test_patch_bump_touching_many_files_is_tier1(charter):
    v = check(task(ChangeType.DEPENDENCY, bump="patch"), result("a", "b", "c"), charter)
    assert v.allowed and v.tier == Tier.APPROVE


def test_patch_bump_in_review_path_is_tier2(charter):
    v = check(task(ChangeType.DEPENDENCY, bump="patch"), result("db/migrations/x"), charter)
    assert v.allowed and v.tier == Tier.REVIEW


def test_small_security_change_is_never_tier0(charter):
    v = check(task(ChangeType.SECURITY, bump="patch"), result("a.csproj"), charter)
    assert v.allowed and v.tier == Tier.APPROVE


def test_default_limits():
    lim = Limits()
    assert lim.max_files_per_change == 20
    assert lim.max_lines_per_change == 400


def test_limits_must_be_positive():
    with pytest.raises(ValidationError):
        Limits(max_files_per_change=0)
    with pytest.raises(ValidationError):
        Limits(max_lines_per_change=0)


def test_rejects_too_many_files(charter):
    r = RunResult(success=True, changed_files=["a", "b"], deleted_files=["c"])
    v = check(task(), r, charter, Limits(max_files_per_change=2))
    assert not v.allowed
    assert any("too many files: 3 > 2" in x for x in v.reasons)


def test_file_limit_is_inclusive(charter):
    r = RunResult(success=True, changed_files=["a"], deleted_files=["c"])
    v = check(task(), r, charter, Limits(max_files_per_change=2))
    assert v.allowed


def test_rejects_too_many_lines(charter):
    r = RunResult(success=True, changed_files=["a"], lines_added=300, lines_removed=101)
    v = check(task(), r, charter)
    assert not v.allowed
    assert any("diff too large: 401 > 400 lines" in x for x in v.reasons)


def test_line_limit_is_inclusive(charter):
    r = RunResult(success=True, changed_files=["a"], lines_added=300, lines_removed=100)
    assert check(task(), r, charter).allowed


def test_default_limits_used_when_none(charter):
    r = RunResult(success=True, changed_files=[f"f{i}" for i in range(21)])
    assert not check(task(), r, charter).allowed


def test_rejects_deleting_protected_file(charter):
    r = RunResult(success=True, changed_files=["src/a.cs"], deleted_files=["auth/login.cs"])
    v = check(task(), r, charter)
    assert not v.allowed
    assert any("auth/login.cs" in x for x in v.reasons)


def test_deleting_review_file_is_tier2(charter):
    r = RunResult(success=True, deleted_files=["db/migrations/1.sql"])
    v = check(task(), r, charter)
    assert v.allowed and v.tier == Tier.REVIEW


def test_deleted_files_count_against_tier0(charter):
    r = RunResult(success=True, changed_files=["a.csproj"], deleted_files=["b", "c"])
    v = check(task(ChangeType.DEPENDENCY, bump="patch"), r, charter)
    assert v.allowed and v.tier == Tier.APPROVE


# Fake secrets are assembled at runtime so no key-shaped literal is committed.
FAKE = {
    "aws": "AK" + "IA" + "Q7" * 8,
    "github": "gh" + "p_" + "a1B2" * 9,
    "github_pat": "github" + "_pat_" + "x9Y8_" * 5,
    "private_key": "-----BEGIN RSA " + "PRIVATE KEY-----",
    "slack": "xo" + "xb-" + "1234-abcd-5678",
    "sk": "s" + "k-ant-" + "Zz9_" * 6,
    "google": "AI" + "za" + "Q" * 35,
    "generic": "api_key = " + '"' + "s3cr3tV4lu3!" + "x" * 4 + '"',
}


def diff_with(*added: str, removed: str = "") -> str:
    lines = ["diff --git a/x b/x", "--- a/x", "+++ b/x", "@@ -1,1 +1,2 @@", " ctx"]
    if removed:
        lines.append("-" + removed)
    lines += ["+" + a for a in added]
    return "\n".join(lines)


@pytest.mark.parametrize("rule", sorted(FAKE))
def test_scan_detects_each_rule(rule):
    findings = scan_diff(diff_with("x = " + FAKE[rule]))
    assert [f.rule for f in findings] == [rule]
    assert findings[0].line_no == 6


def test_generic_rule_is_case_insensitive():
    secret = "PassWord: " + "'" + "hunter2hunter2" + "'"
    assert [f.rule for f in scan_diff(diff_with(secret))] == ["generic"]


def test_scan_ignores_removed_and_context_lines():
    d = diff_with("clean = 1", removed=FAKE["aws"]).replace(" ctx", " " + FAKE["github"])
    assert scan_diff(d) == []


def test_scan_ignores_file_header():
    assert scan_diff("+++ b/" + FAKE["aws"]) == []


def test_clean_diff_passes():
    assert scan_diff(diff_with("def f():", "    return 1")) == []


def test_redaction_never_leaks_value():
    for secret in FAKE.values():
        (f,) = scan_diff(diff_with(secret))
        assert f.redacted.endswith("***")
        assert len(f.redacted) == 7
        assert secret not in f.redacted and secret not in repr(f)
        assert secret[4:12] not in repr(f)


def test_guard_rejects_secret(charter):
    secret = FAKE["aws"]
    r = RunResult(success=True, changed_files=["a.py"], diff=diff_with("k = 1", secret))
    v = check(task(), r, charter)
    assert not v.allowed
    assert "secret detected: aws (line 7)" in v.reasons
    assert all(secret not in x for x in v.reasons)


def test_guard_allows_clean_diff(charter):
    r = RunResult(success=True, changed_files=["a.py"], diff=diff_with("x = 1"))
    assert check(task(), r, charter).allowed
