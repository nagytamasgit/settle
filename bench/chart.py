"""The benchmark chart.

One chart, one axis, three series. Two files are rendered — light and dark —
because a committed PNG cannot respond to a reader's GitHub theme, and an
auto-inverted dark mode is not the same thing as one chosen for the dark
surface.

Palette and mark specs follow the project's data-visualisation rules: the three
categorical slots are taken in fixed order, both sets validated for
colour-vision separation against their own surface. Two of the light slots sit
under 3:1 contrast, which obliges visible direct labels — so every series is
labelled at its right end as well as appearing in the legend, and identity is
never carried by colour alone.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter


@dataclass(frozen=True, slots=True)
class Theme:
    """Everything that changes between the light and dark renderings."""

    name: str
    surface: str
    text_primary: str
    text_secondary: str
    muted: str
    gridline: str
    baseline: str
    #: Categorical slots 1-3, stepped for this surface.
    series: tuple[str, str, str]


LIGHT = Theme(
    name="light",
    surface="#fcfcfb",
    text_primary="#0b0b0b",
    text_secondary="#52514e",
    muted="#898781",
    gridline="#e1e0d9",
    baseline="#c3c2b7",
    series=("#2a78d6", "#1baf7a", "#eda100"),
)

DARK = Theme(
    name="dark",
    surface="#1a1a19",
    text_primary="#ffffff",
    text_secondary="#c3c2b7",
    muted="#898781",
    gridline="#2c2c2a",
    baseline="#383835",
    series=("#3987e5", "#199e70", "#c98500"),
)


@dataclass(frozen=True, slots=True)
class Series:
    label: str
    values: Sequence[float]


def render(
    levels: Sequence[float],
    series: Sequence[Series],
    *,
    directory: Path,
    stem: str = "benchmark",
    title: str = "How far the matcher goes before it needs you",
    subtitle: str = "",
) -> list[Path]:
    """Render the chart once per theme and return the files written."""
    if len(series) > len(LIGHT.series):
        raise ValueError("this chart is specified for at most three series")
    directory.mkdir(parents=True, exist_ok=True)
    return [
        _render_one(levels, series, theme, directory / f"{stem}-{theme.name}.png", title, subtitle)
        for theme in (LIGHT, DARK)
    ]


def _render_one(
    levels: Sequence[float],
    series: Sequence[Series],
    theme: Theme,
    path: Path,
    title: str,
    subtitle: str,
) -> Path:
    fig, ax = plt.subplots(figsize=(9.0, 5.0), dpi=160)
    fig.patch.set_facecolor(theme.surface)
    ax.set_facecolor(theme.surface)

    # Recessive chrome: horizontal hairlines only, no box around the plot.
    ax.grid(axis="y", color=theme.gridline, linewidth=1, zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(theme.baseline)
        ax.spines[side].set_linewidth(1)

    for index, item in enumerate(series):
        colour = theme.series[index]
        ax.plot(
            levels,
            item.values,
            color=colour,
            linewidth=2,
            marker="o",
            markersize=7,
            # A 2px surface ring keeps markers legible where the lines cross.
            markeredgecolor=theme.surface,
            markeredgewidth=2,
            label=item.label,
            zorder=3 + index,
            clip_on=False,
        )
        # Direct label at the right end. The text wears an ink token; the line's
        # own end marker beside it carries the identity.
        ax.annotate(
            item.label,
            xy=(levels[-1], item.values[-1]),
            xytext=(10, 0),
            textcoords="offset points",
            va="center",
            ha="left",
            fontsize=10,
            color=theme.text_secondary,
            zorder=6,
        )

    ax.set_xlim(min(levels), max(levels))
    ax.set_ylim(0, 1.02)
    ax.set_xticks(list(levels))
    ax.yaxis.set_major_formatter(PercentFormatter(xmax=1.0, decimals=0))
    ax.tick_params(axis="both", colors=theme.muted, labelsize=10, length=0)
    for label in (*ax.get_xticklabels(), *ax.get_yticklabels()):
        label.set_color(theme.muted)

    ax.set_xlabel("injected noise level", fontsize=10, color=theme.text_secondary, labelpad=8)

    ax.set_title(title, fontsize=14, color=theme.text_primary, loc="left", pad=24, weight="bold")
    if subtitle:
        ax.text(
            0,
            1.035,
            subtitle,
            transform=ax.transAxes,
            fontsize=10,
            color=theme.text_secondary,
            ha="left",
            va="bottom",
        )

    legend = ax.legend(
        loc="upper left",
        frameon=False,
        fontsize=10,
        ncols=len(series),
        handlelength=1.6,
        columnspacing=2.0,
        bbox_to_anchor=(0, -0.20),
    )
    for text in legend.get_texts():
        text.set_color(theme.text_secondary)

    fig.subplots_adjust(left=0.08, right=0.80, top=0.84, bottom=0.26)
    fig.savefig(path, facecolor=theme.surface, edgecolor="none")
    plt.close(fig)
    return path
