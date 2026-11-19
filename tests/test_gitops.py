"""Integration tests for gitops: real git, a bare origin in tmp_path."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from grafter.gitops import DiffStats, GitError, GitRepo, UnsafePush, scrub

AUTHOR = ("Grafter Bot", "bot@example.invalid")


@pytest.fixture(autouse=True)
def isolated_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the user's global/system git config out of the tests."""
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(
        "[init]\n\tdefaultBranch = main\n[core]\n\tsymlinks = true\n", encoding="utf-8"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def put(path: Path, text: str) -> None:
    """Write exact bytes (no Windows newline translation)."""
    path.write_bytes(text.encode("utf-8"))


def git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "-c", "core.autocrlf=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """Bare origin with one commit on main."""
    bare = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", str(bare), str(seed))
    put(seed / "README.md", "hello\n")
    put(seed / "app.py", "a = 1\nb = 2\n")
    put(seed / "old.txt", "bye\n")
    git(seed, "add", "-A")
    git(
        seed,
        "-c",
        "user.name=Seed",
        "-c",
        "user.email=seed@example.invalid",
        "commit",
        "-m",
        "init",
    )
    git(seed, "push", "origin", "main")
    return bare


@pytest.fixture
def repo(origin: Path, tmp_path: Path) -> GitRepo:
    return GitRepo.clone_or_fetch(str(origin), tmp_path / "work")


def bare_refs(bare: Path) -> str:
    return git(bare, "for-each-ref", "--format=%(refname) %(objectname)")


# --- clone / fetch -------------------------------------------------------------------


def test_clone_then_fetch(origin: Path, tmp_path: Path) -> None:
    dest = tmp_path / "work"
    r = GitRepo.clone_or_fetch(str(origin), dest)
    assert (dest / "README.md").exists()
    assert r.current_branch() == "main"
    sha = r.head_sha()
    assert len(sha) == 40
    assert sha == git(origin, "rev-parse", "main")

    # A second call fetches into the existing clone.
    r2 = GitRepo.clone_or_fetch(str(origin), dest)
    assert r2.path == dest
    assert r2.head_sha() == sha


def test_refuses_non_repo_dest(origin: Path, tmp_path: Path) -> None:
    dest = tmp_path / "plain"
    dest.mkdir()
    put(dest / "x.txt", "x")
    with pytest.raises(GitError, match="not a git repository"):
        GitRepo.clone_or_fetch(str(origin), dest)
    assert sorted(p.name for p in dest.iterdir()) == ["x.txt"]


def test_refuses_file_dest(origin: Path, tmp_path: Path) -> None:
    dest = tmp_path / "afile"
    put(dest, "x")
    with pytest.raises(GitError, match="not a git repository"):
        GitRepo.clone_or_fetch(str(origin), dest)


def test_refuses_dest_nested_in_other_repo(origin: Path, repo: GitRepo) -> None:
    nested = repo.path / "sub"
    nested.mkdir()
    with pytest.raises(GitError, match="not a git repository"):
        GitRepo.clone_or_fetch(str(origin), nested)


def test_refuses_option_like_url(tmp_path: Path) -> None:
    with pytest.raises(GitError, match="unsafe"):
        GitRepo.clone_or_fetch("--upload-pack=evil", tmp_path / "w")


def test_clone_failure_raises(tmp_path: Path) -> None:
    with pytest.raises(GitError) as ei:
        GitRepo.clone_or_fetch(str(tmp_path / "missing.git"), tmp_path / "w")
    assert "git clone" in str(ei.value)


# --- fresh branch ----------------------------------------------------------------------


def test_fresh_branch_resets_dirty_tree(repo: GitRepo, origin: Path) -> None:
    p = repo.path
    put(p / "app.py", "dirty\n")
    put(p / "untracked.txt", "u\n")
    put(p / "ignored.log", "i\n")
    put(p / ".gitignore", "*.log\n")
    (p / "README.md").unlink()

    base = repo.fresh_branch("ai/test-1")

    assert base == git(origin, "rev-parse", "main")
    assert repo.current_branch() == "ai/test-1"
    assert repo.head_sha() == base
    assert (p / "app.py").read_text(encoding="utf-8") == "a = 1\nb = 2\n"
    assert (p / "README.md").exists()
    assert not (p / "untracked.txt").exists()
    assert not (p / "ignored.log").exists()
    assert repo.changed_files() == []


def test_fresh_branch_picks_up_new_origin_commits(
    repo: GitRepo, origin: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other"
    git(tmp_path, "clone", str(origin), str(other))
    put(other / "new.txt", "n\n")
    git(other, "add", "-A")
    git(other, "-c", "user.name=O", "-c", "user.email=o@example.invalid", "commit", "-m", "more")
    git(other, "push", "origin", "main")

    # Existing branch with a stray commit is reset onto the new origin/main.
    repo.fresh_branch("ai/x")
    put(repo.path / "stray.txt", "s\n")
    repo.commit("stray", *AUTHOR)
    base = repo.fresh_branch("ai/x")

    assert base == git(origin, "rev-parse", "main")
    assert (repo.path / "new.txt").exists()
    assert not (repo.path / "stray.txt").exists()


def test_fresh_branch_rejects_bad_names(repo: GitRepo) -> None:
    for name, base in [("-x", "main"), ("ai/x", "-main"), ("a b", "main"), ("ai/..", "main")]:
        with pytest.raises(GitError, match="invalid"):
            repo.fresh_branch(name, base)


def test_fresh_branch_missing_base(repo: GitRepo) -> None:
    with pytest.raises(GitError):
        repo.fresh_branch("ai/x", base="nope")


# --- diff ------------------------------------------------------------------------------


def test_changed_files_diff_and_stats(repo: GitRepo) -> None:
    p = repo.path
    repo.fresh_branch("ai/d")
    put(p / "app.py", "a = 1\nb = 3\nc = 4\n")  # +2 -1
    put(p / "new.py", "x = 1\n")  # +1
    (p / "old.txt").unlink()  # -1
    (p / "img.bin").write_bytes(b"\x00\x01\x02\xff" * 10)  # binary
    (p / "dir with space").mkdir()
    put(p / "dir with space" / "ünï.txt", "u\n")  # +1

    files = repo.changed_files()
    assert files == sorted(["app.py", "new.py", "old.txt", "img.bin", "dir with space/ünï.txt"])

    diff = repo.diff()
    assert "+b = 3" in diff
    assert "-b = 2" in diff
    assert "+x = 1" in diff
    assert "\r\n" not in diff

    stats = repo.diff_stats()
    assert isinstance(stats, DiffStats)
    assert stats.added == 4
    assert stats.removed == 2
    assert stats.changed_files == files
    assert stats.deleted_files == ["old.txt"]
    assert stats.binary_files == ["img.bin"]
    assert stats.symlinks == []


def test_symlink_detected(repo: GitRepo) -> None:
    repo.fresh_branch("ai/s")
    try:
        os.symlink("README.md", repo.path / "link")
    except (OSError, NotImplementedError) as e:  # Windows without symlink privilege
        pytest.skip(f"cannot create symlink: {e}")
    stats = repo.diff_stats()
    assert stats.symlinks == ["link"]
    assert "link" in stats.changed_files


def test_modified_symlink_detected_without_os_symlink(
    repo: GitRepo, origin: Path, tmp_path: Path
) -> None:
    # Commit a symlink purely through the index (works on any OS), then retarget it in a
    # clone with core.symlinks=false, where git checks it out as a plain file.
    other = tmp_path / "other"
    git(tmp_path, "clone", str(origin), str(other))
    blob = (
        subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=other,
            input=b"README.md",
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .strip()
    )
    git(other, "update-index", "--add", "--cacheinfo", f"120000,{blob},link")
    git(other, "-c", "user.name=O", "-c", "user.email=o@example.invalid", "commit", "-m", "ln")
    git(other, "push", "origin", "main")

    git(repo.path, "config", "core.symlinks", "false")
    repo.fresh_branch("ai/s2")
    put(repo.path / "link", "app.py")
    stats = repo.diff_stats()
    assert stats.symlinks == ["link"]
    assert stats.binary_files == []


def test_clean_tree_has_empty_stats(repo: GitRepo) -> None:
    assert repo.changed_files() == []
    assert repo.diff() == ""
    assert repo.diff_stats() == DiffStats(0, 0, [], [], [], [])


# --- commit ----------------------------------------------------------------------------


def test_commit(repo: GitRepo) -> None:
    repo.fresh_branch("ai/c")
    before = repo.head_sha()
    put(repo.path / "new.py", "x = 1\n")
    sha = repo.commit("add new.py", *AUTHOR)
    assert sha == repo.head_sha() != before
    assert git(repo.path, "log", "-1", "--format=%an <%ae>|%s") == (
        "Grafter Bot <bot@example.invalid>|add new.py"
    )
    assert repo.changed_files() == []


def test_commit_nothing_raises(repo: GitRepo) -> None:
    with pytest.raises(GitError, match="nothing to commit"):
        repo.commit("empty", *AUTHOR)


def test_commit_runs_hooks(repo: GitRepo) -> None:
    hook = repo.path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho blocked-by-hook >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    put(repo.path / "new.py", "x = 1\n")
    with pytest.raises(GitError, match="blocked-by-hook"):
        repo.commit("msg", *AUTHOR)


# --- push ------------------------------------------------------------------------------


def test_push_ai_branch(repo: GitRepo, origin: Path) -> None:
    repo.fresh_branch("ai/x")
    put(repo.path / "new.py", "x = 1\n")
    sha = repo.commit("add", *AUTHOR)
    main_before = git(origin, "rev-parse", "main")

    repo.push("ai/x", prefix="ai/", production="main")

    assert git(origin, "rev-parse", "refs/heads/ai/x") == sha
    assert git(origin, "rev-parse", "main") == main_before


@pytest.mark.parametrize(
    "branch",
    [
        "main",
        "ai/../main",
        "-x",
        "feature/x",
        "ai/x y",
        "ai/x:main",
        "ai/",
        "ai/x.lock",
        "ai/x~1",
        "ai/@{u}",
        "ai/x/",
        "ai//x",
        "ai/x\n",
        "",
    ],
)
def test_push_unsafe_refused(repo: GitRepo, origin: Path, branch: str) -> None:
    put(repo.path / "new.py", "x = 1\n")
    repo.commit("add", *AUTHOR)
    before = bare_refs(origin)
    with pytest.raises(UnsafePush):
        repo.push(branch, prefix="ai/", production="main")
    assert bare_refs(origin) == before


def test_push_refuses_production_even_if_prefixed(repo: GitRepo, origin: Path) -> None:
    before = bare_refs(origin)
    with pytest.raises(UnsafePush):
        repo.push("ai/main", prefix="ai/", production="ai/main")
    with pytest.raises(UnsafePush):
        repo.push("main", prefix="", production="main")
    assert bare_refs(origin) == before


def test_push_rejected_by_remote_is_git_error(repo: GitRepo, origin: Path, tmp_path: Path) -> None:
    # Remote ai/x moved on; our non-fast-forward push must fail (no --force).
    other = tmp_path / "other"
    git(tmp_path, "clone", str(origin), str(other))
    git(other, "checkout", "-b", "ai/x")
    put(other / "o.txt", "o\n")
    git(other, "add", "-A")
    git(other, "-c", "user.name=O", "-c", "user.email=o@example.invalid", "commit", "-m", "o")
    git(other, "push", "origin", "ai/x")
    remote_sha = git(origin, "rev-parse", "refs/heads/ai/x")

    repo.fresh_branch("ai/x")
    put(repo.path / "new.py", "x = 1\n")
    repo.commit("add", *AUTHOR)
    with pytest.raises(GitError) as ei:
        repo.push("ai/x", prefix="ai/", production="main")
    assert not isinstance(ei.value, UnsafePush)
    assert git(origin, "rev-parse", "refs/heads/ai/x") == remote_sha


# --- branches / misc -------------------------------------------------------------------


def test_delete_local_branch(repo: GitRepo) -> None:
    repo.fresh_branch("ai/del")
    repo.fresh_branch("ai/other")
    repo.delete_local_branch("ai/del")
    assert git(repo.path, "branch", "--list", "ai/del") == ""
    with pytest.raises(GitError):
        repo.delete_local_branch("ai/del")
    with pytest.raises(GitError, match="invalid"):
        repo.delete_local_branch("-D")


def test_current_branch_detached_raises(repo: GitRepo) -> None:
    git(repo.path, "checkout", "--detach")
    with pytest.raises(GitError):
        repo.current_branch()


TOKEN_URL = "https://x-access-token:ghs_SECRET123abc@github.com/acme/api.git"


def test_scrub() -> None:
    s = scrub(f"fatal: unable to access '{TOKEN_URL}': 403")
    assert "ghs_SECRET123abc" not in s
    assert "x-access-token" not in s
    assert "https://***@github.com/acme/api.git" in s
    assert scrub("http://user:pw@host/x") == "http://***@host/x"
    assert scrub("no secrets here") == "no secrets here"


def test_git_error_scrubs_token_url(tmp_path: Path) -> None:
    # Unreachable port: clone fails fast and git echoes the URL in stderr.
    url = "https://x-access-token:ghs_SECRET123abc@127.0.0.1:1/acme/api.git"
    with pytest.raises(GitError) as ei:
        GitRepo.clone_or_fetch(url, tmp_path / "w")
    text = f"{ei.value} {ei.value.cmd} {ei.value.stderr} {ei.value!r}"
    assert "ghs_SECRET123abc" not in text
    assert "x-access-token" not in text


def test_git_error_message_tail() -> None:
    e = GitError("git x", "line\n" * 100 + "last")
    assert e.cmd == "git x"
    assert str(e).startswith("git x failed")
    assert str(e).endswith("last")
    assert len(e.stderr) <= 2000
