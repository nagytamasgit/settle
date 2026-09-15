"""Money is ``Decimal``, quantised to the cent, and never ``float``.

Every amount that enters the domain passes through :func:`money`. It rejects
``float`` outright rather than rounding it, because a float that reached this
point is a bug upstream, and silently accepting it is how cents go missing.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Final

CENT: Final = Decimal("0.01")
ZERO: Final = Decimal("0.00")

MoneyInput = Decimal | str | int


def money(value: MoneyInput) -> Decimal:
    """Coerce ``value`` to a cent-quantised :class:`Decimal`.

    Raises:
        TypeError: if ``value`` is a ``float``. Use ``str(value)`` at the
            boundary if you genuinely have one; the domain will not guess.
        ValueError: if ``value`` is not a parseable decimal.
    """
    if isinstance(value, float):
        raise TypeError(
            f"float is not accepted as money (got {value!r}); "
            "pass a Decimal, str or int so the value is exact"
        )
    if isinstance(value, Decimal):
        candidate = value
    else:
        try:
            candidate = Decimal(value)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"not a valid money amount: {value!r}") from exc
    if not candidate.is_finite():
        raise ValueError(f"money must be finite, got {value!r}")
    return candidate.quantize(CENT, rounding=ROUND_HALF_UP)


def is_cent_quantised(value: Decimal) -> bool:
    """True when ``value`` carries no sub-cent precision."""
    return value == value.quantize(CENT, rounding=ROUND_HALF_UP)


def total(amounts: Iterable[Decimal]) -> Decimal:
    """Sum an iterable of amounts, returning cent-quantised zero when empty."""
    acc = ZERO
    for amount in amounts:
        acc += amount
    return acc.quantize(CENT, rounding=ROUND_HALF_UP)
