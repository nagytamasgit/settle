"""How the benchmark scores a run.

The scoring rule that matters most: **a payment sent to review is not a
prediction.** It counts against recall and against the review rate, but it
never counts against precision, because the engine did not claim anything.

That is the honest way to measure a system whose whole design is "decline when
unsure". Scoring a review item as a wrong answer would punish exactly the
behaviour the engine exists to have; ignoring it entirely would hide the cost
of declining. It costs recall and shows up in the review column, which is where
a finance team feels it.
"""

from __future__ import annotations

from dataclasses import dataclass

from settle.domain.models import ReconciliationResult


@dataclass(frozen=True, slots=True)
class Score:
    """Quality of one reconciliation run against known ground truth."""

    precision: float
    recall: float
    f1: float
    review_rate: float
    predicted_pairs: int
    true_pairs: int
    correct_pairs: int
    payments: int

    @property
    def review_count(self) -> int:
        return round(self.review_rate * self.payments)


def predicted_pairs(result: ReconciliationResult) -> set[tuple[str, str]]:
    """The (payment, invoice) pairs the engine actually applied.

    Ranked-but-not-applied candidates are excluded on purpose: an option
    offered to a human is not an answer the engine gave.
    """
    return {
        (payment.payment_id, invoice_id)
        for payment in result.payments
        if payment.chosen is not None
        for invoice_id in payment.chosen.invoice_ids
    }


def score(result: ReconciliationResult, truth: set[tuple[str, str]]) -> Score:
    """Compare a run against the generator's ground truth."""
    predicted = predicted_pairs(result)
    correct = predicted & truth

    precision = len(correct) / len(predicted) if predicted else 1.0
    recall = len(correct) / len(truth) if truth else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    report = result.report
    needing_a_human = report.payments_in_review + report.payments_unmatched
    review_rate = needing_a_human / report.payments_total if report.payments_total else 0.0

    return Score(
        precision=precision,
        recall=recall,
        f1=f1,
        review_rate=review_rate,
        predicted_pairs=len(predicted),
        true_pairs=len(truth),
        correct_pairs=len(correct),
        payments=report.payments_total,
    )
