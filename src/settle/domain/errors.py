"""Domain-level exceptions.

These are raised by the pure domain layer and carry no HTTP or CLI meaning.
The API and CLI translate them at their own boundary.
"""

from __future__ import annotations


class SettleError(Exception):
    """Base class for every error the engine raises deliberately."""


class LedgerError(SettleError):
    """The input ledger or statement is not usable as given."""


class DuplicateIdError(LedgerError):
    """Two invoices, or two payments, share an id.

    Not tolerated: identity is what makes a run idempotent, and silently
    de-duplicating would hide a broken export upstream.
    """


class InvariantViolation(SettleError):  # noqa: N818 - the domain calls it a violation
    """A result broke a money invariant.

    Raised by :mod:`settle.domain.invariants`. Reaching this in production
    means the engine produced an unsafe answer and the run must be discarded,
    not patched up.
    """
