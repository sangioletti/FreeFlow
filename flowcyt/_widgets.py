"""
_widgets.py - macOS-friendly matplotlib widget subclasses.

Background
----------
On macOS with the TkAgg backend, the system trackpad doesn't always deliver
the canvas ``button_press_event`` for a light/standard click — sometimes
only the ``button_release_event`` makes it through.  A Force Click ("deep
click") reliably delivers both, which is why stock matplotlib widgets feel
like they need a hard press to register on macOS.

Strategy
--------
Each subclass listens to **both** press and release events and fires its
normal action on whichever arrives first, with an ~80 ms debounce so a
normal click (which fires both events ~5–20 ms apart) is treated as a
single click.

The Linux/Windows code paths keep stock matplotlib semantics — we only
swap in the subclasses on ``darwin``.
"""

from __future__ import annotations

import sys
import time

from matplotlib.widgets import (
    Button as _MplButton,
    RadioButtons as _MplRadioButtons,
    TextBox as _MplTextBox,
)


# Debounce window: long enough to swallow the matching press+release of one
# physical click (~5–20 ms apart), short enough that a user can intentionally
# click two different widgets in rapid succession.
_DEBOUNCE_S = 0.08


def _copy_to_clipboard(text: str) -> None:
    """Write *text* to the system clipboard (best effort, no exceptions)."""
    import subprocess
    if sys.platform == "darwin":
        cmd = ["pbcopy"]
    else:
        # Linux: try xclip first, then xsel
        cmd = ["xclip", "-selection", "clipboard"]
    try:
        subprocess.run(cmd, input=text, text=True, check=False, timeout=2.0)
    except FileNotFoundError:
        if sys.platform != "darwin":
            try:
                subprocess.run(
                    ["xsel", "--clipboard", "--input"],
                    input=text, text=True, check=False, timeout=2.0,
                )
            except Exception:
                pass
    except Exception:
        pass


def _make_debouncer():
    """Returns a callable that returns True if the caller should be blocked
    (i.e. fired within ``_DEBOUNCE_S`` of the last firing).
    """
    state = {"last": 0.0}

    def block_or_pass() -> bool:
        now = time.monotonic()
        if now - state["last"] < _DEBOUNCE_S:
            return True
        state["last"] = now
        return False
    return block_or_pass


# Maximum age (seconds) of a press that still "belongs" to a subsequent
# release.  Used only by the *drift-suppression* code path — i.e. when
# the press landed on widget A but the release lands on widget B's axes
# (cursor drifted between press and release).  Generous enough that a
# slow user click that's physically held down for nearly a second
# still suppresses a spurious release-fallback on a neighbouring widget.
# The matching-release-of-our-own-press case is handled separately by
# the ``_mac_fired_on_press`` flag and does NOT depend on this window.
_PRESS_TRACKING_S = 1.0


def _install_canvas_press_tracker(canvas) -> None:
    """Record on the canvas which axes received the most recent press.

    Used by ``_MacButton._release`` to detect "release drifted into a
    different button than the one that was pressed" and suppress the
    spurious fallback fire — without this, clicking X-scale and drifting
    onto Y-scale (or any neighbouring button) would flip *both* widgets.

    Idempotent: the tracker is installed at most once per canvas, even
    if many widgets get constructed on the same figure.
    """
    if canvas is None:
        return
    if getattr(canvas, "_mac_press_tracker_installed", False):
        return

    def _on_press(event):
        canvas._mac_last_press_ax = getattr(event, "inaxes", None)
        canvas._mac_last_press_t = time.monotonic()

    try:
        canvas.mpl_connect("button_press_event", _on_press)
        canvas._mac_press_tracker_installed = True
    except Exception:
        pass


def _press_belonged_to_another_axes(event, my_ax) -> bool:
    """Return True if the most recent press on the canvas landed on a
    *different* axes than ``my_ax``.  Used to decide whether a release
    event arriving at ``my_ax`` should fire as a light-tap fallback.
    """
    canvas = getattr(event, "canvas", None)
    if canvas is None:
        return False
    last_ax = getattr(canvas, "_mac_last_press_ax", None)
    last_t = getattr(canvas, "_mac_last_press_t", 0.0)
    if last_ax is None or last_ax is my_ax:
        return False
    return (time.monotonic() - last_t) < _PRESS_TRACKING_S


# ---------------------------------------------------------------------------- #
#  Button: fires its on_clicked callbacks on press OR release.
# ---------------------------------------------------------------------------- #

class _MacButton(_MplButton):
    """``matplotlib.widgets.Button`` that fires on press OR release.

    Whichever edge of the click arrives first triggers the callback; the
    other one is debounced away.  Works for both light trackpad taps
    (release-only) and Force Clicks (press+release pair).

    Grab semantics are defensive: a leftover grab from another widget
    (typically a ``TextBox`` that never released because focus moved away
    without a release event) doesn't prevent us from firing.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._mac_block = _make_debouncer()
        # Set to True every time ``_click`` (the press handler) fires the
        # user's callback.  The next ``_release`` then consumes it — that
        # release is the natural completion of the same click, so we
        # must not fire the callback a second time.  Without this, holding
        # the trackpad pressed for ~100–300 ms (a normal click duration on
        # a Magic Trackpad) bypasses the short time-based debouncer and
        # toggles the widget twice — visible to the user as e.g. an axis
        # flipping back to linear the instant they release the button.
        self._mac_fired_on_press = False
        _install_canvas_press_tracker(getattr(self.ax.figure, "canvas", None))

    def _click(self, event):
        if (self.ignore(event) or event.inaxes != self.ax or
                not self.eventson):
            return
        # Try to claim the mouse grab, but tolerate a leftover grab held
        # by another axes (e.g. a TextBox).  matplotlib raises a
        # RuntimeError in ``grab_mouse`` if anyone else already holds it
        # — without this defence the chat-window Yes/No buttons crash
        # because the input TextBox's grab is still active.
        if event.canvas.mouse_grabber is None:
            try:
                event.canvas.grab_mouse(self.ax)
            except RuntimeError:
                pass
        if not self._mac_block():
            self._mac_fired_on_press = True
            self._mac_fire(event)

    def _release(self, event):
        # Drop our grab if we hold it.  Don't touch anyone else's grab.
        try:
            if event.canvas.mouse_grabber is self.ax:
                event.canvas.release_mouse(self.ax)
        except Exception:
            pass
        # If the matching press already fired our callback, consume the
        # flag and stop here — this release is just the natural end of
        # the same physical click.
        if self._mac_fired_on_press:
            self._mac_fired_on_press = False
            return
        if (self.ignore(event) or event.inaxes != self.ax or
                not self.eventson):
            return
        # Suppress the release-as-click fallback when the matching press
        # actually landed on a *different* axes — this happens whenever
        # the user clicks near the border between two adjacent buttons
        # and the cursor drifts a few pixels between press and release.
        # Without this, clicking X-scale and drifting onto Y-scale would
        # fire BOTH widgets and flip both scales.
        if _press_belonged_to_another_axes(event, self.ax):
            return
        if not self._mac_block():
            self._mac_fire(event)

    def _mac_fire(self, event):
        # matplotlib >= 3.4 keeps observers under self._observers (CallbackRegistry)
        observers = getattr(self, "_observers", None)
        if observers is not None and hasattr(observers, "process"):
            try:
                observers.process("clicked", event)
                return
            except Exception:
                pass
        # matplotlib 3.0–3.3 fallback (dict of {cid: func}).
        for _cid, func in (getattr(self, "observers", None) or {}).items():
            try:
                func(event)
            except Exception:
                pass


# ---------------------------------------------------------------------------- #
#  RadioButtons: select-on-press OR release, debounced.
# ---------------------------------------------------------------------------- #

class _MacRadioButtons(_MplRadioButtons):
    """``RadioButtons`` whose selection logic fires on press OR release.

    Parent class connects ``_clicked`` to ``button_press_event``.  We also
    connect it to ``button_release_event`` so a light trackpad tap that
    delivers only the release event still selects the radio item.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._mac_block = _make_debouncer()
        # See ``_MacButton._mac_fired_on_press`` — same idea.  Each press
        # selects the radio item once; the matching release must not
        # re-select.
        self._mac_fired_on_press = False
        _install_canvas_press_tracker(getattr(self.ax.figure, "canvas", None))
        # Add a release-edge handler that triggers the same selection logic.
        self.connect_event("button_release_event", self._mac_fallback_clicked)

    def _clicked(self, event):
        # Parent's press handler — debounce and forward.
        if self._mac_block():
            return
        self._mac_fired_on_press = True
        super()._clicked(event)

    def _mac_fallback_clicked(self, event):
        # The matching release of a recent press: consume the flag and
        # exit — the press already did the work.
        if self._mac_fired_on_press:
            self._mac_fired_on_press = False
            return
        if self._mac_block():
            return
        # Suppress if the press belonged to another widget (see _MacButton).
        if _press_belonged_to_another_axes(event, self.ax):
            return
        super()._clicked(event)


# ---------------------------------------------------------------------------- #
#  TextBox: focus on press OR release, debounced.
# ---------------------------------------------------------------------------- #

class _MacTextBox(_MplTextBox):
    """``TextBox`` that focuses (starts typing) on press OR release.

    Parent class focuses in ``_click`` (press) and uses ``_release`` only to
    drop the mouse grab.  We override ``_release`` to additionally re-run
    the focus logic if the press edge was missed (debounced).

    Important: when the fallback ``_click`` fires from the release path,
    we explicitly drop the grab the parent's ``_click`` takes — otherwise
    the TextBox keeps the canvas-wide mouse grab forever and any later
    button click (e.g. the chat Yes/No confirmation) crashes with
    "Another Axes already grabs mouse input".
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._mac_block = _make_debouncer()
        # See ``_MacButton._mac_fired_on_press`` — same idea: track that
        # the press already focused us so the matching release does not
        # re-trigger the focus logic (which would also re-grab the mouse).
        self._mac_fired_on_press = False
        _install_canvas_press_tracker(getattr(self.ax.figure, "canvas", None))

    def _click(self, event):
        if self._mac_block():
            return
        self._mac_fired_on_press = True
        super()._click(event)

    def _release(self, event):
        # Drop the mouse grab as the parent normally does.
        try:
            super()._release(event)
        except Exception:
            pass
        # If the press already focused us, consume the flag and stop —
        # this is just the natural release of the same click.
        if self._mac_fired_on_press:
            self._mac_fired_on_press = False
            return
        if self._mac_block():
            return
        # Suppress if the press belonged to another widget (see _MacButton).
        if _press_belonged_to_another_axes(event, self.ax):
            return
        # Fall back to focusing on release if press was missed.
        try:
            super()._click(event)
            # super()._click grabs the mouse on the textbox.  Since we got
            # here from a release event, there's no follow-up release to
            # drop the grab — drop it ourselves immediately.
            if event.canvas.mouse_grabber is self.ax:
                event.canvas.release_mouse(self.ax)
        except Exception:
            # Don't ever blow up the release path on a focus quirk.
            pass

    # ------------------------------------------------------------------ #
    #  macOS keyboard shortcuts
    # ------------------------------------------------------------------ #
    #
    # matplotlib's TextBox knows only basic editing: typing, backspace,
    # left/right arrows, home/end.  None of the standard macOS shortcuts
    # (Cmd+←, Cmd+→, Cmd+Shift+←, Cmd+Backspace, Cmd+V, …) work because
    # they reach ``_keypress`` as multi-modifier strings the parent
    # ignores.
    #
    # We translate them here:
    #   * Cmd+←              →  jump cursor to start of field         (= home)
    #   * Cmd+→              →  jump cursor to end of field           (= end)
    #   * Cmd+Backspace      →  clear the entire field
    #   * Cmd+Shift+←        →  delete everything before the cursor
    #   * Cmd+Shift+→        →  delete everything after the cursor
    #   * Cmd+A              →  best-effort "select all" (cursor to end)
    #   * Cmd+V              →  paste from the system clipboard
    #   * Option+Backspace   →  delete the word to the left of the cursor
    #
    # Qt-on-macOS reports the ⌘ key as ``"ctrl+"`` and ⌥ as ``"alt+"``
    # (Qt swaps them on Darwin).  Native macosx backend reports them as
    # ``"cmd+"`` / ``"super+"`` / ``"meta+"`` and ``"alt+"``.  We
    # normalise both spellings to a single internal ``"mod+"`` /
    # ``"opt+"`` prefix before dispatching.

    def _keypress(self, event):
        if self.ignore(event) or not self.capturekeystrokes:
            return
        raw = getattr(event, "key", None) or ""
        if not raw:
            return super()._keypress(event)

        key = raw
        if sys.platform == "darwin":
            # Strip a Cmd-ish prefix.  We deliberately match ``ctrl+`` too
            # because the Qt backend reports ⌘ that way on macOS.
            for p in ("cmd+", "super+", "meta+", "ctrl+"):
                if key.startswith(p):
                    key = "mod+" + key[len(p):]
                    break
            # Option key.
            for p in ("alt+",):
                if key.startswith(p) and not key.startswith("alt+gr"):
                    key = "opt+" + key[len(p):]
                    break

        if key == "mod+backspace":
            self._mac_clear_all()
            return
        if key == "mod+left":
            return self._mac_with_key(event, "home")
        if key == "mod+right":
            return self._mac_with_key(event, "end")
        if key in ("mod+shift+left",):
            self._mac_delete_to_start()
            return
        if key in ("mod+shift+right",):
            self._mac_delete_to_end()
            return
        if key == "mod+a":
            # No selection support in matplotlib's TextBox — approximate
            # "select all" by moving the cursor to the end so the next
            # typed character begins a fresh insertion at the tail.
            return self._mac_with_key(event, "end")
        if key == "mod+c":
            self._mac_copy_clipboard()
            return
        if key == "mod+x":
            self._mac_cut_clipboard()
            return
        if key == "mod+v":
            self._mac_paste_clipboard()
            return
        if key == "opt+backspace":
            self._mac_delete_word_left()
            return
        if key == "opt+left":
            self._mac_move_word_left()
            return
        if key == "opt+right":
            self._mac_move_word_right()
            return

        # Default: hand the event to matplotlib's stock handler.
        return super()._keypress(event)

    # ---- helpers used by the shortcut dispatcher ---- #

    def _mac_with_key(self, event, replacement_key: str):
        """Re-dispatch the parent's _keypress with a replaced ``key``."""
        orig = event.key
        try:
            event.key = replacement_key
            return super()._keypress(event)
        finally:
            event.key = orig

    def _mac_clear_all(self):
        try:
            self.set_val("")
        except Exception:
            pass

    def _mac_delete_to_start(self):
        try:
            cur = self.text or ""
            ci = max(0, min(int(getattr(self, "cursor_index", 0)), len(cur)))
            self.set_val(cur[ci:])
            self.cursor_index = 0
            self._rendercursor()
        except Exception:
            pass

    def _mac_delete_to_end(self):
        try:
            cur = self.text or ""
            ci = max(0, min(int(getattr(self, "cursor_index", 0)), len(cur)))
            self.set_val(cur[:ci])
            self.cursor_index = len(self.text or "")
            self._rendercursor()
        except Exception:
            pass

    def _mac_delete_word_left(self):
        try:
            cur = self.text or ""
            ci = max(0, min(int(getattr(self, "cursor_index", 0)), len(cur)))
            i = ci
            # Skip whitespace just before the cursor, then a word.
            while i > 0 and cur[i - 1].isspace():
                i -= 1
            while i > 0 and not cur[i - 1].isspace():
                i -= 1
            new = cur[:i] + cur[ci:]
            self.set_val(new)
            self.cursor_index = i
            self._rendercursor()
        except Exception:
            pass

    def _mac_move_word_left(self):
        try:
            cur = self.text or ""
            i = max(0, min(int(getattr(self, "cursor_index", 0)), len(cur)))
            while i > 0 and cur[i - 1].isspace():
                i -= 1
            while i > 0 and not cur[i - 1].isspace():
                i -= 1
            self.cursor_index = i
            self._rendercursor()
        except Exception:
            pass

    def _mac_move_word_right(self):
        try:
            cur = self.text or ""
            n = len(cur)
            i = max(0, min(int(getattr(self, "cursor_index", 0)), n))
            while i < n and not cur[i].isspace():
                i += 1
            while i < n and cur[i].isspace():
                i += 1
            self.cursor_index = i
            self._rendercursor()
        except Exception:
            pass

    def _mac_copy_clipboard(self):
        """Copy the full input text to the system clipboard."""
        text = self.text or ""
        if not text:
            return
        _copy_to_clipboard(text)

    def _mac_cut_clipboard(self):
        """Cut: copy the full input text to clipboard, then clear."""
        text = self.text or ""
        if text:
            _copy_to_clipboard(text)
        self._mac_clear_all()

    def _mac_paste_clipboard(self):
        try:
            import subprocess
            clip = subprocess.run(
                ["pbpaste"], capture_output=True, text=True,
                check=False, timeout=2.0,
            ).stdout
        except Exception:
            clip = ""
        if not clip:
            return
        # ``pbpaste`` includes a trailing newline for paragraph copies; we
        # want a clean single-line insertion.
        clip = clip.replace("\r\n", "\n").replace("\r", "\n").replace("\n", " ")
        try:
            cur = self.text or ""
            ci = max(0, min(int(getattr(self, "cursor_index", 0)), len(cur)))
            new = cur[:ci] + clip + cur[ci:]
            self.set_val(new)
            self.cursor_index = ci + len(clip)
            self._rendercursor()
        except Exception:
            pass


# ---------------------------------------------------------------------------- #
#  Public aliases — swap in the macOS-friendly classes on darwin.
# ---------------------------------------------------------------------------- #

if sys.platform == "darwin":
    Button = _MacButton
    RadioButtons = _MacRadioButtons
    TextBox = _MacTextBox
else:
    Button = _MplButton
    RadioButtons = _MplRadioButtons
    TextBox = _MplTextBox


# ---------------------------------------------------------------------------- #
#  Tk-level click bridge (macOS Force Touch workaround)
# ---------------------------------------------------------------------------- #

def install_tk_click_bridge(fig) -> None:
    """Bind Tk mouse-button events directly to the canvas widget and
    synthesise matplotlib events from them.

    Why this exists
    ---------------
    On macOS with a Magic Trackpad / Force Touch trackpad, matplotlib's
    TkAgg backend frequently fails to dispatch ``button_press_event`` /
    ``button_release_event`` for a light click — only a Force Click ("deep
    click") reliably reaches matplotlib's widget callbacks.  By binding
    directly to the underlying Tk widget's ``<ButtonPress-1>`` and
    ``<ButtonRelease-1>`` events (the same trick the codebase already
    uses for Tab via ``_disable_tk_tab_traversal``), we sidestep whatever
    filtering happens above matplotlib and get every click Tk sees.

    Double-firing
    -------------
    On platforms where matplotlib already dispatches the event normally,
    each Tk click now arrives twice at the widget — once via matplotlib's
    own dispatch and once via this bridge.  The ``_DEBOUNCE_S`` window in
    the ``_MacButton`` / ``_MacRadioButtons`` / ``_MacTextBox`` subclasses
    swallows the duplicate, so net behaviour stays "one fire per click".
    """
    if sys.platform != "darwin":
        return
    canvas = getattr(fig, "canvas", None)
    if canvas is None or not hasattr(canvas, "get_tk_widget"):
        return  # Not running under TkAgg (e.g. Agg in tests).

    try:
        tk_widget = canvas.get_tk_widget()
    except Exception:
        return

    from matplotlib.backend_bases import MouseEvent, MouseButton

    def _dispatch(event_name, tk_event):
        # Tk's coordinate origin is top-left; matplotlib's is bottom-left.
        try:
            height = tk_widget.winfo_height()
        except Exception:
            return
        try:
            x = float(tk_event.x)
            y = float(height - tk_event.y)
        except Exception:
            return
        try:
            mpl_event = MouseEvent(
                event_name, canvas, x, y, button=MouseButton.LEFT,
            )
        except Exception:
            return
        try:
            canvas.callbacks.process(event_name, mpl_event)
        except Exception:
            pass

    def _on_press(tk_event):
        _dispatch("button_press_event", tk_event)

    def _on_release(tk_event):
        _dispatch("button_release_event", tk_event)

    # ``add="+"`` keeps any existing bindings — including matplotlib's own
    # — so we never *remove* events that already work; we only add a
    # parallel path that catches anything matplotlib's dispatch misses.
    try:
        tk_widget.bind("<ButtonPress-1>",   _on_press,   add="+")
        tk_widget.bind("<ButtonRelease-1>", _on_release, add="+")
    except Exception:
        pass


__all__ = ["Button", "RadioButtons", "TextBox", "install_tk_click_bridge"]
