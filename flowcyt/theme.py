"""
theme.py - Cohesive light-theme palette and style helpers for FreeFlow.

The helpers are stateless and operate on already-constructed matplotlib
artists / widgets — they never reposition or recreate anything, so
applying the theme is guaranteed to leave the layout and event wiring
unchanged.  Every helper swallows attribute-name changes between
matplotlib versions so a future API tweak silently degrades to "no
theme on this artist" rather than crashing.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


PALETTE = {
    "window_bg":    "#F7F9FC",   # whole figure facecolor
    "panel_bg":     "#EEF2F7",   # right-side panel backdrop / message log
    "panel_bg_alt": "#E4EAF1",   # slightly darker tint for stats / log
    "accent":       "#1F3F69",   # section headers (deep navy)
    "accent2":      "#2C6BB4",   # active radio dot / divider lines
    "text":         "#1F2937",   # default body text
    "text_dim":     "#5C6878",   # secondary / inactive text
    "btn":          "#E6EBF1",   # button face
    "btn_hover":    "#D5DCE5",   # button hover
    "border":       "#B6C1CD",   # subtle borders
    "grid":         "#D5DCE5",   # plot grid colour
    "spine":        "#778899",   # plot spine colour
}


def _safe(call):
    """Run a setter-style callable, swallowing any exception."""
    try:
        call()
    except Exception:
        pass


# ---------------------------------------------------------------------------- #
#  Top-level
# ---------------------------------------------------------------------------- #

def style_window(fig) -> None:
    """Apply the cohesive window backdrop to a figure."""
    if fig is None:
        return
    _safe(lambda: fig.set_facecolor(PALETTE["window_bg"]))
    try:
        # Match the canvas-host background too so popups don't show a
        # white frame around the figure on Qt / Tk.
        fig.canvas.manager.window  # noqa: B018 – just probe
    except Exception:
        pass


def panel_background(fig, x: float, y: float, w: float, h: float,
                     color: str | None = None) -> None:
    """Drop a frame-less tinted rectangle behind a group of widgets.

    The new axes is added with low ``zorder`` so every existing widget
    (which is added later and thus has higher ``zorder``) renders on
    top without any z-fighting.  ``in_layout=False`` keeps the
    autolayout engine happy and prevents the backdrop from nudging the
    other axes around.
    """
    if fig is None or w <= 0 or h <= 0:
        return
    try:
        ax_bg = fig.add_axes([x, y, w, h], zorder=-10)
    except Exception:
        return
    _safe(lambda: ax_bg.set_facecolor(color or PALETTE["panel_bg"]))
    _safe(lambda: ax_bg.set_xticks([]))
    _safe(lambda: ax_bg.set_yticks([]))
    _safe(lambda: ax_bg.xaxis.set_visible(False))
    _safe(lambda: ax_bg.yaxis.set_visible(False))
    for spine_name in ("top", "right", "bottom", "left"):
        try:
            spine = ax_bg.spines[spine_name]
            spine.set_visible(False)
        except Exception:
            pass
    _safe(lambda: ax_bg.set_in_layout(False))


# ---------------------------------------------------------------------------- #
#  Widgets
# ---------------------------------------------------------------------------- #

def style_button(btn) -> None:
    """Recolour a matplotlib Button and its label without changing geometry."""
    if btn is None:
        return
    _safe(lambda: setattr(btn, "color", PALETTE["btn"]))
    _safe(lambda: setattr(btn, "hovercolor", PALETTE["btn_hover"]))
    # On modern matplotlib the button keeps a patch we can recolour too.
    try:
        patch = getattr(btn, "ax", None)
        if patch is not None:
            patch.set_facecolor(PALETTE["btn"])
            for sp in ("top", "right", "bottom", "left"):
                try:
                    s = patch.spines[sp]
                    s.set_edgecolor(PALETTE["border"])
                    s.set_linewidth(0.8)
                except Exception:
                    pass
    except Exception:
        pass
    # Label text colour + weight.
    try:
        label = getattr(btn, "label", None)
        if label is not None:
            label.set_color(PALETTE["text"])
            # Don't override pre-set fontweights; only set if default.
            if label.get_fontweight() in ("normal", 400, None):
                label.set_fontweight("medium")
    except Exception:
        pass


def style_radio(radio, ax) -> None:
    """Tint a ``RadioButtons`` widget so its labels and active dot match."""
    if radio is None:
        return
    _safe(lambda: setattr(radio, "activecolor", PALETTE["accent2"]))
    if ax is not None:
        _safe(lambda: ax.set_facecolor(PALETTE["panel_bg"]))
        for sp in ("top", "right", "bottom", "left"):
            try:
                ax.spines[sp].set_visible(False)
            except Exception:
                pass
    # Recolour the label texts.
    for lbl in (getattr(radio, "labels", None) or []):
        _safe(lambda l=lbl: l.set_color(PALETTE["text"]))


def style_textbox(tb) -> None:
    """Subtle background + border tweak for a TextBox."""
    if tb is None:
        return
    try:
        ax = getattr(tb, "ax", None)
        if ax is not None:
            ax.set_facecolor("white")
            for sp in ("top", "right", "bottom", "left"):
                try:
                    s = ax.spines[sp]
                    s.set_edgecolor(PALETTE["border"])
                    s.set_linewidth(0.8)
                except Exception:
                    pass
    except Exception:
        pass


# ---------------------------------------------------------------------------- #
#  Text / dividers
# ---------------------------------------------------------------------------- #

def style_section_header(text_artist) -> None:
    """Recolour & bold a section-header ``fig.text(...)`` artist."""
    if text_artist is None:
        return
    _safe(lambda: text_artist.set_color(PALETTE["accent"]))
    _safe(lambda: text_artist.set_fontweight("bold"))


def divider(fig, x0: float, x1: float, y: float,
            color: str | None = None, linewidth: float = 0.8) -> None:
    """Add a thin horizontal divider line in figure coords."""
    if fig is None:
        return
    try:
        from matplotlib.lines import Line2D
        line = Line2D(
            [x0, x1], [y, y],
            transform=fig.transFigure,
            color=color or PALETTE["border"],
            linewidth=linewidth, zorder=-5,
        )
        fig.add_artist(line)
    except Exception:
        pass


# ---------------------------------------------------------------------------- #
#  Plot styling — call AFTER ``ax.clear()`` (e.g. inside _do_refresh_plot)
# ---------------------------------------------------------------------------- #

def style_plot_axes(ax) -> None:
    """Apply the subtle grid + spine treatment to a data axes."""
    if ax is None:
        return
    _safe(lambda: ax.set_facecolor("white"))
    _safe(lambda: ax.grid(
        True, which="major", color=PALETTE["grid"],
        linewidth=0.5, alpha=0.45,
    ))
    for sp in ("top", "right"):
        try:
            ax.spines[sp].set_visible(False)
        except Exception:
            pass
    for sp in ("left", "bottom"):
        try:
            s = ax.spines[sp]
            s.set_color(PALETTE["spine"])
            s.set_linewidth(0.9)
        except Exception:
            pass
    # Tick label colour to match the body text.
    _safe(lambda: ax.tick_params(
        colors=PALETTE["text_dim"], labelcolor=PALETTE["text_dim"],
        which="both", length=3, width=0.7,
    ))


def style_stats_panel(ax) -> None:
    """The bottom-left Gate Statistics axes gets a faintly tinted backdrop."""
    if ax is None:
        return
    _safe(lambda: ax.set_facecolor(PALETTE["panel_bg"]))
    for sp in ("top", "right", "bottom", "left"):
        try:
            s = ax.spines[sp]
            s.set_edgecolor(PALETTE["border"])
            s.set_linewidth(0.8)
        except Exception:
            pass


def style_message_log(ax) -> None:
    """Same treatment for the right-side message log panel."""
    style_stats_panel(ax)


__all__ = [
    "PALETTE",
    "style_window", "panel_background",
    "style_button", "style_radio", "style_textbox",
    "style_section_header", "divider",
    "style_plot_axes", "style_stats_panel", "style_message_log",
]
