"""Synthetic ledgers and statements, with the answer known by construction.

This is what makes the benchmark possible. Real reconciliation data has no
ground truth you can publish, and hand-labelling enough of it to measure
anything is weeks of work. Generated data has the mapping by construction: the
generator knows which invoices each payment was built to pay, so precision and
recall are exact rather than estimated.

The cost of that is stated plainly in ``docs/EVAL.md``: this is a *model* of
messy reality, not reality. The noise is the noise the author thought to
implement.

Noise is applied in four independent dimensions so results can say which kind
of mess actually hurts:

* **reference** - identifiers reformatted, truncated, mistyped, or deleted
* **amount**    - bank fees deducted, rounding, overpayment
* **structure** - one payment covering several invoices, one invoice split
                  across several payments
* **collision** - same customer, same window, identical amounts
"""

from __future__ import annotations

import argparse
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from settle.domain.models import Invoice, Payment

START = date(2026, 1, 6)
CURRENCY = "EUR"

COMPANY_NAMES = (
    "Acme Kft",
    "Globex Zrt",
    "Initech BV",
    "Umbrella GmbH",
    "Soylent SA",
    "Hooli Oy",
    "Vehement AB",
    "Massive Dynamic Ltd",
    "Wayne Industries",
    "Stark Solutions",
)

# What a customer writes instead of the invoice number, once reference noise
# applies. Each is a real habit, not a random string.
_JUNK_REFERENCES = (
    "",
    "payment",
    "rent",
    "thank you",
    "monthly transfer",
    "invoice",
    "szamla",
    "our ref 8842177",
)


@dataclass(frozen=True, slots=True)
class NoiseProfile:
    """How much of each kind of mess to apply. Each is a probability in [0, 1]."""

    reference: float = 0.0
    amount: float = 0.0
    structure: float = 0.0
    collision: float = 0.0

    @classmethod
    def uniform(cls, level: float) -> NoiseProfile:
        """All four dimensions at the same level."""
        return cls(reference=level, amount=level, structure=level, collision=level)

    @classmethod
    def only(cls, dimension: str, level: float) -> NoiseProfile:
        """One dimension at ``level``, the rest clean."""
        if dimension not in {"reference", "amount", "structure", "collision"}:
            raise ValueError(f"unknown noise dimension: {dimension}")
        return cls(**{dimension: level})

    def describe(self) -> str:
        parts = [
            f"{name}={value:.2f}"
            for name, value in (
                ("ref", self.reference),
                ("amt", self.amount),
                ("struct", self.structure),
                ("coll", self.collision),
            )
            if value
        ]
        return ", ".join(parts) or "clean"


@dataclass(frozen=True, slots=True)
class Case:
    """A generated ledger, statement, and the mapping between them."""

    invoices: list[Invoice]
    payments: list[Payment]
    #: payment id -> the invoice ids that payment was built to pay.
    truth: dict[str, frozenset[str]] = field(default_factory=dict)

    @property
    def true_pairs(self) -> set[tuple[str, str]]:
        """Every (payment, invoice) pair the generator intended."""
        return {
            (payment_id, invoice_id)
            for payment_id, invoice_ids in self.truth.items()
            for invoice_id in invoice_ids
        }


def _money(value: Decimal | float | int) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _corrupt_reference(number: str, rng: random.Random) -> str:  # noqa: PLR0911
    """Turn a clean invoice number into something a human might have typed."""
    style = rng.choice(["reformat", "slash", "bare", "truncate", "typo", "prefix", "junk", "junk"])
    digits = [part for part in number.replace("-", " ").split() if part.isdigit()]
    year, sequence = [*digits, "2026", "1"][:2]

    match style:
        case "reformat":
            return f"{year}/{sequence}"
        case "slash":
            return f"{sequence}/{year}"
        case "bare":
            return f"#{int(sequence)}"
        case "truncate":
            return number[: max(4, len(number) - 2)]
        case "typo":
            if len(sequence) < 2:
                return f"szamla {sequence}"
            index = rng.randrange(len(sequence))
            swapped = list(sequence)
            swapped[index] = str((int(swapped[index]) + 1) % 10)
            return f"INV-{year}-{''.join(swapped)}"
        case "prefix":
            return f"szamla {int(sequence)}"
        case _:
            return rng.choice(_JUNK_REFERENCES)


def _apply_amount_noise(amount: Decimal, rng: random.Random) -> tuple[Decimal, str]:
    """Deduct a fee, round, or overpay. Returns the new amount and what happened."""
    style = rng.choice(["fee", "fee", "rounding", "overpay"])
    match style:
        case "fee":
            # A correspondent bank taking a cut, typically well under 3%.
            rate = Decimal(str(rng.uniform(0.004, 0.029)))
            return _money(amount - amount * rate), "fee"
        case "rounding":
            return _money(amount - Decimal(str(rng.choice([0.01, 0.02, 0.05])))), "rounding"
        case _:
            return _money(amount + Decimal(str(rng.uniform(0.5, 40.0)))), "overpay"


def generate(
    *,
    seed: int = 0,
    invoice_count: int = 240,
    customer_count: int = 10,
    noise: NoiseProfile | None = None,
) -> Case:
    """Build a ledger and a statement that pays it, with the mapping recorded.

    The same seed and the same profile always produce the same case, which is
    what lets the benchmark be re-run and diffed.
    """
    noise = noise or NoiseProfile()
    rng = random.Random(seed)
    customers = [
        (f"c{index}", COMPANY_NAMES[index % len(COMPANY_NAMES)]) for index in range(customer_count)
    ]

    invoices: list[Invoice] = []
    recent_amounts: dict[str, list[Decimal]] = {customer_id: [] for customer_id, _ in customers}

    for index in range(invoice_count):
        customer_id, customer_name = rng.choice(customers)
        issued = START + timedelta(days=rng.randrange(0, 90))

        seen = recent_amounts[customer_id]
        if seen and rng.random() < noise.collision:
            # Collision noise: the same customer billed the same amount again,
            # which is the case no amount-only matcher can resolve.
            amount = rng.choice(seen)
        else:
            amount = _money(rng.choice([1, 1, 1, 10]) * rng.uniform(45.0, 4000.0))
        seen.append(amount)

        invoices.append(
            Invoice(
                id=f"i{index:04d}",
                customer_id=customer_id,
                number=f"INV-2026-{index:04d}",
                issue_date=issued,
                amount=amount,
                currency=CURRENCY,
                customer_name=customer_name,
            )
        )

    groups = _build_groups(invoices, rng, noise)

    payments: list[Payment] = []
    truth: dict[str, frozenset[str]] = {}
    for index, group in enumerate(groups):
        members, fraction = group
        amount = sum((invoice.amount for invoice in members), Decimal("0.00"))
        if fraction != 1:
            amount = _money(amount * Decimal(str(fraction)))
        if amount <= Decimal("0.00"):
            continue

        if rng.random() < noise.amount:
            amount, _ = _apply_amount_noise(amount, rng)
            if amount <= Decimal("0.00"):
                continue

        reference = " ".join(invoice.number for invoice in members)
        if rng.random() < noise.reference:
            reference = " ".join(_corrupt_reference(invoice.number, rng) for invoice in members)

        latest_issue = max(invoice.issue_date for invoice in members)
        payment_id = f"p{index:04d}"
        payments.append(
            Payment(
                id=payment_id,
                date=latest_issue + timedelta(days=rng.randrange(3, 45)),
                amount=amount,
                currency=CURRENCY,
                reference=reference,
                counterparty=members[0].customer_name,
            )
        )
        truth[payment_id] = frozenset(invoice.id for invoice in members)

    payments.sort(key=lambda p: (p.date, p.id))
    return Case(invoices=invoices, payments=payments, truth=truth)


def _build_groups(
    invoices: list[Invoice], rng: random.Random, noise: NoiseProfile
) -> list[tuple[list[Invoice], float]]:
    """Decide which invoices each payment covers, and how much of them.

    Returns ``(invoices, fraction)`` pairs: fraction 1 means the payment covers
    the whole group, less than 1 means it is one instalment of a split.
    """
    by_customer: dict[str, list[Invoice]] = {}
    for invoice in invoices:
        by_customer.setdefault(invoice.customer_id, []).append(invoice)

    groups: list[tuple[list[Invoice], float]] = []
    for customer_invoices in by_customer.values():
        pending = list(customer_invoices)
        while pending:
            invoice = pending.pop(0)
            roll = rng.random()

            if roll < noise.structure / 2 and pending:
                # Merge: one transfer settles several invoices at once.
                size = min(rng.randrange(1, 3), len(pending))
                members = [invoice, *[pending.pop(0) for _ in range(size)]]
                groups.append((members, 1))
            elif roll < noise.structure:
                # Split: the customer pays two thirds now and the rest later.
                first = rng.choice([0.5, 0.6, 0.667])
                groups.append(([invoice], first))
                groups.append(([invoice], round(1 - first, 3)))
            else:
                groups.append(([invoice], 1))
    return groups


def _write(case: Case, directory: Path) -> None:
    from settle.io import csv_io

    directory.mkdir(parents=True, exist_ok=True)
    csv_io.write_invoices(directory / "invoices.csv", case.invoices)
    csv_io.write_payments(directory / "bank.csv", case.payments)
    truth_path = directory / "truth.csv"
    with truth_path.open("w", encoding="utf-8") as handle:
        handle.write("payment_id,invoice_ids\n")
        for payment_id in sorted(case.truth):
            handle.write(f"{payment_id},{' '.join(sorted(case.truth[payment_id]))}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a synthetic ledger and statement.")
    parser.add_argument("--out", type=Path, default=Path("demo"), help="Output directory.")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--invoices", type=int, default=240)
    parser.add_argument("--customers", type=int, default=10)
    parser.add_argument(
        "--noise", type=float, default=0.0, help="Apply this level to all four dimensions."
    )
    args = parser.parse_args()

    case = generate(
        seed=args.seed,
        invoice_count=args.invoices,
        customer_count=args.customers,
        noise=NoiseProfile.uniform(args.noise),
    )
    _write(case, args.out)
    print(
        f"wrote {len(case.invoices)} invoices and {len(case.payments)} payments "
        f"to {args.out} (noise: {NoiseProfile.uniform(args.noise).describe()})"
    )


if __name__ == "__main__":
    main()
