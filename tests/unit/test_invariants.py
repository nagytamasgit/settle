"""The safety net, tested by breaking things on purpose.

Every other test shows the engine producing correct results. These show that if
it ever stopped, the invariant checker would notice — which is the only reason
it is safe to run ``verify_invariants`` in production.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from settle.domain.errors import InvariantViolation
from settle.domain.invariants import check_result
from settle.domain.models import (
    Allocation,
    Fee,
    InvoiceState,
    InvoiceStatus,
    PaymentResult,
    PaymentStatus,
    ReconciliationResult,
    Residual,
    RunReport,
)
from tests.conftest import inv, pay

INVOICES = [inv("i1", "1000.00")]
PAYMENTS = [pay("p1", "1000.00")]

EMPTY_REPORT = RunReport(
    engine_version="test",
    payments_total=1,
    payments_matched=1,
    payments_partially_matched=0,
    payments_in_review=0,
    payments_unmatched=0,
    invoices_total=1,
    invoices_paid=1,
    invoices_settled_with_fee=0,
    invoices_partially_paid=0,
    invoices_open=0,
    money_in=Decimal("1000.00"),
    money_allocated=Decimal("1000.00"),
    money_residual=Decimal("0.00"),
    money_fees=Decimal("0.00"),
    search_truncations=0,
)


def build(
    *,
    allocations=(),
    fees=(),
    residuals=(),
    states=(),
) -> ReconciliationResult:
    return ReconciliationResult(
        payments=(PaymentResult(payment_id="p1", status=PaymentStatus.MATCHED),),
        allocations=tuple(allocations),
        fees=tuple(fees),
        residuals=tuple(residuals),
        invoice_states=tuple(states),
        review_queue=(),
        report=EMPTY_REPORT,
    )


def paid_state(allocated: str = "1000.00") -> InvoiceState:
    return InvoiceState(
        invoice_id="i1",
        status=InvoiceStatus.PAID,
        face_value=Decimal("1000.00"),
        allocated=Decimal(allocated),
        fee_written_off=Decimal("0.00"),
        open_balance=Decimal("1000.00") - Decimal(allocated),
    )


def test_a_correct_result_passes() -> None:
    check_result(
        INVOICES,
        PAYMENTS,
        build(
            allocations=[Allocation("p1", "i1", Decimal("1000.00"))],
            states=[paid_state()],
        ),
    )


def test_money_vanishing_is_caught() -> None:
    """900 allocated out of a 1000 payment, with no residual for the rest."""
    with pytest.raises(InvariantViolation, match=r"but 1000\.00 arrived"):
        check_result(
            INVOICES,
            PAYMENTS,
            build(
                allocations=[Allocation("p1", "i1", Decimal("900.00"))],
                states=[paid_state("900.00")],
            ),
        )


def test_money_appearing_from_nowhere_is_caught() -> None:
    with pytest.raises(InvariantViolation, match="exceeds face value"):
        check_result(
            INVOICES,
            PAYMENTS,
            build(
                allocations=[Allocation("p1", "i1", Decimal("1500.00"))],
                residuals=[],
                states=[paid_state("1500.00")],
            ),
        )


def test_an_invoice_paid_twice_is_caught() -> None:
    """The classic double-allocation: two payments, one invoice, no bound check."""
    payments = [pay("p1", "1000.00"), pay("p2", "1000.00", day=21)]
    with pytest.raises(InvariantViolation, match="exceeds face value"):
        check_result(
            INVOICES,
            payments,
            build(
                allocations=[
                    Allocation("p1", "i1", Decimal("1000.00")),
                    Allocation("p2", "i1", Decimal("1000.00")),
                ],
                states=[paid_state()],
            ),
        )


def test_a_fee_that_does_not_close_the_invoice_is_caught() -> None:
    with pytest.raises(InvariantViolation, match="settled_with_fee but"):
        check_result(
            INVOICES,
            PAYMENTS,
            build(
                allocations=[Allocation("p1", "i1", Decimal("1000.00"))],
                states=[
                    InvoiceState(
                        invoice_id="i1",
                        status=InvoiceStatus.SETTLED_WITH_FEE,
                        face_value=Decimal("1000.00"),
                        allocated=Decimal("1000.00"),
                        fee_written_off=Decimal("0.00"),
                        open_balance=Decimal("0.00"),
                    )
                ],
            ),
        )


def test_a_paid_status_that_is_not_fully_paid_is_caught() -> None:
    with pytest.raises(InvariantViolation, match="status paid but allocated"):
        check_result(
            INVOICES,
            PAYMENTS,
            build(
                allocations=[Allocation("p1", "i1", Decimal("400.00"))],
                residuals=[Residual("p1", Decimal("600.00"))],
                states=[
                    InvoiceState(
                        invoice_id="i1",
                        status=InvoiceStatus.PAID,
                        face_value=Decimal("1000.00"),
                        allocated=Decimal("400.00"),
                        fee_written_off=Decimal("0.00"),
                        open_balance=Decimal("600.00"),
                    )
                ],
            ),
        )


def test_an_open_status_with_money_on_it_is_caught() -> None:
    with pytest.raises(InvariantViolation, match="status open but allocated"):
        check_result(
            INVOICES,
            PAYMENTS,
            build(
                allocations=[Allocation("p1", "i1", Decimal("1000.00"))],
                states=[
                    InvoiceState(
                        invoice_id="i1",
                        status=InvoiceStatus.OPEN,
                        face_value=Decimal("1000.00"),
                        allocated=Decimal("1000.00"),
                        fee_written_off=Decimal("0.00"),
                        open_balance=Decimal("0.00"),
                    )
                ],
            ),
        )


def test_a_partially_paid_status_with_nothing_paid_is_caught() -> None:
    with pytest.raises(InvariantViolation, match="status partially_paid"):
        check_result(
            INVOICES,
            PAYMENTS,
            build(
                residuals=[Residual("p1", Decimal("1000.00"))],
                states=[
                    InvoiceState(
                        invoice_id="i1",
                        status=InvoiceStatus.PARTIALLY_PAID,
                        face_value=Decimal("1000.00"),
                        allocated=Decimal("0.00"),
                        fee_written_off=Decimal("0.00"),
                        open_balance=Decimal("1000.00"),
                    )
                ],
            ),
        )


def test_a_reference_to_an_unknown_invoice_is_caught() -> None:
    with pytest.raises(InvariantViolation, match="unknown invoice"):
        check_result(
            INVOICES,
            PAYMENTS,
            build(
                allocations=[Allocation("p1", "ghost", Decimal("1000.00"))],
                states=[
                    InvoiceState(
                        invoice_id="i1",
                        status=InvoiceStatus.OPEN,
                        face_value=Decimal("1000.00"),
                        allocated=Decimal("0.00"),
                        fee_written_off=Decimal("0.00"),
                        open_balance=Decimal("1000.00"),
                    )
                ],
            ),
        )


def test_a_missing_invoice_state_is_caught() -> None:
    with pytest.raises(InvariantViolation, match="no state reported"):
        check_result(
            INVOICES,
            PAYMENTS,
            build(residuals=[Residual("p1", Decimal("1000.00"))]),
        )


def test_a_cross_currency_allocation_is_caught() -> None:
    invoices = [inv("i1", "1000.00", currency="HUF")]
    with pytest.raises(InvariantViolation, match="crosses currencies"):
        check_result(
            invoices,
            PAYMENTS,
            build(
                allocations=[Allocation("p1", "i1", Decimal("1000.00"))],
                states=[paid_state()],
            ),
        )


def test_every_violation_is_reported_not_just_the_first() -> None:
    """When something is wrong, the whole picture beats the first broken cent."""
    with pytest.raises(InvariantViolation) as caught:
        check_result(
            INVOICES,
            PAYMENTS,
            build(
                allocations=[Allocation("p1", "ghost", Decimal("400.00"))],
                fees=[Fee("p1", "ghost", Decimal("10.00"))],
            ),
        )

    message = str(caught.value)
    assert message.startswith("5 invariant violation(s)")
    assert "allocation cites unknown invoice" in message
    assert "fee cites unknown invoice" in message
    assert "no state reported" in message
    assert "run total" in message
