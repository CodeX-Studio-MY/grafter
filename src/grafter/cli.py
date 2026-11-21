"""grafter CLI."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import typer
from pydantic import ValidationError

from grafter import __version__
from grafter.charter import load_charter
from grafter.config import Config, RepoConfig, load_config
from grafter.credentials import SecretError
from grafter.doctor import run_checks
from grafter.engines import AgentEngine, get_engine
from grafter.findings import SEVERITIES
from grafter.gateway import GatewayClient, GatewayError, redact
from grafter.github import AppAuth, GitHubClient, app_auth
from grafter.lock import KillSwitchEngaged, LockHeld
from grafter.pipeline import RunDeps, RunSummary, run_night
from grafter.runner import DockerRunner, LocalRunner, default_image
from grafter.scanners import DockerScanner
from grafter.store import Store

MASTER_KEY_ENV = "LITELLM_MASTER_KEY"
# Test-only escape hatch: lets the offline suite exercise real-run wiring with runner: local.
ALLOW_LOCAL_REAL_RUN_ENV = "GRAFTER_UNSAFE_ALLOW_LOCAL_REAL_RUN"

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command()
def validate(
    path: Annotated[Path, typer.Argument()] = Path("grafter.charter.yaml"),
    config: Annotated[
        Path | None, typer.Option("--config", help="Also validate this grafter.yaml.")
    ] = None,
) -> None:
    """Validate a Solution Charter (and optionally a config against it)."""
    try:
        charter = load_charter(path)
    except (ValidationError, ValueError, FileNotFoundError) as e:
        typer.secho(f"invalid charter: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from e
    typer.secho(f"ok: {charter.solution}", fg=typer.colors.GREEN)
    typer.echo(f"  domains: {len(charter.core_domains)}  invariants: {len(charter.invariants)}")
    typer.echo(f"  protected: {charter.protected_paths}")
    typer.echo(f"  review: {charter.review_paths}")
    for name, rules in charter.repos.items():
        typer.echo(f"  {name}: protected {rules.protected_paths}  review {rules.review_paths}")
    if config is None:
        return
    try:
        cfg = load_config(config)
    except (ValidationError, ValueError, FileNotFoundError) as e:
        typer.secho(f"invalid config: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from e
    names = {r.name for r in cfg.repos}
    unknown = sorted(set(charter.repos) - names)
    if unknown:
        typer.secho(
            f"charter repos not in config: {unknown} (config has {sorted(names)})",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)
    typer.echo(f"  repos: {len(cfg.repos)} ({', '.join(r.name for r in cfg.repos)})")


gateway_app = typer.Typer(no_args_is_help=True, help="LiteLLM gateway commands.")
app.add_typer(gateway_app, name="gateway")

CHECK_BUDGET_USD = 0.05


@gateway_app.command("check")
def gateway_check(
    config: Annotated[Path, typer.Option("--config", help="grafter.yaml")] = Path("grafter.yaml"),
) -> None:
    """End-to-end gateway check: health, scoped key, one tiny completion, key deleted."""
    try:
        cfg = load_config(config)
    except (ValidationError, ValueError, FileNotFoundError) as e:
        typer.secho(f"invalid config: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from e
    master_key = os.environ.get(MASTER_KEY_ENV, "")
    if not master_key:
        typer.secho(f"{MASTER_KEY_ENV} is not set", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    client = GatewayClient(cfg.llm_gateway_url, master_key)
    model = cfg.models.coder
    try:
        client.health()
        typer.echo(f"gateway up: {client.url}")
        key = client.create_run_key(f"check-{uuid4().hex[:8]}", [model], CHECK_BUDGET_USD, "1h")
        typer.echo(f"scoped key: {redact(key)} (models [{model}], ${CHECK_BUDGET_USD}, 1h)")
        try:
            reply = client.chat(key, model, "Reply with the single word: ok")
        finally:
            client.delete_key(key)
    except GatewayError as e:
        typer.secho(f"gateway check failed: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from e
    typer.secho(f"ok: {model} replied {reply.strip()[:40]!r}; key deleted", fg=typer.colors.GREEN)


db_app = typer.Typer(no_args_is_help=True, help="State database (SQLite) commands.")
app.add_typer(db_app, name="db")

DEFAULT_HOME = "~/.grafter"
DbOption = Annotated[
    Path | None, typer.Option("--db", help="Database file (default: <home>/grafter.db).")
]


def _db_path(config: Path, db: Path | None) -> Path:
    try:
        cfg = load_config(config)
    except (ValidationError, ValueError, FileNotFoundError) as e:
        typer.secho(f"invalid config: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from e
    if db is not None:
        return db
    home = str(getattr(cfg, "home", None) or DEFAULT_HOME)
    return Path(os.path.expandvars(home)).expanduser() / "grafter.db"


@db_app.command("migrate")
def db_migrate(
    config: Annotated[Path, typer.Option("--config", help="grafter.yaml")] = Path("grafter.yaml"),
    db: DbOption = None,
) -> None:
    """Create or upgrade the state database to the latest schema."""
    path = _db_path(config, db)
    with Store(path) as store:
        version = store.migrate()
    typer.secho(f"ok: {path} at schema version {version}", fg=typer.colors.GREEN)


@db_app.command("backup")
def db_backup(
    dest: Annotated[Path, typer.Argument(help="Backup file to write.")],
    config: Annotated[Path, typer.Option("--config", help="grafter.yaml")] = Path("grafter.yaml"),
    db: DbOption = None,
) -> None:
    """Write a consistent copy of the state database (safe while Grafter runs)."""
    path = _db_path(config, db)
    if not path.is_file():
        typer.secho(f"no database at {path}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    with Store(path) as store:
        out = store.backup(dest)
    typer.secho(f"ok: backup written to {out}", fg=typer.colors.GREEN)


EXIT_KILL_SWITCH = 3
EXIT_LOCK_HELD = 4


def _load(config: Path) -> Config:
    try:
        return load_config(config)
    except (ValidationError, ValueError, FileNotFoundError) as e:
        typer.secho(f"invalid config: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from e


def _fail(message: str, code: int) -> typer.Exit:
    typer.secho(message, fg=typer.colors.RED, err=True)
    return typer.Exit(code)


def _engine(cfg: Config) -> AgentEngine:
    try:
        return get_engine(cfg.engine, cfg)
    except (ValueError, ImportError) as e:
        raise _fail(str(e), 2) from e
    except SecretError as e:
        raise _fail(f"engine {cfg.engine!r}: {e}", 1) from e


def sandbox_image(repo: RepoConfig) -> str:
    return repo.sandbox_image or default_image(repo.language)


def docker_runner(repo: RepoConfig) -> DockerRunner:
    return DockerRunner(sandbox_image(repo))


def scanner_for(cfg: Config) -> DockerScanner | None:
    """Scanners run only with the Docker runner (they are containers too)."""
    if cfg.runner != "docker" or not cfg.scanning.tools:
        return None
    return DockerScanner(cfg.scanning)


def origin_url_for(auth: AppAuth | None) -> Callable[[RepoConfig], str]:
    def origin_url(repo: RepoConfig) -> str:
        if repo.clone_url:
            return repo.clone_url
        if auth is None:
            return f"https://github.com/{repo.github}.git"
        return f"https://x-access-token:{auth.token()}@github.com/{repo.github}.git"

    return origin_url


@app.command()
def run(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Do everything except push and open PRs.")
    ] = False,
    config: Annotated[Path, typer.Option("--config", help="grafter.yaml")] = Path("grafter.yaml"),
) -> None:
    """Run one nightly cycle: find test work, build, verify, open PRs."""
    cfg = _load(config)
    dry = dry_run or cfg.dry_run
    unsafe_ok = os.environ.get(ALLOW_LOCAL_REAL_RUN_ENV) == "1"
    if cfg.runner == "local" and cfg.engine != "noop" and not unsafe_ok:
        raise _fail(
            f"engine: {cfg.engine} with runner: local would run agent-written tests on this "
            "host in the gates, even in a dry run; use runner: docker",
            2,
        )
    if cfg.runner == "local" and not dry and not unsafe_ok:
        raise _fail(
            "runner: local is only allowed with --dry-run: a real run would execute "
            "repo code on this host next to Grafter's secrets; use runner: docker",
            2,
        )
    engine = _engine(cfg)
    auth: AppAuth | None = None
    try:
        auth = app_auth(cfg)
    except SecretError as e:
        if not dry:
            raise _fail(f"GitHub App not usable for a real run: {e}", 1) from e
        typer.secho(f"note: GitHub App not used ({e}); cloning anonymously", err=True)
    github = None if dry or auth is None else GitHubClient(auth, api_url=cfg.github_app.api_url)
    if cfg.runner == "local":
        typer.secho(
            "warning: runner local runs repo commands on this host (offline testing only)",
            fg=typer.colors.YELLOW,
            err=True,
        )
    deps = RunDeps(
        engine=lambda found: engine,
        runner=LocalRunner() if cfg.runner == "local" else docker_runner(cfg.repos[0]),
        runner_for=None if cfg.runner == "local" else docker_runner,
        github=github,
        origin_url=origin_url_for(auth),
        scanner=scanner_for(cfg),
    )
    try:
        summary = run_night(cfg, deps, dry_run=dry)
    except KillSwitchEngaged as e:
        raise _fail(str(e), EXIT_KILL_SWITCH) from e
    except LockHeld as e:
        raise _fail(str(e), EXIT_LOCK_HELD) from e

    mode = "dry run" if dry else "run"
    typer.echo(
        f"{mode} {summary.run_id}: {len(summary.prs)} PR(s){' (would open)' if dry else ''}, "
        f"{len(summary.rejected)} rejected, {len(summary.skipped)} skipped, "
        f"{summary.tokens} tokens; stopped: {summary.stopped_reason}"
    )
    for pr in summary.prs:
        typer.echo(f"  PR {pr.get('url') or pr['branch']}: {pr['target']}")
    for rej in summary.rejected:
        typer.echo(f"  rejected {rej['target']}: {'; '.join(rej['reasons'])[:200]}")
    for err in summary.errors:
        typer.echo(f"  error {err['repo']}: {err['error'][:200]}")
    _print_findings(summary)
    typer.echo(f"report: {summary.report_path}")
    if summary.report_url:
        typer.echo(f"report comment: {summary.report_url}")
    if summary.kill_switch:
        raise typer.Exit(EXIT_KILL_SWITCH)
    if summary.failed:
        raise typer.Exit(1)


def _print_findings(summary: RunSummary) -> None:
    for f in summary.findings:
        counts = f"{f['open']} open ({f['new']} new, {f['gone']} gone)"
        typer.echo(f"  findings {f['repo']} {f['tool']}: {counts}")
    for e in summary.scan_errors:
        typer.echo(f"  scan failed {e['repo']} {e['tool']}: {e['error'][:200]}")


@app.command()
def scan(
    config: Annotated[Path, typer.Option("--config", help="grafter.yaml")] = Path("grafter.yaml"),
) -> None:
    """Refresh scanner databases, scan every repo's main branch, store the findings (1E2).
    Read-only: no engine, no branches, no PRs."""
    cfg = _load(config)
    scanner = scanner_for(cfg)
    if scanner is None:
        raise _fail("scanning needs runner: docker and at least one tool in scanning.tools", 2)
    auth: AppAuth | None = None
    try:
        auth = app_auth(cfg)
    except SecretError as e:
        typer.secho(f"note: GitHub App not used ({e}); cloning anonymously", err=True)
    deps = RunDeps(
        engine=lambda found: get_engine("noop"),
        runner=docker_runner(cfg.repos[0]),
        github=None,
        origin_url=origin_url_for(auth),
        scanner=scanner,
    )
    try:
        summary = run_night(cfg, deps, dry_run=True, scan_only=True)
    except KillSwitchEngaged as e:
        raise _fail(str(e), EXIT_KILL_SWITCH) from e
    except LockHeld as e:
        raise _fail(str(e), EXIT_LOCK_HELD) from e
    typer.echo(f"scan {summary.run_id}: stopped: {summary.stopped_reason}")
    _print_findings(summary)
    for err in summary.errors:
        typer.echo(f"  error {err['repo']}: {err['error'][:200]}")
    typer.echo(f"report: {summary.report_path}")
    if summary.failed or summary.errors or summary.scan_errors:
        raise typer.Exit(1)


@app.command()
def findings(
    config: Annotated[Path, typer.Option("--config", help="grafter.yaml")] = Path("grafter.yaml"),
    repo: Annotated[str | None, typer.Option("--repo")] = None,
    tool: Annotated[str | None, typer.Option("--tool", help="trivy or osv")] = None,
    min_severity: Annotated[
        str, typer.Option("--min-severity", help="low, medium, high or critical")
    ] = "low",
    status: Annotated[str, typer.Option("--status", help="open, gone or all")] = "open",
) -> None:
    """List stored scanner findings (candidate tasks; phase 1 does not act on them)."""
    cfg = _load(config)
    if min_severity not in SEVERITIES:
        raise _fail(f"--min-severity must be one of {', '.join(SEVERITIES)}", 2)
    if status not in ("open", "gone", "all"):
        raise _fail("--status must be open, gone or all", 2)
    db = cfg.paths().db_file
    if not db.is_file():
        typer.echo("no findings yet (no database); run grafter scan")
        return
    with Store(db) as store:
        store.migrate()
        rows = store.list_findings(status, repo, tool, min_severity)
    for r in rows:
        where = f"{r['path']}:{r['line']}" if r["line"] else r["path"]
        gone = "  (gone)" if r["status"] == "gone" else ""
        typer.echo(
            f"{r['severity']:<8} {r['tool']:<5} {r['repo']:<10} {where:<36} {r['rule_id']:<20}"
            f" {r['message'][:80]}{gone}"
        )
    typer.echo(f"{len(rows)} finding(s)")


_STATUS_COLOR = {"ok": typer.colors.GREEN, "warn": typer.colors.YELLOW, "fail": typer.colors.RED}


@app.command()
def doctor(
    config: Annotated[Path, typer.Option("--config", help="grafter.yaml")] = Path("grafter.yaml"),
) -> None:
    """Check this host: config, home, kill switch, lock, DB, git, Docker, engine, gateway, App."""
    checks = run_checks(config)
    width = max(len(c.name) for c in checks)
    for c in checks:
        status = typer.style(f"{c.status:<4}", fg=_STATUS_COLOR[c.status])
        typer.echo(f"{status}  {c.name:<{width}}  {c.detail}")
    failed = [c.name for c in checks if c.status == "fail"]
    if failed:
        typer.secho(f"{len(failed)} check(s) failed: {', '.join(failed)}", fg=typer.colors.RED)
        raise typer.Exit(1)
    warned = sum(c.status == "warn" for c in checks)
    typer.secho(f"ok ({warned} warning(s))", fg=typer.colors.GREEN)


if __name__ == "__main__":
    app()
