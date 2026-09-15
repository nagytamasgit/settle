"""Doing one run: read the files, call the engine, write what it produced.

This is the only place the app touches :func:`~settle.domain.match.reconcile`,
and it is deliberately dull. The CSVs are read with the engine's own readers and
the outputs written with the engine's own writer, so the files a run leaves
behind are byte-identical to the ones ``settle run`` would have written for the
same input. An integration test asserts exactly that, because the moment the app
starts formatting its own version of the answer, there are two implementations
to keep in agreement and only one of them is tested by the engine's suite.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from settle.domain.config import MatchConfig
from settle.domain.match import reconcile
from settle.domain.models import Invoice, Payment, ReconciliationResult
from settle.io import csv_io


class TooManyRowsError(Exception):
    """The upload is larger than this instance is configured to handle.

    Not a failure of the engine — ``settle run`` will process a ledger of any
    size. It is a refusal to hold a synchronous HTTP request open while a
    bounded-exponential search works through a statement nobody meant to upload.
    """


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """The result, plus the inputs the display layer needs to join against."""

    result: ReconciliationResult
    invoices: list[Invoice]
    payments: list[Payment]


def count_rows(path: Path) -> int:
    """Data rows, not counting the header."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return max(0, sum(1 for _ in csv.reader(handle)) - 1)


def execute(
    *,
    invoices_path: Path,
    payments_path: Path,
    out_dir: Path,
    config: MatchConfig,
    max_rows: int,
) -> RunOutcome:
    """Reconcile two canonical CSVs and write the four output files.

    Raises:
        TooManyRowsError: either file exceeds the configured cap.
        LedgerError: the input is unusable; the message names the line.
        InvariantViolation: the engine discarded its own result, which the
            caller must surface rather than paper over.
    """
    for path, label in ((invoices_path, "invoice"), (payments_path, "bank statement")):
        rows = count_rows(path)
        if rows > max_rows:
            raise TooManyRowsError(
                f"the {label} file has {rows:,} rows, and this instance accepts "
                f"{max_rows:,}. Use the command line for a ledger this size: "
                "settle run invoices.csv bank.csv --out results/"
            )

    invoices = csv_io.read_invoices(invoices_path)
    payments = csv_io.read_payments(payments_path)
    result = reconcile(invoices, payments, config=config)
    csv_io.write_all(out_dir, result)
    return RunOutcome(result=result, invoices=invoices, payments=payments)
