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
# Use TkAgg on macOS: the native 'macosx' backend has a Cocoa display-link
# that fires on a separate thread and can race with artist updates, causing
# segfaults during interactive refresh (channel switch, gate creation, etc.).
if sys.platform == "darwin":
    try:
        matplotlib.use("TkAgg")
    except Exception:
        pass
import matplotlib.pyplot as plt
from matplotlib.widgets import RadioButtons, Button
from matplotlib.patches import Rectangle as RectPatch, Polygon as PolyPatch, Ellipse as EllipsePatch

from .reader import FCSData
from .gating import (
    Gate, GateManager, PolygonGate, RectangleGate, EllipseGate, QuadrantGate,
)
from .plotting import (
    density_scatter,
    draw_gate_overlay,
    summary_bar_chart,
    summary_histogram,
)

# ------------------------------------------------------------------ #
#  Constants
# ------------------------------------------------------------------ #
MODE_NAV = "Navigate"
MODE_POLY = "Polygon"
MODE_RECT = "Rectangle"
MODE_ELLIPSE = "Ellipse"
MODE_QUAD = "Quadrant"
MODE_MOVE = "Move Gate"


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

        # Guard against reentrant/concurrent refresh (prevents segfault)
        self._refreshing: bool = False
        self._in_do_refresh: bool = False

        # Message log
        self._messages: list[str] = []
        self._max_messages: int = 20  # Keep last 20 messages

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
        btn_h = 0.04        # standard button height
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

        # --- Tool selector + Parent gate selector (side by side) ---
        radio_h = 0.14                     # height for 6 radio items
        y_cur -= 0.015                     # gap
        self.fig.text(right_start, y_cur, "Tool", fontsize=9, fontweight="bold")
        half_w = ctrl_w / 2 - 0.005
        self.fig.text(right_start + half_w + 0.01, y_cur, "Parent Gate", fontsize=9, fontweight="bold")
        y_cur -= radio_h
        self.ax_mode = self.fig.add_axes([right_start, y_cur, half_w, radio_h])
        self.radio_mode = RadioButtons(
            self.ax_mode,
            [MODE_NAV, MODE_POLY, MODE_RECT, MODE_ELLIPSE, MODE_QUAD, MODE_MOVE],
            active=0,
        )
        self.radio_mode.on_clicked(self._on_mode_change)

        self.ax_parent = self.fig.add_axes([right_start + half_w + 0.01, y_cur, half_w, radio_h])
        self.ax_parent.set_frame_on(False)
        self._radio_parent = None

        # --- Action buttons ---
        button_x = right_start
        y_cur -= btn_h + 0.01             # place first button below radios

        self.ax_btn_scandir = self.fig.add_axes([button_x, y_cur, ctrl_w, btn_h])
        self.btn_scandir = Button(self.ax_btn_scandir, "Scan Directory...")
        self.btn_scandir.on_clicked(lambda e: self._on_scan_directory())
        y_cur -= btn_h + 0.005

        self.ax_btn_summary = self.fig.add_axes([button_x, y_cur, ctrl_w, btn_h])
        self.btn_summary = Button(self.ax_btn_summary, "Summary")
        self.btn_summary.on_clicked(lambda e: self._on_show_summary())
        y_cur -= btn_h + 0.005

        self.ax_btn_export = self.fig.add_axes([button_x, y_cur, ctrl_w, btn_h])
        self.btn_export = Button(self.ax_btn_export, "Export CSV")
        self.btn_export.on_clicked(lambda e: self._on_export_csv())
        y_cur -= btn_h + 0.005

        self.ax_btn_remove = self.fig.add_axes([button_x, y_cur, ctrl_w, btn_h])
        self.btn_remove = Button(self.ax_btn_remove, "Remove Gate...")
        self.btn_remove.on_clicked(lambda e: self._on_remove_gate())
        y_cur -= btn_h + 0.005

        self.ax_btn_clear = self.fig.add_axes([button_x, y_cur, ctrl_w, btn_h])
        self.btn_clear = Button(self.ax_btn_clear, "Clear All Gates")
        self.btn_clear.on_clicked(lambda e: self._on_clear_gates())
        y_cur -= 0.015

        # --- Message log panel (bottom right) ---
        self.fig.text(right_start, y_cur, "Message Log", fontsize=9, fontweight="bold")
        y_cur -= 0.005
        msg_h = max(y_cur - 0.03, 0.10)   # fill remaining space down to 0.03
        self.ax_messages = self.fig.add_axes([right_start, 0.03, ctrl_w, msg_h])
        self.ax_messages.set_frame_on(True)
        self.ax_messages.set_xticks([])
        self.ax_messages.set_yticks([])

        # Connect canvas events
        self.fig.canvas.mpl_connect("button_press_event", self._on_click)
        self.fig.canvas.mpl_connect("button_release_event", self._on_release)
        self.fig.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.fig.canvas.mpl_connect("key_press_event", self._on_key)

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

        # Rebuild channel display names
        self._channel_display_names = self.fcs.display_names()
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
        """Show a popup list of all channels for X or Y axis."""
        if not self._channel_display_names:
            self._log("No file loaded")
            return

        current = self._x_idx if axis == "x" else self._y_idx

        def on_pick(idx):
            if axis == "x":
                self._x_idx = idx
                self._log(f"X: {self._channel_display_names[idx]}")
            else:
                self._y_idx = idx
                self._log(f"Y: {self._channel_display_names[idx]}")
            self._update_channel_labels()
            self._refresh_plot()

        label = "X" if axis == "x" else "Y"
        self._show_popup_list(
            f"Select {label} Channel",
            self._channel_display_names,
            current,
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
        # Truncate long names
        self.btn_xlabel.label.set_text(x_name[:22])
        self.btn_xlabel.label.set_fontsize(8)
        self.btn_ylabel.label.set_text(y_name[:22])
        self.btn_ylabel.label.set_fontsize(8)

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
            self._log("Switched to 1D histogram view")
        else:
            self._view_mode = "2D"
            self.btn_viewmode.label.set_text("View: 2D Scatter")
            self._log("Switched to 2D scatter view")
        self._do_refresh_plot()

    def _cycle_channel(self, axis: str, direction: int):
        """Cycle X or Y channel by direction (+1 or -1)."""
        if self.fcs is None:
            self._log("No file loaded")
            return
        n = len(self._channel_display_names)
        if n == 0:
            return
        # Set _refreshing BEFORE logging so that _refresh_messages
        # does NOT schedule a draw_idle that could race with the
        # upcoming _refresh_plot (which clears and rebuilds axes).
        self._refreshing = True
        if axis == "x":
            self._x_idx = (self._x_idx + direction) % n
            self._log(f"X: {self._channel_display_names[self._x_idx]}")
        else:
            self._y_idx = (self._y_idx + direction) % n
            self._log(f"Y: {self._channel_display_names[self._y_idx]}")
        self._update_channel_labels()
        # Keep _refreshing=True and call _do_refresh_plot directly.
        # This avoids a gap where _refreshing is False between here
        # and _refresh_plot re-setting it — during which the macOS
        # Cocoa event loop could trigger a render of stale artists.
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
        self._clear_handles()
        self._clear_temp()
        modes = {
            MODE_NAV: "Navigate mode - pan/zoom",
            MODE_POLY: "Polygon - click vertices, double-click to close",
            MODE_RECT: "Rectangle - click and drag",
            MODE_ELLIPSE: "Ellipse - click center, drag radius",
            MODE_QUAD: "Quadrant - click to place crosshair, then pick quadrant",
            MODE_MOVE: "Click a gate to select it, then drag handles to resize/rotate",
        }
        self._log(modes.get(label, label))

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

            x_label = self.fcs.display_names()[xi]
            y_label = self.fcs.display_names()[yi]

            self.ax_main.set_navigate(False)

            if self._view_mode == "1D":
                # ── 1D histogram view ──
                self.ax_main.clear()
                if len(x) > 0:
                    self.ax_main.hist(
                        x, bins=256, color="steelblue", edgecolor="none",
                        alpha=0.7, density=True, label="All events",
                    )
                self.ax_main.set_xlabel(x_label)
                self.ax_main.set_ylabel("Density")
                self.ax_main.set_title(f"1D Histogram — {x_label}")

                if self._x_scale == "log":
                    self.ax_main.set_xscale("symlog", linthresh=self._compute_linthresh(x))
                else:
                    self.ax_main.set_xscale("linear")

                # Draw gate ranges as vertical shaded spans
                self._draw_1d_gate_overlays(xn, yn, x, y)
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

                # Draw gates on the current channel pair
                parent_on_view = False
                if self._selected_parent_uid:
                    pg = next((g for g in self.gate_mgr.gates
                               if g.uid == self._selected_parent_uid), None)
                    if pg and pg.x_channel == xn and pg.y_channel == yn:
                        parent_on_view = True

                for gate in self.gate_mgr.gates:
                    if gate.x_channel == xn and gate.y_channel == yn:
                        if parent_on_view:
                            if (gate.uid == self._selected_parent_uid
                                    or gate.parent_gate_uid == self._selected_parent_uid):
                                draw_gate_overlay(self.ax_main, gate)
                        else:
                            draw_gate_overlay(self.ax_main, gate)

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
            lines.append(f"{indent}{s['name']:6s}  {s['count']:>8,}  ({s['percent']:.1f}%)")
        text = "\n".join(lines)
        self.ax_stats.text(
            0.05, 0.95, text, family="monospace", fontsize=8,
            va="top", transform=self.ax_stats.transAxes,
        )

    def _draw_1d_gate_overlays(self, xn: str, yn: str,
                               x: np.ndarray, y: np.ndarray):
        """In 1D histogram mode, overlay per-gate histograms and
        show vertical spans for the X-range of each gate."""
        for gate in self.gate_mgr.gates:
            if gate.x_channel != xn or gate.y_channel != yn:
                continue
            mask = gate.contains(x, y)
            gated_x = x[mask]
            if len(gated_x) == 0:
                continue
            # Overlay the gated histogram
            self.ax_main.hist(
                gated_x, bins=256, color=gate.color,
                edgecolor="none", alpha=0.4, density=True,
                label=gate.name,
            )
            # Vertical span showing gate X range
            xmin_g, xmax_g = float(gated_x.min()), float(gated_x.max())
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
        if event.inaxes != self.ax_main or self.fcs is None:
            return
        if event.xdata is None or event.ydata is None:
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
        elif self._mode == MODE_MOVE:
            self._move_click(event)

    def _on_release(self, event):
        if self._refreshing:
            return
        if event.inaxes != self.ax_main or self.fcs is None:
            return
        if event.xdata is None or event.ydata is None:
            # Cancel in-progress operations when coords are invalid
            self._rect_origin = None
            self._ellipse_origin = None
            self._handle_drag_type = None
            self._handle_drag_start = None
            return
        if self._mode == MODE_RECT and self._rect_origin is not None:
            self._rect_release(event)
        elif self._mode == MODE_ELLIPSE and self._ellipse_origin is not None:
            self._ellipse_release(event)
        elif self._mode == MODE_MOVE and self._handle_drag_type is not None:
            self._move_release(event)

    def _on_motion(self, event):
        if self._refreshing:
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
        elif self._mode == MODE_MOVE and self._handle_drag_type is not None:
            self._move_motion(event)

    def _on_key(self, event):
        if event.key == "enter" and self._mode == MODE_POLY:
            self._close_polygon()

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
            # Approximate bbox ignoring rotation for handle placement
            return (gate.center_x - gate.semi_x, gate.center_x + gate.semi_x,
                    gate.center_y - gate.semi_y, gate.center_y + gate.semi_y)
        elif isinstance(gate, PolygonGate) and gate.vertices:
            xs = [v[0] for v in gate.vertices]
            ys = [v[1] for v in gate.vertices]
            return min(xs), max(xs), min(ys), max(ys)
        elif isinstance(gate, QuadrantGate):
            return gate.mid_x - 1, gate.mid_x + 1, gate.mid_y - 1, gate.mid_y + 1
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
            if gate.contains(np.array([px]), np.array([py]))[0]:
                candidates.append(gate)
        if not candidates:
            return None
        candidates.sort(key=lambda g: len(g.vertices) if g.vertices else 1e9)
        return candidates[0]

    def _compute_handles(self, gate: Gate) -> list[tuple[float, float, str]]:
        """Compute handle positions in data coordinates.

        Returns list of (x, y, handle_type).
        """
        xmin, xmax, ymin, ymax = self._gate_bbox(gate)
        mx = (xmin + xmax) / 2
        my = (ymin + ymax) / 2

        handles = [
            (xmin, ymax, "tl"), (mx, ymax, "t"), (xmax, ymax, "tr"),
            (xmin, my,   "l"),                    (xmax, my,   "r"),
            (xmin, ymin, "bl"), (mx, ymin, "b"), (xmax, ymin, "br"),
        ]

        if not isinstance(gate, QuadrantGate):
            # Rotation handle: above top-center, offset in display space
            # Convert top-center to display, shift up 30px, convert back
            try:
                disp_tc = self.ax_main.transData.transform((mx, ymax))
                disp_rot = (disp_tc[0], disp_tc[1] + 30)
                rx, ry = self.ax_main.transData.inverted().transform(disp_rot)
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
            a0 = np.arctan2(sy - cy, sx - cx)
            a1 = np.arctan2(event.ydata - cy, event.xdata - cx)
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

        sx, sy = self._handle_drag_start
        cx, cy = self._gate_centroid(gate)

        if dtype == "move":
            dx, dy = event.xdata - sx, event.ydata - sy
            if abs(dx) < 1e-12 and abs(dy) < 1e-12:
                return
            self._apply_move(gate, dx, dy)
            self._log(f"Moved gate '{gate.name}'")

        elif dtype == "rot":
            a0 = np.arctan2(sy - cy, sx - cx)
            a1 = np.arctan2(event.ydata - cy, event.xdata - cx)
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
        if isinstance(gate, PolygonGate):
            gate.vertices = [(vx + dx, vy + dy) for vx, vy in gate.vertices]
        elif isinstance(gate, RectangleGate):
            gate.x_min += dx; gate.x_max += dx
            gate.y_min += dy; gate.y_max += dy
        elif isinstance(gate, EllipseGate):
            gate.center_x += dx; gate.center_y += dy
        elif isinstance(gate, QuadrantGate):
            gate.mid_x += dx; gate.mid_y += dy

    def _preview_move(self, gate, dx, dy):
        if isinstance(gate, PolygonGate):
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
    def _apply_rotate(self, gate, delta, cx, cy):
        if isinstance(gate, PolygonGate):
            gate.vertices = [self._rotate_point(vx, vy, cx, cy, delta)
                             for vx, vy in gate.vertices]
        elif isinstance(gate, RectangleGate):
            corners = gate.vertices
            rotated = [self._rotate_point(vx, vy, cx, cy, delta)
                       for vx, vy in corners]
            new_gate = PolygonGate(
                name=gate.name, x_channel=gate.x_channel,
                y_channel=gate.y_channel, vertices=rotated,
                color=gate.color, uid=gate.uid,
                parent_gate_uid=gate.parent_gate_uid,
            )
            idx = next((i for i, g in enumerate(self.gate_mgr.gates)
                        if g.uid == gate.uid), None)
            if idx is not None:
                self.gate_mgr.gates[idx] = new_gate
                self._handle_selected_gate = new_gate
        elif isinstance(gate, EllipseGate):
            gate.angle += delta

    def _preview_rotate(self, gate, delta, cx, cy):
        if isinstance(gate, (PolygonGate, RectangleGate)):
            verts = gate.vertices
            rotated = [self._rotate_point(vx, vy, cx, cy, delta)
                       for vx, vy in verts]
            p = PolyPatch(rotated, closed=True, fill=False,
                          edgecolor=gate.color, lw=1.5, ls="--")
            self.ax_main.add_patch(p); self._temp_artists.append(p)
        elif isinstance(gate, EllipseGate):
            e = EllipsePatch((cx, cy), 2*gate.semi_x, 2*gate.semi_y,
                             angle=np.degrees(gate.angle + delta),
                             fill=False, edgecolor=gate.color, lw=1.5, ls="--")
            self.ax_main.add_patch(e); self._temp_artists.append(e)
        ln = self.ax_main.plot([cx, cx + (cx-cx)], [cy, cy],  # center dot
                               "o", color=gate.color, ms=4, alpha=0.5)[0]
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
        # Close all gate sub-windows
        for uid in list(self._gate_windows.keys()):
            self._close_gate_window(uid)
        self.gate_mgr.clear()
        self._selected_parent_uid = None
        self._refresh_plot()
        self._log("All gates cleared")

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
        small = 0.05
        lw = cw - 2 * small

        y_cur = 0.95

        # X channel
        self.fig.text(rs, y_cur, "X Channel", fontsize=8, fontweight="bold")
        y_cur -= btn_h + 0.005
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
        y_cur -= btn_h + 0.005
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
        y_cur -= btn_h + 0.005
        ax_vm = self.fig.add_axes([rs, y_cur, cw, btn_h])
        self._btn_vm = Button(ax_vm, "View: 2D Scatter")
        self._btn_vm.on_clicked(lambda e: self._toggle_view())

        # --- Tool selector (radio) ---
        y_cur -= 0.01
        self.fig.text(rs, y_cur, "Tool", fontsize=8, fontweight="bold")
        radio_h = 0.12
        y_cur -= radio_h
        self.ax_mode = self.fig.add_axes([rs, y_cur, cw, radio_h])
        self.radio_mode = RadioButtons(
            self.ax_mode,
            [MODE_NAV, MODE_POLY, MODE_RECT, MODE_ELLIPSE, MODE_QUAD, MODE_MOVE],
            active=0,
        )
        self.radio_mode.on_clicked(self._on_mode_change)

        # --- Action buttons ---
        y_cur -= btn_h + 0.01
        ax_rm = self.fig.add_axes([rs, y_cur, cw, btn_h])
        self._btn_remove = Button(ax_rm, "Remove Gate...")
        self._btn_remove.on_clicked(lambda e: self._on_remove_gate())

        y_cur -= btn_h + 0.005
        ax_clr = self.fig.add_axes([rs, y_cur, cw, btn_h])
        self._btn_clear = Button(ax_clr, "Clear Child Gates")
        self._btn_clear.on_clicked(lambda e: self._on_clear_child_gates())

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
        else:
            self._view_mode = "2D"
            self._btn_vm.label.set_text("View: 2D Scatter")
        self._refresh()

    def _on_mode_change(self, label):
        self._mode = label
        self._poly_verts.clear()
        self._rect_origin = None
        self._ellipse_origin = None
        self._handle_selected_gate = None
        self._handle_drag_type = None
        self._handle_drag_start = None
        self._clear_handles()
        self._clear_temp()

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
            if len(x) > 0:
                self.ax.hist(x, bins=256, color="steelblue",
                             edgecolor="none", alpha=0.7, density=True)
            self.ax.set_xlabel(names[xi])
            self.ax.set_ylabel("Density")
            self.ax.set_title(
                f"Gate: {self.gate.name}  ({mask.sum():,} events) — 1D"
            )
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
            for g in self.app.gate_mgr.gates:
                if g.parent_gate_uid == self.gate.uid and g.x_channel == xn and g.y_channel == yn:
                    draw_gate_overlay(self.ax, g)

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
        if event.inaxes != self.ax or self.app.fcs is None:
            return
        if event.xdata is None or event.ydata is None:
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
        elif self._mode == MODE_MOVE:
            self._move_click(event)

    def _on_release(self, event):
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
        elif self._mode == MODE_MOVE and self._handle_drag_type is not None:
            self._move_release(event)

    def _on_motion(self, event):
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
        elif self._mode == MODE_MOVE and self._handle_drag_type is not None:
            self._move_motion(event)

    def _on_key(self, event):
        if event.key == "enter" and self._mode == MODE_POLY:
            self._close_polygon()

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
            a0 = np.arctan2(sy - cy, sx - cx)
            a1 = np.arctan2(event.ydata - cy, event.xdata - cx)
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
            a0 = np.arctan2(sy - cy, sx - cx)
            a1 = np.arctan2(event.ydata - cy, event.xdata - cx)
            delta = a1 - a0
            if abs(delta) < 1e-6:
                return
            # _apply_rotate needs gate_mgr for rect→poly conversion
            self.app._apply_rotate(gate, delta, cx, cy)
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
        if isinstance(gate, (PolygonGate, RectangleGate)):
            verts = gate.vertices
            rotated = [FlowCytApp._rotate_point(vx, vy, cx, cy, delta)
                       for vx, vy in verts]
            p = PolyPatch(rotated, closed=True, fill=False,
                          edgecolor=gate.color, lw=1.5, ls="--")
            self.ax.add_patch(p); self._temp_artists.append(p)
        elif isinstance(gate, EllipseGate):
            e = EllipsePatch((cx, cy), 2*gate.semi_x, 2*gate.semi_y,
                             angle=np.degrees(gate.angle + delta),
                             fill=False, edgecolor=gate.color, lw=1.5, ls="--")
            self.ax.add_patch(e); self._temp_artists.append(e)

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
