"""Bounded search and run policy, tested directly."""

from __future__ import annotations

from decimal import Decimal

import pytest

from settle.domain.config import AmbiguityPolicy, MatchConfig, TieBreak
from settle.domain.search import apportion, find_subsets


def amounts(*values: str) -> list[Decimal]:
    return [Decimal(v) for v in values]


class TestSubsetSearch:
    def test_it_finds_an_exact_subset(self) -> None:
        result = find_subsets(
            amounts("100.00", "250.00", "750.00"), Decimal("1000.00"), Decimal("1000.00")
        )

        assert result.subsets == ((1, 2),)
        assert not result.truncated

    def test_it_returns_every_tied_subset(self) -> None:
        """Two ways to make 300. Both are reported; choosing is not its job."""
        result = find_subsets(
            amounts("100.00", "200.00", "150.00", "150.00"),
            Decimal("300.00"),
            Decimal("300.00"),
        )

        assert (0, 1) in result.subsets
        assert (2, 3) in result.subsets

    def test_subsets_are_reported_as_ascending_caller_indices(self) -> None:
        """The caller's ordering encodes the tie-break policy, so it is preserved."""
        result = find_subsets(amounts("750.00", "250.00"), Decimal("1000.00"), Decimal("1000.00"))

        assert result.subsets == ((0, 1),)

    def test_nothing_is_returned_when_nothing_adds_up(self) -> None:
        result = find_subsets(amounts("100.00", "200.00"), Decimal("999.00"), Decimal("999.00"))

        assert result.subsets == ()
        assert not result.truncated

    def test_the_node_budget_stops_the_search_and_says_so(self) -> None:
        result = find_subsets(
            amounts(*[f"{10 + n}.01" for n in range(24)]),
            Decimal("87.00"),
            Decimal("89.69"),
            node_budget=5,
        )

        assert result.truncated
        assert result.nodes_visited <= 6

    def test_max_size_bounds_the_depth(self) -> None:
        result = find_subsets(
            amounts("10.00", "10.00", "10.00", "10.00"),
            Decimal("40.00"),
            Decimal("40.00"),
            max_size=3,
        )

        assert result.subsets == ()

    def test_a_tolerance_window_admits_a_near_miss(self) -> None:
        result = find_subsets(amounts("100.00", "200.00"), Decimal("295.00"), Decimal("300.00"))

        assert result.subsets == ((0, 1),)

    def test_zero_and_negative_amounts_are_rejected(self) -> None:
        """The prunes depend on every amount being positive."""
        with pytest.raises(ValueError, match="strictly positive"):
            find_subsets(amounts("100.00", "0.00"), Decimal("100.00"), Decimal("100.00"))

    def test_an_empty_candidate_set_is_fine(self) -> None:
        assert find_subsets([], Decimal("10.00"), Decimal("10.00")).subsets == ()


class TestApportionment:
    def test_a_split_that_does_not_divide_evenly_still_sums_exactly(self) -> None:
        """1.00 across three equal invoices is 0.34 / 0.33 / 0.33, not 0.33 x 3."""
        shares = apportion(Decimal("1.00"), amounts("100.00", "100.00", "100.00"))

        assert sum(shares) == Decimal("1.00")
        assert sorted(shares) == [Decimal("0.33"), Decimal("0.33"), Decimal("0.34")]

    def test_shares_follow_the_weights(self) -> None:
        shares = apportion(Decimal("30.00"), amounts("100.00", "200.00"))

        assert shares == [Decimal("10.00"), Decimal("20.00")]
        assert sum(shares) == Decimal("30.00")

    @pytest.mark.parametrize("total", ["0.01", "0.02", "5.00", "99.99"])
    def test_the_sum_is_exact_for_any_total(self, total: str) -> None:
        weights = amounts("33.33", "66.67", "10.00", "1.00")
        assert sum(apportion(Decimal(total), weights)) == Decimal(total)

    def test_an_empty_weight_set_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="empty set"):
            apportion(Decimal("1.00"), [])

    def test_weights_must_be_positive_overall(self) -> None:
        with pytest.raises(ValueError, match="positive amount"):
            apportion(Decimal("1.00"), amounts("0.00"))


class TestConfigValidation:
    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"fee_tolerance_pct": Decimal("-0.1")}, "fee_tolerance_pct"),
            ({"fee_tolerance_pct": Decimal("1.5")}, "fee_tolerance_pct"),
            ({"fee_tolerance_abs": Decimal("-1")}, "fee_tolerance_abs"),
            ({"confidence_threshold": Decimal("1.5")}, "confidence_threshold"),
            ({"max_combination_size": 1}, "max_combination_size"),
            ({"max_combination_candidates": 0}, "max_combination_candidates"),
            ({"search_node_budget": 0}, "search_node_budget"),
            ({"fuzzy_ref_threshold": 101}, "fuzzy_ref_threshold"),
        ],
    )
    def test_nonsense_policy_is_rejected_at_construction(self, kwargs: dict, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            MatchConfig(**kwargs)

    def test_the_fee_tolerance_is_the_larger_of_percentage_and_floor(self) -> None:
        config = MatchConfig(fee_tolerance_pct=Decimal("0.03"), fee_tolerance_abs=Decimal("1.00"))

        assert config.fee_tolerance_for(Decimal("1000.00")) == Decimal("30.00")
        # On a tiny invoice the percentage is below the rounding floor.
        assert config.fee_tolerance_for(Decimal("10.00")) == Decimal("1.00")

    def test_the_cap_bounds_the_tolerance_on_large_invoices(self) -> None:
        config = MatchConfig(fee_tolerance_cap=Decimal("25.00"))

        assert config.fee_tolerance_for(Decimal("1000000.00")) == Decimal("25.00")

    def test_policies_are_string_valued_for_clean_serialisation(self) -> None:
        assert TieBreak.OLDEST_FIRST == "oldest_first"
        assert AmbiguityPolicy.REVIEW == "review"
