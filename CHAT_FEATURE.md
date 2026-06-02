# Chat Assistant & Marker Mapping

FreeFlow includes a DeepSeek-powered chat assistant that can perform gating
operations from natural-language requests, and a per-file marker mapping
editor that lets the assistant (and the channel selectors) refer to channels
by their protein marker rather than the raw fluorophore name.

## Opening the windows

Two new buttons live on the right-hand control panel, beneath **Save Plot**:

- **Chat** — opens the DeepSeek assistant popup.
- **Markers** — opens the fluorophore → protein-marker mapping editor.

Both windows can be left open while you keep clicking around the main UI; the
mapping window auto-refreshes when you load a different `.fcs` file.

## DeepSeek API key

The chat window resolves your API key in this order:

1. The environment variable `DEEPSEEK_API_KEY`.
2. A file named `deepseek_api_key` (or `.deepseek_api_key`) in the directory
   you launched FreeFlow from. *If this file exists, it's used as-is.*
3. `~/.freeflow/deepseek_api_key`.

If none of these contain a key, the chat window opens in **key-entry mode**:
type your key, click **Save key**, and it'll be written to
`~/.freeflow/deepseek_api_key` with permissions `0600`. Subsequent launches
pick it up automatically.

The header strip shows your remaining DeepSeek balance and the per-session
cost (estimated against the per-token pricing constants in
`flowcyt/llm.py` — edit the `PRICING` dict if DeepSeek changes their rates).
Click **Refresh $** to re-query the balance.

## Marker mapping

The mapping editor lists every channel in the loaded `.fcs` file with a text
box next to it. Pre-populated from the FCS `PnS` field where present; type
the protein marker you've assigned (e.g. `CD4`) and click **Save**.

- Saved overrides live in `<your_file>.fcs.markers.json` right next to the
  FCS file. Different files keep independent mappings.
- The channel selectors on the right panel immediately update to show
  `FL2-A (CD4)` etc.
- **Reload from FCS** reverts the on-screen text boxes to the FCS PnS
  defaults (deletes overrides — click **Save** to commit).
- You can edit the mapping any time the window is open, even after the chat
  has been used.

## How the chat assistant uses the mapping

Every chat request to DeepSeek is prefixed with a fresh system prompt that
lists, for the currently loaded file:

- The full `fluorophore → marker` table.
- The currently displayed X and Y channel (with markers).
- The current axis scales and selected parent gate.
- Every existing gate, its type, and its parent.

This means you can write `draw a polygon around the CD4+ population on FSC vs
SSC` and DeepSeek will resolve `CD4` to the right channel automatically.

## What the assistant can do

The assistant has these tools available:

**Additive** (auto-executed):
`create_polygon_gate`, `create_rectangle_gate`, `create_ellipse_gate`,
`create_quadrant_gate`, `create_threshold_gate`, `select_channels`,
`set_parent_gate`, `set_axis_scale`, `rename_gate`.

**Read-only** (auto-executed):
`list_channels`, `list_gates`, `get_channel_range`, `summarise_state`.

**Destructive** (always asks first):
`remove_gate`, `clear_all_gates`, `export_csv`.

When the assistant invokes a destructive tool the chat window swaps the
**Send** button for **Yes** / **No** buttons and waits for your decision
before going further.

## Example session

```
You          : what channels do I have?
→ tool       : list_channels({})
← result     : FSC-A -> FSC - Area
               SSC-A -> SSC - Area
               FL1-A -> CD4
               FL2-A -> CD8
               ...
Assistant    : You have 13 channels. Two have marker annotations: FL1-A → CD4
               and FL2-A → CD8. The rest currently have only the PnS labels
               from the FCS file. Open the Markers window if you'd like to add
               more.

You          : create a polygon around the CD4 high CD8 low quadrant
→ tool       : select_channels({"x_channel": "CD4", "y_channel": "CD8"})
← result     : Selected channels: X=FL1-A, Y=FL2-A.
→ tool       : get_channel_range({"channel": "CD4"})
← result     : {"channel": "FL1-A", "min": 0, "max": 262144, "p99": 198000,
                "median": 14200}
→ tool       : create_polygon_gate({...})
← result     : Created polygon gate 'CD4+CD8-' on FL1-A × FL2-A with 4 vertices.
Assistant    : Done — gate `CD4+CD8-` covers the high-CD4 / low-CD8 region.
               It contains ~12% of events.
```

## Limitations / notes

- The chat window blocks during a DeepSeek round-trip; expect a 1–3 s wait per
  response. Tool-chained workflows can take longer (max 6 internal iterations
  per user message).
- DeepSeek's pricing constants in `flowcyt/llm.py` need updating manually if
  the provider revises rates — session cost is otherwise estimated correctly.
- The `requests` package is now a runtime dependency
  (`pip install -r requirements.txt`).
