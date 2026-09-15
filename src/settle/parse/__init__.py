"""Reference parsers: adapters for the domain's :class:`ReferenceParser` port.

``RuleReferenceParser`` is deterministic and always available.
``ModelReferenceParser`` needs the optional ``anthropic`` extra and is imported
lazily, so the package works without it installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from settle.domain.ports import ParseOutcome, ReferenceParser

from .base import ChainedParser
from .rules import RuleReferenceParser

if TYPE_CHECKING:  # pragma: no cover
    from .model import ModelReferenceParser

__all__ = [
    "ChainedParser",
    "ModelReferenceParser",
    "ParseOutcome",
    "ReferenceParser",
    "RuleReferenceParser",
    "default_parser",
]


def __getattr__(name: str) -> Any:
    if name == "ModelReferenceParser":
        from .model import ModelReferenceParser

        return ModelReferenceParser
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def default_parser(*, use_model: bool = False) -> ReferenceParser:
    """The rules tier, optionally with the model tier behind it."""
    if not use_model:
        return RuleReferenceParser()
    from .model import ModelReferenceParser

    return ChainedParser([RuleReferenceParser(), ModelReferenceParser()])
