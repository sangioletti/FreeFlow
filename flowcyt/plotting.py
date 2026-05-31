"""
plotting.py - Density-coloured scatter plots and summary charts.

Uses a fast 2-D histogram approach for density estimation (no scipy needed).
Provides:
  * density_scatter  – 2-D scatter coloured by local point density
  * draw_gate_overlay – draw polygon/rectangle outlines on an Axes
  * summary_bar_chart – bar chart of gated population percentages
  * summary_histogram – overlaid histograms per gate for a chosen channel
"""

from __future__ import annotations

import numpy as np
import matplotlib.patheffects as path_effects
from matplotlib.patches import Polygon as MplPolygon

from .gating import Gate, QuadrantGate, ThresholdGate


# White outline applied to every gate label so the text stays readable
# against busy density plots without painting a solid rectangle over the
# data points underneath.
_LABEL_OUTLINE = [path_effects.withStroke(linewidth=2.5, foreground="white")]


# ------------------------------------------------------------------ #
#  Fast histogram-based density (replaces scipy.stats.gaussian_kde)
# ------------------------------------------------------------------ #

def _hist_density(x: np.ndarray, y: np.ndarray, bins: int = 200) -> np.ndarray:
    """
    Estimate point density via a 2-D histogram lookup in arcsinh-transformed
    space.  Flow cytometry data spans many decades, so linear bins produce
    banding artifacts — most bins cover the sparse high range while the dense
    low range gets too few bins.  arcsinh (like log but handles 0 / negative)
    gives uniform bin widths across decades.

    Each point gets the density value of the bin it falls into.
    Fast and dependency-free.
    """
    n = len(x)
    if n == 0:
        return np.array([], dtype=np.float64)

    # Transform to arcsinh space for even bin spacing across decades
    # cofactor=150 is a standard choice for flow cytometry
    cofactor = 150.0
    x_t = np.arcsinh(x / cofactor)
    y_t = np.arcsinh(y / cofactor)

    # Build 2D histogram in transformed space
    hist, xedges, yedges = np.histogram2d(x_t, y_t, bins=bins)
    nbins_x = hist.shape[0]
    nbins_y = hist.shape[1]

    # Map each transformed point to a bin index (clamp to valid range)
    xi = np.clip(
        np.searchsorted(xedges, x_t, side="right") - 1, 0, nbins_x - 1
    )
    yi = np.clip(
        np.searchsorted(yedges, y_t, side="right") - 1, 0, nbins_y - 1
    )
    density = hist[xi, yi]
    return density


# ------------------------------------------------------------------ #
#  Density scatter
# ------------------------------------------------------------------ #

def density_scatter(
    ax,
    x: np.ndarray,
    y: np.ndarray,
    *,
    cmap: str = "jet",
    point_size: float = 1.0,
    alpha: float = 0.6,
    density_bins: int = 200,
):
    """
    Draw a scatter plot on *ax* where each point is coloured by local
    density — the standard "flow cytometry look".

    Guards against renderer crashes from NaN/Inf and degenerate data.
    """
    ax.clear()
    n = len(x)
    if n == 0:
        return

    # ── Sanitise data: drop NaN / Inf  ──
    finite = np.isfinite(x) & np.isfinite(y)
    if not finite.all():
        x = x[finite]
        y = y[finite]
        n = len(x)
        if n == 0:
            return

    # ── Adaptive bin count: reduce bins when range is degenerate  ──
    # Very small ranges cause histogram2d to produce a near-singular grid
    # which can lead to renderer issues on interactive backends.
    effective_bins = density_bins
    x_range = float(np.ptp(x))
    y_range = float(np.ptp(y))
    if x_range == 0 or y_range == 0:
        # All points identical on one axis — fall back to uniform colour
        ax.scatter(x, y, c="steelblue", s=point_size, alpha=alpha,
                   edgecolors="none", rasterized=True)
        return
    if n < density_bins:
        effective_bins = max(10, n // 2)

    try:
        density = _hist_density(x, y, bins=effective_bins)
    except Exception:
        # Fall back to uniform colour on any histogram failure
        ax.scatter(x, y, c="steelblue", s=point_size, alpha=alpha,
                   edgecolors="none", rasterized=True)
        return

    density = np.log1p(density)  # log scale for visual spread

    order = density.argsort()
    ax.scatter(
        x[order],
        y[order],
        c=density[order],
        s=point_size,
        alpha=alpha,
        cmap=cmap,
        edgecolors="none",
        rasterized=True,
    )


# ------------------------------------------------------------------ #
#  Gate overlays
# ------------------------------------------------------------------ #

def draw_gate_overlay(ax, gate: Gate, linewidth: float = 2.0,
                      label_text: str | None = None,
                      quadrant_stats: dict[str, dict] | None = None):
    """Draw the outline of a gate on *ax*.

    *label_text* overrides the default label (gate.name).
    *quadrant_stats* is only meaningful for :class:`QuadrantGate` — when
    supplied, each quadrant is labelled with its own count + percentage
    (instead of the single label that previously appeared only on the
    selected quadrant).
    """
    # Special handling for QuadrantGate — draw crosshair lines
    if isinstance(gate, QuadrantGate):
        _draw_quadrant_overlay(ax, gate, linewidth, label_text, quadrant_stats)
        return

    # Special handling for ThresholdGate — draw vertical line + shading
    if isinstance(gate, ThresholdGate):
        _draw_threshold_overlay(ax, gate, linewidth, label_text)
        return

    verts = gate.vertices
    if not verts:
        return
    display_label = label_text if label_text is not None else gate.name
    poly = MplPolygon(
        verts,
        closed=True,
        fill=False,
        edgecolor=gate.color,
        linewidth=linewidth,
        linestyle="--",
        label=gate.name,
    )
    ax.add_patch(poly)
    # Label near first vertex.  The text uses a white-stroke path effect
    # so it stays legible on dense scatter without a solid background
    # rectangle hiding the points underneath.
    txt = ax.annotate(
        display_label,
        xy=verts[0],
        fontsize=8,
        fontweight="bold",
        color=gate.color,
    )
    txt.set_path_effects(_LABEL_OUTLINE)


def _draw_quadrant_overlay(ax, gate: QuadrantGate, linewidth: float = 2.0,
                           label_text: str | None = None,
                           quadrant_stats: dict[str, dict] | None = None):
    """Draw the crosshair, shade the selected quadrant, and label each
    quadrant with its count + percentage.

    If ``quadrant_stats`` is omitted, falls back to the legacy
    single-label-on-the-selected-quadrant behaviour.
    """
    mx, my = gate.mid_x, gate.mid_y

    ax.axvline(mx, color=gate.color, lw=linewidth, ls="--", alpha=0.6)
    ax.axhline(my, color=gate.color, lw=linewidth, ls="--", alpha=0.6)

    xlim = ax.get_xlim()
    ylim = ax.get_ylim()

    shade_coords = {
        "Q1": (mx, xlim[1], my, ylim[1]),
        "Q2": (xlim[0], mx, my, ylim[1]),
        "Q3": (xlim[0], mx, ylim[0], my),
        "Q4": (mx, xlim[1], ylim[0], my),
    }
    if gate.quadrant in shade_coords:
        x0, x1, y0, y1 = shade_coords[gate.quadrant]
        ax.fill_between(
            [x0, x1], y0, y1,
            color=gate.color, alpha=0.10,
        )

    if not quadrant_stats:
        # Legacy fallback: single label at the crosshair.
        display_label = label_text if label_text is not None else gate.name
        txt = ax.annotate(
            display_label,
            xy=(mx, my), xytext=(5, 5), textcoords="offset points",
            fontsize=8, fontweight="bold", color=gate.color,
        )
        txt.set_path_effects(_LABEL_OUTLINE)
        return

    # Per-quadrant labels.  Position each label at a point that mostly
    # works on both linear and log axes: the midpoint between the
    # crosshair and the corresponding axis edge in *display* coordinates,
    # converted back to data.  Falls back to linear midpoints if the
    # display-coord round-trip fails.
    corners = {
        "Q1": (xlim[1], ylim[1]),
        "Q2": (xlim[0], ylim[1]),
        "Q3": (xlim[0], ylim[0]),
        "Q4": (xlim[1], ylim[0]),
    }
    halign = {"Q1": "left",  "Q2": "right", "Q3": "right", "Q4": "left"}
    valign = {"Q1": "top",   "Q2": "top",   "Q3": "bottom", "Q4": "bottom"}

    try:
        to_display = ax.transData.transform
        to_data = ax.transData.inverted().transform
        mid_disp = to_display((mx, my))
    except Exception:
        to_display = to_data = None
        mid_disp = None

    for q, (cx, cy) in corners.items():
        s = quadrant_stats.get(q)
        if not s:
            continue
        if mid_disp is not None and to_display is not None and to_data is not None:
            try:
                corner_disp = to_display((cx, cy))
                # 60% of the way from the crosshair to the corner reads
                # cleanly in both linear and log scales.
                lx = mid_disp[0] + 0.6 * (corner_disp[0] - mid_disp[0])
                ly = mid_disp[1] + 0.6 * (corner_disp[1] - mid_disp[1])
                px, py = to_data((lx, ly))
            except Exception:
                px = (mx + cx) / 2.0
                py = (my + cy) / 2.0
        else:
            px = (mx + cx) / 2.0
            py = (my + cy) / 2.0

        is_selected = (q == gate.quadrant)
        text = f"{q}: {s.get('count', 0):,}  ({s.get('percent', 0.0):.1f}%)"
        txt = ax.text(
            px, py, text,
            ha="center", va="center",
            fontsize=9 if is_selected else 8,
            fontweight="bold" if is_selected else "normal",
            color=gate.color if is_selected else "#333333",
        )
        txt.set_path_effects(_LABEL_OUTLINE)


def _draw_threshold_overlay(ax, gate: ThresholdGate, linewidth: float = 2.0,
                             label_text: str | None = None):
    """Draw a vertical threshold line and shade the selected side."""
    display_label = label_text if label_text is not None else gate.name
    tx = gate.threshold

    ax.axvline(tx, color=gate.color, lw=linewidth, ls="--", alpha=0.7)

    xlim = ax.get_xlim()
    ylim = ax.get_ylim()
    if gate.side == "left":
        ax.axvspan(xlim[0], tx, color=gate.color, alpha=0.08)
    else:
        ax.axvspan(tx, xlim[1], color=gate.color, alpha=0.08)

    # Label near the threshold line
    txt = ax.annotate(
        display_label,
        xy=(tx, ylim[1] * 0.9 if ylim[1] > 0 else ylim[0] * 0.1),
        xytext=(5, -10),
        textcoords="offset points",
        fontsize=8,
        fontweight="bold",
        color=gate.color,
    )
    txt.set_path_effects(_LABEL_OUTLINE)


# ------------------------------------------------------------------ #
#  Summary charts
# ------------------------------------------------------------------ #

def summary_bar_chart(ax, stats: list[dict]):
    """Horizontal bar chart of gated population percentages."""
    ax.clear()
    if not stats:
        ax.text(
            0.5, 0.5, "No gates defined",
            ha="center", va="center",
            transform=ax.transAxes, fontsize=12, color="grey",
        )
        return

    names = [s["name"] for s in stats]
    pcts = [s["percent"] for s in stats]
    colors = [s["color"] for s in stats]

    y_pos = np.arange(len(names))
    bars = ax.barh(y_pos, pcts, color=colors, edgecolor="black", height=0.6)
    ax.set_yticks(y_pos)
    ax.set_yticklabels(names)
    ax.set_xlabel("% of Total Events")
    ax.set_title("Gated Populations")
    ax.set_xlim(0, max(pcts) * 1.25 if pcts else 100)

    for bar, pct, s in zip(bars, pcts, stats):
        ax.text(
            bar.get_width() + 0.5,
            bar.get_y() + bar.get_height() / 2,
            f"{pct:.1f}%  ({s['count']:,}/{s['total']:,})",
            va="center", fontsize=9,
        )


def summary_histogram(
    ax,
    data: np.ndarray,
    channel_idx: int,
    channel_name: str,
    gates: list,
    channel_names: list[str],
    bins: int = 128,
):
    """
    Overlaid histograms: full population in grey, each gate in its colour.
    """
    ax.clear()
    col = data[:, channel_idx]

    ax.hist(
        col, bins=bins, color="lightgrey", edgecolor="grey",
        alpha=0.5, label="All events", density=True,
    )

    for gate in gates:
        try:
            xi = channel_names.index(gate.x_channel)
            yi = channel_names.index(gate.y_channel)
        except ValueError:
            continue
        mask = gate.contains(data[:, xi], data[:, yi])
        if mask.sum() > 0:
            ax.hist(
                col[mask], bins=bins,
                color=gate.color, alpha=0.45,
                label=gate.name, density=True,
            )

    ax.set_xlabel(channel_name)
    ax.set_ylabel("Density")
    ax.set_title(f"Histogram — {channel_name}")
    ax.legend(fontsize=8)
