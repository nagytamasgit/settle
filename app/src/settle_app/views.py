"""Turning the files a run produced into something a person can read.

Two design points, and they are the same point twice.

First, :class:`~settle.domain.models.ReconciliationResult` carries ids and
nothing else — no customer names, no invoice numbers, no dates, no references.
That is right for the engine, which has no business remembering what its inputs
looked like, and it is why this module exists: a table of ``p17 → i93`` tells a
finance team nothing. So the uploaded invoices and payments are indexed once and
every row is joined against those dictionaries.

Second, the rows come from the CSVs the engine itself wrote, not from a
rehydrated result object. That keeps the promise the results page makes: what
you see on screen is the file you can download, because it *is* the file you can
download. It also means there is no second deserialiser to drift from
``json_io``, and a page still renders after a restart without the database
holding a second copy of every row.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from settle.domain.models import Invoice, Payment
from settle.io import csv_io

#: How many rows a table shows before deferring to the download. A big run
#: should stay a readable page rather than a 40 MB one; the CSV has everything.
DEFAULT_ROW_LIMIT: Final = 250


@dataclass(frozen=True, slots=True)
class AllocationView:
    """One allocation, with enough context to recognise it."""

    payment_id: str
    payment_date: str
    reference: str
    counterparty: str
    invoice_id: str
    invoice_number: str
    customer_name: str
    amount: str
    fee: str
    strategy: str
    confidence: str
    reason: str


@dataclass(frozen=True, slots=True)
class ReviewView:
    """One payment the engine declined to decide, and what it would need."""

    payment_id: str
    payment_date: str
    amount: str
    reference: str
    counterparty: str
    reason: str
    message: str
    options: str
    best_candidate: str
    best_confidence: str


@dataclass(frozen=True, slots=True)
class InvoiceStateView:
    """Where an invoice stands after the run."""

    invoice_id: str
    number: str
    customer_name: str
    status: str
    face_value: str
    allocated: str
    fee_written_off: str
    open_balance: str


@dataclass(frozen=True, slots=True)
class Table[Row]:
    """Rows, plus how many were left off them."""

    rows: tuple[Row, ...]
    total: int

    @property
    def truncated(self) -> bool:
        return len(self.rows) < self.total

    @property
    def omitted(self) -> int:
        return self.total - len(self.rows)


@dataclass(frozen=True, slots=True)
class ResultViews:
    allocations: Table[AllocationView]
    review_queue: Table[ReviewView]
    invoice_states: Table[InvoiceStateView]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RunFiles:
    """Where a run's artefacts are. All six are written by one run."""

    invoices: Path
    payments: Path
    allocations: Path
    review_queue: Path
    invoice_states: Path
    result_json: Path

    def all_present(self) -> bool:
        return all(
            path.is_file()
            for path in (
                self.invoices,
                self.payments,
                self.allocations,
                self.review_queue,
                self.invoice_states,
                self.result_json,
            )
        )


def build_views(files: RunFiles, *, limit: int = DEFAULT_ROW_LIMIT) -> ResultViews:
    """Join a run's outputs against its inputs, once, into display rows."""
    invoices = {invoice.id: invoice for invoice in csv_io.read_invoices(files.invoices)}
    payments = {payment.id: payment for payment in csv_io.read_payments(files.payments)}

    allocations, allocation_total = _rows(files.allocations, limit)
    review, review_total = _rows(files.review_queue, limit)
    states, states_total = _rows(files.invoice_states, limit)

    return ResultViews(
        allocations=Table(
            tuple(_allocation(row, invoices, payments) for row in allocations), allocation_total
        ),
        review_queue=Table(tuple(_review(row, payments) for row in review), review_total),
        invoice_states=Table(tuple(_state(row, invoices) for row in states), states_total),
        warnings=_warnings(files.result_json),
    )


def _rows(path: Path, limit: int) -> tuple[list[dict[str, str]], int]:
    """The first ``limit`` rows, and how many there were in total.

    The whole file is walked so the count is honest; only the kept rows are
    materialised, so a large file costs a scan rather than a heap of dicts.
    """
    kept: list[dict[str, str]] = []
    total = 0
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            total += 1
            if total <= limit:
                kept.append(row)
    return kept, total


def _allocation(
    row: dict[str, str], invoices: dict[str, Invoice], payments: dict[str, Payment]
) -> AllocationView:
    payment = payments.get(row.get("payment_id", ""))
    invoice = invoices.get(row.get("invoice_id", ""))
    return AllocationView(
        payment_id=row.get("payment_id", ""),
        payment_date=payment.date.isoformat() if payment else "",
        reference=payment.reference if payment else "",
        counterparty=payment.counterparty if payment else "",
        invoice_id=row.get("invoice_id", ""),
        invoice_number=invoice.number if invoice else "",
        customer_name=invoice.customer_name if invoice else "",
        amount=row.get("amount", ""),
        fee=row.get("fee", ""),
        strategy=row.get("strategy", ""),
        confidence=row.get("confidence", ""),
        reason=row.get("reason", ""),
    )


def _review(row: dict[str, str], payments: dict[str, Payment]) -> ReviewView:
    payment = payments.get(row.get("payment_id", ""))
    return ReviewView(
        payment_id=row.get("payment_id", ""),
        payment_date=payment.date.isoformat() if payment else "",
        amount=str(payment.amount) if payment else "",
        reference=payment.reference if payment else "",
        counterparty=payment.counterparty if payment else "",
        reason=row.get("reason", ""),
        message=row.get("message", ""),
        options=row.get("options", ""),
        best_candidate=row.get("best_candidate", ""),
        best_confidence=row.get("best_confidence", ""),
    )


def _state(row: dict[str, str], invoices: dict[str, Invoice]) -> InvoiceStateView:
    invoice = invoices.get(row.get("invoice_id", ""))
    return InvoiceStateView(
        invoice_id=row.get("invoice_id", ""),
        number=invoice.number if invoice else "",
        customer_name=invoice.customer_name if invoice else "",
        status=row.get("status", ""),
        face_value=row.get("face_value", ""),
        allocated=row.get("allocated", ""),
        fee_written_off=row.get("fee_written_off", ""),
        open_balance=row.get("open_balance", ""),
    )


def _warnings(result_json: Path) -> tuple[str, ...]:
    """Warnings live only in the JSON; reading one key is not a rehydration."""
    try:
        document = json.loads(result_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):  # pragma: no cover - defensive
        return ()
    raw = document.get("warnings", [])
    return tuple(str(item) for item in raw) if isinstance(raw, list) else ()
