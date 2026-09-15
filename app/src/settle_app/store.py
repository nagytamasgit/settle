"""Run history, in SQLite, with no ORM.

The engine is a pure function and has no database; that is recorded in the
engine's own decision log and stays true. This app is one of its callers, and
callers are where persistence was always supposed to live.

What is stored here is *outputs* — what a run reported. No stored row is ever
read back into :func:`~settle.domain.match.reconcile`, so the determinism the
engine proves about itself is unaffected by anything in this file.

Money is ``TEXT``. A ``REAL`` column would reintroduce, in storage, precisely
the bug the engine spends its entire design avoiding.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

#: Forward-only. Each entry is applied once, in order, inside one transaction,
#: before the socket binds. There is no downgrade: rolling back a release means
#: restoring the backup taken before it.
MIGRATIONS: Final[tuple[str, ...]] = (
    """
    CREATE TABLE runs (
      id             TEXT PRIMARY KEY,
      created_at     TEXT NOT NULL,
      label          TEXT NOT NULL DEFAULT '',
      status         TEXT NOT NULL,
      engine_version TEXT NOT NULL DEFAULT '',
      config_json    TEXT NOT NULL DEFAULT '{}',
      report_json    TEXT,
      invoice_count  INTEGER NOT NULL DEFAULT 0,
      payment_count  INTEGER NOT NULL DEFAULT 0,
      review_count   INTEGER NOT NULL DEFAULT 0,
      money_in       TEXT NOT NULL DEFAULT '0',
      error          TEXT
    );
    CREATE INDEX runs_created_idx ON runs (created_at DESC);

    CREATE TABLE run_mappings (
      run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
      kind   TEXT NOT NULL,
      target TEXT NOT NULL,
      source TEXT NOT NULL,
      origin TEXT NOT NULL
    );
    CREATE INDEX run_mappings_run_idx ON run_mappings (run_id);

    CREATE TABLE access_log (
      at     TEXT NOT NULL,
      ip     TEXT NOT NULL,
      action TEXT NOT NULL,
      run_id TEXT
    );
    CREATE INDEX access_log_at_idx ON access_log (at DESC);
    """,
)

STATUS_DRAFT: Final = "draft"
STATUS_COMPLETE: Final = "complete"
STATUS_FAILED: Final = "failed"
STATUSES: Final = frozenset({STATUS_DRAFT, STATUS_COMPLETE, STATUS_FAILED})

#: Columns a URL is allowed to sort by. Anything else is ignored rather than
#: interpolated — the query is built from this tuple, never from user input.
SORTABLE: Final = ("created_at", "money_in", "review_count", "payment_count")


class StoreError(Exception):
    """The database is not in a state this version can use."""


@dataclass(frozen=True, slots=True)
class RunRow:
    """One row of run history, as the list and detail pages need it."""

    id: str
    created_at: str
    label: str
    status: str
    engine_version: str
    invoice_count: int
    payment_count: int
    review_count: int
    money_in: str
    error: str | None

    @property
    def is_complete(self) -> bool:
        return self.status == STATUS_COMPLETE


class RunStore:
    """Every statement in here is parameterised. There is no string building."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """A connection per operation.

        SQLite is happy with this, and it sidesteps the thread-affinity problem
        entirely: FastAPI runs synchronous handlers in a worker pool, so a
        long-lived connection would need either a lock or
        ``check_same_thread=False`` and a promise nobody can keep.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path, timeout=10.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self) -> None:
        """Bring the schema up to date. Safe to call on every start."""
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
            )
            row = connection.execute("SELECT version FROM schema_version").fetchone()
            applied = 0 if row is None else int(row["version"])
            if applied > len(MIGRATIONS):
                raise StoreError(
                    f"{self._path} is at schema version {applied}, but this build only "
                    f"knows {len(MIGRATIONS)}. It was written by a newer version of "
                    "settle-app; upgrade rather than downgrade."
                )
            for statement in MIGRATIONS[applied:]:
                connection.executescript(statement)
            if row is None:
                connection.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (len(MIGRATIONS),)
                )
            else:
                connection.execute("UPDATE schema_version SET version = ?", (len(MIGRATIONS),))

    def create_draft(self, run_id: str, *, label: str = "", config_json: str = "{}") -> None:
        """A run exists from the moment files are accepted, before it is run."""
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO runs (id, created_at, label, status, config_json) VALUES (?,?,?,?,?)",
                (run_id, _now(), label, STATUS_DRAFT, config_json),
            )

    def mark_complete(
        self,
        run_id: str,
        *,
        engine_version: str,
        report_json: str,
        invoice_count: int,
        payment_count: int,
        review_count: int,
        money_in: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE runs SET status=?, engine_version=?, report_json=?, invoice_count=?,"
                " payment_count=?, review_count=?, money_in=?, error=NULL WHERE id=?",
                (
                    STATUS_COMPLETE,
                    engine_version,
                    report_json,
                    invoice_count,
                    payment_count,
                    review_count,
                    money_in,
                    run_id,
                ),
            )

    def mark_failed(self, run_id: str, *, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE runs SET status=?, error=? WHERE id=?", (STATUS_FAILED, error, run_id)
            )

    def get(self, run_id: str) -> RunRow | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return None if row is None else _to_row(row)

    def report_json(self, run_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT report_json FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        return None if row is None else row["report_json"]

    def count(self, *, status: str | None = None) -> int:
        clause, params = _status_clause(status)
        with self._connect() as connection:
            row = connection.execute(f"SELECT COUNT(*) AS n FROM runs{clause}", params).fetchone()
        return int(row["n"])

    def list_runs(
        self,
        *,
        limit: int = 25,
        offset: int = 0,
        status: str | None = None,
        sort: str = "created_at",
    ) -> list[RunRow]:
        column = sort if sort in SORTABLE else "created_at"
        clause, params = _status_clause(status)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM runs{clause} ORDER BY {column} DESC, id DESC LIMIT ? OFFSET ?",
                (*params, max(1, limit), max(0, offset)),
            ).fetchall()
        return [_to_row(row) for row in rows]

    def delete(self, run_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        return cursor.rowcount > 0

    def save_mapping(self, run_id: str, entries: Sequence[tuple[str, str, str, str]]) -> None:
        """``(kind, target, source, origin)`` — what was proposed and confirmed."""
        with self._connect() as connection:
            connection.execute("DELETE FROM run_mappings WHERE run_id = ?", (run_id,))
            connection.executemany(
                "INSERT INTO run_mappings (run_id, kind, target, source, origin)"
                " VALUES (?,?,?,?,?)",
                [(run_id, *entry) for entry in entries],
            )

    def mapping(self, run_id: str) -> list[tuple[str, str, str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT kind, target, source, origin FROM run_mappings WHERE run_id = ?",
                (run_id,),
            ).fetchall()
        return [(r["kind"], r["target"], r["source"], r["origin"]) for r in rows]

    def log_access(self, *, ip: str, action: str, run_id: str | None = None) -> None:
        """Partial accountability: one shared password cannot say *who*."""
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO access_log (at, ip, action, run_id) VALUES (?,?,?,?)",
                (_now(), ip, action, run_id),
            )

    def expired_run_ids(self, *, retention_days: int, now: datetime | None = None) -> list[str]:
        """Runs past the retention window, oldest first."""
        moment = now or datetime.now(UTC)
        cutoff = (moment - timedelta(days=retention_days)).isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM runs WHERE created_at < ? ORDER BY created_at", (cutoff,)
            ).fetchall()
        return [row["id"] for row in rows]

    def stale_draft_ids(
        self, *, older_than_hours: int = 24, now: datetime | None = None
    ) -> list[str]:
        """Uploads that were never confirmed. They still hold customer data."""
        moment = now or datetime.now(UTC)
        cutoff = (moment - timedelta(hours=older_than_hours)).isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM runs WHERE status = ? AND created_at < ?",
                (STATUS_DRAFT, cutoff),
            ).fetchall()
        return [row["id"] for row in rows]

    def backup_to(self, destination: Path) -> None:
        """SQLite's online backup API.

        Copying the file while the app is running captures a torn database,
        because WAL means the newest commits are not in it yet.
        """
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            target = sqlite3.connect(destination)
            try:
                connection.backup(target)
            finally:
                target.close()


def _status_clause(status: str | None) -> tuple[str, tuple[Any, ...]]:
    if status is None or status not in STATUSES:
        return "", ()
    return " WHERE status = ?", (status,)


def _to_row(row: sqlite3.Row) -> RunRow:
    return RunRow(
        id=row["id"],
        created_at=row["created_at"],
        label=row["label"],
        status=row["status"],
        engine_version=row["engine_version"],
        invoice_count=int(row["invoice_count"]),
        payment_count=int(row["payment_count"]),
        review_count=int(row["review_count"]),
        money_in=row["money_in"],
        error=row["error"],
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()
