"""Shared builders. Tests should read like the scenario they describe."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal

import pytest
import structlog
from hypothesis import HealthCheck, settings

from settle.domain.models import Invoice, Payment

BASE_DATE = date(2026, 1, 5)

# Two profiles: the default keeps the suite fast enough to run on every save,
# and `thorough` is what CI runs, because an invariant that only breaks on the
# 400th example is still broken.
settings.register_profile(
    "default", max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
settings.register_profile(
    "thorough", max_examples=1000, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
settings.load_profile("default")


@pytest.fixture(autouse=True, scope="session")
def _quiet_logs() -> None:
    """The engine logs every decision; tests do not need to read them."""
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL))


def inv(
    id: str,
    amount: str,
    *,
    customer: str = "c1",
    number: str | None = None,
    day: int = 0,
    currency: str = "EUR",
    customer_name: str = "Acme Kft",
) -> Invoice:
    return Invoice(
        id=id,
        customer_id=customer,
        number=number if number is not None else f"INV-2026-{id.lstrip('i').zfill(4)}",
        issue_date=BASE_DATE + timedelta(days=day),
        amount=Decimal(amount),
        currency=currency,
        customer_name=customer_name,
    )


def pay(
    id: str,
    amount: str,
    *,
    reference: str = "",
    day: int = 20,
    currency: str = "EUR",
    counterparty: str = "Acme Kft",
    customer: str | None = None,
) -> Payment:
    return Payment(
        id=id,
        date=BASE_DATE + timedelta(days=day),
        amount=Decimal(amount),
        currency=currency,
        reference=reference,
        counterparty=counterparty,
        customer_id=customer,
    )
