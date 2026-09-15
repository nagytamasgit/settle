"""Files belonging to a run, on disk.

The database holds what a run *reported*; the files are what it actually
produced, written by the engine's own :func:`~settle.io.csv_io.write_all`. The
download links hand back those exact bytes, which is the point: what a finance
system ingests should be the file the engine wrote, not something this app
reassembled from a database and might reassemble differently.

Nothing here accepts a path from a request. A run id must be 32 hex characters
and a filename must be one of a fixed set, so there is no input from which a
traversal could be built — and the resolved path is checked against the root
anyway, because defence that costs one line should not be argued about.
"""

from __future__ import annotations

import re
import secrets
import shutil
from pathlib import Path
from typing import Final

RUN_ID_PATTERN: Final = re.compile(r"^[0-9a-f]{32}$")

#: What the user uploaded, kept verbatim so a mapping can be re-examined.
UPLOAD_INVOICES: Final = "upload-invoices.csv"
UPLOAD_PAYMENTS: Final = "upload-bank.csv"

#: The same data with settle's column names, which is what the engine reads.
CANONICAL_INVOICES: Final = "invoices.csv"
CANONICAL_PAYMENTS: Final = "bank.csv"

#: Written by settle.io.csv_io.write_all, in its order.
RESULT_JSON: Final = "result.json"
ALLOCATIONS_CSV: Final = "allocations.csv"
REVIEW_QUEUE_CSV: Final = "review_queue.csv"
INVOICE_STATES_CSV: Final = "invoice_states.csv"

#: Offered to the user, in the order the results page lists them. The originals
#: are included: when a mapping looks wrong, the first thing anyone wants is the
#: file they actually uploaded, next to the one the engine actually read.
DOWNLOADABLE: Final = (
    ALLOCATIONS_CSV,
    REVIEW_QUEUE_CSV,
    INVOICE_STATES_CSV,
    RESULT_JSON,
    CANONICAL_INVOICES,
    CANONICAL_PAYMENTS,
    UPLOAD_INVOICES,
    UPLOAD_PAYMENTS,
)

#: Everything that may exist in a run directory.
KNOWN_FILES: Final = frozenset({*DOWNLOADABLE, UPLOAD_INVOICES, UPLOAD_PAYMENTS})

HUMAN_NAMES: Final = {
    ALLOCATIONS_CSV: "Allocations",
    REVIEW_QUEUE_CSV: "Review queue",
    INVOICE_STATES_CSV: "Invoice states",
    RESULT_JSON: "Full result (JSON)",
    CANONICAL_INVOICES: "Invoices, as read",
    CANONICAL_PAYMENTS: "Bank lines, as read",
    UPLOAD_INVOICES: "Invoices, as uploaded",
    UPLOAD_PAYMENTS: "Bank lines, as uploaded",
}


def new_run_id() -> str:
    """32 hex characters, unguessable. Also the directory name."""
    return secrets.token_hex(16)


def is_run_id(value: str) -> bool:
    return RUN_ID_PATTERN.match(value) is not None


class ArtifactStore:
    """The run directories, rooted at one place and never leaving it."""

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    def directory(self, run_id: str) -> Path | None:
        """The directory for a run, or ``None`` if the id is not well formed."""
        if not is_run_id(run_id):
            return None
        return self._root / run_id

    def ensure(self, run_id: str) -> Path:
        directory = self.directory(run_id)
        if directory is None:
            raise ValueError(f"not a run id: {run_id!r}")
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def path(self, run_id: str, name: str) -> Path | None:
        """A readable artefact, or ``None``.

        ``None`` covers every rejection — bad id, unknown filename, missing
        file, or a resolved path that somehow escaped the root — because the
        caller's response is 404 in all four cases and distinguishing them for
        the client would only describe the filesystem to them.
        """
        directory = self.directory(run_id)
        if directory is None or name not in KNOWN_FILES:
            return None
        candidate = (directory / name).resolve()
        if not candidate.is_file():
            return None
        if not candidate.is_relative_to(self._root.resolve()):
            return None
        return candidate

    def available(self, run_id: str) -> list[str]:
        """Which downloadable files this run actually has, in display order."""
        return [name for name in DOWNLOADABLE if self.path(run_id, name) is not None]

    def delete(self, run_id: str) -> bool:
        """Remove a run's directory. Deleting must really delete."""
        directory = self.directory(run_id)
        if directory is None or not directory.is_dir():
            return False
        shutil.rmtree(directory)
        return True

    def size_bytes(self, run_id: str) -> int:
        directory = self.directory(run_id)
        if directory is None or not directory.is_dir():
            return 0
        return sum(item.stat().st_size for item in directory.iterdir() if item.is_file())
