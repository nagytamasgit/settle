"""Validated boundaries. The same schemas serve the CLI, the API and the benchmark.

The one rule worth stating explicitly: **a monetary amount arriving as a JSON
float is rejected, not rounded.** ``1000.10`` parsed as a float and coerced to
Decimal becomes ``1000.099999999999994315658113919198513031005859375``. That is
precisely the bug this engine exists to avoid, and the boundary is the right
place to stop it, so amounts must be sent as strings (or integers).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from settle.domain.config import AmbiguityPolicy, MatchConfig, TieBreak
from settle.domain.models import Invoice, Payment

_MONEY_HELP = (
    'send money as a string ("1000.10") or an integer; a JSON float cannot '
    "represent most decimal amounts exactly"
)


def _reject_float(value: Any) -> Any:
    if isinstance(value, float):
        raise ValueError(f"{value!r} arrived as a float: {_MONEY_HELP}")
    return value


class _Row(BaseModel):
    # Real exports carry extra columns; unknown ones are ignored rather than
    # rejected, so a ledger export does not have to be trimmed to be usable.
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class InvoiceRow(_Row):
    """One row of the invoice ledger."""

    id: str
    customer_id: str
    number: str
    issue_date: date
    amount: Decimal
    currency: str
    customer_name: str = ""
    due_date: date | None = None

    _no_float_amount = field_validator("amount", mode="before")(_reject_float)

    @field_validator("due_date", mode="before")
    @classmethod
    def _blank_is_none(cls, value: Any) -> Any:
        return None if value in ("", None) else value

    def to_domain(self) -> Invoice:
        return Invoice(
            id=self.id,
            customer_id=self.customer_id,
            number=self.number,
            issue_date=self.issue_date,
            amount=self.amount,
            currency=self.currency,
            customer_name=self.customer_name,
            due_date=self.due_date,
        )


class PaymentRow(_Row):
    """One line of the bank statement."""

    id: str
    date: date
    amount: Decimal
    currency: str
    reference: str = ""
    counterparty: str = ""
    customer_id: str | None = None

    _no_float_amount = field_validator("amount", mode="before")(_reject_float)

    @field_validator("customer_id", mode="before")
    @classmethod
    def _blank_is_none(cls, value: Any) -> Any:
        return None if value in ("", None) else value

    def to_domain(self) -> Payment:
        return Payment(
            id=self.id,
            date=self.date,
            amount=self.amount,
            currency=self.currency,
            reference=self.reference,
            counterparty=self.counterparty,
            customer_id=self.customer_id,
        )


class ConfigModel(BaseModel):
    """Run policy, as it crosses the API and CLI boundary.

    Mirrors :class:`~settle.domain.config.MatchConfig` field for field. It is a
    separate type on purpose: the domain object must not grow a dependency on
    Pydantic just because one of its callers speaks HTTP.
    """

    model_config = ConfigDict(extra="forbid")

    date_window_days_after: int = Field(default=120, ge=0)
    date_window_days_before: int = Field(default=5, ge=0)
    fee_tolerance_pct: Decimal = Decimal("0.03")
    fee_tolerance_abs: Decimal = Decimal("1.00")
    fee_tolerance_cap: Decimal | None = None
    confidence_threshold: Decimal = Decimal("0.80")
    max_combination_size: int = Field(default=4, ge=2)
    max_combination_candidates: int = Field(default=24, ge=1)
    search_node_budget: int = Field(default=50_000, ge=1)
    max_combinations_returned: int = Field(default=16, ge=1)
    tie_break: TieBreak = TieBreak.OLDEST_FIRST
    ambiguity_policy: AmbiguityPolicy = AmbiguityPolicy.REVIEW
    allow_partial: bool = True
    allow_overpayment: bool = True
    allow_combinations: bool = True
    require_customer_match: bool = True
    fuzzy_ref_threshold: int = Field(default=88, ge=0, le=100)
    verify_invariants: bool = True

    _no_float_tolerances = field_validator(
        "fee_tolerance_pct",
        "fee_tolerance_abs",
        "fee_tolerance_cap",
        "confidence_threshold",
        mode="before",
    )(_reject_float)

    def to_domain(self) -> MatchConfig:
        return MatchConfig(**self.model_dump())


class ReconcileRequest(BaseModel):
    """The body of ``POST /reconcile``."""

    model_config = ConfigDict(extra="forbid")

    invoices: list[InvoiceRow]
    payments: list[PaymentRow]
    config: ConfigModel = Field(default_factory=ConfigModel)
