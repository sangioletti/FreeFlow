"""
app.py - Interactive matplotlib GUI for FlowCyt.

Uses matplotlib widgets (RadioButtons, Button) and event handlers
so there is NO dependency on tkinter, Qt, or any other GUI framework.
The user's native matplotlib backend (TkAgg, QtAgg, etc.) is used
automatically.

Features:
  * Density-coloured scatter of any two channels
  * Polygon gating (left-click vertices, right-click / Enter to close)
  * Rectangle gating (click-drag)
  * Live stats printed to the console & shown on-plot
  * Summary window (bar chart + histograms)
  * CSV export of gated events
"""

from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

import numpy as np
import matplotlib
import matplotlib.pyplot as plt
from matplotlib.widgets import RadioButtons, Button
from matplotlib.patches import Rectangle as RectPatch, Polygon as PolyPatch

from .reader import FCSData
from .gating import GateManager
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

        # Parent gate selection for sub-gating
        self._selected_parent_uid: str | None = None

        self._build_ui()

        if filepath:
            self._open_file(filepath)
        else:
            self.ax_main.text(
                0.5, 0.5,
                "No file loaded.\nPress [Open FCS] or re-run with  -i file.fcs",
                ha="center", va="center", fontsize=13,
                transform=self.ax_main.transAxes, color="grey",
            )
            print("\n[FlowCyt] Welcome! Features:")
            print("  • Polygon: Preview lines + double-click to close")
            print("  • Ellipse: Click-drag for elliptical gates")
            print("  • Sub-gating: Select parent in 'Parent Gate' before creating child")
            print("  • Auto Cluster: Automatic gate creation (requires scikit-learn)")
            print("\nPress [Open FCS] to load a file, or it will auto-load test_sample.fcs\n")
            self.fig.canvas.draw_idle()

    # ================================================================ #
    #  UI layout
    # ================================================================ #
    def _build_ui(self):
        self.fig = plt.figure("FlowCyt", figsize=(13, 8))
        self.fig.subplots_adjust(left=0.30, right=0.98, top=0.93, bottom=0.08)

        # Main scatter axes
        self.ax_main = self.fig.add_axes([0.30, 0.08, 0.67, 0.83])

        # --- Left panel: channel selectors + mode + buttons -----------
        # X channel selector
        self.fig.text(0.015, 0.92, "X channel", fontsize=10, fontweight="bold")
        self.ax_xsel = self.fig.add_axes([0.01, 0.58, 0.17, 0.33])
        self.ax_xsel.set_frame_on(False)

        # Y channel selector
        self.fig.text(0.015, 0.55, "Y channel", fontsize=10, fontweight="bold")
        self.ax_ysel = self.fig.add_axes([0.01, 0.22, 0.17, 0.33])
        self.ax_ysel.set_frame_on(False)

        # Mode selector
        self.fig.text(0.185, 0.92, "Tool", fontsize=10, fontweight="bold")
        self.ax_mode = self.fig.add_axes([0.185, 0.72, 0.10, 0.19])
        self.radio_mode = RadioButtons(
            self.ax_mode, [MODE_NAV, MODE_POLY, MODE_RECT, MODE_ELLIPSE], active=0,
        )
        self.radio_mode.on_clicked(self._on_mode_change)

        # Parent gate selector (for sub-gating)
        self.fig.text(0.185, 0.68, "Parent Gate", fontsize=10, fontweight="bold")
        self.ax_parent = self.fig.add_axes([0.185, 0.52, 0.10, 0.15])
        self.ax_parent.set_frame_on(False)
        self._radio_parent = None  # Will be updated when gates exist

        # Buttons
        self.ax_btn_clear = self.fig.add_axes([0.185, 0.44, 0.10, 0.05])
        self.btn_clear = Button(self.ax_btn_clear, "Clear Gates")
        self.btn_clear.on_clicked(lambda e: self._on_clear_gates())

        self.ax_btn_summary = self.fig.add_axes([0.185, 0.37, 0.10, 0.05])
        self.btn_summary = Button(self.ax_btn_summary, "Summary")
        self.btn_summary.on_clicked(lambda e: self._on_show_summary())

        self.ax_btn_export = self.fig.add_axes([0.185, 0.30, 0.10, 0.05])
        self.btn_export = Button(self.ax_btn_export, "Export CSV")
        self.btn_export.on_clicked(lambda e: self._on_export_csv())

        self.ax_btn_open = self.fig.add_axes([0.185, 0.23, 0.10, 0.05])
        self.btn_open = Button(self.ax_btn_open, "Open FCS")
        self.btn_open.on_clicked(lambda e: self._on_open_dialog())

        self.ax_btn_cluster = self.fig.add_axes([0.185, 0.16, 0.10, 0.05])
        self.btn_cluster = Button(self.ax_btn_cluster, "Auto Cluster")
        self.btn_cluster.on_clicked(lambda e: self._on_auto_cluster())

        # Stats text area (bottom-left)
        self.ax_stats = self.fig.add_axes([0.01, 0.02, 0.26, 0.18])
        self.ax_stats.set_frame_on(True)
        self.ax_stats.set_xticks([])
        self.ax_stats.set_yticks([])
        self.ax_stats.set_title("Gate Statistics", fontsize=9, loc="left")

        # Connect canvas events
        self.fig.canvas.mpl_connect("button_press_event", self._on_click)
        self.fig.canvas.mpl_connect("button_release_event", self._on_release)
        self.fig.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.fig.canvas.mpl_connect("key_press_event", self._on_key)

        # Placeholder radio buttons (will be replaced on file load)
        self._radio_x = None
        self._radio_y = None

    # ================================================================ #
    #  File loading
    # ================================================================ #
    def _on_open_dialog(self):
        """Open file via dialog or find test file."""
        path = None
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            path = filedialog.askopenfilename(
                filetypes=[("FCS files", "*.fcs"), ("All", "*.*")]
            )
            root.destroy()
        except Exception as e:
            print(f"[FlowCyt] File dialog not available: {e}")
            # Try to find test_sample.fcs
            test_paths = [
                "test_sample.fcs",
                "./test_sample.fcs",
                os.path.join(os.path.dirname(__file__), "..", "test_sample.fcs"),
            ]
            for test_path in test_paths:
                if os.path.isfile(test_path):
                    path = test_path
                    print(f"[FlowCyt] Auto-loading: {path}")
                    break

        if path and os.path.isfile(path):
            self._open_file(path)
        else:
            print("[FlowCyt] No file selected or found.")

    def _open_file(self, path: str):
        try:
            self.fcs = FCSData(path)
        except Exception as exc:
            print(f"[FlowCyt] Error loading {path}: {exc}", file=sys.stderr)
            return

        self.gate_mgr.clear()
        print(self.fcs.summary())

        # Rebuild channel selectors
        names = self.fcs.display_names()
        # Limit labels to fit the panel (truncate long names)
        short = [n[:28] for n in names]

        # X channel radio
        self.ax_xsel.clear()
        self.ax_xsel.set_frame_on(False)
        self._radio_x = RadioButtons(self.ax_xsel, short, active=0)
        self._radio_x.on_clicked(self._on_channel_change)

        # Y channel radio
        self.ax_ysel.clear()
        self.ax_ysel.set_frame_on(False)
        active_y = min(1, len(names) - 1)
        self._radio_y = RadioButtons(self.ax_ysel, short, active=active_y)
        self._radio_y.on_clicked(self._on_channel_change)

        self._x_idx = 0
        self._y_idx = active_y
        self._refresh_plot()

    # ================================================================ #
    #  Channel / mode callbacks
    # ================================================================ #
    def _on_channel_change(self, label):
        if self.fcs is None:
            return
        short = [n[:28] for n in self.fcs.display_names()]
        if label in short:
            idx = short.index(label)
            # Figure out which radio was clicked by checking active
            if self._radio_x and label == self._radio_x.value_selected:
                self._x_idx = idx
            if self._radio_y and label == self._radio_y.value_selected:
                self._y_idx = idx
        self._refresh_plot()

    def _on_mode_change(self, label):
        self._mode = label
        self._poly_verts.clear()
        self._rect_origin = None
        self._clear_temp()
        modes = {
            MODE_NAV: "Navigate — pan/zoom normally.",
            MODE_POLY: "Polygon gate — left-click vertices, double-click / right-click / Enter to close.",
            MODE_RECT: "Rectangle gate — click and drag.",
            MODE_ELLIPSE: "Ellipse gate — click for center, drag to define radius.",
        }
        print(f"[FlowCyt] {modes.get(label, label)}")

    # ================================================================ #
    #  Plotting
    # ================================================================ #
    def _current_xy(self):
        xi, yi = self._x_idx, self._y_idx
        return (
            xi, yi,
            self.fcs.channel_names[xi],
            self.fcs.channel_names[yi],
        )

    def _refresh_plot(self):
        if self.fcs is None:
            return
        xi, yi, xn, yn = self._current_xy()
        x = self.fcs.data[:, xi]
        y = self.fcs.data[:, yi]

        density_scatter(self.ax_main, x, y)
        self.ax_main.set_xlabel(self.fcs.display_names()[xi])
        self.ax_main.set_ylabel(self.fcs.display_names()[yi])

        for gate in self.gate_mgr.gates:
            if gate.x_channel == xn and gate.y_channel == yn:
                draw_gate_overlay(self.ax_main, gate)

        self._refresh_stats()
        self._refresh_parent_selector()
        self.fig.canvas.draw_idle()

    def _refresh_parent_selector(self):
        """Update parent gate selector with current gates."""
        self.ax_parent.clear()
        self.ax_parent.set_frame_on(False)

        had_gates = self._radio_parent is not None

        if not self.gate_mgr.gates:
            self._radio_parent = None
            self._selected_parent_uid = None
            return

        # Build list: "None" + gate names
        options = ["None"] + [g.name for g in self.gate_mgr.gates]
        self._radio_parent = RadioButtons(self.ax_parent, options, active=0)
        self._radio_parent.on_clicked(self._on_parent_change)
        self._selected_parent_uid = None

        # Show message when parent selector first appears
        if not had_gates and self.gate_mgr.gates:
            print("[FlowCyt] Tip: Use 'Parent Gate' selector (left panel) to create sub-gates")

    def _on_parent_change(self, label):
        """Handle parent gate selection change."""
        if label == "None":
            self._selected_parent_uid = None
            print("[FlowCyt] Sub-gating: Creating root-level gates (no parent)")
        else:
            # Find gate with this name
            for gate in self.gate_mgr.gates:
                if gate.name == label:
                    self._selected_parent_uid = gate.uid
                    print(f"[FlowCyt] Sub-gating: New gates will be children of '{label}'")
                    break

    def _refresh_stats(self):
        self.ax_stats.clear()
        self.ax_stats.set_xticks([])
        self.ax_stats.set_yticks([])
        self.ax_stats.set_title("Gate Statistics", fontsize=9, loc="left")

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
            # Indent child gates
            indent = "  " if s.get("parent_uid") else ""
            lines.append(f"{indent}{s['name']:6s}  {s['count']:>8,}  ({s['percent']:.1f}%)")
        text = "\n".join(lines)
        self.ax_stats.text(
            0.05, 0.95, text, family="monospace", fontsize=8,
            va="top", transform=self.ax_stats.transAxes,
        )

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
        if event.inaxes != self.ax_main or self.fcs is None:
            return
        if self._mode == MODE_POLY:
            self._poly_click(event)
        elif self._mode == MODE_RECT:
            if event.button == 1:
                self._rect_origin = (event.xdata, event.ydata)
        elif self._mode == MODE_ELLIPSE:
            if event.button == 1:
                self._ellipse_origin = (event.xdata, event.ydata)

    def _on_release(self, event):
        if event.inaxes != self.ax_main or self.fcs is None:
            return
        if self._mode == MODE_RECT and self._rect_origin is not None:
            self._rect_release(event)
        elif self._mode == MODE_ELLIPSE and self._ellipse_origin is not None:
            self._ellipse_release(event)

    def _on_motion(self, event):
        if event.inaxes != self.ax_main or self.fcs is None:
            return
        if self._mode == MODE_RECT and self._rect_origin is not None:
            self._rect_motion(event)
        elif self._mode == MODE_POLY and len(self._poly_verts) >= 1:
            self._poly_motion(event)
        elif self._mode == MODE_ELLIPSE and self._ellipse_origin is not None:
            self._ellipse_motion(event)

    def _on_key(self, event):
        if event.key == "enter" and self._mode == MODE_POLY:
            self._close_polygon()

    # -- Polygon -------------------------------------------------------
    def _poly_motion(self, event):
        """Show preview line from last vertex to cursor, and closing line."""
        if event.xdata is None or event.ydata is None:
            return

        # Clear previous preview (but keep vertex markers and committed lines)
        # We'll redraw them, but this prevents accumulation
        temp_to_keep = []
        for artist in self._temp_artists:
            # Keep markers (points) and solid/dashed lines (committed edges)
            # Remove dotted lines (preview lines)
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
        if event.button == 1:
            import time
            current_time = time.time()

            # Check for double-click (within 300ms)
            if len(self._poly_verts) >= 3 and (current_time - self._poly_last_click_time) < 0.3:
                # Double-click detected, close polygon
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
            print("[FlowCyt] Need >= 3 vertices for a polygon gate.")
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

    # -- Rectangle -----------------------------------------------------
    def _rect_motion(self, event):
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

    # -- Ellipse -------------------------------------------------------
    def _ellipse_motion(self, event):
        """Draw preview ellipse during drag."""
        if event.xdata is None or event.ydata is None:
            return

        self._clear_temp()
        cx, cy = self._ellipse_origin
        dx = abs(event.xdata - cx)
        dy = abs(event.ydata - cy)

        # Create ellipse patch for preview
        from matplotlib.patches import Ellipse as EllipsePatch
        ellipse = EllipsePatch(
            (cx, cy), 2 * dx, 2 * dy, angle=0,
            fill=False, edgecolor="red", lw=1.5, linestyle="--"
        )
        self.ax_main.add_patch(ellipse)
        self._temp_artists.append(ellipse)
        self.fig.canvas.draw_idle()

    def _ellipse_release(self, event):
        """Finalize ellipse gate."""
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

    def _print_gate_created(self, gate):
        stats = self.gate_mgr.compute_stats(
            self.fcs.data, self.fcs.channel_names
        )
        for s in stats:
            if s["uid"] == gate.uid:
                parent_info = ""
                if gate.parent_gate_uid:
                    # Find parent name
                    parent = next((g for g in self.gate_mgr.gates if g.uid == gate.parent_gate_uid), None)
                    if parent:
                        parent_info = f" (child of {parent.name})"
                print(
                    f"[FlowCyt] Gate '{gate.name}'{parent_info}: "
                    f"{s['count']:,} events ({s['percent']:.2f}%)"
                )

    # ================================================================ #
    #  Actions
    # ================================================================ #
    def _on_clear_gates(self):
        self.gate_mgr.clear()
        self._refresh_plot()
        print("[FlowCyt] All gates cleared.")

    def _on_show_summary(self):
        if self.fcs is None:
            return

        # Close any existing "FlowCyt Summary" windows
        # Find all figures with this title and close them
        for fignum in plt.get_fignums():
            fig = plt.figure(fignum)
            if fig.canvas.manager.get_window_title() == "FlowCyt Summary":
                plt.close(fig)

        stats = self.gate_mgr.compute_stats(
            self.fcs.data, self.fcs.channel_names
        )
        xi, yi, xn, yn = self._current_xy()

        # Create new figure without storing reference
        # Let matplotlib manage it
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

        # Text table
        axes[1, 1].axis("off")
        if stats:
            cell_text = [
                [s["name"], f"{s['count']:,}", f"{s['percent']:.2f}%"]
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
        plt.show(block=False)  # Non-blocking show

    def _on_export_csv(self):
        if self.fcs is None:
            print("[FlowCyt] No file loaded.")
            return
        stats = self.gate_mgr.compute_stats(
            self.fcs.data, self.fcs.channel_names
        )
        if not stats:
            print("[FlowCyt] Define at least one gate before exporting.")
            return

        # Try tkinter file dialog; fall back to console
        outpath = None
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            outpath = filedialog.asksaveasfilename(
                defaultextension=".csv",
                filetypes=[("CSV", "*.csv")],
            )
            root.destroy()
        except ImportError:
            base = Path(self.fcs.filepath).stem
            outpath = f"{base}_gated.csv"

        if not outpath:
            return

        with open(outpath, "w", newline="") as f:
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
        print(f"[FlowCyt] Exported gated events to {outpath}")

    def _on_auto_cluster(self):
        """Show clustering dialog and create gates from clusters."""
        if self.fcs is None:
            print("[FlowCyt] No file loaded.")
            return

        # Default to KMeans with 3 clusters for simplicity
        algorithm = "kmeans"
        params = {"n_clusters": 3}

        # Try tkinter dialog
        try:
            import tkinter as tk
            from tkinter import simpledialog, messagebox

            root = tk.Tk()
            root.withdraw()

            # Ask for algorithm
            algo = messagebox.askquestion(
                "Clustering Algorithm",
                "Use DBSCAN? (Yes) or KMeans? (No)\n\n(Default: KMeans with 3 clusters)"
            )

            if algo == "yes":
                eps = simpledialog.askfloat(
                    "DBSCAN", "Epsilon (distance threshold):",
                    initialvalue=0.5, minvalue=0.01, maxvalue=10.0
                )
                if eps is not None:
                    algorithm = "dbscan"
                    params = {"eps": eps, "min_samples": 5}
            else:
                n_clusters = simpledialog.askinteger(
                    "KMeans", "Number of clusters:",
                    initialvalue=3, minvalue=2, maxvalue=20
                )
                if n_clusters is not None:
                    algorithm = "kmeans"
                    params = {"n_clusters": n_clusters}

            root.destroy()
        except Exception as e:
            print(f"[FlowCyt] Dialog not available: {e}")
            print(f"[FlowCyt] Using default: KMeans with 3 clusters")

        self._perform_clustering(algorithm, params)

    def _perform_clustering(self, algorithm: str, params: dict):
        """Execute clustering and create gates."""
        print(f"[FlowCyt] Starting clustering: {algorithm} with params {params}")

        try:
            from .clustering import (
                cluster_dbscan,
                cluster_kmeans,
                create_gate_polygons,
            )
            print("[FlowCyt] Clustering modules imported successfully")
        except ImportError as e:
            print(
                f"[FlowCyt] ERROR: Clustering requires scikit-learn and scipy.\n"
                f"Run: pip install scikit-learn scipy\n"
                f"Error: {e}"
            )
            import traceback
            traceback.print_exc()
            return

        xi, yi, xn, yn = self._current_xy()
        x = self.fcs.data[:, xi]
        y = self.fcs.data[:, yi]

        print(f"[FlowCyt] Clustering {len(x)} points on channels {xn} vs {yn}")

        try:
            if algorithm == "dbscan":
                print(f"[FlowCyt] Running DBSCAN...")
                labels = cluster_dbscan(x, y, **params)
            elif algorithm == "kmeans":
                print(f"[FlowCyt] Running KMeans...")
                labels = cluster_kmeans(x, y, **params)
            else:
                print(f"[FlowCyt] ERROR: Unknown algorithm: {algorithm}")
                return

            unique_labels = set(labels)
            print(f"[FlowCyt] Found {len(unique_labels)} unique labels: {sorted(unique_labels)}")

        except Exception as e:
            print(f"[FlowCyt] ERROR: Clustering failed: {e}")
            import traceback
            traceback.print_exc()
            return

        # Create gates from clusters
        try:
            print(f"[FlowCyt] Creating gate polygons from clusters...")
            gate_defs = create_gate_polygons(x, y, labels, xn, yn)
            print(f"[FlowCyt] Created {len(gate_defs)} gate definitions")
        except Exception as e:
            print(f"[FlowCyt] ERROR: Gate creation failed: {e}")
            import traceback
            traceback.print_exc()
            return

        if not gate_defs:
            print("[FlowCyt] No clusters found (gate_defs is empty).")
            return

        # Add gates to manager
        try:
            for i, gate_def in enumerate(gate_defs):
                print(f"[FlowCyt] Adding gate {i+1}/{len(gate_defs)}: {gate_def['name']}")
                self.gate_mgr.add_polygon_gate(**gate_def)
        except Exception as e:
            print(f"[FlowCyt] ERROR: Failed to add gates: {e}")
            import traceback
            traceback.print_exc()
            return

        self._refresh_plot()
        print(f"[FlowCyt] SUCCESS: Created {len(gate_defs)} gates from clustering!")

    # ================================================================ #
    #  Run
    # ================================================================ #
    def run(self):
        plt.show()
