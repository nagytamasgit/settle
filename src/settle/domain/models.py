"""Domain objects: plain frozen dataclasses, no framework, no I/O.

Everything here is immutable and hashable. The engine never mutates an
:class:`Invoice`; it tracks open balances separately and reports the outcome as
:class:`InvoiceState`. That keeps the inputs of a run inspectable after it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from .money import ZERO, money

_CURRENCY_LENGTH = 3

#: Confidence is a Decimal, not a float, so that "these two candidates are
#: exactly as good" is an exact comparison rather than an epsilon guess. Tie
#: detection drives the review queue, so it has to be exact.
CONFIDENCE_QUANTUM = Decimal("0.0001")
CONFIDENCE_MAX = Decimal("0.99")


def quantise_confidence(value: Decimal) -> Decimal:
    """Clamp to ``[0, CONFIDENCE_MAX]`` and quantise, so ties compare equal."""
    clamped = min(max(value, Decimal(0)), CONFIDENCE_MAX)
    return clamped.quantize(CONFIDENCE_QUANTUM, rounding=ROUND_HALF_UP)


class InvoiceStatus(StrEnum):
    """Terminal state of an invoice after a run.

    ``SETTLED_WITH_FEE`` is deliberately distinct from ``PAID``: the customer
    did not pay the full amount, a fee ate the difference, and finance should
    be able to report on that separately rather than discovering it in a
    write-off account at year end.
    """

    OPEN = "open"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    SETTLED_WITH_FEE = "settled_with_fee"


class PaymentStatus(StrEnum):
    """Terminal state of a bank line after a run."""

    UNMATCHED = "unmatched"
    MATCHED = "matched"
    PARTIALLY_MATCHED = "partially_matched"
    REVIEW = "review"


class Strategy(StrEnum):
    """How a candidate explains a payment."""

    EXACT_SINGLE = "exact_single"
    FEE_TOLERANCE = "fee_tolerance"
    COMBINATION = "combination"
    PARTIAL = "partial"
    OVERPAYMENT = "overpayment"


class ReviewReason(StrEnum):
    """Why a payment was routed to a human instead of applied."""

    NO_CANDIDATE = "no_candidate"
    BELOW_THRESHOLD = "below_threshold"
    AMBIGUOUS = "ambiguous"
    SEARCH_TRUNCATED = "search_truncated"


def _validate_currency(code: str) -> str:
    normalised = code.strip().upper()
    if len(normalised) != _CURRENCY_LENGTH or not normalised.isalpha():
        raise ValueError(f"currency must be a 3-letter code, got {code!r}")
    return normalised


@dataclass(frozen=True, slots=True)
class Invoice:
    """A receivable. ``amount`` is the full face value, always positive."""

    id: str
    customer_id: str
    number: str
    issue_date: date
    amount: Decimal
    currency: str
    customer_name: str = ""
    due_date: date | None = None

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("invoice id must not be empty")
        if not self.customer_id:
            raise ValueError(f"invoice {self.id}: customer_id must not be empty")
        object.__setattr__(self, "amount", money(self.amount))
        object.__setattr__(self, "currency", _validate_currency(self.currency))
        if self.amount <= ZERO:
            raise ValueError(f"invoice {self.id}: amount must be positive, got {self.amount}")


@dataclass(frozen=True, slots=True)
class Payment:
    """An incoming bank line.

    ``customer_id`` is optional because bank statements frequently do not carry
    one; ``counterparty`` holds whatever name the bank supplied, and the engine
    resolves it against the ledger when it can.
    """

    id: str
    date: date
    amount: Decimal
    currency: str
    reference: str = ""
    counterparty: str = ""
    customer_id: str | None = None

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("payment id must not be empty")
        object.__setattr__(self, "amount", money(self.amount))
        object.__setattr__(self, "currency", _validate_currency(self.currency))
        if self.amount <= ZERO:
            raise ValueError(
                f"payment {self.id}: amount must be positive, got {self.amount}; "
                "outgoing lines and reversals are out of scope"
            )


@dataclass(frozen=True, slots=True)
class Allocation:
    """Payment money applied to an invoice. Always positive."""

    payment_id: str
    invoice_id: str
    amount: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", money(self.amount))
        if self.amount <= ZERO:
            raise ValueError(
                f"allocation {self.payment_id}->{self.invoice_id}: "
                f"amount must be positive, got {self.amount}"
            )


@dataclass(frozen=True, slots=True)
class Fee:
    """A shortfall absorbed on the invoice side.

    Fee money never arrived, so it is *not* part of payment conservation. It
    closes the gap between what was allocated to an invoice and the invoice's
    face value. See :mod:`settle.domain.invariants`.
    """

    payment_id: str
    invoice_id: str
    amount: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", money(self.amount))
        if self.amount <= ZERO:
            raise ValueError(
                f"fee {self.payment_id}->{self.invoice_id}: "
                f"amount must be positive, got {self.amount}"
            )


@dataclass(frozen=True, slots=True)
class Residual:
    """Payment money that was not allocated to any invoice."""

    payment_id: str
    amount: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", money(self.amount))
        if self.amount <= ZERO:
            raise ValueError(f"residual {self.payment_id}: amount must be positive")


@dataclass(frozen=True, slots=True)
class Candidate:
    """One complete explanation of a payment.

    A candidate is self-contained: applying its allocations, fees and residual
    fully accounts for the payment. The engine ranks candidates and either
    applies the winner or sends the whole ranked list to review.
    """

    strategy: Strategy
    allocations: tuple[Allocation, ...]
    fees: tuple[Fee, ...] = ()
    residual: Decimal = ZERO
    confidence: Decimal = ZERO
    reasons: tuple[str, ...] = ()
    ref_matched_invoice_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.allocations:
            raise ValueError("a candidate must allocate to at least one invoice")
        object.__setattr__(self, "residual", money(self.residual))
        if self.residual < ZERO:
            raise ValueError("residual must not be negative")
        object.__setattr__(self, "confidence", quantise_confidence(self.confidence))

    @property
    def invoice_ids(self) -> tuple[str, ...]:
        return tuple(a.invoice_id for a in self.allocations)

    @property
    def allocated(self) -> Decimal:
        return sum((a.amount for a in self.allocations), ZERO)

    @property
    def fee_total(self) -> Decimal:
        return sum((f.amount for f in self.fees), ZERO)


@dataclass(frozen=True, slots=True)
class InvoiceState:
    """What happened to one invoice across a run."""

    invoice_id: str
    status: InvoiceStatus
    face_value: Decimal
    allocated: Decimal
    fee_written_off: Decimal
    open_balance: Decimal


@dataclass(frozen=True, slots=True)
class ReviewItem:
    """A payment a human has to look at, with the evidence that got it here."""

    payment_id: str
    reason: ReviewReason
    message: str
    candidates: tuple[Candidate, ...] = ()


@dataclass(frozen=True, slots=True)
class PaymentResult:
    """What happened to one bank line."""

    payment_id: str
    status: PaymentStatus
    chosen: Candidate | None = None
    ranked_candidates: tuple[Candidate, ...] = ()
    review_reason: ReviewReason | None = None
    search_truncated: bool = False


@dataclass(frozen=True, slots=True)
class RunReport:
    """Aggregate counts and money totals for a run. Cheap to log, easy to diff."""

    engine_version: str
    payments_total: int
    payments_matched: int
    payments_partially_matched: int
    payments_in_review: int
    payments_unmatched: int
    invoices_total: int
    invoices_paid: int
    invoices_settled_with_fee: int
    invoices_partially_paid: int
    invoices_open: int
    money_in: Decimal
    money_allocated: Decimal
    money_residual: Decimal
    money_fees: Decimal
    search_truncations: int
    model_parser_calls: int = 0

    @property
    def review_rate(self) -> Decimal:
        if self.payments_total == 0:
            return ZERO
        return Decimal(self.payments_in_review) / Decimal(self.payments_total)


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """The complete, serialisable answer for one run."""

    payments: tuple[PaymentResult, ...]
    allocations: tuple[Allocation, ...]
    fees: tuple[Fee, ...]
    residuals: tuple[Residual, ...]
    invoice_states: tuple[InvoiceState, ...]
    review_queue: tuple[ReviewItem, ...]
    report: RunReport
    warnings: tuple[str, ...] = ()

    def allocations_for(self, payment_id: str) -> tuple[Allocation, ...]:
        return tuple(a for a in self.allocations if a.payment_id == payment_id)

    def state_of(self, invoice_id: str) -> InvoiceState:
        for state in self.invoice_states:
            if state.invoice_id == invoice_id:
                return state
        raise KeyError(invoice_id)

    def result_for(self, payment_id: str) -> PaymentResult:
        for result in self.payments:
            if result.payment_id == payment_id:
                return result
        raise KeyError(payment_id)
