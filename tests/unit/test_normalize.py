"""Reference normalisation: the formats it decides, and the ones it declines to."""

from __future__ import annotations

import pytest

from settle.domain.normalize import (
    canonical,
    extract_refs,
    normalize_invoice_number,
    similarity,
)


@pytest.mark.parametrize(
    ("text", "seq", "year"),
    [
        ("INV-2026-0042", 42, 2026),
        ("2026/0042", 42, 2026),
        ("0042/2026", 42, 2026),
        ("szamla 42", 42, None),
        ("számla 42", 42, None),
        ("#42", 42, None),
        ("payment for INV-2026-0042 thank you", 42, 2026),
        ("Rechnung Nr. 0042/2026", 42, 2026),
        ("20260042", 42, 2026),
        ("INV2026042", 42, 2026),
        ("no. 42", 42, None),
    ],
)
def test_the_listed_formats_all_resolve(text: str, seq: int, year: int | None) -> None:
    tokens = extract_refs(text)
    assert tokens, f"expected a reference in {text!r}"
    assert (tokens[0].seq, tokens[0].year) == (seq, year)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "rent",
        "thanks for everything",
        "payment",
        "HU42117730161111101800000000",  # an account number, not a reference
    ],
)
def test_undecidable_text_falls_through_cleanly(text: str) -> None:
    """No guessing. An empty result is what hands the case to the model tier."""
    assert extract_refs(text) == ()


def test_leading_zeros_are_never_read_as_a_year() -> None:
    """0042 is invoice 42, not year 42, whichever side of the slash it is on."""
    assert extract_refs("0042/2026")[0].year == 2026
    assert extract_refs("2026/0042")[0].year == 2026


def test_a_bare_year_is_read_as_a_sequence_number() -> None:
    """Invoice 2026 exists; a reference to the year 2026 alone does not."""
    token = extract_refs("invoice 2026")[0]
    assert (token.seq, token.year) == (2026, None)


def test_several_references_are_all_returned() -> None:
    tokens = extract_refs("ref 42 and 43")
    assert [t.seq for t in tokens] == [42, 43]


def test_keyworded_references_outrank_bare_numbers() -> None:
    """A number next to the word 'invoice' is better evidence than a loose one."""
    tokens = extract_refs("order 7 invoice 2026/0042")
    assert (tokens[0].seq, tokens[0].year) == (42, 2026)


def test_a_missing_year_matches_any_year() -> None:
    loose = extract_refs("szamla 42")[0]
    specific = normalize_invoice_number("INV-2026-0042")
    assert specific is not None
    assert loose.matches(specific)


def test_years_that_disagree_do_not_match() -> None:
    a = extract_refs("INV-2025-0042")[0]
    b = normalize_invoice_number("INV-2026-0042")
    assert b is not None
    assert not a.matches(b)


def test_invoice_numbers_without_digits_cannot_be_matched_by_reference() -> None:
    assert normalize_invoice_number("ACME/OPENING-BALANCE") is None


def test_canonical_folds_accents_and_punctuation() -> None:
    assert canonical("Számla-2026/0042") == "SZAMLA20260042"


def test_short_numbers_are_not_fuzzy_matched() -> None:
    """'42' scores a perfect partial ratio inside '1042'; refuse to play."""
    assert similarity("42", "invoice 1042") == 0


def test_fuzzy_matching_survives_a_typo() -> None:
    assert similarity("INV-2026-0042", "paid inv 2026 0043 sorry") < 100
    assert similarity("INV-2026-0042", "payment ref INV2026-0042 thanks") == 100
