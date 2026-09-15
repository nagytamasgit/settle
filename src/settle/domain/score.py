"""Confidence scoring: one table, no hidden rules.

Every number that moves a score is a named constant in this module. A reviewer
asking "why is this 0.86 and not 0.79?" can add the terms up by hand, and the
reason strings attached to a candidate spell out the same arithmetic in words.

Confidence is a :class:`~decimal.Decimal`, not a float, for one specific
reason: two candidates tie when their confidences are *equal*, and a tie is what
sends a payment to a human. Exact equality on Decimal makes that decision exact
rather than a comparison against an epsilon nobody chose deliberately.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from .models import Strategy, quantise_confidence

#: Starting point per strategy. The ordering encodes the engine's opinion of
#: how much each kind of explanation is worth on its own evidence: an exact
#: single-invoice hit beats a combination, which beats a part payment.
BASE: Final[dict[Strategy, Decimal]] = {
    Strategy.EXACT_SINGLE: Decimal("0.70"),
    Strategy.COMBINATION: Decimal("0.62"),
    Strategy.FEE_TOLERANCE: Decimal("0.58"),
    Strategy.PARTIAL: Decimal("0.50"),
    Strategy.OVERPAYMENT: Decimal("0.48"),
}

#: Every invoice in the candidate was named in the payment reference. This is
#: the strongest single signal available, which is why the model tier that
#: produces references is also the one kept on the shortest leash.
REF_ALL: Final = Decimal("0.29")
#: Some but not all were named, scaled by coverage.
REF_SOME: Final = Decimal("0.16")

#: The payment could be tied to a customer, by id or by counterparty name.
CUSTOMER_RESOLVED: Final = Decimal("0.06")
#: Nothing else in the eligible set explains the amount this way.
UNIQUE_EXPLANATION: Final = Decimal("0.10")
#: The customer had exactly one open invoice, so there was nothing to confuse.
SOLE_OPEN_INVOICE: Final = Decimal("0.04")

#: Charged per invoice beyond the first: a four-invoice combination is a
#: weaker claim than a two-invoice one, even when both add up exactly.
COMBINATION_SIZE_PENALTY: Final = Decimal("0.04")
#: Charged in proportion to how much of the fee tolerance was consumed. A
#: payment 0.1% short is far more obviously a bank fee than one 2.9% short.
FEE_CONSUMPTION_PENALTY: Final = Decimal("0.06")


@dataclass(frozen=True, slots=True)
class Evidence:
    """Everything the scorer is allowed to know about a candidate."""

    strategy: Strategy
    invoice_count: int
    #: Fraction of the candidate's invoices named in the payment reference.
    ref_coverage: Decimal = Decimal("0")
    customer_resolved: bool = False
    unique_explanation: bool = False
    sole_open_invoice: bool = False
    #: shortfall / tolerance, in ``[0, 1]``. Zero when no fee is involved.
    fee_consumption: Decimal = Decimal("0")
    #: Set when the subset search hit its budget, which makes
    #: ``unique_explanation`` unreliable and suppresses its bonus.
    search_truncated: bool = False


def score(evidence: Evidence) -> tuple[Decimal, tuple[str, ...]]:
    """Return ``(confidence, reasons)`` for one candidate."""
    value = BASE[evidence.strategy]
    reasons: list[str] = [_headline(evidence)]

    if evidence.ref_coverage >= Decimal("1"):
        value += REF_ALL
    elif evidence.ref_coverage > 0:
        value += REF_SOME * evidence.ref_coverage

    if evidence.customer_resolved:
        value += CUSTOMER_RESOLVED
    else:
        reasons.append("customer not identified from the bank line")

    if evidence.unique_explanation and not evidence.search_truncated:
        value += UNIQUE_EXPLANATION
        reasons.append("no other invoice or combination explains this amount")
    elif evidence.search_truncated:
        reasons.append("combination search hit its budget, so uniqueness is unproven")

    if evidence.sole_open_invoice:
        value += SOLE_OPEN_INVOICE
        reasons.append("only one invoice was open for this customer")

    if evidence.invoice_count > 1:
        value -= COMBINATION_SIZE_PENALTY * (evidence.invoice_count - 1)

    if evidence.fee_consumption > 0:
        value -= FEE_CONSUMPTION_PENALTY * evidence.fee_consumption

    return quantise_confidence(value), tuple(reasons)


def _headline(evidence: Evidence) -> str:
    """The one-line explanation finance sees next to the allocation."""
    reference = _reference_phrase(evidence)
    match evidence.strategy:
        case Strategy.EXACT_SINGLE:
            return f"exact amount{reference}"
        case Strategy.FEE_TOLERANCE:
            return f"amount within fee tolerance{reference}"
        case Strategy.COMBINATION:
            return f"combination of {evidence.invoice_count} invoices{reference}"
        case Strategy.PARTIAL:
            return f"part payment, invoice stays open{reference}"
        case Strategy.OVERPAYMENT:
            return f"pays the invoice in full and leaves a residual{reference}"


def _reference_phrase(evidence: Evidence) -> str:
    if evidence.ref_coverage >= Decimal("1"):
        if evidence.invoice_count == 1:
            return " and reference"
        return f", reference matched all {evidence.invoice_count}"
    if evidence.ref_coverage > 0:
        matched = int(evidence.ref_coverage * evidence.invoice_count)
        return f", reference matched {matched} of {evidence.invoice_count}"
    return ", no reference"
