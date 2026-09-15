"""Verify the committed benchmark numbers are still the numbers you get.

The tables in ``bench/results/`` and the figures quoted in the README are
generated, not typed. If a change to the matcher moves them, the commit has to
move them too — otherwise the README slowly becomes a claim about a version of
the engine that no longer exists.

CI runs this. It re-runs the benchmark at exactly the configuration recorded in
``meta.json`` and compares the quality metrics. Runtime is deliberately not
compared: it is wall-clock on whatever machine happened to run it.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import structlog

from bench.run import RESULTS_DIR, Row, run

#: Metrics are means over several seeded runs of deterministic code, so they
#: should reproduce exactly. The tolerance absorbs last-digit float differences
#: across platforms, nothing more.
TOLERANCE = 0.001

COMPARED = ("precision", "recall", "f1", "review_rate")


def load(directory: Path) -> tuple[list[Row], dict[str, Any]]:
    results = directory / "results.csv"
    meta_path = directory / "meta.json"
    if not results.exists() or not meta_path.exists():
        raise SystemExit(
            f"no committed benchmark in {directory} — run `make bench` and commit the output"
        )
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    rows = []
    with results.open(encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            rows.append(
                Row(
                    scenario=raw["scenario"],
                    level=float(raw["level"]),
                    precision=float(raw["precision"]),
                    recall=float(raw["recall"]),
                    f1=float(raw["f1"]),
                    review_rate=float(raw["review_rate"]),
                    runtime_ms=float(raw["runtime_ms"]),
                    payments=int(raw["payments"]),
                    invoices=int(raw["invoices"]),
                    seeds=int(raw["seeds"]),
                )
            )
    return rows, meta


def compare(committed: list[Row], fresh: list[Row]) -> list[str]:
    """Return a list of human-readable differences; empty means reproduced."""
    problems: list[str] = []
    by_key = {(row.scenario, row.level): row for row in fresh}

    if len(committed) != len(fresh):
        problems.append(f"row count changed: committed {len(committed)}, fresh {len(fresh)}")

    for row in committed:
        key = (row.scenario, row.level)
        current = by_key.get(key)
        if current is None:
            problems.append(f"{key}: present in committed results, missing from a fresh run")
            continue
        committed_values = asdict(row)
        fresh_values = asdict(current)
        for metric in COMPARED:
            difference = abs(committed_values[metric] - fresh_values[metric])
            if difference > TOLERANCE:
                problems.append(
                    f"{row.scenario} @ {row.level}: {metric} "
                    f"committed {committed_values[metric]:.4f}, "
                    f"fresh {fresh_values[metric]:.4f} "
                    f"(differs by {difference:.4f})"
                )
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description="Check committed benchmark results reproduce.")
    parser.add_argument("--results", type=Path, default=RESULTS_DIR)
    args = parser.parse_args()

    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL))

    committed, meta = load(args.results)
    print(
        f"re-running: {meta['invoices']} invoices, {meta['seeds']} seeds, levels {meta['levels']}"
    )
    fresh = run(
        levels=tuple(meta["levels"]),
        seeds=meta["seeds"],
        invoice_count=meta["invoices"],
    )

    problems = compare(committed, fresh)
    if problems:
        print(f"\nthe committed benchmark no longer reproduces ({len(problems)} difference(s)):")
        for problem in problems:
            print(f"  - {problem}")
        print("\nrun `make bench` and commit the updated results.")
        sys.exit(1)

    print(f"reproduced: {len(committed)} rows match within {TOLERANCE}")


if __name__ == "__main__":
    main()
