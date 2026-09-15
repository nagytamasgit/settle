"""The deterministic tier: a thin adapter over the domain's normaliser.

There is no logic here. Reference normalisation is a domain concern and lives
in :mod:`settle.domain.normalize`; this class only presents it as a
:class:`~settle.domain.ports.ReferenceParser` so it can be composed with, and
compared against, the model tier.
"""

from __future__ import annotations

from settle.domain.normalize import extract_refs
from settle.domain.ports import ParseOutcome


class RuleReferenceParser:
    """Deterministic reference extraction. Free, instant, and always first."""

    @property
    def name(self) -> str:
        return "rules"

    def parse(self, text: str) -> ParseOutcome:
        return ParseOutcome(tokens=extract_refs(text), source="rules")
