"""Local git operations for one target repo: clone, fresh branch, diff, commit, safe push.

Every call runs `git` with an argument list (never a shell), no prompts, and stable output.
Errors never carry credentials: URLs like `https://x-access-token:...@host` are scrubbed.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

GIT_BASE = ("git", "-c", "core.autocrlf=false", "-c", "core.quotepath=off")
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "GCM_INTERACTIVE": "never"}
TIMEOUT_S = 600
STDERR_TAIL = 2000

_CRED_URL = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s@'\"]+@")
_BAD_REF_CHARS = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")
SYMLINK_MODE = "120000"


def scrub(text: str) -> str:
    """Remove userinfo (user:token@) from any URL in `text`."""
    return _CRED_URL.sub(r"\1***@", text)


class GitError(RuntimeError):
    def __init__(self, cmd: str, stderr: str = "") -> None:
        self.cmd = scrub(cmd)
        self.stderr = scrub(stderr.strip())[-STDERR_TAIL:]
        super().__init__(f"{self.cmd} failed: {self.stderr}" if self.stderr else self.cmd)


class UnsafePush(GitError):
    """A push was refused before running git."""


@dataclass
class DiffStats:
    added: int
    removed: int
    changed_files: list[str] = field(default_factory=list)
    deleted_files: list[str] = field(default_factory=list)
    symlinks: list[str] = field(default_factory=list)
    binary_files: list[str] = field(default_factory=list)


def ref_problem(name: str) -> str | None:
    """Why `name` is not a safe branch name (subset of `git check-ref-format`), or None."""
    if not name:
        return "empty"
    if name.startswith("-"):
        return "starts with '-'"
    if _BAD_REF_CHARS.search(name):
        return "contains a space, control or special character"
    if ".." in name or "@{" in name or "//" in name or name == "@":
        return "contains '..', '@{', '//' or is '@'"
    if name.startswith("/") or name.endswith(("/", ".")):
        return "starts with '/' or ends with '/' or '.'"
    if any(part.startswith(".") or part.endswith(".lock") for part in name.split("/")):
        return "a component starts with '.' or ends with '.lock'"
    return None


def _check_ref(name: str, what: str = "branch") -> None:
    problem = ref_problem(name)
    if problem:
        raise GitError(f"invalid {what} name {name!r}", problem)


def _run(args: list[str], cwd: Path | None = None) -> str:
    cmd = [*GIT_BASE, *args]
    summary = "git " + " ".join(args[:2])
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env={**os.environ, **GIT_ENV},
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as e:  # pragma: no cover - needs a hung remote
        raise GitError(summary, f"timed out after {TIMEOUT_S}s") from e
    out = proc.stdout.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace") or out
        raise GitError(summary, err)
    return out


def _split_z(out: str) -> list[str]:
    return [p for p in out.split("\0") if p]


class GitRepo:
    def __init__(self, path: Path) -> None:
        self.path = path

    def git(self, *args: str) -> str:
        return _run(list(args), cwd=self.path)

    # --- setup -------------------------------------------------------------------------

    @classmethod
    def clone_or_fetch(cls, url: str, dest: Path) -> GitRepo:
        if url.startswith("-"):
            raise GitError("git clone", "unsafe url (starts with '-')")
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            _run(["clone", "--", url, str(dest)])
            return cls(dest)
        if not _is_repo_root(dest):
            raise GitError("git fetch", f"{dest} exists and is not a git repository")
        repo = cls(dest)
        repo.git("fetch", "--prune", "origin")
        return repo

    # --- inspection --------------------------------------------------------------------

    def head_sha(self) -> str:
        return self.git("rev-parse", "--verify", "HEAD").strip()

    def current_branch(self) -> str:
        return self.git("symbolic-ref", "--short", "HEAD").strip()

    # --- branches ----------------------------------------------------------------------

    def fresh_branch(self, name: str, base: str = "main") -> str:
        """Point `name` at origin/`base` with a pristine work tree; return the base sha."""
        _check_ref(name)
        _check_ref(base, "base")
        remote = f"refs/remotes/origin/{base}"
        self.git("fetch", "origin", f"+refs/heads/{base}:{remote}")
        self.git("checkout", "-f", "-B", name, remote)
        self.git("reset", "--hard", remote)
        self.git("clean", "-fdx")
        return self.head_sha()

    def delete_local_branch(self, name: str) -> None:
        _check_ref(name)
        self.git("branch", "-D", "--", name)

    # --- diff --------------------------------------------------------------------------

    def _stage(self) -> None:
        self.git("add", "-A")

    def changed_files(self) -> list[str]:
        """Files changed vs HEAD, untracked included (stages everything)."""
        self._stage()
        return _split_z(self.git("diff", "--cached", "--no-renames", "--name-only", "-z"))

    def diff(self) -> str:
        self._stage()
        return self.git("diff", "--cached", "--no-renames", "--no-color", "--no-ext-diff")

    def diff_stats(self) -> DiffStats:
        self._stage()
        stats = DiffStats(0, 0)
        for rec in _split_z(self.git("diff", "--cached", "--no-renames", "--numstat", "-z")):
            added, removed, path = rec.split("\t", 2)
            stats.changed_files.append(path)
            if added == "-" or removed == "-":
                stats.binary_files.append(path)
            else:
                stats.added += int(added)
                stats.removed += int(removed)
        stats.deleted_files = _split_z(
            self.git("diff", "--cached", "--no-renames", "--diff-filter=D", "--name-only", "-z")
        )
        # --raw -z: ":<old> <new> <sha> <sha> <status>\0<path>\0" per file.
        raw = _split_z(self.git("diff", "--cached", "--no-renames", "--raw", "-z"))
        for meta, path in zip(raw[::2], raw[1::2], strict=True):
            if meta.split()[1] == SYMLINK_MODE:
                stats.symlinks.append(path)
        return stats

    # --- commit / push -----------------------------------------------------------------

    def commit(self, message: str, author_name: str, author_email: str) -> str:
        """Commit all changes (hooks run; no --no-verify) and return the new sha."""
        self._stage()
        if not self.git("diff", "--cached", "--name-only", "-z"):
            raise GitError("git commit", "nothing to commit")
        self.git(
            "-c",
            f"user.name={author_name}",
            "-c",
            f"user.email={author_email}",
            "commit",
            "-q",
            "-m",
            message,
        )
        return self.head_sha()

    def push(self, branch: str, prefix: str, production: str) -> None:
        """Push `branch` to the same name on origin. Never forced; never to production."""
        problem = ref_problem(branch)
        if problem:
            raise UnsafePush("git push", f"unsafe branch {branch!r}: {problem}")
        if not prefix or not branch.startswith(prefix) or branch == prefix:
            raise UnsafePush("git push", f"branch {branch!r} lacks prefix {prefix!r}")
        if branch.casefold() == production.casefold():
            raise UnsafePush("git push", f"refusing to push to production branch {production!r}")
        ref = f"refs/heads/{branch}"
        self.git("push", "--porcelain", "origin", f"{ref}:{ref}")


def _is_repo_root(path: Path) -> bool:
    if not path.is_dir():
        return False
    try:
        top = _run(["rev-parse", "--show-toplevel"], cwd=path).strip()
    except GitError:
        return False
    try:
        return os.path.samefile(top, path)
    except OSError:  # pragma: no cover - toplevel vanished mid-call
        return False
