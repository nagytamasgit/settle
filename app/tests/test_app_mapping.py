"""Working out which column is which.

The load-bearing test is ``test_a_mapped_file_reads_identically_to_a_canonical_one``.
The whole design depends on ``to_canonical`` producing a file the engine's own
readers accept without knowing a mapping happened; if that ever stops being
true, the engine grows a branch for "files that came from the web app", which is
the thing this module exists to avoid.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from settle.io import csv_io
from settle_app.mapping import (
    FUZZY_THRESHOLD,
    Kind,
    Origin,
    apply_overrides,
    normalise,
    propose,
    read_headers,
    to_canonical,
)

CANONICAL_INVOICES = (
    "id,customer_id,customer_name,number,issue_date,due_date,amount,currency\n"
    "i1,c1,Acme Kft,INV-2026-0001,2026-01-05,,300.00,EUR\n"
)

# The same data as a plausible accounting export: different names, different
# order, and two columns settle has no use for.
XERO_INVOICES = (
    "ContactName,Invoice Number,InvoiceDate,DueDate,Total,Currency,AccountCode,Reference,Sent\n"
    "Acme Kft,INV-2026-0001,2026-01-05,,300.00,EUR,c1,i1,true\n"
)

BANK_EXPORT = (
    "Transaction ID,Booking Date,Credit Amount,Ccy,Remittance Information,Payer Name\n"
    "p1,2026-01-25,300.00,EUR,INV-2026-0001,Acme Kft\n"
)


class TestNormalising:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Invoice Number", "invoice number"),
            ("invoice_number", "invoice number"),
            ("INVOICE-NUMBER", "invoice number"),
            ("  Invoice   Number  ", "invoice number"),
            ("Számlaszám", "szamlaszam"),
            ("Invoice No.", "invoice no"),
            ("", ""),
        ],
    )
    def test_case_punctuation_and_accents_all_fold(self, raw: str, expected: str) -> None:
        assert normalise(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("InvoiceDate", "invoice date"),
            ("ContactName", "contact name"),
            ("AccountCode", "account code"),
            ("InvoiceID", "invoice id"),
            # Acronyms have no lowercase before the capital, so they stay whole.
            ("ID", "id"),
            ("VAT", "vat"),
            ("Line1Amount", "line1 amount"),
        ],
    )
    def test_camel_case_is_split(self, raw: str, expected: str) -> None:
        """Xero exports InvoiceDate as one word. Without this the alias table
        would need a second entry for every name it already has."""
        assert normalise(raw) == expected


class TestAliases:
    def test_a_canonical_file_maps_to_itself(self) -> None:
        proposal = propose(
            [
                "id",
                "customer_id",
                "customer_name",
                "number",
                "issue_date",
                "due_date",
                "amount",
                "currency",
            ],
            Kind.INVOICE,
        )
        assert proposal.is_complete
        assert proposal.as_dict()["number"] == "number"

    def test_an_accounting_export_maps_by_alias(self) -> None:
        headers = XERO_INVOICES.splitlines()[0].split(",")
        proposal = propose(headers, Kind.INVOICE)
        mapping = proposal.as_dict()
        assert mapping["number"] == "Invoice Number"
        assert mapping["issue_date"] == "InvoiceDate"
        assert mapping["amount"] == "Total"
        assert mapping["customer_name"] == "ContactName"
        assert mapping["customer_id"] == "AccountCode"

    def test_a_bank_export_maps_by_alias(self) -> None:
        headers = BANK_EXPORT.splitlines()[0].split(",")
        proposal = propose(headers, Kind.PAYMENT)
        mapping = proposal.as_dict()
        assert mapping["id"] == "Transaction ID"
        assert mapping["date"] == "Booking Date"
        assert mapping["amount"] == "Credit Amount"
        assert mapping["reference"] == "Remittance Information"
        assert mapping["counterparty"] == "Payer Name"

    def test_hungarian_headers_map(self) -> None:
        proposal = propose(
            ["azonosito", "Számlaszám", "Kelt", "Összeg", "Pénznem", "Vevő neve"],
            Kind.INVOICE,
        )
        mapping = proposal.as_dict()
        assert mapping["number"] == "Számlaszám"
        assert mapping["issue_date"] == "Kelt"
        assert mapping["amount"] == "Összeg"
        assert mapping["currency"] == "Pénznem"

    def test_one_column_is_never_used_twice(self) -> None:
        proposal = propose(["Amount", "Amount"], Kind.PAYMENT)
        sources = [c.source for c in proposal.choices if c.resolved]
        assert len(sources) == len(set(sources))

    def test_unknown_columns_are_simply_left_out(self) -> None:
        proposal = propose(["Sent", "Notes", "Internal Flag"], Kind.INVOICE)
        assert not proposal.is_complete
        assert "amount" in proposal.unresolved_required


class TestFuzzyTier:
    def test_a_near_miss_is_accepted(self) -> None:
        proposal = propose(["counterpart"], Kind.PAYMENT)
        choice = next(c for c in proposal.choices if c.target == "counterparty")
        assert choice.source == "counterpart"
        assert choice.origin is Origin.FUZZY

    def test_an_unrelated_name_is_not(self) -> None:
        proposal = propose(["sausages"], Kind.PAYMENT)
        assert not any(c.resolved for c in proposal.choices)

    def test_a_near_tie_is_refused_rather_than_guessed(self) -> None:
        """Two columns almost equally like 'currency' is a question for a human."""
        proposal = propose(["currencyy", "currencyx"], Kind.PAYMENT)
        choice = next(c for c in proposal.choices if c.target == "currency")
        assert not choice.resolved

    def test_the_threshold_is_high_enough_to_be_meaningful(self) -> None:
        assert FUZZY_THRESHOLD >= 85


class TestRequirements:
    def test_a_missing_optional_column_is_fine(self) -> None:
        proposal = propose(
            ["id", "customer_id", "number", "issue_date", "amount", "currency"], Kind.INVOICE
        )
        assert proposal.is_complete
        assert "customer_name" not in proposal.as_dict()

    def test_a_missing_required_column_is_not(self) -> None:
        proposal = propose(["id", "customer_id", "number", "issue_date"], Kind.INVOICE)
        assert not proposal.is_complete
        assert set(proposal.unresolved_required) == {"amount", "currency"}

    def test_a_fully_aliased_mapping_still_wants_a_look(self) -> None:
        """Complete is not the same as obviously right."""
        proposal = propose(["counterpart", "id", "date", "amount", "currency"], Kind.PAYMENT)
        assert proposal.needs_attention


class TestOverrides:
    def test_a_human_choice_wins_and_is_recorded_as_manual(self) -> None:
        proposal = propose(["Total", "Value"], Kind.INVOICE)
        amended = apply_overrides(proposal, {"amount": "Value"})
        choice = next(c for c in amended.choices if c.target == "amount")
        assert choice.source == "Value"
        assert choice.origin is Origin.MANUAL

    def test_clearing_a_column_unsets_it(self) -> None:
        proposal = propose(["Total"], Kind.INVOICE)
        amended = apply_overrides(proposal, {"amount": ""})
        assert "amount" in amended.unresolved_required

    def test_a_header_that_is_not_in_the_file_is_ignored(self) -> None:
        """The select is a closed list; a hand-crafted POST is not."""
        proposal = propose(["Total"], Kind.INVOICE)
        amended = apply_overrides(proposal, {"amount": "../../etc/passwd"})
        assert not next(c for c in amended.choices if c.target == "amount").resolved

    def test_the_same_column_cannot_be_claimed_twice(self) -> None:
        proposal = propose(["Total", "Ccy"], Kind.INVOICE)
        amended = apply_overrides(proposal, {"amount": "Total", "currency": "Total"})
        sources = [c.source for c in amended.choices if c.resolved]
        assert sources.count("Total") == 1


class TestToCanonical:
    def test_a_mapped_file_reads_identically_to_a_canonical_one(self, tmp_path: Path) -> None:
        """The load-bearing one. The engine must not be able to tell."""
        canonical_path = tmp_path / "canonical.csv"
        canonical_path.write_text(CANONICAL_INVOICES, encoding="utf-8")

        export_path = tmp_path / "xero.csv"
        export_path.write_text(XERO_INVOICES, encoding="utf-8")
        mapped_path = tmp_path / "mapped.csv"
        proposal = propose(read_headers(export_path), Kind.INVOICE)
        assert proposal.is_complete
        to_canonical(export_path, mapped_path, proposal)

        assert csv_io.read_invoices(mapped_path) == csv_io.read_invoices(canonical_path)

    def test_a_mapped_bank_export_reads_as_payments(self, tmp_path: Path) -> None:
        export_path = tmp_path / "bank.csv"
        export_path.write_text(BANK_EXPORT, encoding="utf-8")
        mapped_path = tmp_path / "mapped.csv"
        proposal = propose(read_headers(export_path), Kind.PAYMENT)
        to_canonical(export_path, mapped_path, proposal)

        payments = csv_io.read_payments(mapped_path)
        assert len(payments) == 1
        assert payments[0].reference == "INV-2026-0001"
        assert payments[0].counterparty == "Acme Kft"

    def test_columns_settle_has_no_use_for_are_dropped(self, tmp_path: Path) -> None:
        export_path = tmp_path / "xero.csv"
        export_path.write_text(XERO_INVOICES, encoding="utf-8")
        mapped_path = tmp_path / "mapped.csv"
        to_canonical(export_path, mapped_path, propose(read_headers(export_path), Kind.INVOICE))
        assert "Sent" not in mapped_path.read_text(encoding="utf-8")

    def test_values_are_stripped(self, tmp_path: Path) -> None:
        export_path = tmp_path / "padded.csv"
        export_path.write_text(
            "id,customer_id,number,issue_date,amount,currency\n"
            "  i1 , c1 , INV-1 , 2026-01-05 , 300.00 , EUR \n",
            encoding="utf-8",
        )
        mapped_path = tmp_path / "mapped.csv"
        to_canonical(export_path, mapped_path, propose(read_headers(export_path), Kind.INVOICE))
        assert csv_io.read_invoices(mapped_path)[0].id == "i1"


class TestReadHeaders:
    def test_headers_are_read_and_stripped(self, tmp_path: Path) -> None:
        path = tmp_path / "f.csv"
        path.write_text(" a , b \n1,2\n", encoding="utf-8")
        assert read_headers(path) == ("a", "b")

    def test_an_empty_file_has_none(self, tmp_path: Path) -> None:
        path = tmp_path / "f.csv"
        path.write_text("", encoding="utf-8")
        assert read_headers(path) == ()

    def test_a_byte_order_mark_does_not_become_part_of_a_name(self, tmp_path: Path) -> None:
        """Excel writes one, and it silently breaks the first column."""
        path = tmp_path / "f.csv"
        path.write_bytes("﻿id,amount\n".encode())
        assert read_headers(path) == ("id", "amount")
