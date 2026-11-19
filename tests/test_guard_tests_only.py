from pathlib import Path

import pytest

from grafter.charter import ChangeType, Charter, load_charter
from grafter.config import RepoConfig
from grafter.engines.base import RunResult, Task
from grafter.guard import Tier, check

ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / "examples/acme-harvest/grafter.charter.yaml"


@pytest.fixture
def charter() -> Charter:
    return load_charter(EXAMPLE)


@pytest.fixture
def api() -> RepoConfig:
    return RepoConfig(
        name="api",
        github="acme/api",
        language="dotnet",
        test_command="dotnet test",
        test_paths=["tests/**", "**/*.test.ts", "test/**"],
    )


def task(t: ChangeType = ChangeType.TEST, repo: str = "api") -> Task:
    return Task(id="1", repo=repo, change_type=t, charter_ref="domain: grading", instruction="x")


def diff(added: list[str] | None = None, removed: list[str] | None = None) -> str:
    added, removed = added or [], removed or []
    lines = ["diff --git a/t b/t", "--- a/tests/t", "+++ b/tests/t", "@@ -1,1 +1,2 @@", " ctx"]
    return "\n".join([*lines, *("-" + r for r in removed), *("+" + a for a in added)])


def res(*files: str, d: str = "", deleted: list[str] | None = None) -> RunResult:
    return RunResult(success=True, changed_files=list(files), deleted_files=deleted or [], diff=d)


def test_clean_test_change_is_tier1(charter, api):
    d = diff(added=["def test_new():", "    assert grade(1) == 'A'"], removed=["    x = 1"])
    v = check(task(), res("tests/test_grading.py", "src/grading.test.ts", d=d), charter, repo=api)
    assert v.allowed and v.tier == Tier.APPROVE, v.reasons


def test_test_change_needs_repo_config(charter):
    v = check(task(), res("tests/a.py"), charter)
    assert not v.allowed
    assert "test change needs repo config" in v.reasons


def test_repo_config_must_match_task_repo(charter, api):
    v = check(task(repo="web"), res("tests/a.py"), charter, repo=api)
    assert not v.allowed
    assert any("repo config mismatch" in r for r in v.reasons)


def test_non_test_change_ignores_test_rules(charter, api):
    d = diff(removed=["def test_old():"])
    v = check(task(ChangeType.BUGFIX), res("src/a.cs", d=d), charter)
    assert v.allowed
    v = check(task(ChangeType.BUGFIX), res("src/a.cs", d=d), charter, repo=api)
    assert v.allowed


def test_rejects_non_test_files(charter, api):
    v = check(task(), res("tests/a.py", "src/a.cs", "src/b.ts"), charter, repo=api)
    assert not v.allowed
    assert "non-test files in test change: ['src/a.cs', 'src/b.ts']" in v.reasons


def test_unsafe_path_rejected_even_though_matcher_fails_closed(charter, api):
    v = check(task(), res("../tests/a.py"), charter, repo=api)
    assert not v.allowed


def test_rejects_deleted_files(charter, api):
    v = check(task(), res("tests/a.py", deleted=["tests/old.py"]), charter, repo=api)
    assert not v.allowed
    assert any("test changes may not delete files" in r for r in v.reasons)


@pytest.mark.parametrize(
    "line",
    [
        "def test_grading():",
        "    async def test_async_grading(client):",
        "    [Fact]",
        "    [Theory]",
        "[Test]",
        "  [TestMethod]",
        "  it('grades lots', () => {",
        'test("x", async () => {',
        "describe('Grading', () => {",
        "  testWidgets('renders', (tester) async {",
        "  group('grading', () {",
    ],
)
def test_rejects_removed_test_declarations(charter, api, line):
    v = check(task(), res("tests/a", d=diff(removed=[line])), charter, repo=api)
    assert not v.allowed
    assert "removes test declarations" in v.reasons


@pytest.mark.parametrize(
    "line",
    [
        "    submit(form)",
        "    m.group(1)",
        "    contest(1)",
        "    [TestCase(1)]",
        "    def helper_test_x():",
        "    # it was removed",
    ],
)
def test_removed_non_declarations_allowed(charter, api, line):
    v = check(task(), res("tests/a", d=diff(removed=[line])), charter, repo=api)
    assert v.allowed, v.reasons


def test_added_declarations_and_file_headers_ignored(charter, api):
    d = "--- a/def test_x():\n+++ b/tests/a\n+def test_new():\n+    [Fact]"
    assert check(task(), res("tests/a", d=d), charter, repo=api).allowed


@pytest.mark.parametrize(
    "line",
    [
        "@pytest.mark.skip(reason='flaky')",
        "@pytest.mark.skipif(sys.platform == 'win32', reason='x')",
        "    pytest.skip('later')",
        "it.skip('grades', () => {",
        "test.skip('grades', () => {",
        "xit('grades', () => {",
        "describe.skip('Grading', () => {",
        '    [Fact(Skip = "flaky")]',
        "  test('x', () {}, skip: true);",
    ],
)
def test_rejects_added_skips(charter, api, line):
    v = check(task(), res("tests/a", d=diff(added=[line])), charter, repo=api)
    assert not v.allowed
    assert "skips tests" in v.reasons


def test_removed_skip_is_fine(charter, api):
    d = diff(removed=["@pytest.mark.skip(reason='flaky')"])
    assert check(task(), res("tests/a", d=d), charter, repo=api).allowed


def test_collects_all_reasons(charter, api):
    d = diff(added=["xit('x', () => {})"], removed=["def test_a():"])
    v = check(task(), res("src/a.cs", d=d, deleted=["tests/b.py"]), charter, repo=api)
    assert not v.allowed
    assert len(v.reasons) == 4
