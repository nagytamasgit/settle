"""Deleting what should no longer be here, and the run list at scale.

Retention is the part that makes persistence defensible. An app that keeps other
people's bank statements forever because nobody wrote the sweep is not one to
hand a finance team.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from settle_app.app import PAGE_SIZE, create_app
from settle_app.artifacts import ArtifactStore, new_run_id
from settle_app.cli import app as cli
from settle_app.maintenance import STALE_DRAFT_HOURS, sweep
from settle_app.settings import WebSettings
from settle_app.store import STATUS_COMPLETE, STATUS_DRAFT, RunStore

PASSWORD = "correct-horse-battery-staple"
runner = CliRunner()


@pytest.fixture
def settings(tmp_path: Path) -> WebSettings:
    return WebSettings(data_dir=tmp_path, password=PASSWORD, retention_days=30)


def _seed(settings: WebSettings, *, status: str, age_days: float) -> str:
    """A run of a given age, with a file on disk, written directly."""
    store = RunStore(settings.database_path)
    store.migrate()
    artifacts = ArtifactStore(settings.runs_dir)

    run_id = new_run_id()
    store.create_draft(run_id)
    if status == STATUS_COMPLETE:
        store.mark_complete(
            run_id,
            engine_version="0.1.0",
            report_json="{}",
            invoice_count=1,
            payment_count=1,
            review_count=0,
            money_in="1.00",
        )
    when = (datetime.now(UTC) - timedelta(days=age_days)).isoformat()
    with store._connect() as connection:
        connection.execute("UPDATE runs SET created_at = ? WHERE id = ?", (when, run_id))

    (artifacts.ensure(run_id) / "result.json").write_text("{}", encoding="utf-8")
    return run_id


def _sweep(settings: WebSettings) -> object:
    return sweep(
        store=RunStore(settings.database_path),
        artifacts=ArtifactStore(settings.runs_dir),
        settings=settings,
    )


class TestRetention:
    def test_an_old_run_is_deleted_with_its_files(self, settings: WebSettings) -> None:
        run_id = _seed(settings, status=STATUS_COMPLETE, age_days=40)
        _sweep(settings)
        assert RunStore(settings.database_path).get(run_id) is None
        assert not (settings.runs_dir / run_id).exists()

    def test_a_recent_run_is_kept(self, settings: WebSettings) -> None:
        run_id = _seed(settings, status=STATUS_COMPLETE, age_days=5)
        _sweep(settings)
        assert RunStore(settings.database_path).get(run_id) is not None

    def test_without_a_window_nothing_expires(self, tmp_path: Path) -> None:
        """Keeping data forever must be a decision, but it is a valid one."""
        settings = WebSettings(data_dir=tmp_path, password=PASSWORD, retention_days=None)
        run_id = _seed(settings, status=STATUS_COMPLETE, age_days=4000)
        _sweep(settings)
        assert RunStore(settings.database_path).get(run_id) is not None


class TestAbandonedDrafts:
    def test_an_abandoned_upload_is_cleaned_up(self, settings: WebSettings) -> None:
        """Somebody closed the tab. Their bank statement is still on disk."""
        run_id = _seed(settings, status=STATUS_DRAFT, age_days=2)
        _sweep(settings)
        assert RunStore(settings.database_path).get(run_id) is None
        assert not (settings.runs_dir / run_id).exists()

    def test_a_fresh_draft_is_left_alone(self, settings: WebSettings) -> None:
        run_id = _seed(settings, status=STATUS_DRAFT, age_days=0)
        _sweep(settings)
        assert RunStore(settings.database_path).get(run_id) is not None

    def test_drafts_are_cleaned_even_without_a_retention_window(self, tmp_path: Path) -> None:
        settings = WebSettings(data_dir=tmp_path, password=PASSWORD)
        run_id = _seed(settings, status=STATUS_DRAFT, age_days=2)
        _sweep(settings)
        assert RunStore(settings.database_path).get(run_id) is None

    def test_the_window_is_a_day_not_a_minute(self) -> None:
        assert STALE_DRAFT_HOURS >= 12


class TestCounts:
    def test_the_sweep_reports_what_it_did(self, settings: WebSettings) -> None:
        _seed(settings, status=STATUS_COMPLETE, age_days=40)
        _seed(settings, status=STATUS_DRAFT, age_days=2)
        _seed(settings, status=STATUS_COMPLETE, age_days=1)
        result = _sweep(settings)
        assert (result.expired, result.abandoned, result.total) == (1, 1, 2)

    def test_a_run_is_never_counted_twice(self, settings: WebSettings) -> None:
        """An old draft is both expired and abandoned; it is one deletion."""
        _seed(settings, status=STATUS_DRAFT, age_days=40)
        result = _sweep(settings)
        assert result.total == 1


class TestItRunsOnStartup:
    def test_starting_the_app_cleans_up(self, settings: WebSettings) -> None:
        run_id = _seed(settings, status=STATUS_COMPLETE, age_days=40)
        with TestClient(create_app(settings), follow_redirects=False):
            pass
        assert RunStore(settings.database_path).get(run_id) is None


class TestPurgeCommand:
    def test_it_deletes_and_reports(
        self, settings: WebSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The retention window comes from the environment, as it does in prod."""
        _seed(settings, status=STATUS_COMPLETE, age_days=40)
        monkeypatch.setenv("SETTLE_WEB_RETENTION_DAYS", "30")
        result = runner.invoke(cli, ["purge", "--data-dir", str(settings.data_dir)])
        assert result.exit_code == 0, result.output
        assert "1 expired" in result.output

    def test_it_says_so_when_nothing_can_expire(self, tmp_path: Path) -> None:
        settings = WebSettings(data_dir=tmp_path, password=PASSWORD)
        _seed(settings, status=STATUS_COMPLETE, age_days=4000)
        result = runner.invoke(cli, ["purge", "--data-dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "no retention window" in result.output


class TestRunList:
    @pytest.fixture
    def client(self, tmp_path: Path) -> Iterator[TestClient]:
        settings = WebSettings(data_dir=tmp_path, password=PASSWORD)
        for _ in range(PAGE_SIZE + 3):
            _seed(settings, status=STATUS_COMPLETE, age_days=1)
        _seed(settings, status=STATUS_DRAFT, age_days=0)
        with TestClient(create_app(settings), follow_redirects=False) as client:
            client.post("/login", data={"password": PASSWORD})
            yield client

    def test_the_first_page_is_capped(self, client: TestClient) -> None:
        page = client.get("/runs").text
        assert page.count('<span class="pill') == PAGE_SIZE

    def test_there_is_a_second_page(self, client: TestClient) -> None:
        assert "Older" in client.get("/runs").text
        assert "Newer" in client.get("/runs?page=2").text

    def test_a_page_past_the_end_clamps_rather_than_empties(self, client: TestClient) -> None:
        assert "Page 2 of 2" in client.get("/runs?page=99").text

    def test_a_page_before_the_start_clamps_too(self, client: TestClient) -> None:
        assert "Page 1 of 2" in client.get("/runs?page=-5").text

    def test_filtering_by_status_narrows_the_list(self, client: TestClient) -> None:
        page = client.get("/runs?status=draft").text
        assert page.count('<span class="pill') == 1

    @pytest.mark.parametrize(
        "query",
        ["status=' OR 1=1 --", "sort=amount; DROP TABLE runs", "status=nonsense", "page=abc"],
    )
    def test_a_crafted_query_string_cannot_reach_the_sql(
        self, client: TestClient, query: str
    ) -> None:
        response = client.get(f"/runs?{query}")
        assert response.status_code in (200, 422)
        assert client.get("/runs").status_code == 200
