"""Load secrets from the environment or from a protected file. Standard library only.

Lookup order for a secret `NAME`:
  1. env `GRAFTER_<NAME>`
  2. env `<NAME>` (compatibility, e.g. `LITELLM_MASTER_KEY`)
  3. file at env `GRAFTER_<NAME>_FILE`

A file is refused when it is missing or empty, sits inside a git work tree or a forbidden
directory (e.g. the sandbox work dir), or (POSIX) is readable by group/other.
Error messages name variables and paths, never secret values.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class SecretError(RuntimeError):
    """A required secret is missing or stored unsafely."""


def _normalise(value: str) -> str:
    """Drop the trailing newline of a single-line secret; keep multi-line (PEM) as is."""
    stripped = value.rstrip("\r\n")
    return value if "\n" in stripped else stripped


def _in_git_work_tree(path: Path) -> Path | None:
    for parent in path.parents:
        if (parent / ".git").exists():
            return parent
    return None


def _read_secret_file(var: str, raw: str, forbidden_dirs: Iterable[Path]) -> str:
    path = Path(os.path.expandvars(raw)).expanduser()
    if not path.exists():
        raise SecretError(f"{var}: file {path} does not exist")
    if not path.is_file():
        raise SecretError(f"{var}: {path} is not a regular file")
    resolved = path.resolve()
    repo = _in_git_work_tree(resolved)
    if repo is not None:
        raise SecretError(
            f"{var}: {resolved} is inside the git work tree {repo}; move it out of any repo"
        )
    for d in forbidden_dirs:
        base = Path(os.path.expandvars(str(d))).expanduser().resolve()
        if resolved.is_relative_to(base):
            raise SecretError(f"{var}: {resolved} is inside the forbidden directory {base}")
    if os.name != "nt":
        mode = resolved.stat().st_mode & 0o777
        if mode & 0o077:
            raise SecretError(
                f"{var}: {resolved} has mode {mode:03o}; it must not be readable by group or "
                f"others (chmod 600 {resolved})"
            )
    try:
        content = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise SecretError(f"{var}: cannot read {resolved} ({type(e).__name__})") from None
    if not content.strip():
        raise SecretError(f"{var}: file {resolved} is empty")
    return _normalise(content)


def load_secret(
    name: str,
    environ: Mapping[str, str] | None = None,
    forbidden_dirs: Iterable[Path] = (),
) -> str:
    if not _NAME_RE.fullmatch(name):
        raise SecretError(f"invalid secret name {name!r}; use UPPER_SNAKE_CASE")
    env = os.environ if environ is None else environ
    prefixed, file_var = f"GRAFTER_{name}", f"GRAFTER_{name}_FILE"
    for var in (prefixed, name):
        value = env.get(var, "")
        if value.strip():
            return _normalise(value)
    raw = env.get(file_var, "").strip()
    if raw:
        return _read_secret_file(file_var, raw, forbidden_dirs)
    raise SecretError(f"secret {name} is not set; set {prefixed} or {file_var} (path to a file)")


def require_secrets(
    names: Iterable[str],
    environ: Mapping[str, str] | None = None,
    forbidden_dirs: Iterable[Path] = (),
) -> dict[str, str]:
    """Load every secret; report all problems at once."""
    dirs = list(forbidden_dirs)
    found: dict[str, str] = {}
    errors: list[str] = []
    for name in names:
        try:
            found[name] = load_secret(name, environ=environ, forbidden_dirs=dirs)
        except SecretError as e:
            errors.append(str(e))
    if errors:
        raise SecretError("missing or unsafe secrets:\n  " + "\n  ".join(errors))
    return found
