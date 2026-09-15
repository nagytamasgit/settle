"""The model tier of the column mapper, driven by a fake client.

CI has no network and no API key, by design, so every test here supplies its own
client. The pattern is copied from the engine's ``tests/unit/test_model_parser.py``
for the same reason the module copies ``settle/parse/model.py``: a model that can
only propose, and is verified before anything acts on what it said.

Most of these tests are about the model being wrong. That is the interesting
case — a mapper that works when the model behaves is not the claim; a mapper
that is safe when it misbehaves is.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from settle_app.mapper_model import ColumnMapping, ProposedColumn, fill_gaps
from settle_app.mapping import Kind, Origin, propose

# Headers the alias and fuzzy tiers cannot place, so the model tier is reached.
OPAQUE = ["col_a", "col_b", "col_c", "col_d", "col_e", "col_f"]


class _Response:
    def __init__(self, parsed: ColumnMapping | None, stop_reason: str = "end_turn") -> None:
        self.parsed_output = parsed
        self.stop_reason = stop_reason
        self.usage = type("Usage", (), {"input_tokens": 10, "output_tokens": 5})()


class _Messages:
    def __init__(self, response: Any, explode: Exception | None = None) -> None:
        self._response = response
        self._explode = explode
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._explode is not None:
            raise self._explode
        return self._response


class _Client:
    def __init__(self, response: Any = None, explode: Exception | None = None) -> None:
        self.messages = _Messages(response, explode)


def _client_proposing(**fields: str) -> _Client:
    return _Client(
        _Response(
            ColumnMapping(columns=[ProposedColumn(field=k, header=v) for k, v in fields.items()])
        )
    )


class TestItIsNotCalledUnnecessarily:
    def test_a_complete_mapping_asks_nothing(self) -> None:
        """A clean export must never reach the model tier at all."""
        proposal = propose(
            ["id", "customer_id", "number", "issue_date", "amount", "currency"], Kind.INVOICE
        )
        client = _Client()
        assert fill_gaps(proposal, client=client) is proposal
        assert client.messages.calls == []

    def test_a_file_with_no_spare_columns_asks_nothing(self) -> None:
        proposal = propose(["id"], Kind.PAYMENT)
        client = _Client()
        fill_gaps(proposal, client=client)
        assert client.messages.calls == []


class TestItOnlyProposes:
    def test_a_good_proposal_fills_a_required_gap(self) -> None:
        proposal = propose(OPAQUE, Kind.PAYMENT)
        filled = fill_gaps(proposal, client=_client_proposing(amount="col_c"))
        choice = next(c for c in filled.choices if c.target == "amount")
        assert choice.source == "col_c"
        assert choice.origin is Origin.MODEL

    def test_a_header_that_is_not_in_the_file_is_dropped(self) -> None:
        """The closed set of real headers is the verification."""
        proposal = propose(OPAQUE, Kind.PAYMENT)
        filled = fill_gaps(proposal, client=_client_proposing(amount="TotalAmount"))
        assert "amount" in filled.unresolved_required

    def test_an_invented_field_name_is_dropped(self) -> None:
        proposal = propose(OPAQUE, Kind.PAYMENT)
        filled = fill_gaps(proposal, client=_client_proposing(vat_rate="col_a"))
        assert not any(c.origin is Origin.MODEL for c in filled.choices)

    def test_it_cannot_overwrite_something_the_rules_resolved(self) -> None:
        """The deterministic tiers are trusted over the model, always."""
        proposal = propose(["amount", "col_a", "col_b", "col_c"], Kind.PAYMENT)
        filled = fill_gaps(proposal, client=_client_proposing(amount="col_a"))
        choice = next(c for c in filled.choices if c.target == "amount")
        assert choice.source == "amount"
        assert choice.origin is Origin.ALIAS

    def test_it_cannot_claim_one_column_for_two_fields(self) -> None:
        proposal = propose(OPAQUE, Kind.PAYMENT)
        filled = fill_gaps(proposal, client=_client_proposing(amount="col_a", currency="col_a"))
        sources = [c.source for c in filled.choices if c.resolved]
        assert sources.count("col_a") == 1

    def test_optional_fields_are_not_filled_by_the_model(self) -> None:
        """It is asked about required gaps only, so it answers about those."""
        proposal = propose(OPAQUE, Kind.PAYMENT)
        filled = fill_gaps(proposal, client=_client_proposing(counterparty="col_b"))
        choice = next(c for c in filled.choices if c.target == "counterparty")
        assert not choice.resolved


class TestItNeverBreaksARun:
    def test_an_outage_degrades_to_no_proposal(self) -> None:
        proposal = propose(OPAQUE, Kind.PAYMENT)
        filled = fill_gaps(proposal, client=_Client(explode=RuntimeError("503")))
        assert filled.unresolved_required == proposal.unresolved_required

    def test_a_refusal_degrades_to_no_proposal(self) -> None:
        proposal = propose(OPAQUE, Kind.PAYMENT)
        client = _Client(_Response(ColumnMapping(columns=[]), stop_reason="refusal"))
        assert fill_gaps(proposal, client=client).unresolved_required

    def test_an_unparsed_response_degrades_to_no_proposal(self) -> None:
        proposal = propose(OPAQUE, Kind.PAYMENT)
        assert fill_gaps(proposal, client=_Client(_Response(None))).unresolved_required

    def test_an_empty_proposal_changes_nothing(self) -> None:
        proposal = propose(OPAQUE, Kind.PAYMENT)
        client = _Client(_Response(ColumnMapping(columns=[])))
        assert fill_gaps(proposal, client=client).as_dict() == proposal.as_dict()


class TestWhatItIsSent:
    def test_headers_are_sent_but_values_never_are_by_default(self, tmp_path: Path) -> None:
        """The data-protection claim in DEPLOY.md, checked rather than promised."""
        source = tmp_path / "bank.csv"
        source.write_text("col_a,col_b\nDr Schmidt,INV-2026-0042\n", encoding="utf-8")

        client = _client_proposing(amount="col_a")
        fill_gaps(propose(["col_a", "col_b"], Kind.PAYMENT), client=client, source=source)

        sent = str(client.messages.calls[0]["messages"])
        assert "col_a" in sent
        assert "Dr Schmidt" not in sent
        assert "INV-2026-0042" not in sent

    def test_samples_are_sent_only_when_explicitly_asked_for(self, tmp_path: Path) -> None:
        source = tmp_path / "bank.csv"
        source.write_text("col_a,col_b\nDr Schmidt,INV-2026-0042\n", encoding="utf-8")

        client = _client_proposing(amount="col_a")
        fill_gaps(
            propose(["col_a", "col_b"], Kind.PAYMENT),
            client=client,
            source=source,
            send_samples=True,
        )
        assert "Dr Schmidt" in str(client.messages.calls[0]["messages"])

    def test_asking_for_samples_without_a_file_sends_none(self) -> None:
        client = _client_proposing(amount="col_a")
        fill_gaps(propose(OPAQUE, Kind.PAYMENT), client=client, send_samples=True)
        assert "Example values" not in str(client.messages.calls[0]["messages"])

    def test_only_the_missing_fields_are_asked_about(self) -> None:
        proposal = propose(["amount", "currency", "col_a", "col_b"], Kind.PAYMENT)
        client = _client_proposing(id="col_a")
        fill_gaps(proposal, client=client)
        asked = str(client.messages.calls[0]["messages"])
        assert "'id'" in asked
        assert "'amount'" not in asked


class TestItIsDeletable:
    def test_without_the_extra_the_mapping_is_simply_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Removing this tier degrades mapping recall and nothing else."""
        from settle_app import mapper_model

        def no_sdk() -> None:
            raise RuntimeError("the column mapper's model tier needs the 'model' extra")

        monkeypatch.setattr(mapper_model, "_build_client", no_sdk)
        proposal = propose(OPAQUE, Kind.PAYMENT)
        assert fill_gaps(proposal).as_dict() == proposal.as_dict()
