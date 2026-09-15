"""Ports: what the domain needs from the outside, expressed as Protocols.

The port lives in the domain and the adapters live in :mod:`settle.parse`, so
the dependency arrow points inward and the domain imports nothing outward. That
is why the model-backed parser can be deleted from the repository without the
matcher noticing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .normalize import RefToken, extract_refs


@dataclass(frozen=True, slots=True)
class ParseOutcome:
    """What a parser made of one free-text reference."""

    tokens: tuple[RefToken, ...]
    #: Which tier produced the tokens, e.g. ``"rules"`` or ``"model:claude"``.
    #: Carried through to the logs so every allocation can be traced to the
    #: thing that proposed it.
    source: str = "rules"
    #: Model calls made while producing this outcome. Zero for every
    #: deterministic tier; the run report sums it.
    model_calls: int = 0


@runtime_checkable
class ReferenceParser(Protocol):
    """Turns a free-text payment reference into candidate invoice identifiers.

    A parser only ever *proposes*. It has no access to the ledger and its
    output is verified against real invoices by the matcher before it can
    affect a single cent.
    """

    @property
    def name(self) -> str: ...

    def parse(self, text: str) -> ParseOutcome: ...


def default_parse(text: str) -> ParseOutcome:
    """The built-in deterministic parse, used when no parser is injected."""
    return ParseOutcome(tokens=extract_refs(text), source="rules")
