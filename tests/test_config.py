from datetime import date, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from grafter.charter import ChangeType
from grafter.config import Config, GitHubApp, KillSwitch, Models, load_config

ROOT = Path(__file__).parent.parent
REPOS = {
    "repos": [
        {
            "name": "api",
            "github": "org/api",
            "language": "python",
            "test_command": "pytest",
            "test_paths": ["tests/**"],
        }
    ]
}


def test_defaults() -> None:
    c = Config.model_validate(REPOS)
    assert c.dry_run is False
    assert c.nightly_focus == [ChangeType.TEST]
    assert c.models == Models(
        planner="claude-opus-5-5",
        coder="claude-sonnet-5-5",
        reviewer="claude-opus-5-5",
        judge="claude-opus-5-5",
    )
    assert c.limits.usd_budget_per_night == 5.0
    assert c.github_app == GitHubApp(app_id=None, installation_id=None)
    assert c.kill_switch == KillSwitch(file="~/.grafter/STOP", env="GRAFTER_STOP")
    assert c.tier0_auto_merge is False


@pytest.mark.parametrize("value", ["1", "true", "YES", "On"])
def test_kill_switch_env_truthy(value: str) -> None:
    ks = KillSwitch(file="/nonexistent/grafter/STOP")
    assert ks.is_engaged({"GRAFTER_STOP": value}) is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_kill_switch_env_falsy(value: str) -> None:
    ks = KillSwitch(file="/nonexistent/grafter/STOP")
    assert ks.is_engaged({"GRAFTER_STOP": value}) is False
    assert ks.is_engaged({}) is False


def test_kill_switch_uses_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    ks = KillSwitch(file="/nonexistent/grafter/STOP", env="MY_STOP")
    monkeypatch.delenv("MY_STOP", raising=False)
    assert ks.is_engaged() is False
    monkeypatch.setenv("MY_STOP", "true")
    assert ks.is_engaged() is True


def test_kill_switch_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    ks = KillSwitch()  # ~/.grafter/STOP
    assert ks.is_engaged({}) is False
    stop = tmp_path / ".grafter" / "STOP"
    stop.parent.mkdir()
    stop.touch()
    assert ks.is_engaged({}) is True


def test_focus_rotation() -> None:
    focus = [ChangeType.TEST, ChangeType.SECURITY, ChangeType.PERFORMANCE]
    c = Config.model_validate({**REPOS, "nightly_focus": focus})
    start = date(2026, 11, 20)
    got = [c.focus_for(start + timedelta(days=i)) for i in range(6)]
    first = focus.index(got[0])
    assert got == [focus[(first + i) % 3] for i in range(6)]
    assert set(got) == set(focus)


def test_focus_single_default() -> None:
    c = Config.model_validate(REPOS)
    assert c.focus_for(date(2026, 1, 1)) is ChangeType.TEST


def test_focus_rejects_empty_and_unknown() -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({**REPOS, "nightly_focus": []})
    with pytest.raises(ValidationError):
        Config.model_validate({**REPOS, "nightly_focus": ["feature"]})


@pytest.mark.parametrize(
    "data",
    [
        {"github_app": {"app_id": 1, "private_key": "x"}},
        {"github_app": {"private_key_path": "/k.pem"}},
        {"webhook_secret": "x"},
        {"password": "x"},
        {"github_token": "x"},
        {"llm_api_key": "x"},
        {"limits": {"nested": [{"API_KEY": "x"}]}},
    ],
)
def test_secret_keys_rejected(data: dict) -> None:
    with pytest.raises(ValidationError, match="environment variable"):
        Config.model_validate({**REPOS, **data})


def test_token_budget_allowed() -> None:
    c = Config.model_validate({**REPOS, "limits": {"token_budget_per_night": 1000}})
    assert c.limits.token_budget_per_night == 1000


@pytest.mark.parametrize(
    "data",
    [{"unknown": 1}, {"github_app": {"app_slug": "x"}}, {"kill_switch": {"path": "x"}}],
)
def test_extra_keys_rejected(data: dict) -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({**REPOS, **data})


def test_github_app_ids() -> None:
    c = Config.model_validate({**REPOS, "github_app": {"app_id": 12, "installation_id": 34}})
    assert (c.github_app.app_id, c.github_app.installation_id) == (12, 34)


def test_example_yaml_loads() -> None:
    c = load_config(ROOT / "grafter.example.yaml")
    assert [r.name for r in c.repos] == ["api", "web", "mobile"]
    assert c.repo("web").test_paths == ["**/*.test.ts", "**/*.test.tsx", "**/__tests__/**"]
    assert c.dry_run is False
    assert c.nightly_focus


def test_models_configurable_per_role() -> None:
    c = Config.model_validate({**REPOS, "models": {"coder": "claude-opus-5-5", "judge": "x"}})
    assert c.models.coder == "claude-opus-5-5"
    assert c.models.judge == "x"
    assert c.models.planner == "claude-opus-5-5"


@pytest.mark.parametrize("bad", [{"coder": ""}, {"writer": "claude-opus-5-5"}])
def test_models_rejects_empty_or_unknown_role(bad: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({**REPOS, "models": bad})


@pytest.mark.parametrize("value", [0, -1])
def test_usd_budget_must_be_positive(value: float) -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({**REPOS, "limits": {"usd_budget_per_night": value}})


def test_old_single_model_field_rejected() -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({**REPOS, "model": "claude-opus-5-5"})


# --- run settings (1D3a) ---


def test_run_settings_defaults() -> None:
    c = Config.model_validate(REPOS)
    assert c.charter_repo == ""
    assert c.charter_file == "grafter.charter.yaml"
    assert c.runner == "docker"
    assert c.git_author_name == "Grafter"
    assert c.git_author_email == "grafter-bot@users.noreply.github.com"
    assert c.charter_repo_config().name == "api"
    assert c.github_app.api_url == "https://api.github.com"
    assert c.repos[0].clone_url == ""


def test_charter_repo_must_be_configured() -> None:
    with pytest.raises(ValidationError, match="charter_repo"):
        Config.model_validate({**REPOS, "charter_repo": "web"})
    two = {"repos": [*REPOS["repos"], {**REPOS["repos"][0], "name": "web", "github": "org/web"}]}
    c = Config.model_validate({**two, "charter_repo": "web"})
    assert c.charter_repo_config().name == "web"


def test_runner_choices() -> None:
    assert Config.model_validate({**REPOS, "runner": "local"}).runner == "local"
    with pytest.raises(ValidationError):
        Config.model_validate({**REPOS, "runner": "ssh"})


@pytest.mark.parametrize("bad", ["", "../charter.yaml", "/etc/charter.yaml"])
def test_charter_file_must_be_repo_relative(bad: str) -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({**REPOS, "charter_file": bad})


def test_charter_file_normalised() -> None:
    c = Config.model_validate({**REPOS, "charter_file": r"./docs\grafter.charter.yaml"})
    assert c.charter_file == "docs/grafter.charter.yaml"


@pytest.mark.parametrize(
    "url", ["https://user:pw@github.com/o/r.git", "https://x-access-token:abc@github.com/o/r"]
)
def test_clone_url_rejects_credentials(url: str) -> None:
    repo = {**REPOS["repos"][0], "clone_url": url}
    with pytest.raises(ValidationError, match="credentials"):
        Config.model_validate({"repos": [repo]})


def test_clone_url_accepts_path_or_plain_url() -> None:
    for url in ("/srv/git/api.git", "C:/git/api.git", "https://github.com/o/r.git"):
        repo = {**REPOS["repos"][0], "clone_url": url}
        assert Config.model_validate({"repos": [repo]}).repos[0].clone_url == url


def test_sandbox_config_runner_is_docker() -> None:
    c = load_config(ROOT / "deploy/grafter.sandbox.yaml")
    assert c.runner == "docker"
    assert c.charter_repo_config().name == "api"
