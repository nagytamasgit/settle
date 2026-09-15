"""The model tier proposes; the ledger decides.

These tests use a fake client rather than a live model: what is being tested is
the contract around the model, not the model. The contract is that nothing it
returns can move money on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from settle.domain.match import reconcile
from settle.domain.models import InvoiceStatus, PaymentStatus
from settle.parse.base import ChainedParser
from settle.parse.model import (
    ExtractedReference,
    ModelReferenceParser,
    ReferenceExtraction,
)
from settle.parse.rules import RuleReferenceParser
from tests.conftest import inv, pay


@dataclass
class FakeUsage:
    input_tokens: int = 120
    output_tokens: int = 30
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class FakeResponse:
    parsed_output: ReferenceExtraction | None
    stop_reason: str = "end_turn"
    usage: FakeUsage = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.usage is None:
            self.usage = FakeUsage()


class FakeMessages:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self._responses[min(len(self.calls) - 1, len(self._responses) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeClient:
    def __init__(self, responses: list[Any]) -> None:
        self.messages = FakeMessages(responses)


def extraction(*pairs: tuple[int, int | None]) -> ReferenceExtraction:
    return ReferenceExtraction(
        references=[ExtractedReference(sequence_number=s, year=y) for s, y in pairs]
    )


def parser_returning(*pairs: tuple[int, int | None]) -> ModelReferenceParser:
    client = FakeClient([FakeResponse(extraction(*pairs))])
    return ModelReferenceParser(client=client)  # type: ignore[arg-type]


class TestProposal:
    def test_the_models_answer_becomes_candidate_tokens(self) -> None:
        outcome = parser_returning((42, 2026)).parse("szamla negyvenketto")

        assert [(t.seq, t.year) for t in outcome.tokens] == [(42, 2026)]
        assert outcome.model_calls == 1

    def test_empty_text_never_reaches_the_model(self) -> None:
        client = FakeClient([FakeResponse(extraction((1, None)))])
        parser = ModelReferenceParser(client=client)  # type: ignore[arg-type]

        assert parser.parse("   ").tokens == ()
        assert client.messages.calls == []

    def test_repeated_references_are_answered_from_cache(self) -> None:
        """Statements repeat references constantly; paying twice for the same
        answer is the easiest cost mistake to make."""
        client = FakeClient([FakeResponse(extraction((7, None)))])
        parser = ModelReferenceParser(client=client)  # type: ignore[arg-type]

        first = parser.parse("the march bill, no 7")
        second = parser.parse("the march bill, no 7")

        assert first.tokens == second.tokens
        assert len(client.messages.calls) == 1
        assert second.model_calls == 0

    @pytest.mark.parametrize(
        ("sequence", "year"),
        [(-5, None), (0, None), (123456789, None)],
    )
    def test_impossible_identifiers_are_discarded(self, sequence: int, year: int | None) -> None:
        assert parser_returning((sequence, year)).parse("something").tokens == ()

    def test_an_impossible_year_is_dropped_but_the_number_is_kept(self) -> None:
        tokens = parser_returning((42, 1200)).parse("szamla 42 in 1200").tokens
        assert [(t.seq, t.year) for t in tokens] == [(42, None)]


class TestDegradation:
    def test_a_model_outage_is_a_recall_problem_not_an_error(self) -> None:
        client = FakeClient([RuntimeError("503 overloaded")])
        parser = ModelReferenceParser(client=client)  # type: ignore[arg-type]

        outcome = parser.parse("szamla negyvenketto")

        assert outcome.tokens == ()
        assert outcome.source.endswith(":error")
        assert outcome.model_calls == 1

    def test_a_refusal_yields_nothing_rather_than_a_crash(self) -> None:
        client = FakeClient([FakeResponse(None, stop_reason="refusal")])
        parser = ModelReferenceParser(client=client)  # type: ignore[arg-type]

        assert parser.parse("anything").tokens == ()


class TestTierOrdering:
    def test_a_readable_reference_never_reaches_the_model(self) -> None:
        client = FakeClient([FakeResponse(extraction((99, None)))])
        chain = ChainedParser(
            [RuleReferenceParser(), ModelReferenceParser(client=client)]  # type: ignore[arg-type]
        )

        outcome = chain.parse("INV-2026-0042")

        assert [(t.seq, t.year) for t in outcome.tokens] == [(42, 2026)]
        assert client.messages.calls == []
        assert outcome.model_calls == 0

    def test_the_model_is_asked_only_about_the_residue(self) -> None:
        client = FakeClient([FakeResponse(extraction((42, None)))])
        chain = ChainedParser(
            [RuleReferenceParser(), ModelReferenceParser(client=client)]  # type: ignore[arg-type]
        )

        outcome = chain.parse("szamla negyvenketto")

        assert [t.seq for t in outcome.tokens] == [42]
        assert len(client.messages.calls) == 1
        assert outcome.model_calls == 1


class TestVerificationAgainstTheLedger:
    def test_a_proposal_that_matches_the_ledger_is_used(self) -> None:
        """With the model tier, a reference no rule can read becomes a match."""
        invoices = [inv("i1", "1000.00"), inv("i2", "640.00")]
        payment = pay("p1", "1000.00", reference="szamla egy")

        result = reconcile(
            invoices,
            [payment],
            parser=ChainedParser([RuleReferenceParser(), parser_returning((1, 2026))]),
        )

        chosen = result.result_for("p1").chosen
        assert chosen is not None
        assert chosen.invoice_ids == ("i1",)
        assert "reference" in chosen.reasons[0]

    def test_an_invented_invoice_number_matches_nothing(self) -> None:
        """The model names invoice 999. There is no invoice 999. Nothing happens."""
        invoices = [inv("i1", "1000.00"), inv("i2", "640.00")]
        payment = pay("p1", "77.00", reference="gibberish", counterparty="")

        result = reconcile(
            invoices,
            [payment],
            parser=ChainedParser([RuleReferenceParser(), parser_returning((999, 2026))]),
        )

        assert result.result_for("p1").status is PaymentStatus.UNMATCHED
        assert all(s.status is InvoiceStatus.OPEN for s in result.invoice_states)
        assert result.report.money_residual == Decimal("77.00")

    def test_a_proposal_for_the_wrong_amount_is_still_checked(self) -> None:
        """The model correctly names invoice 1, but 5000 does not pay a 1000
        invoice. The reference raises confidence; it does not bypass the maths."""
        invoices = [inv("i1", "1000.00")]
        payment = pay("p1", "5000.00", reference="szamla egy")

        result = reconcile(
            invoices,
            [payment],
            parser=ChainedParser([RuleReferenceParser(), parser_returning((1, 2026))]),
        )

        chosen = result.result_for("p1").chosen
        assert chosen is not None
        assert chosen.allocated == Decimal("1000.00")
        assert result.report.money_residual == Decimal("4000.00")

    def test_removing_the_model_costs_recall_and_nothing_else(self) -> None:
        """The milestone claim, as an assertion.

        Two identical invoices and a reference only a model can read. Without
        the model tier the payment is genuinely ambiguous and goes to a human;
        with it, the reference resolves the ambiguity. Neither run can break an
        invariant, because ``reconcile`` verifies them before returning.
        """
        invoices = [inv("i1", "1000.00"), inv("i2", "1000.00")]
        payments = [pay("p1", "1000.00", reference="szamla egy")]

        with_model = reconcile(
            invoices,
            payments,
            parser=ChainedParser([RuleReferenceParser(), parser_returning((1, 2026))]),
        )
        without_model = reconcile(invoices, payments, parser=RuleReferenceParser())

        # Recall: the model tier turns a review item into a match.
        chosen = with_model.result_for("p1").chosen
        assert chosen is not None
        assert chosen.invoice_ids == ("i1",)

        # Without it: no guess, just a human.
        assert without_model.result_for("p1").status is PaymentStatus.REVIEW
        assert without_model.result_for("p1").chosen is None

        # Correctness: identical money in, nothing over-allocated, either way.
        assert with_model.report.money_in == without_model.report.money_in
        assert without_model.report.money_residual == Decimal("1000.00")
        assert with_model.report.money_allocated == Decimal("1000.00")
