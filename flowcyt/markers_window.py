"""
markers_window.py - Editor for fluorophore -> protein marker mappings.

Opens a matplotlib popup with one row per FCS channel:

    [fluorophore]  [marker text box]  [Hide / Show toggle]

The fluorophore is read-only; the marker is editable; the third column is
a button that toggles whether the channel is hidden from the main window's
channel selectors.  Save persists both the marker map *and* the hidden
list to ``<fcs_path>.markers.json`` — the underlying FCS file is never
modified.  Reload from FCS reverts both columns to PnS defaults / un-hidden.
"""

from __future__ import annotations

import logging

import matplotlib.pyplot as plt

from ._widgets import Button, TextBox, install_tk_click_bridge  # macOS-friendly variants
from . import theme as _theme
from .markers import (
    effective_channel_label, load_hidden_channels, load_markers, save_markers,
)

logger = logging.getLogger(__name__)


class MarkersWindow:
    """Modal-ish popup window for editing channel marker mappings."""

    def __init__(self, app):
        self.app = app
        self.fig = None
        self._row_textboxes: dict[str, TextBox] = {}
        self._row_label_axes: dict[str, "plt.Axes"] = {}
        self._row_label_artists: dict[str, "plt.Text"] = {}
        self._row_hide_buttons: dict[str, Button] = {}
        self._draft: dict[str, str] = {}
        # Working copy of the hidden set — committed to disk on Save.
        self._hidden_draft: set[str] = set()
        self._build_ui()

    # ----------------------------------------------------------------- #
    #  Layout
    # ----------------------------------------------------------------- #
    def _build_ui(self):
        if self.app.fcs is None:
            # Tiny "load a file first" placeholder.
            self.fig = plt.figure("Marker Mapping", figsize=(5, 2.5))
            _theme.style_window(self.fig)
            self.fig.clf()
            install_tk_click_bridge(self.fig)
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
        fig_h = max(4.5, min(0.50 * n + 2.4, 13.0))
        self.fig = plt.figure("Marker Mapping", figsize=(8.5, fig_h))
        _theme.style_window(self.fig)
        self.fig.clf()
        install_tk_click_bridge(self.fig)

        title = "Fluorophore → Protein Marker"
        self.fig.text(0.5, 0.97, title, ha="center", fontsize=12, fontweight="bold")
        self.fig.text(
            0.5, 0.935,
            f"File: {self.app.fcs.filepath}",
            ha="center", fontsize=8, color="grey",
        )

        # Column headers.
        header_y = 0.905
        self.fig.text(0.06, header_y, "Fluorophore (FCS PnN)",
                      fontsize=9, fontweight="bold")
        self.fig.text(0.42, header_y, "Marker",
                      fontsize=9, fontweight="bold")
        self.fig.text(0.82, header_y, "Visibility",
                      fontsize=9, fontweight="bold")

        # Each row's vertical span in figure-fraction coordinates.
        top = 0.89
        bottom = 0.13  # leave room for the action buttons
        row_h_total = (top - bottom) / max(1, n)
        row_h = min(0.045, row_h_total * 0.82)
        row_gap = max(0.001, row_h_total - row_h)

        self._row_textboxes = {}
        self._row_label_axes = {}
        self._row_label_artists = {}
        self._row_hide_buttons = {}
        self._draft = {}
        # Take a fresh snapshot of the app's hidden set as our draft.
        self._hidden_draft = set(self.app._hidden_channels or set())

        current_map = self.app._marker_map or {}

        # Column geometry: label | textbox | hide button.
        x_label_l, x_label_r = 0.04, 0.38
        x_tb_l,    x_tb_r    = 0.40, 0.78
        x_hide_l,  x_hide_r  = 0.80, 0.96

        for i, short in enumerate(channels):
            y = top - (i + 1) * row_h - i * row_gap
            is_hidden = short in self._hidden_draft

            # Fluorophore label (read-only axes with text).
            ax_label = self.fig.add_axes([x_label_l, y, x_label_r - x_label_l, row_h])
            ax_label.set_xticks([])
            ax_label.set_yticks([])
            for s in ax_label.spines.values():
                s.set_visible(False)
            label_artist = ax_label.text(
                0.01, 0.5, short, ha="left", va="center",
                fontsize=9, transform=ax_label.transAxes, family="monospace",
                color=("#888888" if is_hidden else "black"),
            )
            self._row_label_axes[short] = ax_label
            self._row_label_artists[short] = label_artist

            # Marker text box.
            ax_tb = self.fig.add_axes([x_tb_l, y, x_tb_r - x_tb_l, row_h])
            initial = current_map.get(short, "")
            tb = TextBox(ax_tb, "", initial=initial, label_pad=0.0)
            tb.label.set_visible(False)
            try:
                tb.text_disp.set_fontsize(9)
                if is_hidden:
                    tb.text_disp.set_color("#888888")
            except Exception:
                pass
            tb.on_submit(self._make_on_submit(short))
            self._row_textboxes[short] = tb
            self._draft[short] = initial

            # Hide / Show toggle button.
            ax_hide = self.fig.add_axes([x_hide_l, y, x_hide_r - x_hide_l, row_h])
            btn_label = "Show" if is_hidden else "Hide"
            btn = Button(ax_hide, btn_label)
            try:
                btn.label.set_fontsize(8)
            except Exception:
                pass
            btn.on_clicked(self._make_on_hide_toggle(short))
            self._row_hide_buttons[short] = btn

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

        # Theme: section headers in accent navy + style every button + textbox.
        for txt in list(self.fig.texts):
            try:
                if txt.get_fontweight() in ("bold", 700, "700"):
                    _theme.style_section_header(txt)
            except Exception:
                pass
        for btn in (self.btn_reload, self.btn_save, self.btn_close,
                    *self._row_hide_buttons.values()):
            _theme.style_button(btn)
        for tb in self._row_textboxes.values():
            _theme.style_textbox(tb)

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

    def _make_on_hide_toggle(self, short: str):
        def _toggle(_event):
            if short in self._hidden_draft:
                self._hidden_draft.discard(short)
            else:
                self._hidden_draft.add(short)
            self._refresh_row_appearance(short)
            self.fig.canvas.draw_idle()
        return _toggle

    def _refresh_row_appearance(self, short: str):
        """Update the visual state of a row after its hidden-ness flips."""
        is_hidden = short in self._hidden_draft
        # Label colour.
        artist = self._row_label_artists.get(short)
        if artist is not None:
            try:
                artist.set_color("#888888" if is_hidden else "black")
            except Exception:
                pass
        # Marker text-box colour.
        tb = self._row_textboxes.get(short)
        if tb is not None:
            try:
                tb.text_disp.set_color("#888888" if is_hidden else "black")
            except Exception:
                pass
        # Toggle button label.
        btn = self._row_hide_buttons.get(short)
        if btn is not None:
            try:
                btn.label.set_text("Show" if is_hidden else "Hide")
            except Exception:
                pass

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
        non_empty = {k: v for k, v in new_map.items() if v}
        try:
            save_markers(
                self.app.fcs.filepath,
                non_empty,
                fcs=self.app.fcs,
                hidden=self._hidden_draft,
            )
        except OSError as e:
            self.app._log(f"Failed to save marker map: {e}")
            return

        # Re-merge with FCS defaults so PnS-derived entries reappear too.
        self.app._marker_map = load_markers(self.app.fcs.filepath, self.app.fcs)
        self.app._hidden_channels = load_hidden_channels(self.app.fcs.filepath)

        # If the user just hid the currently-displayed X or Y channel,
        # auto-switch to the first available non-hidden channel so the
        # plot doesn't get stuck on something they've removed from view.
        self._fix_visible_selection()

        # Rebuild display names + refresh main plot.
        self.app._channel_display_names = [
            effective_channel_label(s, self.app._marker_map)
            for s in self.app.fcs.channel_names
        ]
        self.app._update_channel_labels()
        self.app._refresh_plot()
        n_hidden = len(self.app._hidden_channels)
        if n_hidden:
            self.app._log(f"Saved markers; {n_hidden} channel(s) hidden from selectors.")
        else:
            self.app._log("Saved fluorophore → marker mapping.")
        self.fig.canvas.draw_idle()

    def _fix_visible_selection(self):
        """If the current X / Y channel is now hidden, switch to a visible one."""
        names = self.app.fcs.channel_names
        hidden = self.app._hidden_channels or set()
        visible_idxs = [i for i, s in enumerate(names) if s not in hidden]
        if not visible_idxs:
            return  # everything is hidden — leave selection as-is
        if names[self.app._x_idx] in hidden:
            self.app._x_idx = visible_idxs[0]
        if names[self.app._y_idx] in hidden:
            self.app._y_idx = (
                visible_idxs[1] if len(visible_idxs) > 1 else visible_idxs[0]
            )

    def _on_reload(self, event):
        """Revert TextBoxes to FCS-PnS defaults and un-hide every channel."""
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
        # Clear hidden state and update appearance.
        self._hidden_draft.clear()
        for short in self.app.fcs.channel_names:
            self._refresh_row_appearance(short)
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
