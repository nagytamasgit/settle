"""FastAPI wrapper. A thin one, on purpose.

The endpoint validates, calls :func:`~settle.domain.match.reconcile`, and
serialises with the same function the CLI uses. There is no matching logic in
this file and there should never be any: if a rule lived here, the CLI and the
API could disagree, and the milestone this project set itself is that they
cannot.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException

from settle.domain.errors import InvariantViolation, LedgerError
from settle.domain.match import ENGINE_VERSION, reconcile
from settle.io.json_io import result_to_dict
from settle.io.schemas import ReconcileRequest

app = FastAPI(
    title="settle",
    version=ENGINE_VERSION,
    summary="Match incoming payments to invoices, or route them to a human.",
    description=(
        "Reconciliation is a logic problem with a messy edge. This service solves "
        "the logic deterministically and routes anything it cannot explain "
        "confidently to a review queue rather than guessing."
    ),
)


@app.get("/health", tags=["ops"])
def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok", "engine_version": ENGINE_VERSION}


@app.post("/reconcile", tags=["reconciliation"])
def post_reconcile(request: ReconcileRequest) -> dict[str, Any]:
    """Reconcile a statement against a ledger.

    Returns the same document ``settle run`` writes to ``result.json``.

    Raises:
        HTTPException: 422 when the ledger or statement is unusable (duplicate
            ids, bad amounts); 500 when a money invariant would be violated,
            because in that case the only safe answer is no answer.
    """
    try:
        result = reconcile(
            [row.to_domain() for row in request.invoices],
            [row.to_domain() for row in request.payments],
            config=request.config.to_domain(),
        )
    except LedgerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except InvariantViolation as exc:
        raise HTTPException(
            status_code=500,
            detail=f"the engine produced an unsafe result and discarded it: {exc}",
        ) from exc
    return result_to_dict(result)
