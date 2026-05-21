"""
markers_window.py - Editor for fluorophore -> protein marker mappings.

Opens a matplotlib popup with one row per FCS channel: a read-only fluorophore
label on the left and a TextBox for the marker on the right.  A Save button
persists the edits to ``<fcs_path>.markers.json``, then asks the host
``FlowCytApp`` to refresh its channel selectors and main plot.
"""

from __future__ import annotations

import logging

import matplotlib.pyplot as plt
from matplotlib.widgets import Button, TextBox

from .markers import effective_channel_label, load_markers, save_markers

logger = logging.getLogger(__name__)


class MarkersWindow:
    """Modal-ish popup window for editing channel marker mappings."""

    def __init__(self, app):
        self.app = app
        self.fig = None
        self._row_textboxes: dict[str, TextBox] = {}
        self._draft: dict[str, str] = {}
        self._build_ui()

    # ----------------------------------------------------------------- #
    #  Layout
    # ----------------------------------------------------------------- #
    def _build_ui(self):
        if self.app.fcs is None:
            # Tiny "load a file first" placeholder.
            self.fig = plt.figure("Marker Mapping", figsize=(5, 2.5))
            self.fig.clf()
            ax = self.fig.add_axes([0.05, 0.05, 0.9, 0.9])
            ax.set_xticks([])
            ax.set_yticks([])
            ax.text(
                0.5, 0.5,
                "Load an FCS file first, then reopen this window\n"
                "to edit fluorophore -> marker mappings.",
                ha="center", va="center", fontsize=11, color="grey",
                transform=ax.transAxes,
            )
            self.fig.canvas.mpl_connect("close_event", self._on_close)
            self.fig.canvas.draw_idle()
            return

        channels = list(self.app.fcs.channel_names)
        n = len(channels)
        # Figure height scales with channel count, capped to keep things sane.
        fig_h = max(4.0, min(0.45 * n + 2.2, 12.0))
        self.fig = plt.figure("Marker Mapping", figsize=(7.5, fig_h))
        self.fig.clf()

        title = "Fluorophore → Protein Marker"
        self.fig.text(0.5, 0.97, title, ha="center", fontsize=12, fontweight="bold")
        self.fig.text(
            0.5, 0.935,
            f"File: {self.app.fcs.filepath}",
            ha="center", fontsize=8, color="grey",
        )

        # Column headers.
        header_y = 0.905
        self.fig.text(0.07, header_y, "Fluorophore (FCS PnN)",
                      fontsize=9, fontweight="bold")
        self.fig.text(0.50, header_y, "Marker",
                      fontsize=9, fontweight="bold")

        # Each row's vertical span in figure-fraction coordinates.
        top = 0.89
        bottom = 0.13  # leave room for the action buttons
        row_h_total = (top - bottom) / max(1, n)
        row_h = min(0.045, row_h_total * 0.82)
        row_gap = max(0.001, row_h_total - row_h)

        self._row_textboxes = {}
        self._draft = {}
        current_map = self.app._marker_map or {}

        for i, short in enumerate(channels):
            y = top - (i + 1) * row_h - i * row_gap
            # Fluorophore label (read-only axes with text).
            ax_label = self.fig.add_axes([0.05, y, 0.40, row_h])
            ax_label.set_xticks([])
            ax_label.set_yticks([])
            for s in ax_label.spines.values():
                s.set_visible(False)
            ax_label.text(
                0.01, 0.5, short, ha="left", va="center",
                fontsize=9, transform=ax_label.transAxes, family="monospace",
            )

            # Marker text box.
            ax_tb = self.fig.add_axes([0.47, y, 0.48, row_h])
            initial = current_map.get(short, "")
            tb = TextBox(ax_tb, "", initial=initial, label_pad=0.0)
            tb.label.set_visible(False)
            try:
                tb.text_disp.set_fontsize(9)
            except Exception:
                pass
            tb.on_submit(self._make_on_submit(short))
            self._row_textboxes[short] = tb
            self._draft[short] = initial

        # Action buttons (bottom strip).
        btn_w = 0.20
        btn_h = 0.05
        btn_y = 0.05
        gap = 0.02
        total = btn_w * 3 + gap * 2
        start_x = (1.0 - total) / 2

        ax_reload = self.fig.add_axes([start_x, btn_y, btn_w, btn_h])
        self.btn_reload = Button(ax_reload, "Reload from FCS")
        self.btn_reload.on_clicked(self._on_reload)

        ax_save = self.fig.add_axes([start_x + btn_w + gap, btn_y, btn_w, btn_h])
        self.btn_save = Button(ax_save, "Save")
        self.btn_save.on_clicked(self._on_save)

        ax_close = self.fig.add_axes([start_x + 2 * (btn_w + gap), btn_y, btn_w, btn_h])
        self.btn_close = Button(ax_close, "Close")
        self.btn_close.on_clicked(self._on_close_clicked)

        self.fig.canvas.mpl_connect("close_event", self._on_close)
        self.fig.canvas.draw_idle()

    # ----------------------------------------------------------------- #
    #  External refresh — called when the host app opens a new FCS file
    # ----------------------------------------------------------------- #
    def refresh(self):
        if self.fig is not None:
            try:
                plt.close(self.fig)
            except Exception:
                pass
        self._build_ui()

    # ----------------------------------------------------------------- #
    #  Callbacks
    # ----------------------------------------------------------------- #
    def _make_on_submit(self, short: str):
        def _on_submit(text: str):
            self._draft[short] = (text or "").strip()
        return _on_submit

    def _collect_current_drafts(self) -> dict[str, str]:
        """Read every TextBox's current text (handles unsubmitted edits)."""
        result: dict[str, str] = {}
        for short, tb in self._row_textboxes.items():
            try:
                result[short] = (tb.text or "").strip()
            except Exception:
                result[short] = self._draft.get(short, "")
        return result

    def _on_save(self, event):
        if self.app.fcs is None:
            return
        new_map = self._collect_current_drafts()
        # Drop empty entries so the sidecar stays minimal.
        non_empty = {k: v for k, v in new_map.items() if v}
        try:
            save_markers(self.app.fcs.filepath, non_empty, fcs=self.app.fcs)
        except OSError as e:
            self.app._log(f"Failed to save marker map: {e}")
            return

        # Re-merge with FCS defaults so PnS-derived entries reappear too.
        self.app._marker_map = load_markers(self.app.fcs.filepath, self.app.fcs)

        # Rebuild display names + refresh main plot.
        self.app._channel_display_names = [
            effective_channel_label(s, self.app._marker_map)
            for s in self.app.fcs.channel_names
        ]
        self.app._update_channel_labels()
        self.app._refresh_plot()
        self.app._log("Updated fluorophore → marker mapping.")
        self.fig.canvas.draw_idle()

    def _on_reload(self, event):
        """Revert TextBoxes to the FCS-PnS defaults (deletes overrides)."""
        if self.app.fcs is None:
            return
        # Reset every text box to its PnS default (empty if none).
        for short in self.app.fcs.channel_names:
            label = ""
            try:
                idx = self.app.fcs.channel_names.index(short)
                lbl = self.app.fcs.channel_labels[idx]
                if lbl and lbl != short:
                    label = lbl
            except Exception:
                pass
            tb = self._row_textboxes.get(short)
            if tb is not None:
                try:
                    tb.set_val(label)
                except Exception:
                    pass
            self._draft[short] = label
        self.app._log("Reverted markers display to FCS defaults — click Save to commit.")
        self.fig.canvas.draw_idle()

    def _on_close_clicked(self, event):
        try:
            plt.close(self.fig)
        except Exception:
            pass

    def _on_close(self, event):
        # Clear the host app's reference so the next "Markers" click re-opens.
        if getattr(self.app, "_markers_window", None) is self:
            self.app._markers_window = None
