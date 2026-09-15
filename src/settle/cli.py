"""``settle`` on the command line.

Like the API, this is a wrapper. It reads CSVs, calls the domain, writes files,
and prints a summary a human can act on.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import structlog
import typer

from settle.domain.config import MatchConfig
from settle.domain.errors import InvariantViolation, LedgerError, SettleError
from settle.domain.match import ENGINE_VERSION, reconcile
from settle.domain.models import ReconciliationResult
from settle.domain.ports import ReferenceParser
from settle.io import csv_io
from settle.io.schemas import ConfigModel

app = typer.Typer(
    name="settle",
    help="Match incoming payments to invoices, or route them to a human.",
    no_args_is_help=True,
    add_completion=False,
)

EXIT_BAD_INPUT = 2
EXIT_INVARIANT_VIOLATION = 3


def _configure_logging(*, verbose: bool, json_logs: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    renderer = structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(level),
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            renderer,
        ],
    )


@app.command()
def run(
    invoices: Annotated[Path, typer.Argument(help="Invoice ledger CSV.")],
    payments: Annotated[Path, typer.Argument(help="Bank statement CSV.")],
    out: Annotated[
        Path, typer.Option("--out", "-o", help="Directory for result.json and the CSV outputs.")
    ] = Path("out"),
    config_file: Annotated[
        Path | None, typer.Option("--config", "-c", help="JSON file of run policy overrides.")
    ] = None,
    use_model: Annotated[
        bool,
        typer.Option(
            "--use-model",
            help=(
                "Enable the model-backed reference parser for references the "
                "deterministic tier cannot read. Needs the 'model' extra and an "
                "ANTHROPIC_API_KEY. Off by default: the engine is complete without it."
            ),
        ),
    ] = False,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Log every allocation decision.")
    ] = False,
    json_logs: Annotated[bool, typer.Option("--json-logs", help="Emit logs as JSON.")] = False,
) -> None:
    """Reconcile a bank statement against an invoice ledger."""
    _configure_logging(verbose=verbose, json_logs=json_logs)

    try:
        config = _load_config(config_file)
        ledger = csv_io.read_invoices(invoices)
        statement = csv_io.read_payments(payments)
        parser = _build_parser(use_model=use_model)
        result = reconcile(ledger, statement, config=config, parser=parser)
    except LedgerError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_BAD_INPUT) from exc
    except InvariantViolation as exc:
        typer.secho(
            f"the engine produced an unsafe result and discarded it:\n{exc}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(EXIT_INVARIANT_VIOLATION) from exc
    except SettleError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_BAD_INPUT) from exc

    written = csv_io.write_all(out, result)
    _print_summary(result, written)


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Bind port.")] = 8000,
) -> None:
    """Serve POST /reconcile, the same engine behind HTTP."""
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover
        typer.secho(
            "the API needs the 'api' extra: pip install 'settle-engine[api]'",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(EXIT_BAD_INPUT) from exc
    uvicorn.run("settle.api.app:app", host=host, port=port)


@app.command()
def version() -> None:
    """Print the engine version."""
    typer.echo(ENGINE_VERSION)


def _load_config(path: Path | None) -> MatchConfig:
    if path is None:
        return ConfigModel().to_domain()
    if not path.exists():
        raise LedgerError(f"config file not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"), parse_float=str)
    except json.JSONDecodeError as exc:
        raise LedgerError(f"{path}: not valid JSON: {exc}") from exc
    try:
        return ConfigModel.model_validate(raw).to_domain()
    except ValueError as exc:
        raise LedgerError(f"{path}: invalid config: {exc}") from exc


def _build_parser(*, use_model: bool) -> ReferenceParser:
    from settle.parse import default_parser

    try:
        return default_parser(use_model=use_model)
    except RuntimeError as exc:
        raise LedgerError(str(exc)) from exc


def _print_summary(result: ReconciliationResult, written: Sequence[Path]) -> None:
    report = result.report
    bold = typer.style("settle", bold=True)
    typer.echo(f"{bold} {report.engine_version}")
    typer.echo(
        f"  {report.payments_total} payments, {report.money_in} in, "
        f"{report.money_allocated} allocated, {report.money_residual} unallocated"
    )
    typer.echo(
        f"  matched {report.payments_matched}"
        f"  partial {report.payments_partially_matched}"
        f"  review {report.payments_in_review}"
        f"  unmatched {report.payments_unmatched}"
    )
    typer.echo(
        f"  invoices: {report.invoices_paid} paid, "
        f"{report.invoices_settled_with_fee} settled with fee "
        f"({report.money_fees}), {report.invoices_partially_paid} part paid, "
        f"{report.invoices_open} open"
    )
    if report.model_parser_calls:
        typer.echo(f"  model parser calls: {report.model_parser_calls}")

    for warning in result.warnings:
        typer.secho(f"  warning: {warning}", fg=typer.colors.YELLOW)

    queue = len(result.review_queue)
    if queue:
        typer.secho(
            f"  {queue} payment(s) need a human: {written[2]}",
            fg=typer.colors.YELLOW,
        )
    else:
        typer.secho("  nothing needs a human", fg=typer.colors.GREEN)

    typer.echo("  wrote " + ", ".join(str(path) for path in written))


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
