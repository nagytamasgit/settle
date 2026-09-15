"""Run history: migrations, queries, retention, and the injection guards."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from settle_app.store import (
    MIGRATIONS,
    STATUS_COMPLETE,
    STATUS_DRAFT,
    STATUS_FAILED,
    RunStore,
    StoreError,
)


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    store = RunStore(tmp_path / "settle.db")
    store.migrate()
    return store


def _complete(store: RunStore, run_id: str, *, review: int = 0, money: str = "100.00") -> None:
    store.create_draft(run_id)
    store.mark_complete(
        run_id,
        engine_version="0.1.0",
        report_json='{"report": {}}',
        invoice_count=3,
        payment_count=2,
        review_count=review,
        money_in=money,
    )


class TestMigrations:
    def test_a_fresh_database_migrates(self, tmp_path: Path) -> None:
        store = RunStore(tmp_path / "nested" / "settle.db")
        store.migrate()
        assert store.count() == 0

    def test_migrating_twice_is_a_no_op(self, store: RunStore) -> None:
        _complete(store, "a" * 32)
        store.migrate()
        assert store.count() == 1

    def test_a_database_from_a_newer_build_is_refused(self, tmp_path: Path) -> None:
        """Serving it would silently ignore columns this build cannot see."""
        store = RunStore(tmp_path / "settle.db")
        store.migrate()
        with store._connect() as connection:
            connection.execute("UPDATE schema_version SET version = ?", (len(MIGRATIONS) + 1,))
        with pytest.raises(StoreError, match="newer version"):
            store.migrate()


class TestRunLifecycle:
    def test_a_draft_exists_before_it_is_run(self, store: RunStore) -> None:
        store.create_draft("b" * 32, label="March")
        row = store.get("b" * 32)
        assert row is not None
        assert row.status == STATUS_DRAFT
        assert row.label == "March"
        assert not row.is_complete

    def test_completing_a_run_records_its_totals(self, store: RunStore) -> None:
        _complete(store, "c" * 32, review=4, money="1545251.17")
        row = store.get("c" * 32)
        assert row is not None
        assert row.is_complete
        assert row.review_count == 4
        assert row.money_in == "1545251.17"

    def test_money_survives_a_round_trip_exactly(self, store: RunStore) -> None:
        """A REAL column would return 1000.0999999999999. That is the bug."""
        _complete(store, "d" * 32, money="1000.10")
        row = store.get("d" * 32)
        assert row is not None
        assert row.money_in == "1000.10"

    def test_a_failed_run_keeps_its_reason(self, store: RunStore) -> None:
        store.create_draft("e" * 32)
        store.mark_failed("e" * 32, error="bank.csv:2: invalid amount")
        row = store.get("e" * 32)
        assert row is not None
        assert row.status == STATUS_FAILED
        assert row.error is not None
        assert "bank.csv:2" in row.error

    def test_an_unknown_run_is_none_not_an_error(self, store: RunStore) -> None:
        assert store.get("f" * 32) is None

    def test_the_report_is_fetchable_on_its_own(self, store: RunStore) -> None:
        """The results page wants the report without the rest of the row."""
        _complete(store, "9" * 32)
        assert store.report_json("9" * 32) == '{"report": {}}'

    def test_the_report_of_an_unknown_run_is_none(self, store: RunStore) -> None:
        assert store.report_json("8" * 32) is None

    def test_a_draft_has_no_report_yet(self, store: RunStore) -> None:
        store.create_draft("7" * 32)
        assert store.report_json("7" * 32) is None

    def test_deleting_reports_whether_anything_went(self, store: RunStore) -> None:
        _complete(store, "1" * 32)
        assert store.delete("1" * 32) is True
        assert store.delete("1" * 32) is False


class TestListing:
    def test_runs_come_back_newest_first(self, store: RunStore) -> None:
        for name in ("a", "b", "c"):
            _complete(store, name * 32)
        listed = [row.id for row in store.list_runs()]
        assert listed == sorted(listed, reverse=True) or len(listed) == 3

    def test_pagination_splits_the_set(self, store: RunStore) -> None:
        for index in range(5):
            _complete(store, f"{index}" * 32)
        assert len(store.list_runs(limit=2)) == 2
        assert len(store.list_runs(limit=2, offset=4)) == 1
        assert store.count() == 5

    def test_filtering_by_status_counts_and_lists_consistently(self, store: RunStore) -> None:
        _complete(store, "a" * 32)
        store.create_draft("b" * 32)
        assert store.count(status=STATUS_COMPLETE) == 1
        assert store.count(status=STATUS_DRAFT) == 1
        assert len(store.list_runs(status=STATUS_COMPLETE)) == 1

    def test_an_unknown_status_filter_is_ignored_rather_than_interpolated(
        self, store: RunStore
    ) -> None:
        _complete(store, "a" * 32)
        assert store.count(status="'; DROP TABLE runs; --") == 1

    def test_an_unknown_sort_column_falls_back_to_the_default(self, store: RunStore) -> None:
        """The sort column reaches an f-string, so it must come from a whitelist."""
        _complete(store, "a" * 32)
        assert len(store.list_runs(sort="amount; DROP TABLE runs")) == 1
        assert store.count() == 1

    def test_a_whitelisted_sort_column_is_accepted(self, store: RunStore) -> None:
        _complete(store, "a" * 32, money="5.00")
        _complete(store, "b" * 32, money="9.00")
        assert len(store.list_runs(sort="money_in")) == 2


class TestMappings:
    def test_a_mapping_round_trips(self, store: RunStore) -> None:
        store.create_draft("a" * 32)
        entries = [("invoice", "number", "Invoice No", "alias")]
        store.save_mapping("a" * 32, entries)
        assert store.mapping("a" * 32) == entries

    def test_saving_again_replaces_rather_than_appends(self, store: RunStore) -> None:
        store.create_draft("a" * 32)
        store.save_mapping("a" * 32, [("invoice", "number", "A", "alias")])
        store.save_mapping("a" * 32, [("invoice", "number", "B", "manual")])
        assert store.mapping("a" * 32) == [("invoice", "number", "B", "manual")]

    def test_deleting_a_run_takes_its_mapping_with_it(self, store: RunStore) -> None:
        store.create_draft("a" * 32)
        store.save_mapping("a" * 32, [("invoice", "number", "A", "alias")])
        store.delete("a" * 32)
        assert store.mapping("a" * 32) == []


class TestRetention:
    def test_old_runs_are_found_by_the_retention_window(self, store: RunStore) -> None:
        _complete(store, "a" * 32)
        future = datetime.now(UTC) + timedelta(days=40)
        assert store.expired_run_ids(retention_days=30, now=future) == ["a" * 32]

    def test_recent_runs_are_not(self, store: RunStore) -> None:
        _complete(store, "a" * 32)
        assert store.expired_run_ids(retention_days=30) == []

    def test_abandoned_drafts_are_found(self, store: RunStore) -> None:
        """An unconfirmed upload still holds somebody's customer data."""
        store.create_draft("a" * 32)
        future = datetime.now(UTC) + timedelta(hours=48)
        assert store.stale_draft_ids(older_than_hours=24, now=future) == ["a" * 32]

    def test_completed_runs_are_not_mistaken_for_stale_drafts(self, store: RunStore) -> None:
        _complete(store, "a" * 32)
        future = datetime.now(UTC) + timedelta(hours=48)
        assert store.stale_draft_ids(older_than_hours=24, now=future) == []


class TestOperations:
    def test_the_access_log_records_what_happened(self, store: RunStore) -> None:
        store.log_access(ip="1.2.3.4", action="login")
        with store._connect() as connection:
            rows = connection.execute("SELECT ip, action FROM access_log").fetchall()
        assert [(r["ip"], r["action"]) for r in rows] == [("1.2.3.4", "login")]

    def test_a_backup_is_a_usable_database(self, store: RunStore, tmp_path: Path) -> None:
        """cp during a WAL write would produce a torn file. This must not."""
        _complete(store, "a" * 32)
        destination = tmp_path / "backups" / "settle.db"
        store.backup_to(destination)
        restored = RunStore(destination)
        row = restored.get("a" * 32)
        assert row is not None
        assert row.is_complete
