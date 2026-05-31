"""
app.py - Interactive matplotlib GUI for FlowCyt with redesigned UI.

UI Layout:
- Left: Main scatter plot (larger) + gate statistics
- Right: All controls (channels, tools, parent gate, buttons) + message log
"""

from __future__ import annotations

import csv
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

import numpy as np
import matplotlib
# Backend selection on macOS.
#
# Goal: have normal Magic Trackpad clicks (light or firm) register without
# needing a Force/deep click, and keep the app responsive.
#
# Why not the 'macosx' native backend?  It handles trackpad clicks
# correctly via Cocoa NSEvent, but it requires a *framework build* of
# Python (it has to own the main NSApplication thread).  Anaconda's
# default ``python`` is not a framework build, so the backend tries to
# re-exec through ``pythonw``/``python3.x`` inside ``python.app`` and
# fails with "no python 3.x installed" when that helper isn't on PATH.
#
# Why not 'TkAgg'?  Tk on macOS swallows light trackpad clicks unless
# Force Click escalates them, which is exactly the problem we're trying
# to fix.
#
# 'QtAgg' uses Qt's native macOS event handling — it sees light and firm
# clicks identically, doesn't need framework Python, and ships with most
# Anaconda installs via PyQt5.  Fall back to macosx (in case the user is
# on a framework Python) and finally to TkAgg if Qt isn't available.
if sys.platform == "darwin":
    for _candidate_backend in ("QtAgg", "macosx", "TkAgg"):
        try:
            matplotlib.use(_candidate_backend)
            break
        except Exception:
            continue
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle as RectPatch, Polygon as PolyPatch, Ellipse as EllipsePatch

# Use our macOS-friendly widget variants where a stock matplotlib widget
# would otherwise need a Force/deep click on macOS trackpads.  On
# Linux/Windows ``_widgets`` re-exports the stock classes unchanged.
from ._widgets import Button, RadioButtons, install_tk_click_bridge

from .reader import FCSData
from .gating import (
    Gate, GateManager, PolygonGate, RectangleGate, EllipseGate, QuadrantGate,
    ThresholdGate,
)
from .plotting import (
    density_scatter,
    draw_gate_overlay,
    summary_bar_chart,
    summary_histogram,
)
from .markers import (
    load_markers, load_hidden_channels, effective_channel_label,
)
from . import gate_io

# ------------------------------------------------------------------ #
#  Constants
# ------------------------------------------------------------------ #
MODE_NAV = "Navigate"
MODE_POLY = "Polygon"
MODE_RECT = "Rectangle"
MODE_ELLIPSE = "Ellipse"
MODE_QUAD = "Quadrant"
MODE_THRESH = "1D Gate"
MODE_TRANSLATE = "Translate"
MODE_ROTATE = "Rotate"
MODE_STRETCH = "Stretch"


class FlowCytApp:
    """Interactive matplotlib-based flow cytometry viewer."""

    def __init__(self, filepath: str | None = None):
        self.fcs: FCSData | None = None
        self.gate_mgr = GateManager()

        # Axis selection indices
        self._x_idx: int = 0
        self._y_idx: int = 1

        # Interaction state
        self._mode: str = MODE_NAV
        self._poly_verts: list[tuple[float, float]] = []
        self._poly_last_click_time: float = 0.0  # For double-click detection
        self._rect_origin: tuple[float, float] | None = None
        self._ellipse_origin: tuple[float, float] | None = None
        self._temp_artists: list = []

        # Handle-based gate editing state (PowerPoint/Keynote style)
        self._handle_selected_gate: Gate | None = None
        self._handle_positions: list = []
        self._handle_artists: list = []
        self._handle_drag_type: str | None = None
        self._handle_drag_start: tuple[float, float] | None = None

        # Quadrant gating state
        self._quad_midpoint: tuple[float, float] | None = None

        # Gate sub-windows {gate_uid: GateWindow}
        self._gate_windows: dict[str, "GateWindow"] = {}

        # Parent gate selection for sub-gating
        self._selected_parent_uid: str | None = None

        # Axis scale: "linear" or "log"
        self._x_scale: str = "linear"
        self._y_scale: str = "linear"

        # View mode: "2D" (scatter) or "1D" (histogram)
        self._view_mode: str = "2D"

        # 1D axis compression via click-drag
        # Anchor = data value where user clicked; frac = where it sits on
        # screen [0,1] after dragging.  None = no compression active.
        self._compress_anchor: float | None = None
        self._compress_frac: float | None = None   # display fraction [0,1]
        self._compress_dmin: float = 0.0
        self._compress_dmax: float = 1.0
        self._compress_dragging: bool = False
        self._compress_drag_px: float = 0.0     # pixel-x at drag start
        self._compress_base_frac: float = 0.5   # frac at drag start

        # Stretch mode state
        self._stretch_selected_gate: Gate | None = None
        self._stretch_points: list[tuple[float, float]] = []
        self._stretch_point_idx: int = -1
        self._stretch_point_artists: list = []
        # Mouse-drag state for stretch mode (in addition to Tab + arrows).
        self._stretch_dragging: bool = False
        self._stretch_last_xy: tuple[float, float] | None = None

        # Undo / redo stacks
        self._undo_stack: list[dict] = []
        self._redo_stack: list[dict] = []
        self._max_undo: int = 50

        # Guard against reentrant/concurrent refresh (prevents segfault)
        self._refreshing: bool = False
        self._in_do_refresh: bool = False

        # Message log
        self._messages: list[str] = []
        self._max_messages: int = 20  # Keep last 20 messages

        # Per-file fluorophore -> protein marker mapping
        self._marker_map: dict[str, str] = {}
        # Per-file set of fluorophore short names the user has hidden from
        # the channel selectors.  Purely an in-app view filter — doesn't
        # touch the FCS file.
        self._hidden_channels: set[str] = set()

        # DeepSeek chat assistant state
        self._llm_client = None                # DeepSeekClient | None (lazy)
        self._chat_window = None               # ChatWindow | None
        self._markers_window = None            # MarkersWindow | None
        self._chat_history: list[dict] = []    # OpenAI-format running history
        self._session_cost_usd: float = 0.0
        self._session_tokens_in: int = 0
        self._session_tokens_out: int = 0

        self._build_ui()

        if filepath:
            self._open_file(filepath)
        else:
            # Scan current directory for FCS files on startup
            self._scan_fcs_files(os.getcwd())
            self.ax_main.text(
                0.5, 0.5,
                "No file loaded.\nUse [<] [>] to browse FCS files\nor re-run with -i file.fcs",
                ha="center", va="center", fontsize=13,
                transform=self.ax_main.transAxes, color="grey",
            )
            self._log("Welcome to FlowCyt!")
            self._log("Use [<] [>] next to 'File' to browse FCS files")
            self._log("Features:")
            self._log("  • [<] [>] to select files and channels")
            self._log("  • Polygon: click vertices, double-click to close")
            self._log("  • Ellipse: click-drag for elliptical gates")
            self._log("  • Sub-gating: select parent before creating child")
            self._log("  • Move Gate: click to select, drag handles to edit")
            self.fig.canvas.draw_idle()

    # ================================================================ #
    #  Message logging (appears in GUI)
    # ================================================================ #
    def _log(self, message: str):
        """Add message to GUI log and console."""
        self._messages.append(message)
        # Keep only last N messages
        if len(self._messages) > self._max_messages:
            self._messages = self._messages[-self._max_messages:]
        logger.info(message)
        self._refresh_messages()

    def _refresh_messages(self):
        """Update message display in GUI."""
        self.ax_messages.clear()
        self.ax_messages.set_xticks([])
        self.ax_messages.set_yticks([])

        if self._messages:
            # Compute how many lines fit based on actual panel height.
            # ax height in figure fraction → inches → points at 6.5pt font
            bbox = self.ax_messages.get_position()
            panel_inches = bbox.height * self.fig.get_figheight()
            line_pts = 6.5 * 1.25 + 1       # font + linespacing + padding
            max_visible = max(3, int(panel_inches * 72 / line_pts) - 1)
            visible = self._messages[-max_visible:]
            text = "\n".join(visible)
            self.ax_messages.text(
                0.02, 0.98, text, family="monospace", fontsize=6.5,
                va="top", transform=self.ax_messages.transAxes,
                linespacing=1.25,
            )
        # Only schedule a draw if we're NOT inside _refresh_plot.
        # During refresh, the single draw_idle at the end covers everything.
        if not self._refreshing:
            self.fig.canvas.draw_idle()

    # ================================================================ #
    #  UI layout - NEW DESIGN
    # ================================================================ #
    def _build_ui(self):
        self.fig = plt.figure("FlowCyt", figsize=(14, 9))
        self.fig.subplots_adjust(left=0.05, right=0.98, top=0.95, bottom=0.05)

        # LEFT SIDE: Main scatter plot (bigger) + statistics
        # Plot sits higher to leave room for x-axis label + gap + stats
        self.ax_main = self.fig.add_axes([0.06, 0.35, 0.58, 0.60])

        # Stats panel below plot — clear gap from x-axis label
        self.ax_stats = self.fig.add_axes([0.06, 0.03, 0.58, 0.25])
        self.ax_stats.set_frame_on(True)
        self.ax_stats.set_xticks([])
        self.ax_stats.set_yticks([])
        self.ax_stats.set_title("Gate Statistics", fontsize=9, loc="left", fontweight="bold")

        # RIGHT SIDE: All controls
        right_start = 0.68
        ctrl_w = 0.29       # full width of right panel
        btn_h = 0.038       # standard button height (compact so all
                            # buttons + message log fit without overlap)
        btn_gap = 0.004     # vertical gap between stacked action buttons
        small_btn = 0.05    # width of [<] / [>] arrows
        label_w = ctrl_w - 2 * small_btn  # width of label between arrows

        # --- File selector: [<] filename.fcs [>]  (click label to open list) ---
        self.fig.text(right_start, 0.96, "File  (click name to list)", fontsize=9, fontweight="bold")
        self.ax_fprev = self.fig.add_axes([right_start, 0.915, small_btn, btn_h])
        self.btn_fprev = Button(self.ax_fprev, "<")
        self.btn_fprev.on_clicked(lambda e: self._cycle_file(-1))

        self.ax_flabel = self.fig.add_axes([right_start + small_btn, 0.915, label_w, btn_h])
        self.btn_flabel = Button(self.ax_flabel, "(no files found)")
        self.btn_flabel.on_clicked(lambda e: self._show_file_popup())

        self.ax_fnext = self.fig.add_axes([right_start + small_btn + label_w, 0.915, small_btn, btn_h])
        self.btn_fnext = Button(self.ax_fnext, ">")
        self.btn_fnext.on_clicked(lambda e: self._cycle_file(+1))

        # --- X Channel selector: [<] ChannelName [>] (click label to list) ---
        self.fig.text(right_start, 0.895, "X Channel", fontsize=10, fontweight="bold")
        self.ax_xprev = self.fig.add_axes([right_start, 0.855, small_btn, btn_h])
        self.btn_xprev = Button(self.ax_xprev, "<")
        self.btn_xprev.on_clicked(lambda e: self._cycle_channel("x", -1))

        self.ax_xlabel = self.fig.add_axes([right_start + small_btn, 0.855, label_w, btn_h])
        self.btn_xlabel = Button(self.ax_xlabel, "(no file)")
        self.btn_xlabel.on_clicked(lambda e: self._show_channel_popup("x"))

        self.ax_xnext = self.fig.add_axes([right_start + small_btn + label_w, 0.855, small_btn, btn_h])
        self.btn_xnext = Button(self.ax_xnext, ">")
        self.btn_xnext.on_clicked(lambda e: self._cycle_channel("x", +1))

        # --- Y Channel selector: [<] ChannelName [>] (click label to list) ---
        self.fig.text(right_start, 0.835, "Y Channel", fontsize=10, fontweight="bold")
        self.ax_yprev = self.fig.add_axes([right_start, 0.795, small_btn, btn_h])
        self.btn_yprev = Button(self.ax_yprev, "<")
        self.btn_yprev.on_clicked(lambda e: self._cycle_channel("y", -1))

        self.ax_ylabel = self.fig.add_axes([right_start + small_btn, 0.795, label_w, btn_h])
        self.btn_ylabel = Button(self.ax_ylabel, "(no file)")
        self.btn_ylabel.on_clicked(lambda e: self._show_channel_popup("y"))

        self.ax_ynext = self.fig.add_axes([right_start + small_btn + label_w, 0.795, small_btn, btn_h])
        self.btn_ynext = Button(self.ax_ynext, ">")
        self.btn_ynext.on_clicked(lambda e: self._cycle_channel("y", +1))

        # --- Axis scale toggles ---
        self.fig.text(right_start, 0.775, "Scale", fontsize=10, fontweight="bold")
        scale_btn_w = ctrl_w / 2 - 0.005
        self.ax_xscale = self.fig.add_axes([right_start, 0.735, scale_btn_w, btn_h])
        self.btn_xscale = Button(self.ax_xscale, "X: Linear")
        self.btn_xscale.on_clicked(lambda e: self._toggle_scale("x"))

        self.ax_yscale = self.fig.add_axes([right_start + scale_btn_w + 0.01, 0.735, scale_btn_w, btn_h])
        self.btn_yscale = Button(self.ax_yscale, "Y: Linear")
        self.btn_yscale.on_clicked(lambda e: self._toggle_scale("y"))

        # --- View mode toggle (2D scatter ↔ 1D histogram) ---
        y_cur = 0.69
        self.ax_viewmode = self.fig.add_axes([right_start, y_cur, ctrl_w, btn_h])
        self.btn_viewmode = Button(self.ax_viewmode, "View: 2D Scatter")
        self.btn_viewmode.on_clicked(lambda e: self._toggle_view_mode())

        # --- 1D compression hint (visible only in 1D Navigate mode) ---
        self.ax_compress_hint = self.fig.add_axes([0.06, 0.30, 0.58, 0.03])
        self.ax_compress_hint.set_xticks([])
        self.ax_compress_hint.set_yticks([])
        self.ax_compress_hint.set_frame_on(False)
        self._compress_hint_text = self.ax_compress_hint.text(
            0.5, 0.5, "Navigate mode: click & drag to compress axis  •  double-click to reset",
            ha="center", va="center", fontsize=7, color="grey",
            transform=self.ax_compress_hint.transAxes,
        )
        self.ax_compress_hint.set_visible(False)

        # --- Tool selector + Parent gate selector (side by side) ---
        radio_h = 0.195                    # height for 9 radio items
        y_cur -= 0.015                     # gap
        self.fig.text(right_start, y_cur, "Tool", fontsize=9, fontweight="bold")
        half_w = ctrl_w / 2 - 0.005
        self.fig.text(right_start + half_w + 0.01, y_cur, "Parent Gate", fontsize=9, fontweight="bold")
        y_cur -= radio_h
        self.ax_mode = self.fig.add_axes([right_start, y_cur, half_w, radio_h])
        self.radio_mode = RadioButtons(
            self.ax_mode,
            [MODE_NAV, MODE_POLY, MODE_RECT, MODE_ELLIPSE, MODE_QUAD, MODE_THRESH, MODE_TRANSLATE, MODE_ROTATE, MODE_STRETCH],
            active=0,
        )
        self.radio_mode.on_clicked(self._on_mode_change)

        self.ax_parent = self.fig.add_axes([right_start + half_w + 0.01, y_cur, half_w, radio_h])
        # Hide the empty parent-gate panel's default tick marks, labels
        # and frame so they don't visually leak over the adjacent Tool
        # radio labels before any gates are created.  ``set_xticks([])``
        # alone is reset by the autoscaler on the first draw, so we go
        # through ``xaxis.set_visible(False)`` which suppresses the axis
        # artists themselves.  ``_refresh_parent_selector`` re-creates
        # the RadioButtons on top of these axes without re-enabling the
        # spines.
        self.ax_parent.set_frame_on(False)
        self.ax_parent.xaxis.set_visible(False)
        self.ax_parent.yaxis.set_visible(False)
        self._radio_parent = None

        # --- Action buttons ---
        button_x = right_start
        y_cur -= btn_h + 0.01             # place first button below radios

        self.ax_btn_scandir = self.fig.add_axes([button_x, y_cur, ctrl_w, btn_h])
        self.btn_scandir = Button(self.ax_btn_scandir, "Scan Directory...")
        self.btn_scandir.on_clicked(lambda e: self._on_scan_directory())
        y_cur -= btn_h + btn_gap

        self.ax_btn_summary = self.fig.add_axes([button_x, y_cur, ctrl_w, btn_h])
        self.btn_summary = Button(self.ax_btn_summary, "Summary")
        self.btn_summary.on_clicked(lambda e: self._on_show_summary())
        y_cur -= btn_h + btn_gap

        self.ax_btn_export = self.fig.add_axes([button_x, y_cur, ctrl_w, btn_h])
        self.btn_export = Button(self.ax_btn_export, "Export CSV")
        self.btn_export.on_clicked(lambda e: self._on_export_csv())
        y_cur -= btn_h + btn_gap

        self.ax_btn_rename = self.fig.add_axes([button_x, y_cur, ctrl_w, btn_h])
        self.btn_rename = Button(self.ax_btn_rename, "Rename Gate...")
        self.btn_rename.on_clicked(lambda e: self._on_rename_gate())
        y_cur -= btn_h + btn_gap

        self.ax_btn_remove = self.fig.add_axes([button_x, y_cur, ctrl_w, btn_h])
        self.btn_remove = Button(self.ax_btn_remove, "Remove Gate...")
        self.btn_remove.on_clicked(lambda e: self._on_remove_gate())
        y_cur -= btn_h + btn_gap

        self.ax_btn_clear = self.fig.add_axes([button_x, y_cur, ctrl_w, btn_h])
        self.btn_clear = Button(self.ax_btn_clear, "Clear All Gates")
        self.btn_clear.on_clicked(lambda e: self._on_clear_gates())
        y_cur -= btn_h + btn_gap

        self.ax_btn_save = self.fig.add_axes([button_x, y_cur, ctrl_w, btn_h])
        self.btn_save = Button(self.ax_btn_save, "Save Plot...")
        self.btn_save.on_clicked(lambda e: self._on_save_plot())
        y_cur -= btn_h + btn_gap

        # --- Save Gates / Load Gates buttons (side-by-side) ---
        half_w_pair = ctrl_w / 2 - 0.005
        self.ax_btn_save_gates = self.fig.add_axes([button_x, y_cur, half_w_pair, btn_h])
        self.btn_save_gates = Button(self.ax_btn_save_gates, "Save Gates")
        self.btn_save_gates.on_clicked(lambda e: self._on_save_gates())

        self.ax_btn_load_gates = self.fig.add_axes(
            [button_x + half_w_pair + 0.01, y_cur, half_w_pair, btn_h]
        )
        self.btn_load_gates = Button(self.ax_btn_load_gates, "Load Gates")
        self.btn_load_gates.on_clicked(lambda e: self._on_load_gates())
        y_cur -= btn_h + btn_gap

        # --- Chat + Markers buttons (side-by-side) ---
        self.ax_btn_chat = self.fig.add_axes([button_x, y_cur, half_w_pair, btn_h])
        self.btn_chat = Button(self.ax_btn_chat, "Chat")
        self.btn_chat.on_clicked(lambda e: self._on_open_chat())

        self.ax_btn_markers = self.fig.add_axes(
            [button_x + half_w_pair + 0.01, y_cur, half_w_pair, btn_h]
        )
        self.btn_markers = Button(self.ax_btn_markers, "Markers")
        self.btn_markers.on_clicked(lambda e: self._on_open_markers())
        y_cur -= 0.013   # spacing before Message Log

        # --- Message log panel (bottom right) ---
        self.fig.text(right_start, y_cur, "Message Log", fontsize=9, fontweight="bold")
        y_cur -= 0.008   # gap between the "Message Log" label and its panel
        # Honest height — do NOT clamp to a minimum, otherwise the panel
        # extends above its label and overlaps the Chat/Markers buttons.
        msg_bottom = 0.03
        msg_h = max(y_cur - msg_bottom, 0.04)
        self.ax_messages = self.fig.add_axes([right_start, msg_bottom, ctrl_w, msg_h])
        self.ax_messages.set_frame_on(True)
        self.ax_messages.set_xticks([])
        self.ax_messages.set_yticks([])

        # Connect canvas events
        self.fig.canvas.mpl_connect("button_press_event", self._on_click)
        self.fig.canvas.mpl_connect("button_release_event", self._on_release)
        self.fig.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.fig.canvas.mpl_connect("key_press_event", self._on_key)

        # Prevent Tk from swallowing Tab for widget-focus traversal so that
        # our key_press_event handler can use it (Stretch mode).
        self._disable_tk_tab_traversal(self.fig)

        # macOS Force-Touch workaround: bind Tk's button events directly so
        # a normal-pressure click registers without needing a deep press.
        install_tk_click_bridge(self.fig)

        # Channel names cache (populated on file load)
        self._channel_display_names: list[str] = []

        # File selector state
        self._fcs_files: list[str] = []   # List of absolute paths to FCS files
        self._fcs_file_idx: int = -1       # Index of currently loaded file
        self._scan_dir: str | None = None  # Directory being scanned

    # ================================================================ #
    #  File loading & file selector
    # ================================================================ #
    def _scan_fcs_files(self, directory: str):
        """Scan a directory (and subdirectories one level deep) for .fcs files."""
        import glob
        directory = os.path.abspath(directory)
        # Scan current dir + one level of subdirs
        patterns = [
            os.path.join(directory, "*.fcs"),
            os.path.join(directory, "*.FCS"),
            os.path.join(directory, "*", "*.fcs"),
            os.path.join(directory, "*", "*.FCS"),
        ]
        found = set()
        for pat in patterns:
            found.update(glob.glob(pat))
        self._fcs_files = sorted(found)
        self._scan_dir = directory
        self._update_file_label()
        if self._fcs_files:
            self._log(f"Found {len(self._fcs_files)} FCS file(s) in {directory}")
        else:
            self._log(f"No FCS files found in {directory}")

    def _update_file_label(self):
        """Update the file selector button label."""
        if not self._fcs_files:
            self.btn_flabel.label.set_text("(no files found)")
            return
        if self._fcs_file_idx < 0 or self._fcs_file_idx >= len(self._fcs_files):
            self.btn_flabel.label.set_text("(click to list)")
            return
        fname = os.path.basename(self._fcs_files[self._fcs_file_idx])
        # Truncate long filenames
        if len(fname) > 22:
            fname = fname[:19] + "..."
        idx = self._fcs_file_idx + 1
        total = len(self._fcs_files)
        self.btn_flabel.label.set_text(f"{fname} [{idx}/{total}]")
        self.btn_flabel.label.set_fontsize(7)

    def _cycle_file(self, direction: int):
        """Cycle to next/previous FCS file and load it."""
        if not self._fcs_files:
            # No files scanned yet — try scanning current working dir
            cwd = os.getcwd()
            self._scan_fcs_files(cwd)
            if not self._fcs_files:
                self._log("No FCS files found. Place .fcs files in the working directory.")
                return

        n = len(self._fcs_files)
        if self._fcs_file_idx < 0:
            # First selection
            self._fcs_file_idx = 0 if direction >= 0 else n - 1
        else:
            self._fcs_file_idx = (self._fcs_file_idx + direction) % n

        path = self._fcs_files[self._fcs_file_idx]
        self._update_file_label()
        self._open_file(path)

    def _open_file(self, path: str):
        """Load an FCS file and update all UI elements.

        Gates, parent selection, and X/Y channels are all preserved.
        """
        # Save current channel names BEFORE loading new file
        old_channel_names: list[str] = []
        if self.fcs is not None:
            old_channel_names = list(self.fcs.channel_names)

        # Remember which raw channel names were selected
        old_x_name = old_channel_names[self._x_idx] if self._x_idx < len(old_channel_names) else None
        old_y_name = old_channel_names[self._y_idx] if self._y_idx < len(old_channel_names) else None

        try:
            self.fcs = FCSData(path)
        except Exception as exc:
            self._log(f"Error loading {path}: {exc}")
            return

        # Load per-file marker map (FCS PnS defaults merged with sidecar overrides)
        try:
            self._marker_map = load_markers(path, self.fcs)
        except Exception as exc:
            logger.warning("Failed to load marker map for %s: %s", path, exc)
            self._marker_map = {}
        # Load per-file hidden-channel set (purely a view filter).
        try:
            self._hidden_channels = load_hidden_channels(path)
        except Exception as exc:
            logger.warning("Failed to load hidden channels for %s: %s", path, exc)
            self._hidden_channels = set()

        num_gates = len(self.gate_mgr.gates)
        self._log(f"Loaded: {os.path.basename(path)}")
        self._log(f"Events: {self.fcs.num_events:,}, Channels: {self.fcs.num_channels}")
        if num_gates > 0:
            self._log(f"Keeping {num_gates} existing gate(s)")

        # Scan directory for other FCS files (if not already scanned for this dir)
        file_dir = os.path.dirname(os.path.abspath(path))
        if file_dir != self._scan_dir:
            self._scan_fcs_files(file_dir)

        # Set the file index to match the loaded file
        abs_path = os.path.abspath(path)
        if abs_path in self._fcs_files:
            self._fcs_file_idx = self._fcs_files.index(abs_path)
        self._update_file_label()

        # Rebuild channel display names using the merged marker map so
        # user overrides show up next to the fluorophore short name.
        self._channel_display_names = [
            effective_channel_label(short, self._marker_map)
            for short in self.fcs.channel_names
        ]
        new_names = self.fcs.channel_names

        # Restore X/Y channel by matching raw name (e.g. "FSC-A")
        if old_x_name is not None and old_x_name in new_names:
            self._x_idx = new_names.index(old_x_name)
        else:
            self._x_idx = 0
        if old_y_name is not None and old_y_name in new_names:
            self._y_idx = new_names.index(old_y_name)
        else:
            self._y_idx = min(1, len(new_names) - 1)

        self._log(f"X: {self._channel_display_names[self._x_idx]}, Y: {self._channel_display_names[self._y_idx]}")
        self._update_channel_labels()
        self._refresh_plot()

        # Refresh all open gate sub-windows so they show data from the new file
        self._refresh_gate_windows()

        # Refresh markers window if open (so it shows the new file's channels)
        if self._markers_window is not None:
            try:
                self._markers_window.refresh()
            except Exception:
                logger.exception("Failed to refresh markers window")

    def _on_scan_directory(self):
        """Scan parent directory (go up one level) for FCS files."""
        if self._scan_dir:
            scan_path = os.path.dirname(self._scan_dir)
        else:
            scan_path = os.getcwd()
        self._log(f"Scanning: {scan_path}")
        self._fcs_file_idx = -1
        self._scan_fcs_files(scan_path)
        self.fig.canvas.draw_idle()

    # ================================================================ #
    #  Popup list (pure matplotlib — no tkinter needed)
    # ================================================================ #
    def _show_popup_list(self, title: str, items: list[str],
                         current_idx: int, callback):
        """Open a matplotlib figure window showing a clickable list.

        When the user clicks an item the window closes and
        *callback(index)* is called.
        """
        if not items:
            self._log("Nothing to show.")
            return

        n = len(items)
        # Figure height scales with number of items (min 3, max 20 visible)
        visible = min(n, 25)
        fig_h = max(3, visible * 0.35 + 1.0)
        popup_fig = plt.figure(title, figsize=(6, fig_h))
        popup_fig.clf()
        ax = popup_fig.add_axes([0.05, 0.05, 0.9, 0.9])
        ax.set_xlim(0, 1)
        ax.set_ylim(-n, 1)
        ax.set_xticks([])
        ax.set_yticks([])
        ax.set_title(f"{title}  (click to select)", fontsize=11, fontweight="bold")

        # Draw each item as a text row
        text_artists = []
        for i, item in enumerate(items):
            y_pos = -i
            weight = "bold" if i == current_idx else "normal"
            bg = "#cce5ff" if i == current_idx else ("white" if i % 2 == 0 else "#f0f0f0")
            # Background rectangle
            ax.axhspan(y_pos - 0.5, y_pos + 0.5, facecolor=bg, edgecolor="#cccccc", linewidth=0.5)
            prefix = ">> " if i == current_idx else "   "
            t = ax.text(0.03, y_pos, f"{prefix}{item}",
                        fontsize=9, family="monospace", va="center",
                        fontweight=weight, clip_on=True)
            text_artists.append(t)

        def on_click(event):
            if event.inaxes != ax:
                return
            row = -int(round(event.ydata))
            if 0 <= row < n:
                plt.close(popup_fig)
                callback(row)

        popup_fig.canvas.mpl_connect("button_press_event", on_click)
        popup_fig.canvas.draw()
        popup_fig.show()

    def _show_text_input(self, title: str, prompt: str,
                         initial: str, callback):
        """Open a matplotlib popup with a text field + OK / Cancel buttons.

        OK and Cancel sit *below* the TextBox rather than beside it, so
        a long typed value (which matplotlib renders overflowing past
        the right edge of the box) can never visually cover the buttons
        and lock the user out.  Enter inside the TextBox also submits.
        """
        from ._widgets import TextBox
        popup_fig = plt.figure(title, figsize=(8.0, 2.4))
        popup_fig.clf()
        popup_fig.text(0.05, 0.85, prompt, fontsize=10, fontweight="bold")

        # Wide TextBox spanning ~90% of the popup so most inputs fit
        # without overflowing in the first place.
        ax_text = popup_fig.add_axes([0.05, 0.50, 0.90, 0.22])
        tbox = TextBox(ax_text, "", initial=initial)
        try:
            tbox.text_disp.set_fontsize(10)
            # Clip text rendering to the box so an overflowing value can't
            # paint over surrounding axes.
            tbox.text_disp.set_clip_on(True)
        except Exception:
            pass

        # OK / Cancel BELOW the textbox — overflow above cannot cover them.
        ax_ok = popup_fig.add_axes([0.28, 0.10, 0.20, 0.22])
        btn_ok = Button(ax_ok, "OK")
        ax_cancel = popup_fig.add_axes([0.52, 0.10, 0.20, 0.22])
        btn_cancel = Button(ax_cancel, "Cancel")

        def _submit(text=None):
            try:
                val = (tbox.text or "").strip()
            except Exception:
                val = ""
            if not val:
                return
            plt.close(popup_fig)
            callback(val)

        def _cancel(_e=None):
            try:
                plt.close(popup_fig)
            except Exception:
                pass

        btn_ok.on_clicked(lambda e: _submit())
        btn_cancel.on_clicked(_cancel)
        tbox.on_submit(_submit)
        popup_fig.canvas.draw()
        popup_fig.show()

    def _on_rename_gate(self):
        """Show popup to select a gate, then a text input to rename it."""
        if not self.gate_mgr.gates:
            self._log("No gates to rename")
            return

        items = [f"{g.name}  ({g.x_channel} vs {g.y_channel})"
                 for g in self.gate_mgr.gates]

        def on_pick(idx):
            gate = self.gate_mgr.gates[idx]
            old_name = gate.name

            def on_rename(new_name):
                gate.name = new_name
                self._refresh_plot()
                self._refresh_gate_windows()
                self._log(f"Renamed '{old_name}' → '{new_name}'")

            self._show_text_input(
                "Rename Gate", f"Rename '{old_name}' to:",
                old_name, on_rename,
            )

        self._show_popup_list("Select gate to rename", items, -1, on_pick)

    def _show_file_popup(self):
        """Show a popup list of all discovered FCS files."""
        if not self._fcs_files:
            self._scan_fcs_files(os.getcwd())
            if not self._fcs_files:
                self._log("No FCS files found. Use 'Scan Directory...' button.")
                return

        display_names = [os.path.basename(f) for f in self._fcs_files]

        def on_pick(idx):
            self._fcs_file_idx = idx
            self._update_file_label()
            self._open_file(self._fcs_files[idx])
            self.fig.canvas.draw_idle()

        self._show_popup_list(
            "Select FCS File", display_names, self._fcs_file_idx, on_pick,
        )

    def _show_channel_popup(self, axis: str):
        """Show a popup list of all channels for X or Y axis.

        Channels the user has hidden via the Markers window are filtered
        out — but the currently-selected channel is always listed (with
        a ``"(hidden)"`` annotation) so the user can still navigate away
        from it.
        """
        if not self._channel_display_names:
            self._log("No file loaded")
            return

        current = self._x_idx if axis == "x" else self._y_idx
        hidden = self._hidden_channels or set()
        names = self.fcs.channel_names if self.fcs is not None else []

        # Build the filtered (display_name, real_index) list.
        visible_pairs: list[tuple[str, int]] = []
        for i, short in enumerate(names):
            if short in hidden and i != current:
                continue
            label_str = self._channel_display_names[i]
            if short in hidden and i == current:
                label_str = f"{label_str}  (hidden)"
            visible_pairs.append((label_str, i))

        visible_labels = [lbl for lbl, _ in visible_pairs]
        visible_indices = [idx for _, idx in visible_pairs]
        try:
            sel_pos = visible_indices.index(current)
        except ValueError:
            sel_pos = -1

        def on_pick(visible_pos):
            real_idx = visible_indices[visible_pos]
            if axis == "x":
                self._x_idx = real_idx
                self._log(f"X: {self._channel_display_names[real_idx]}")
            else:
                self._y_idx = real_idx
                self._log(f"Y: {self._channel_display_names[real_idx]}")
            self._update_channel_labels()
            self._refresh_plot()

        label = "X" if axis == "x" else "Y"
        self._show_popup_list(
            f"Select {label} Channel",
            visible_labels,
            sel_pos,
            on_pick,
        )

    # ================================================================ #
    #  Channel / mode callbacks
    # ================================================================ #
    def _update_channel_labels(self):
        """Update the X/Y channel button labels."""
        if not self._channel_display_names:
            self.btn_xlabel.label.set_text("(no file)")
            self.btn_ylabel.label.set_text("(no file)")
            return
        x_name = self._channel_display_names[self._x_idx]
        y_name = self._channel_display_names[self._y_idx]
        # Truncate long names — slightly more room for "SHORT (marker)" labels.
        self.btn_xlabel.label.set_text(x_name[:32])
        self.btn_xlabel.label.set_fontsize(7)
        self.btn_ylabel.label.set_text(y_name[:32])
        self.btn_ylabel.label.set_fontsize(7)

    def _toggle_scale(self, axis: str):
        """Toggle axis scale between linear and log."""
        self._refreshing = True
        if axis == "x":
            self._x_scale = "log" if self._x_scale == "linear" else "linear"
            label = f"X: {self._x_scale.capitalize()}"
            self.btn_xscale.label.set_text(label)
            self._log(f"X scale: {self._x_scale}")
        else:
            self._y_scale = "log" if self._y_scale == "linear" else "linear"
            label = f"Y: {self._y_scale.capitalize()}"
            self.btn_yscale.label.set_text(label)
            self._log(f"Y scale: {self._y_scale}")
        # Keep _refreshing=True → call worker directly
        self._do_refresh_plot()

    def _toggle_view_mode(self):
        """Switch between 2D scatter and 1D histogram views."""
        self._refreshing = True
        if self._view_mode == "2D":
            self._view_mode = "1D"
            self.btn_viewmode.label.set_text("View: 1D Histogram")
            self._update_compress_hint()
            self._log("Switched to 1D histogram view")
            self._log("  In Navigate mode: click & drag to compress axis")
        else:
            self._view_mode = "2D"
            self.btn_viewmode.label.set_text("View: 2D Scatter")
            self.ax_compress_hint.set_visible(False)
            self._reset_compression()
            self._log("Switched to 2D scatter view")
        self._do_refresh_plot()

    def _update_compress_hint(self):
        """Show/hide the compression hint based on mode."""
        show = (self._view_mode == "1D" and self._mode == MODE_NAV)
        self.ax_compress_hint.set_visible(show)

    def _reset_compression(self):
        """Clear axis compression state."""
        self._compress_anchor = None
        self._compress_frac = None
        self._compress_dragging = False

    # ── Piecewise linear axis compression ──

    @staticmethod
    def _pw_transform(x: np.ndarray, dmin: float, dmax: float,
                      anchor: float, frac: float) -> np.ndarray:
        """Piecewise linear transform mapping [dmin, dmax] → [0, 1].

        *anchor* is a data value; *frac* is where anchor appears on screen
        (0..1).  Data left of anchor maps to [0, frac], right to [frac, 1].
        """
        result = np.empty_like(x, dtype=np.float64)
        left = x <= anchor
        a_left = anchor - dmin
        a_right = dmax - anchor
        if a_left > 0:
            result[left] = (x[left] - dmin) / a_left * frac
        else:
            result[left] = 0.0
        if a_right > 0:
            result[~left] = frac + (x[~left] - anchor) / a_right * (1.0 - frac)
        else:
            result[~left] = 1.0
        return result

    @staticmethod
    def _pw_inverse(t: float, dmin: float, dmax: float,
                    anchor: float, frac: float) -> float:
        """Inverse of _pw_transform: screen fraction → data value."""
        if frac > 0 and t <= frac:
            return dmin + t / frac * (anchor - dmin)
        elif (1.0 - frac) > 0:
            return anchor + (t - frac) / (1.0 - frac) * (dmax - anchor)
        return anchor

    @staticmethod
    def _set_pw_ticks(ax, dmin: float, dmax: float, anchor: float,
                      frac: float, num_ticks: int = 10):
        """Set tick labels for a piecewise-transformed axis.

        Picks nice values in data space, transforms to [0,1] for positions,
        shows original values as labels.
        """
        from matplotlib.ticker import FixedLocator, FixedFormatter

        data_range = dmax - dmin
        if data_range <= 0:
            return

        raw_ticks = np.linspace(dmin, dmax, num_ticks + 2)
        magnitude = 10 ** int(np.log10(max(abs(dmax), abs(dmin), 1)))
        if magnitude >= 10:
            raw_ticks = np.round(raw_ticks / (magnitude / 10)) * (magnitude / 10)
        nice_ticks = np.unique(raw_ticks)

        tick_positions = FlowCytApp._pw_transform(
            nice_ticks, dmin, dmax, anchor, frac
        )

        def _fmt(v):
            av = abs(v)
            if av >= 1e6:
                return f"{v:.0e}"
            elif av >= 100:
                return f"{v:.0f}"
            elif av >= 1:
                return f"{v:.1f}"
            elif av >= 0.01:
                return f"{v:.2f}"
            else:
                return f"{v:.1e}"

        tick_labels = [_fmt(v) for v in nice_ticks]
        ax.xaxis.set_major_locator(FixedLocator(tick_positions))
        ax.xaxis.set_major_formatter(FixedFormatter(tick_labels))
        for lbl in ax.get_xticklabels():
            lbl.set_rotation(30)
            lbl.set_fontsize(7)

    def _cycle_channel(self, axis: str, direction: int):
        """Cycle X or Y channel by direction (+1 or -1)."""
        if self.fcs is None:
            self._log("No file loaded")
            return
        n = len(self._channel_display_names)
        if n == 0:
            return
        # Build the list of visible channel indices, skipping any the
        # user has hidden via the Markers window.  We always include
        # the currently-selected one so the user can navigate off it.
        hidden = self._hidden_channels or set()
        names = self.fcs.channel_names
        cur = self._x_idx if axis == "x" else self._y_idx
        visible = [i for i, s in enumerate(names) if s not in hidden]
        if cur not in visible:
            visible.append(cur)
            visible.sort()
        if not visible:
            return
        try:
            pos = visible.index(cur)
        except ValueError:
            pos = 0
        new_idx = visible[(pos + direction) % len(visible)]
        # Set _refreshing BEFORE logging so that _refresh_messages
        # does NOT schedule a draw_idle that could race with the
        # upcoming _refresh_plot (which clears and rebuilds axes).
        self._refreshing = True
        if axis == "x":
            self._x_idx = new_idx
            self._log(f"X: {self._channel_display_names[new_idx]}")
            self._reset_compression()  # compression is channel-specific
        else:
            self._y_idx = new_idx
            self._log(f"Y: {self._channel_display_names[new_idx]}")
        self._update_channel_labels()
        self._do_refresh_plot()

    def _on_mode_change(self, label):
        self._mode = label
        self._poly_verts.clear()
        self._rect_origin = None
        self._ellipse_origin = None
        self._handle_selected_gate = None
        self._handle_drag_type = None
        self._handle_drag_start = None
        self._quad_midpoint = None
        self._stretch_selected_gate = None
        self._stretch_points = []
        self._stretch_point_idx = -1
        self._clear_stretch_highlight()
        self._clear_handles()
        self._clear_temp()
        self._update_compress_hint()
        modes = {
            MODE_NAV: "Navigate mode - pan/zoom",
            MODE_POLY: "Polygon - click vertices, double-click to close",
            MODE_RECT: "Rectangle - click and drag",
            MODE_ELLIPSE: "Ellipse - click center, drag radius",
            MODE_QUAD: "Quadrant - click to place crosshair, then pick quadrant",
            MODE_THRESH: "1D Gate - switch to 1D view, click to set threshold",
            MODE_TRANSLATE: "Translate - select gate, arrows move in all 4 directions",
            MODE_ROTATE: "Rotate - select gate, left/right arrows rotate",
            MODE_STRETCH: "Stretch - select gate, Tab cycles points, arrows move point",
        }
        self._log(modes.get(label, label))

        # When entering Translate or Rotate mode, show gate picker dropdown
        if label == MODE_TRANSLATE:
            self._show_gate_picker("Translate")
        elif label == MODE_ROTATE:
            self._show_gate_picker("Rotate")
        elif label == MODE_STRETCH:
            self._show_stretch_picker()

    def _get_pw_params(self, x: np.ndarray):
        """Return (dmin, dmax, anchor, frac) if compression is active, else None."""
        if self._compress_anchor is None or self._compress_frac is None:
            return None
        return (self._compress_dmin, self._compress_dmax,
                self._compress_anchor, self._compress_frac)

    def _show_gate_picker(self, action: str = "Translate"):
        """Show a popup to select which gate to translate/rotate."""
        if not self.gate_mgr.gates:
            self._log("No gates defined — create a gate first")
            return
        _, _, xn, yn = self._current_xy()
        # Show ALL gates, but highlight ones on current view
        items = []
        for g in self.gate_mgr.gates:
            on_view = (g.x_channel == xn and g.y_channel == yn)
            marker = "●" if on_view else " "
            items.append(f"{marker} {g.name}  ({g.x_channel} / {g.y_channel})")

        def on_pick(idx):
            gate = self.gate_mgr.gates[idx]
            self._handle_selected_gate = gate
            self._clear_handles()
            self._draw_handles(gate)
            if action == "Translate":
                self._log(f"Selected '{gate.name}' — arrows translate in all directions")
            else:
                self._log(f"Selected '{gate.name}' — left/right arrows rotate")
            self.fig.canvas.draw_idle()

        self._show_popup_list(f"Select Gate to {action}", items, -1, on_pick)

    # ── Stretch mode helpers ──

    def _show_stretch_picker(self):
        """Show a popup to select which gate to stretch with Tab/arrows."""
        if not self.gate_mgr.gates:
            self._log("No gates defined — create a gate first")
            return
        _, _, xn, yn = self._current_xy()
        items = []
        for g in self.gate_mgr.gates:
            if isinstance(g, ThresholdGate):
                continue  # ThresholdGate has no stretch points
            on_view = (g.x_channel == xn and g.y_channel == yn)
            marker = "●" if on_view else " "
            items.append(f"{marker} {g.name}  ({g.x_channel} / {g.y_channel})")

        stretchable = [g for g in self.gate_mgr.gates
                       if not isinstance(g, ThresholdGate)]
        if not stretchable:
            self._log("No stretchable gates (ThresholdGates cannot be stretched)")
            return

        def on_pick(idx):
            gate = stretchable[idx]
            self._stretch_selected_gate = gate
            self._stretch_points = self._get_stretch_points(gate)
            self._stretch_point_idx = 0 if self._stretch_points else -1
            self._draw_stretch_highlight()
            n_pts = len(self._stretch_points)
            self._log(f"Stretch '{gate.name}' — {n_pts} control points. "
                      f"Tab cycles points, arrows move selected point")
            self._grab_canvas_focus(self.fig)
            self.fig.canvas.draw_idle()

        self._show_popup_list("Select Gate to Stretch", items, -1, on_pick)

    @staticmethod
    def _get_stretch_points(gate: Gate) -> list[tuple[float, float]]:
        """Return the control/vertex points that can be moved for stretching."""
        if isinstance(gate, PolygonGate):
            return list(gate.vertices)
        elif isinstance(gate, RectangleGate):
            return [
                (gate.x_min, gate.y_min), (gate.x_max, gate.y_min),
                (gate.x_max, gate.y_max), (gate.x_min, gate.y_max),
            ]
        elif isinstance(gate, EllipseGate):
            # Return the 4 axis endpoints (semi-axis tips in rotated frame)
            cos_a = np.cos(gate.angle)
            sin_a = np.sin(gate.angle)
            return [
                (gate.center_x + gate.semi_x * cos_a,
                 gate.center_y + gate.semi_x * sin_a),   # +X axis
                (gate.center_x - gate.semi_x * cos_a,
                 gate.center_y - gate.semi_x * sin_a),   # -X axis
                (gate.center_x - gate.semi_y * sin_a,
                 gate.center_y + gate.semi_y * cos_a),   # +Y axis
                (gate.center_x + gate.semi_y * sin_a,
                 gate.center_y - gate.semi_y * cos_a),   # -Y axis
            ]
        elif isinstance(gate, QuadrantGate):
            return [(gate.mid_x, gate.mid_y)]
        return []

    def _apply_stretch_point(self, gate: Gate, idx: int, dx: float, dy: float):
        """Move a single control point of a gate."""
        if isinstance(gate, PolygonGate):
            vx, vy = gate.vertices[idx]
            gate.vertices[idx] = (vx + dx, vy + dy)
        elif isinstance(gate, RectangleGate):
            # 0=BL, 1=BR, 2=TR, 3=TL
            if idx == 0:
                gate.x_min += dx; gate.y_min += dy
            elif idx == 1:
                gate.x_max += dx; gate.y_min += dy
            elif idx == 2:
                gate.x_max += dx; gate.y_max += dy
            elif idx == 3:
                gate.x_min += dx; gate.y_max += dy
        elif isinstance(gate, EllipseGate):
            # Adjust semi-axis length based on which axis endpoint moved
            cos_a = np.cos(gate.angle)
            sin_a = np.sin(gate.angle)
            # Project dx,dy onto the axis direction
            if idx in (0, 1):
                # X-axis endpoints: project onto axis direction
                proj = dx * cos_a + dy * sin_a
                sign = 1 if idx == 0 else -1
                gate.semi_x = max(0.01, gate.semi_x + sign * proj)
            else:
                # Y-axis endpoints: project onto perpendicular direction
                proj = -dx * sin_a + dy * cos_a
                sign = 1 if idx == 2 else -1
                gate.semi_y = max(0.01, gate.semi_y + sign * proj)
        elif isinstance(gate, QuadrantGate):
            gate.mid_x += dx; gate.mid_y += dy
        # Update the cached stretch points
        self._stretch_points = self._get_stretch_points(gate)

    def _draw_stretch_highlight(self):
        """Draw a highlighted marker on the currently selected stretch point."""
        self._clear_stretch_highlight()
        if (self._stretch_selected_gate is None or
                self._stretch_point_idx < 0 or
                self._stretch_point_idx >= len(self._stretch_points)):
            return
        # Draw all points as small dots, selected point as large ring
        for i, (px, py) in enumerate(self._stretch_points):
            if i == self._stretch_point_idx:
                marker = self.ax_main.plot(
                    px, py, "o", color="#ff4400", markersize=12,
                    markeredgecolor="black", markeredgewidth=2,
                    markerfacecolor="none", zorder=101,
                )[0]
            else:
                marker = self.ax_main.plot(
                    px, py, "o", color="#888888", markersize=6,
                    markeredgecolor="black", markeredgewidth=1,
                    zorder=100,
                )[0]
            self._stretch_point_artists.append(marker)

    def _clear_stretch_highlight(self):
        for a in getattr(self, '_stretch_point_artists', []):
            try:
                a.remove()
            except Exception:
                pass
        self._stretch_point_artists = []

    # ── Mouse interaction for stretch mode ──
    # (Tab + arrows still work; the mouse path is an additional convenience.)

    _STRETCH_PICK_PIXELS = 18

    def _stretch_click(self, event):
        """Pick the nearest stretch point under the cursor and start a drag.

        If no gate is selected yet, fall back to showing the gate picker —
        matches the keyboard flow that begins by choosing a gate.
        """
        if event.button != 1:
            return
        if event.xdata is None or event.ydata is None:
            return
        if self._stretch_selected_gate is None:
            self._show_stretch_picker()
            return
        if not self._stretch_points:
            return

        # Pixel-space hit test so the threshold is independent of axis units
        # (linear vs log vs vastly different ranges on x / y).
        trans = self.ax_main.transData.transform
        try:
            cx, cy = trans((event.xdata, event.ydata))
        except Exception:
            return
        best_idx = -1
        best_dist = float("inf")
        for i, (px, py) in enumerate(self._stretch_points):
            try:
                pcx, pcy = trans((px, py))
            except Exception:
                continue
            d = ((cx - pcx) ** 2 + (cy - pcy) ** 2) ** 0.5
            if d < best_dist:
                best_dist = d
                best_idx = i
        if best_idx < 0 or best_dist > self._STRETCH_PICK_PIXELS:
            self._log(
                "Stretch: click closer to a control point "
                f"(need within {self._STRETCH_PICK_PIXELS} px)."
            )
            return

        # Snap selection to this point, then arm the drag.
        self._stretch_point_idx = best_idx
        self._clear_stretch_highlight()
        self._draw_stretch_highlight()
        self._stretch_dragging = True
        self._stretch_last_xy = (event.xdata, event.ydata)
        self._push_undo()
        self._log(
            f"Stretch: dragging point {best_idx + 1}/{len(self._stretch_points)}"
        )
        self.fig.canvas.draw_idle()

    def _stretch_drag_motion(self, event):
        """Apply the per-frame delta to the selected stretch point."""
        if (not self._stretch_dragging
                or self._stretch_selected_gate is None
                or self._stretch_point_idx < 0
                or self._stretch_last_xy is None):
            return
        if event.xdata is None or event.ydata is None:
            return
        last_x, last_y = self._stretch_last_xy
        dx = event.xdata - last_x
        dy = event.ydata - last_y
        if dx == 0 and dy == 0:
            return
        gate = self._stretch_selected_gate
        idx = self._stretch_point_idx
        self._apply_stretch_point(gate, idx, dx, dy)
        self._stretch_last_xy = (event.xdata, event.ydata)
        self._clear_stretch_highlight()
        self._refresh_plot()
        # _refresh_plot regenerates the artist set; re-attach our selection
        # so the highlighted marker keeps tracking the dragged point.
        self._stretch_selected_gate = gate
        self._draw_stretch_highlight()
        self.fig.canvas.draw_idle()

    def _stretch_drag_end(self, _event):
        if not self._stretch_dragging:
            return
        gate = self._stretch_selected_gate
        self._stretch_dragging = False
        self._stretch_last_xy = None
        if gate is not None:
            self._log(f"Stretch: finished editing '{gate.name}'")

    # ── Undo / redo helpers ──

    def _snapshot_gates(self) -> dict:
        """Capture current gate state + open gate windows for undo."""
        import copy
        gate_copies = []
        for g in self.gate_mgr.gates:
            gate_copies.append(copy.deepcopy(g))
        open_windows = list(self._gate_windows.keys())
        return {"gates": gate_copies, "open_windows": open_windows}

    def _push_undo(self):
        """Save current state to undo stack (call BEFORE making changes)."""
        snapshot = self._snapshot_gates()
        self._undo_stack.append(snapshot)
        if len(self._undo_stack) > self._max_undo:
            self._undo_stack = self._undo_stack[-self._max_undo:]
        # Any new action clears redo
        self._redo_stack.clear()

    def _undo(self):
        if not self._undo_stack:
            self._log("Nothing to undo")
            return
        # Save current state to redo stack
        self._redo_stack.append(self._snapshot_gates())
        snapshot = self._undo_stack.pop()
        self._restore_snapshot(snapshot)
        self._log("Undo")

    def _redo(self):
        if not self._redo_stack:
            self._log("Nothing to redo")
            return
        self._undo_stack.append(self._snapshot_gates())
        snapshot = self._redo_stack.pop()
        self._restore_snapshot(snapshot)
        self._log("Redo")

    def _restore_snapshot(self, snapshot: dict):
        """Restore gate state from a snapshot."""
        self.gate_mgr.gates = snapshot["gates"]
        # Reopen any windows that were open in the snapshot but are now closed
        prev_windows = set(snapshot.get("open_windows", []))
        current_windows = set(self._gate_windows.keys())
        # Close windows that weren't open in the snapshot
        for uid in current_windows - prev_windows:
            gw = self._gate_windows.get(uid)
            if gw:
                try:
                    plt.close(gw.fig)
                except Exception:
                    pass
                del self._gate_windows[uid]
        # Reopen windows that were open in snapshot but currently closed
        for uid in prev_windows - current_windows:
            gate = next((g for g in self.gate_mgr.gates if g.uid == uid), None)
            if gate:
                self._open_gate_window(gate)
        # Clear any selection state
        self._handle_selected_gate = None
        self._stretch_selected_gate = None
        self._stretch_points = []
        self._stretch_point_idx = -1
        self._clear_handles()
        self._clear_stretch_highlight()
        self._clear_temp()
        self._refresh_plot()
        # Refresh any open gate windows
        for uid, gw in self._gate_windows.items():
            try:
                gw._refresh()
            except Exception:
                pass

    # ================================================================ #
    #  Plotting
    # ================================================================ #
    def _current_xy(self):
        xi, yi = self._x_idx, self._y_idx
        n = len(self.fcs.channel_names)
        # Clamp indices to valid range (prevents crash on channel switch)
        xi = max(0, min(xi, n - 1))
        yi = max(0, min(yi, n - 1))
        self._x_idx = xi
        self._y_idx = yi
        return (
            xi, yi,
            self.fcs.channel_names[xi],
            self.fcs.channel_names[yi],
        )

    @staticmethod
    def _compute_linthresh(arr: np.ndarray) -> float:
        """Compute linthresh for symlog scale from data.

        The linear/log boundary is set at the 1st percentile of absolute
        non-zero values — a narrow band around zero that adapts to each
        channel's range.
        """
        if len(arr) == 0:
            return 1.0
        abs_vals = np.abs(arr)
        nonzero = abs_vals[abs_vals > 0]
        if len(nonzero) == 0:
            return 1.0
        return max(float(np.percentile(nonzero, 1)), 1e-3)

    def _refresh_plot(self, log_zoom: bool = False):
        """Public entry point — sets _refreshing and delegates to worker."""
        if self.fcs is None or self._refreshing:
            return
        self._refreshing = True
        self._do_refresh_plot(log_zoom)

    def _do_refresh_plot(self, log_zoom: bool = False):
        """Worker — assumes _refreshing is already True.

        Hides axes while rebuilding, then does a single synchronous
        draw once everything is consistent.
        """
        # Reentrancy guard: flush_events() can pump callbacks that
        # trigger another refresh — skip if we are already inside.
        if self._in_do_refresh:
            return
        self._in_do_refresh = True

        canvas = self.fig.canvas

        self.ax_main.set_visible(False)
        self.ax_stats.set_visible(False)
        self.ax_parent.set_visible(False)

        try:
            xi, yi, xn, yn = self._current_xy()
            x = self.fcs.data[:, xi].copy()
            y = self.fcs.data[:, yi].copy()

            # If parent gate selected, show only those points
            parent_mask = None
            if self._selected_parent_uid:
                parent_gate = next((g for g in self.gate_mgr.gates if g.uid == self._selected_parent_uid), None)
                if parent_gate and parent_gate.x_channel == xn and parent_gate.y_channel == yn:
                    parent_mask = parent_gate.contains(x, y)
                    x = x[parent_mask]
                    y = y[parent_mask]
                    if log_zoom:
                        self._log(f"Displaying {parent_mask.sum():,} points from '{parent_gate.name}'")

            # Sanitise: replace NaN/Inf with 0 to prevent renderer crashes
            finite_mask = np.isfinite(x) & np.isfinite(y)
            if not finite_mask.all():
                x = x[finite_mask]
                y = y[finite_mask]

            x_label = effective_channel_label(xn, self._marker_map)
            y_label = effective_channel_label(yn, self._marker_map)

            self.ax_main.set_navigate(False)

            if self._view_mode == "1D":
                # ── 1D histogram view ──
                self.ax_main.clear()
                pw = self._get_pw_params(x)  # (dmin,dmax,anchor,frac) or None
                if pw is not None and len(x) > 0:
                    dmin, dmax, anchor, frac = pw
                    x_t = self._pw_transform(x, dmin, dmax, anchor, frac)
                    self.ax_main.hist(
                        x_t, bins=256, color="steelblue", edgecolor="none",
                        alpha=0.7, density=True, label="All events",
                    )
                    self._set_pw_ticks(self.ax_main, dmin, dmax, anchor, frac)
                    self.ax_main.set_xlim(-0.02, 1.02)
                    # Draw anchor marker
                    anchor_t = self._pw_transform(
                        np.array([anchor]), dmin, dmax, anchor, frac
                    )[0]
                    self.ax_main.axvline(anchor_t, color="red", lw=1,
                                        ls=":", alpha=0.5)
                    self.ax_main.set_xlabel(f"{x_label}  (compressed)")
                elif len(x) > 0:
                    self.ax_main.hist(
                        x, bins=256, color="steelblue", edgecolor="none",
                        alpha=0.7, density=True, label="All events",
                    )
                    self.ax_main.set_xlabel(x_label)
                else:
                    self.ax_main.set_xlabel(x_label)
                self.ax_main.set_ylabel("Density")
                self.ax_main.set_title(f"1D Histogram — {x_label}")

                if pw is None:
                    if self._x_scale == "log":
                        self.ax_main.set_xscale("symlog", linthresh=self._compute_linthresh(x))
                    else:
                        self.ax_main.set_xscale("linear")

                # Draw gate ranges as vertical shaded spans
                self._draw_1d_gate_overlays(xn, yn, x, y, pw_params=pw)
            else:
                # ── 2D scatter view ──
                density_scatter(self.ax_main, x, y)
                self.ax_main.set_xlabel(x_label)
                self.ax_main.set_ylabel(y_label)

                # Apply axis scale AFTER plotting
                if self._x_scale == "log":
                    self.ax_main.set_xscale("symlog", linthresh=self._compute_linthresh(x))
                else:
                    self.ax_main.set_xscale("linear")

                if self._y_scale == "log":
                    self.ax_main.set_yscale("symlog", linthresh=self._compute_linthresh(y))
                else:
                    self.ax_main.set_yscale("linear")

                # Compute stats for gate labels
                _stats = self.gate_mgr.compute_stats(
                    self.fcs.data, self.fcs.channel_names
                )
                _stats_by_uid = {s["uid"]: s for s in _stats}

                # Draw gates on the current channel pair
                parent_on_view = False
                if self._selected_parent_uid:
                    pg = next((g for g in self.gate_mgr.gates
                               if g.uid == self._selected_parent_uid), None)
                    if pg and pg.x_channel == xn and pg.y_channel == yn:
                        parent_on_view = True

                for gate in self.gate_mgr.gates:
                    if gate.x_channel == xn and gate.y_channel == yn:
                        # Build label: name (pct_total% | pct_parent%)
                        s = _stats_by_uid.get(gate.uid)
                        if s:
                            lbl = f"{gate.name}\n{s['percent_of_total']:.1f}% total | {s['percent']:.1f}% parent"
                        else:
                            lbl = gate.name
                        # For quadrant gates, pass the per-quadrant
                        # breakdown so each quadrant gets its own label.
                        qstats = s.get("quadrant_breakdown") if s else None
                        if parent_on_view:
                            if (gate.uid == self._selected_parent_uid
                                    or gate.parent_gate_uid == self._selected_parent_uid):
                                draw_gate_overlay(self.ax_main, gate,
                                                  label_text=lbl,
                                                  quadrant_stats=qstats)
                        else:
                            draw_gate_overlay(self.ax_main, gate,
                                              label_text=lbl,
                                              quadrant_stats=qstats)

            self.ax_main.set_navigate(True)
            self._refresh_stats()
            self._refresh_parent_selector()
        except Exception as exc:
            self._log(f"Plot error: {exc}")
            logger.exception("Plot error")
        finally:
            self.ax_main.set_visible(True)
            self.ax_stats.set_visible(True)
            self.ax_parent.set_visible(True)
            # Single synchronous draw, then process events.
            # Keep _refreshing True until done so that any callbacks
            # triggered by flush_events() cannot schedule a draw_idle.
            try:
                canvas.draw()
            except Exception:
                pass
            try:
                canvas.flush_events()
            except Exception:
                pass
            self._refreshing = False
            self._in_do_refresh = False
            # Propagate the refresh to any open gate sub-windows so edits
            # to a gate's geometry (translate / rotate / stretch / rename
            # / remove / parent-gate change) are reflected in its child
            # window immediately. GateWindow._refresh only operates on
            # its own axes so this can't recurse back into us.
            try:
                self._refresh_gate_windows()
            except Exception:
                logger.exception("Failed to refresh gate sub-windows")

    def _refresh_parent_selector(self):
        """Update parent gate selector with current gates."""
        # CRITICAL: disconnect the old RadioButtons' event callbacks BEFORE
        # clearing the axes.  RadioButtons registers a 'draw_event' handler
        # (_clear) that tries to redraw its PathCollection.  After ax.clear()
        # the collection is detached from the figure (get_figure() → None),
        # so the stale callback causes either an AttributeError or a segfault
        # when canvas.draw() fires later.
        had_gates = self._radio_parent is not None
        if self._radio_parent is not None:
            try:
                self._radio_parent.disconnect_events()
            except Exception:
                pass
            self._radio_parent = None

        self.ax_parent.clear()
        self.ax_parent.set_frame_on(False)

        if not self.gate_mgr.gates:
            self._selected_parent_uid = None
            return

        # Build list: "None" + gate names
        options = ["None"] + [g.name for g in self.gate_mgr.gates]

        # Find which option should be active based on current selection
        active_idx = 0  # Default to "None"
        if self._selected_parent_uid:
            for i, gate in enumerate(self.gate_mgr.gates):
                if gate.uid == self._selected_parent_uid:
                    active_idx = i + 1  # +1 because "None" is at index 0
                    break

        self._radio_parent = RadioButtons(self.ax_parent, options, active=active_idx)
        self._radio_parent.on_clicked(self._on_parent_change)
        # DON'T reset _selected_parent_uid - preserve the current selection!

        if not had_gates and self.gate_mgr.gates:
            self._log("Tip: Use 'Parent Gate' for sub-gating")

    def _on_parent_change(self, label):
        """Handle parent gate selection change."""
        self._refreshing = True          # suppress draw_idle from _log
        if label == "None":
            self._selected_parent_uid = None
            self._log("Sub-gating: Root-level gates (no parent)")
            self._log("View: Showing all data")
        else:
            for gate in self.gate_mgr.gates:
                if gate.name == label:
                    self._selected_parent_uid = gate.uid
                    self._log(f"Sub-gating: Children of '{label}'")
                    break
        # Keep _refreshing=True → call worker directly
        self._do_refresh_plot(log_zoom=True)

    def _refresh_stats(self):
        self.ax_stats.clear()
        self.ax_stats.set_xticks([])
        self.ax_stats.set_yticks([])
        self.ax_stats.set_title("Gate Statistics", fontsize=9, loc="left", fontweight="bold")

        if self.fcs is None:
            return

        stats = self.gate_mgr.compute_stats(
            self.fcs.data, self.fcs.channel_names
        )
        if not stats:
            self.ax_stats.text(
                0.5, 0.5, "No gates", ha="center", va="center",
                fontsize=10, color="grey", transform=self.ax_stats.transAxes,
            )
            return

        lines = []
        for s in stats:
            indent = "  " if s.get("parent_uid") else ""
            name = s["name"]
            # For quadrant gates, show a header line (the gate's name +
            # selected quadrant) followed by one indented line per quadrant
            # so the user sees Q1..Q4 stats at a glance, not just the
            # selected one.
            qb = s.get("quadrant_breakdown")
            if qb:
                sel = s.get("selected_quadrant", "")
                lines.append(f"{indent}{name:6s}  (Q-split, sel={sel})")
                for q in ("Q1", "Q2", "Q3", "Q4"):
                    qs = qb.get(q) or {}
                    marker = "*" if q == sel else " "
                    lines.append(
                        f"{indent}  {marker}{q}  {qs.get('count', 0):>8,}  "
                        f"({qs.get('percent', 0.0):.1f}%)"
                    )
            else:
                lines.append(
                    f"{indent}{name:6s}  {s['count']:>8,}  ({s['percent']:.1f}%)"
                )
        text = "\n".join(lines)
        self.ax_stats.text(
            0.05, 0.95, text, family="monospace", fontsize=8,
            va="top", transform=self.ax_stats.transAxes,
        )

    def _draw_1d_gate_overlays(self, xn: str, yn: str,
                               x: np.ndarray, y: np.ndarray,
                               pw_params: tuple | None = None):
        """In 1D histogram mode, overlay per-gate histograms and
        show vertical spans for the X-range of each gate.

        If *pw_params* = (dmin, dmax, anchor, frac) is given, data and gate
        positions are displayed in piecewise-transformed space.
        """
        stats = self.gate_mgr.compute_stats(
            self.fcs.data, self.fcs.channel_names
        ) if self.fcs else []
        stats_by_uid = {s["uid"]: s for s in stats}

        for gate in self.gate_mgr.gates:
            # ThresholdGate matches on x_channel only; 2D gates need both
            if isinstance(gate, ThresholdGate):
                if gate.x_channel != xn:
                    continue
            else:
                if gate.x_channel != xn or gate.y_channel != yn:
                    continue
            mask = gate.contains(x, y)
            gated_x = x[mask]
            if len(gated_x) == 0:
                continue

            # Build label with percentages
            s = stats_by_uid.get(gate.uid)
            if s:
                lbl = f"{gate.name} ({s['percent_of_total']:.1f}%|{s['percent']:.1f}%)"
            else:
                lbl = gate.name

            if isinstance(gate, ThresholdGate):
                if pw_params is not None:
                    dmin, dmax, anchor, frac = pw_params
                    t_pos = self._pw_transform(
                        np.array([gate.threshold]), dmin, dmax, anchor, frac
                    )[0]
                    xlim = self.ax_main.get_xlim()
                    self.ax_main.axvline(t_pos, color=gate.color, lw=2,
                                        ls="--", alpha=0.7)
                    if gate.side == "left":
                        self.ax_main.axvspan(xlim[0], t_pos,
                                             color=gate.color, alpha=0.08)
                    else:
                        self.ax_main.axvspan(t_pos, xlim[1],
                                             color=gate.color, alpha=0.08)
                    ylim = self.ax_main.get_ylim()
                    import matplotlib.patheffects as _pe
                    _ann = self.ax_main.annotate(
                        lbl, xy=(t_pos, ylim[1] * 0.9 if ylim[1] > 0 else 0),
                        xytext=(5, -10), textcoords="offset points",
                        fontsize=8, fontweight="bold", color=gate.color,
                    )
                    _ann.set_path_effects(
                        [_pe.withStroke(linewidth=2.5, foreground="white")]
                    )
                else:
                    from .plotting import _draw_threshold_overlay
                    _draw_threshold_overlay(self.ax_main, gate, label_text=lbl)
            else:
                # Overlay the gated histogram
                if pw_params is not None:
                    dmin, dmax, anchor, frac = pw_params
                    gated_x_t = self._pw_transform(
                        gated_x, dmin, dmax, anchor, frac
                    )
                else:
                    gated_x_t = gated_x
                self.ax_main.hist(
                    gated_x_t, bins=256, color=gate.color,
                    edgecolor="none", alpha=0.4, density=True,
                    label=lbl,
                )
                xmin_g, xmax_g = float(gated_x_t.min()), float(gated_x_t.max())
                self.ax_main.axvspan(
                    xmin_g, xmax_g, color=gate.color, alpha=0.08,
                )
        if self.gate_mgr.gates:
            self.ax_main.legend(fontsize=7, loc="upper right")

    # ================================================================ #
    #  Mouse / key interaction
    # ================================================================ #
    def _clear_temp(self):
        for a in self._temp_artists:
            try:
                a.remove()
            except Exception:
                pass
        self._temp_artists.clear()

    def _on_click(self, event):
        if self._refreshing:
            return

        # Click outside plot area → switch to Navigate (Task #34)
        if (event.inaxes != self.ax_main and self.fcs is not None
                and self._mode not in (MODE_NAV,)):
            # Only auto-switch for action modes, not if clicking on controls
            if event.inaxes is None:  # clicked outside any axes
                self._mode = MODE_NAV
                self.radio_mode.set_active(0)
                self._log("Switched to Navigate (clicked outside plot)")
                return

        if event.inaxes != self.ax_main or self.fcs is None:
            return
        if event.xdata is None or event.ydata is None:
            return

        # ── Navigate + 1D: axis compression drag ──
        if self._mode == MODE_NAV and self._view_mode == "1D" and event.button == 1:
            if event.dblclick:
                # Double-click resets compression
                self._reset_compression()
                self._refreshing = True
                self._do_refresh_plot()
                self._log("Axis compression reset")
                return
            self._start_compress_drag(event)
            return

        if self._mode == MODE_POLY:
            self._poly_click(event)
        elif self._mode == MODE_RECT:
            if event.button == 1:
                self._rect_origin = (event.xdata, event.ydata)
        elif self._mode == MODE_ELLIPSE:
            if event.button == 1:
                self._ellipse_origin = (event.xdata, event.ydata)
        elif self._mode == MODE_QUAD:
            if event.button == 1:
                self._quad_click(event)
        elif self._mode == MODE_THRESH:
            if event.button == 1:
                self._thresh_click(event)
        elif self._mode in (MODE_TRANSLATE, MODE_ROTATE):
            self._move_click(event)
        elif self._mode == MODE_STRETCH:
            self._stretch_click(event)

    def _on_release(self, event):
        if self._refreshing:
            return
        # End compression drag regardless of inaxes
        if self._compress_dragging:
            self._compress_dragging = False
            return
        if event.inaxes != self.ax_main or self.fcs is None:
            return
        if event.xdata is None or event.ydata is None:
            self._rect_origin = None
            self._ellipse_origin = None
            self._handle_drag_type = None
            self._handle_drag_start = None
            self._stretch_dragging = False
            self._stretch_last_xy = None
            return
        if self._mode == MODE_RECT and self._rect_origin is not None:
            self._rect_release(event)
        elif self._mode == MODE_ELLIPSE and self._ellipse_origin is not None:
            self._ellipse_release(event)
        elif self._mode in (MODE_TRANSLATE, MODE_ROTATE) and self._handle_drag_type is not None:
            self._move_release(event)
        elif self._mode == MODE_STRETCH and self._stretch_dragging:
            self._stretch_drag_end(event)

    def _on_motion(self, event):
        if self._refreshing:
            return
        # Compression drag: use pixel coords, works even outside axes
        if self._compress_dragging:
            self._update_compress_drag(event)
            return
        if event.inaxes != self.ax_main or self.fcs is None:
            return
        if event.xdata is None or event.ydata is None:
            return
        if self._mode == MODE_RECT and self._rect_origin is not None:
            self._rect_motion(event)
        elif self._mode == MODE_POLY and len(self._poly_verts) >= 1:
            self._poly_motion(event)
        elif self._mode == MODE_ELLIPSE and self._ellipse_origin is not None:
            self._ellipse_motion(event)
        elif self._mode in (MODE_TRANSLATE, MODE_ROTATE) and self._handle_drag_type is not None:
            self._move_motion(event)
        elif self._mode == MODE_STRETCH and self._stretch_dragging:
            self._stretch_drag_motion(event)

    # ── Compression drag helpers ──

    def _start_compress_drag(self, event):
        """Begin axis compression drag in 1D Navigate mode."""
        if self._compress_anchor is not None and self._compress_frac is not None:
            # Already compressed — click is in [0,1] space, inverse to data
            anchor_data = self._pw_inverse(
                event.xdata, self._compress_dmin, self._compress_dmax,
                self._compress_anchor, self._compress_frac,
            )
        else:
            anchor_data = event.xdata

        xi, _, _, _ = self._current_xy()
        x_all = self.fcs.data[:, xi]
        dmin, dmax = float(np.min(x_all)), float(np.max(x_all))
        if dmax <= dmin:
            return

        natural_frac = (anchor_data - dmin) / (dmax - dmin)
        natural_frac = max(0.02, min(0.98, natural_frac))

        self._compress_anchor = anchor_data
        self._compress_dmin = dmin
        self._compress_dmax = dmax
        self._compress_frac = natural_frac
        self._compress_base_frac = natural_frac
        self._compress_drag_px = event.x
        self._compress_dragging = True

    def _update_compress_drag(self, event):
        """Update compression during drag (pixel coordinates)."""
        if not self._compress_dragging or event.x is None:
            return
        ax_extent = self.ax_main.get_window_extent()
        ax_width = ax_extent.width
        if ax_width <= 0:
            return

        dx_px = event.x - self._compress_drag_px
        shift = dx_px / ax_width
        new_frac = self._compress_base_frac + shift
        new_frac = max(0.02, min(0.98, new_frac))
        self._compress_frac = new_frac

        self._refreshing = True
        self._do_refresh_plot()

    def _on_key(self, event):
        # Undo / redo (Ctrl+Z / Ctrl+Shift+Z)
        if event.key in ("ctrl+z", "cmd+z"):
            self._undo()
            return
        if event.key in ("ctrl+shift+z", "cmd+shift+z", "ctrl+y", "cmd+y"):
            self._redo()
            return

        if event.key == "enter" and self._mode == MODE_POLY:
            self._close_polygon()

        # Parse arrow direction and shift modifier from key string
        arrow_keys = {"left", "right", "up", "down",
                      "shift+left", "shift+right", "shift+up", "shift+down"}
        direction = event.key.replace("shift+", "") if event.key in arrow_keys else None
        fine = event.key.startswith("shift+") if event.key in arrow_keys else False
        scale = 1.0 / 3.0 if fine else 1.0

        # Arrow keys in Translate mode — all 4 directions move the gate
        if self._mode == MODE_TRANSLATE and self._handle_selected_gate is not None:
            gate = self._handle_selected_gate
            if direction in ("left", "right", "up", "down"):
                self._push_undo()
                dx, dy = 0.0, 0.0
                if direction in ("left", "right"):
                    dx = self._scale_aware_step(
                        self.ax_main, gate, "x",
                        self._x_scale, direction == "right",
                    ) * scale
                else:
                    # ThresholdGate only moves horizontally
                    if isinstance(gate, ThresholdGate):
                        return
                    dy = self._scale_aware_step(
                        self.ax_main, gate, "y",
                        self._y_scale, direction == "up",
                    ) * scale
                self._apply_move(gate, dx, dy)
                self._clear_handles()
                self._refresh_plot()
                self._handle_selected_gate = gate
                self._draw_handles(gate)

        # Arrow keys in Rotate mode — left/right rotate the gate
        if self._mode == MODE_ROTATE and self._handle_selected_gate is not None:
            gate = self._handle_selected_gate
            if direction in ("left", "right"):
                if isinstance(gate, (ThresholdGate, QuadrantGate)):
                    return  # no rotation for these
                self._push_undo()
                base_delta = np.radians(2) if direction == "right" else np.radians(-2)
                delta = base_delta * scale
                cx, cy = self._gate_centroid(gate)
                self._apply_rotate(gate, delta, cx, cy)
                gate = next((g for g in self.gate_mgr.gates if g.uid == gate.uid), gate)
                self._handle_selected_gate = gate
                self._clear_handles()
                self._refresh_plot()
                self._handle_selected_gate = gate
                self._draw_handles(gate)

        # Stretch mode: Tab cycles points, arrows move selected point
        if self._mode == MODE_STRETCH and self._stretch_selected_gate is not None:
            if event.key == "tab":
                if not self._stretch_points:
                    return
                self._stretch_point_idx = (
                    (self._stretch_point_idx + 1) % len(self._stretch_points)
                )
                self._clear_stretch_highlight()
                self._draw_stretch_highlight()
                self._log(f"Control point {self._stretch_point_idx + 1}/"
                          f"{len(self._stretch_points)}")
                self.fig.canvas.draw_idle()
            elif direction in ("left", "right", "up", "down"):
                if self._stretch_point_idx < 0:
                    return
                self._push_undo()
                gate = self._stretch_selected_gate
                idx = self._stretch_point_idx
                # Anchor the log-mode step at the point being stretched,
                # not the gate centroid — otherwise the visible step on a
                # log axis is wrong for any point that isn't at the centre
                # of the gate (and that's almost every stretch point).
                px, py = self._stretch_points[idx]
                dx, dy = 0.0, 0.0
                if direction in ("left", "right"):
                    dx = self._scale_aware_step(
                        self.ax_main, gate, "x",
                        self._x_scale, direction == "right",
                        position=px,
                    ) * scale
                else:
                    dy = self._scale_aware_step(
                        self.ax_main, gate, "y",
                        self._y_scale, direction == "up",
                        position=py,
                    ) * scale
                self._apply_stretch_point(gate, idx, dx, dy)
                self._clear_stretch_highlight()
                self._refresh_plot()
                self._stretch_selected_gate = gate
                self._draw_stretch_highlight()
                self.fig.canvas.draw_idle()

    @staticmethod
    def _disable_tk_tab_traversal(fig):
        """Prevent Tk from consuming Tab for widget-focus traversal.

        Without this, pressing Tab while the cursor is away from the canvas
        triggers Tk's focus-cycling instead of reaching matplotlib's
        key_press_event handler.
        """
        try:
            canvas = fig.canvas
            tk_canvas = canvas.get_tk_widget()
            # Bind Tab directly on the canvas so it fires our matplotlib
            # handler and then returns 'break' to suppress Tk traversal.
            def _on_tk_tab(tk_event):
                # Manually fire the matplotlib key_press_event
                from matplotlib.backend_bases import KeyEvent
                key_event = KeyEvent("key_press_event", canvas,
                                     "tab", x=0, y=0)
                canvas.callbacks.process("key_press_event", key_event)
                return "break"
            tk_canvas.bind('<Tab>', _on_tk_tab)
            # Also bind on the top-level window to catch Tab when focus
            # is on a radio button or other widget.
            tk_canvas.winfo_toplevel().bind('<Tab>', _on_tk_tab)
        except Exception:
            pass  # Non-TkAgg backend — nothing to do

    @staticmethod
    def _grab_canvas_focus(fig):
        """Move keyboard focus to the matplotlib canvas widget."""
        try:
            fig.canvas.get_tk_widget().focus_set()
        except Exception:
            pass

    @staticmethod
    def _scale_aware_step(ax, gate, axis: str, scale: str,
                          positive: bool, position: float | None = None) -> float:
        """Compute a movement step that is constant in the displayed scale.

        * Linear axis: 2% of the visible range, returned as ``±dx`` / ``±dy``.
        * Log axis: a constant step in log10-space → multiplicative in data
          space.  The step magnitude is anchored at ``position`` when
          provided (e.g. the actual control point being stretched), so the
          visible distance the point moves is uniform on a log axis no
          matter where the point sits.  When ``position`` is ``None`` we
          fall back to the gate's centroid — the right behaviour for whole-
          gate moves (translate / rotate) where there's no single point.
        """
        if axis == "x":
            lo, hi = ax.get_xlim()
        else:
            lo, hi = ax.get_ylim()

        if scale == "log" and lo > 0 and hi > 0:
            log_range = np.log10(hi) - np.log10(lo)
            log_step = log_range * 0.02
            factor = 10 ** (log_step if positive else -log_step)
            if position is not None and position > 0:
                anchor = position
            else:
                cx, cy = FlowCytApp._gate_centroid(gate)
                anchor = cx if axis == "x" else cy
                if anchor <= 0:
                    anchor = max(lo, 1e-3)
            return anchor * (factor - 1.0)
        else:
            step = (hi - lo) * 0.02
            return step if positive else -step

    # -- Polygon -------------------------------------------------------
    def _poly_motion(self, event):
        """Show preview line from last vertex to cursor, and closing line."""
        if event.xdata is None or event.ydata is None:
            return

        # Clear previous preview
        temp_to_keep = []
        for artist in self._temp_artists:
            if hasattr(artist, 'get_linestyle'):
                if artist.get_linestyle() == ':':
                    try:
                        artist.remove()
                    except Exception:
                        pass
                else:
                    temp_to_keep.append(artist)
            else:
                temp_to_keep.append(artist)
        self._temp_artists = temp_to_keep

        if len(self._poly_verts) >= 1:
            # Preview line from last vertex to cursor
            xs = [self._poly_verts[-1][0], event.xdata]
            ys = [self._poly_verts[-1][1], event.ydata]
            ln = self.ax_main.plot(xs, ys, "r:", lw=1.0, alpha=0.7)[0]
            self._temp_artists.append(ln)

            # If we have 2+ vertices, also show closing line preview
            if len(self._poly_verts) >= 2:
                xs_close = [event.xdata, self._poly_verts[0][0]]
                ys_close = [event.ydata, self._poly_verts[0][1]]
                ln_close = self.ax_main.plot(xs_close, ys_close, "r:", lw=1.0, alpha=0.5)[0]
                self._temp_artists.append(ln_close)

        self.fig.canvas.draw_idle()

    def _poly_click(self, event):
        if event.xdata is None or event.ydata is None:
            return
        if event.button == 1:
            import time
            current_time = time.time()

            # Check for double-click (within 300ms)
            if len(self._poly_verts) >= 3 and (current_time - self._poly_last_click_time) < 0.3:
                self._close_polygon()
                self._poly_last_click_time = 0.0
                return

            self._poly_last_click_time = current_time

            self._poly_verts.append((event.xdata, event.ydata))
            pt = self.ax_main.plot(
                event.xdata, event.ydata, "rx", markersize=8
            )[0]
            self._temp_artists.append(pt)
            if len(self._poly_verts) > 1:
                xs = [v[0] for v in self._poly_verts[-2:]]
                ys = [v[1] for v in self._poly_verts[-2:]]
                ln = self.ax_main.plot(xs, ys, "r--", lw=1.2)[0]
                self._temp_artists.append(ln)
            self.fig.canvas.draw_idle()
        elif event.button == 3:
            self._close_polygon()

    def _close_polygon(self):
        if len(self._poly_verts) < 3:
            self._log("Need >= 3 vertices for polygon")
            return
        self._push_undo()
        xi, yi, xn, yn = self._current_xy()
        n = len(self.gate_mgr.gates) + 1
        gate = self.gate_mgr.add_polygon_gate(
            name=f"P{n}", x_channel=xn, y_channel=yn,
            vertices=list(self._poly_verts),
            parent_gate_uid=self._selected_parent_uid,
        )
        self._poly_verts.clear()
        self._clear_temp()
        self._refresh_plot()
        self._print_gate_created(gate)
        self._open_gate_window(gate)

    # -- Rectangle -----------------------------------------------------
    def _rect_motion(self, event):
        if event.xdata is None or event.ydata is None:
            return
        self._clear_temp()
        x0, y0 = self._rect_origin
        x1, y1 = event.xdata, event.ydata
        rect = RectPatch(
            (min(x0, x1), min(y0, y1)),
            abs(x1 - x0), abs(y1 - y0),
            fill=False, edgecolor="red", lw=1.5, linestyle="--",
        )
        self.ax_main.add_patch(rect)
        self._temp_artists.append(rect)
        self.fig.canvas.draw_idle()

    def _rect_release(self, event):
        if event.xdata is None or event.ydata is None:
            self._rect_origin = None
            self._clear_temp()
            return
        x0, y0 = self._rect_origin
        x1, y1 = event.xdata, event.ydata
        self._rect_origin = None
        self._clear_temp()

        if abs(x1 - x0) < 1e-9 or abs(y1 - y0) < 1e-9:
            return

        self._push_undo()
        xi, yi, xn, yn = self._current_xy()
        n = len(self.gate_mgr.gates) + 1
        gate = self.gate_mgr.add_rectangle_gate(
            name=f"R{n}", x_channel=xn, y_channel=yn,
            x_min=min(x0, x1), x_max=max(x0, x1),
            y_min=min(y0, y1), y_max=max(y0, y1),
            parent_gate_uid=self._selected_parent_uid,
        )
        self._refresh_plot()
        self._print_gate_created(gate)
        self._open_gate_window(gate)

    # -- Ellipse -------------------------------------------------------
    def _ellipse_motion(self, event):
        if event.xdata is None or event.ydata is None:
            return

        self._clear_temp()
        cx, cy = self._ellipse_origin
        dx = abs(event.xdata - cx)
        dy = abs(event.ydata - cy)

        ellipse = EllipsePatch(
            (cx, cy), 2 * dx, 2 * dy, angle=0,
            fill=False, edgecolor="red", lw=1.5, linestyle="--"
        )
        self.ax_main.add_patch(ellipse)
        self._temp_artists.append(ellipse)
        self.fig.canvas.draw_idle()

    def _ellipse_release(self, event):
        if event.xdata is None or event.ydata is None:
            self._ellipse_origin = None
            self._clear_temp()
            return

        cx, cy = self._ellipse_origin
        dx = abs(event.xdata - cx)
        dy = abs(event.ydata - cy)

        self._ellipse_origin = None
        self._clear_temp()

        if dx < 1e-9 or dy < 1e-9:
            return

        self._push_undo()
        xi, yi, xn, yn = self._current_xy()
        n = len(self.gate_mgr.gates) + 1
        gate = self.gate_mgr.add_ellipse_gate(
            name=f"E{n}",
            x_channel=xn,
            y_channel=yn,
            center_x=cx,
            center_y=cy,
            semi_x=dx,
            semi_y=dy,
            angle=0.0,
            parent_gate_uid=self._selected_parent_uid,
        )
        self._refresh_plot()
        self._print_gate_created(gate)
        self._open_gate_window(gate)

    # -- Handle-based gate editing (PowerPoint/Keynote style) ----------------
    # All operations (move, resize, rotate) use left-click on visible
    # handles, making them trackpad-friendly.
    #
    # Handle layout for a selected gate:
    #   [rotate]   ← circle above top-center
    #       |
    #   TL ─ T ─ TR
    #   |         |
    #   L    ●    R    (● = centroid; clicking interior = move)
    #   |         |
    #   BL ─ B ─ BR
    #
    # Corner handles (TL/TR/BL/BR) → resize in both X and Y
    # Edge handles (T/B/L/R) → stretch in one axis only
    # Rotate handle → rotate around centroid
    # Interior click → translate

    HANDLE_RADIUS_PX = 10  # pixel radius for handle hit-test

    @staticmethod
    def _gate_centroid(gate: Gate) -> tuple[float, float]:
        if isinstance(gate, EllipseGate):
            return gate.center_x, gate.center_y
        elif isinstance(gate, RectangleGate):
            return (gate.x_min + gate.x_max) / 2, (gate.y_min + gate.y_max) / 2
        elif isinstance(gate, PolygonGate) and gate.vertices:
            xs = [v[0] for v in gate.vertices]
            ys = [v[1] for v in gate.vertices]
            return float(np.mean(xs)), float(np.mean(ys))
        elif isinstance(gate, QuadrantGate):
            return gate.mid_x, gate.mid_y
        return 0.0, 0.0

    @staticmethod
    def _gate_bbox(gate: Gate) -> tuple[float, float, float, float]:
        """Return (xmin, xmax, ymin, ymax) bounding box of a gate."""
        if isinstance(gate, RectangleGate):
            return gate.x_min, gate.x_max, gate.y_min, gate.y_max
        elif isinstance(gate, EllipseGate):
            # Compute rotated bounding box
            cos_a = np.cos(gate.angle)
            sin_a = np.sin(gate.angle)
            # Half-widths in x/y after rotation
            hw = np.sqrt((gate.semi_x * cos_a) ** 2 + (gate.semi_y * sin_a) ** 2)
            hh = np.sqrt((gate.semi_x * sin_a) ** 2 + (gate.semi_y * cos_a) ** 2)
            return (gate.center_x - hw, gate.center_x + hw,
                    gate.center_y - hh, gate.center_y + hh)
        elif isinstance(gate, PolygonGate) and gate.vertices:
            xs = [v[0] for v in gate.vertices]
            ys = [v[1] for v in gate.vertices]
            return min(xs), max(xs), min(ys), max(ys)
        elif isinstance(gate, QuadrantGate):
            return gate.mid_x - 1, gate.mid_x + 1, gate.mid_y - 1, gate.mid_y + 1
        elif isinstance(gate, ThresholdGate):
            t = gate.threshold
            return t, t, 0, 1  # degenerate: single vertical line
        return 0, 1, 0, 1

    @staticmethod
    def _rotate_point(px, py, cx, cy, angle):
        dx, dy = px - cx, py - cy
        cos_a, sin_a = np.cos(angle), np.sin(angle)
        return cx + dx * cos_a - dy * sin_a, cy + dx * sin_a + dy * cos_a

    def _find_gate_at(self, px: float, py: float) -> Gate | None:
        _, _, xn, yn = self._current_xy()
        candidates = []
        for gate in self.gate_mgr.gates:
            if gate.x_channel != xn or gate.y_channel != yn:
                continue
            if isinstance(gate, ThresholdGate):
                # Check proximity to threshold line (in pixel space)
                try:
                    gate_px = self.ax_main.transData.transform((gate.threshold, 0))[0]
                    click_px = self.ax_main.transData.transform((px, 0))[0]
                    if abs(gate_px - click_px) < 15:
                        candidates.append(gate)
                except Exception:
                    pass
                continue
            if gate.contains(np.array([px]), np.array([py]))[0]:
                candidates.append(gate)
        if not candidates:
            return None
        candidates.sort(key=lambda g: len(g.vertices) if g.vertices else 1e9)
        return candidates[0]

    def _compute_handles(self, gate: Gate, ax=None) -> list[tuple[float, float, str]]:
        """Compute handle positions in data coordinates.

        Returns list of (x, y, handle_type).
        """
        if ax is None:
            ax = self.ax_main

        # ThresholdGate: single handle on the threshold line
        if isinstance(gate, ThresholdGate):
            ylim = ax.get_ylim()
            my = (ylim[0] + ylim[1]) / 2
            return [(gate.threshold, my, "move")]

        xmin, xmax, ymin, ymax = self._gate_bbox(gate)
        mx = (xmin + xmax) / 2
        my = (ymin + ymax) / 2

        handles = [
            (xmin, ymax, "tl"), (mx, ymax, "t"), (xmax, ymax, "tr"),
            (xmin, my,   "l"),                    (xmax, my,   "r"),
            (xmin, ymin, "bl"), (mx, ymin, "b"), (xmax, ymin, "br"),
        ]

        if not isinstance(gate, (QuadrantGate, ThresholdGate)):
            try:
                disp_tc = ax.transData.transform((mx, ymax))
                disp_rot = (disp_tc[0], disp_tc[1] + 30)
                rx, ry = ax.transData.inverted().transform(disp_rot)
                handles.append((rx, ry, "rot"))
            except Exception:
                handles.append((mx, ymax, "rot"))

        return handles

    def _draw_handles(self, gate: Gate):
        """Draw selection handles around gate."""
        handles = self._compute_handles(gate)
        self._handle_positions = handles
        self._handle_artists = []

        xmin, xmax, ymin, ymax = self._gate_bbox(gate)
        mx, my = (xmin + xmax) / 2, (ymin + ymax) / 2

        # Dashed bounding box
        bbox_patch = RectPatch(
            (xmin, ymin), xmax - xmin, ymax - ymin,
            fill=False, edgecolor="#444444", lw=1.0, ls=":",
            alpha=0.7,
        )
        self.ax_main.add_patch(bbox_patch)
        self._handle_artists.append(bbox_patch)

        for hx, hy, htype in handles:
            if htype == "rot":
                # Rotation handle: green circle + line from top-center
                marker = self.ax_main.plot(
                    hx, hy, "o", color="#22aa22", markersize=8,
                    markeredgecolor="black", markeredgewidth=1,
                    zorder=100,
                )[0]
                # Connector line from top-center to rotation handle
                line = self.ax_main.plot(
                    [mx, hx], [ymax, hy],
                    color="#444444", lw=1.0, ls=":", alpha=0.6,
                )[0]
                self._handle_artists.extend([marker, line])
            else:
                # Resize/stretch handles: blue squares
                marker = self.ax_main.plot(
                    hx, hy, "s", color="#3388dd", markersize=7,
                    markeredgecolor="black", markeredgewidth=1,
                    zorder=100,
                )[0]
                self._handle_artists.append(marker)

        self.fig.canvas.draw_idle()

    def _clear_handles(self):
        """Remove handle artists from the plot."""
        for a in getattr(self, '_handle_artists', []):
            try:
                a.remove()
            except Exception:
                pass
        self._handle_artists = []
        self._handle_positions = []

    def _hit_test_handle(self, event) -> str | None:
        """Check if click is near a handle. Returns handle type or None."""
        if not getattr(self, '_handle_positions', []):
            return None
        try:
            click_disp = self.ax_main.transData.transform(
                (event.xdata, event.ydata)
            )
        except Exception:
            return None

        for hx, hy, htype in self._handle_positions:
            try:
                h_disp = self.ax_main.transData.transform((hx, hy))
                dist = np.hypot(click_disp[0] - h_disp[0],
                                click_disp[1] - h_disp[1])
                if dist <= self.HANDLE_RADIUS_PX:
                    return htype
            except Exception:
                continue
        return None

    def _move_click(self, event):
        """Handle click in Move Gate mode.

        1. If a gate is already selected, check if click is on a handle
           or inside the gate (move) or outside (deselect + re-select).
        2. If no gate selected, find one under cursor and select it.
        """
        hit = self._hit_test_handle(event)
        if hit is not None:
            # Clicked on a handle → start that operation
            self._handle_drag_type = hit
            self._handle_drag_start = (event.xdata, event.ydata)
            return

        # Not on a handle — find gate under cursor
        gate = self._find_gate_at(event.xdata, event.ydata)

        # Deselect old gate
        self._clear_handles()
        self._clear_temp()

        if gate is None:
            self._handle_selected_gate = None
            return

        if gate == self._handle_selected_gate:
            # Clicked interior of already-selected gate → start move
            self._handle_drag_type = "move"
            self._handle_drag_start = (event.xdata, event.ydata)
            return

        # Select new gate and draw handles
        self._handle_selected_gate = gate
        self._draw_handles(gate)
        self._log(f"Selected gate '{gate.name}' — drag handles to edit")

    def _move_motion(self, event):
        """Preview move/resize/rotate during drag."""
        if event.xdata is None or event.ydata is None:
            return
        gate = self._handle_selected_gate
        if gate is None:
            return
        dtype = self._handle_drag_type
        if dtype is None:
            return

        self._clear_temp()
        sx, sy = self._handle_drag_start
        cx, cy = self._gate_centroid(gate)

        if dtype == "move":
            dx, dy = event.xdata - sx, event.ydata - sy
            self._preview_move(gate, dx, dy)

        elif dtype == "rot":
            # Compute angle in display (pixel) space for correct visual rotation
            trans = self.ax_main.transData
            cx_d, cy_d = trans.transform((cx, cy))
            s_d = trans.transform((sx, sy))
            e_d = trans.transform((event.xdata, event.ydata))
            a0 = np.arctan2(s_d[1] - cy_d, s_d[0] - cx_d)
            a1 = np.arctan2(e_d[1] - cy_d, e_d[0] - cx_d)
            self._preview_rotate(gate, a1 - a0, cx, cy)

        else:
            # Resize / stretch handle
            self._preview_resize(gate, dtype, event.xdata, event.ydata)

        self.fig.canvas.draw_idle()

    def _move_release(self, event):
        """Apply the transformation on release."""
        dtype = self._handle_drag_type
        gate = self._handle_selected_gate
        self._handle_drag_type = None
        self._clear_temp()

        if dtype is None or gate is None:
            return
        if event.xdata is None or event.ydata is None:
            return

        self._push_undo()
        sx, sy = self._handle_drag_start
        cx, cy = self._gate_centroid(gate)

        if dtype == "move":
            dx, dy = event.xdata - sx, event.ydata - sy
            if abs(dx) < 1e-12 and abs(dy) < 1e-12:
                self._undo_stack.pop()  # nothing changed, remove undo entry
                return
            self._apply_move(gate, dx, dy)
            self._log(f"Moved gate '{gate.name}'")

        elif dtype == "rot":
            # Compute angle in display (pixel) space
            trans = self.ax_main.transData
            cx_d, cy_d = trans.transform((cx, cy))
            s_d = trans.transform((sx, sy))
            e_d = trans.transform((event.xdata, event.ydata))
            a0 = np.arctan2(s_d[1] - cy_d, s_d[0] - cx_d)
            a1 = np.arctan2(e_d[1] - cy_d, e_d[0] - cx_d)
            delta = a1 - a0
            if abs(delta) < 1e-6:
                return
            self._apply_rotate(gate, delta, cx, cy)
            self._log(f"Rotated '{gate.name}' by {np.degrees(delta):.1f}°")

        else:
            self._apply_resize(gate, dtype, event.xdata, event.ydata)
            self._log(f"Resized gate '{gate.name}'")

        # Re-select gate with updated handles
        self._clear_handles()
        self._refresh_plot()
        self._handle_selected_gate = gate
        self._draw_handles(gate)

    # ── Move helpers ──
    def _apply_move(self, gate, dx, dy):
        if isinstance(gate, ThresholdGate):
            gate.threshold += dx  # only horizontal movement
        elif isinstance(gate, PolygonGate):
            gate.vertices = [(vx + dx, vy + dy) for vx, vy in gate.vertices]
        elif isinstance(gate, RectangleGate):
            gate.x_min += dx; gate.x_max += dx
            gate.y_min += dy; gate.y_max += dy
        elif isinstance(gate, EllipseGate):
            gate.center_x += dx; gate.center_y += dy
        elif isinstance(gate, QuadrantGate):
            gate.mid_x += dx; gate.mid_y += dy

    def _preview_move(self, gate, dx, dy):
        if isinstance(gate, ThresholdGate):
            v = self.ax_main.axvline(gate.threshold + dx, color=gate.color,
                                     lw=2, ls="--", alpha=0.7)
            self._temp_artists.append(v)
        elif isinstance(gate, PolygonGate):
            shifted = [(vx + dx, vy + dy) for vx, vy in gate.vertices]
            p = PolyPatch(shifted, closed=True, fill=False,
                          edgecolor=gate.color, lw=1.5, ls="--")
            self.ax_main.add_patch(p); self._temp_artists.append(p)
        elif isinstance(gate, RectangleGate):
            r = RectPatch((gate.x_min + dx, gate.y_min + dy),
                          gate.x_max - gate.x_min, gate.y_max - gate.y_min,
                          fill=False, edgecolor=gate.color, lw=1.5, ls="--")
            self.ax_main.add_patch(r); self._temp_artists.append(r)
        elif isinstance(gate, EllipseGate):
            e = EllipsePatch((gate.center_x + dx, gate.center_y + dy),
                             2*gate.semi_x, 2*gate.semi_y,
                             angle=np.degrees(gate.angle),
                             fill=False, edgecolor=gate.color, lw=1.5, ls="--")
            self.ax_main.add_patch(e); self._temp_artists.append(e)
        elif isinstance(gate, QuadrantGate):
            v = self.ax_main.axvline(gate.mid_x+dx, color=gate.color, lw=1.5, ls="--", alpha=0.6)
            h = self.ax_main.axhline(gate.mid_y+dy, color=gate.color, lw=1.5, ls="--", alpha=0.6)
            self._temp_artists.extend([v, h])

    # ── Rotate helpers ──
    def _apply_rotate(self, gate, delta, cx, cy, ax=None):
        if ax is None:
            ax = self.ax_main
        if isinstance(gate, PolygonGate):
            # Rotate polygon vertices in display space so it looks correct
            # even when x and y axes have very different scales.
            self._rotate_vertices_visual(gate, delta, cx, cy, ax)
        elif isinstance(gate, RectangleGate):
            corners = gate.vertices
            # Promote to polygon, then rotate in display space
            new_gate = PolygonGate(
                name=gate.name, x_channel=gate.x_channel,
                y_channel=gate.y_channel, vertices=list(corners),
                color=gate.color, uid=gate.uid,
                parent_gate_uid=gate.parent_gate_uid,
            )
            self._rotate_vertices_visual(new_gate, delta, cx, cy, ax)
            idx = next((i for i, g in enumerate(self.gate_mgr.gates)
                        if g.uid == gate.uid), None)
            if idx is not None:
                self.gate_mgr.gates[idx] = new_gate
                self._handle_selected_gate = new_gate
        elif isinstance(gate, EllipseGate):
            self._rotate_ellipse_visual(gate, delta, ax)

    def _rotate_vertices_visual(self, gate, delta, cx, cy, ax):
        """Rotate polygon vertices by *delta* radians in display (pixel) space."""
        trans = ax.transData
        inv = trans.inverted()
        cx_d, cy_d = trans.transform((cx, cy))
        cos_d, sin_d = np.cos(delta), np.sin(delta)
        new_verts = []
        for vx, vy in gate.vertices:
            dx_d, dy_d = trans.transform((vx, vy))
            rx = dx_d - cx_d
            ry = dy_d - cy_d
            nx = cx_d + rx * cos_d - ry * sin_d
            ny = cy_d + rx * sin_d + ry * cos_d
            nvx, nvy = inv.transform((nx, ny))
            new_verts.append((nvx, nvy))
        gate.vertices = new_verts

    def _rotate_ellipse_visual(self, gate, delta, ax):
        """Rotate an EllipseGate by *delta* radians in display (pixel) space.

        This converts the semi-axis endpoints to pixel coordinates, rotates
        them visually, then converts back — so the rotation always looks
        correct on screen regardless of the axis aspect ratio.
        """
        trans = ax.transData
        inv = trans.inverted()

        # Center in display coords
        cx_d, cy_d = trans.transform((gate.center_x, gate.center_y))

        # Semi-axis endpoints in data coords
        cos_a = np.cos(gate.angle)
        sin_a = np.sin(gate.angle)
        px_data = (gate.center_x + gate.semi_x * cos_a,
                   gate.center_y + gate.semi_x * sin_a)
        py_data = (gate.center_x - gate.semi_y * sin_a,
                   gate.center_y + gate.semi_y * cos_a)

        # To display coords
        px_d = trans.transform(px_data)
        py_d = trans.transform(py_data)

        # Rotate in display space
        cos_d, sin_d = np.cos(delta), np.sin(delta)
        def _rot(pt):
            rx, ry = pt[0] - cx_d, pt[1] - cy_d
            return (cx_d + rx * cos_d - ry * sin_d,
                    cy_d + rx * sin_d + ry * cos_d)
        px_d_rot = _rot(px_d)
        py_d_rot = _rot(py_d)

        # Back to data coords
        px_r = inv.transform(px_d_rot)
        py_r = inv.transform(py_d_rot)

        # Reconstruct semi-axes and angle from rotated endpoints
        dx_x = px_r[0] - gate.center_x
        dy_x = px_r[1] - gate.center_y
        gate.semi_x = max(1e-6, np.sqrt(dx_x ** 2 + dy_x ** 2))

        dx_y = py_r[0] - gate.center_x
        dy_y = py_r[1] - gate.center_y
        gate.semi_y = max(1e-6, np.sqrt(dx_y ** 2 + dy_y ** 2))

        gate.angle = np.arctan2(dy_x, dx_x)

    def _preview_rotate(self, gate, delta, cx, cy):
        # Rotate preview vertices in display space for visual accuracy
        trans = self.ax_main.transData
        inv = trans.inverted()
        cx_d, cy_d = trans.transform((cx, cy))
        cos_d, sin_d = np.cos(delta), np.sin(delta)

        verts = gate.vertices
        if verts:
            rotated = []
            for vx, vy in verts:
                dx_d, dy_d = trans.transform((vx, vy))
                rx, ry = dx_d - cx_d, dy_d - cy_d
                nx = cx_d + rx * cos_d - ry * sin_d
                ny = cy_d + rx * sin_d + ry * cos_d
                nvx, nvy = inv.transform((nx, ny))
                rotated.append((nvx, nvy))
            p = PolyPatch(rotated, closed=True, fill=False,
                          edgecolor=gate.color, lw=1.5, ls="--")
            self.ax_main.add_patch(p); self._temp_artists.append(p)

        ln = self.ax_main.plot(cx, cy, "o", color=gate.color,
                               ms=4, alpha=0.5)[0]
        self._temp_artists.append(ln)

    # ── Resize helpers ──
    def _apply_resize(self, gate, htype, mx, my):
        """Resize gate by moving the handle edge/corner to (mx, my)."""
        xmin, xmax, ymin, ymax = self._gate_bbox(gate)
        # Compute new bbox based on which handle was dragged
        nxmin, nxmax, nymin, nymax = xmin, xmax, ymin, ymax
        if "l" in htype:
            nxmin = mx
        if "r" in htype:
            nxmax = mx
        if "b" in htype:
            nymin = my
        if "t" in htype:
            nymax = my
        # Edge-only handles
        if htype == "t":
            nymax = my
        elif htype == "b":
            nymin = my
        elif htype == "l":
            nxmin = mx
        elif htype == "r":
            nxmax = mx

        # Ensure min < max
        if nxmin > nxmax:
            nxmin, nxmax = nxmax, nxmin
        if nymin > nymax:
            nymin, nymax = nymax, nymin

        # Compute scale factors relative to old bbox
        old_w = xmax - xmin or 1e-12
        old_h = ymax - ymin or 1e-12
        new_w = nxmax - nxmin or 1e-12
        new_h = nymax - nymin or 1e-12
        sx = new_w / old_w
        sy = new_h / old_h

        # Compute new center
        new_cx = (nxmin + nxmax) / 2
        new_cy = (nymin + nymax) / 2
        old_cx = (xmin + xmax) / 2
        old_cy = (ymin + ymax) / 2

        if isinstance(gate, PolygonGate):
            gate.vertices = [
                (new_cx + (vx - old_cx) * sx, new_cy + (vy - old_cy) * sy)
                for vx, vy in gate.vertices
            ]
        elif isinstance(gate, RectangleGate):
            gate.x_min = nxmin; gate.x_max = nxmax
            gate.y_min = nymin; gate.y_max = nymax
        elif isinstance(gate, EllipseGate):
            gate.center_x = new_cx; gate.center_y = new_cy
            gate.semi_x *= sx; gate.semi_y *= sy
        elif isinstance(gate, QuadrantGate):
            gate.mid_x = new_cx; gate.mid_y = new_cy

    def _preview_resize(self, gate, htype, mx, my):
        """Draw preview of resized gate."""
        xmin, xmax, ymin, ymax = self._gate_bbox(gate)
        nxmin, nxmax, nymin, nymax = xmin, xmax, ymin, ymax
        if "l" in htype:
            nxmin = mx
        if "r" in htype:
            nxmax = mx
        if "b" in htype:
            nymin = my
        if "t" in htype:
            nymax = my
        if htype == "t":
            nymax = my
        elif htype == "b":
            nymin = my
        elif htype == "l":
            nxmin = mx
        elif htype == "r":
            nxmax = mx

        if nxmin > nxmax:
            nxmin, nxmax = nxmax, nxmin
        if nymin > nymax:
            nymin, nymax = nymax, nymin

        # Draw preview bounding box
        r = RectPatch((nxmin, nymin), nxmax - nxmin, nymax - nymin,
                      fill=False, edgecolor=gate.color, lw=1.5, ls="--")
        self.ax_main.add_patch(r)
        self._temp_artists.append(r)

    # -- Quadrant ----------------------------------------------------------
    def _quad_click(self, event):
        """Place crosshair and show popup to pick which quadrant."""
        mx, my = event.xdata, event.ydata

        # Draw preview crosshair
        self._clear_temp()
        ln_v = self.ax_main.axvline(mx, color="red", lw=1.5, ls="--", alpha=0.7)
        ln_h = self.ax_main.axhline(my, color="red", lw=1.5, ls="--", alpha=0.7)
        self._temp_artists.extend([ln_v, ln_h])
        self.fig.canvas.draw_idle()

        # Show popup to select quadrant
        options = [
            "Q1 — upper-right  (x≥mid, y≥mid)",
            "Q2 — upper-left   (x<mid, y≥mid)",
            "Q3 — lower-left   (x<mid, y<mid)",
            "Q4 — lower-right  (x≥mid, y<mid)",
        ]

        def on_pick(idx):
            quadrant = ["Q1", "Q2", "Q3", "Q4"][idx]
            self._clear_temp()
            self._push_undo()
            xi, yi, xn, yn = self._current_xy()
            n = len(self.gate_mgr.gates) + 1
            gate = self.gate_mgr.add_quadrant_gate(
                name=f"Quad{n}-{quadrant}",
                x_channel=xn,
                y_channel=yn,
                mid_x=mx,
                mid_y=my,
                quadrant=quadrant,
                parent_gate_uid=self._selected_parent_uid,
            )
            self._refresh_plot()
            self._print_gate_created(gate)
            self._open_gate_window(gate)

        self._show_popup_list("Select Quadrant", options, -1, on_pick)

    def _thresh_click(self, event):
        """Place a threshold line and choose left/right gating (1D)."""
        tx_display = event.xdata
        if self._view_mode != "1D":
            self._view_mode = "1D"
            self.btn_viewmode.label.set_text("View: 1D Histogram")
            self._update_compress_hint()
            self._refresh_plot()

        # Convert display → original data space when compression active
        pw = self._get_pw_params(np.array([]))
        if pw is not None:
            dmin, dmax, anchor, frac = pw
            tx = self._pw_inverse(tx_display, dmin, dmax, anchor, frac)
        else:
            tx = tx_display

        self._clear_temp()
        ln = self.ax_main.axvline(tx_display, color="red", lw=2, ls="--", alpha=0.8)
        self._temp_artists.append(ln)
        self.fig.canvas.draw_idle()

        options = [
            f"Left  (x < {tx:.1f})",
            f"Right (x ≥ {tx:.1f})",
        ]

        def on_pick(idx):
            side = "left" if idx == 0 else "right"
            self._clear_temp()
            self._push_undo()
            xi, yi, xn, yn = self._current_xy()
            n = len(self.gate_mgr.gates) + 1
            gate = self.gate_mgr.add_threshold_gate(
                name=f"T{n}-{side[0].upper()}",
                x_channel=xn,
                y_channel=yn,
                threshold=tx,
                side=side,
                parent_gate_uid=self._selected_parent_uid,
            )
            self._refresh_plot()
            self._print_gate_created(gate)
            self._open_gate_window(gate)

        self._show_popup_list("Select side of threshold", options, -1, on_pick)

    def _print_gate_created(self, gate):
        logger.debug("Created gate '%s' with parent_gate_uid=%s",
                     gate.name, gate.parent_gate_uid)
        logger.debug("  _selected_parent_uid was: %s", self._selected_parent_uid)

        stats = self.gate_mgr.compute_stats(
            self.fcs.data, self.fcs.channel_names
        )
        for s in stats:
            if s["uid"] == gate.uid:
                parent_info = ""
                if gate.parent_gate_uid:
                    parent = next((g for g in self.gate_mgr.gates if g.uid == gate.parent_gate_uid), None)
                    if parent:
                        parent_info = f" (child of {parent.name})"
                self._log(f"Gate '{gate.name}'{parent_info}: {s['count']:,} events ({s['percent']:.1f}%)")

    # ================================================================ #
    #  Actions
    # ================================================================ #
    def _on_remove_gate(self):
        """Show a popup list of gates; click one to remove it."""
        if not self.gate_mgr.gates:
            self._log("No gates to remove")
            return

        # Build display list with channel info and event counts
        stats = self.gate_mgr.compute_stats(
            self.fcs.data, self.fcs.channel_names
        ) if self.fcs else []
        stat_map = {s["uid"]: s for s in stats}

        items = []
        for g in self.gate_mgr.gates:
            s = stat_map.get(g.uid)
            count_str = f"  ({s['count']:,} events, {s['percent']:.1f}%)" if s else ""
            parent_str = ""
            if g.parent_gate_uid:
                pg = next((p for p in self.gate_mgr.gates
                           if p.uid == g.parent_gate_uid), None)
                if pg:
                    parent_str = f"  [child of {pg.name}]"
            items.append(f"{g.name}  ({g.x_channel} vs {g.y_channel}){count_str}{parent_str}")

        def on_pick(idx):
            self._push_undo()
            gate = self.gate_mgr.gates[idx]
            name = gate.name
            uid = gate.uid

            # Also remove any child gates whose parent is this gate
            children = [g for g in self.gate_mgr.gates
                        if g.parent_gate_uid == uid]
            self.gate_mgr.remove_gate(uid)
            for child in children:
                self.gate_mgr.remove_gate(child.uid)

            # Close corresponding gate windows
            self._close_gate_window(uid)
            for child in children:
                self._close_gate_window(child.uid)

            # If we removed the selected parent, clear parent selection
            if self._selected_parent_uid == uid:
                self._selected_parent_uid = None

            child_msg = f" (and {len(children)} child gate(s))" if children else ""
            self._log(f"Removed gate '{name}'{child_msg}")
            self._refresh_plot()

        self._show_popup_list("Remove Gate (click to delete)", items, -1, on_pick)

    def _on_clear_gates(self):
        self._push_undo()
        # Close all gate sub-windows
        for uid in list(self._gate_windows.keys()):
            self._close_gate_window(uid)
        self.gate_mgr.clear()
        self._selected_parent_uid = None
        self._refresh_plot()
        self._log("All gates cleared")

    def _on_save_gates(self):
        """Persist the current gating strategy to a sidecar JSON next to the FCS file."""
        if self.fcs is None:
            self._log("No file loaded")
            return
        if not self.gate_mgr.gates:
            self._log("No gates to save")
            return
        try:
            out = gate_io.save_gates(self.fcs.filepath, self.gate_mgr)
            self._log(f"Saved {len(self.gate_mgr.gates)} gate(s) → {os.path.basename(out)}")
        except Exception as exc:
            self._log(f"Save gates failed: {exc}")
            logger.exception("save_gates failed")

    def _on_load_gates(self):
        """Replace current gates with the strategy stored next to the FCS file."""
        if self.fcs is None:
            self._log("No file loaded")
            return
        sidecar = gate_io.sidecar_path(self.fcs.filepath)
        if not os.path.exists(sidecar):
            self._log(f"No saved strategy at {os.path.basename(sidecar)}")
            return
        self._push_undo()
        # Drop all open sub-windows; we're about to replace the underlying gate list.
        for uid in list(self._gate_windows.keys()):
            self._close_gate_window(uid)
        try:
            result = gate_io.load_gates(
                self.fcs.filepath, self.gate_mgr,
                replace=True,
                available_channels=list(self.fcs.channel_names),
            )
        except Exception as exc:
            self._log(f"Load gates failed: {exc}")
            logger.exception("load_gates failed")
            return
        self._selected_parent_uid = None
        self._refresh_plot()
        n = result.get("loaded", 0)
        self._log(f"Loaded {n} gate(s) from {os.path.basename(sidecar)}")
        for skipped in result.get("skipped") or []:
            self._log(f"  skipped malformed entry: {skipped}")
        missing = result.get("missing_channels") or []
        if missing:
            self._log(
                f"  warning: {len(missing)} gate(s) reference channels not in this "
                f"FCS file ({', '.join(missing[:5])}{'…' if len(missing) > 5 else ''})"
            )

    def _on_show_summary(self):
        if self.fcs is None:
            return

        # Close any existing summary windows
        for fignum in plt.get_fignums():
            fig = plt.figure(fignum)
            if fig.canvas.manager.get_window_title() == "FlowCyt Summary":
                plt.close(fig)

        stats = self.gate_mgr.compute_stats(
            self.fcs.data, self.fcs.channel_names
        )
        xi, yi, xn, yn = self._current_xy()

        fig, axes = plt.subplots(2, 2, figsize=(10, 8))
        fig.canvas.manager.set_window_title("FlowCyt Summary")
        fig.suptitle("FlowCyt — Summary", fontsize=13)

        summary_bar_chart(axes[0, 0], stats)

        summary_histogram(
            axes[0, 1], self.fcs.data, xi,
            self.fcs.display_names()[xi],
            self.gate_mgr.gates, self.fcs.channel_names,
        )
        summary_histogram(
            axes[1, 0], self.fcs.data, yi,
            self.fcs.display_names()[yi],
            self.gate_mgr.gates, self.fcs.channel_names,
        )

        axes[1, 1].axis("off")
        if stats:
            cell_text = [
                [s["name"], f"{s['count']:,}", f"{s['percent']:.1f}%"]
                for s in stats
            ]
            table = axes[1, 1].table(
                cellText=cell_text,
                colLabels=["Gate", "Count", "% Parent" if any(s.get("parent_uid") for s in stats) else "% Total"],
                loc="center", cellLoc="center",
            )
            table.auto_set_font_size(False)
            table.set_fontsize(10)
            table.scale(1, 1.6)
            axes[1, 1].set_title("Gate Statistics", fontsize=11)
        else:
            axes[1, 1].text(
                0.5, 0.5, "No gates defined",
                ha="center", va="center", fontsize=12, color="grey",
            )

        fig.tight_layout()
        plt.show(block=False)

    @staticmethod
    def _save_axes_to_file(fig, ax, filepath: str, dpi: int = 150):
        """Save just an axes (with labels, title, ticks) to a file.

        Calls ``fig.canvas.draw()`` first so ``get_renderer()`` is
        guaranteed to return a usable renderer — without this some
        backends (notably QtAgg on Windows) hand back ``None`` and
        ``savefig`` ends up writing an empty / nonsense file with the
        requested filename but no image data inside.

        On any failure to compute the tight bbox we fall back to
        ``bbox_inches="tight"`` so the user still gets a saved figure
        rather than a silent error.
        """
        try:
            fig.canvas.draw()
            renderer = fig.canvas.get_renderer()
            bbox = ax.get_tightbbox(renderer)
            if bbox is None:
                bbox = ax.get_window_extent(renderer)
            bbox_inches = bbox.transformed(fig.dpi_scale_trans.inverted())
            bbox_inches = bbox_inches.expanded(1.05, 1.05)
            fig.savefig(
                filepath, dpi=dpi, bbox_inches=bbox_inches,
                facecolor="white", edgecolor="none",
            )
        except Exception:
            # Fall back to a whole-figure tight save — always produces a
            # valid file even if the per-axes bbox path failed.
            fig.savefig(
                filepath, dpi=dpi, bbox_inches="tight",
                facecolor="white", edgecolor="none",
            )

    @staticmethod
    def _ask_save_path(default_name: str, filetypes: list[tuple[str, str]],
                       default_ext: str, title: str = "Save") -> str | None:
        """Open a native Save As dialog and return the chosen path.

        Preference order:

        1. **Qt ``QFileDialog``** when the matplotlib backend is Qt-based
           (the user's setup on macOS).  On macOS this delegates to
           ``NSSavePanel`` and is the real system Save As dialog —
           full keyboard support (Cmd+A, Cmd+Shift+←, etc.), correct
           handling of long filenames, drive letters on Windows, and a
           proper file-type picker.
        2. **tkinter ``filedialog``** as a portable fallback for
           non-Qt backends (TkAgg or otherwise).
        3. **Home-directory autosave** as the last resort if no GUI
           toolkit is available at all.

        Returns ``None`` if the user cancels.
        """
        # 1. Qt QFileDialog — works correctly on Qt backends on every OS.
        backend = matplotlib.get_backend().lower()
        if "qt" in backend:
            try:
                from matplotlib.backends.qt_compat import QtWidgets
                # Qt expects filter strings like "PNG image (*.png);;PDF (*.pdf)".
                filt = ";;".join(f"{name} ({pat})" for name, pat in filetypes)
                # Reuse the existing QApplication created by the matplotlib
                # backend rather than constructing a second one.
                app_qt = QtWidgets.QApplication.instance()
                if app_qt is None:
                    app_qt = QtWidgets.QApplication([])
                path, _ = QtWidgets.QFileDialog.getSaveFileName(
                    None, title, default_name, filt,
                )
                return path or None
            except Exception:
                pass  # fall through to Tk

        # 2. Tk filedialog — portable fallback.
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            try:
                root.attributes("-topmost", True)
            except Exception:
                pass
            path = filedialog.asksaveasfilename(
                title=title,
                initialfile=default_name,
                defaultextension=default_ext,
                filetypes=filetypes,
            )
            try:
                root.destroy()
            except Exception:
                pass
            return path or None
        except Exception:
            # 3. No usable GUI toolkit — just dump into the user's home dir.
            from pathlib import Path
            return str(Path.home() / default_name)

    def _on_save_plot(self):
        """Save the main plot to an image file via a native Save dialog."""
        if self.fcs is None:
            self._log("No file loaded")
            return

        base = (Path(self._fcs_files[self._fcs_file_idx]).stem
                if self._fcs_files and self._fcs_file_idx >= 0 else "plot")
        xi, yi, xn, yn = self._current_xy()
        suffix = f"_{xn}_vs_{yn}" if self._view_mode == "2D" else f"_{xn}_1D"
        safe = lambda s: (
            s.replace("/", "-").replace("\\", "-").replace(" ", "_")
             .replace(":", "-").replace("*", "").replace("?", "")
             .replace("\"", "").replace("<", "").replace(">", "").replace("|", "")
        )
        default_name = f"{safe(base)}{safe(suffix)}.png"

        path = self._ask_save_path(
            default_name=default_name,
            filetypes=[("PNG image", "*.png"),
                       ("PDF document", "*.pdf"),
                       ("SVG vector", "*.svg"),
                       ("All files", "*.*")],
            default_ext=".png",
            title="Save Plot",
        )
        if not path:
            self._log("Save cancelled")
            return

        fpath = Path(path)
        if not fpath.suffix:
            fpath = fpath.with_suffix(".png")
        try:
            self._save_axes_to_file(self.fig, self.ax_main, str(fpath))
            self._log(f"Plot saved to {fpath}")
        except Exception as exc:
            self._log(f"Save error: {exc}")

    def _on_export_csv(self):
        """Export gated events to CSV - saves directly to workspace folder."""
        if self.fcs is None:
            self._log("ERROR: No file loaded")
            return
        stats = self.gate_mgr.compute_stats(
            self.fcs.data, self.fcs.channel_names
        )
        if not stats:
            self._log("ERROR: Define at least one gate first")
            return

        self._log("═══ CSV EXPORT START ═══")

        # Generate output filename in workspace folder
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = Path(self.fcs.filepath).stem
        filename = f"{base}_gated_{timestamp}.csv"

        # Save to workspace folder (where user can see it)
        workspace_path = Path(__file__).parent.parent / filename
        outpath = str(workspace_path)

        self._log(f"Output file: {filename}")
        self._log(f"Location: workspace folder")

        try:
            self._log(f"[1/2] Writing CSV file...")
            with open(outpath, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["gate", "event_idx"] + self.fcs.channel_names)

                total_rows = 0
                for gate_idx, gate in enumerate(self.gate_mgr.gates, 1):
                    try:
                        xi = self.fcs.channel_names.index(gate.x_channel)
                        yi = self.fcs.channel_names.index(gate.y_channel)
                    except ValueError:
                        self._log(f"⚠ Skipping {gate.name}: channels not found")
                        continue

                    mask = gate.contains(self.fcs.data[:, xi], self.fcs.data[:, yi])
                    gate_count = 0
                    for idx in np.where(mask)[0]:
                        writer.writerow(
                            [gate.name, int(idx)] + self.fcs.data[idx].tolist()
                        )
                        total_rows += 1
                        gate_count += 1

                    self._log(f"  → {gate.name}: {gate_count:,} events")

            self._log(f"[2/2] File saved successfully!")
            self._log(f"✓ Exported {total_rows:,} total events from {len(self.gate_mgr.gates)} gates")
            self._log(f"═══ CSV EXPORT COMPLETE ═══")
            self._log(f"📄 File: {filename}")
        except Exception as e:
            self._log(f"✗ ERROR: Export failed: {e}")
            logger.exception("Export failed")

    # ================================================================ #
    #  Gate sub-windows
    # ================================================================ #
    def _open_gate_window(self, gate: Gate):
        """Open an independent window showing only events inside *gate*."""
        if self.fcs is None:
            return
        # Close existing window for this gate if any
        if gate.uid in self._gate_windows:
            try:
                plt.close(self._gate_windows[gate.uid].fig)
            except Exception:
                pass

        gw = GateWindow(self, gate)
        self._gate_windows[gate.uid] = gw
        self._log(f"Opened window for gate '{gate.name}'")

    def _close_gate_window(self, uid: str):
        """Close and clean up a gate sub-window by gate UID."""
        gw = self._gate_windows.pop(uid, None)
        if gw is not None:
            try:
                plt.close(gw.fig)
            except Exception:
                pass

    def _refresh_gate_windows(self):
        """Refresh all open gate sub-windows (e.g. after file change)."""
        dead = []
        for uid, gw in self._gate_windows.items():
            try:
                if not plt.fignum_exists(gw.fig.number):
                    dead.append(uid)
                    continue
                gw._update_labels()
                gw._refresh()
            except Exception:
                dead.append(uid)
        for uid in dead:
            del self._gate_windows[uid]

    # ================================================================ #
    #  Public API for the chat assistant / programmatic control
    # ================================================================ #
    def refresh_plot(self):
        """Redraw the main plot.  Safe to call from any context."""
        # Keep the parent-gate radio in sync after gate-list mutations.
        self._refresh_parent_selector()
        self._refresh_plot()

    def _resolve_channel_idx(self, name_or_idx) -> int:
        """Resolve a fluorophore short name, protein marker, or integer
        index to a column index in ``self.fcs.data``.
        """
        if self.fcs is None:
            raise ValueError("No FCS file loaded.")
        if isinstance(name_or_idx, int):
            return max(0, min(name_or_idx, len(self.fcs.channel_names) - 1))
        target = str(name_or_idx).strip()
        if not target:
            raise ValueError("Channel name is empty.")
        # 1. Exact fluorophore short-name match.
        for idx, short in enumerate(self.fcs.channel_names):
            if short == target:
                return idx
        # 2. Exact marker match (case-insensitive).
        t_lower = target.lower()
        for short, marker in (self._marker_map or {}).items():
            if marker and marker.lower() == t_lower and short in self.fcs.channel_names:
                return self.fcs.channel_names.index(short)
        # 3. Match against the effective display label.
        for idx, label in enumerate(self._channel_display_names):
            if label == target:
                return idx
        # 4. Case-insensitive fluorophore.
        for idx, short in enumerate(self.fcs.channel_names):
            if short.lower() == t_lower:
                return idx
        raise ValueError(f"Channel '{name_or_idx}' not found.")

    def set_x_channel(self, name_or_idx):
        idx = self._resolve_channel_idx(name_or_idx)
        self._x_idx = idx
        self._update_channel_labels()
        self._refresh_plot()
        return idx

    def set_y_channel(self, name_or_idx):
        idx = self._resolve_channel_idx(name_or_idx)
        self._y_idx = idx
        self._update_channel_labels()
        self._refresh_plot()
        return idx

    def set_parent_gate_by_name(self, name):
        """Set the parent-gate selection programmatically."""
        if name in (None, "", "None", "null"):
            self._selected_parent_uid = None
        else:
            g = self.find_gate_by_name(name)
            if g is None:
                raise ValueError(f"Gate '{name}' not found.")
            self._selected_parent_uid = g.uid
        self._refresh_parent_selector()
        self._refresh_plot()

    def set_axis_scale(self, axis: str, scale: str):
        """Set X or Y axis scale ('linear' or 'log')."""
        axis = axis.lower()
        scale = scale.lower()
        if axis not in {"x", "y"} or scale not in {"linear", "log"}:
            raise ValueError("axis must be x/y and scale linear/log")
        current = self._x_scale if axis == "x" else self._y_scale
        if current == scale:
            return
        # Re-use the toggle path so labels stay in sync.
        self._toggle_scale(axis)

    def find_gate_by_name(self, name: str):
        for g in self.gate_mgr.gates:
            if g.name == name:
                return g
        return None

    def get_channel_range(self, name_or_idx) -> tuple[float, float]:
        if self.fcs is None:
            raise ValueError("No FCS file loaded.")
        idx = self._resolve_channel_idx(name_or_idx)
        col = self.fcs.data[:, idx]
        return float(np.min(col)), float(np.max(col))

    def export_csv(self, filepath: str | None = None) -> str:
        """Programmatic CSV export used by the chat tool. Returns the path."""
        if self.fcs is None:
            raise ValueError("No FCS file loaded.")
        if not self.gate_mgr.gates:
            raise ValueError("Define at least one gate first.")
        if filepath is None:
            from datetime import datetime
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            base = Path(self.fcs.filepath).stem
            filepath = str(
                Path(__file__).parent.parent / f"{base}_gated_{timestamp}.csv"
            )
        with open(filepath, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["gate", "event_idx"] + self.fcs.channel_names)
            for gate in self.gate_mgr.gates:
                try:
                    xi = self.fcs.channel_names.index(gate.x_channel)
                    yi = self.fcs.channel_names.index(gate.y_channel)
                except ValueError:
                    continue
                mask = gate.contains(self.fcs.data[:, xi], self.fcs.data[:, yi])
                for idx in np.where(mask)[0]:
                    writer.writerow(
                        [gate.name, int(idx)] + self.fcs.data[idx].tolist()
                    )
        self._log(f"Exported gated events → {os.path.basename(filepath)}")
        return filepath

    # ================================================================ #
    #  Chat / Markers windows (opened from the right-panel buttons)
    # ================================================================ #
    def _on_open_chat(self):
        """Open (or focus) the DeepSeek chat assistant window."""
        from .chat_window import ChatWindow
        if self._chat_window is not None:
            try:
                if plt.fignum_exists(self._chat_window.fig.number):
                    try:
                        self._chat_window.fig.canvas.manager.show()
                    except Exception:
                        pass
                    return
            except Exception:
                pass
        self._chat_window = ChatWindow(self)

    def _on_open_markers(self):
        """Open (or focus) the fluorophore → marker mapping editor."""
        from .markers_window import MarkersWindow
        if self._markers_window is not None:
            try:
                if plt.fignum_exists(self._markers_window.fig.number):
                    try:
                        self._markers_window.fig.canvas.manager.show()
                    except Exception:
                        pass
                    return
            except Exception:
                pass
        self._markers_window = MarkersWindow(self)

    # ================================================================ #
    #  Run
    # ================================================================ #
    def run(self):
        plt.show()


# ================================================================== #
#  Independent gate sub-window
# ================================================================== #

class GateWindow:
    """A secondary matplotlib figure that displays only events
    inside a specific gate, with its own independent X/Y channel
    selectors and full gating tools to create child gates.
    """

    HANDLE_RADIUS_PX = 10

    def __init__(self, app: FlowCytApp, gate: Gate, parent_window: "GateWindow | None" = None):
        self.app = app
        self.gate = gate
        self.parent_window = parent_window  # The GateWindow that spawned this one (or None if from main)

        # Own channel indices (start with same as main)
        self._x_idx = app._x_idx
        self._y_idx = app._y_idx

        # Interaction state
        self._mode: str = MODE_NAV
        self._poly_verts: list[tuple[float, float]] = []
        self._poly_last_click_time: float = 0.0
        self._rect_origin: tuple[float, float] | None = None
        self._ellipse_origin: tuple[float, float] | None = None
        self._temp_artists: list = []

        # Handle-based gate editing
        self._handle_selected_gate: Gate | None = None
        self._handle_positions: list = []
        self._handle_artists: list = []
        self._handle_drag_type: str | None = None
        self._handle_drag_start: tuple[float, float] | None = None

        # Child gate windows {gate_uid: GateWindow}
        self._child_windows: dict[str, "GateWindow"] = {}

        self.fig = plt.figure(f"Gate: {gate.name}", figsize=(10, 8))
        self.fig.subplots_adjust(left=0.08, right=0.72, top=0.93, bottom=0.08)

        # Main scatter axes
        self.ax = self.fig.add_axes([0.08, 0.10, 0.58, 0.80])

        # Controls on the right side
        rs = 0.70
        cw = 0.27
        btn_h = 0.035
        btn_gap = 0.004     # matches the main-window layout constant
        small = 0.05
        lw = cw - 2 * small

        y_cur = 0.95

        # X channel
        self.fig.text(rs, y_cur, "X Channel", fontsize=8, fontweight="bold")
        y_cur -= btn_h + btn_gap
        ax_xp = self.fig.add_axes([rs, y_cur, small, btn_h])
        self._btn_xp = Button(ax_xp, "<")
        self._btn_xp.on_clicked(lambda e: self._cycle("x", -1))
        ax_xl = self.fig.add_axes([rs + small, y_cur, lw, btn_h])
        self._btn_xl = Button(ax_xl, "")
        self._btn_xl.on_clicked(lambda e: self._show_ch_popup("x"))
        ax_xn = self.fig.add_axes([rs + small + lw, y_cur, small, btn_h])
        self._btn_xn = Button(ax_xn, ">")
        self._btn_xn.on_clicked(lambda e: self._cycle("x", +1))

        # Y channel
        y_cur -= btn_h + btn_gap
        self.fig.text(rs, y_cur + btn_h, "Y Channel", fontsize=8, fontweight="bold")
        y_cur -= 0.01
        ax_yp = self.fig.add_axes([rs, y_cur, small, btn_h])
        self._btn_yp = Button(ax_yp, "<")
        self._btn_yp.on_clicked(lambda e: self._cycle("y", -1))
        ax_yl = self.fig.add_axes([rs + small, y_cur, lw, btn_h])
        self._btn_yl = Button(ax_yl, "")
        self._btn_yl.on_clicked(lambda e: self._show_ch_popup("y"))
        ax_yn = self.fig.add_axes([rs + small + lw, y_cur, small, btn_h])
        self._btn_yn = Button(ax_yn, ">")
        self._btn_yn.on_clicked(lambda e: self._cycle("y", +1))

        # Scale buttons
        self._x_scale = "linear"
        self._y_scale = "linear"
        half = cw / 2 - 0.005
        y_cur -= btn_h + 0.01
        ax_xs = self.fig.add_axes([rs, y_cur, half, btn_h])
        self._btn_xs = Button(ax_xs, "X: Lin")
        self._btn_xs.on_clicked(lambda e: self._toggle_scale("x"))
        ax_ys = self.fig.add_axes([rs + half + 0.01, y_cur, half, btn_h])
        self._btn_ys = Button(ax_ys, "Y: Lin")
        self._btn_ys.on_clicked(lambda e: self._toggle_scale("y"))

        # View mode toggle
        self._view_mode = "2D"
        # 1D axis compression state
        self._compress_anchor: float | None = None
        self._compress_frac: float | None = None
        self._compress_dmin: float = 0.0
        self._compress_dmax: float = 1.0
        self._compress_dragging: bool = False
        self._compress_drag_px: float = 0.0
        self._compress_base_frac: float = 0.5
        y_cur -= btn_h + btn_gap
        ax_vm = self.fig.add_axes([rs, y_cur, cw, btn_h])
        self._btn_vm = Button(ax_vm, "View: 2D Scatter")
        self._btn_vm.on_clicked(lambda e: self._toggle_view())

        # Compression hint (below plot, visible in 1D Navigate)
        self._ax_compress_hint = self.fig.add_axes([0.08, 0.04, 0.58, 0.03])
        self._ax_compress_hint.set_xticks([])
        self._ax_compress_hint.set_yticks([])
        self._ax_compress_hint.set_frame_on(False)
        self._ax_compress_hint.text(
            0.5, 0.5,
            "Navigate: click & drag to compress  •  double-click to reset",
            ha="center", va="center", fontsize=6, color="grey",
            transform=self._ax_compress_hint.transAxes,
        )
        self._ax_compress_hint.set_visible(False)

        # Stretch mode state
        self._stretch_selected_gate: Gate | None = None
        self._stretch_points: list[tuple[float, float]] = []
        self._stretch_point_idx: int = -1
        self._stretch_point_artists: list = []

        # --- Tool selector (radio) ---
        y_cur -= 0.01
        self.fig.text(rs, y_cur, "Tool", fontsize=8, fontweight="bold")
        radio_h = 0.155
        y_cur -= radio_h
        self.ax_mode = self.fig.add_axes([rs, y_cur, cw, radio_h])
        self.radio_mode = RadioButtons(
            self.ax_mode,
            [MODE_NAV, MODE_POLY, MODE_RECT, MODE_ELLIPSE, MODE_QUAD, MODE_THRESH, MODE_TRANSLATE, MODE_ROTATE, MODE_STRETCH],
            active=0,
        )
        self.radio_mode.on_clicked(self._on_mode_change)

        # --- Action buttons ---
        y_cur -= btn_h + 0.01
        ax_rm = self.fig.add_axes([rs, y_cur, cw, btn_h])
        self._btn_remove = Button(ax_rm, "Remove Gate...")
        self._btn_remove.on_clicked(lambda e: self._on_remove_gate())

        y_cur -= btn_h + btn_gap
        ax_clr = self.fig.add_axes([rs, y_cur, cw, btn_h])
        self._btn_clear = Button(ax_clr, "Clear Child Gates")
        self._btn_clear.on_clicked(lambda e: self._on_clear_child_gates())

        y_cur -= btn_h + btn_gap
        ax_save = self.fig.add_axes([rs, y_cur, cw, btn_h])
        self._btn_save = Button(ax_save, "Save Plot...")
        self._btn_save.on_clicked(lambda e: self._on_save_plot())

        # Stats panel (fills remaining space)
        y_cur -= 0.01
        stats_h = max(y_cur - 0.03, 0.08)
        self.ax_stats = self.fig.add_axes([rs, 0.03, cw, stats_h])
        self.ax_stats.set_frame_on(True)
        self.ax_stats.set_xticks([])
        self.ax_stats.set_yticks([])

        # Connect events
        self.fig.canvas.mpl_connect("button_press_event", self._on_click)
        self.fig.canvas.mpl_connect("button_release_event", self._on_release)
        self.fig.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.fig.canvas.mpl_connect("key_press_event", self._on_key)
        self.fig.canvas.mpl_connect("close_event", self._on_close)

        FlowCytApp._disable_tk_tab_traversal(self.fig)
        install_tk_click_bridge(self.fig)

        self._update_labels()
        self._refresh()
        self.fig.show()

    # ================================================================ #
    #  Data helpers
    # ================================================================ #
    def _get_mask(self) -> np.ndarray:
        """Boolean mask for events inside the gate on the gate's own channels."""
        fcs = self.app.fcs
        if fcs is None:
            return np.array([], dtype=bool)
        try:
            xi = fcs.channel_names.index(self.gate.x_channel)
            yi = fcs.channel_names.index(self.gate.y_channel)
        except ValueError:
            return np.zeros(fcs.num_events, dtype=bool)

        mask = self.gate.contains(fcs.data[:, xi], fcs.data[:, yi])

        # Handle parent mask (walk up the hierarchy)
        if self.gate.parent_gate_uid:
            parent = next((g for g in self.app.gate_mgr.gates
                           if g.uid == self.gate.parent_gate_uid), None)
            if parent:
                try:
                    pxi = fcs.channel_names.index(parent.x_channel)
                    pyi = fcs.channel_names.index(parent.y_channel)
                    pmask = parent.contains(fcs.data[:, pxi], fcs.data[:, pyi])
                    mask = mask & pmask
                except ValueError:
                    pass
        return mask

    def _current_xy(self):
        """Return (x_idx, y_idx, x_name, y_name) for current channels."""
        fcs = self.app.fcs
        n = len(fcs.channel_names)
        xi = max(0, min(self._x_idx, n - 1))
        yi = max(0, min(self._y_idx, n - 1))
        return xi, yi, fcs.channel_names[xi], fcs.channel_names[yi]

    # ================================================================ #
    #  UI helpers
    # ================================================================ #
    def _update_labels(self):
        if self.app.fcs is None:
            return
        names = self.app.fcs.display_names()
        self._btn_xl.label.set_text(names[self._x_idx][:18])
        self._btn_xl.label.set_fontsize(7)
        self._btn_yl.label.set_text(names[self._y_idx][:18])
        self._btn_yl.label.set_fontsize(7)

    def _cycle(self, axis: str, d: int):
        if self.app.fcs is None:
            return
        n = len(self.app.fcs.channel_names)
        if axis == "x":
            self._x_idx = (self._x_idx + d) % n
        else:
            self._y_idx = (self._y_idx + d) % n
        self._update_labels()
        self._refresh()

    def _show_ch_popup(self, axis: str):
        if self.app.fcs is None:
            return
        names = self.app.fcs.display_names()
        current = self._x_idx if axis == "x" else self._y_idx

        def on_pick(idx):
            if axis == "x":
                self._x_idx = idx
            else:
                self._y_idx = idx
            self._update_labels()
            self._refresh()

        self.app._show_popup_list(
            f"Select {'X' if axis == 'x' else 'Y'} Channel ({self.gate.name})",
            names, current, on_pick,
        )

    def _toggle_scale(self, axis: str):
        if axis == "x":
            self._x_scale = "log" if self._x_scale == "linear" else "linear"
            self._btn_xs.label.set_text(f"X: {'Log' if self._x_scale == 'log' else 'Lin'}")
        else:
            self._y_scale = "log" if self._y_scale == "linear" else "linear"
            self._btn_ys.label.set_text(f"Y: {'Log' if self._y_scale == 'log' else 'Lin'}")
        self._refresh()

    def _toggle_view(self):
        if self._view_mode == "2D":
            self._view_mode = "1D"
            self._btn_vm.label.set_text("View: 1D Histogram")
            self._update_gw_compress_hint()
        else:
            self._view_mode = "2D"
            self._btn_vm.label.set_text("View: 2D Scatter")
            self._ax_compress_hint.set_visible(False)
            self._reset_gw_compression()
        self._refresh()

    def _update_gw_compress_hint(self):
        show = (self._view_mode == "1D" and self._mode == MODE_NAV)
        self._ax_compress_hint.set_visible(show)

    def _reset_gw_compression(self):
        self._compress_anchor = None
        self._compress_frac = None
        self._compress_dragging = False

    def _get_gw_pw_params(self, x):
        if self._compress_anchor is None or self._compress_frac is None:
            return None
        return (self._compress_dmin, self._compress_dmax,
                self._compress_anchor, self._compress_frac)

    def _on_mode_change(self, label):
        self._mode = label
        self._poly_verts.clear()
        self._rect_origin = None
        self._ellipse_origin = None
        self._handle_selected_gate = None
        self._handle_drag_type = None
        self._handle_drag_start = None
        self._stretch_selected_gate = None
        self._stretch_points = []
        self._stretch_point_idx = -1
        self._clear_gw_stretch_highlight()
        self._clear_handles()
        self._clear_temp()
        self._update_gw_compress_hint()

        # Show gate picker when entering Translate/Rotate mode
        if label == MODE_TRANSLATE:
            self._show_gw_gate_picker("Translate")
        elif label == MODE_ROTATE:
            self._show_gw_gate_picker("Rotate")
        elif label == MODE_STRETCH:
            self._show_gw_stretch_picker()

    def _show_gw_gate_picker(self, action: str = "Translate"):
        """Show a popup to select which child gate to translate/rotate."""
        child_gates = [g for g in self.app.gate_mgr.gates
                       if g.parent_gate_uid == self.gate.uid]
        if not child_gates:
            return
        items = [f"{g.name}  ({g.x_channel}/{g.y_channel})" for g in child_gates]

        def on_pick(idx):
            gate = child_gates[idx]
            self._handle_selected_gate = gate
            self._clear_handles()
            self._draw_handles(gate)
            self.fig.canvas.draw_idle()

        self.app._show_popup_list(f"Select Gate to {action}", items, -1, on_pick)

    def _show_gw_stretch_picker(self):
        """Show a popup to select which child gate to stretch."""
        child_gates = [g for g in self.app.gate_mgr.gates
                       if g.parent_gate_uid == self.gate.uid
                       and not isinstance(g, ThresholdGate)]
        if not child_gates:
            self.app._log(f"[{self.gate.name}] No stretchable child gates")
            return
        items = [f"{g.name}  ({g.x_channel}/{g.y_channel})" for g in child_gates]

        def on_pick(idx):
            gate = child_gates[idx]
            self._stretch_selected_gate = gate
            self._stretch_points = FlowCytApp._get_stretch_points(gate)
            self._stretch_point_idx = 0 if self._stretch_points else -1
            self._draw_gw_stretch_highlight()
            n_pts = len(self._stretch_points)
            self.app._log(f"[{self.gate.name}] Stretch '{gate.name}' — "
                          f"{n_pts} points. Tab cycles, arrows move")
            FlowCytApp._grab_canvas_focus(self.fig)
            self.fig.canvas.draw_idle()

        self.app._show_popup_list("Select Gate to Stretch", items, -1, on_pick)

    def _draw_gw_stretch_highlight(self):
        self._clear_gw_stretch_highlight()
        if (self._stretch_selected_gate is None or
                self._stretch_point_idx < 0 or
                self._stretch_point_idx >= len(self._stretch_points)):
            return
        for i, (px, py) in enumerate(self._stretch_points):
            if i == self._stretch_point_idx:
                marker = self.ax.plot(
                    px, py, "o", color="#ff4400", markersize=12,
                    markeredgecolor="black", markeredgewidth=2,
                    markerfacecolor="none", zorder=101,
                )[0]
            else:
                marker = self.ax.plot(
                    px, py, "o", color="#888888", markersize=6,
                    markeredgecolor="black", markeredgewidth=1,
                    zorder=100,
                )[0]
            self._stretch_point_artists.append(marker)

    def _clear_gw_stretch_highlight(self):
        for a in getattr(self, '_stretch_point_artists', []):
            try:
                a.remove()
            except Exception:
                pass
        self._stretch_point_artists = []

    def _clear_temp(self):
        for a in self._temp_artists:
            try:
                a.remove()
            except Exception:
                pass
        self._temp_artists.clear()

    # ================================================================ #
    #  Refresh / drawing
    # ================================================================ #
    def _refresh(self):
        fcs = self.app.fcs
        if fcs is None:
            return

        mask = self._get_mask()
        gated_data = fcs.data[mask]

        xi = self._x_idx
        yi = self._y_idx
        n = len(fcs.channel_names)
        xi = max(0, min(xi, n - 1))
        yi = max(0, min(yi, n - 1))

        x = gated_data[:, xi] if len(gated_data) > 0 else np.array([])
        y = gated_data[:, yi] if len(gated_data) > 0 else np.array([])

        names = fcs.display_names()

        if self._view_mode == "1D":
            self.ax.clear()
            pw = self._get_gw_pw_params(x)
            if pw is not None and len(x) > 0:
                dmin, dmax, anchor, frac = pw
                x_t = FlowCytApp._pw_transform(x, dmin, dmax, anchor, frac)
                self.ax.hist(x_t, bins=256, color="steelblue",
                             edgecolor="none", alpha=0.7, density=True)
                FlowCytApp._set_pw_ticks(self.ax, dmin, dmax, anchor, frac)
                self.ax.set_xlim(-0.02, 1.02)
                anchor_t = FlowCytApp._pw_transform(
                    np.array([anchor]), dmin, dmax, anchor, frac
                )[0]
                self.ax.axvline(anchor_t, color="red", lw=1, ls=":", alpha=0.5)
                self.ax.set_xlabel(f"{names[xi]}  (compressed)")
            elif len(x) > 0:
                self.ax.hist(x, bins=256, color="steelblue",
                             edgecolor="none", alpha=0.7, density=True)
                self.ax.set_xlabel(names[xi])
            else:
                self.ax.set_xlabel(names[xi])
            self.ax.set_ylabel("Density")
            self.ax.set_title(
                f"Gate: {self.gate.name}  ({mask.sum():,} events) — 1D"
            )
            if pw is None:
                if self._x_scale == "log" and len(x) > 0:
                    self.ax.set_xscale("symlog",
                                       linthresh=FlowCytApp._compute_linthresh(x))
                else:
                    self.ax.set_xscale("linear")
        else:
            density_scatter(self.ax, x, y)
            self.ax.set_xlabel(names[xi])
            self.ax.set_ylabel(names[yi])
            self.ax.set_title(f"Gate: {self.gate.name}  ({mask.sum():,} events)")

            if self._x_scale == "log" and len(x) > 0:
                self.ax.set_xscale("symlog",
                                   linthresh=FlowCytApp._compute_linthresh(x))
            else:
                self.ax.set_xscale("linear")
            if self._y_scale == "log" and len(y) > 0:
                self.ax.set_yscale("symlog",
                                   linthresh=FlowCytApp._compute_linthresh(y))
            else:
                self.ax.set_yscale("linear")

            # Draw child gate overlays on this window's plot
            xn = fcs.channel_names[xi]
            yn = fcs.channel_names[yi]
            _stats = self.app.gate_mgr.compute_stats(fcs.data, fcs.channel_names)
            _sbu = {s["uid"]: s for s in _stats}
            for g in self.app.gate_mgr.gates:
                if g.parent_gate_uid == self.gate.uid and g.x_channel == xn and g.y_channel == yn:
                    s = _sbu.get(g.uid)
                    if s:
                        lbl = f"{g.name}\n{s['percent_of_total']:.1f}% total | {s['percent']:.1f}% parent"
                    else:
                        lbl = g.name
                    draw_gate_overlay(self.ax, g, label_text=lbl)

        # Stats panel
        self.ax_stats.clear()
        self.ax_stats.set_xticks([])
        self.ax_stats.set_yticks([])
        self.ax_stats.set_title("Stats", fontsize=8, loc="left", fontweight="bold")
        total = fcs.num_events
        count = int(mask.sum())
        pct = 100.0 * count / total if total else 0
        info = (
            f"Gate: {self.gate.name}\n"
            f"Events: {count:,} / {total:,}\n"
            f"Pct of total: {pct:.1f}%\n"
        )
        # Show child gate stats
        child_gates = [g for g in self.app.gate_mgr.gates
                       if g.parent_gate_uid == self.gate.uid]
        if child_gates:
            info += f"\nChild gates ({len(child_gates)}):\n"
            for cg in child_gates:
                try:
                    cxi = fcs.channel_names.index(cg.x_channel)
                    cyi = fcs.channel_names.index(cg.y_channel)
                    cmask = cg.contains(fcs.data[:, cxi], fcs.data[:, cyi]) & mask
                    cc = int(cmask.sum())
                    cpct = 100.0 * cc / count if count else 0
                    info += f"  {cg.name}: {cc:,} ({cpct:.1f}%)\n"
                except ValueError:
                    info += f"  {cg.name}: (channels n/a)\n"
        elif count > 0:
            info += "\nMedians:\n"
            for ci, ch in enumerate(fcs.channel_names):
                med = float(np.median(gated_data[:, ci]))
                info += f"  {ch}: {med:.1f}\n"

        self.ax_stats.text(0.05, 0.95, info, fontsize=6.5, family="monospace",
                           va="top", transform=self.ax_stats.transAxes,
                           linespacing=1.2)

        try:
            self.fig.canvas.draw()
        except Exception:
            pass

    # ================================================================ #
    #  Event dispatchers
    # ================================================================ #
    def _on_click(self, event):
        # Click outside plot area → switch to Navigate
        if (event.inaxes != self.ax and self.app.fcs is not None
                and self._mode not in (MODE_NAV,)):
            if event.inaxes is None:
                self._mode = MODE_NAV
                self.radio_mode.set_active(0)
                self.app._log(f"[{self.gate.name}] Switched to Navigate")
                return

        if event.inaxes != self.ax or self.app.fcs is None:
            return
        if event.xdata is None or event.ydata is None:
            return

        # Navigate + 1D: compression drag
        if self._mode == MODE_NAV and self._view_mode == "1D" and event.button == 1:
            if event.dblclick:
                self._reset_gw_compression()
                self._refresh()
                return
            self._start_gw_compress_drag(event)
            return

        if self._mode == MODE_POLY:
            self._poly_click(event)
        elif self._mode == MODE_RECT:
            if event.button == 1:
                self._rect_origin = (event.xdata, event.ydata)
        elif self._mode == MODE_ELLIPSE:
            if event.button == 1:
                self._ellipse_origin = (event.xdata, event.ydata)
        elif self._mode == MODE_QUAD:
            if event.button == 1:
                self._quad_click(event)
        elif self._mode == MODE_THRESH:
            if event.button == 1:
                self._thresh_click(event)
        elif self._mode in (MODE_TRANSLATE, MODE_ROTATE):
            self._move_click(event)
        elif self._mode == MODE_STRETCH:
            pass  # Stretch uses Tab + arrows

    def _on_release(self, event):
        if self._compress_dragging:
            self._compress_dragging = False
            return
        if event.inaxes != self.ax or self.app.fcs is None:
            return
        if event.xdata is None or event.ydata is None:
            self._rect_origin = None
            self._ellipse_origin = None
            self._handle_drag_type = None
            self._handle_drag_start = None
            return
        if self._mode == MODE_RECT and self._rect_origin is not None:
            self._rect_release(event)
        elif self._mode == MODE_ELLIPSE and self._ellipse_origin is not None:
            self._ellipse_release(event)
        elif self._mode in (MODE_TRANSLATE, MODE_ROTATE) and self._handle_drag_type is not None:
            self._move_release(event)

    def _on_motion(self, event):
        if self._compress_dragging:
            self._update_gw_compress_drag(event)
            return
        if event.inaxes != self.ax or self.app.fcs is None:
            return
        if event.xdata is None or event.ydata is None:
            return
        if self._mode == MODE_RECT and self._rect_origin is not None:
            self._rect_motion(event)
        elif self._mode == MODE_POLY and len(self._poly_verts) >= 1:
            self._poly_motion(event)
        elif self._mode == MODE_ELLIPSE and self._ellipse_origin is not None:
            self._ellipse_motion(event)
        elif self._mode in (MODE_TRANSLATE, MODE_ROTATE) and self._handle_drag_type is not None:
            self._move_motion(event)

    # ── GateWindow compression drag helpers ──

    def _start_gw_compress_drag(self, event):
        if self._compress_anchor is not None and self._compress_frac is not None:
            anchor_data = FlowCytApp._pw_inverse(
                event.xdata, self._compress_dmin, self._compress_dmax,
                self._compress_anchor, self._compress_frac,
            )
        else:
            anchor_data = event.xdata

        mask = self._get_mask()
        fcs = self.app.fcs
        if fcs is None:
            return
        xi = self._x_idx
        xi = max(0, min(xi, len(fcs.channel_names) - 1))
        x_all = fcs.data[mask, xi] if mask.sum() > 0 else fcs.data[:, xi]
        dmin, dmax = float(np.min(x_all)), float(np.max(x_all))
        if dmax <= dmin:
            return

        natural_frac = (anchor_data - dmin) / (dmax - dmin)
        natural_frac = max(0.02, min(0.98, natural_frac))

        self._compress_anchor = anchor_data
        self._compress_dmin = dmin
        self._compress_dmax = dmax
        self._compress_frac = natural_frac
        self._compress_base_frac = natural_frac
        self._compress_drag_px = event.x
        self._compress_dragging = True

    def _update_gw_compress_drag(self, event):
        if not self._compress_dragging or event.x is None:
            return
        ax_extent = self.ax.get_window_extent()
        ax_width = ax_extent.width
        if ax_width <= 0:
            return
        dx_px = event.x - self._compress_drag_px
        shift = dx_px / ax_width
        new_frac = self._compress_base_frac + shift
        new_frac = max(0.02, min(0.98, new_frac))
        self._compress_frac = new_frac
        self._refresh()

    def _on_key(self, event):
        # Undo / redo (delegates to main app)
        if event.key in ("ctrl+z", "cmd+z"):
            self.app._undo()
            self._refresh()
            return
        if event.key in ("ctrl+shift+z", "cmd+shift+z", "ctrl+y", "cmd+y"):
            self.app._redo()
            self._refresh()
            return

        if event.key == "enter" and self._mode == MODE_POLY:
            self._close_polygon()

        # Parse arrow direction and shift modifier
        arrow_keys = {"left", "right", "up", "down",
                      "shift+left", "shift+right", "shift+up", "shift+down"}
        direction = event.key.replace("shift+", "") if event.key in arrow_keys else None
        fine = event.key.startswith("shift+") if event.key in arrow_keys else False
        scale = 1.0 / 3.0 if fine else 1.0

        # Arrow keys in Translate mode — all 4 directions
        if self._mode == MODE_TRANSLATE and self._handle_selected_gate is not None:
            gate = self._handle_selected_gate
            if direction in ("left", "right", "up", "down"):
                self.app._push_undo()
                dx, dy = 0.0, 0.0
                if direction in ("left", "right"):
                    dx = FlowCytApp._scale_aware_step(
                        self.ax, gate, "x",
                        self._x_scale, direction == "right",
                    ) * scale
                else:
                    if isinstance(gate, ThresholdGate):
                        return
                    dy = FlowCytApp._scale_aware_step(
                        self.ax, gate, "y",
                        self._y_scale, direction == "up",
                    ) * scale
                self.app._apply_move(gate, dx, dy)
                self._clear_handles()
                self._refresh()
                self.app._refresh_plot()
                self._handle_selected_gate = gate
                self._draw_handles(gate)

        # Arrow keys in Rotate mode — left/right rotate
        if self._mode == MODE_ROTATE and self._handle_selected_gate is not None:
            gate = self._handle_selected_gate
            if direction in ("left", "right"):
                if isinstance(gate, (ThresholdGate, QuadrantGate)):
                    return
                self.app._push_undo()
                base_delta = np.radians(2) if direction == "right" else np.radians(-2)
                delta = base_delta * scale
                cx, cy = FlowCytApp._gate_centroid(gate)
                self.app._apply_rotate(gate, delta, cx, cy, ax=self.ax)
                gate = next((g for g in self.app.gate_mgr.gates if g.uid == gate.uid), gate)
                self._handle_selected_gate = gate
                self._clear_handles()
                self._refresh()
                self.app._refresh_plot()
                self._handle_selected_gate = gate
                self._draw_handles(gate)

        # Stretch mode: Tab cycles points, arrows move selected point
        if self._mode == MODE_STRETCH and self._stretch_selected_gate is not None:
            if event.key == "tab":
                if not self._stretch_points:
                    return
                self._stretch_point_idx = (
                    (self._stretch_point_idx + 1) % len(self._stretch_points)
                )
                self._clear_gw_stretch_highlight()
                self._draw_gw_stretch_highlight()
                self.app._log(f"[{self.gate.name}] Control point "
                              f"{self._stretch_point_idx + 1}/{len(self._stretch_points)}")
                self.fig.canvas.draw_idle()
            elif direction in ("left", "right", "up", "down"):
                if self._stretch_point_idx < 0:
                    return
                self.app._push_undo()
                gate = self._stretch_selected_gate
                idx = self._stretch_point_idx
                # Same fix as the main window: anchor the log-mode step
                # at the point being stretched, not the gate centroid.
                px, py = self._stretch_points[idx]
                dx, dy = 0.0, 0.0
                if direction in ("left", "right"):
                    dx = FlowCytApp._scale_aware_step(
                        self.ax, gate, "x",
                        self._x_scale, direction == "right",
                        position=px,
                    ) * scale
                else:
                    dy = FlowCytApp._scale_aware_step(
                        self.ax, gate, "y",
                        self._y_scale, direction == "up",
                        position=py,
                    ) * scale
                self.app._apply_stretch_point(gate, idx, dx, dy)
                self._stretch_points = FlowCytApp._get_stretch_points(gate)
                self._clear_gw_stretch_highlight()
                self._refresh()
                self.app._refresh_plot()
                self._stretch_selected_gate = gate
                self._draw_gw_stretch_highlight()
                self.fig.canvas.draw_idle()

    # ================================================================ #
    #  Gating tools
    # ================================================================ #
    def _poly_click(self, event):
        if event.xdata is None or event.ydata is None:
            return
        if event.button == 1:
            import time
            current_time = time.time()
            if len(self._poly_verts) >= 3 and (current_time - self._poly_last_click_time) < 0.3:
                self._close_polygon()
                self._poly_last_click_time = 0.0
                return
            self._poly_last_click_time = current_time
            self._poly_verts.append((event.xdata, event.ydata))
            pt = self.ax.plot(event.xdata, event.ydata, "rx", markersize=8)[0]
            self._temp_artists.append(pt)
            if len(self._poly_verts) > 1:
                xs = [v[0] for v in self._poly_verts[-2:]]
                ys = [v[1] for v in self._poly_verts[-2:]]
                ln = self.ax.plot(xs, ys, "r--", lw=1.2)[0]
                self._temp_artists.append(ln)
            self.fig.canvas.draw_idle()
        elif event.button == 3:
            self._close_polygon()

    def _poly_motion(self, event):
        if event.xdata is None or event.ydata is None:
            return
        temp_to_keep = []
        for artist in self._temp_artists:
            if hasattr(artist, 'get_linestyle') and artist.get_linestyle() == ':':
                try:
                    artist.remove()
                except Exception:
                    pass
            else:
                temp_to_keep.append(artist)
        self._temp_artists = temp_to_keep

        if len(self._poly_verts) >= 1:
            lx, ly = self._poly_verts[-1]
            ln = self.ax.plot([lx, event.xdata], [ly, event.ydata],
                              "r:", lw=1.0, alpha=0.5)[0]
            self._temp_artists.append(ln)
            if len(self._poly_verts) >= 3:
                fx, fy = self._poly_verts[0]
                cl = self.ax.plot([event.xdata, fx], [event.ydata, fy],
                                  "r:", lw=1.0, alpha=0.3)[0]
                self._temp_artists.append(cl)
        self.fig.canvas.draw_idle()

    def _close_polygon(self):
        if len(self._poly_verts) < 3:
            self.app._log(f"[{self.gate.name}] Need >= 3 vertices for polygon")
            return
        xi, yi, xn, yn = self._current_xy()
        n = len(self.app.gate_mgr.gates) + 1
        gate = self.app.gate_mgr.add_polygon_gate(
            name=f"P{n}", x_channel=xn, y_channel=yn,
            vertices=list(self._poly_verts),
            parent_gate_uid=self.gate.uid,
        )
        self._poly_verts.clear()
        self._clear_temp()
        self._refresh()
        self.app._refresh_plot()
        self._log_gate_created(gate)
        self._open_child_window(gate)

    def _rect_motion(self, event):
        if event.xdata is None or event.ydata is None:
            return
        self._clear_temp()
        x0, y0 = self._rect_origin
        x1, y1 = event.xdata, event.ydata
        rect = RectPatch(
            (min(x0, x1), min(y0, y1)),
            abs(x1 - x0), abs(y1 - y0),
            fill=False, edgecolor="red", lw=1.5, linestyle="--",
        )
        self.ax.add_patch(rect)
        self._temp_artists.append(rect)
        self.fig.canvas.draw_idle()

    def _rect_release(self, event):
        if event.xdata is None or event.ydata is None:
            self._rect_origin = None
            self._clear_temp()
            return
        x0, y0 = self._rect_origin
        x1, y1 = event.xdata, event.ydata
        self._rect_origin = None
        self._clear_temp()
        if abs(x1 - x0) < 1e-9 or abs(y1 - y0) < 1e-9:
            return
        xi, yi, xn, yn = self._current_xy()
        n = len(self.app.gate_mgr.gates) + 1
        gate = self.app.gate_mgr.add_rectangle_gate(
            name=f"R{n}", x_channel=xn, y_channel=yn,
            x_min=min(x0, x1), x_max=max(x0, x1),
            y_min=min(y0, y1), y_max=max(y0, y1),
            parent_gate_uid=self.gate.uid,
        )
        self._refresh()
        self.app._refresh_plot()
        self._log_gate_created(gate)
        self._open_child_window(gate)

    def _ellipse_motion(self, event):
        if event.xdata is None or event.ydata is None:
            return
        self._clear_temp()
        cx, cy = self._ellipse_origin
        dx = abs(event.xdata - cx)
        dy = abs(event.ydata - cy)
        ellipse = EllipsePatch(
            (cx, cy), 2 * dx, 2 * dy, angle=0,
            fill=False, edgecolor="red", lw=1.5, linestyle="--"
        )
        self.ax.add_patch(ellipse)
        self._temp_artists.append(ellipse)
        self.fig.canvas.draw_idle()

    def _ellipse_release(self, event):
        if event.xdata is None or event.ydata is None:
            self._ellipse_origin = None
            self._clear_temp()
            return
        cx, cy = self._ellipse_origin
        dx = abs(event.xdata - cx)
        dy = abs(event.ydata - cy)
        self._ellipse_origin = None
        self._clear_temp()
        if dx < 1e-9 or dy < 1e-9:
            return
        xi, yi, xn, yn = self._current_xy()
        n = len(self.app.gate_mgr.gates) + 1
        gate = self.app.gate_mgr.add_ellipse_gate(
            name=f"E{n}", x_channel=xn, y_channel=yn,
            center_x=cx, center_y=cy,
            semi_x=dx, semi_y=dy, angle=0.0,
            parent_gate_uid=self.gate.uid,
        )
        self._refresh()
        self.app._refresh_plot()
        self._log_gate_created(gate)
        self._open_child_window(gate)

    def _quad_click(self, event):
        mx, my = event.xdata, event.ydata
        self._clear_temp()
        ln_v = self.ax.axvline(mx, color="red", lw=1.5, ls="--", alpha=0.7)
        ln_h = self.ax.axhline(my, color="red", lw=1.5, ls="--", alpha=0.7)
        self._temp_artists.extend([ln_v, ln_h])
        self.fig.canvas.draw_idle()

        options = [
            "Q1 — upper-right  (x≥mid, y≥mid)",
            "Q2 — upper-left   (x<mid, y≥mid)",
            "Q3 — lower-left   (x<mid, y<mid)",
            "Q4 — lower-right  (x≥mid, y<mid)",
        ]

        def on_pick(idx):
            quadrant = ["Q1", "Q2", "Q3", "Q4"][idx]
            self._clear_temp()
            xi, yi, xn, yn = self._current_xy()
            n = len(self.app.gate_mgr.gates) + 1
            gate = self.app.gate_mgr.add_quadrant_gate(
                name=f"Quad{n}-{quadrant}",
                x_channel=xn, y_channel=yn,
                mid_x=mx, mid_y=my, quadrant=quadrant,
                parent_gate_uid=self.gate.uid,
            )
            self._refresh()
            self.app._refresh_plot()
            self._log_gate_created(gate)
            self._open_child_window(gate)

        self.app._show_popup_list("Select Quadrant", options, -1, on_pick)

    def _thresh_click(self, event):
        """Place a threshold line and choose left/right gating (1D)."""
        tx_display = event.xdata
        if self._view_mode != "1D":
            self._view_mode = "1D"
            self._btn_vm.label.set_text("View: 1D Histogram")
            self._update_gw_compress_hint()
            self._refresh()

        # Convert display → original space when compression active
        pw = self._get_gw_pw_params(np.array([]))
        if pw is not None:
            dmin, dmax, anchor, frac = pw
            tx = FlowCytApp._pw_inverse(tx_display, dmin, dmax, anchor, frac)
        else:
            tx = tx_display

        self._clear_temp()
        ln = self.ax.axvline(tx_display, color="red", lw=2, ls="--", alpha=0.8)
        self._temp_artists.append(ln)
        self.fig.canvas.draw_idle()

        options = [
            f"Left  (x < {tx:.1f})",
            f"Right (x ≥ {tx:.1f})",
        ]

        def on_pick(idx):
            side = "left" if idx == 0 else "right"
            self._clear_temp()
            xi, yi, xn, yn = self._current_xy()
            n = len(self.app.gate_mgr.gates) + 1
            gate = self.app.gate_mgr.add_threshold_gate(
                name=f"T{n}-{side[0].upper()}",
                x_channel=xn, y_channel=yn,
                threshold=tx, side=side,
                parent_gate_uid=self.gate.uid,
            )
            self._refresh()
            self.app._refresh_plot()
            self._log_gate_created(gate)
            self._open_child_window(gate)

        self.app._show_popup_list("Select side of threshold", options, -1, on_pick)

    def _log_gate_created(self, gate):
        """Log creation of a child gate with stats."""
        fcs = self.app.fcs
        if fcs is None:
            return
        mask = self._get_mask()
        parent_count = int(mask.sum())
        try:
            cxi = fcs.channel_names.index(gate.x_channel)
            cyi = fcs.channel_names.index(gate.y_channel)
            cmask = gate.contains(fcs.data[:, cxi], fcs.data[:, cyi]) & mask
            child_count = int(cmask.sum())
            pct = 100.0 * child_count / parent_count if parent_count else 0
            self.app._log(
                f"[{self.gate.name}] Created '{gate.name}': "
                f"{child_count:,} events ({pct:.1f}% of parent)"
            )
        except ValueError:
            self.app._log(f"[{self.gate.name}] Created gate '{gate.name}'")

    # ================================================================ #
    #  Handle-based gate editing (reuses FlowCytApp patterns)
    # ================================================================ #
    def _find_gate_at(self, px, py):
        """Find a child gate under cursor position."""
        _, _, xn, yn = self._current_xy()
        candidates = []
        for g in self.app.gate_mgr.gates:
            if g.parent_gate_uid != self.gate.uid:
                continue
            if g.x_channel != xn or g.y_channel != yn:
                continue
            if g.contains(np.array([px]), np.array([py]))[0]:
                candidates.append(g)
        if not candidates:
            return None
        candidates.sort(key=lambda g: len(g.vertices) if g.vertices else 1e9)
        return candidates[0]

    def _compute_handles(self, gate):
        xmin, xmax, ymin, ymax = FlowCytApp._gate_bbox(gate)
        mx = (xmin + xmax) / 2
        my = (ymin + ymax) / 2
        handles = [
            (xmin, ymax, "tl"), (mx, ymax, "t"), (xmax, ymax, "tr"),
            (xmin, my,   "l"),                    (xmax, my,   "r"),
            (xmin, ymin, "bl"), (mx, ymin, "b"), (xmax, ymin, "br"),
        ]
        if not isinstance(gate, QuadrantGate):
            try:
                disp_tc = self.ax.transData.transform((mx, ymax))
                disp_rot = (disp_tc[0], disp_tc[1] + 30)
                rx, ry = self.ax.transData.inverted().transform(disp_rot)
                handles.append((rx, ry, "rot"))
            except Exception:
                handles.append((mx, ymax, "rot"))
        return handles

    def _draw_handles(self, gate):
        handles = self._compute_handles(gate)
        self._handle_positions = handles
        self._handle_artists = []
        xmin, xmax, ymin, ymax = FlowCytApp._gate_bbox(gate)
        mx, my = (xmin + xmax) / 2, (ymin + ymax) / 2
        bbox_patch = RectPatch(
            (xmin, ymin), xmax - xmin, ymax - ymin,
            fill=False, edgecolor="#444444", lw=1.0, ls=":", alpha=0.7,
        )
        self.ax.add_patch(bbox_patch)
        self._handle_artists.append(bbox_patch)
        for hx, hy, htype in handles:
            if htype == "rot":
                marker = self.ax.plot(
                    hx, hy, "o", color="#22aa22", markersize=8,
                    markeredgecolor="black", markeredgewidth=1, zorder=100,
                )[0]
                line = self.ax.plot(
                    [mx, hx], [ymax, hy],
                    color="#444444", lw=1.0, ls=":", alpha=0.6,
                )[0]
                self._handle_artists.extend([marker, line])
            else:
                marker = self.ax.plot(
                    hx, hy, "s", color="#3388dd", markersize=7,
                    markeredgecolor="black", markeredgewidth=1, zorder=100,
                )[0]
                self._handle_artists.append(marker)
        self.fig.canvas.draw_idle()

    def _clear_handles(self):
        for a in getattr(self, '_handle_artists', []):
            try:
                a.remove()
            except Exception:
                pass
        self._handle_artists = []
        self._handle_positions = []

    def _hit_test_handle(self, event):
        if not getattr(self, '_handle_positions', []):
            return None
        try:
            click_disp = self.ax.transData.transform((event.xdata, event.ydata))
        except Exception:
            return None
        for hx, hy, htype in self._handle_positions:
            try:
                h_disp = self.ax.transData.transform((hx, hy))
                dist = np.hypot(click_disp[0] - h_disp[0], click_disp[1] - h_disp[1])
                if dist <= self.HANDLE_RADIUS_PX:
                    return htype
            except Exception:
                continue
        return None

    def _move_click(self, event):
        hit = self._hit_test_handle(event)
        if hit is not None:
            self._handle_drag_type = hit
            self._handle_drag_start = (event.xdata, event.ydata)
            return
        gate = self._find_gate_at(event.xdata, event.ydata)
        self._clear_handles()
        self._clear_temp()
        if gate is None:
            self._handle_selected_gate = None
            return
        if gate == self._handle_selected_gate:
            self._handle_drag_type = "move"
            self._handle_drag_start = (event.xdata, event.ydata)
            return
        self._handle_selected_gate = gate
        self._draw_handles(gate)
        self.app._log(f"[{self.gate.name}] Selected '{gate.name}' — drag handles to edit")

    def _move_motion(self, event):
        if event.xdata is None or event.ydata is None:
            return
        gate = self._handle_selected_gate
        if gate is None or self._handle_drag_type is None:
            return
        self._clear_temp()
        sx, sy = self._handle_drag_start
        cx, cy = FlowCytApp._gate_centroid(gate)
        dtype = self._handle_drag_type
        if dtype == "move":
            dx, dy = event.xdata - sx, event.ydata - sy
            self._gw_preview_move(gate, dx, dy)
        elif dtype == "rot":
            # Compute angle in display (pixel) space for correct visual rotation
            trans = self.ax.transData
            cx_d, cy_d = trans.transform((cx, cy))
            s_d = trans.transform((sx, sy))
            e_d = trans.transform((event.xdata, event.ydata))
            a0 = np.arctan2(s_d[1] - cy_d, s_d[0] - cx_d)
            a1 = np.arctan2(e_d[1] - cy_d, e_d[0] - cx_d)
            self._gw_preview_rotate(gate, a1 - a0, cx, cy)
        else:
            self._gw_preview_resize(gate, dtype, event.xdata, event.ydata)
        self.fig.canvas.draw_idle()

    def _move_release(self, event):
        dtype = self._handle_drag_type
        gate = self._handle_selected_gate
        self._handle_drag_type = None
        self._clear_temp()
        if dtype is None or gate is None:
            return
        if event.xdata is None or event.ydata is None:
            return
        sx, sy = self._handle_drag_start
        cx, cy = FlowCytApp._gate_centroid(gate)
        if dtype == "move":
            dx, dy = event.xdata - sx, event.ydata - sy
            if abs(dx) < 1e-12 and abs(dy) < 1e-12:
                return
            self.app._apply_move(gate, dx, dy)
            self.app._log(f"[{self.gate.name}] Moved '{gate.name}'")
        elif dtype == "rot":
            # Compute angle in display (pixel) space
            trans = self.ax.transData
            cx_d, cy_d = trans.transform((cx, cy))
            s_d = trans.transform((sx, sy))
            e_d = trans.transform((event.xdata, event.ydata))
            a0 = np.arctan2(s_d[1] - cy_d, s_d[0] - cx_d)
            a1 = np.arctan2(e_d[1] - cy_d, e_d[0] - cx_d)
            delta = a1 - a0
            if abs(delta) < 1e-6:
                return
            # _apply_rotate needs gate_mgr for rect→poly conversion
            self.app._apply_rotate(gate, delta, cx, cy, ax=self.ax)
            # If rect was converted to polygon, update our reference
            gate = next((g for g in self.app.gate_mgr.gates if g.uid == gate.uid), gate)
            self.app._log(f"[{self.gate.name}] Rotated '{gate.name}' by {np.degrees(delta):.1f}°")
        else:
            self.app._apply_resize(gate, dtype, event.xdata, event.ydata)
            self.app._log(f"[{self.gate.name}] Resized '{gate.name}'")
        self._clear_handles()
        self._refresh()
        self.app._refresh_plot()
        self._handle_selected_gate = gate
        self._draw_handles(gate)

    # ── Preview/apply helpers (use self.ax instead of self.ax_main) ──
    def _gw_preview_move(self, gate, dx, dy):
        if isinstance(gate, PolygonGate):
            shifted = [(vx + dx, vy + dy) for vx, vy in gate.vertices]
            p = PolyPatch(shifted, closed=True, fill=False,
                          edgecolor=gate.color, lw=1.5, ls="--")
            self.ax.add_patch(p); self._temp_artists.append(p)
        elif isinstance(gate, RectangleGate):
            r = RectPatch((gate.x_min + dx, gate.y_min + dy),
                          gate.x_max - gate.x_min, gate.y_max - gate.y_min,
                          fill=False, edgecolor=gate.color, lw=1.5, ls="--")
            self.ax.add_patch(r); self._temp_artists.append(r)
        elif isinstance(gate, EllipseGate):
            e = EllipsePatch((gate.center_x + dx, gate.center_y + dy),
                             2*gate.semi_x, 2*gate.semi_y,
                             angle=np.degrees(gate.angle),
                             fill=False, edgecolor=gate.color, lw=1.5, ls="--")
            self.ax.add_patch(e); self._temp_artists.append(e)
        elif isinstance(gate, QuadrantGate):
            v = self.ax.axvline(gate.mid_x+dx, color=gate.color, lw=1.5, ls="--", alpha=0.6)
            h = self.ax.axhline(gate.mid_y+dy, color=gate.color, lw=1.5, ls="--", alpha=0.6)
            self._temp_artists.extend([v, h])

    def _gw_preview_rotate(self, gate, delta, cx, cy):
        # Rotate preview vertices in display space for visual accuracy
        trans = self.ax.transData
        inv = trans.inverted()
        cx_d, cy_d = trans.transform((cx, cy))
        cos_d, sin_d = np.cos(delta), np.sin(delta)

        verts = gate.vertices
        if verts:
            rotated = []
            for vx, vy in verts:
                dx_d, dy_d = trans.transform((vx, vy))
                rx, ry = dx_d - cx_d, dy_d - cy_d
                nx = cx_d + rx * cos_d - ry * sin_d
                ny = cy_d + rx * sin_d + ry * cos_d
                nvx, nvy = inv.transform((nx, ny))
                rotated.append((nvx, nvy))
            p = PolyPatch(rotated, closed=True, fill=False,
                          edgecolor=gate.color, lw=1.5, ls="--")
            self.ax.add_patch(p); self._temp_artists.append(p)

        ln = self.ax.plot(cx, cy, "o", color=gate.color,
                           ms=4, alpha=0.5)[0]
        self._temp_artists.append(ln)

    def _gw_preview_resize(self, gate, htype, mx, my):
        xmin, xmax, ymin, ymax = FlowCytApp._gate_bbox(gate)
        nxmin, nxmax, nymin, nymax = xmin, xmax, ymin, ymax
        if "l" in htype: nxmin = mx
        if "r" in htype: nxmax = mx
        if "b" in htype: nymin = my
        if "t" in htype: nymax = my
        if htype == "t": nymax = my
        elif htype == "b": nymin = my
        elif htype == "l": nxmin = mx
        elif htype == "r": nxmax = mx
        if nxmin > nxmax: nxmin, nxmax = nxmax, nxmin
        if nymin > nymax: nymin, nymax = nymax, nymin
        r = RectPatch((nxmin, nymin), nxmax - nxmin, nymax - nymin,
                      fill=False, edgecolor=gate.color, lw=1.5, ls="--")
        self.ax.add_patch(r); self._temp_artists.append(r)

    # ================================================================ #
    #  Child window management
    # ================================================================ #
    def _open_child_window(self, gate: Gate):
        """Open a sub-window for a child gate."""
        if self.app.fcs is None:
            return
        if gate.uid in self._child_windows:
            try:
                plt.close(self._child_windows[gate.uid].fig)
            except Exception:
                pass
        gw = GateWindow(self.app, gate, parent_window=self)
        self._child_windows[gate.uid] = gw
        # Also register in the main app's gate_windows for file-change sync
        self.app._gate_windows[gate.uid] = gw

    def _close_child_window(self, uid: str):
        gw = self._child_windows.pop(uid, None)
        self.app._gate_windows.pop(uid, None)
        if gw is not None:
            # Recursively close grandchildren
            for child_uid in list(gw._child_windows.keys()):
                gw._close_child_window(child_uid)
            try:
                plt.close(gw.fig)
            except Exception:
                pass

    def _refresh_child_windows(self):
        dead = []
        for uid, gw in self._child_windows.items():
            try:
                if not plt.fignum_exists(gw.fig.number):
                    dead.append(uid)
                    continue
                gw._update_labels()
                gw._refresh()
            except Exception:
                dead.append(uid)
        for uid in dead:
            self._child_windows.pop(uid, None)
            self.app._gate_windows.pop(uid, None)

    # ================================================================ #
    #  Actions
    # ================================================================ #
    def _on_save_plot(self):
        """Save just the data axes of this gate window via a native dialog."""
        xi, yi, xn, yn = self._current_xy()
        safe = lambda s: (
            s.replace("/", "-").replace("\\", "-").replace(" ", "_")
             .replace(":", "-").replace("*", "").replace("?", "")
             .replace("\"", "").replace("<", "").replace(">", "").replace("|", "")
        )
        base = safe(self.gate.name)
        suffix = f"_{safe(xn)}_vs_{safe(yn)}" if self._view_mode == "2D" else f"_{safe(xn)}_1D"
        default_name = f"{base}{suffix}.png"

        path = FlowCytApp._ask_save_path(
            default_name=default_name,
            filetypes=[("PNG image", "*.png"),
                       ("PDF document", "*.pdf"),
                       ("SVG vector", "*.svg"),
                       ("All files", "*.*")],
            default_ext=".png",
            title=f"Save Plot ({self.gate.name})",
        )
        if not path:
            self.app._log(f"[{self.gate.name}] Save cancelled")
            return
        fpath = Path(path)
        if not fpath.suffix:
            fpath = fpath.with_suffix(".png")
        try:
            FlowCytApp._save_axes_to_file(self.fig, self.ax, str(fpath))
            self.app._log(f"[{self.gate.name}] Plot saved to {fpath}")
        except Exception as exc:
            self.app._log(f"[{self.gate.name}] Save error: {exc}")

    def _on_remove_gate(self):
        """Remove a child gate created in this window."""
        child_gates = [g for g in self.app.gate_mgr.gates
                       if g.parent_gate_uid == self.gate.uid]
        if not child_gates:
            self.app._log(f"[{self.gate.name}] No child gates to remove")
            return

        items = []
        for g in child_gates:
            items.append(f"{g.name}  ({g.x_channel} vs {g.y_channel})")

        def on_pick(idx):
            gate = child_gates[idx]
            name = gate.name
            uid = gate.uid

            # Remove grandchildren too
            grandchildren = [g for g in self.app.gate_mgr.gates
                             if g.parent_gate_uid == uid]
            self.app.gate_mgr.remove_gate(uid)
            for gc in grandchildren:
                self.app.gate_mgr.remove_gate(gc.uid)

            # Close windows
            self._close_child_window(uid)
            for gc in grandchildren:
                self._close_child_window(gc.uid)

            gc_msg = f" (and {len(grandchildren)} sub-gate(s))" if grandchildren else ""
            self.app._log(f"[{self.gate.name}] Removed '{name}'{gc_msg}")
            self._refresh()
            self.app._refresh_plot()

        self.app._show_popup_list(
            f"Remove Child Gate ({self.gate.name})", items, -1, on_pick
        )

    def _on_clear_child_gates(self):
        """Remove all child gates created in this window."""
        child_gates = [g for g in self.app.gate_mgr.gates
                       if g.parent_gate_uid == self.gate.uid]
        if not child_gates:
            self.app._log(f"[{self.gate.name}] No child gates to clear")
            return

        for g in child_gates:
            # Also remove grandchildren
            grandchildren = [gc for gc in self.app.gate_mgr.gates
                             if gc.parent_gate_uid == g.uid]
            for gc in grandchildren:
                self.app.gate_mgr.remove_gate(gc.uid)
                self._close_child_window(gc.uid)
            self.app.gate_mgr.remove_gate(g.uid)
            self._close_child_window(g.uid)

        self.app._log(f"[{self.gate.name}] Cleared {len(child_gates)} child gate(s)")
        self._refresh()
        self.app._refresh_plot()

    def _on_close(self, event):
        uid = self.gate.uid
        # Remove from parent window's child list
        if self.parent_window is not None:
            self.parent_window._child_windows.pop(uid, None)
        # Remove from main app's gate_windows
        self.app._gate_windows.pop(uid, None)
        # Close all child windows
        for child_uid in list(self._child_windows.keys()):
            self._close_child_window(child_uid)
