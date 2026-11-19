from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from grafter.charter import Charter, RepoRules, load_charter, matches_any
from grafter.cli import app
from grafter.config import Config, RepoConfig, load_config

ROOT = Path(__file__).parent.parent
EXAMPLE_CHARTER = ROOT / "examples/acme-harvest/grafter.charter.yaml"
EXAMPLE_CONFIG = ROOT / "grafter.example.yaml"


def repo(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "name": "api",
        "github": "acme/api",
        "language": "dotnet",
        "test_command": "dotnet test",
        "test_paths": ["tests/**"],
    }
    return {**base, **kw}


def make(**kw: object) -> Charter:
    base: dict[str, object] = {
        "solution": "x",
        "purpose": "y",
        "core_domains": ["a"],
        "allowed_change_types": ["bugfix"],
    }
    return Charter.model_validate({**base, **kw})


# --- RepoConfig ---------------------------------------------------------------


def test_repo_config_defaults():
    r = RepoConfig.model_validate(repo())
    assert r.default_branch == "main"
    assert r.coverage_command == ""
    assert r.sandbox_image == ""
    assert r.test_paths == ["tests/**"]


@pytest.mark.parametrize("name", ["API", "-api", "", "a b", "api/x", "_api"])
def test_repo_name_rejected(name):
    with pytest.raises(ValidationError):
        RepoConfig.model_validate(repo(name=name))


@pytest.mark.parametrize("name", ["api", "web-2", "mobile_app", "0"])
def test_repo_name_accepted(name):
    assert RepoConfig.model_validate(repo(name=name)).name == name


@pytest.mark.parametrize("github", ["acme", "acme/", "/api", "acme/api/x", "acme api/x", ""])
def test_repo_github_rejected(github):
    with pytest.raises(ValidationError):
        RepoConfig.model_validate(repo(github=github))


def test_repo_github_accepted():
    assert RepoConfig.model_validate(repo(github="Acme-Org/my.api_2")).github == "Acme-Org/my.api_2"


def test_repo_requires_test_paths():
    with pytest.raises(ValidationError):
        RepoConfig.model_validate(repo(test_paths=[]))
    data = repo()
    del data["test_paths"]
    with pytest.raises(ValidationError):
        RepoConfig.model_validate(data)


def test_repo_test_paths_normalised_and_unsafe_rejected():
    r = RepoConfig.model_validate(repo(test_paths=["./tests\\unit/", "tests/unit/"]))
    assert r.test_paths == ["tests/unit/"]
    with pytest.raises(ValidationError):
        RepoConfig.model_validate(repo(test_paths=["../tests/**"]))


def test_repo_extra_key_rejected():
    with pytest.raises(ValidationError):
        RepoConfig.model_validate(repo(owner="x"))


# --- Config.repos ---------------------------------------------------------------


def test_config_repos_required():
    with pytest.raises(ValidationError):
        Config.model_validate({"repos": []})
    with pytest.raises(ValidationError):
        Config.model_validate({})


def test_config_rejects_plain_string_repos():
    with pytest.raises(ValidationError):
        Config.model_validate({"repos": ["acme/api"]})


def test_config_rejects_duplicate_names():
    with pytest.raises(ValidationError, match="duplicate repo names"):
        Config.model_validate({"repos": [repo(), repo(github="acme/api2")]})


def test_config_repo_lookup():
    c = Config.model_validate({"repos": [repo(), repo(name="web", github="acme/web")]})
    assert c.repo("web").github == "acme/web"
    with pytest.raises(KeyError, match=r"unknown repo 'nope'.*api, web"):
        c.repo("nope")


# --- matches_any -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "patterns", "expected"),
    [
        ("tests/unit/a.cs", ["tests/**"], True),
        ("src/a.test.ts", ["**/*.test.ts"], True),
        ("a.test.ts", ["**/*.test.ts"], True),
        ("src/a.ts", ["**/*.test.ts", "tests/**"], False),
        ("TESTS\\x.py", ["tests/"], True),
        ("x.py", [], False),
        ("../escape.py", ["tests/**"], True),  # unsafe paths fail closed
    ],
)
def test_matches_any(path, patterns, expected):
    assert matches_any(path, patterns) is expected


# --- repo-scoped charter rules --------------------------------------------------


def test_repo_rules_scope_protection():
    c = make(protected_paths=["auth/"], repos={"mobile": {"protected_paths": ["ios/"]}})
    assert c.is_protected("ios/Runner/Info.plist", "mobile")
    assert not c.is_protected("ios/Runner/Info.plist", "api")
    assert not c.is_protected("ios/Runner/Info.plist")
    assert c.is_protected("auth/x.cs", "mobile")  # global still applies


def test_repo_rules_scope_review():
    c = make(repos={"api": {"review_paths": ["db/migrations/"]}})
    assert c.needs_review("db/migrations/1.sql", "api")
    assert not c.needs_review("db/migrations/1.sql", "web")
    assert not c.needs_review("db/migrations/1.sql")


def test_unknown_repo_falls_back_to_global():
    c = make(review_paths=["docs/"], repos={"api": {"review_paths": ["db/"]}})
    assert c.needs_review("docs/a.md", "nope")
    assert not c.needs_review("db/a.sql", "nope")
    assert c.is_protected("grafter.charter.yaml", "nope")


def test_repo_rules_normalised():
    c = make(repos={"api": {"protected_paths": ["./secrets\\", "secrets/"]}})
    assert c.repos["api"].protected_paths == ["secrets/"]
    assert c.is_protected("SECRETS/a", "api")


def test_repo_rules_reject_unsafe_patterns():
    with pytest.raises(ValidationError):
        make(repos={"api": {"protected_paths": ["../x"]}})
    with pytest.raises(ValidationError):
        make(repos={"api": {"review_paths": ["C:/x"]}})


def test_repo_review_overlaps_repo_protected():
    with pytest.raises(ValidationError, match="api"):
        make(repos={"api": {"protected_paths": ["db/"], "review_paths": ["db/"]}})


def test_repo_review_overlaps_global_protected():
    with pytest.raises(ValidationError, match="both protected and review"):
        make(protected_paths=["db/"], repos={"api": {"review_paths": ["db/"]}})
    with pytest.raises(ValidationError, match="both protected and review"):
        make(repos={"api": {"review_paths": [".github/"]}})  # always protected


def test_repo_protected_overlaps_global_review():
    with pytest.raises(ValidationError, match="both protected and review"):
        make(review_paths=["db/"], repos={"api": {"protected_paths": ["db/"]}})


def test_repo_rules_extra_key_and_bad_name():
    with pytest.raises(ValidationError):
        make(repos={"api": {"paths": ["x"]}})
    with pytest.raises(ValidationError):
        make(repos={"API": {}})


def test_repo_rules_default_empty():
    assert make().repos == {}
    assert RepoRules() == RepoRules(protected_paths=[], review_paths=[])


# --- examples ---------------------------------------------------------------------


def test_example_names_agree():
    charter = load_charter(EXAMPLE_CHARTER)
    cfg = load_config(EXAMPLE_CONFIG)
    names = {r.name for r in cfg.repos}
    assert {"api", "web", "mobile"} <= names
    assert set(charter.repos) <= names


# --- CLI ------------------------------------------------------------------------------

runner = CliRunner()


def test_cli_validate_without_config():
    res = runner.invoke(app, ["validate", str(EXAMPLE_CHARTER)])
    assert res.exit_code == 0, res.output
    assert "ok: AcmeHarvest" in res.output
    assert "repos:" not in res.output


def test_cli_validate_with_config():
    res = runner.invoke(app, ["validate", str(EXAMPLE_CHARTER), "--config", str(EXAMPLE_CONFIG)])
    assert res.exit_code == 0, res.output
    assert "repos: 3" in res.output


def test_cli_validate_invalid_charter(tmp_path):
    bad = tmp_path / "c.yaml"
    bad.write_text("solution: x\n", encoding="utf-8")
    res = runner.invoke(app, ["validate", str(bad)])
    assert res.exit_code == 1
    assert "invalid charter" in res.output


def test_cli_validate_invalid_config(tmp_path):
    bad = tmp_path / "g.yaml"
    bad.write_text("repos: [acme/api]\n", encoding="utf-8")
    res = runner.invoke(app, ["validate", str(EXAMPLE_CHARTER), "--config", str(bad)])
    assert res.exit_code == 1
    assert "invalid config" in res.output
    res = runner.invoke(app, ["validate", str(EXAMPLE_CHARTER), "--config", str(tmp_path / "x")])
    assert res.exit_code == 1


def test_cli_validate_repo_mismatch(tmp_path):
    cfg = tmp_path / "g.yaml"
    cfg.write_text(yaml.safe_dump({"repos": [repo()]}), encoding="utf-8")
    res = runner.invoke(app, ["validate", str(EXAMPLE_CHARTER), "--config", str(cfg)])
    assert res.exit_code == 1
    assert "mobile" in res.output
