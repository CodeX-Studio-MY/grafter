"""grafter CLI."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import typer
from pydantic import ValidationError

from grafter import __version__
from grafter.charter import load_charter
from grafter.config import load_config
from grafter.gateway import GatewayClient, GatewayError, redact
from grafter.store import Store

MASTER_KEY_ENV = "LITELLM_MASTER_KEY"

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


@app.command()
def run() -> None:
    """Run one nightly cycle (phase 1)."""
    typer.secho("not implemented yet: lands in phase 1", fg=typer.colors.YELLOW)
    raise typer.Exit(2)


if __name__ == "__main__":
    app()
