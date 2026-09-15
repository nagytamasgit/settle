"""Uploading files and getting answers back.

The load-bearing test is ``test_the_web_result_is_byte_identical_to_the_engines``.
The app must never become a second implementation of the answer: if the file it
serves ever differs from what ``settle run`` would have written for the same
input, the engine's own test suite stops being evidence about this app.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from settle.domain.config import DEFAULT_CONFIG
from settle.domain.match import reconcile
from settle.io import csv_io
from settle.io.json_io import dumps
from settle_app.app import create_app
from settle_app.settings import WebSettings

PASSWORD = "correct-horse-battery-staple"

INVOICES = (
    "id,customer_id,customer_name,number,issue_date,due_date,amount,currency\n"
    "i1,c1,Acme Kft,INV-2026-0001,2026-01-05,,300.00,EUR\n"
    "i2,c1,Acme Kft,INV-2026-0002,2026-01-05,,120.00,EUR\n"
    "i3,c2,Hooli Oy,INV-2026-0003,2026-01-06,,80.00,EUR\n"
)

PAYMENTS = (
    "id,date,amount,currency,reference,counterparty,customer_id\n"
    "p1,2026-01-25,300.00,EUR,INV-2026-0001,Acme Kft,c1\n"
    "p2,2026-01-26,120.00,EUR,szamla 2,Acme Kft,c1\n"
    "p3,2026-01-27,80.00,EUR,,Hooli Oy,c2\n"
)


@pytest.fixture
def settings(tmp_path: Path) -> WebSettings:
    return WebSettings(data_dir=tmp_path, password=PASSWORD)


@pytest.fixture
def client(settings: WebSettings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), follow_redirects=False) as client:
        client.post("/login", data={"password": PASSWORD})
        yield client


def _csrf(client: TestClient) -> str:
    page = client.get("/runs/new").text
    marker = 'name="csrf_token" value="'
    start = page.index(marker) + len(marker)
    return page[start : page.index('"', start)]


def _upload(
    client: TestClient, invoices: str = INVOICES, payments: str = PAYMENTS, **extra: str
) -> str:
    """Post a run and return the created run id."""
    response = client.post(
        "/runs",
        data={"csrf_token": _csrf(client), **extra},
        files={
            "invoices": ("invoices.csv", invoices, "text/csv"),
            "payments": ("bank.csv", payments, "text/csv"),
        },
    )
    assert response.status_code == 303, response.text
    return response.headers["location"].rsplit("/", 1)[-1]


class TestTheHappyPath:
    def test_a_run_produces_a_results_page(self, client: TestClient) -> None:
        run_id = _upload(client)
        page = client.get(f"/runs/{run_id}").text
        assert "Allocations" in page
        assert "Acme Kft" in page

    def test_the_run_appears_in_the_history(self, client: TestClient) -> None:
        _upload(client, label="March 2026")
        assert "March 2026" in client.get("/runs").text

    def test_the_summary_reports_the_money(self, client: TestClient) -> None:
        run_id = _upload(client)
        page = client.get(f"/runs/{run_id}").text
        assert "500.00" in page

    def test_every_output_file_is_downloadable(self, client: TestClient) -> None:
        run_id = _upload(client)
        for name in ("result.json", "allocations.csv", "review_queue.csv", "invoice_states.csv"):
            response = client.get(f"/runs/{run_id}/files/{name}")
            assert response.status_code == 200, name
            assert response.content

    def test_the_sample_runs_without_any_upload(self, client: TestClient) -> None:
        """The 'just looking' path has to work, or nobody gets past the form."""
        response = client.post("/runs", data={"csrf_token": _csrf(client), "use_sample": "1"})
        assert response.status_code == 303
        run_id = response.headers["location"].rsplit("/", 1)[-1]
        assert "Allocations" in client.get(f"/runs/{run_id}").text


class TestEquivalence:
    def test_the_web_result_is_byte_identical_to_the_engines(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """One engine, one serialiser, one answer. No second implementation."""
        run_id = _upload(client)
        from_web = client.get(f"/runs/{run_id}/files/result.json").text

        invoices_path = tmp_path / "invoices.csv"
        payments_path = tmp_path / "bank.csv"
        invoices_path.write_text(INVOICES, encoding="utf-8")
        payments_path.write_text(PAYMENTS, encoding="utf-8")
        direct = reconcile(
            csv_io.read_invoices(invoices_path),
            csv_io.read_payments(payments_path),
            config=DEFAULT_CONFIG,
        )

        assert from_web == dumps(direct) + "\n"

    def test_the_tables_agree_with_the_downloaded_csv(self, client: TestClient) -> None:
        run_id = _upload(client)
        page = client.get(f"/runs/{run_id}").text
        allocations = client.get(f"/runs/{run_id}/files/allocations.csv").text
        for line in allocations.strip().splitlines()[1:]:
            assert line.split(",")[0] in page


class TestRejections:
    def test_a_missing_invoice_file_is_refused(self, client: TestClient) -> None:
        response = client.post(
            "/runs",
            data={"csrf_token": _csrf(client)},
            files={"payments": ("bank.csv", PAYMENTS, "text/csv")},
        )
        assert response.status_code == 422
        assert "Choose a invoice CSV" in response.text

    def test_an_empty_file_is_refused(self, client: TestClient) -> None:
        response = client.post(
            "/runs",
            data={"csrf_token": _csrf(client)},
            files={
                "invoices": ("invoices.csv", "", "text/csv"),
                "payments": ("bank.csv", PAYMENTS, "text/csv"),
            },
        )
        assert response.status_code == 422

    def test_a_broken_row_names_the_line(self, client: TestClient) -> None:
        """The engine's error message is worth more than a generic one."""
        broken = PAYMENTS.replace("300.00", "not-a-number")
        response = client.post(
            "/runs",
            data={"csrf_token": _csrf(client)},
            files={
                "invoices": ("invoices.csv", INVOICES, "text/csv"),
                "payments": ("bank.csv", broken, "text/csv"),
            },
        )
        assert response.status_code == 422
        assert "bank.csv:2" in response.text

    def test_an_oversized_upload_is_refused(self, tmp_path: Path) -> None:
        settings = WebSettings(data_dir=tmp_path, password=PASSWORD, max_upload_bytes=1024)
        with TestClient(create_app(settings), follow_redirects=False) as client:
            client.post("/login", data={"password": PASSWORD})
            response = client.post(
                "/runs",
                data={"csrf_token": _csrf(client)},
                files={
                    "invoices": ("invoices.csv", "x" * 5000, "text/csv"),
                    "payments": ("bank.csv", PAYMENTS, "text/csv"),
                },
            )
            assert response.status_code == 413

    def test_too_many_rows_points_at_the_command_line(self, tmp_path: Path) -> None:
        settings = WebSettings(data_dir=tmp_path, password=PASSWORD, max_rows=2)
        with TestClient(create_app(settings), follow_redirects=False) as client:
            client.post("/login", data={"password": PASSWORD})
            response = client.post(
                "/runs",
                data={"csrf_token": _csrf(client)},
                files={
                    "invoices": ("invoices.csv", INVOICES, "text/csv"),
                    "payments": ("bank.csv", PAYMENTS, "text/csv"),
                },
            )
            assert response.status_code == 422
            assert "settle run" in response.text

    def test_a_rejected_upload_leaves_nothing_behind(
        self, client: TestClient, settings: WebSettings
    ) -> None:
        client.post(
            "/runs",
            data={"csrf_token": _csrf(client)},
            files={"payments": ("bank.csv", PAYMENTS, "text/csv")},
        )
        assert list(settings.runs_dir.iterdir()) == []


class TestCsrf:
    def test_a_post_without_a_token_is_refused(self, client: TestClient) -> None:
        response = client.post(
            "/runs",
            files={
                "invoices": ("invoices.csv", INVOICES, "text/csv"),
                "payments": ("bank.csv", PAYMENTS, "text/csv"),
            },
        )
        assert response.status_code == 403

    def test_a_delete_without_a_token_is_refused(self, client: TestClient) -> None:
        run_id = _upload(client)
        assert client.post(f"/runs/{run_id}/delete").status_code == 403
        assert client.get(f"/runs/{run_id}").status_code == 200


class TestDownloadsAreLockedDown:
    @pytest.mark.parametrize(
        "name",
        ["secret.key", "../secret.key", "../../etc/passwd", "settle.db", "upload-invoices.csv"],
    )
    def test_only_whitelisted_files_are_served(self, client: TestClient, name: str) -> None:
        run_id = _upload(client)
        assert client.get(f"/runs/{run_id}/files/{name}").status_code == 404

    def test_an_unknown_run_is_a_404(self, client: TestClient) -> None:
        assert client.get("/runs/" + "a" * 32).status_code == 404

    def test_a_malformed_run_id_is_a_404(self, client: TestClient) -> None:
        assert client.get("/runs/not-a-run-id").status_code == 404


class TestDeletion:
    def test_deleting_removes_the_row_and_the_files(
        self, client: TestClient, settings: WebSettings
    ) -> None:
        run_id = _upload(client)
        directory = settings.runs_dir / run_id
        assert directory.is_dir()

        response = client.post(f"/runs/{run_id}/delete", data={"csrf_token": _csrf(client)})
        assert response.status_code == 303
        assert not directory.exists()
        assert client.get(f"/runs/{run_id}").status_code == 404


class TestSignedOutCannotReachRuns:
    """Every run route is behind the password, not just the list."""

    @pytest.fixture
    def stranger(self, settings: WebSettings) -> Iterator[TestClient]:
        with TestClient(create_app(settings), follow_redirects=False) as client:
            yield client

    def test_the_upload_form_redirects(self, stranger: TestClient) -> None:
        assert stranger.get("/runs/new").headers["location"] == "/login"

    def test_creating_a_run_redirects(self, stranger: TestClient) -> None:
        response = stranger.post("/runs", files={"invoices": ("i.csv", INVOICES, "text/csv")})
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_a_results_page_redirects(self, client: TestClient, stranger: TestClient) -> None:
        run_id = _upload(client)
        assert stranger.get(f"/runs/{run_id}").headers["location"] == "/login"

    def test_a_download_redirects_rather_than_serving(
        self, client: TestClient, stranger: TestClient
    ) -> None:
        """The files are the customer data; this is the one that matters."""
        run_id = _upload(client)
        response = stranger.get(f"/runs/{run_id}/files/result.json")
        assert response.status_code == 303
        assert "payments" not in response.text

    def test_a_delete_redirects(self, client: TestClient, stranger: TestClient) -> None:
        run_id = _upload(client)
        assert stranger.post(f"/runs/{run_id}/delete").status_code == 303
        assert client.get(f"/runs/{run_id}").status_code == 200


class TestUnsafeResults:
    def test_a_broken_money_law_is_reported_not_rendered(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the engine discards its own result, there is no table to show."""
        from settle.domain.errors import InvariantViolation
        from settle_app import runs_routes

        def explode(**_: object) -> None:
            raise InvariantViolation("allocations exceed payments by 0.01")

        monkeypatch.setattr(runs_routes, "execute", explode)
        response = client.post(
            "/runs",
            data={"csrf_token": _csrf(client)},
            files={
                "invoices": ("invoices.csv", INVOICES, "text/csv"),
                "payments": ("bank.csv", PAYMENTS, "text/csv"),
            },
        )
        assert response.status_code == 500
        assert "discarded" in response.text

    def test_the_failure_is_recorded_against_the_run(
        self, client: TestClient, settings: WebSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from settle.domain.errors import InvariantViolation
        from settle_app import runs_routes
        from settle_app.store import STATUS_FAILED, RunStore

        def explode(**_: object) -> None:
            raise InvariantViolation("boom")

        monkeypatch.setattr(runs_routes, "execute", explode)
        client.post(
            "/runs",
            data={"csrf_token": _csrf(client)},
            files={
                "invoices": ("invoices.csv", INVOICES, "text/csv"),
                "payments": ("bank.csv", PAYMENTS, "text/csv"),
            },
        )
        rows = RunStore(settings.database_path).list_runs()
        assert [row.status for row in rows] == [STATUS_FAILED]


class TestEscaping:
    def test_a_script_tag_in_a_counterparty_is_escaped(self, client: TestClient) -> None:
        """Customer names arrive from a CSV somebody else produced."""
        payments = PAYMENTS.replace("Acme Kft,c1", "<script>alert(1)</script>,c1")
        run_id = _upload(client, payments=payments)
        page = client.get(f"/runs/{run_id}").text
        assert "<script>alert(1)</script>" not in page


class TestSurvivingARestart:
    def test_a_run_is_still_readable_after_the_process_restarts(
        self, settings: WebSettings
    ) -> None:
        """The whole reason for a database rather than an in-memory cache."""
        with TestClient(create_app(settings), follow_redirects=False) as client:
            client.post("/login", data={"password": PASSWORD})
            run_id = _upload(client)

        with TestClient(create_app(settings), follow_redirects=False) as reborn:
            reborn.post("/login", data={"password": PASSWORD})
            page = reborn.get(f"/runs/{run_id}")
            assert page.status_code == 200
            assert "Allocations" in page.text
            body = reborn.get(f"/runs/{run_id}/files/result.json").text
            assert json.loads(body)["report"]["payments"]["total"] == 3
