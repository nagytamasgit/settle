"""The benchmark's scoring rule, which is a claim and deserves a test.

The rule: a payment sent to review is not a wrong answer. It costs recall and
shows up in the review rate, but never in precision. Every number in the README
depends on that being implemented the way `docs/EVAL.md` says it is.
"""

from __future__ import annotations

from decimal import Decimal

from bench.generate import NoiseProfile, generate
from bench.metrics import predicted_pairs, score
from settle.domain.match import reconcile
from tests.conftest import inv, pay


def test_a_clean_match_scores_perfectly() -> None:
    result = reconcile([inv("i1", "1000.00")], [pay("p1", "1000.00", reference="szamla 1")])

    measured = score(result, {("p1", "i1")})

    assert measured.precision == 1.0
    assert measured.recall == 1.0
    assert measured.review_rate == 0.0


def test_a_payment_in_review_costs_recall_but_not_precision() -> None:
    """Two identical invoices, no reference: the engine declines to choose."""
    invoices = [inv("i1", "500.00"), inv("i2", "500.00")]
    result = reconcile(invoices, [pay("p1", "500.00")])

    measured = score(result, {("p1", "i1")})

    assert measured.precision == 1.0, "declining is not a wrong answer"
    assert measured.recall == 0.0, "but it is a missed one"
    assert measured.review_rate == 1.0


def test_ranked_but_unapplied_candidates_are_not_predictions() -> None:
    invoices = [inv("i1", "500.00"), inv("i2", "500.00")]
    result = reconcile(invoices, [pay("p1", "500.00")])

    # The engine offered two candidates; it predicted neither.
    assert result.result_for("p1").ranked_candidates
    assert predicted_pairs(result) == set()


def test_a_wrong_allocation_does_cost_precision() -> None:
    """The counterpart: when the engine does commit, it is scored on it."""
    result = reconcile([inv("i1", "1000.00")], [pay("p1", "1000.00", reference="szamla 1")])

    measured = score(result, {("p1", "i9")})

    assert measured.precision == 0.0
    assert measured.recall == 0.0


class TestGenerator:
    def test_clean_data_has_one_payment_per_invoice(self) -> None:
        case = generate(seed=3, invoice_count=40, noise=NoiseProfile())

        assert len(case.payments) == len(case.invoices)
        assert len(case.true_pairs) == len(case.invoices)

    def test_the_same_seed_produces_the_same_case(self) -> None:
        first = generate(seed=7, invoice_count=30, noise=NoiseProfile.uniform(0.4))
        second = generate(seed=7, invoice_count=30, noise=NoiseProfile.uniform(0.4))

        assert first.invoices == second.invoices
        assert first.payments == second.payments
        assert first.truth == second.truth

    def test_clean_data_is_reconciled_perfectly(self) -> None:
        """A sanity floor: if this ever drops, the harness is measuring noise
        it did not intend to inject."""
        case = generate(seed=11, invoice_count=120, noise=NoiseProfile())

        measured = score(reconcile(case.invoices, case.payments), case.true_pairs)

        assert measured.precision == 1.0
        assert measured.recall == 1.0

    def test_structure_noise_produces_merged_and_split_payments(self) -> None:
        case = generate(seed=5, invoice_count=120, noise=NoiseProfile(structure=0.6))

        sizes = {len(invoice_ids) for invoice_ids in case.truth.values()}
        assert max(sizes) > 1, "expected at least one payment covering several invoices"

        paid_by = [invoice_id for invoice_ids in case.truth.values() for invoice_id in invoice_ids]
        assert len(paid_by) != len(set(paid_by)), "expected at least one invoice split in two"

    def test_amount_noise_moves_amounts_off_the_invoice_total(self) -> None:
        clean = generate(seed=2, invoice_count=80, noise=NoiseProfile())
        noisy = generate(seed=2, invoice_count=80, noise=NoiseProfile(amount=1.0))

        clean_amounts = [p.amount for p in clean.payments]
        noisy_amounts = [p.amount for p in noisy.payments]
        assert clean_amounts != noisy_amounts
        assert all(amount > Decimal("0.00") for amount in noisy_amounts)

    def test_reference_noise_destroys_some_references(self) -> None:
        noisy = generate(seed=4, invoice_count=80, noise=NoiseProfile(reference=1.0))

        clean_form = sum(1 for p in noisy.payments if p.reference.startswith("INV-2026-"))
        assert clean_form < len(noisy.payments)
