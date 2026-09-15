"""Money refuses to be approximate."""

from __future__ import annotations

from decimal import Decimal

import pytest

from settle.domain.models import Invoice, Payment
from settle.domain.money import money
from tests.conftest import BASE_DATE


def test_float_is_rejected_rather_than_rounded() -> None:
    """The whole thesis in one assertion."""
    with pytest.raises(TypeError, match="float is not accepted"):
        money(19.99)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("19.9", "19.90"),
        ("19.994", "19.99"),
        ("19.995", "20.00"),
        (20, "20.00"),
        (Decimal("0.1"), "0.10"),
    ],
)
def test_amounts_are_quantised_to_the_cent(value: str | int | Decimal, expected: str) -> None:
    assert money(value) == Decimal(expected)


def test_non_numeric_input_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="not a valid money amount"):
        money("twenty euros")


def test_infinities_are_not_money() -> None:
    with pytest.raises(ValueError, match="finite"):
        money(Decimal("Infinity"))


def test_invoice_amount_must_be_positive() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        Invoice("i1", "c1", "INV-1", BASE_DATE, Decimal("0.00"), "EUR")


def test_outgoing_lines_are_out_of_scope() -> None:
    with pytest.raises(ValueError, match="out of scope"):
        Payment("p1", BASE_DATE, Decimal("-5.00"), "EUR")


@pytest.mark.parametrize("code", ["EU", "EURO", "12A", ""])
def test_currency_must_be_a_three_letter_code(code: str) -> None:
    with pytest.raises(ValueError, match="3-letter code"):
        Invoice("i1", "c1", "INV-1", BASE_DATE, Decimal("10.00"), code)


def test_currency_is_normalised_to_upper_case() -> None:
    assert Invoice("i1", "c1", "INV-1", BASE_DATE, Decimal("10.00"), "eur").currency == "EUR"
