"""Working out which of your columns is which.

Nobody's accounting system exports settle's column names. Xero says
``Invoice Number``, QuickBooks says ``Num``, a Hungarian system says
``szamlaszam``, and somebody's spreadsheet says ``Inv #``. Requiring people to
rename columns before uploading would make the app useless for its actual
audience.

Three tiers, in cost order. An alias table handles the names that recur, a fuzzy
pass catches near-misses, and only what is left over is worth asking a model
about. The same shape as the engine's reference parser, and for the same reason:
the cheap deterministic thing should answer most of the question, so the
expensive non-deterministic thing is doing the small hard part rather than the
whole job.

Whatever proposes a mapping, a human confirms it before a run happens. There is
no code path that skips that screen.
"""

from __future__ import annotations

import csv
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from rapidfuzz import fuzz

from settle.io.csv_io import INVOICE_COLUMNS, PAYMENT_COLUMNS


class Kind(StrEnum):
    INVOICE = "invoice"
    PAYMENT = "payment"


class Origin(StrEnum):
    """How a column got mapped. Shown on the confirmation screen."""

    ALIAS = "alias"
    FUZZY = "fuzzy"
    MODEL = "model"
    MANUAL = "manual"


#: Without these, the row cannot be built at all. Everything else is optional
#: and mirrors the defaults in settle.io.schemas.
REQUIRED: Final[dict[Kind, tuple[str, ...]]] = {
    Kind.INVOICE: ("id", "customer_id", "number", "issue_date", "amount", "currency"),
    Kind.PAYMENT: ("id", "date", "amount", "currency"),
}

TARGETS: Final[dict[Kind, tuple[str, ...]]] = {
    Kind.INVOICE: INVOICE_COLUMNS,
    Kind.PAYMENT: PAYMENT_COLUMNS,
}

#: Only accept a fuzzy match this good. Set high on purpose: a wrong column
#: silently reconciles the wrong numbers, which is worse than asking.
FUZZY_THRESHOLD: Final = 90
#: And only if it beats the runner-up by this much, so near-ties go to a human.
FUZZY_MARGIN: Final = 8

#: Normalised alias -> canonical column. Written normalised, so "Invoice No."
#: and "invoice_no" and "INVOICE NO" are all one entry.
ALIASES: Final[dict[Kind, dict[str, str]]] = {
    Kind.INVOICE: {
        "invoice id": "id",
        "invoiceid": "id",
        "id": "id",
        "key": "id",
        "reference": "id",
        "customer id": "customer_id",
        "customerid": "customer_id",
        "client id": "customer_id",
        "account": "customer_id",
        "account code": "customer_id",
        "contact id": "customer_id",
        "customer": "customer_name",
        "customer name": "customer_name",
        "client": "customer_name",
        "client name": "customer_name",
        "contact": "customer_name",
        "contact name": "customer_name",
        "partner": "customer_name",
        "vevo": "customer_name",
        "vevo neve": "customer_name",
        "invoice number": "number",
        "invoice no": "number",
        "invoice": "number",
        "number": "number",
        "num": "number",
        "no": "number",
        "doc number": "number",
        "document number": "number",
        "bill number": "number",
        "szamlaszam": "number",
        "issue date": "issue_date",
        "invoice date": "issue_date",
        "date": "issue_date",
        "created": "issue_date",
        "issued": "issue_date",
        "issued on": "issue_date",
        "kelt": "issue_date",
        "due date": "due_date",
        "due": "due_date",
        "payment due": "due_date",
        "hatarido": "due_date",
        "amount": "amount",
        "total": "amount",
        "gross": "amount",
        "gross amount": "amount",
        "total amount": "amount",
        "invoice total": "amount",
        "value": "amount",
        "osszeg": "amount",
        "brutto": "amount",
        "currency": "currency",
        "ccy": "currency",
        "cur": "currency",
        "currency code": "currency",
        "penznem": "currency",
    },
    Kind.PAYMENT: {
        "id": "id",
        "transaction id": "id",
        "transactionid": "id",
        "txn id": "id",
        "bank reference": "id",
        "entry reference": "id",
        "line": "id",
        "date": "date",
        "value date": "date",
        "transaction date": "date",
        "booking date": "date",
        "posted": "date",
        "posting date": "date",
        "datum": "date",
        "konyveles datuma": "date",
        "amount": "amount",
        "credit": "amount",
        "credit amount": "amount",
        "paid in": "amount",
        "value": "amount",
        "osszeg": "amount",
        "currency": "currency",
        "ccy": "currency",
        "cur": "currency",
        "currency code": "currency",
        "penznem": "currency",
        "reference": "reference",
        "payment reference": "reference",
        "remittance information": "reference",
        "remittance info": "reference",
        "narrative": "reference",
        "description": "reference",
        "details": "reference",
        "memo": "reference",
        "kozlemeny": "reference",
        "counterparty": "counterparty",
        "counterparty name": "counterparty",
        "payer": "counterparty",
        "payer name": "counterparty",
        "from": "counterparty",
        "paid by": "counterparty",
        "name": "counterparty",
        "partner": "counterparty",
        "customer id": "customer_id",
        "customerid": "customer_id",
        "client id": "customer_id",
        "account": "customer_id",
    },
}


@dataclass(frozen=True, slots=True)
class Choice:
    """One target column, and where its data would come from."""

    target: str
    source: str
    origin: Origin

    @property
    def resolved(self) -> bool:
        return bool(self.source)


@dataclass(frozen=True, slots=True)
class Proposal:
    """A whole mapping for one file, and whether it is usable as it stands."""

    kind: Kind
    headers: tuple[str, ...]
    choices: tuple[Choice, ...]

    @property
    def unresolved_required(self) -> tuple[str, ...]:
        required = REQUIRED[self.kind]
        return tuple(
            choice.target
            for choice in self.choices
            if choice.target in required and not choice.resolved
        )

    @property
    def is_complete(self) -> bool:
        return not self.unresolved_required

    @property
    def needs_attention(self) -> bool:
        """True when a human has real work to do rather than a rubber stamp."""
        return bool(self.unresolved_required) or any(
            choice.origin is Origin.FUZZY for choice in self.choices if choice.resolved
        )

    def as_dict(self) -> dict[str, str]:
        return {c.target: c.source for c in self.choices if c.resolved}

    def is_required(self, target: str) -> bool:
        return target in REQUIRED[self.kind]


def normalise(header: str) -> str:
    """Fold case, accents, punctuation and camelCase so aliases stay readable.

    ``"Szamlaszám"``, ``"SZAMLASZAM"`` and ``"szamla_szam"`` all reduce to the
    same key, which is what keeps the alias table from needing every spelling.

    camelCase matters more than it looks: Xero exports ``InvoiceDate`` and
    ``ContactName`` as single words, so without splitting them the alias table
    would need a second entry for every name it already has.
    """
    stripped = unicodedata.normalize("NFKD", header)
    without_accents = "".join(ch for ch in stripped if not unicodedata.combining(ch))
    spaced = _split_camel_case(without_accents)
    cleaned = "".join(ch if ch.isalnum() else " " for ch in spaced.lower())
    return " ".join(cleaned.split())


def _split_camel_case(text: str) -> str:
    """``InvoiceDate`` -> ``Invoice Date``; ``ID`` and ``VAT`` stay whole."""
    out: list[str] = []
    for index, char in enumerate(text):
        previous = text[index - 1] if index else ""
        if char.isupper() and (previous.islower() or previous.isdigit()):
            out.append(" ")
        out.append(char)
    return "".join(out)


def propose(headers: Sequence[str], kind: Kind) -> Proposal:
    """Map the file's headers onto settle's columns, deterministically."""
    available = [header for header in headers if header.strip()]
    normalised = {header: normalise(header) for header in available}
    taken: set[str] = set()
    choices: list[Choice] = []

    for target in TARGETS[kind]:
        source = _by_alias(target, kind, normalised, taken)
        origin = Origin.ALIAS
        if source is None:
            source = _by_fuzz(target, normalised, taken)
            origin = Origin.FUZZY
        if source is not None:
            taken.add(source)
            choices.append(Choice(target=target, source=source, origin=origin))
        else:
            choices.append(Choice(target=target, source="", origin=Origin.MANUAL))

    return Proposal(kind=kind, headers=tuple(available), choices=tuple(choices))


def apply_overrides(proposal: Proposal, overrides: dict[str, str]) -> Proposal:
    """Take what the human actually chose on the confirmation screen.

    A target the user cleared becomes unresolved again, and one they picked is
    marked ``MANUAL`` — so the stored record shows a person decided it, not the
    alias table and not a model.
    """
    valid = set(proposal.headers)
    claimed: set[str] = set()
    choices: list[Choice] = []
    for choice in proposal.choices:
        chosen = overrides.get(choice.target, choice.source).strip()
        if chosen and (chosen not in valid or chosen in claimed):
            chosen = ""
        if chosen:
            claimed.add(chosen)
        origin = choice.origin if chosen == choice.source else Origin.MANUAL
        choices.append(Choice(target=choice.target, source=chosen, origin=origin))
    return Proposal(kind=proposal.kind, headers=proposal.headers, choices=tuple(choices))


def read_headers(path: Path) -> tuple[str, ...]:
    """The header row, or empty if the file has none."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.reader(handle):
            return tuple(cell.strip() for cell in row)
    return ()


def to_canonical(source: Path, destination: Path, proposal: Proposal) -> None:
    """Rewrite a file under settle's column names.

    The whole point of this module. After it runs,
    :func:`settle.io.csv_io.read_invoices` and ``read_payments`` work on the
    result exactly as they do for a file that was already canonical — no
    branch in the engine, no second reader, nothing downstream that knows a
    mapping happened.
    """
    mapping = proposal.as_dict()
    targets = [target for target in TARGETS[proposal.kind] if target in mapping]

    with (
        source.open(newline="", encoding="utf-8-sig") as reader_handle,
        destination.open("w", newline="", encoding="utf-8") as writer_handle,
    ):
        reader = csv.DictReader(reader_handle)
        writer = csv.writer(writer_handle)
        writer.writerow(targets)
        for row in reader:
            writer.writerow([(row.get(mapping[target]) or "").strip() for target in targets])


def _by_alias(target: str, kind: Kind, normalised: dict[str, str], taken: set[str]) -> str | None:
    table = ALIASES[kind]
    for header, key in normalised.items():
        if header in taken:
            continue
        if table.get(key) == target:
            return header
    return None


def _by_fuzz(target: str, normalised: dict[str, str], taken: set[str]) -> str | None:
    """Accept a near-miss only when it is both good and unambiguous.

    Two columns scoring 91 and 90 against ``amount`` is exactly the case where
    guessing costs someone a wrong reconciliation, so a close second sends the
    decision to a human instead.
    """
    wanted = target.replace("_", " ")
    scored = sorted(
        (
            (fuzz.token_sort_ratio(wanted, key), header)
            for header, key in normalised.items()
            if header not in taken
        ),
        reverse=True,
    )
    if not scored or scored[0][0] < FUZZY_THRESHOLD:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < FUZZY_MARGIN:
        return None
    return scored[0][1]
