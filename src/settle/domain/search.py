"""Bounded subset-sum: which open invoices add up to this payment?

Subset-sum is NP-complete, so an unbounded search is not an option on a real
ledger. Three bounds are applied and all three are configuration, not magic
numbers buried in the code:

* **width** - how many candidate invoices enter the search at all
  (``max_combination_candidates``, applied by the caller)
* **depth** - how many invoices one payment may cover (``max_combination_size``)
* **work** - a hard node budget (``search_node_budget``)

When the budget runs out the search says so rather than quietly returning what
it happened to find. The caller downgrades its confidence accordingly, because
"I found one answer" means much less if you stopped looking early.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from .money import ZERO


@dataclass(frozen=True, slots=True)
class SubsetSearchResult:
    """Subsets whose total lands in ``[target, upper]``, plus the search's honesty."""

    #: Each subset is a tuple of indices into the caller's item sequence,
    #: ascending, so the caller's deterministic ordering is preserved.
    subsets: tuple[tuple[int, ...], ...]
    nodes_visited: int
    truncated: bool


def find_subsets(
    amounts: list[Decimal],
    target: Decimal,
    upper: Decimal,
    *,
    min_size: int = 2,
    max_size: int = 4,
    node_budget: int = 50_000,
    max_results: int = 16,
) -> SubsetSearchResult:
    """Find index subsets of ``amounts`` summing into ``[target, upper]``.

    ``amounts`` must all be positive; that is what makes the two prunes below
    valid. The caller's ordering is meaningful (it encodes the tie-break
    policy), so results are reported as ascending index tuples and sorted by
    size then by index, never by amount.
    """
    if min_size < 1:
        raise ValueError("min_size must be at least 1")
    if any(amount <= ZERO for amount in amounts):
        raise ValueError("subset search requires strictly positive amounts")

    count = len(amounts)
    # Largest first purely for pruning power; indices carry the real order.
    order = sorted(range(count), key=lambda i: (-amounts[i], i))

    # suffix_total[k] = what is still reachable from position k onwards.
    suffix_total = [ZERO] * (count + 1)
    for position in range(count - 1, -1, -1):
        suffix_total[position] = suffix_total[position + 1] + amounts[order[position]]

    found: list[tuple[int, ...]] = []
    nodes = 0
    truncated = False

    def walk(position: int, chosen: list[int], running: Decimal) -> None:  # noqa: PLR0911
        nonlocal nodes, truncated
        if truncated or len(found) >= max_results:
            return
        if running > upper:
            return
        if len(chosen) >= min_size and target <= running <= upper:
            found.append(tuple(sorted(chosen)))
            if len(found) >= max_results:
                return
        if len(chosen) >= max_size or position >= count:
            return
        # Nothing left to reach the target with.
        if running + suffix_total[position] < target:
            return

        for next_position in range(position, count):
            nodes += 1
            if nodes > node_budget:
                truncated = True
                return
            if running + suffix_total[next_position] < target:
                return
            index = order[next_position]
            chosen.append(index)
            walk(next_position + 1, chosen, running + amounts[index])
            chosen.pop()
            if truncated or len(found) >= max_results:
                return

    walk(0, [], ZERO)

    unique = sorted(set(found), key=lambda subset: (len(subset), subset))
    return SubsetSearchResult(
        subsets=tuple(unique),
        nodes_visited=nodes,
        truncated=truncated,
    )


def apportion(amount: Decimal, weights: list[Decimal]) -> list[Decimal]:
    """Split ``amount`` across ``weights`` so the parts sum to it exactly.

    Largest-remainder apportionment. Proportional splitting with rounding is
    where a cent goes missing in a combination match, so the remainder is
    handed out explicitly rather than left to chance.
    """
    if not weights:
        raise ValueError("cannot apportion across an empty set")
    weight_total = sum(weights, ZERO)
    if weight_total <= ZERO:
        raise ValueError("apportionment weights must sum to a positive amount")

    cent = Decimal("0.01")
    exact = [amount * weight / weight_total for weight in weights]
    floors = [value.quantize(cent, rounding=ROUND_DOWN) for value in exact]
    shortfall = amount - sum(floors, ZERO)

    remainder_order = sorted(
        range(len(weights)),
        key=lambda i: (-(exact[i] - floors[i]), -weights[i], i),
    )
    steps = int((shortfall / cent).to_integral_value())
    for offset in range(steps):
        floors[remainder_order[offset % len(weights)]] += cent
    return floors
