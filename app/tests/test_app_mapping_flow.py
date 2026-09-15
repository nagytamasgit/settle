"""The confirmation screen, end to end.

The point of this milestone is that a real accounting export works without
anyone renaming columns first, and that nothing is reconciled until a person has
looked at the mapping. Both halves are tested here; the second one matters more.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from settle_app.app import create_app
from settle_app.settings import WebSettings

PASSWORD = "correct-horse-battery-staple"

# A plausible Xero-shaped export: different names, different order, extra
# columns settle has no use for.
XERO_INVOICES = (
    "ContactName,Invoice Number,InvoiceDate,DueDate,Total,Currency,AccountCode,Reference,Sent\n"
    "Acme Kft,INV-2026-0001,2026-01-05,,300.00,EUR,c1,i1,true\n"
    "Acme Kft,INV-2026-0002,2026-01-05,,120.00,EUR,c1,i2,true\n"
)

BANK_EXPORT = (
    "Transaction ID,Booking Date,Credit Amount,Ccy,Remittance Information,Payer Name\n"
    "p1,2026-01-25,300.00,EUR,INV-2026-0001,Acme Kft\n"
    "p2,2026-01-26,120.00,EUR,INV-2026-0002,Acme Kft\n"
)

OPAQUE_INVOICES = "col_a,col_b,col_c,col_d,col_e,col_f\ni1,c1,INV-1,2026-01-05,300.00,EUR\n"


@pytest.fixture
def settings(tmp_path: Path) -> WebSettings:
    return WebSettings(data_dir=tmp_path, password=PASSWORD)


@pytest.fixture
def client(settings: WebSettings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), follow_redirects=False) as client:
        client.post("/login", data={"password": PASSWORD})
        yield client


def _csrf(client: TestClient, path: str) -> str:
    page = client.get(path).text
    marker = 'name="csrf_token" value="'
    start = page.index(marker) + len(marker)
    return page[start : page.index('"', start)]


def _start(client: TestClient, invoices: str, payments: str) -> str:
    response = client.post(
        "/runs",
        data={"csrf_token": _csrf(client, "/runs/new")},
        files={
            "invoices": ("ledger.csv", invoices, "text/csv"),
            "payments": ("statement.csv", payments, "text/csv"),
        },
    )
    assert response.status_code == 303, response.text
    return response.headers["location"].split("/")[2]


class TestARealExportJustWorks:
    def test_an_accounting_export_reconciles_without_renaming_anything(
        self, client: TestClient
    ) -> None:
        run_id = _start(client, XERO_INVOICES, BANK_EXPORT)
        path = f"/runs/{run_id}/mapping"

        screen = client.get(path).text
        assert "Invoice Number" in screen
        assert "Remittance Information" in screen

        response = client.post(path, data={"csrf_token": _csrf(client, path)})
        assert response.status_code == 303

        page = client.get(f"/runs/{run_id}").text
        assert "420.00" in page
        assert "Acme Kft" in page

    def test_the_screen_says_where_each_guess_came_from(self, client: TestClient) -> None:
        run_id = _start(client, XERO_INVOICES, BANK_EXPORT)
        screen = client.get(f"/runs/{run_id}/mapping").text
        assert "alias" in screen
        assert "required" in screen

    def test_both_the_original_and_the_mapped_file_are_downloadable(
        self, client: TestClient
    ) -> None:
        """When a mapping looks wrong, you want to see both."""
        run_id = _start(client, XERO_INVOICES, BANK_EXPORT)
        path = f"/runs/{run_id}/mapping"
        client.post(path, data={"csrf_token": _csrf(client, path)})

        original = client.get(f"/runs/{run_id}/files/upload-invoices.csv")
        mapped = client.get(f"/runs/{run_id}/files/invoices.csv")
        assert "ContactName" in original.text
        assert "customer_name" in mapped.text


class TestNothingRunsWithoutConfirmation:
    def test_uploading_does_not_reconcile(self, client: TestClient) -> None:
        run_id = _start(client, XERO_INVOICES, BANK_EXPORT)
        assert client.get(f"/runs/{run_id}/files/result.json").status_code == 404

    def test_the_run_sits_as_a_draft_until_confirmed(
        self, client: TestClient, settings: WebSettings
    ) -> None:
        from settle_app.store import STATUS_DRAFT, RunStore

        _start(client, XERO_INVOICES, BANK_EXPORT)
        rows = RunStore(settings.database_path).list_runs()
        assert [row.status for row in rows] == [STATUS_DRAFT]

    def test_confirming_without_a_token_does_not_run(self, client: TestClient) -> None:
        run_id = _start(client, XERO_INVOICES, BANK_EXPORT)
        response = client.post(f"/runs/{run_id}/mapping")
        assert response.status_code == 403
        assert client.get(f"/runs/{run_id}/files/result.json").status_code == 404

    def test_a_stranger_cannot_reach_the_mapping_screen(
        self, client: TestClient, settings: WebSettings
    ) -> None:
        run_id = _start(client, XERO_INVOICES, BANK_EXPORT)
        with TestClient(create_app(settings), follow_redirects=False) as stranger:
            assert stranger.get(f"/runs/{run_id}/mapping").status_code == 303
            assert stranger.post(f"/runs/{run_id}/mapping").status_code == 303

    def test_an_unknown_run_has_no_mapping_screen(self, client: TestClient) -> None:
        assert client.get("/runs/" + "a" * 32 + "/mapping").status_code == 404
        assert client.post("/runs/" + "a" * 32 + "/mapping").status_code == 404


class TestUnmappableFiles:
    def test_a_file_the_rules_cannot_read_stops_at_the_screen(self, client: TestClient) -> None:
        run_id = _start(client, OPAQUE_INVOICES, BANK_EXPORT)
        path = f"/runs/{run_id}/mapping"
        response = client.post(path, data={"csrf_token": _csrf(client, path)})
        assert response.status_code == 422
        assert "Still needed" in response.text

    def test_the_user_can_fill_the_gaps_by_hand(self, client: TestClient) -> None:
        """The mapping screen is a fallback, not just a confirmation."""
        run_id = _start(client, OPAQUE_INVOICES, BANK_EXPORT)
        path = f"/runs/{run_id}/mapping"
        response = client.post(
            path,
            data={
                "csrf_token": _csrf(client, path),
                "invoice.id": "col_a",
                "invoice.customer_id": "col_b",
                "invoice.number": "col_c",
                "invoice.issue_date": "col_d",
                "invoice.amount": "col_e",
                "invoice.currency": "col_f",
            },
        )
        assert response.status_code == 303
        assert "Allocations" in client.get(f"/runs/{run_id}").text

    def test_a_hand_crafted_column_name_is_ignored(self, client: TestClient) -> None:
        """The select is a closed list; a crafted POST is not."""
        run_id = _start(client, OPAQUE_INVOICES, BANK_EXPORT)
        path = f"/runs/{run_id}/mapping"
        response = client.post(
            path,
            data={"csrf_token": _csrf(client, path), "invoice.amount": "/etc/passwd"},
        )
        assert response.status_code == 422


class TestTheModelTierIsOffByDefault:
    def test_no_model_is_consulted_unless_configured(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called = False

        def spy(*_: object, **__: object) -> None:
            nonlocal called
            called = True
            raise AssertionError("the model tier must not run when disabled")

        monkeypatch.setattr("settle_app.mapper_model.fill_gaps", spy, raising=False)
        run_id = _start(client, OPAQUE_INVOICES, BANK_EXPORT)
        client.get(f"/runs/{run_id}/mapping")
        assert not called

    def test_the_screen_does_not_mention_a_model_when_it_is_off(self, client: TestClient) -> None:
        run_id = _start(client, XERO_INVOICES, BANK_EXPORT)
        assert "A model suggested" not in client.get(f"/runs/{run_id}/mapping").text
