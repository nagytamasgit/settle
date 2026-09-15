"""Getting rid of data that should no longer be here.

Persisting run history made the operator of this app a data controller, which
nothing else in the repository is. Deletion therefore has to be real — the
database row *and* the files — and it has to happen without anyone remembering
to do it.

Two sweeps, for two different problems:

* **Retention.** Runs older than the configured window. Off by default, because
  "keep customer financial data forever" should be a decision somebody made
  rather than a default they inherited. ``docs/DEPLOY.md`` says so in those
  words.
* **Abandoned drafts.** Somebody uploaded two files, saw the mapping screen and
  closed the tab. The run never completed, so retention would not catch it for a
  year, but the upload is sitting on disk and it is somebody's bank statement.
"""

from __future__ import annotations

from dataclasses import dataclass

from settle_app.artifacts import ArtifactStore
from settle_app.settings import WebSettings
from settle_app.store import RunStore

#: A draft older than this was abandoned, not in progress.
STALE_DRAFT_HOURS = 24


@dataclass(frozen=True, slots=True)
class SweepResult:
    expired: int
    abandoned: int

    @property
    def total(self) -> int:
        return self.expired + self.abandoned


def sweep(*, store: RunStore, artifacts: ArtifactStore, settings: WebSettings) -> SweepResult:
    """Delete what has aged out. Safe to call on every start."""
    expired: list[str] = []
    if settings.retention_days is not None:
        expired = store.expired_run_ids(retention_days=settings.retention_days)

    abandoned = [
        run_id
        for run_id in store.stale_draft_ids(older_than_hours=STALE_DRAFT_HOURS)
        if run_id not in set(expired)
    ]

    for run_id in (*expired, *abandoned):
        # Files first. A row without its directory is a 404; a directory
        # without its row is customer data nothing will ever clean up.
        artifacts.delete(run_id)
        store.delete(run_id)

    return SweepResult(expired=len(expired), abandoned=len(abandoned))
