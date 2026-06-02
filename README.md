# FreeFlow — FCS Viewer & Gating Tool

FreeFlow is a lightweight, scriptable, and conversational flow-cytometry
viewer. It opens raw `.fcs` files, lets you draw polygon / rectangle /
ellipse / quadrant / threshold gates by mouse, edit them after the fact,
attach protein-marker labels to fluorophores, save and reload entire
gating strategies, and — if you provide a DeepSeek API key — also lets
you do all of the above by chatting in plain English.

Everything is pure Python on top of NumPy and Matplotlib, with one HTTP
client (`requests`) for the optional chat assistant.

---

## Table of contents

1. [Installation](#installation)
2. [Launching the app](#launching-the-app)
3. [The interface at a glance](#the-interface-at-a-glance)
4. [Right-panel controls](#right-panel-controls)
   - [File / channel selectors](#file--channel-selectors)
   - [Scale and view-mode toggles](#scale-and-view-mode-toggles)
   - [Tool selector — drawing & editing modes](#tool-selector--drawing--editing-modes)
   - [Parent Gate selector — hierarchical gating](#parent-gate-selector--hierarchical-gating)
   - [Action buttons](#action-buttons)
   - [Message log](#message-log)
5. [Gate sub-windows](#gate-sub-windows)
6. [Marker mapping window](#marker-mapping-window)
7. [Save / Load gating strategy](#save--load-gating-strategy)
8. [Chat assistant (DeepSeek)](#chat-assistant-deepseek)
9. [Keyboard shortcuts](#keyboard-shortcuts)
10. [Files written next to your data](#files-written-next-to-your-data)
11. [Troubleshooting](#troubleshooting)
12. [Project layout](#project-layout)

---

## Installation

FreeFlow runs on **macOS, Linux, and Windows**.  Pick the path that
matches your platform and comfort level with Python.

### For Windows users — download the executable (no Python needed)

The simplest option on Windows is to grab the prebuilt `.exe`:

1. Open the project's **Releases** page on GitHub.
2. Download `FreeFlow.exe` from the latest release.
3. Double-click it. That's it — no Python, NumPy, or matplotlib install
   required. Drop `.fcs` files in the same folder as the `.exe` (or
   anywhere — use the **File** picker once it's running).

If you'd rather build the `.exe` yourself (or you're on a corporate
machine that blocks downloads), see
[Building the Windows executable from source](#building-the-windows-executable-from-source)
further down.

### For macOS / Linux — install from source

#### Requirements

- Python **≥ 3.9**
- NumPy ≥ 1.22
- Matplotlib ≥ 3.5
- `requests` ≥ 2.28 *(only used by the chat assistant; harmless if you never open the chat)*
- A GUI matplotlib backend — one of:
  - **Qt** (`PyQt5` or `PySide6`) — recommended on macOS; gives the best
    trackpad and keyboard behaviour, and the most reliable Save As dialog.
  - **Tk** (ships with most Python distributions) — used as a fallback.

```bash
git clone <repo-url> freeflow
cd freeflow/FreeFlow            # the inner directory that contains setup.py
pip install -e .                # editable install creates the 'flowcyt' command
```

This installs `numpy`, `matplotlib`, and `requests`. If you don't
already have a Qt binding, install one for the best experience:

```bash
pip install PyQt5               # or:  pip install PySide6
```

#### Run without installing

```bash
cd FreeFlow
pip install -r requirements.txt
python -m flowcyt.cli -i path/to/sample.fcs
```

#### Verify the install

```bash
flowcyt --info Test_sample.fcs
```

This prints the FCS file metadata and exits — no GUI. If you see the
channel list, you're good.

### Building the Windows executable from source

The repository ships a PyInstaller specification (`freeflow.spec`) that
bundles the whole app — Python interpreter, NumPy, matplotlib, Qt, the
DeepSeek client — into one standalone `FreeFlow.exe`.

On a Windows machine:

```powershell
# One-time setup
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install PyQt5 pyinstaller

# Build
pyinstaller --clean --noconfirm freeflow.spec

# Run
.\dist\FreeFlow.exe
```

This typically produces a `~50–80 MB` single file.  No installation,
no PATH changes — copy `dist\FreeFlow.exe` anywhere and double-click.

The build also runs automatically on the project's CI: every Git tag
of the form `vX.Y.Z` triggers
`.github/workflows/build-windows.yml`, which produces a fresh
`FreeFlow.exe` on a Windows runner and attaches it to the matching
GitHub Release. End users just download the file from the Releases
page — no compilation needed on their side.

---

## Launching the app

```bash
flowcyt                                  # GUI, no file pre-loaded
flowcyt -i path/to/sample.fcs            # open a file directly
flowcyt --info path/to/sample.fcs        # print metadata only, no GUI
```

When no file is given, FreeFlow scans the current directory (one level
deep) for `.fcs` files. The first one becomes the active file; use the
File selector arrows to step through the others.

A log file `flowcyt.log` is written in the working directory each
launch. Useful when reporting bugs.

---

## The interface at a glance

```
┌──────────────────────────────────────┬──────────────┐
│                                      │  File        │
│                                      │  X channel   │
│        Main scatter / 1-D plot       │  Y channel   │
│                                      │  Scale  View │
│                                      ├──────────────┤
│                                      │  Tool        │
│                                      │  Parent gate │
├──────────────────────────────────────┤              │
│                                      │  Action      │
│        Gate Statistics panel         │  buttons     │
│                                      ├──────────────┤
│                                      │  Message log │
└──────────────────────────────────────┴──────────────┘
```

- **Left side**: the active plot (2-D density scatter or 1-D histogram)
  on top, and a text statistics panel below it.
- **Right side**: all controls — file/channel pickers, scale and view
  toggles, the tool selector, parent-gate radio, action buttons, and a
  rolling message log of everything the app has done.

The plot updates *live* as you change channels, toggle scales, draw
gates, or run actions from the chat assistant.

---

## Right-panel controls

### File / channel selectors

Each selector is an arrow-button pair around a clickable label:

| Control | What clicking the label does |
|---|---|
| **File** | Opens a popup list of every `.fcs` file in the current scan directory. `< >` step through them in order. |
| **X channel** | Opens a popup list of channels for the X axis. `< >` cycle through them, **skipping any channel you've marked hidden** in the Markers window. |
| **Y channel** | Same as X but for the Y axis. |

Channel labels show the fluorophore short name and (when defined) the
protein marker in parentheses, e.g. `FL1-A (CD4)`.

### Scale and view-mode toggles

- **X: Linear / X: Log** — flips the X axis between linear and `symlog`
  (handles zero / negative values gracefully). Independent of Y.
- **Y: Linear / Y: Log** — same for Y.
- **View: 2D Scatter / 1D Histogram** — switches the plot type.
  - In 2D mode you see a density-coloured scatter plot.
  - In 1D mode you see a histogram of the X channel. Gates are shown as
    vertical shaded spans where their X range overlaps the histogram.
  - In 1D Navigate mode you can **click-and-drag** on the plot to
    compress the axis around an anchor; **double-click** resets it.

### Tool selector — drawing & editing modes

| Mode | What clicks do |
|---|---|
| **Navigate** | Pan / zoom only, no gate interaction. Default. |
| **Polygon** | Left-click places vertices, with a dotted preview line to the cursor. **Double-click** (or press Enter) closes the polygon and creates the gate. |
| **Rectangle** | Click-drag a rectangle. Release commits the gate. |
| **Ellipse** | Click-drag from the centre outward; release commits. |
| **Quadrant** | Click anywhere to place the crosshair, then a small popup picks which quadrant (Q1–Q4) is the "selected" one. All four quadrants always show their event counts and percentages on the plot — the selected one is shaded and bold. |
| **1D Gate** | Threshold gate — click on the X axis at the threshold value, popup picks "left" or "right" side. |
| **Translate** | Click on an existing gate to select it, then drag the centre handle to move it. |
| **Rotate** | Click on a gate to select, drag the rotation handle to rotate around its centroid. |
| **Stretch** | Edit individual control points of a gate. **Click** any control point to grab it, then drag — or use **Tab** to cycle between points and the **arrow keys** to nudge the selected one. On a log axis the keyboard step is constant *in log space* — the visible distance is the same wherever you press. |

### Parent Gate selector — hierarchical gating

After at least one gate exists, the Parent Gate radio appears with
`None` plus every existing gate name. Pick a parent and **all gates you
draw next will be children of it** — their event counts are computed
inside the parent, their masks are intersected with the parent's, and
they appear indented in the Gate Statistics panel.

Pick `None` to go back to drawing root-level gates.

When a parent gate is selected, the main plot zooms / filters to that
parent's events so you can gate finer populations.

### Action buttons

The right panel has a stack of action buttons, then a two-by-two grid:

| Button | Action |
|---|---|
| **Scan Directory...** | Re-scan the parent of the current directory for `.fcs` files (lets you walk up one level and pick siblings). |
| **Summary** | Opens a separate window with a bar chart of every gate's % population and per-channel histograms. |
| **Export CSV** | Writes every gated event to `<file>_gated_<timestamp>.csv` in the project folder, one row per gate membership, full channel values. |
| **Rename Gate...** | Popup list → text input → renames the chosen gate. |
| **Remove Gate...** | Popup list → removes the chosen gate (and any of its sub-windows). |
| **Clear All Gates** | Deletes every gate. Tracked in the undo history, so Ctrl/Cmd+Z restores them. |
| **Save Plot...** | Saves the *main plot axes* as PNG / PDF / SVG. Uses the system's native Save As dialog (Qt's `NSSavePanel` on macOS, Tk's `asksaveasfilename` otherwise). |
| **Save Gates** *(left of pair)* | Saves the entire current gating strategy (geometry, parent-child links, names, colours) to `<fcs_path>.gates.json` next to the FCS file. |
| **Load Gates** *(right of pair)* | Replaces the current gates with the strategy stored in `<fcs_path>.gates.json`. Warns in the message log if any saved gate references a channel that isn't in the loaded file. |
| **Chat** *(left of pair)* | Opens the DeepSeek chat assistant window. See [Chat assistant](#chat-assistant-deepseek). |
| **Markers** *(right of pair)* | Opens the fluorophore → marker mapping editor. See [Marker mapping](#marker-mapping-window). |

### Message log

Bottom-right corner. Everything the app does — file loads, gate
creations, channel changes, exports, chat tool calls, errors — is
echoed here. The newest messages stay at the top. Also written to
`flowcyt.log` for post-mortem debugging.

---

## Gate sub-windows

Every gate you create — by mouse, by the chat assistant, or via
**Load Gates** — opens its own **sub-window** showing only the events
inside that gate. The sub-window has the same controls as the main
window (channel selectors, tool selector, scale toggles, etc.) so you
can immediately gate child populations on different channels.

Editing the *parent* gate on the main window (translate, rotate,
stretch, rename, remove, …) **propagates live into the sub-window**:
the displayed events, counts, and per-quadrant labels all update
without you having to refresh anything.

Close a sub-window any time; it doesn't delete the gate. You can
re-open it by removing-and-recreating, or just keep working on the
main window.

---

## Marker mapping window

Click **Markers**. You get a popup with one row per channel in the
loaded file:

```
Fluorophore (FCS PnN)     Marker             Visibility
FSC-A                     FSC - Area         [ Hide ]
SSC-A                     SSC - Area         [ Hide ]
FL1-A                                        [ Hide ]
FL2-A                                        [ Hide ]
...                                          [ Reload from FCS ]
                                             [ Save ]  [ Close ]
```

- **Fluorophore** — the short name from the FCS file's PnN keyword,
  read-only.
- **Marker** — editable text. Pre-filled from the FCS PnS keyword when
  one is present (e.g. `"FSC - Area"`). Type your own protein-marker
  label (`CD4`, `CD8`, etc.) to override.
- **Hide / Show** — toggles whether the channel is offered in the X / Y
  channel selectors. Hidden channels:
  - Are skipped by the `< >` arrows.
  - Don't appear in the channel popup list (the current selection stays
    visible if it's the hidden one, with a `(hidden)` annotation, so you
    can still navigate away from it).
  - Stay loaded in the FCS data — you're only hiding them from the UI.

Click **Save** to persist your edits to `<fcs_path>.markers.json` next
to the FCS file. Reopening the same file restores them automatically.

**Reload from FCS** discards every override and goes back to the FCS
file's own PnS labels and an empty hidden list. Click **Save** after
to commit the reset.

The FCS file itself is **never modified**.

The marker map is also injected into every chat request, so you can say
"draw a polygon around the CD4+ population" and DeepSeek knows which
channel that means.

---

## Save / Load gating strategy

The **Save Gates** button writes every gate currently in the manager
to `<fcs_path>.gates.json`. The JSON includes:

- Format version + a timestamp + the source FCS file basename, for
  forward-compatibility.
- One entry per gate with its geometry, channel references (by name),
  colour, and `parent_gate_uid` link to preserve the hierarchy.

**Load Gates** reads that sidecar back, clears the current strategy,
and reconstructs every gate — including the parent-child relationships.
A warning shows in the message log if any gate references channels that
aren't in the loaded FCS file (so a strategy saved on one panel can be
re-applied to another file with matching channel names).

If the channel names of two files don't match, the gates still load —
they just won't draw on the plot until you switch to channels that
match.

---

## Chat assistant (DeepSeek)

Click **Chat** to open the DeepSeek-powered assistant window. You can
type natural-language requests like "select FSC vs SSC and create a
polygon around the main lymphocyte population", or "list all my gates
and tell me the smallest", and the assistant calls the same gating
APIs the GUI uses.

### API-key configuration

The first time you click Chat, the window asks for a DeepSeek API key.
Resolution order on subsequent launches:

1. Environment variable `DEEPSEEK_API_KEY`.
2. A file called `deepseek_api_key` (or `.deepseek_api_key`) in the
   directory you launched FreeFlow from.
3. `~/.freeflow/deepseek_api_key` (where the chat window writes it
   when you save it from the in-app entry form, mode `0600`).

If none of these contain a key, the chat opens in "Enter your key"
mode. Saving writes it to `~/.freeflow/deepseek_api_key`.

### Chat window layout

```
Balance: $0.4123 USD ✓     Session: $0.0012 (1432 in / 87 out)    [ Refresh $ ]
┌────────────────────────────────────────────────────────────────────┐
│ info  : Hello — describe what you want to do…                      │
│ You   : draw a polygon around the CD4+ CD8- population             │
│ → tool: select_channels({"x_channel": "CD4", "y_channel": "CD8"})  │
│ ← result: Selected channels: X=FL1-A, Y=FL2-A.                     │
│ → tool: create_polygon_gate({...})                                 │
│ ← result: Created polygon gate 'CD4+CD8-' on FL1-A × FL2-A.        │
│ Assist: Done — the gate covers the upper-left quadrant.            │
└────────────────────────────────────────────────────────────────────┘
Thinking…
┌──────────────────────────────────────────────────────┬───────────┐
│  <your input here>                                   │   Send    │
└──────────────────────────────────────────────────────┴───────────┘
```

- **Balance** is fetched from DeepSeek on open and after each turn.
- **Session** tracks the cumulative cost of the current chat session
  (estimated from `usage` tokens in each response, against the rates in
  `flowcyt/llm.py:PRICING`).
- **Mouse wheel** scrolls the transcript.
- **Enter** in the input field submits the message; **Send** does the
  same. The input clears automatically after you send.

### What the assistant can do

The assistant has the following tools available, grouped by safety:

**Additive — auto-executed:**
`create_polygon_gate`, `create_rectangle_gate`, `create_ellipse_gate`,
`create_quadrant_gate`, `create_threshold_gate`, `select_channels`,
`set_parent_gate`, `set_axis_scale`, `rename_gate`.

**Read-only — auto-executed:**
`list_channels`, `list_gates`, `get_channel_range`, `summarise_state`.

**Destructive — require explicit Yes/No confirmation:**
`remove_gate`, `clear_all_gates`, `export_csv`.

When the assistant tries a destructive action, the **Send** button is
replaced with **Yes / No** buttons. You click; the assistant either
proceeds and reports the outcome, or apologises and stops.

### Implicit context

Every request automatically includes:

- The loaded file path and event/channel counts.
- The full fluorophore → marker mapping, so you can refer to channels
  by their marker.
- The currently selected X / Y channels and their axis scales.
- The currently selected parent gate.
- Every existing gate with its type and parent.

You don't have to remind the assistant what's on screen.

It's also instructed to **preserve your axis scales** — if you've set
X to log, it picks gate coordinates appropriate to a log view and
doesn't silently flip the axis back to linear.

---

## Keyboard shortcuts

### Anywhere

| Key | Action |
|---|---|
| **Ctrl/Cmd + Z** | Undo |
| **Ctrl/Cmd + Shift + Z** | Redo |
| **Enter** | Close polygon (Polygon mode) / submit text input |
| **Esc** | Cancel polygon in progress |

### In Stretch mode

| Key | Action |
|---|---|
| **Tab** | Cycle to the next control point |
| **← / →** | Nudge the selected point left / right (constant step in log space when X is log) |
| **↑ / ↓** | Nudge the selected point up / down (constant step in log space when Y is log) |
| **Shift + arrows** | Finer nudge (1/5 of the normal step) — useful for precise positioning |

### In any text input field (Mac)

The standard macOS shortcuts work:

| Shortcut | Action |
|---|---|
| **Cmd + ←** | Jump to start of field |
| **Cmd + →** | Jump to end of field |
| **Cmd + Backspace** | Clear the entire field |
| **Cmd + Shift + ←** | Delete everything before the cursor |
| **Cmd + Shift + →** | Delete everything after the cursor |
| **Cmd + V** | Paste from system clipboard |
| **Option + Backspace** | Delete the word to the left |
| **Option + ← / →** | Move cursor to start / past end of the adjacent word |

---

## Files written next to your data

FreeFlow never modifies your `.fcs` file. Anything you save lives in
small JSON sidecars beside it:

| File | What's in it |
|---|---|
| `<sample>.fcs.markers.json` | Fluorophore → marker overrides and the list of hidden channels |
| `<sample>.fcs.gates.json` | Full gating strategy: every gate's geometry, channels, colour, and parent-child links |
| `<sample>_gated_<timestamp>.csv` | Output of the **Export CSV** button — gated events with all channel values |
| `flowcyt.log` | Rolling debug log written to the current directory each launch |
| `~/.freeflow/deepseek_api_key` | DeepSeek API key entered through the chat window (chmod 600) |

Delete any of these any time. The marker / gate sidecars are
re-created when you next click Save in the corresponding window.

---

## Troubleshooting

**Buttons need a "deep click" on the Magic Trackpad**
You're probably on the old TkAgg backend. Install PyQt5 or PySide6
(`pip install PyQt5`) and FreeFlow will switch to QtAgg automatically,
which uses native macOS event handling and treats every press the same
way Finder does.

**`No python 3.11 installed` on launch**
This is the matplotlib `macosx` backend trying to re-exec through
`pythonw` on Anaconda Python. Install PyQt5 (`pip install PyQt5`);
FreeFlow's backend selection prefers Qt and skips the offending path.

**Save Plot writes a file but it isn't an image**
Fixed in current versions — the renderer is forced to draw before
`get_renderer()` is called, and `bbox_inches="tight"` is used as a
fallback if the per-axes bbox computation fails. If you're still seeing
this, please send the contents of `flowcyt.log`.

**Chat says "Could not fetch balance"**
Either the API key is invalid, your network is blocked, or DeepSeek is
down. The chat still works — it just can't show the balance. Type a
message; the response itself will surface any auth error.

**Stretching a gate moves the point unevenly on a log axis**
Each arrow press uses a step that's constant *in log space*, anchored
at the point being moved. If you see linear behaviour, double-check
that the relevant scale toggle actually says "Log" (the button label
changes after a click).

**The Parent Gate panel shows tick numbers**
You're on a very old build — the empty parent-gate axes used to leak
its default 0.0–1.0 ticks. Pull the latest code; the fix is in
`_build_ui` via `xaxis.set_visible(False)`.

---

## Project layout

```
FreeFlow/
├── flowcyt/
│   ├── __init__.py
│   ├── _widgets.py           # macOS-friendly Button / RadioButtons / TextBox subclasses
│   ├── app.py                # The main GUI (FlowCytApp + GateWindow)
│   ├── chat_window.py        # DeepSeek chat popup
│   ├── cli.py                # `flowcyt` command-line entry point
│   ├── gate_io.py            # Save / load gating strategies as JSON sidecars
│   ├── gating.py             # Gate dataclasses + GateManager + per-quadrant stats
│   ├── llm.py                # DeepSeek HTTP client + balance + cost estimation
│   ├── markers.py            # Fluorophore → marker mapping persistence (incl. hidden channels)
│   ├── markers_window.py     # Marker editor popup
│   ├── plotting.py           # Density scatter, gate overlays, quadrant labels, histograms
│   ├── reader.py             # Pure-Python FCS 2.0 / 3.0 / 3.1 parser
│   ├── theme.py              # Cohesive light-theme palette + style helpers
│   └── tools.py              # Tool schemas + dispatcher + agentic loop for the chat assistant
├── .github/workflows/
│   └── build-windows.yml     # CI: build dist/FreeFlow.exe on each tag, attach to release
├── freeflow.spec             # PyInstaller spec used to build the Windows executable
├── LICENSE                   # MIT license + extended disclaimer
├── setup.py
├── requirements.txt
└── README.md                 # This file
```

---

## License

FreeFlow is released under the **MIT License**.  See
[`LICENSE`](LICENSE) for the full text — it spells out the standard
MIT permissions plus an explicit additional disclaimer:

> The author (Stefano Angioletti-Uberti) provides this software in the
> hope that it will be useful, but on an entirely "as-is" basis and
> accepts **no responsibility whatsoever** for any use, misuse, error,
> bug, data loss, or any consequence — direct or indirect — of running
> the Software.  **You use this Software at your own risk.**

Copyright © 2026 **Stefano Angioletti-Uberti** —
[s.angioletti-uberti@imperial.ac.uk](mailto:s.angioletti-uberti@imperial.ac.uk).

---

## Donation

FreeFlow is developed and maintained by a single person in their spare
time — alongside a full research workload — and is given away for free
to anyone who finds it useful.

If FreeFlow saves you time, makes your figures nicer, helps a paper
get written, or simply spares you from clicking through a commercial
GUI for the hundredth time, please consider sending a small donation.
**Every contribution — large or small — helps keep the software free,
open-source, and actively maintained**, and is a meaningful recognition
of the time and care that goes into building a tool like this.

**Transfer details**

> *Bank transfer (IBAN / SWIFT):* _to be filled in by the author_
>
> *PayPal:* _to be filled in by the author_
>
> *Other (e.g. GitHub Sponsors, Stripe, Wise):* _to be filled in by the author_

If you'd like to donate but the option above isn't convenient for you,
get in touch at the email above and we'll arrange something that works.

Thank you for using FreeFlow.

---

## Reporting issues

Include `flowcyt.log` from the working directory at the time of the
problem, your matplotlib backend (`python -c "import matplotlib;
print(matplotlib.get_backend())"`), and the OS / Python version.
