"""The cases that make reconciliation hard, one test each."""

from __future__ import annotations

from decimal import Decimal

import pytest

from settle.domain.config import AmbiguityPolicy, MatchConfig, TieBreak
from settle.domain.errors import DuplicateIdError
from settle.domain.match import reconcile
from settle.domain.models import (
    InvoiceStatus,
    PaymentStatus,
    ReviewReason,
    Strategy,
)
from tests.conftest import inv, pay


class TestExactMatching:
    def test_amount_and_reference_agree(self) -> None:
        result = reconcile([inv("i1", "1000.00")], [pay("p1", "1000.00", reference="szamla 1")])

        chosen = result.result_for("p1").chosen
        assert chosen is not None
        assert chosen.strategy is Strategy.EXACT_SINGLE
        assert chosen.reasons[0] == "exact amount and reference"
        assert result.state_of("i1").status is InvoiceStatus.PAID

    def test_an_implausible_payment_is_offered_weakly_and_applied_to_nothing(self) -> None:
        """17.50 against a lone 1000 invoice could be a deposit. It is not the
        engine's job to decide that, only to say so and move on."""
        result = reconcile([inv("i1", "1000.00")], [pay("p1", "17.50", reference="rent")])

        payment_result = result.result_for("p1")
        assert payment_result.chosen is None
        assert payment_result.review_reason is ReviewReason.BELOW_THRESHOLD
        assert result.report.money_residual == Decimal("17.50")
        assert result.state_of("i1").status is InvoiceStatus.OPEN

    def test_a_payment_with_nothing_to_match_against_is_unmatched(self) -> None:
        result = reconcile([inv("i1", "1000.00", day=-200)], [pay("p1", "1000.00")])

        assert result.result_for("p1").status is PaymentStatus.UNMATCHED
        assert result.review_queue[0].reason is ReviewReason.NO_CANDIDATE

    def test_currencies_never_mix(self) -> None:
        result = reconcile(
            [inv("i1", "1000.00", currency="EUR")],
            [pay("p1", "1000.00", currency="HUF", reference="szamla 1")],
        )
        assert result.result_for("p1").status is PaymentStatus.UNMATCHED

    def test_a_repeated_id_is_an_error_not_a_silent_merge(self) -> None:
        with pytest.raises(DuplicateIdError, match="duplicate payment id"):
            reconcile([inv("i1", "10.00")], [pay("p1", "10.00"), pay("p1", "10.00")])


class TestReferenceEvidence:
    """Exact identification beats near-identification, and it has to.

    Found by the benchmark: with both tiers contributing, clean data scored
    0.94 recall instead of 1.00, because sequential invoice numbers are one
    digit apart and fuzzy-match each other above any useful threshold.
    """

    def test_a_near_miss_invoice_number_is_not_evidence(self) -> None:
        invoices = [
            inv("i1", "500.00", number="INV-2026-0042"),
            inv("i2", "500.00", number="INV-2026-0142"),
        ]

        result = reconcile(invoices, [pay("p1", "500.00", reference="INV-2026-0042")])

        chosen = result.result_for("p1").chosen
        assert chosen is not None
        assert chosen.invoice_ids == ("i1",)

    def test_fuzzy_matching_still_rescues_a_mistyped_reference(self) -> None:
        """When nothing matches exactly, a near miss is the best evidence there is."""
        invoices = [inv("i1", "500.00", number="INV-2026-0042")]

        result = reconcile(invoices, [pay("p1", "500.00", reference="INV-2026-004")])

        chosen = result.result_for("p1").chosen
        assert chosen is not None
        assert "reference" in chosen.reasons[0]


class TestPartialPayments:
    def test_a_300_invoice_paid_200_then_100(self) -> None:
        """The milestone case: residuals are first class, and the second payment closes it."""
        invoices = [inv("i1", "300.00")]
        payments = [
            pay("p1", "200.00", reference="szamla 1", day=10),
            pay("p2", "100.00", reference="szamla 1", day=20),
        ]

        result = reconcile(invoices, payments)

        first = result.result_for("p1").chosen
        assert first is not None
        assert first.strategy is Strategy.PARTIAL
        assert result.result_for("p2").status is PaymentStatus.MATCHED
        assert result.state_of("i1").status is InvoiceStatus.PAID
        assert result.state_of("i1").open_balance == Decimal("0.00")

    def test_an_invoice_left_short_stays_partially_paid(self) -> None:
        result = reconcile([inv("i1", "300.00")], [pay("p1", "200.00", reference="szamla 1")])

        state = result.state_of("i1")
        assert state.status is InvoiceStatus.PARTIALLY_PAID
        assert state.open_balance == Decimal("100.00")

    def test_an_overpayment_closes_the_invoice_and_leaves_a_residual(self) -> None:
        result = reconcile([inv("i1", "300.00")], [pay("p1", "500.00", reference="szamla 1")])

        assert result.result_for("p1").status is PaymentStatus.PARTIALLY_MATCHED
        assert result.state_of("i1").status is InvoiceStatus.PAID
        assert result.report.money_residual == Decimal("200.00")


class TestFeeTolerance:
    def test_a_payment_29_percent_short_is_settled_with_a_fee(self) -> None:
        """An international transfer arrives 2.9% light because a bank took a cut."""
        result = reconcile([inv("i1", "1000.00")], [pay("p1", "971.00", reference="szamla 1")])

        chosen = result.result_for("p1").chosen
        assert chosen is not None
        assert chosen.strategy is Strategy.FEE_TOLERANCE
        state = result.state_of("i1")
        assert state.fee_written_off == Decimal("29.00")
        assert state.allocated == Decimal("971.00")
        assert state.open_balance == Decimal("0.00")

    def test_settled_with_fee_is_not_paid(self) -> None:
        """The leakage guard: a fee closes the invoice into its own state, visibly."""
        result = reconcile([inv("i1", "1000.00")], [pay("p1", "971.00", reference="szamla 1")])

        assert result.state_of("i1").status is InvoiceStatus.SETTLED_WITH_FEE
        assert result.state_of("i1").status is not InvoiceStatus.PAID
        assert result.report.money_fees == Decimal("29.00")

    def test_a_shortfall_beyond_tolerance_is_a_part_payment_not_a_fee(self) -> None:
        """3.1% short of a 1000 invoice is someone paying less, not a bank fee."""
        result = reconcile([inv("i1", "1000.00")], [pay("p1", "969.00", reference="szamla 1")])

        chosen = result.result_for("p1").chosen
        assert chosen is not None
        assert chosen.strategy is Strategy.PARTIAL
        assert result.state_of("i1").status is InvoiceStatus.PARTIALLY_PAID
        assert result.report.money_fees == Decimal("0.00")

    def test_fee_money_is_not_payment_money(self) -> None:
        """Fees sit outside payment conservation: that money never arrived."""
        result = reconcile([inv("i1", "1000.00")], [pay("p1", "971.00", reference="szamla 1")])

        assert result.report.money_allocated + result.report.money_residual == Decimal("971.00")
        assert result.report.money_fees == Decimal("29.00")

    def test_zero_tolerance_turns_every_shortfall_into_a_part_payment(self) -> None:
        config = MatchConfig(fee_tolerance_pct=Decimal("0"), fee_tolerance_abs=Decimal("0"))
        result = reconcile(
            [inv("i1", "1000.00")], [pay("p1", "999.99", reference="szamla 1")], config=config
        )

        chosen = result.result_for("p1").chosen
        assert chosen is not None
        assert chosen.strategy is Strategy.PARTIAL


class TestCombinations:
    def test_one_payment_covering_three_invoices(self) -> None:
        invoices = [inv("i1", "100.00"), inv("i2", "200.00"), inv("i3", "300.00")]
        payment = pay("p1", "600.00", reference="INV-2026-0001 INV-2026-0002 INV-2026-0003")

        result = reconcile(invoices, [payment])

        chosen = result.result_for("p1").chosen
        assert chosen is not None
        assert chosen.strategy is Strategy.COMBINATION
        assert set(chosen.invoice_ids) == {"i1", "i2", "i3"}
        assert all(s.status is InvoiceStatus.PAID for s in result.invoice_states)

    def test_a_combination_short_by_a_fee_splits_the_fee_to_the_cent(self) -> None:
        invoices = [inv("i1", "100.00"), inv("i2", "200.00")]
        payment = pay("p1", "295.00", reference="INV-2026-0001 INV-2026-0002")

        result = reconcile(invoices, [payment])

        chosen = result.result_for("p1").chosen
        assert chosen is not None
        assert chosen.fee_total == Decimal("5.00")
        assert chosen.allocated == Decimal("295.00")
        assert all(s.status is InvoiceStatus.SETTLED_WITH_FEE for s in result.invoice_states)

    def test_the_search_budget_is_respected_on_adversarial_input(self) -> None:
        """Two dozen invoices whose cents make an exact sum impossible.

        The search cannot succeed and cannot prune its way out, so the only
        acceptable behaviour is to stop at the budget and say so.
        """
        invoices = [inv(f"i{n}", f"{10 + n}.01", day=n % 5) for n in range(24)]
        payment = pay("p1", "87.00")
        config = MatchConfig(search_node_budget=5)

        result = reconcile(invoices, [payment], config=config)

        assert result.result_for("p1").search_truncated
        assert result.result_for("p1").review_reason is ReviewReason.SEARCH_TRUNCATED
        assert result.report.search_truncations == 1
        assert "search budget" in result.warnings[0]


class TestAmbiguity:
    def test_two_invoices_that_explain_a_payment_equally_go_to_review(self) -> None:
        """Same customer, same amount, same day. The engine refuses to guess."""
        invoices = [inv("i1", "500.00"), inv("i2", "500.00")]

        result = reconcile(invoices, [pay("p1", "500.00")])

        payment_result = result.result_for("p1")
        assert payment_result.status is PaymentStatus.REVIEW
        assert payment_result.review_reason is ReviewReason.AMBIGUOUS
        assert payment_result.chosen is None
        assert len(result.review_queue[0].candidates) == 2
        assert all(s.status is InvoiceStatus.OPEN for s in result.invoice_states)

    def test_review_returns_the_candidates_ranked_not_just_a_flag(self) -> None:
        invoices = [inv("i1", "500.00"), inv("i2", "500.00")]
        result = reconcile(invoices, [pay("p1", "500.00")])

        ranked = result.result_for("p1").ranked_candidates
        assert [c.invoice_ids for c in ranked] == [("i1",), ("i2",)]
        assert ranked[0].confidence == ranked[1].confidence

    def test_a_low_scoring_best_guess_is_offered_not_applied(self) -> None:
        """A fee-sized shortfall with no reference is plausible, not convincing."""
        result = reconcile([inv("i1", "1000.00")], [pay("p1", "971.00")])

        payment_result = result.result_for("p1")
        assert payment_result.review_reason is ReviewReason.BELOW_THRESHOLD
        assert payment_result.chosen is None
        assert payment_result.ranked_candidates[0].invoice_ids == ("i1",)
        assert payment_result.ranked_candidates[0].confidence < Decimal("0.80")
        assert result.state_of("i1").status is InvoiceStatus.OPEN

    def test_an_exact_unique_amount_is_confident_without_a_reference(self) -> None:
        """The counterpart: nothing else explains it, so no human is needed."""
        result = reconcile([inv("i1", "500.00"), inv("i2", "640.00")], [pay("p1", "500.00")])

        chosen = result.result_for("p1").chosen
        assert chosen is not None
        assert chosen.invoice_ids == ("i1",)
        assert "no other invoice or combination explains this amount" in chosen.reasons

    @pytest.mark.parametrize(
        ("policy", "expected"),
        [(TieBreak.OLDEST_FIRST, "i1"), (TieBreak.NEWEST_FIRST, "i2")],
    )
    def test_tie_break_is_a_policy_with_a_stated_outcome(
        self, policy: TieBreak, expected: str
    ) -> None:
        """Opting in to auto-resolution makes the policy, not chance, decide."""
        invoices = [inv("i1", "500.00", day=0), inv("i2", "500.00", day=3)]
        config = MatchConfig(
            tie_break=policy,
            ambiguity_policy=AmbiguityPolicy.RESOLVE_BY_POLICY,
            confidence_threshold=Decimal("0.50"),
        )

        result = reconcile(invoices, [pay("p1", "500.00")], config=config)

        chosen = result.result_for("p1").chosen
        assert chosen is not None
        assert chosen.invoice_ids == (expected,)


class TestCustomerResolution:
    def test_a_payment_is_confined_to_its_own_customer(self) -> None:
        invoices = [
            inv("i1", "500.00", customer="c1", customer_name="Acme Kft"),
            inv("i2", "500.00", customer="c2", customer_name="Globex Zrt"),
        ]
        result = reconcile(invoices, [pay("p1", "500.00", counterparty="Globex Zrt")])

        chosen = result.result_for("p1").chosen
        assert chosen is not None
        assert chosen.invoice_ids == ("i2",)

    def test_an_unidentifiable_payer_needs_a_reference(self) -> None:
        invoices = [inv("i1", "500.00", customer_name="Acme Kft")]
        result = reconcile(invoices, [pay("p1", "500.00", counterparty="", reference="szamla 1")])

        assert result.result_for("p1").chosen is not None

    def test_an_unidentifiable_payer_without_a_reference_is_not_guessed_at(self) -> None:
        invoices = [inv("i1", "500.00", customer_name="Acme Kft")]
        result = reconcile(invoices, [pay("p1", "500.00", counterparty="")])

        assert result.result_for("p1").status is PaymentStatus.UNMATCHED
