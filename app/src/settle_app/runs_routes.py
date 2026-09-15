"""Uploading, reconciling, reading and deleting a run.

The handlers here are deliberately short. Anything with a decision in it lives
in :mod:`settle_app.runner`, :mod:`settle_app.views` or the engine, because a
route handler is the one place in a web application where logic reliably goes
untested.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Final

from fastapi import FastAPI, Form, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from settle.domain.config import DEFAULT_CONFIG
from settle.domain.errors import InvariantViolation, LedgerError
from settle.domain.match import ENGINE_VERSION
from settle.io.json_io import result_to_dict
from settle_app import artifacts as art
from settle_app.runner import TooManyRowsError, execute
from settle_app.views import ResultViews, RunFiles, build_views

if TYPE_CHECKING:  # pragma: no cover
    from settle_app.app import Web

SAMPLES_DIR: Final = Path(__file__).parent / "samples"
UPLOAD_CHUNK: Final = 64 * 1024


def register(app: FastAPI, web: Web) -> None:
    """Attach the run routes. Called by the app factory."""
    from settle_app.app import CSRF_FIELD, redirect

    def _guard(request: Request) -> Response | None:
        return None if web.is_signed_in(request) else redirect("/login")

    @app.get("/runs/new", response_class=HTMLResponse, include_in_schema=False)
    def new_run(request: Request) -> Response:
        if (blocked := _guard(request)) is not None:
            return blocked
        return web.render(
            request,
            "new.html",
            {"max_rows": web.ctx.settings.max_rows, "max_mb": _megabytes(web)},
        )

    @app.post("/runs", include_in_schema=False)
    async def create_run(
        request: Request,
        invoices: UploadFile | None = None,
        payments: UploadFile | None = None,
        label: str = Form(default=""),
        use_sample: str = Form(default=""),
        csrf_token: str = Form(default="", alias=CSRF_FIELD),
    ) -> Response:
        if (blocked := _guard(request)) is not None:
            return blocked
        if not web.csrf_ok(request, csrf_token):
            return _refuse(web, request, "That form expired. Please try again.", 403)

        run_id = art.new_run_id()
        directory = web.ctx.artifacts.ensure(run_id)

        try:
            if use_sample:
                label = label or "Sample data"
                _install_samples(directory)
            else:
                await _store_upload(web, invoices, directory / art.CANONICAL_INVOICES, "invoice")
                await _store_upload(web, payments, directory / art.CANONICAL_PAYMENTS, "bank")
        except _UploadRejectedError as exc:
            web.ctx.artifacts.delete(run_id)
            return _refuse(web, request, str(exc), exc.status_code)

        web.ctx.store.create_draft(run_id, label=label)
        return _reconcile_now(web, request, run_id, directory)

    @app.get("/runs/{run_id}", response_class=HTMLResponse, include_in_schema=False)
    def show_run(request: Request, run_id: str) -> Response:
        if (blocked := _guard(request)) is not None:
            return blocked
        row = web.ctx.store.get(run_id) if art.is_run_id(run_id) else None
        if row is None:
            return web.render(request, "missing.html", status_code=404)

        report = web.ctx.store.report_json(run_id)
        return web.render(
            request,
            "run.html",
            {
                "run": row,
                "report": json.loads(report) if report else None,
                "views": _views_for(web, run_id),
                "files": [
                    (name, art.HUMAN_NAMES.get(name, name))
                    for name in web.ctx.artifacts.available(run_id)
                ],
            },
        )

    @app.get("/runs/{run_id}/files/{name}", include_in_schema=False)
    def download(request: Request, run_id: str, name: str) -> Response:
        if (blocked := _guard(request)) is not None:
            return blocked
        path = web.ctx.artifacts.path(run_id, name)
        if path is None:
            return web.render(request, "missing.html", status_code=404)
        web.ctx.store.log_access(
            ip=web.client_ip(request), action=f"download:{name}", run_id=run_id
        )
        return FileResponse(path, filename=name, media_type="application/octet-stream")

    @app.post("/runs/{run_id}/delete", include_in_schema=False)
    def delete_run(
        request: Request, run_id: str, csrf_token: str = Form(default="", alias=CSRF_FIELD)
    ) -> Response:
        if (blocked := _guard(request)) is not None:
            return blocked
        if not web.csrf_ok(request, csrf_token):
            return _refuse(web, request, "That form expired. Please try again.", 403)
        web.ctx.artifacts.delete(run_id)
        web.ctx.store.delete(run_id)
        web.ctx.store.log_access(ip=web.client_ip(request), action="delete", run_id=run_id)
        return redirect("/runs")


# ---- the work ----------------------------------------------------------


def _reconcile_now(web: Web, request: Request, run_id: str, directory: Path) -> Response:
    """Run the engine and record what happened, success or not."""
    from settle_app.app import redirect

    try:
        outcome = execute(
            invoices_path=directory / art.CANONICAL_INVOICES,
            payments_path=directory / art.CANONICAL_PAYMENTS,
            out_dir=directory,
            config=DEFAULT_CONFIG,
            max_rows=web.ctx.settings.max_rows,
        )
    except (LedgerError, TooManyRowsError) as exc:
        web.ctx.store.mark_failed(run_id, error=str(exc))
        return _refuse(web, request, str(exc), 422)
    except InvariantViolation as exc:
        # The engine discarded its own result. Saying so plainly is the only
        # honest option; a reconciliation that broke a money law is not one to
        # show someone a table of.
        message = f"The engine produced an unsafe result and discarded it: {exc}"
        web.ctx.store.mark_failed(run_id, error=message)
        return _refuse(web, request, message, 500)

    report = outcome.result.report
    web.ctx.store.mark_complete(
        run_id,
        engine_version=ENGINE_VERSION,
        report_json=json.dumps(result_to_dict(outcome.result)["report"]),
        invoice_count=len(outcome.invoices),
        payment_count=len(outcome.payments),
        review_count=len(outcome.result.review_queue),
        money_in=str(report.money_in),
    )
    return redirect(f"/runs/{run_id}")


def _views_for(web: Web, run_id: str) -> ResultViews | None:
    """Display rows, read back from the files the run left behind.

    Re-reading is cheap, and it means a results page survives a restart without
    the database holding a second copy of every row.
    """
    directory = web.ctx.artifacts.directory(run_id)
    if directory is None:
        return None
    files = RunFiles(
        invoices=directory / art.CANONICAL_INVOICES,
        payments=directory / art.CANONICAL_PAYMENTS,
        allocations=directory / art.ALLOCATIONS_CSV,
        review_queue=directory / art.REVIEW_QUEUE_CSV,
        invoice_states=directory / art.INVOICE_STATES_CSV,
        result_json=directory / art.RESULT_JSON,
    )
    return build_views(files) if files.all_present() else None


# ---- uploads -----------------------------------------------------------


class _UploadRejectedError(Exception):
    def __init__(self, message: str, *, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


async def _store_upload(web: Web, upload: UploadFile | None, target: Path, label: str) -> None:
    """Stream to disk, refusing anything past the cap as it arrives."""
    if upload is None or not upload.filename:
        raise _UploadRejectedError(f"Choose a {label} CSV.")

    limit = web.ctx.settings.max_upload_bytes
    written = 0
    with target.open("wb") as handle:
        while chunk := await upload.read(UPLOAD_CHUNK):
            written += len(chunk)
            if written > limit:
                raise _UploadRejectedError(
                    f"The {label} file is larger than {_megabytes(web)} MB.", status_code=413
                )
            handle.write(chunk)
    if written == 0:
        raise _UploadRejectedError(f"The {label} file is empty.")


def _install_samples(directory: Path) -> None:
    for source, name in (
        (SAMPLES_DIR / "invoices.csv", art.CANONICAL_INVOICES),
        (SAMPLES_DIR / "bank.csv", art.CANONICAL_PAYMENTS),
    ):
        (directory / name).write_bytes(source.read_bytes())


def _refuse(web: Web, request: Request, message: str, status_code: int) -> Response:
    return web.render(
        request,
        "new.html",
        {
            "error": message,
            "max_rows": web.ctx.settings.max_rows,
            "max_mb": _megabytes(web),
        },
        status_code=status_code,
    )


def _megabytes(web: Web) -> int:
    return max(1, web.ctx.settings.max_upload_bytes // (1024 * 1024))
