"""Serialisation of a run.

:func:`result_to_dict` is the single place a :class:`ReconciliationResult`
becomes JSON. The CLI and the API both call it, which is what makes
``settle run`` and ``POST /reconcile`` produce byte-identical output for
identical input — there is no second implementation to drift.

Money is serialised as a **string**, never a JSON number. A consumer that parses
``1000.10`` as a float reintroduces exactly the error the engine spent its whole
design avoiding, and JSON has no decimal type to prevent it.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from settle.domain.models import (
    Candidate,
    InvoiceState,
    PaymentResult,
    ReconciliationResult,
    ReviewItem,
    RunReport,
)


def result_to_dict(result: ReconciliationResult) -> dict[str, Any]:
    """The canonical, ordered, JSON-safe form of a run."""
    return {
        "report": _report(result.report),
        "payments": [_payment_result(p) for p in result.payments],
        "allocations": [
            {
                "payment_id": a.payment_id,
                "invoice_id": a.invoice_id,
                "amount": str(a.amount),
            }
            for a in result.allocations
        ],
        "fees": [
            {
                "payment_id": f.payment_id,
                "invoice_id": f.invoice_id,
                "amount": str(f.amount),
            }
            for f in result.fees
        ],
        "residuals": [
            {"payment_id": r.payment_id, "amount": str(r.amount)} for r in result.residuals
        ],
        "invoice_states": [_invoice_state(s) for s in result.invoice_states],
        "review_queue": [_review_item(i) for i in result.review_queue],
        "warnings": list(result.warnings),
    }


def _report(report: RunReport) -> dict[str, Any]:
    return {
        "engine_version": report.engine_version,
        "payments": {
            "total": report.payments_total,
            "matched": report.payments_matched,
            "partially_matched": report.payments_partially_matched,
            "in_review": report.payments_in_review,
            "unmatched": report.payments_unmatched,
        },
        "invoices": {
            "total": report.invoices_total,
            "paid": report.invoices_paid,
            "settled_with_fee": report.invoices_settled_with_fee,
            "partially_paid": report.invoices_partially_paid,
            "open": report.invoices_open,
        },
        "money": {
            "in": str(report.money_in),
            "allocated": str(report.money_allocated),
            "residual": str(report.money_residual),
            "fees": str(report.money_fees),
        },
        "search_truncations": report.search_truncations,
        "model_parser_calls": report.model_parser_calls,
    }


def _candidate(candidate: Candidate) -> dict[str, Any]:
    return {
        "strategy": candidate.strategy.value,
        "confidence": str(candidate.confidence),
        "reasons": list(candidate.reasons),
        "invoice_ids": list(candidate.invoice_ids),
        "allocations": [
            {"invoice_id": a.invoice_id, "amount": str(a.amount)} for a in candidate.allocations
        ],
        "fees": [{"invoice_id": f.invoice_id, "amount": str(f.amount)} for f in candidate.fees],
        "residual": str(candidate.residual),
        "reference_matched": sorted(candidate.ref_matched_invoice_ids),
    }


def _payment_result(result: PaymentResult) -> dict[str, Any]:
    return {
        "payment_id": result.payment_id,
        "status": result.status.value,
        "review_reason": result.review_reason.value if result.review_reason else None,
        "search_truncated": result.search_truncated,
        "chosen": _candidate(result.chosen) if result.chosen else None,
        "ranked_candidates": [_candidate(c) for c in result.ranked_candidates],
    }


def _invoice_state(state: InvoiceState) -> dict[str, Any]:
    return {
        "invoice_id": state.invoice_id,
        "status": state.status.value,
        "face_value": str(state.face_value),
        "allocated": str(state.allocated),
        "fee_written_off": str(state.fee_written_off),
        "open_balance": str(state.open_balance),
    }


def _review_item(item: ReviewItem) -> dict[str, Any]:
    return {
        "payment_id": item.payment_id,
        "reason": item.reason.value,
        "message": item.message,
        "candidates": [_candidate(c) for c in item.candidates],
    }


def _default(value: Any) -> str:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"{type(value).__name__} is not JSON-serialisable")


def dumps(result: ReconciliationResult, *, indent: int | None = 2) -> str:
    return json.dumps(result_to_dict(result), indent=indent, default=_default, sort_keys=False)


def write_json(path: Path, result: ReconciliationResult, *, indent: int | None = 2) -> None:
    path.write_text(dumps(result, indent=indent) + "\n", encoding="utf-8")
