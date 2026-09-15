"""The model tier: the one place in this engine where a model is used at all.

It is used here because this is the one input that is genuinely unstructured —
a free-text field a human typed. Everything else about reconciliation is a
fact, and facts are matched with rules and search.

Three constraints keep it honest, and they are the point of the design:

1. **It only proposes.** The return value is a list of candidate invoice
   identifiers. It never sees the ledger, never picks an invoice, and never
   touches an amount. The matcher verifies every proposal against real open
   invoices before a cent moves.
2. **It is last.** :class:`~settle.parse.base.ChainedParser` calls it only for
   references the deterministic tier could not read, so a clean statement
   costs nothing.
3. **It can be removed.** Deleting this file degrades recall on messy
   references and changes no other behaviour. The invariants in
   :mod:`settle.domain.invariants` hold with it, without it, and when it
   returns nonsense.

Every call logs its token usage, so the cost of the model tier is a number in
the run report rather than a surprise on an invoice.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Final

import structlog
from pydantic import BaseModel, Field

from settle.domain.normalize import MAX_REF_DIGITS, YEAR_MAX, YEAR_MIN, RefToken
from settle.domain.ports import ParseOutcome

if TYPE_CHECKING:  # pragma: no cover
    from anthropic import Anthropic

log = structlog.get_logger("settle.parse.model")

DEFAULT_MODEL: Final = "claude-opus-4-8"
DEFAULT_MAX_TOKENS: Final = 512
DEFAULT_CACHE_SIZE: Final = 1024

SYSTEM_PROMPT: Final = """\
You extract invoice identifiers from bank payment references.

A payment reference is free text a customer typed. It may contain an invoice \
number in any format, in any language, or none at all.

Return every invoice identifier you can find, as a sequence number and an \
optional year. Examples of the reading you should apply:

  "INV-2026-0042"        -> sequence 42, year 2026
  "szamla negyvenketto"  -> sequence 42, no year
  "the march bill, no 7" -> sequence 7, no year
  "rent"                 -> nothing

Rules:
- Return an empty list when the text contains no invoice identifier. Guessing \
is worse than returning nothing: a wrong identifier wastes a human's time, and \
an empty list simply means the payment is matched on amount and customer instead.
- Never invent a year that is not implied by the text.
- Do not return bank account numbers, card numbers, dates, or amounts.
"""


class ExtractedReference(BaseModel):
    """One invoice identifier the model believes it found."""

    sequence_number: int = Field(description="The invoice's number, without padding.")
    year: int | None = Field(
        default=None, description="The invoice's year, only if the text implies one."
    )


class ReferenceExtraction(BaseModel):
    """The model's complete answer for one reference."""

    references: list[ExtractedReference] = Field(
        default_factory=list, description="Empty when the text names no invoice."
    )


class ModelReferenceParser:
    """Model-backed reference parsing, behind the same interface as the rules.

    Args:
        client: an ``anthropic.Anthropic`` instance. Constructed from the
            environment when omitted, so the caller does not have to.
        model: model id. See :data:`DEFAULT_MODEL`.
        max_tokens: output cap. The answer is a short list; 512 is generous.
        cache_size: how many distinct reference strings to remember. Statements
            repeat references constantly (standing orders, instalments), so this
            removes most of the calls a naive implementation would make.
    """

    def __init__(
        self,
        *,
        client: Anthropic | None = None,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        cache_size: int = DEFAULT_CACHE_SIZE,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._cache_size = max(0, cache_size)
        self._cache: OrderedDict[str, tuple[RefToken, ...]] = OrderedDict()
        self._client = client if client is not None else _build_client()

    @property
    def name(self) -> str:
        return f"model:{self._model}"

    def parse(self, text: str) -> ParseOutcome:
        """Propose invoice identifiers for ``text``. Never raises."""
        stripped = text.strip()
        if not stripped:
            return ParseOutcome(tokens=(), source=self.name)

        cached = self._cache.get(stripped)
        if cached is not None:
            self._cache.move_to_end(stripped)
            return ParseOutcome(tokens=cached, source=f"{self.name}:cached")

        try:
            tokens = self._extract(stripped)
        except Exception as exc:
            # A model outage is a recall problem, never a correctness problem:
            # the run continues on deterministic evidence alone.
            log.warning(
                "model_parser_unavailable",
                model=self._model,
                error=type(exc).__name__,
                detail=str(exc),
            )
            return ParseOutcome(tokens=(), source=f"{self.name}:error", model_calls=1)

        self._remember(stripped, tokens)
        return ParseOutcome(tokens=tokens, source=self.name, model_calls=1)

    def _extract(self, text: str) -> tuple[RefToken, ...]:
        response = self._client.messages.parse(
            model=self._model,
            max_tokens=self._max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"Payment reference: {text}"}],
            output_format=ReferenceExtraction,
        )
        usage = response.usage
        log.info(
            "model_parser_call",
            model=self._model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0),
            cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0),
            stop_reason=response.stop_reason,
        )
        if response.stop_reason == "refusal":
            log.warning("model_parser_refused", model=self._model)
            return ()

        extraction = response.parsed_output
        if extraction is None:
            return ()
        return _to_tokens(extraction, text)

    def _remember(self, text: str, tokens: tuple[RefToken, ...]) -> None:
        if self._cache_size == 0:
            return
        self._cache[text] = tokens
        self._cache.move_to_end(text)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)


def _build_client() -> Any:
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - exercised by install shape
        raise RuntimeError(
            "the model tier needs the anthropic SDK: pip install 'settle-engine[model]'. "
            "The engine runs without it; only messy-reference recall is affected."
        ) from exc
    return anthropic.Anthropic()


def _to_tokens(extraction: ReferenceExtraction, raw: str) -> tuple[RefToken, ...]:
    """Convert the model's answer into domain tokens, discarding the implausible.

    This is the first of two checks. Here we drop values that could not be an
    invoice number at all; the matcher then drops everything that does not
    correspond to a real open invoice for the right customer and amount. The
    model's output is a hypothesis until both have passed.
    """
    tokens: list[RefToken] = []
    seen: set[tuple[int, int | None]] = set()
    for reference in extraction.references:
        sequence = reference.sequence_number
        if sequence <= 0 or len(str(sequence)) > MAX_REF_DIGITS:
            continue
        year = reference.year
        if year is not None and not (YEAR_MIN <= year <= YEAR_MAX):
            year = None
        key = (sequence, year)
        if key in seen:
            continue
        seen.add(key)
        tokens.append(RefToken(seq=sequence, year=year, raw=raw, keyworded=False))
    return tuple(tokens)
