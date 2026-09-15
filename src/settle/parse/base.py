"""The reference-parser interface, and the tier chain that composes parsers.

The :class:`ReferenceParser` protocol itself lives in
:mod:`settle.domain.ports`, so the domain depends on nothing outward. This
module re-exports it as the public name adapters implement, and adds
:class:`ChainedParser`, which is how the deterministic and model tiers are
actually wired together: rules first, model only for what rules could not
decide.
"""

from __future__ import annotations

from collections.abc import Sequence

from settle.domain.ports import ParseOutcome, ReferenceParser

__all__ = ["ChainedParser", "ParseOutcome", "ReferenceParser"]


class ChainedParser:
    """Try each parser in order and return the first that finds anything.

    This ordering is the whole cost-control story for the model tier: a
    reference the deterministic parser can read never reaches the model, so on
    a clean statement the model is never called at all. The benchmark reports
    how often that holds.
    """

    def __init__(self, parsers: Sequence[ReferenceParser]) -> None:
        if not parsers:
            raise ValueError("a chained parser needs at least one tier")
        self._parsers = tuple(parsers)

    @property
    def name(self) -> str:
        return " -> ".join(parser.name for parser in self._parsers)

    def parse(self, text: str) -> ParseOutcome:
        calls = 0
        last = ParseOutcome(tokens=(), source=self.name)
        for parser in self._parsers:
            outcome = parser.parse(text)
            calls += outcome.model_calls
            if outcome.tokens:
                return ParseOutcome(tokens=outcome.tokens, source=outcome.source, model_calls=calls)
            last = outcome
        return ParseOutcome(tokens=(), source=last.source, model_calls=calls)
