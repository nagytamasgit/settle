"""Run the engine at increasing noise levels and write the results.

Three sweeps:

1. **Overall** - all four noise dimensions raised together, 0.0 to 0.5. This is
   the headline: how much mess does the matcher survive?
2. **Each dimension alone** - which kind of mess hurts on its own. The answer
   turns out to be "none of them", because the signals are redundant.
3. **Reference noise plus one other** - which is what removes the redundancy,
   and is where the interesting number lives.

Every number in ``bench/results/`` and in the README comes from here. Nothing is
typed by hand, and CI re-runs this to check the committed numbers still hold.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import statistics
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

import structlog

from bench.generate import NoiseProfile, generate
from bench.metrics import score
from settle.domain.match import ENGINE_VERSION, reconcile

DIMENSIONS = ("reference", "amount", "structure", "collision")
DEFAULT_LEVELS = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5)
DEFAULT_SEEDS = 5
DEFAULT_INVOICES = 400
#: The level used for the isolating sweeps. High enough that a dimension which
#: mattered on its own would clearly show it.
DIMENSION_LEVEL = 0.5

RESULTS_DIR = Path(__file__).parent / "results"


@dataclass(frozen=True, slots=True)
class Row:
    """One measured configuration, averaged over seeds."""

    scenario: str
    level: float
    precision: float
    recall: float
    f1: float
    review_rate: float
    runtime_ms: float
    payments: int
    invoices: int
    seeds: int


def _measure(
    scenario: str, level: float, profile: NoiseProfile, *, seeds: int, invoice_count: int
) -> Row:
    precisions: list[float] = []
    recalls: list[float] = []
    f1s: list[float] = []
    reviews: list[float] = []
    runtimes: list[float] = []
    payments: list[int] = []

    for seed in range(seeds):
        case = generate(seed=seed, invoice_count=invoice_count, noise=profile)
        started = time.perf_counter()
        # verify_invariants stays on: the benchmark measures the engine as it
        # actually runs, safety net included, not a faster unguarded variant.
        result = reconcile(case.invoices, case.payments)
        runtimes.append((time.perf_counter() - started) * 1000)

        measured = score(result, case.true_pairs)
        precisions.append(measured.precision)
        recalls.append(measured.recall)
        f1s.append(measured.f1)
        reviews.append(measured.review_rate)
        payments.append(measured.payments)

    return Row(
        scenario=scenario,
        level=round(level, 3),
        precision=round(statistics.fmean(precisions), 4),
        recall=round(statistics.fmean(recalls), 4),
        f1=round(statistics.fmean(f1s), 4),
        review_rate=round(statistics.fmean(reviews), 4),
        runtime_ms=round(statistics.fmean(runtimes), 1),
        payments=round(statistics.fmean(payments)),
        invoices=invoice_count,
        seeds=seeds,
    )


def _profile(dimensions: tuple[str, ...], level: float) -> NoiseProfile:
    return NoiseProfile(**dict.fromkeys(dimensions, level))


def run(
    *,
    levels: Sequence[float] = DEFAULT_LEVELS,
    seeds: int = DEFAULT_SEEDS,
    invoice_count: int = DEFAULT_INVOICES,
) -> list[Row]:
    """Three sweeps, in a deterministic order.

    The third one exists because the second came back uninformative. Every
    dimension alone scores near-perfectly, since whichever signal the noise
    destroys, another one still identifies the payment. Pairing each dimension
    with reference noise removes that redundancy and shows what actually costs
    recall.
    """
    rows = [
        _measure(
            "overall", level, NoiseProfile.uniform(level), seeds=seeds, invoice_count=invoice_count
        )
        for level in levels
    ]
    rows += [
        _measure(
            f"only:{dimension}",
            DIMENSION_LEVEL,
            _profile((dimension,), DIMENSION_LEVEL),
            seeds=seeds,
            invoice_count=invoice_count,
        )
        for dimension in DIMENSIONS
    ]
    rows += [
        _measure(
            f"reference+{dimension}",
            DIMENSION_LEVEL,
            _profile(("reference", dimension), DIMENSION_LEVEL),
            seeds=seeds,
            invoice_count=invoice_count,
        )
        for dimension in DIMENSIONS
        if dimension != "reference"
    ]
    return rows


def write_csv(rows: list[Row], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(rows[0])))
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def write_markdown(rows: list[Row], path: Path, *, chart_stem: str = "benchmark") -> None:
    overall = [row for row in rows if row.scenario == "overall"]
    alone = [row for row in rows if row.scenario.startswith("only:")]
    paired = [row for row in rows if row.scenario.startswith("reference+")]
    settings = overall[0] if overall else rows[0]

    lines = [
        "# Benchmark results",
        "",
        f"Engine `{ENGINE_VERSION}`. "
        f"{settings.invoices} invoices per run, {settings.seeds} seeds per row, "
        "means reported. Generated by `make bench` — do not edit by hand.",
        "",
        "A payment routed to review is **not** counted as a wrong answer. It costs",
        "recall and shows up in the review column, which is where a finance team",
        "feels it. See `docs/EVAL.md` for what these numbers do and do not mean.",
        "",
        "## Overall: all four noise dimensions together",
        "",
        "| noise | precision | recall | F1 | sent to review | runtime |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for row in overall:
        lines.append(
            f"| {row.level:.1f} | {_pct(row.precision)} | {_pct(row.recall)} | "
            f"{_pct(row.f1)} | {_pct(row.review_rate)} | {row.runtime_ms:.0f} ms |"
        )

    lines += [
        "",
        f"## Each dimension alone, at {DIMENSION_LEVEL:.1f}",
        "",
        "Almost nothing happens. That is the finding, not a broken harness: the",
        "signals are redundant. Destroy the references and the amounts still",
        "identify the payment; collide the amounts and the references still do.",
        "",
        "| noise dimension | precision | recall | F1 | sent to review |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in sorted(alone, key=lambda r: r.recall):
        lines.append(
            f"| {row.scenario.removeprefix('only:')} | {_pct(row.precision)} | "
            f"{_pct(row.recall)} | {_pct(row.f1)} | {_pct(row.review_rate)} |"
        )

    lines += [
        "",
        f"## Reference noise at {DIMENSION_LEVEL:.1f}, plus one other dimension",
        "",
        "Remove the redundancy and the picture changes. Reference noise is what",
        "makes every other kind of mess expensive, because once the identifier is",
        "gone the amount is the only thing left to match on — and the second",
        "dimension is precisely what makes the amount unreliable.",
        "",
        "| also noisy | precision | recall | F1 | sent to review |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in sorted(paired, key=lambda r: r.recall):
        lines.append(
            f"| {row.scenario.removeprefix('reference+')} | {_pct(row.precision)} | "
            f"{_pct(row.recall)} | {_pct(row.f1)} | {_pct(row.review_rate)} |"
        )

    lines += [
        "",
        "## Chart",
        "",
        f"![Benchmark]({chart_stem}-light.png)",
        "",
        "Runtime is wall-clock on the machine that generated this file and is not",
        "comparable across machines; it is here to show the bounded search stays",
        "bounded, not as a performance claim.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def write_chart(rows: list[Row], directory: Path) -> list[Path]:
    from bench.chart import Series, render

    overall = [row for row in rows if row.scenario == "overall"]
    levels = [row.level for row in overall]
    return render(
        levels,
        [
            Series("precision", [row.precision for row in overall]),
            Series("recall", [row.recall for row in overall]),
            Series("sent to review", [row.review_rate for row in overall]),
        ],
        directory=directory,
        subtitle=(
            "Precision holds because the engine declines instead of guessing; "
            "the cost lands on recall and the review queue."
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the settle benchmark.")
    parser.add_argument("--out", type=Path, default=RESULTS_DIR)
    parser.add_argument("--seeds", type=int, default=DEFAULT_SEEDS)
    parser.add_argument("--invoices", type=int, default=DEFAULT_INVOICES)
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Fast, reduced-scale pass for CI: fewer seeds, fewer invoices, fewer levels.",
    )
    parser.add_argument("--no-chart", action="store_true", help="Skip rendering the chart.")
    args = parser.parse_args()

    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL))

    levels = (0.0, 0.2, 0.4) if args.smoke else DEFAULT_LEVELS
    seeds = 2 if args.smoke else args.seeds
    invoices = 120 if args.smoke else args.invoices

    rows = run(levels=levels, seeds=seeds, invoice_count=invoices)
    args.out.mkdir(parents=True, exist_ok=True)
    write_csv(rows, args.out / "results.csv")
    write_markdown(rows, args.out / "results.md")
    (args.out / "meta.json").write_text(
        json.dumps(
            {
                "engine_version": ENGINE_VERSION,
                "levels": list(levels),
                "seeds": seeds,
                "invoices": invoices,
                "dimension_level": DIMENSION_LEVEL,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if not args.no_chart:
        write_chart(rows, args.out)

    for row in rows:
        print(
            f"{row.scenario:>10} {row.level:.1f}  "
            f"P={row.precision:.3f}  R={row.recall:.3f}  "
            f"review={row.review_rate:.3f}  {row.runtime_ms:6.1f} ms"
        )
    print(f"\nwrote {args.out}/results.csv, results.md, meta.json")


if __name__ == "__main__":
    main()
