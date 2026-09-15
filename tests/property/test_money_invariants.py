"""The money laws, as properties rather than examples.

These are the tests the project exists to be able to write. They do not check
that the matcher is *clever*; they check that whatever it decides, no cent is
created, destroyed, or spent twice, on ledgers the author never thought of.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from settle.domain.config import AmbiguityPolicy, MatchConfig, TieBreak
from settle.domain.match import reconcile
from settle.domain.models import Invoice, Payment
from settle.domain.money import ZERO

amounts = st.decimals(
    min_value=Decimal("0.01"), max_value=Decimal("50000.00"), places=2, allow_nan=False
)
day_offsets = st.integers(min_value=0, max_value=60)
customers = st.sampled_from(["c1", "c2", "c3"])
currencies = st.sampled_from(["EUR", "HUF"])


@st.composite
def ledgers(draw: st.DrawFn, max_invoices: int = 8) -> list[Invoice]:
    rows = draw(
        st.lists(
            st.tuples(amounts, day_offsets, customers, currencies),
            min_size=0,
            max_size=max_invoices,
        )
    )
    return [
        Invoice(
            id=f"i{index}",
            customer_id=customer,
            number=f"INV-2026-{index:04d}",
            issue_date=date(2026, 1, 1) + timedelta(days=offset),
            amount=amount,
            currency=currency,
            customer_name=f"Customer {customer}",
        )
        for index, (amount, offset, customer, currency) in enumerate(rows)
    ]


@st.composite
def statements(draw: st.DrawFn, ledger: list[Invoice], max_payments: int = 6) -> list[Payment]:
    """Payments drawn from shapes that actually occur: exact, sums, partials, junk."""
    count = draw(st.integers(min_value=0, max_value=max_payments))
    payments: list[Payment] = []
    for index in range(count):
        if ledger and draw(st.booleans()):
            picked = draw(st.lists(st.sampled_from(ledger), min_size=1, max_size=3, unique=True))
            amount = sum((i.amount for i in picked), ZERO)
            currency = picked[0].currency
            counterparty = picked[0].customer_name
            reference = draw(
                st.sampled_from(
                    [
                        " ".join(i.number for i in picked),
                        f"szamla {picked[0].number.split('-')[-1].lstrip('0') or '0'}",
                        "",
                        "thanks",
                    ]
                )
            )
            shave = draw(st.sampled_from([ZERO, Decimal("0.01"), amount / 3]))
            amount = (amount - shave).quantize(Decimal("0.01"))
            if amount <= ZERO:
                continue
        else:
            amount = draw(amounts)
            currency = draw(currencies)
            counterparty = draw(st.sampled_from(["Customer c1", "unknown ltd", ""]))
            reference = draw(st.sampled_from(["", "INV-2026-0001", "rent", "#3"]))
        payments.append(
            Payment(
                id=f"p{index}",
                date=date(2026, 3, 1) + timedelta(days=draw(day_offsets)),
                amount=amount,
                currency=currency,
                reference=reference,
                counterparty=counterparty,
            )
        )
    return payments


@st.composite
def scenarios(draw: st.DrawFn) -> tuple[list[Invoice], list[Payment]]:
    ledger = draw(ledgers())
    return ledger, draw(statements(ledger))


configs = st.builds(
    MatchConfig,
    fee_tolerance_pct=st.sampled_from([Decimal("0"), Decimal("0.01"), Decimal("0.03")]),
    confidence_threshold=st.sampled_from([Decimal("0.50"), Decimal("0.80"), Decimal("0.95")]),
    max_combination_size=st.integers(min_value=2, max_value=4),
    tie_break=st.sampled_from(list(TieBreak)),
    ambiguity_policy=st.sampled_from(list(AmbiguityPolicy)),
)


@given(scenario=scenarios(), config=configs)
def test_money_is_conserved(
    scenario: tuple[list[Invoice], list[Payment]], config: MatchConfig
) -> None:
    """Every cent that arrived is allocated or left as residual. Nothing evaporates.

    ``reconcile`` also asserts this at runtime, so this test is really asking
    whether any input can drive the engine into a state where it would have to.
    """
    invoices, payments = scenario
    result = reconcile(invoices, payments, config=config)

    allocated = sum((a.amount for a in result.allocations), ZERO)
    residual = sum((r.amount for r in result.residuals), ZERO)
    assert allocated + residual == sum((p.amount for p in payments), ZERO)


@given(scenario=scenarios(), config=configs)
def test_no_invoice_is_over_allocated(
    scenario: tuple[list[Invoice], list[Payment]], config: MatchConfig
) -> None:
    """An invoice never receives more than its face value, fees included."""
    invoices, payments = scenario
    result = reconcile(invoices, payments, config=config)

    for state in result.invoice_states:
        face = next(i.amount for i in invoices if i.id == state.invoice_id)
        assert state.allocated + state.fee_written_off <= face
        assert state.open_balance >= ZERO
        assert state.allocated + state.fee_written_off + state.open_balance == face


@given(scenario=scenarios(), config=configs)
def test_reconciliation_is_deterministic(
    scenario: tuple[list[Invoice], list[Payment]], config: MatchConfig
) -> None:
    """The same input yields the same output, byte for byte."""
    invoices, payments = scenario
    assert reconcile(invoices, payments, config=config) == reconcile(
        invoices, payments, config=config
    )


@given(scenario=scenarios(), config=configs, seed=st.integers())
def test_input_order_does_not_change_the_answer(
    scenario: tuple[list[Invoice], list[Payment]], config: MatchConfig, seed: int
) -> None:
    """Shuffling the CSV rows must not move a cent.

    Payments are processed in (date, id) order regardless of how they arrived,
    which is what makes a re-run after a re-export reproduce the same result.
    """
    invoices, payments = scenario
    rotate = seed % (len(payments) + 1) if payments else 0
    shuffled_payments = payments[rotate:] + payments[:rotate]
    shuffled_invoices = list(reversed(invoices))

    assert reconcile(shuffled_invoices, shuffled_payments, config=config) == reconcile(
        invoices, payments, config=config
    )


@given(scenario=scenarios(), config=configs)
def test_inputs_are_not_mutated(
    scenario: tuple[list[Invoice], list[Payment]], config: MatchConfig
) -> None:
    """The engine is a pure function: its inputs survive the call unchanged."""
    invoices, payments = scenario
    before = ([*invoices], [*payments])
    reconcile(invoices, payments, config=config)
    assert (invoices, payments) == before


@given(scenario=scenarios(), config=configs)
def test_allocations_respect_currency_and_the_date_window(
    scenario: tuple[list[Invoice], list[Payment]], config: MatchConfig
) -> None:
    """No allocation crosses a currency or reaches outside the configured window."""
    invoices, payments = scenario
    result = reconcile(invoices, payments, config=config)
    by_id = {i.id: i for i in invoices}
    payments_by_id = {p.id: p for p in payments}

    for allocation in result.allocations:
        invoice = by_id[allocation.invoice_id]
        payment = payments_by_id[allocation.payment_id]
        assert invoice.currency == payment.currency
        assert (
            payment.date - timedelta(days=config.date_window_days_after)
            <= invoice.issue_date
            <= payment.date + timedelta(days=config.date_window_days_before)
        )


@given(scenario=scenarios(), config=configs)
def test_every_payment_is_accounted_for_exactly_once(
    scenario: tuple[list[Invoice], list[Payment]], config: MatchConfig
) -> None:
    """One result per bank line, and its own allocations plus residual equal it."""
    invoices, payments = scenario
    result = reconcile(invoices, payments, config=config)

    assert {r.payment_id for r in result.payments} == {p.id for p in payments}
    assert len(result.payments) == len(payments)

    for payment in payments:
        allocated = sum((a.amount for a in result.allocations_for(payment.id)), ZERO)
        residual = sum((r.amount for r in result.residuals if r.payment_id == payment.id), ZERO)
        assert allocated + residual == payment.amount
