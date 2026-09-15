"""A browser interface for the settle reconciliation engine.

This package is a *caller* of :mod:`settle`, never an extension of it. It holds
everything the engine deliberately refuses to hold — a database, sessions,
uploaded files, HTML — so that the engine can go on being a pure function of
``(ledger, statement, policy)``.

The rule that keeps the two apart: nothing under ``settle/`` imports anything
under ``settle_app/``. ``app/tests/test_app_separation.py`` checks it rather
than trusting it.
"""

from __future__ import annotations

APP_VERSION = "0.1.0"

__all__ = ["APP_VERSION"]
