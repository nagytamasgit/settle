"""The money laws, checked in tests *and* at runtime.

There are two conservation laws, not one, and the distinction is the whole
reason fees are modelled separately:

**Payment side.** Every cent that arrived is either allocated to an invoice or
left as residual::

    sum(allocations) + sum(residuals) == sum(payments)

**Invoice side.** Nothing is ever allocated beyond an invoice's face value, and
a closed invoice is exactly explained by what was allocated plus what a fee ate::

    allocated(i) + fee(i) + open_balance(i) == face_value(i)

A fee is money that never arrived, so it appears only in the second law. The
one-line version of this in the README ("allocations plus fees plus residual
equals payments") is the informal statement; these two laws are the precise one.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from .errors import InvariantViolation
from .models import (
    Invoice,
    InvoiceState,
    InvoiceStatus,
    Payment,
    ReconciliationResult,
)
from .money import ZERO


def _sum(amounts: Sequence[Decimal]) -> Decimal:
    return sum(amounts, ZERO)


def check_result(
    invoices: Sequence[Invoice],
    payments: Sequence[Payment],
    result: ReconciliationResult,
) -> None:
    """Verify every money invariant, or raise with the full list of breaks.

    All violations are collected before raising: when something is wrong it is
    far more useful to see the whole picture than the first broken cent.
    """
    problems: list[str] = []
    invoices_by_id = {inv.id: inv for inv in invoices}
    payments_by_id = {p.id: p for p in payments}

    problems += _check_references(result, invoices_by_id, payments_by_id)
    problems += _check_payment_conservation(result, payments_by_id)
    problems += _check_invoice_bounds(result, invoices_by_id)
    problems += _check_invoice_states(result, invoices_by_id)
    problems += _check_global_conservation(result, payments)
    problems += _check_currency_alignment(result, invoices_by_id, payments_by_id)

    if problems:
        raise InvariantViolation(
            f"{len(problems)} invariant violation(s):\n  - " + "\n  - ".join(problems)
        )


def _check_references(
    result: ReconciliationResult,
    invoices_by_id: dict[str, Invoice],
    payments_by_id: dict[str, Payment],
) -> list[str]:
    problems: list[str] = []
    for alloc in result.allocations:
        if alloc.payment_id not in payments_by_id:
            problems.append(f"allocation cites unknown payment {alloc.payment_id!r}")
        if alloc.invoice_id not in invoices_by_id:
            problems.append(f"allocation cites unknown invoice {alloc.invoice_id!r}")
    for fee in result.fees:
        if fee.invoice_id not in invoices_by_id:
            problems.append(f"fee cites unknown invoice {fee.invoice_id!r}")
    for residual in result.residuals:
        if residual.payment_id not in payments_by_id:
            problems.append(f"residual cites unknown payment {residual.payment_id!r}")
    return problems


def _check_payment_conservation(
    result: ReconciliationResult,
    payments_by_id: dict[str, Payment],
) -> list[str]:
    problems: list[str] = []
    allocated: dict[str, Decimal] = {}
    for alloc in result.allocations:
        allocated[alloc.payment_id] = allocated.get(alloc.payment_id, ZERO) + alloc.amount
    residual: dict[str, Decimal] = {}
    for res in result.residuals:
        residual[res.payment_id] = residual.get(res.payment_id, ZERO) + res.amount

    for payment_id, payment in payments_by_id.items():
        used = allocated.get(payment_id, ZERO) + residual.get(payment_id, ZERO)
        if used != payment.amount:
            problems.append(
                f"payment {payment_id}: allocated {allocated.get(payment_id, ZERO)} "
                f"+ residual {residual.get(payment_id, ZERO)} = {used}, "
                f"but {payment.amount} arrived"
            )
    return problems


def _check_invoice_bounds(
    result: ReconciliationResult,
    invoices_by_id: dict[str, Invoice],
) -> list[str]:
    problems: list[str] = []
    allocated: dict[str, Decimal] = {}
    for alloc in result.allocations:
        allocated[alloc.invoice_id] = allocated.get(alloc.invoice_id, ZERO) + alloc.amount
    fees: dict[str, Decimal] = {}
    for fee in result.fees:
        fees[fee.invoice_id] = fees.get(fee.invoice_id, ZERO) + fee.amount

    for invoice_id, invoice in invoices_by_id.items():
        claimed = allocated.get(invoice_id, ZERO) + fees.get(invoice_id, ZERO)
        if claimed > invoice.amount:
            problems.append(
                f"invoice {invoice_id}: allocated {allocated.get(invoice_id, ZERO)} "
                f"+ fees {fees.get(invoice_id, ZERO)} = {claimed} "
                f"exceeds face value {invoice.amount}"
            )
    return problems


def _check_invoice_states(
    result: ReconciliationResult,
    invoices_by_id: dict[str, Invoice],
) -> list[str]:
    problems: list[str] = []
    seen: set[str] = set()
    for state in result.invoice_states:
        seen.add(state.invoice_id)
        invoice = invoices_by_id.get(state.invoice_id)
        if invoice is None:
            problems.append(f"state reported for unknown invoice {state.invoice_id!r}")
            continue
        accounted = state.allocated + state.fee_written_off + state.open_balance
        if accounted != invoice.amount:
            problems.append(
                f"invoice {state.invoice_id}: allocated {state.allocated} "
                f"+ fee {state.fee_written_off} + open {state.open_balance} = {accounted}, "
                f"face value is {invoice.amount}"
            )
        if state.open_balance < ZERO:
            problems.append(f"invoice {state.invoice_id}: negative open balance")
        problems += _check_status_consistency(state, invoice.amount)

    missing = set(invoices_by_id) - seen
    if missing:
        problems.append(f"no state reported for invoices: {sorted(missing)}")
    return problems


def _check_status_consistency(state: InvoiceState, face_value: Decimal) -> list[str]:
    problems: list[str] = []
    match state.status:
        case InvoiceStatus.PAID:
            if state.allocated != face_value or state.fee_written_off != ZERO:
                problems.append(
                    f"invoice {state.invoice_id}: status paid but allocated "
                    f"{state.allocated} of {face_value} with fee {state.fee_written_off}"
                )
        case InvoiceStatus.SETTLED_WITH_FEE:
            if state.fee_written_off <= ZERO:
                problems.append(f"invoice {state.invoice_id}: settled_with_fee but no fee recorded")
            if state.open_balance != ZERO:
                problems.append(
                    f"invoice {state.invoice_id}: settled_with_fee but {state.open_balance} open"
                )
        case InvoiceStatus.PARTIALLY_PAID:
            if not (ZERO < state.allocated < face_value):
                problems.append(
                    f"invoice {state.invoice_id}: status partially_paid but allocated "
                    f"{state.allocated} of {face_value}"
                )
        case InvoiceStatus.OPEN:
            if state.allocated != ZERO or state.fee_written_off != ZERO:
                problems.append(
                    f"invoice {state.invoice_id}: status open but allocated {state.allocated}"
                )
    return problems


def _check_global_conservation(
    result: ReconciliationResult,
    payments: Sequence[Payment],
) -> list[str]:
    money_in = _sum([p.amount for p in payments])
    allocated = _sum([a.amount for a in result.allocations])
    residual = _sum([r.amount for r in result.residuals])
    if allocated + residual != money_in:
        return [
            f"run total: allocated {allocated} + residual {residual} = {allocated + residual}, "
            f"but {money_in} arrived"
        ]
    return []


def _check_currency_alignment(
    result: ReconciliationResult,
    invoices_by_id: dict[str, Invoice],
    payments_by_id: dict[str, Payment],
) -> list[str]:
    problems: list[str] = []
    for alloc in result.allocations:
        invoice = invoices_by_id.get(alloc.invoice_id)
        payment = payments_by_id.get(alloc.payment_id)
        if invoice is None or payment is None:
            continue
        if invoice.currency != payment.currency:
            problems.append(
                f"allocation {alloc.payment_id}->{alloc.invoice_id} crosses currencies "
                f"{payment.currency}->{invoice.currency}; FX is out of scope"
            )
    return problems
