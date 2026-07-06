"""
chat_window.py - DeepSeek-powered chat assistant window.

A separate matplotlib popup that lets the user converse with DeepSeek to
drive gating operations.  Features:

    * API-key entry mode shown automatically when no key is configured.
    * Header strip showing remaining DeepSeek balance and per-session cost.
    * Scrollable transcript (mouse wheel) with monospace formatting and a
      separate visual treatment for user / assistant / tool-call / result.
    * TextBox + Send button (also Enter-submits via TextBox.on_submit).
    * In-line Yes/No confirmation for destructive tool calls (clear/remove/
      export); the agentic loop blocks on the user's choice.
"""

from __future__ import annotations

import logging
import textwrap

import matplotlib.pyplot as plt

from ._widgets import Button, TextBox, install_tk_click_bridge  # macOS-friendly variants
from . import theme as _theme
from . import tools as tools_mod
from .llm import (
    DeepSeekClient, DeepSeekError,
    load_api_key, save_api_key_to_home, HOME_KEY_PATH,
)

logger = logging.getLogger(__name__)


# Rendering style per event kind.
_STYLE = {
    "user":         ("You",            "#0a3d62"),
    "assistant":    ("Assistant",      "#1e1e1e"),
    "tool_call":    ("→ tool",         "#7b6f00"),
    "tool_result":  ("← result",       "#0a6c0a"),
    "tool_error":   ("← error",        "#9b1c1c"),
    "info":         ("info",           "#555555"),
    "confirm":      ("confirm",        "#a05a00"),
}


class ChatWindow:
    """DeepSeek chat assistant popup window."""

    def __init__(self, app):
        self.app = app
        self.fig = None

        # Rendering state.
        self._events: list[dict] = []   # what is currently shown in history
        self._scroll_offset: int = 0    # lines from bottom (0 = stick to end)
        self._max_line_chars: int = 84

        # Confirmation state — when set, _confirm_buttons are shown.
        self._pending_confirm: dict | None = None
        self._confirm_result: bool | None = None

        # Balance display state.
        self._balance_text = None
        self._session_text = None

        self._build_ui()

    # ----------------------------------------------------------------- #
    #  Layout
    # ----------------------------------------------------------------- #
    def _build_ui(self):
        api_key = load_api_key()
        # Slightly larger figure so the input field and transcript
        # comfortably hold long lines of text.
        self.fig = plt.figure("FlowCyt Assistant", figsize=(11, 9.5))
        _theme.style_window(self.fig)
        self.fig.clf()
        self.fig.canvas.mpl_connect("close_event", self._on_close)
        install_tk_click_bridge(self.fig)

        if not api_key:
            self._build_key_entry()
            self.fig.canvas.draw_idle()
            return

        self._init_client(api_key)
        self._build_chat_ui()
        self._seed_welcome()
        self._refresh_balance(initial=True)
        self.fig.canvas.draw_idle()

    def _init_client(self, api_key: str):
        try:
            self.app._llm_client = DeepSeekClient(api_key)
        except Exception as e:
            logger.exception("Failed to construct DeepSeek client")
            self.app._llm_client = None
            self._append_event({"kind": "info",
                                "content": f"Could not initialise DeepSeek client: {e}"})

    # ----- key entry mode --------------------------------------------- #
    def _build_key_entry(self):
        self.fig.text(
            0.5, 0.85, "DeepSeek API Key",
            ha="center", fontsize=14, fontweight="bold",
        )
        self.fig.text(
            0.5, 0.78,
            "No DeepSeek API key was found.\n"
            f"After saving, your key will be stored at:\n{HOME_KEY_PATH}\n"
            "You can also set $DEEPSEEK_API_KEY or drop a `deepseek_api_key`\n"
            "file in the current folder.",
            ha="center", va="top", fontsize=9, color="#444",
        )

        ax_tb = self.fig.add_axes([0.10, 0.55, 0.80, 0.06])
        self._key_textbox = TextBox(ax_tb, "Key ", initial="", label_pad=0.02)
        self._key_textbox.on_submit(lambda _t: self._on_save_key(None))

        ax_save = self.fig.add_axes([0.35, 0.42, 0.30, 0.07])
        self.btn_save_key = Button(ax_save, "Save key")
        self.btn_save_key.on_clicked(self._on_save_key)
        _theme.style_button(self.btn_save_key)
        _theme.style_textbox(self._key_textbox)

        self.fig.text(
            0.5, 0.30,
            "(your key is stored locally with permissions 0600)",
            ha="center", fontsize=8, color="grey",
        )

    def _on_save_key(self, _event):
        try:
            key = (self._key_textbox.text or "").strip()
        except Exception:
            key = ""
        if not key:
            return
        try:
            path = save_api_key_to_home(key)
        except Exception as e:
            logger.exception("Failed to save API key")
            self.fig.text(
                0.5, 0.20, f"Could not save key: {e}",
                ha="center", color="red", fontsize=9,
            )
            self.fig.canvas.draw_idle()
            return
        self.app._log(f"Saved DeepSeek API key to {path}")

        # Swap to chat UI in place.
        self.fig.clf()
        self.fig.canvas.mpl_connect("close_event", self._on_close)
        self._init_client(key)
        self._build_chat_ui()
        self._seed_welcome()
        self._refresh_balance(initial=True)
        self.fig.canvas.draw_idle()

    # ----- chat mode -------------------------------------------------- #
    def _build_chat_ui(self):
        # Header strip (top): balance + session + refresh button.
        self._ax_header = self.fig.add_axes([0.02, 0.93, 0.96, 0.05])
        self._ax_header.set_xticks([])
        self._ax_header.set_yticks([])
        for s in self._ax_header.spines.values():
            s.set_visible(False)

        self._balance_text = self._ax_header.text(
            0.01, 0.5, "Balance: ...", va="center", ha="left",
            fontsize=9, transform=self._ax_header.transAxes,
        )
        self._session_text = self._ax_header.text(
            0.50, 0.5, "Session: $0.0000 (0 in / 0 out)",
            va="center", ha="left", fontsize=9,
            transform=self._ax_header.transAxes,
        )

        ax_refresh = self.fig.add_axes([0.85, 0.935, 0.12, 0.04])
        self.btn_refresh = Button(ax_refresh, "Refresh $")
        self.btn_refresh.on_clicked(lambda _e: self._refresh_balance())

        # History axes.
        self._ax_history = self.fig.add_axes([0.02, 0.17, 0.96, 0.74])
        self._ax_history.set_xticks([])
        self._ax_history.set_yticks([])
        for s in self._ax_history.spines.values():
            s.set_color("#cccccc")

        # Input row (TextBox + Send + status). Slightly taller and using a
        # smaller font so users see what they type even on long messages.
        self._ax_input = self.fig.add_axes([0.02, 0.045, 0.82, 0.07])
        self._textbox = TextBox(self._ax_input, "", initial="", label_pad=0.0)
        self._textbox.label.set_visible(False)
        try:
            self._textbox.text_disp.set_fontsize(10)
            # Anchor the input text at top-left of the field so the cursor
            # and most-recent characters stay visible when text overflows.
            self._textbox.text_disp.set_horizontalalignment("left")
            self._textbox.text_disp.set_verticalalignment("center")
        except Exception:
            pass
        self._textbox.on_submit(self._on_send_submit)

        ax_send = self.fig.add_axes([0.86, 0.045, 0.12, 0.07])
        self.btn_send = Button(ax_send, "Send")
        self.btn_send.on_clicked(self._on_send_click)

        # Theme: tint balance/session header text + style buttons + tint
        # the history axes with the soft panel background.
        for txt in (self._balance_text, self._session_text):
            if txt is not None:
                try:
                    txt.set_color(_theme.PALETTE["accent"])
                    txt.set_fontweight("bold")
                except Exception:
                    pass
        _theme.style_button(self.btn_send)
        _theme.style_button(getattr(self, "btn_refresh", None))
        _theme.style_textbox(self._textbox)
        if getattr(self, "_ax_history", None) is not None:
            try:
                self._ax_history.set_facecolor("white")
                for sp in ("top", "right", "bottom", "left"):
                    self._ax_history.spines[sp].set_edgecolor(_theme.PALETTE["border"])
                    self._ax_history.spines[sp].set_linewidth(0.8)
            except Exception:
                pass

        # Status line (just above the input row).
        self._ax_status = self.fig.add_axes([0.02, 0.125, 0.96, 0.03])
        self._ax_status.set_xticks([])
        self._ax_status.set_yticks([])
        for s in self._ax_status.spines.values():
            s.set_visible(False)
        self._status_text = self._ax_status.text(
            0.01, 0.5, "", va="center", ha="left", fontsize=8,
            color="#666", transform=self._ax_status.transAxes,
        )

        # Mouse-wheel scrolling on the history axes.
        self.fig.canvas.mpl_connect("scroll_event", self._on_scroll)

        # Containers for the (optional) confirmation buttons.
        self._confirm_yes_ax = None
        self._confirm_no_ax = None
        self._confirm_yes_btn = None
        self._confirm_no_btn = None

        self._render_history()

    def _seed_welcome(self):
        self._append_event({"kind": "info", "content":
            "Hello — describe what you want to do (e.g. \"draw a polygon around "
            "the CD4+ population on FSC vs SSC\"). The marker map is shown in "
            "system context automatically."})

    # ----------------------------------------------------------------- #
    #  Event rendering
    # ----------------------------------------------------------------- #
    def _append_event(self, event: dict):
        self._events.append(event)
        self._scroll_offset = 0   # snap to end
        self._render_history()

    def _render_history(self):
        if not hasattr(self, "_ax_history") or self._ax_history is None:
            return
        ax = self._ax_history
        ax.clear()
        ax.set_xticks([])
        ax.set_yticks([])
        for s in ax.spines.values():
            s.set_color("#cccccc")
        ax.set_xlim(0, 1)

        # Build wrapped lines with associated colors.
        lines: list[tuple[str, str]] = []  # (text, color)
        for ev in self._events:
            kind = ev.get("kind", "info")
            if kind == "tool_result" and ev.get("is_error"):
                kind = "tool_error"
            label, color = _STYLE.get(kind, _STYLE["info"])
            content = self._format_event_content(ev)
            wrapped = self._wrap_content(label, content)
            for ln in wrapped:
                lines.append((ln, color))

        # Compute line height from the actual font size so consecutive
        # lines don't overlap.  Convert ``fontsize * linespacing`` points
        # into axes-fraction units via the axes' real height on the figure.
        fontsize = 9.0
        linespacing = 1.45
        line_h_pts = fontsize * linespacing
        fig_h_in = self.fig.get_figheight()
        ax_h_frac = ax.get_position().height
        ax_h_pts = max(ax_h_frac * fig_h_in * 72.0, 1.0)
        line_step = line_h_pts / ax_h_pts

        # How many lines can we comfortably display?
        max_visible = max(5, int((1.0 - 0.04) / line_step))
        n_lines = len(lines)
        if n_lines <= max_visible:
            visible = lines
        else:
            end = n_lines - self._scroll_offset
            end = max(max_visible, min(end, n_lines))
            start = max(0, end - max_visible)
            visible = lines[start:end]

        y = 0.98
        for text, color in visible:
            ax.text(
                0.01, y, text,
                family="monospace", fontsize=fontsize,
                va="top", ha="left", color=color,
                transform=ax.transAxes,
            )
            y -= line_step

        # Scroll hint.
        if n_lines > max_visible:
            shown_to = n_lines - self._scroll_offset
            ax.text(
                0.99, 0.01,
                f"{shown_to}/{n_lines} (wheel to scroll)",
                ha="right", va="bottom", fontsize=7, color="#888",
                transform=ax.transAxes,
            )
        self.fig.canvas.draw_idle()

    def _format_event_content(self, ev: dict) -> str:
        kind = ev.get("kind", "info")
        if kind == "tool_call":
            args = ev.get("arguments") or {}
            try:
                import json as _json
                payload = _json.dumps(args, default=str)
            except Exception:
                payload = str(args)
            return f"{ev.get('name')}({payload})"
        return str(ev.get("content", ""))

    def _wrap_content(self, label: str, content: str) -> list[str]:
        prefix = f"{label:<10}: "
        cont = " " * len(prefix)
        out: list[str] = []
        # Preserve embedded newlines.
        for i, raw_line in enumerate(content.split("\n")):
            wrapped = textwrap.wrap(
                raw_line, width=self._max_line_chars,
                initial_indent=(prefix if i == 0 else cont),
                subsequent_indent=cont,
                break_long_words=False, drop_whitespace=False,
            ) or [(prefix if i == 0 else cont)]
            out.extend(wrapped)
        return out

    def _on_scroll(self, event):
        if event.inaxes is not self._ax_history:
            return
        step = 3
        if event.button == "up":
            self._scroll_offset += step
        else:
            self._scroll_offset = max(0, self._scroll_offset - step)
        self._render_history()

    # ----------------------------------------------------------------- #
    #  Balance / cost display
    # ----------------------------------------------------------------- #
    def _refresh_balance(self, initial: bool = False):
        if self.app._llm_client is None or self._balance_text is None:
            return
        try:
            balance = self.app._llm_client.get_balance()
        except DeepSeekError as e:
            self._balance_text.set_text(f"Balance: (unavailable: {e.__class__.__name__})")
            self.fig.canvas.draw_idle()
            if initial:
                self._append_event({"kind": "info",
                    "content": f"Could not fetch balance: {e}"})
            return
        amount, currency = DeepSeekClient.primary_balance(balance)
        is_available = balance.get("is_available")
        flag = "" if is_available is None else (" ✓" if is_available else " ⚠")
        self._balance_text.set_text(f"Balance: {amount:.4f} {currency}{flag}")
        self._update_session_text()
        self.fig.canvas.draw_idle()

    def _update_session_text(self):
        if self._session_text is None:
            return
        self._session_text.set_text(
            f"Session: ${self.app._session_cost_usd:.4f} "
            f"({self.app._session_tokens_in} in / "
            f"{self.app._session_tokens_out} out)"
        )

    # ----------------------------------------------------------------- #
    #  Send flow
    # ----------------------------------------------------------------- #
    def _on_send_submit(self, text: str):
        self._send(text)

    def _on_send_click(self, _event):
        try:
            text = self._textbox.text or ""
        except Exception:
            text = ""
        self._send(text)

    def _send(self, raw_text: str):
        text = (raw_text or "").strip()
        if not text:
            return
        if self.app._llm_client is None:
            self._append_event({"kind": "info",
                "content": "DeepSeek client is not initialised."})
            return
        # Clear input.
        try:
            self._textbox.set_val("")
        except Exception:
            pass

        # Drop any mouse grab the TextBox is still holding from typing,
        # so destructive-confirm buttons spawned mid-turn don't crash.
        self._release_any_mouse_grab()

        self._set_status("Thinking…")
        try:
            usage = tools_mod.run_chat_turn(
                app=self.app,
                client=self.app._llm_client,
                user_message=text,
                history=self.app._chat_history,
                on_event=self._append_event,
                confirm_destructive=self._confirm_destructive,
            )
        except Exception as e:
            logger.exception("Chat turn crashed")
            self._append_event({"kind": "info",
                "content": f"Internal error during chat: {e}"})
            self._set_status("")
            return

        # Accumulate session totals.
        self.app._session_cost_usd += float(usage.get("cost_usd") or 0.0)
        self.app._session_tokens_in += int(usage.get("tokens_in") or 0)
        self.app._session_tokens_out += int(usage.get("tokens_out") or 0)
        self._update_session_text()
        self._set_status("")
        # Refresh balance every turn — cheap, and keeps the user honest.
        self._refresh_balance()

    def _set_status(self, text: str):
        if getattr(self, "_status_text", None) is None:
            return
        self._status_text.set_text(text)
        self.fig.canvas.draw_idle()
        try:
            # Let the canvas paint immediately so users see "Thinking…".
            self.fig.canvas.flush_events()
        except Exception:
            pass

    # ----------------------------------------------------------------- #
    #  Destructive-action confirmation
    # ----------------------------------------------------------------- #
    def _confirm_destructive(self, tool_name: str, summary: str, args: dict) -> bool:
        """Render Yes/No buttons in the input row and block until clicked."""
        self._pending_confirm = {"name": tool_name, "summary": summary, "args": args}
        self._confirm_result = None

        # Critical: clear any leftover mouse grab (typically held by the
        # input TextBox from the last time the user typed) before showing
        # the Yes/No buttons. Without this, the Yes button's _click raises
        # "Another Axes already grabs mouse input" and the chat locks up.
        self._release_any_mouse_grab()

        # Show the confirmation entry in the transcript.
        self._append_event({
            "kind": "confirm",
            "content": f"Confirm `{tool_name}` → {summary}  (Yes / No)",
        })

        # Hide the normal Send button area; show Yes / No.
        try:
            self.btn_send.ax.set_visible(False)
        except Exception:
            pass
        try:
            self._ax_input.set_visible(False)
        except Exception:
            pass

        self._confirm_yes_ax = self.fig.add_axes([0.30, 0.045, 0.18, 0.07])
        self._confirm_yes_btn = Button(self._confirm_yes_ax, "Yes")
        self._confirm_yes_btn.on_clicked(lambda _e: self._resolve_confirm(True))

        self._confirm_no_ax = self.fig.add_axes([0.52, 0.045, 0.18, 0.07])
        self._confirm_no_btn = Button(self._confirm_no_ax, "No")
        self._confirm_no_btn.on_clicked(lambda _e: self._resolve_confirm(False))

        self.fig.canvas.draw_idle()
        try:
            self.fig.canvas.start_event_loop(timeout=0)  # block until stop_event_loop
        except Exception:
            # Fallback: poll plt.pause until resolved.
            while self._confirm_result is None:
                plt.pause(0.05)

        result = bool(self._confirm_result)

        # Drop any grab the Yes/No buttons might have taken so the next
        # input click works.
        self._release_any_mouse_grab()

        # Tear down confirmation widgets and restore the Send row.
        for ax in (self._confirm_yes_ax, self._confirm_no_ax):
            if ax is not None:
                try:
                    self.fig.delaxes(ax)
                except Exception:
                    pass
        self._confirm_yes_ax = self._confirm_no_ax = None
        self._confirm_yes_btn = self._confirm_no_btn = None
        try:
            self._ax_input.set_visible(True)
            self.btn_send.ax.set_visible(True)
        except Exception:
            pass
        self._pending_confirm = None
        self.fig.canvas.draw_idle()
        return result

    def _release_any_mouse_grab(self):
        """Drop whichever axes currently holds the canvas mouse grab.

        matplotlib's ``release_mouse(ax)`` is a no-op if ``ax`` isn't the
        current grabber, so the only safe call is to release the actual
        grabber.  Safe to call when nothing holds the grab.
        """
        try:
            canvas = self.fig.canvas
            grabber = canvas.mouse_grabber
            if grabber is not None:
                canvas.release_mouse(grabber)
        except Exception:
            pass

    def _resolve_confirm(self, value: bool):
        self._confirm_result = value
        try:
            self.fig.canvas.stop_event_loop()
        except Exception:
            pass

    # ----------------------------------------------------------------- #
    #  Close
    # ----------------------------------------------------------------- #
    def _on_close(self, _event):
        if getattr(self.app, "_chat_window", None) is self:
            self.app._chat_window = None
