"""grafter CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from grafter import __version__
from grafter.charter import load_charter

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command()
def validate(path: Annotated[Path, typer.Argument()] = Path("grafter.charter.yaml")) -> None:
    """Validate a Solution Charter."""
    try:
        charter = load_charter(path)
    except (ValidationError, ValueError, FileNotFoundError) as e:
        typer.secho(f"invalid charter: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from e
    typer.secho(f"ok: {charter.solution}", fg=typer.colors.GREEN)
    typer.echo(f"  domains: {len(charter.core_domains)}  invariants: {len(charter.invariants)}")
    typer.echo(f"  protected: {charter.protected_paths}")
    typer.echo(f"  review: {charter.review_paths}")


@app.command()
def run() -> None:
    """Run one nightly cycle (phase 1)."""
    typer.secho("not implemented yet: lands in phase 1", fg=typer.colors.YELLOW)
    raise typer.Exit(2)


if __name__ == "__main__":
    app()
