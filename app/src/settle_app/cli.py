"""``settle-app`` on the command line.

Separate from the engine's ``settle`` command on purpose. Adding a ``settle
web`` subcommand would make the engine aware of the app, and the point of two
distributions is that it is not.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from typing import Annotated

import typer

from settle_app import APP_VERSION
from settle_app.settings import WebConfigError, WebSettings
from settle_app.store import RunStore

app = typer.Typer(
    name="settle-app",
    help="Serve the settle reconciliation engine in a browser.",
    no_args_is_help=True,
    add_completion=False,
)

EXIT_BAD_CONFIG = 2


@app.command()
def serve(
    host: Annotated[
        str | None, typer.Option(help="Bind address. Overrides SETTLE_WEB_HOST.")
    ] = None,
    port: Annotated[int | None, typer.Option(help="Bind port. Overrides SETTLE_WEB_PORT.")] = None,
    data_dir: Annotated[
        Path | None, typer.Option("--data-dir", help="Where the database and run files live.")
    ] = None,
) -> None:
    """Serve the web interface."""
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - uvicorn is a hard dependency
        typer.secho("uvicorn is not installed", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_BAD_CONFIG) from exc

    from settle_app.app import create_app

    settings = _settings(host=host, port=port, data_dir=data_dir)
    instance = create_app(settings)
    if settings.password is None:
        typer.secho(
            "no password set: this instance is unprotected and bound to "
            f"{settings.host}. Do not expose it.",
            fg=typer.colors.YELLOW,
            err=True,
        )
    uvicorn.run(instance, host=settings.host, port=settings.port)  # pragma: no cover


@app.command()
def backup(
    destination: Annotated[Path, typer.Argument(help="Where to write the database copy.")],
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """Copy the database safely while the app is running.

    Uses SQLite's online backup API. Copying the file with ``cp`` during a WAL
    write captures a torn database, which is the kind of thing you find out
    about on the day you need the backup.
    """
    settings = _settings(data_dir=data_dir)
    if not settings.database_path.exists():
        typer.secho(f"no database at {settings.database_path}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_BAD_CONFIG)
    RunStore(settings.database_path).backup_to(destination)
    typer.echo(f"wrote {destination}")


@app.command()
def purge(data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None) -> None:
    """Delete runs past their retention window, and abandoned uploads.

    The app also does this at startup. This command exists for deployments that
    stay up for months, where "at startup" is not often enough.
    """
    from settle_app.artifacts import ArtifactStore
    from settle_app.maintenance import sweep

    settings = _settings(data_dir=data_dir)
    result = sweep(
        store=RunStore(settings.database_path),
        artifacts=ArtifactStore(settings.runs_dir),
        settings=settings,
    )
    if settings.retention_days is None:
        typer.secho(
            "no retention window is set, so nothing expires; "
            "set SETTLE_WEB_RETENTION_DAYS if runs should not be kept forever",
            fg=typer.colors.YELLOW,
            err=True,
        )
    typer.echo(f"deleted {result.expired} expired and {result.abandoned} abandoned run(s)")


@app.command()
def version() -> None:
    """Print the app version."""
    typer.echo(APP_VERSION)


def _settings(
    *, host: str | None = None, port: int | None = None, data_dir: Path | None = None
) -> WebSettings:
    """Environment first, then explicit flags, then validate once."""
    try:
        settings = WebSettings.from_env()
        return replace(
            settings,
            host=settings.host if host is None else host,
            port=settings.port if port is None else port,
            data_dir=settings.data_dir if data_dir is None else data_dir,
        )
    except WebConfigError as exc:
        typer.secho(f"configuration error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_BAD_CONFIG) from exc


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
