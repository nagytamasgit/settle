"""Uploading, mapping, reconciling, reading and deleting a run.

The handlers here are deliberately short. Anything with a decision in it lives
in :mod:`settle_app.runner`, :mod:`settle_app.mapping`, :mod:`settle_app.views`
or the engine, because a route handler is the one place in a web application
where logic reliably goes untested.

The flow is upload, then **confirm the column mapping**, then reconcile. That
middle step is always shown, even when every column matched by alias and there
is nothing to change. The moment a path exists that skips it, the model tier is
one flag away from deciding where somebody's money went unsupervised.
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
from settle_app import mapping as mp
from settle_app.runner import TooManyRowsError, execute
from settle_app.views import ResultViews, RunFiles, build_views

if TYPE_CHECKING:  # pragma: no cover
    from settle_app.app import Web

SAMPLES_DIR: Final = Path(__file__).parent / "samples"
UPLOAD_CHUNK: Final = 64 * 1024


def register(app: FastAPI, web: Web) -> None:
    """Attach the run routes. Called by the app factory."""
    _register_upload(app, web)
    _register_mapping(app, web)
    _register_results(app, web)


def _register_upload(app: FastAPI, web: Web) -> None:
    from settle_app.app import CSRF_FIELD, redirect

    def _guard(request: Request) -> Response | None:
        return None if web.is_signed_in(request) else redirect("/login")

    @app.get("/runs/new", response_class=HTMLResponse, include_in_schema=False)
    def new_run(request: Request) -> Response:
        if (blocked := _guard(request)) is not None:
            return blocked
        return web.render(request, "new.html", _form_context(web))

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
                await _store_upload(web, invoices, directory / art.UPLOAD_INVOICES, "invoice")
                await _store_upload(web, payments, directory / art.UPLOAD_PAYMENTS, "bank")
        except _UploadRejectedError as exc:
            web.ctx.artifacts.delete(run_id)
            return _refuse(web, request, str(exc), exc.status_code)

        web.ctx.store.create_draft(run_id, label=label)
        return redirect(f"/runs/{run_id}/mapping")


def _register_mapping(app: FastAPI, web: Web) -> None:
    from settle_app.app import CSRF_FIELD, redirect

    def _guard(request: Request) -> Response | None:
        return None if web.is_signed_in(request) else redirect("/login")

    @app.get("/runs/{run_id}/mapping", response_class=HTMLResponse, include_in_schema=False)
    def mapping_form(request: Request, run_id: str) -> Response:
        if (blocked := _guard(request)) is not None:
            return blocked
        proposals = _proposals(web, run_id)
        if proposals is None:
            return web.render(request, "missing.html", status_code=404)
        return _render_mapping(web, request, run_id, proposals)

    @app.post("/runs/{run_id}/mapping", include_in_schema=False)
    async def confirm_mapping(request: Request, run_id: str) -> Response:
        if (blocked := _guard(request)) is not None:
            return blocked
        proposals = _proposals(web, run_id)
        if proposals is None:
            return web.render(request, "missing.html", status_code=404)

        form = await request.form()
        if not web.csrf_ok(request, str(form.get(CSRF_FIELD, ""))):
            return _render_mapping(
                web, request, run_id, proposals, error="That form expired.", status_code=403
            )

        confirmed = {
            kind: mp.apply_overrides(proposal, _overrides(form, kind))
            for kind, proposal in proposals.items()
        }
        incomplete = [kind for kind, proposal in confirmed.items() if not proposal.is_complete]
        if incomplete:
            missing = ", ".join(
                f"{kind.value}: {', '.join(confirmed[kind].unresolved_required)}"
                for kind in incomplete
            )
            return _render_mapping(
                web,
                request,
                run_id,
                confirmed,
                error=f"Still needed before this can run — {missing}.",
                status_code=422,
            )

        directory = web.ctx.artifacts.ensure(run_id)
        mp.to_canonical(
            directory / art.UPLOAD_INVOICES,
            directory / art.CANONICAL_INVOICES,
            confirmed[mp.Kind.INVOICE],
        )
        mp.to_canonical(
            directory / art.UPLOAD_PAYMENTS,
            directory / art.CANONICAL_PAYMENTS,
            confirmed[mp.Kind.PAYMENT],
        )
        web.ctx.store.save_mapping(
            run_id,
            [
                (kind.value, choice.target, choice.source, choice.origin.value)
                for kind, proposal in confirmed.items()
                for choice in proposal.choices
                if choice.resolved
            ],
        )
        return _reconcile_now(web, request, run_id, directory, confirmed)


def _register_results(app: FastAPI, web: Web) -> None:
    from settle_app.app import CSRF_FIELD, redirect

    def _guard(request: Request) -> Response | None:
        return None if web.is_signed_in(request) else redirect("/login")

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
                "mapping": web.ctx.store.mapping(run_id),
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


# ---- mapping -----------------------------------------------------------


def _proposals(web: Web, run_id: str) -> dict[mp.Kind, mp.Proposal] | None:
    """Re-derive the proposal from the uploaded headers.

    Deterministic, so recomputing gives the same answer every time and there is
    no half-finished mapping to persist between two requests.
    """
    directory = web.ctx.artifacts.directory(run_id)
    if directory is None:
        return None
    invoices = directory / art.UPLOAD_INVOICES
    payments = directory / art.UPLOAD_PAYMENTS
    if not invoices.is_file() or not payments.is_file():
        return None

    proposals = {
        mp.Kind.INVOICE: mp.propose(mp.read_headers(invoices), mp.Kind.INVOICE),
        mp.Kind.PAYMENT: mp.propose(mp.read_headers(payments), mp.Kind.PAYMENT),
    }
    if web.ctx.settings.model_mapper:
        from settle_app.mapper_model import fill_gaps

        proposals = {
            kind: fill_gaps(proposal, send_samples=web.ctx.settings.model_samples)
            for kind, proposal in proposals.items()
        }
    return proposals


def _overrides(form: object, kind: mp.Kind) -> dict[str, str]:
    """Form fields are named ``invoice.amount`` / ``payment.date``."""
    prefix = f"{kind.value}."
    items = form.items() if hasattr(form, "items") else []
    return {
        str(key)[len(prefix) :]: str(value) for key, value in items if str(key).startswith(prefix)
    }


def _render_mapping(
    web: Web,
    request: Request,
    run_id: str,
    proposals: dict[mp.Kind, mp.Proposal],
    *,
    error: str = "",
    status_code: int = 200,
) -> Response:
    return web.render(
        request,
        "mapping.html",
        {
            "run_id": run_id,
            "error": error,
            "sections": [
                ("Invoice ledger", mp.Kind.INVOICE.value, proposals[mp.Kind.INVOICE]),
                ("Bank statement", mp.Kind.PAYMENT.value, proposals[mp.Kind.PAYMENT]),
            ],
            "model_used": any(
                choice.origin is mp.Origin.MODEL
                for proposal in proposals.values()
                for choice in proposal.choices
            ),
        },
        status_code=status_code,
    )


# ---- the work ----------------------------------------------------------


def _reconcile_now(
    web: Web,
    request: Request,
    run_id: str,
    directory: Path,
    proposals: dict[mp.Kind, mp.Proposal],
) -> Response:
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
        return _render_mapping(web, request, run_id, proposals, error=str(exc), status_code=422)
    except InvariantViolation as exc:
        # The engine discarded its own result. Saying so plainly is the only
        # honest option; a reconciliation that broke a money law is not one to
        # show someone a table of.
        message = f"The engine produced an unsafe result and discarded it: {exc}"
        web.ctx.store.mark_failed(run_id, error=message)
        return _render_mapping(web, request, run_id, proposals, error=message, status_code=500)

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
    """The bundled sample is already canonical, but still goes through mapping.

    Writing it to the upload names rather than the canonical ones keeps the
    sample on exactly the same path as a real file — including the confirmation
    screen, which is the step worth never letting anything skip.
    """
    for source, name in (
        (SAMPLES_DIR / "invoices.csv", art.UPLOAD_INVOICES),
        (SAMPLES_DIR / "bank.csv", art.UPLOAD_PAYMENTS),
    ):
        (directory / name).write_bytes(source.read_bytes())


def _refuse(web: Web, request: Request, message: str, status_code: int) -> Response:
    return web.render(
        request, "new.html", {"error": message, **_form_context(web)}, status_code=status_code
    )


def _form_context(web: Web) -> dict[str, object]:
    return {"max_rows": web.ctx.settings.max_rows, "max_mb": _megabytes(web)}


def _megabytes(web: Web) -> int:
    return max(1, web.ctx.settings.max_upload_bytes // (1024 * 1024))
