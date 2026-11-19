import os
from pathlib import Path

import pytest

from grafter.credentials import SecretError, load_secret, require_secrets

PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEabc\ndef==\n-----END RSA PRIVATE KEY-----\n"
VALUE = "s3cr3t-value-xyz"


@pytest.fixture
def secrets_dir(tmp_path: Path) -> Path:
    d = tmp_path / "secrets"
    d.mkdir()
    return d


def put(path: Path, content: str, mode: int = 0o600) -> Path:
    path.write_bytes(content.encode())
    os.chmod(path, mode)
    return path


# --- env lookup -------------------------------------------------------------


def test_grafter_prefixed_env_wins() -> None:
    env = {"GRAFTER_LITELLM_MASTER_KEY": "a", "LITELLM_MASTER_KEY": "b"}
    assert load_secret("LITELLM_MASTER_KEY", environ=env) == "a"


def test_plain_env_fallback() -> None:
    assert load_secret("LITELLM_MASTER_KEY", environ={"LITELLM_MASTER_KEY": "b\n"}) == "b"


def test_env_beats_file(secrets_dir: Path) -> None:
    f = put(secrets_dir / "k", "from-file")
    env = {"GRAFTER_X": "from-env", "GRAFTER_X_FILE": str(f)}
    assert load_secret("X", environ=env) == "from-env"


def test_empty_env_falls_through_to_file(secrets_dir: Path) -> None:
    f = put(secrets_dir / "k", "from-file\n")
    env = {"GRAFTER_X": "", "X": "  ", "GRAFTER_X_FILE": str(f)}
    assert load_secret("X", environ=env) == "from-file"


def test_default_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRAFTER_ZZ_TEST_SECRET", VALUE)
    assert load_secret("ZZ_TEST_SECRET") == VALUE


@pytest.mark.parametrize("name", ["", "lower", "1ABC", "A-B", "A B"])
def test_bad_name(name: str) -> None:
    with pytest.raises(SecretError, match="invalid secret name"):
        load_secret(name, environ={})


def test_missing() -> None:
    with pytest.raises(SecretError) as err:
        load_secret("GITHUB_APP_PRIVATE_KEY", environ={})
    msg = str(err.value)
    assert "GRAFTER_GITHUB_APP_PRIVATE_KEY" in msg
    assert "GRAFTER_GITHUB_APP_PRIVATE_KEY_FILE" in msg


# --- file lookup ------------------------------------------------------------


def test_file_single_line_stripped(secrets_dir: Path) -> None:
    f = put(secrets_dir / "k", VALUE + "\r\n")
    assert load_secret("X", environ={"GRAFTER_X_FILE": str(f)}) == VALUE


def test_pem_kept_intact(secrets_dir: Path) -> None:
    f = put(secrets_dir / "app.pem", PEM)
    assert (
        load_secret(
            "GITHUB_APP_PRIVATE_KEY", environ={"GRAFTER_GITHUB_APP_PRIVATE_KEY_FILE": str(f)}
        )
        == PEM
    )


def test_file_path_expanded(secrets_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    put(secrets_dir / "k", VALUE)
    monkeypatch.setenv("ZZ_SECRETS_DIR", str(secrets_dir))
    assert load_secret("X", environ={"GRAFTER_X_FILE": "$ZZ_SECRETS_DIR/k"}) == VALUE


def test_file_missing(secrets_dir: Path) -> None:
    with pytest.raises(SecretError, match="does not exist"):
        load_secret("X", environ={"GRAFTER_X_FILE": str(secrets_dir / "nope")})


def test_file_is_directory(secrets_dir: Path) -> None:
    with pytest.raises(SecretError, match="not a regular file"):
        load_secret("X", environ={"GRAFTER_X_FILE": str(secrets_dir)})


@pytest.mark.parametrize("content", ["", "\n", "  \r\n"])
def test_file_empty(secrets_dir: Path, content: str) -> None:
    f = put(secrets_dir / "k", content)
    with pytest.raises(SecretError, match="empty"):
        load_secret("X", environ={"GRAFTER_X_FILE": str(f)})


def test_file_unreadable(secrets_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = put(secrets_dir / "k", VALUE)

    def boom(self: Path, *a: object, **k: object) -> str:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_text", boom)
    with pytest.raises(SecretError, match="cannot read") as err:
        load_secret("X", environ={"GRAFTER_X_FILE": str(f)})
    assert VALUE not in str(err.value)


@pytest.mark.parametrize("marker", ["dir", "file"])
def test_file_inside_git_work_tree(tmp_path: Path, marker: str) -> None:
    repo = tmp_path / "repo"
    (repo / "config" / "keys").mkdir(parents=True)
    if marker == "dir":
        (repo / ".git").mkdir()
    else:  # worktrees and submodules use a `.git` file
        (repo / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    f = put(repo / "config" / "keys" / "app.pem", PEM)
    with pytest.raises(SecretError, match="git work tree") as err:
        load_secret(
            "GITHUB_APP_PRIVATE_KEY", environ={"GRAFTER_GITHUB_APP_PRIVATE_KEY_FILE": str(f)}
        )
    assert "MIIE" not in str(err.value)


def test_file_inside_forbidden_dir(tmp_path: Path) -> None:
    work = tmp_path / "work"
    (work / "sandbox").mkdir(parents=True)
    f = put(work / "sandbox" / "k", VALUE)
    with pytest.raises(SecretError, match="forbidden") as err:
        load_secret(
            "X",
            environ={"GRAFTER_X_FILE": str(f)},
            forbidden_dirs=[tmp_path / "other", tmp_path / "work" / ".." / "work"],
        )
    assert VALUE not in str(err.value)


def test_file_outside_forbidden_dir_ok(tmp_path: Path, secrets_dir: Path) -> None:
    f = put(secrets_dir / "k", VALUE)
    env = {"GRAFTER_X_FILE": str(f)}
    assert load_secret("X", environ=env, forbidden_dirs=[tmp_path / "work"]) == VALUE


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660])
def test_file_mode_too_open(secrets_dir: Path, mode: int) -> None:
    f = put(secrets_dir / "k", VALUE, mode)
    with pytest.raises(SecretError, match="chmod 600") as err:
        load_secret("X", environ={"GRAFTER_X_FILE": str(f)})
    assert VALUE not in str(err.value)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_file_mode_400_ok(secrets_dir: Path) -> None:
    f = put(secrets_dir / "k", VALUE, 0o400)
    assert load_secret("X", environ={"GRAFTER_X_FILE": str(f)}) == VALUE


# --- require_secrets --------------------------------------------------------


def test_require_secrets_all_present(secrets_dir: Path) -> None:
    f = put(secrets_dir / "app.pem", PEM)
    env = {"LITELLM_MASTER_KEY": "sk-master", "GRAFTER_GITHUB_APP_PRIVATE_KEY_FILE": str(f)}
    got = require_secrets(["LITELLM_MASTER_KEY", "GITHUB_APP_PRIVATE_KEY"], environ=env)
    assert got == {"LITELLM_MASTER_KEY": "sk-master", "GITHUB_APP_PRIVATE_KEY": PEM}


def test_require_secrets_collects_all_errors(tmp_path: Path) -> None:
    env = {"GRAFTER_B": "present-value", "GRAFTER_C_FILE": str(tmp_path / "missing")}
    with pytest.raises(SecretError) as err:
        require_secrets(["A", "B", "C"], environ=env, forbidden_dirs=[tmp_path / "w"])
    msg = str(err.value)
    assert "GRAFTER_A" in msg and "missing" in msg
    assert "present-value" not in msg
    assert msg.count("\n") >= 2
