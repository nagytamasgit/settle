"""End-to-end: the CLI and the API are the same engine.

The milestone this file exists to prove is that ``settle run`` and
``POST /reconcile`` produce identical output for identical input. They share
the domain function and the serialiser, so the only way they could differ is if
someone put logic in a wrapper — which is exactly what this test would catch.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from settle.api.app import app as api
from settle.cli import app as cli
from settle.io import csv_io
from tests.conftest import inv, pay

runner = CliRunner()
client = TestClient(api)


LEDGER = [
    inv("i1", "1000.00", day=0),
    inv("i2", "250.00", day=1),
    inv("i3", "750.00", day=2),
    inv("i4", "480.00", day=3, customer="c2", customer_name="Globex Zrt"),
]
STATEMENT = [
    pay("p1", "1000.00", reference="szamla 1", day=20),
    pay("p2", "1000.00", reference="INV-2026-0002 INV-2026-0003", day=21),
    pay("p3", "465.60", reference="", counterparty="Globex Zrt", day=22),
    pay("p4", "88.10", reference="rent", counterparty="nobody", day=23),
]


@pytest.fixture
def ledger_files(tmp_path: Path) -> tuple[Path, Path]:
    invoices_path = tmp_path / "invoices.csv"
    payments_path = tmp_path / "bank.csv"
    csv_io.write_invoices(invoices_path, LEDGER)
    csv_io.write_payments(payments_path, STATEMENT)
    return invoices_path, payments_path


def _api_payload() -> dict:
    return {
        "invoices": [
            {
                "id": i.id,
                "customer_id": i.customer_id,
                "customer_name": i.customer_name,
                "number": i.number,
                "issue_date": i.issue_date.isoformat(),
                "amount": str(i.amount),
                "currency": i.currency,
            }
            for i in LEDGER
        ],
        "payments": [
            {
                "id": p.id,
                "date": p.date.isoformat(),
                "amount": str(p.amount),
                "currency": p.currency,
                "reference": p.reference,
                "counterparty": p.counterparty,
            }
            for p in STATEMENT
        ],
    }


class TestEquivalence:
    def test_cli_and_api_produce_identical_output(self, ledger_files, tmp_path: Path) -> None:
        invoices_path, payments_path = ledger_files
        out = tmp_path / "out"

        result = runner.invoke(
            cli, ["run", str(invoices_path), str(payments_path), "--out", str(out)]
        )
        assert result.exit_code == 0, result.output
        from_cli = json.loads((out / "result.json").read_text())

        response = client.post("/reconcile", json=_api_payload())
        assert response.status_code == 200, response.text
        from_api = response.json()

        assert from_cli == from_api


class TestCli:
    def test_it_writes_every_output_file(self, ledger_files, tmp_path: Path) -> None:
        invoices_path, payments_path = ledger_files
        out = tmp_path / "out"

        result = runner.invoke(
            cli, ["run", str(invoices_path), str(payments_path), "--out", str(out)]
        )

        assert result.exit_code == 0, result.output
        for name in ("result.json", "allocations.csv", "review_queue.csv", "invoice_states.csv"):
            assert (out / name).exists(), name

    def test_the_summary_reports_what_needs_a_human(self, ledger_files, tmp_path: Path) -> None:
        invoices_path, payments_path = ledger_files
        result = runner.invoke(
            cli, ["run", str(invoices_path), str(payments_path), "--out", str(tmp_path / "out")]
        )

        assert "4 payments" in result.output
        assert "need a human" in result.output or "nothing needs a human" in result.output

    def test_a_broken_row_names_the_line(self, tmp_path: Path) -> None:
        invoices_path = tmp_path / "invoices.csv"
        payments_path = tmp_path / "bank.csv"
        csv_io.write_invoices(invoices_path, LEDGER)
        payments_path.write_text(
            "id,date,amount,currency,reference,counterparty,customer_id\n"
            "p1,2026-01-25,not-a-number,EUR,szamla 1,Acme Kft,\n",
            encoding="utf-8",
        )

        result = runner.invoke(
            cli, ["run", str(invoices_path), str(payments_path), "--out", str(tmp_path / "out")]
        )

        assert result.exit_code == 2
        assert "bank.csv:2" in result.output
        assert "amount" in result.output

    def test_a_missing_file_is_an_error_not_a_traceback(self, tmp_path: Path) -> None:
        result = runner.invoke(
            cli, ["run", str(tmp_path / "nope.csv"), str(tmp_path / "also-nope.csv")]
        )

        assert result.exit_code == 2
        assert "not found" in result.output
        assert "Traceback" not in result.output

    def test_config_overrides_are_applied(self, ledger_files, tmp_path: Path) -> None:
        invoices_path, payments_path = ledger_files
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"allow_combinations": False}), encoding="utf-8")
        out = tmp_path / "out"

        result = runner.invoke(
            cli,
            [
                "run",
                str(invoices_path),
                str(payments_path),
                "--out",
                str(out),
                "--config",
                str(config),
            ],
        )

        assert result.exit_code == 0, result.output
        payload = json.loads((out / "result.json").read_text())
        combination_matches = [
            p
            for p in payload["payments"]
            if p["chosen"] and p["chosen"]["strategy"] == "combination"
        ]
        assert combination_matches == []

    def test_version(self) -> None:
        result = runner.invoke(cli, ["version"])
        assert result.exit_code == 0
        assert result.output.strip()


class TestApi:
    def test_health(self) -> None:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_money_is_returned_as_strings_never_json_numbers(self) -> None:
        """A float in the response would let a consumer reintroduce the bug."""
        response = client.post("/reconcile", json=_api_payload())
        body = response.text

        payload = response.json()
        assert isinstance(payload["report"]["money"]["in"], str)
        for allocation in payload["allocations"]:
            assert isinstance(allocation["amount"], str)
        assert '"amount": 1000.0' not in body

    def test_a_float_amount_is_rejected_at_the_boundary(self) -> None:
        payload = _api_payload()
        payload["payments"][0]["amount"] = 1000.10

        response = client.post("/reconcile", json=payload)

        assert response.status_code == 422
        assert "float" in response.text

    def test_a_duplicate_id_is_a_422_not_a_500(self) -> None:
        payload = _api_payload()
        payload["payments"].append(payload["payments"][0])

        response = client.post("/reconcile", json=payload)

        assert response.status_code == 422
        assert "duplicate" in response.text

    def test_an_unknown_config_field_is_rejected(self) -> None:
        payload = _api_payload()
        payload["config"] = {"fee_tolerance_pct": "0.03", "nonsense": True}

        response = client.post("/reconcile", json=payload)

        assert response.status_code == 422

    def test_money_still_balances_over_http(self) -> None:
        payload = client.post("/reconcile", json=_api_payload()).json()

        allocated = Decimal(payload["report"]["money"]["allocated"])
        residual = Decimal(payload["report"]["money"]["residual"])
        assert allocated + residual == Decimal(payload["report"]["money"]["in"])
