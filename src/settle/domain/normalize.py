"""Deterministic reference normalisation.

A payment reference is free text. Customers write ``INV-2026-0042``,
``2026/0042``, ``0042/2026``, ``szamla 42``, ``#42``, or their dog's name. This
module handles every format that can be decided by rule, and nothing else: what
it cannot decide it returns nothing for, so the model tier in
:mod:`settle.parse.model` has a clean, small residue to work on.

The output is a :class:`RefToken`, a (sequence number, optional year) pair. That
is the shape an invoice number actually has once the decoration is stripped, and
comparing two of them is exact rather than fuzzy.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Final

from rapidfuzz import fuzz

#: Four-digit runs in this range are read as years, not sequence numbers.
YEAR_MIN: Final = 1990
YEAR_MAX: Final = 2100
YEAR_DIGITS: Final = 4

#: A run of 5 or more digits may still be a year glued to a sequence number,
#: e.g. ``20260042``. Shorter than that and there is nothing to split.
MIN_GLUED_LENGTH: Final = YEAR_DIGITS + 1

#: Digit runs longer than this are bank account numbers, IBAN fragments or
#: card tails, not invoice numbers. Reading them as references produces
#: confident nonsense, so they are dropped.
MAX_REF_DIGITS: Final = 8

#: Separators that still join two digit runs into one reference.
_JOINERS: Final = frozenset("-/._ ")

#: Markers that tend to precede an invoice identifier, in the languages this
#: engine has been exercised against. Presence raises a token's rank; absence
#: never disqualifies one.
_KEYWORDS: Final = (
    "INVOICE",
    "INV",
    "SZAMLA",
    "SZLA",
    "FAKTURA",
    "RECHNUNG",
    "FACTURE",
    "FATTURA",
    "REFERENCE",
    "REF",
    "BILL",
    "NR",
    "NO",
    "#",
)

_KEYWORD_LOOKBEHIND: Final = 14

_DIGIT_RUN = re.compile(r"\d+")


@dataclass(frozen=True, slots=True, order=True)
class RefToken:
    """A normalised invoice identifier: a sequence number and maybe a year."""

    seq: int
    year: int | None = None
    raw: str = ""
    keyworded: bool = False

    def matches(self, other: RefToken) -> bool:
        """True when these two could be the same invoice.

        A missing year is a wildcard: ``szamla 42`` matches invoice 42 of any
        year. When both carry a year they must agree.
        """
        if self.seq != other.seq:
            return False
        if self.year is None or other.year is None:
            return True
        return self.year == other.year

    @property
    def specificity(self) -> int:
        """How much evidence this token carries, for ranking."""
        return (1 if self.year is not None else 0) + (1 if self.keyworded else 0)


def fold_accents(text: str) -> str:
    """Strip diacritics so ``számla`` and ``szamla`` are the same keyword."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def canonical(text: str) -> str:
    """Uppercase alphanumerics only. The string form used for fuzzy comparison."""
    return "".join(ch for ch in fold_accents(text).upper() if ch.isalnum())


def _is_year(value: int, digits: str) -> bool:
    # A leading zero means the writer was padding a sequence number, not
    # writing a year: 0042 is invoice 42, never year 42.
    return (
        len(digits) == YEAR_DIGITS and not digits.startswith("0") and YEAR_MIN <= value <= YEAR_MAX
    )


def _split_glued_year(digits: str) -> tuple[int, int] | None:
    """Split ``20260042`` into (2026, 42); return None when that reading is wrong."""
    if not (MIN_GLUED_LENGTH <= len(digits) <= MAX_REF_DIGITS):
        return None
    head, tail = digits[:YEAR_DIGITS], digits[YEAR_DIGITS:]
    value = int(head)
    if not _is_year(value, head):
        return None
    return value, int(tail)


def _has_keyword_before(text: str, start: int) -> bool:
    window = fold_accents(text[max(0, start - _KEYWORD_LOOKBEHIND) : start]).upper()
    return any(keyword in window for keyword in _KEYWORDS)


def _group_runs(text: str) -> list[list[re.Match[str]]]:
    """Group digit runs that are joined by a single separator into one reference."""
    groups: list[list[re.Match[str]]] = []
    current: list[re.Match[str]] = []
    for run in _DIGIT_RUN.finditer(text):
        if current:
            gap = text[current[-1].end() : run.start()]
            if len(gap) == 1 and gap in _JOINERS:
                current.append(run)
                continue
            groups.append(current)
        current = [run]
    if current:
        groups.append(current)
    return groups


def _token_from_group(text: str, group: list[re.Match[str]]) -> RefToken | None:
    digit_runs = [m.group() for m in group if len(m.group()) <= MAX_REF_DIGITS]
    if not digit_runs:
        return None

    raw = text[group[0].start() : group[-1].end()]
    keyworded = _has_keyword_before(text, group[0].start())

    years = [int(d) for d in digit_runs if _is_year(int(d), d)]
    others = [int(d) for d in digit_runs if not _is_year(int(d), d)]

    if years and others:
        return RefToken(seq=others[0], year=years[0], raw=raw, keyworded=keyworded)
    if len(digit_runs) == 1:
        only = digit_runs[0]
        glued = _split_glued_year(only)
        if glued is not None:
            year, seq = glued
            return RefToken(seq=seq, year=year, raw=raw, keyworded=keyworded)
        # A lone year-shaped number is read as a sequence number: invoice 2026
        # is a real thing, and a bare year is not a reference to anything.
        return RefToken(seq=int(only), year=None, raw=raw, keyworded=keyworded)
    if others:
        return RefToken(seq=others[0], year=None, raw=raw, keyworded=keyworded)
    # Several year-shaped runs and nothing else: take the first as sequence.
    return RefToken(seq=years[0], year=None, raw=raw, keyworded=keyworded)


def extract_refs(text: str) -> tuple[RefToken, ...]:
    """Every plausible invoice identifier in ``text``, best evidence first.

    Ordering is deterministic: more specific tokens (year present, keyword
    present) come first, then by position in the text. Returns an empty tuple
    when the text carries no usable identifier, which is a clean fall-through
    rather than a guess.
    """
    if not text:
        return ()
    seen: dict[tuple[int, int | None], RefToken] = {}
    order: dict[tuple[int, int | None], int] = {}
    for position, group in enumerate(_group_runs(text)):
        token = _token_from_group(text, group)
        if token is None:
            continue
        key = (token.seq, token.year)
        if key not in seen:
            seen[key] = token
            order[key] = position
        elif token.specificity > seen[key].specificity:
            seen[key] = token
    return tuple(
        sorted(seen.values(), key=lambda t: (-t.specificity, order[(t.seq, t.year)], t.seq))
    )


#: Below this length a canonical invoice number is too short to fuzzy-match
#: safely: "42" scores a perfect partial ratio against "INVOICE 1042".
MIN_FUZZY_LENGTH: Final = 5


def similarity(invoice_number: str, payment_reference: str) -> int:
    """Canonicalised 0-100 similarity of an invoice number to a reference.

    Returns 0 for numbers too short to compare safely, so the caller falls back
    to exact token matching rather than to a confident coincidence.
    """
    needle = canonical(invoice_number)
    haystack = canonical(payment_reference)
    if len(needle) < MIN_FUZZY_LENGTH or not haystack:
        return 0
    return int(fuzz.partial_ratio(needle, haystack))


def normalize_invoice_number(number: str) -> RefToken | None:
    """Normalise an invoice's own number, e.g. ``INV-2026-0042`` -> seq 42, year 2026.

    Returns ``None`` for numbers with no usable digits, which the engine treats
    as "this invoice cannot be matched by reference".
    """
    tokens = extract_refs(number)
    if not tokens:
        return None
    # An invoice number is a single identifier, so prefer the token that used
    # the most of it rather than the most decorated one.
    return max(tokens, key=lambda t: (len(t.raw), t.specificity))
