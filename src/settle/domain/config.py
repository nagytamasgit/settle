"""Run configuration: every policy the engine applies is a value here.

Nothing in the matcher reads a module-level constant to decide behaviour. If a
reviewer asks "why did it do that?", the answer is in this object plus the
weight table in :mod:`settle.domain.score`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

#: A combination must cover at least two invoices; one invoice is a single match.
MIN_COMBINATION_SIZE: Final = 2
#: rapidfuzz scores run 0-100.
MAX_FUZZ_SCORE: Final = 100


class TieBreak(StrEnum):
    """How equally-scoring candidates are *ordered*.

    Ordering is not the same as choosing. Tie-breaking makes the output stable
    across runs; whether a tie may be applied at all is
    :class:`AmbiguityPolicy`.
    """

    OLDEST_FIRST = "oldest_first"
    NEWEST_FIRST = "newest_first"
    LARGEST_FIRST = "largest_first"
    SMALLEST_FIRST = "smallest_first"


class AmbiguityPolicy(StrEnum):
    """What to do when two candidates explain a payment equally well."""

    #: Apply nothing, send the ranked list to a human. The default, because
    #: guessing is the failure mode this project exists to avoid.
    REVIEW = "review"
    #: Apply the top candidate after tie-breaking. Deterministic, but it is a
    #: decision made by policy rather than by evidence; opt in knowingly.
    RESOLVE_BY_POLICY = "resolve_by_policy"


@dataclass(frozen=True, slots=True)
class MatchConfig:
    """Policy for one run."""

    #: A payment can settle an invoice issued at most this many days earlier.
    date_window_days_after: int = 120
    #: ...and at most this many days later, for prepayments and clock skew.
    date_window_days_before: int = 5

    #: A shortfall is treated as a fee when it is within
    #: ``max(fee_tolerance_pct * face_value, fee_tolerance_abs)``, optionally
    #: capped by ``fee_tolerance_cap``. Anything larger is a partial payment.
    #: That boundary is what stops a tolerance from silently closing invoices.
    fee_tolerance_pct: Decimal = Decimal("0.03")
    fee_tolerance_abs: Decimal = Decimal("1.00")
    fee_tolerance_cap: Decimal | None = None

    #: Candidates scoring below this are never applied automatically.
    confidence_threshold: Decimal = Decimal("0.80")

    #: Bounds on the subset-sum search. Exponential in general, so all three
    #: are needed: width, depth, and a hard node budget.
    max_combination_size: int = 4
    max_combination_candidates: int = 24
    search_node_budget: int = 50_000
    max_combinations_returned: int = 16

    tie_break: TieBreak = TieBreak.OLDEST_FIRST
    ambiguity_policy: AmbiguityPolicy = AmbiguityPolicy.REVIEW

    allow_partial: bool = True
    allow_overpayment: bool = True
    allow_combinations: bool = True

    #: When True, a payment is only matched against invoices whose customer was
    #: resolved. Turning it off widens recall and is how cross-customer
    #: mismatches happen; the benchmark reports both.
    require_customer_match: bool = True

    #: rapidfuzz score (0-100) above which a reference is considered a match.
    fuzzy_ref_threshold: int = 88

    #: Check money invariants at the end of every run, not just in tests.
    verify_invariants: bool = True

    def __post_init__(self) -> None:
        if self.fee_tolerance_pct < 0 or self.fee_tolerance_pct >= 1:
            raise ValueError("fee_tolerance_pct must be in [0, 1)")
        if self.fee_tolerance_abs < 0:
            raise ValueError("fee_tolerance_abs must not be negative")
        if not (0 <= self.confidence_threshold <= 1):
            raise ValueError("confidence_threshold must be in [0, 1]")
        if self.max_combination_size < MIN_COMBINATION_SIZE:
            raise ValueError(f"max_combination_size must be at least {MIN_COMBINATION_SIZE}")
        if self.max_combination_candidates < 1:
            raise ValueError("max_combination_candidates must be at least 1")
        if self.search_node_budget < 1:
            raise ValueError("search_node_budget must be at least 1")
        if not (0 <= self.fuzzy_ref_threshold <= MAX_FUZZ_SCORE):
            raise ValueError(f"fuzzy_ref_threshold must be in [0, {MAX_FUZZ_SCORE}]")

    def fee_tolerance_for(self, face_value: Decimal) -> Decimal:
        """The largest shortfall that still counts as a fee on ``face_value``."""
        tolerance = max(self.fee_tolerance_pct * face_value, self.fee_tolerance_abs)
        if self.fee_tolerance_cap is not None:
            tolerance = min(tolerance, self.fee_tolerance_cap)
        return tolerance.quantize(Decimal("0.01"))


DEFAULT_CONFIG = MatchConfig()
