"""The model tier of the column mapper. Optional, and deletable.

This is the second place in the whole project where a model is used, and it
copies the first one's shape exactly — see ``settle/parse/model.py``. The rules
are the same because the reasoning is the same:

1. **It only proposes.** It returns a header name for a column the deterministic
   tiers could not place. It never reads a value, never touches an amount, and
   never causes a reconciliation on its own. A human confirms the mapping on
   screen before anything runs.
2. **It is last.** Only *required* columns still unresolved after the alias and
   fuzzy tiers are sent, so a normal export never reaches it.
3. **It is verified.** A proposal naming a column that is not in the file, or
   one already claimed, is dropped. The closed set of real headers is the check.
4. **It never raises.** An outage, a refusal or nonsense back all degrade to
   "no proposal", which means the user fills that row in themselves. A model
   problem is a convenience problem here, never a correctness one.

By default it sends **column headers only** — no sample values. Headers are
rarely personal data; the values under them are customer names and payment
references. Sending samples is a separate opt-in for that reason.

Deleting this file removes an import in ``runs_routes`` and nothing else.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import structlog
from pydantic import BaseModel, Field

from settle_app.mapping import REQUIRED, Choice, Origin, Proposal

if TYPE_CHECKING:  # pragma: no cover
    import anthropic

log = structlog.get_logger(__name__)

DEFAULT_MODEL: Final = "claude-opus-4-8"
DEFAULT_MAX_TOKENS: Final = 512
SAMPLE_ROWS: Final = 3

SYSTEM_PROMPT: Final = """\
You match spreadsheet column headers to a fixed set of field names.

You are given the headers of a CSV exported from an accounting or banking
system, and the fields that still need a source column. For each needed field,
choose the header most likely to hold that data, or leave it out.

Rules:
- Only ever return a header exactly as it was given to you.
- Use each header at most once.
- If no header plausibly holds a field, omit that field. Omitting is correct
  and expected; guessing is not.
- Headers may be in any language.

Field meanings:
  id            a unique identifier for the row
  customer_id   a code identifying the customer or account
  customer_name the customer's name
  number        the human-facing invoice number
  issue_date    the date the invoice was issued
  due_date      the date payment is due
  date          the date of the bank transaction
  amount        the monetary amount
  currency      the currency code
  reference     free text the payer wrote on the payment
  counterparty  the name of the party who paid
"""


class ProposedColumn(BaseModel):
    field: str = Field(description="The field name that needs a source column.")
    header: str = Field(description="The header from the file, copied exactly.")


class ColumnMapping(BaseModel):
    columns: list[ProposedColumn] = Field(
        default_factory=list, description="One entry per field you could place."
    )


def fill_gaps(
    proposal: Proposal,
    *,
    send_samples: bool = False,
    source: Path | None = None,
    client: Any = None,
    model: str = DEFAULT_MODEL,
) -> Proposal:
    """Ask a model about required columns the rules could not place.

    Returns the proposal unchanged if there is nothing to ask about, if the
    model is unavailable, or if everything it said failed verification.
    """
    missing = proposal.unresolved_required
    if not missing:
        return proposal

    try:
        backend = client or _build_client()
    except RuntimeError as exc:
        log.warning("model_mapper_unavailable", error=str(exc))
        return proposal

    taken = {choice.source for choice in proposal.choices if choice.resolved}
    candidates = [header for header in proposal.headers if header not in taken]
    if not candidates:
        return proposal

    samples = _samples(source, candidates) if (send_samples and source) else {}
    proposed = _ask(backend, model, missing, candidates, samples)
    if not proposed:
        return proposal

    return _merge(proposal, proposed, candidates, taken)


def _ask(
    client: Any,
    model: str,
    missing: tuple[str, ...],
    candidates: list[str],
    samples: dict[str, list[str]],
) -> dict[str, str]:
    """One call. Every failure path returns an empty mapping."""
    lines = [
        f"Headers in the file: {candidates}",
        f"Fields still needing a column: {list(missing)}",
    ]
    if samples:
        lines.append(f"Example values: {samples}")

    try:
        response = client.messages.parse(
            model=model,
            max_tokens=DEFAULT_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": "\n".join(lines)}],
            output_format=ColumnMapping,
        )
    except Exception as exc:  # an outage must not break a run
        log.warning("model_mapper_failed", error=str(exc))
        return {}

    usage = getattr(response, "usage", None)
    log.info(
        "model_mapper_called",
        model=model,
        fields=len(missing),
        input_tokens=getattr(usage, "input_tokens", 0),
        output_tokens=getattr(usage, "output_tokens", 0),
    )

    if getattr(response, "stop_reason", None) == "refusal":
        return {}
    parsed = getattr(response, "parsed_output", None)
    if parsed is None:
        return {}
    return {entry.field: entry.header for entry in parsed.columns}


def _merge(
    proposal: Proposal, proposed: dict[str, str], candidates: list[str], taken: set[str]
) -> Proposal:
    """Apply only what survives verification against the real headers."""
    required = REQUIRED[proposal.kind]
    claimed = set(taken)
    choices: list[Choice] = []

    for choice in proposal.choices:
        header = proposed.get(choice.target, "")
        usable = (
            not choice.resolved
            and choice.target in required
            and header in candidates
            and header not in claimed
        )
        if usable:
            claimed.add(header)
            choices.append(Choice(target=choice.target, source=header, origin=Origin.MODEL))
        else:
            if header and not usable:
                log.info("model_mapper_rejected", field=choice.target, header=header)
            choices.append(choice)

    return Proposal(kind=proposal.kind, headers=proposal.headers, choices=tuple(choices))


def _samples(source: Path, headers: list[str]) -> dict[str, list[str]]:
    """A few values per column. Opt-in, because these are real customer data."""
    collected: dict[str, list[str]] = {header: [] for header in headers}
    with source.open(newline="", encoding="utf-8-sig") as handle:
        for index, row in enumerate(csv.DictReader(handle)):
            if index >= SAMPLE_ROWS:
                break
            for header in headers:
                value = (row.get(header) or "").strip()
                if value:
                    collected[header].append(value)
    return {header: values for header, values in collected.items() if values}


def _build_client() -> anthropic.Anthropic:
    """Imported lazily so the app works without the ``model`` extra."""
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - exercised by absence
        raise RuntimeError(
            "the column mapper's model tier needs the 'model' extra: "
            "pip install 'settle-app[model]'"
        ) from exc
    return anthropic.Anthropic()
