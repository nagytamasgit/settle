"""CSV in, CSV out. The CSV contract is the integration point.

There is deliberately no bank or ERP adapter in this repository. Two CSVs and a
schema are a contract any finance system can already produce, and they keep the
engine testable without a single credential. Adapters are a later concern, and
that decision is recorded in ``docs/DECISIONS.md``.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Sequence
from pathlib import Path

from pydantic import ValidationError

from settle.domain.errors import LedgerError
from settle.domain.models import Invoice, Payment, ReconciliationResult

from .schemas import InvoiceRow, PaymentRow

INVOICE_COLUMNS = (
    "id",
    "customer_id",
    "customer_name",
    "number",
    "issue_date",
    "due_date",
    "amount",
    "currency",
)

PAYMENT_COLUMNS = (
    "id",
    "date",
    "amount",
    "currency",
    "reference",
    "counterparty",
    "customer_id",
)


def read_invoices(path: Path) -> list[Invoice]:
    """Load the invoice ledger, reporting the offending line on bad input."""
    return [row.to_domain() for row in _read(path, InvoiceRow, "invoice")]


def read_payments(path: Path) -> list[Payment]:
    """Load the bank statement, reporting the offending line on bad input."""
    return [row.to_domain() for row in _read(path, PaymentRow, "bank line")]


def _read(path: Path, model: type[InvoiceRow] | type[PaymentRow], label: str) -> list:  # type: ignore[type-arg]
    if not path.exists():
        raise LedgerError(f"{label} file not found: {path}")
    rows = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise LedgerError(f"{path} is empty; a header row is required")
        for line_number, raw in enumerate(reader, start=2):
            cleaned = {k: v for k, v in raw.items() if k is not None}
            try:
                rows.append(model.model_validate(cleaned))
            except ValidationError as exc:
                raise LedgerError(
                    f"{path}:{line_number}: invalid {label} row\n{_explain(exc)}"
                ) from exc
            except ValueError as exc:
                raise LedgerError(f"{path}:{line_number}: invalid {label} row: {exc}") from exc
    return rows


def _explain(error: ValidationError) -> str:
    lines = []
    for problem in error.errors():
        field = ".".join(str(part) for part in problem["loc"]) or "<row>"
        lines.append(f"  {field}: {problem['msg']}")
    return "\n".join(lines)


def write_invoices(path: Path, invoices: Iterable[Invoice]) -> None:
    """Write a ledger in the format :func:`read_invoices` accepts."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(INVOICE_COLUMNS)
        for invoice in invoices:
            writer.writerow(
                [
                    invoice.id,
                    invoice.customer_id,
                    invoice.customer_name,
                    invoice.number,
                    invoice.issue_date.isoformat(),
                    invoice.due_date.isoformat() if invoice.due_date else "",
                    str(invoice.amount),
                    invoice.currency,
                ]
            )


def write_payments(path: Path, payments: Iterable[Payment]) -> None:
    """Write a statement in the format :func:`read_payments` accepts."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(PAYMENT_COLUMNS)
        for payment in payments:
            writer.writerow(
                [
                    payment.id,
                    payment.date.isoformat(),
                    str(payment.amount),
                    payment.currency,
                    payment.reference,
                    payment.counterparty,
                    payment.customer_id or "",
                ]
            )


def write_allocations(path: Path, result: ReconciliationResult) -> None:
    """One row per allocation, with the reason it was made.

    This is the file a finance team actually opens, so the reason travels with
    the number rather than living in a log nobody reads.
    """
    strategy: dict[str, str] = {}
    confidence: dict[str, str] = {}
    reason: dict[str, str] = {}
    for payment_result in result.payments:
        if payment_result.chosen is None:
            continue
        strategy[payment_result.payment_id] = payment_result.chosen.strategy.value
        confidence[payment_result.payment_id] = str(payment_result.chosen.confidence)
        reason[payment_result.payment_id] = "; ".join(payment_result.chosen.reasons)

    fees: dict[tuple[str, str], str] = {
        (fee.payment_id, fee.invoice_id): str(fee.amount) for fee in result.fees
    }

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["payment_id", "invoice_id", "amount", "fee", "strategy", "confidence", "reason"]
        )
        for allocation in result.allocations:
            key = (allocation.payment_id, allocation.invoice_id)
            writer.writerow(
                [
                    allocation.payment_id,
                    allocation.invoice_id,
                    str(allocation.amount),
                    fees.get(key, ""),
                    strategy.get(allocation.payment_id, ""),
                    confidence.get(allocation.payment_id, ""),
                    reason.get(allocation.payment_id, ""),
                ]
            )


def write_review_queue(path: Path, result: ReconciliationResult) -> None:
    """The work list for a human, ordered as the engine ranked it."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["payment_id", "reason", "message", "best_candidate", "best_confidence", "options"]
        )
        for item in result.review_queue:
            best = item.candidates[0] if item.candidates else None
            writer.writerow(
                [
                    item.payment_id,
                    item.reason.value,
                    item.message,
                    " + ".join(best.invoice_ids) if best else "",
                    str(best.confidence) if best else "",
                    len(item.candidates),
                ]
            )


def write_invoice_states(path: Path, result: ReconciliationResult) -> None:
    """Closing position of every invoice after the run."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["invoice_id", "status", "face_value", "allocated", "fee_written_off", "open_balance"]
        )
        for state in result.invoice_states:
            writer.writerow(
                [
                    state.invoice_id,
                    state.status.value,
                    str(state.face_value),
                    str(state.allocated),
                    str(state.fee_written_off),
                    str(state.open_balance),
                ]
            )


def write_all(directory: Path, result: ReconciliationResult) -> Sequence[Path]:
    """Write every output file for a run and return what was written."""
    directory.mkdir(parents=True, exist_ok=True)
    from .json_io import write_json

    written = [
        directory / "result.json",
        directory / "allocations.csv",
        directory / "review_queue.csv",
        directory / "invoice_states.csv",
    ]
    write_json(written[0], result)
    write_allocations(written[1], result)
    write_review_queue(written[2], result)
    write_invoice_states(written[3], result)
    return written
