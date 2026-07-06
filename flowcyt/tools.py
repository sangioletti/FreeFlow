"""
tools.py - DeepSeek tool / function-call surface for FreeFlow.

Defines the schema of tools exposed to the language model and a dispatcher
that maps tool invocations back to ``FlowCytApp`` methods.  Also implements
the agentic loop (chat -> tool calls -> results -> chat -> ...) and the
implicit system prompt that gives DeepSeek the channel-marker map and the
current GUI state.

Tools are grouped into three categories:
    * Additive       — auto-execute (create gates, change channels, ...).
    * Read-only      — auto-execute (list channels, list gates, get range).
    * Destructive    — require user confirmation (remove, clear, export).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from .llm import DeepSeekClient, DeepSeekError, estimate_cost
from .markers import effective_channel_label

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------- #
#  Channel resolution
# ---------------------------------------------------------------------------- #

def _open_window_for(app, gate) -> None:
    """Open a dedicated sub-window for *gate*, matching the manual-gating
    behaviour (which opens a window automatically after each polygon /
    rectangle / ellipse / quadrant / threshold gate is finalised).

    Failures are swallowed and logged — gate creation should never fail
    just because the secondary window couldn't open.
    """
    opener = getattr(app, "_open_gate_window", None)
    if opener is None:
        return
    try:
        opener(gate)
    except Exception:
        logger.exception("Failed to open sub-window for gate %r", getattr(gate, "name", None))


def _resolve_channel(app, name: str) -> tuple[int, str]:
    """Resolve a channel reference (fluorophore or marker name) to (index, short_name).

    Raises ``ValueError`` with a helpful message if not found.
    """
    if app.fcs is None:
        raise ValueError("No FCS file is currently loaded.")
    if name is None:
        raise ValueError("Channel name is required.")

    target = str(name).strip()
    if not target:
        raise ValueError("Channel name is empty.")

    short_names = app.fcs.channel_names
    marker_map = getattr(app, "_marker_map", {}) or {}

    # 1. Exact match against fluorophore short name.
    for idx, short in enumerate(short_names):
        if target == short:
            return idx, short

    # 2. Exact match against marker (case-insensitive).
    target_lower = target.lower()
    for short, marker in marker_map.items():
        if marker and marker.lower() == target_lower and short in short_names:
            return short_names.index(short), short

    # 3. Exact match against effective label "SHORT (marker)".
    for idx, short in enumerate(short_names):
        if effective_channel_label(short, marker_map) == target:
            return idx, short

    # 4. Case-insensitive fluorophore match.
    for idx, short in enumerate(short_names):
        if short.lower() == target_lower:
            return idx, short

    raise ValueError(
        f"Channel '{name}' not found. Known channels: "
        + ", ".join(effective_channel_label(s, marker_map) for s in short_names)
    )


# ---------------------------------------------------------------------------- #
#  Tool definitions  (JSON Schema, OpenAI function-call format)
# ---------------------------------------------------------------------------- #

_CHANNEL_DESC = (
    "Channel reference. Either the fluorophore short name (e.g. 'FL1-A') "
    "or the protein marker (e.g. 'CD4'). Marker names take precedence "
    "when both are available."
)

_PARENT_DESC = (
    "Optional parent gate name for hierarchical (sub-)gating. Omit or pass "
    "null for a root-level gate."
)


def _additive_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "create_polygon_gate",
                "description": "Create a polygon gate on the current/selected channels.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Display name for the new gate (e.g. 'CD4+')."},
                        "x_channel": {"type": "string", "description": _CHANNEL_DESC},
                        "y_channel": {"type": "string", "description": _CHANNEL_DESC},
                        "vertices": {
                            "type": "array",
                            "description": "Ordered list of (x, y) vertices, at least 3.",
                            "items": {
                                "type": "array",
                                "items": {"type": "number"},
                                "minItems": 2, "maxItems": 2,
                            },
                            "minItems": 3,
                        },
                        "parent_gate": {"type": ["string", "null"], "description": _PARENT_DESC},
                    },
                    "required": ["name", "x_channel", "y_channel", "vertices"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "create_rectangle_gate",
                "description": "Create an axis-aligned rectangle gate.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "x_channel": {"type": "string", "description": _CHANNEL_DESC},
                        "y_channel": {"type": "string", "description": _CHANNEL_DESC},
                        "x_min": {"type": "number"},
                        "x_max": {"type": "number"},
                        "y_min": {"type": "number"},
                        "y_max": {"type": "number"},
                        "parent_gate": {"type": ["string", "null"], "description": _PARENT_DESC},
                    },
                    "required": ["name", "x_channel", "y_channel",
                                 "x_min", "x_max", "y_min", "y_max"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "create_ellipse_gate",
                "description": "Create an ellipse gate.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "x_channel": {"type": "string", "description": _CHANNEL_DESC},
                        "y_channel": {"type": "string", "description": _CHANNEL_DESC},
                        "center_x": {"type": "number"},
                        "center_y": {"type": "number"},
                        "semi_x": {"type": "number", "description": "Semi-axis along x."},
                        "semi_y": {"type": "number", "description": "Semi-axis along y."},
                        "angle": {"type": "number", "description": "Rotation angle (radians). 0 = axis-aligned.", "default": 0.0},
                        "parent_gate": {"type": ["string", "null"], "description": _PARENT_DESC},
                    },
                    "required": ["name", "x_channel", "y_channel",
                                 "center_x", "center_y", "semi_x", "semi_y"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "create_quadrant_gate",
                "description": "Create a quadrant gate (one of Q1..Q4 around a crosshair).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "x_channel": {"type": "string", "description": _CHANNEL_DESC},
                        "y_channel": {"type": "string", "description": _CHANNEL_DESC},
                        "mid_x": {"type": "number"},
                        "mid_y": {"type": "number"},
                        "quadrant": {"type": "string", "enum": ["Q1", "Q2", "Q3", "Q4"]},
                        "parent_gate": {"type": ["string", "null"], "description": _PARENT_DESC},
                    },
                    "required": ["name", "x_channel", "y_channel",
                                 "mid_x", "mid_y", "quadrant"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "create_threshold_gate",
                "description": "Create a 1D threshold gate on a single channel.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "channel": {"type": "string", "description": _CHANNEL_DESC},
                        "threshold": {"type": "number"},
                        "side": {"type": "string", "enum": ["left", "right"], "default": "right"},
                        "parent_gate": {"type": ["string", "null"], "description": _PARENT_DESC},
                    },
                    "required": ["name", "channel", "threshold"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "select_channels",
                "description": "Set the currently-displayed X and Y channels on the main scatter plot.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "x_channel": {"type": "string", "description": _CHANNEL_DESC},
                        "y_channel": {"type": "string", "description": _CHANNEL_DESC},
                    },
                    "required": ["x_channel", "y_channel"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "set_parent_gate",
                "description": "Set the parent gate used when creating subsequent child gates. Pass null to clear.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "gate_name": {"type": ["string", "null"], "description": "Existing gate name or null."},
                    },
                    "required": ["gate_name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "set_axis_scale",
                "description": "Set the scale of an axis ('linear' or 'log').",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "axis": {"type": "string", "enum": ["x", "y"]},
                        "scale": {"type": "string", "enum": ["linear", "log"]},
                    },
                    "required": ["axis", "scale"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "rename_gate",
                "description": "Rename an existing gate.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "old_name": {"type": "string"},
                        "new_name": {"type": "string"},
                    },
                    "required": ["old_name", "new_name"],
                },
            },
        },
    ]


def _readonly_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "list_channels",
                "description": "List all available channels with their fluorophore short names and protein-marker mappings.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_gates",
                "description": "List all currently defined gates, their hierarchy, and population statistics.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_channel_range",
                "description": "Get the (min, max, p1, p99, median) value range of a channel — useful before choosing gate coordinates.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "channel": {"type": "string", "description": _CHANNEL_DESC},
                    },
                    "required": ["channel"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "summarise_state",
                "description": "Get a single combined summary: loaded file, channels (with markers), current X/Y selection, current parent gate, and existing gates.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ]


def _destructive_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "remove_gate",
                "description": "Delete an existing gate by name (requires user confirmation).",
                "parameters": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "clear_all_gates",
                "description": "Delete every gate currently defined (requires user confirmation).",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "export_csv",
                "description": "Export the gated events to a CSV file. Requires confirmation if the target file would be overwritten.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filepath": {
                            "type": ["string", "null"],
                            "description": "Optional output path. If omitted, uses the auto-generated path next to the FCS file.",
                        },
                    },
                },
            },
        },
    ]


DESTRUCTIVE_TOOL_NAMES = {"remove_gate", "clear_all_gates", "export_csv"}


def all_tools() -> list[dict[str, Any]]:
    return _additive_tools() + _readonly_tools() + _destructive_tools()


# ---------------------------------------------------------------------------- #
#  System prompt (implicit context)
# ---------------------------------------------------------------------------- #

def build_system_prompt(app) -> str:
    lines = [
        "You are an assistant embedded in FreeFlow, a flow-cytometry gating tool.",
        "You help the user define gates and navigate their FCS data by calling",
        "the provided tools.  Prefer marker names (e.g. 'CD4') over fluorophore",
        "codes when the user uses them.  Always pick concrete numeric coordinates",
        "for gate geometry — call `get_channel_range` first if you need to know",
        "the data scale.",
        "",
        "AXIS SCALE PROTOCOL — respect the current axis settings, and keep them.",
        "  * If an axis is currently 'log', the user is reading the data on a",
        "    logarithmic scale.  Pick gate coordinates that make sense on that",
        "    scale: a 'CD4 high' or 'positive' population on a log axis typically",
        "    spans one to two orders of magnitude near the top of the data range,",
        "    not a narrow linear band.  Use the p99 / median values returned by",
        "    `get_channel_range` to anchor the boundary in log-space.",
        "  * Do NOT silently flip an axis from log to linear (or vice versa) just",
        "    because you are creating a new gate.  Only call `set_axis_scale` if",
        "    the user explicitly asks for a scale change.  Anything you draw is",
        "    rendered on the user's currently-selected scale; preserve it.",
        "  * When the user references the plot they're looking at, assume they",
        "    mean it as currently scaled (log axes stay log).",
        "",
        "DESTRUCTIVE ACTIONS PROTOCOL — applies to `remove_gate`,",
        "`clear_all_gates`, and `export_csv`. Both steps are mandatory:",
        "  Step 1. When the user first asks for a destructive action, REPLY",
        "          asking them to confirm in plain English (e.g. \"Are you",
        "          sure you want to clear all gates?\"). Do NOT call any",
        "          destructive tool in this turn.",
        "  Step 2. Once the user confirms verbally (e.g. \"yes\", \"go ahead\",",
        "          \"do it\"), reply with a short message telling them the GUI",
        "          will now show a Yes/No button that they must press to",
        "          finalise the action, and IMMEDIATELY call the requested",
        "          destructive tool in the same turn. The GUI's Yes/No",
        "          button is the only mechanism that actually authorises",
        "          the action — your tool call only surfaces the button.",
        "Never skip Step 1, even if the user sounds certain.  Never call a",
        "destructive tool without also telling the user to press the Yes",
        "button.",
        "",
    ]
    if app.fcs is None:
        lines.append("No FCS file is currently loaded.")
        return "\n".join(lines)

    lines.append(f"Loaded file: {app.fcs.filepath}")
    lines.append(f"Events: {app.fcs.num_events:,}    Channels: {app.fcs.num_channels}")
    lines.append("")
    lines.append("Channels (fluorophore -> marker):")
    marker_map = getattr(app, "_marker_map", {}) or {}
    for short in app.fcs.channel_names:
        marker = marker_map.get(short, "")
        if marker:
            lines.append(f"  {short} -> {marker}")
        else:
            lines.append(f"  {short} -> (no marker)")
    lines.append("")

    try:
        xi, yi, xn, yn = app._current_xy()
        lines.append(
            f"Current X channel: {effective_channel_label(xn, marker_map)}"
        )
        lines.append(
            f"Current Y channel: {effective_channel_label(yn, marker_map)}"
        )
    except Exception:
        pass

    x_scale = getattr(app, "_x_scale", "linear")
    y_scale = getattr(app, "_y_scale", "linear")
    lines.append(f"Axis scales: x={x_scale}, y={y_scale}")

    parent_uid = getattr(app, "_selected_parent_uid", None)
    parent_name = None
    if parent_uid:
        for g in app.gate_mgr.gates:
            if g.uid == parent_uid:
                parent_name = g.name
                break
    lines.append(f"Selected parent gate: {parent_name or 'None'}")
    lines.append("")

    if app.gate_mgr.gates:
        lines.append("Existing gates:")
        # Build name lookup for parent display.
        name_by_uid = {g.uid: g.name for g in app.gate_mgr.gates}
        for g in app.gate_mgr.gates:
            kind = type(g).__name__.replace("Gate", "").lower()
            parent = name_by_uid.get(g.parent_gate_uid or "", None)
            parent_str = f" (child of {parent})" if parent else " (root)"
            lines.append(f"  {g.name} [{kind}]{parent_str}")
    else:
        lines.append("Existing gates: (none)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------- #
#  Tool dispatcher
# ---------------------------------------------------------------------------- #

class ToolError(Exception):
    """Raised inside a dispatcher to send a structured error back to the model."""


def _format_gate_summary(app) -> str:
    if not app.gate_mgr.gates:
        return "(no gates)"
    try:
        stats = app.gate_mgr.compute_stats(app.fcs.data, app.fcs.channel_names)
    except Exception:
        stats = []
    by_uid = {s["uid"]: s for s in stats}
    name_by_uid = {g.uid: g.name for g in app.gate_mgr.gates}
    lines = []
    for g in app.gate_mgr.gates:
        s = by_uid.get(g.uid, {})
        kind = type(g).__name__.replace("Gate", "").lower()
        parent = name_by_uid.get(g.parent_gate_uid or "", None)
        parent_str = f", child of {parent}" if parent else ", root"
        if s:
            lines.append(
                f"{g.name} [{kind}{parent_str}] — {s['count']:,} events "
                f"({s['percent']:.1f}% of parent, "
                f"{s['percent_of_total']:.1f}% of total)"
            )
        else:
            lines.append(f"{g.name} [{kind}{parent_str}]")
    return "\n".join(lines)


def _tool_create_polygon_gate(app, args):
    xi, xn = _resolve_channel(app, args["x_channel"])
    yi, yn = _resolve_channel(app, args["y_channel"])
    parent_uid = _parent_uid_from_name(app, args.get("parent_gate"))
    verts = [tuple(v) for v in args["vertices"]]
    if len(verts) < 3:
        raise ToolError("A polygon gate requires at least 3 vertices.")
    g = app.gate_mgr.add_polygon_gate(
        name=args["name"], x_channel=xn, y_channel=yn,
        vertices=verts, parent_gate_uid=parent_uid,
    )
    app.refresh_plot()
    _open_window_for(app, g)
    return f"Created polygon gate '{g.name}' on {xn} × {yn} with {len(verts)} vertices."


def _tool_create_rectangle_gate(app, args):
    xi, xn = _resolve_channel(app, args["x_channel"])
    yi, yn = _resolve_channel(app, args["y_channel"])
    parent_uid = _parent_uid_from_name(app, args.get("parent_gate"))
    g = app.gate_mgr.add_rectangle_gate(
        name=args["name"], x_channel=xn, y_channel=yn,
        x_min=float(args["x_min"]), x_max=float(args["x_max"]),
        y_min=float(args["y_min"]), y_max=float(args["y_max"]),
        parent_gate_uid=parent_uid,
    )
    app.refresh_plot()
    _open_window_for(app, g)
    return (f"Created rectangle gate '{g.name}' on {xn} × {yn} "
            f"[{g.x_min:.3g}..{g.x_max:.3g}] × [{g.y_min:.3g}..{g.y_max:.3g}].")


def _tool_create_ellipse_gate(app, args):
    xi, xn = _resolve_channel(app, args["x_channel"])
    yi, yn = _resolve_channel(app, args["y_channel"])
    parent_uid = _parent_uid_from_name(app, args.get("parent_gate"))
    g = app.gate_mgr.add_ellipse_gate(
        name=args["name"], x_channel=xn, y_channel=yn,
        center_x=float(args["center_x"]), center_y=float(args["center_y"]),
        semi_x=float(args["semi_x"]), semi_y=float(args["semi_y"]),
        angle=float(args.get("angle", 0.0) or 0.0),
        parent_gate_uid=parent_uid,
    )
    app.refresh_plot()
    _open_window_for(app, g)
    return (f"Created ellipse gate '{g.name}' on {xn} × {yn} "
            f"centred at ({g.center_x:.3g}, {g.center_y:.3g}).")


def _tool_create_quadrant_gate(app, args):
    xi, xn = _resolve_channel(app, args["x_channel"])
    yi, yn = _resolve_channel(app, args["y_channel"])
    quadrant = str(args["quadrant"]).upper()
    if quadrant not in {"Q1", "Q2", "Q3", "Q4"}:
        raise ToolError("Quadrant must be one of Q1, Q2, Q3, Q4.")
    parent_uid = _parent_uid_from_name(app, args.get("parent_gate"))
    g = app.gate_mgr.add_quadrant_gate(
        name=args["name"], x_channel=xn, y_channel=yn,
        mid_x=float(args["mid_x"]), mid_y=float(args["mid_y"]),
        quadrant=quadrant, parent_gate_uid=parent_uid,
    )
    app.refresh_plot()
    _open_window_for(app, g)
    return f"Created quadrant gate '{g.name}' ({quadrant}) on {xn} × {yn}."


def _tool_create_threshold_gate(app, args):
    xi, xn = _resolve_channel(app, args["channel"])
    side = str(args.get("side", "right")).lower()
    if side not in {"left", "right"}:
        raise ToolError("Threshold side must be 'left' or 'right'.")
    parent_uid = _parent_uid_from_name(app, args.get("parent_gate"))
    # Threshold gates need a y_channel for bookkeeping; pick current Y.
    try:
        _, _, _, current_y = app._current_xy()
    except Exception:
        current_y = xn
    g = app.gate_mgr.add_threshold_gate(
        name=args["name"], x_channel=xn, y_channel=current_y,
        threshold=float(args["threshold"]), side=side,
        parent_gate_uid=parent_uid,
    )
    app.refresh_plot()
    _open_window_for(app, g)
    return f"Created threshold gate '{g.name}' on {xn} at {g.threshold:.3g} ({side})."


def _tool_select_channels(app, args):
    xi, xn = _resolve_channel(app, args["x_channel"])
    yi, yn = _resolve_channel(app, args["y_channel"])
    app.set_x_channel(xi)
    app.set_y_channel(yi)
    return f"Selected channels: X={xn}, Y={yn}."


def _tool_set_parent_gate(app, args):
    name = args.get("gate_name")
    if name in (None, "", "null", "None"):
        app.set_parent_gate_by_name(None)
        return "Cleared parent gate selection (new gates will be root-level)."
    app.set_parent_gate_by_name(name)
    return f"Parent gate set to '{name}'."


def _tool_set_axis_scale(app, args):
    axis = str(args["axis"]).lower()
    scale = str(args["scale"]).lower()
    if axis not in {"x", "y"} or scale not in {"linear", "log"}:
        raise ToolError("axis must be 'x' or 'y' and scale 'linear' or 'log'.")
    app.set_axis_scale(axis, scale)
    return f"Set {axis}-axis scale to {scale}."


def _tool_rename_gate(app, args):
    old_name = args["old_name"]
    new_name = args["new_name"]
    g = app.find_gate_by_name(old_name)
    if g is None:
        raise ToolError(f"No gate named '{old_name}'.")
    g.name = new_name
    app.refresh_plot()
    return f"Renamed '{old_name}' to '{new_name}'."


def _tool_list_channels(app, args):
    if app.fcs is None:
        return "No FCS file loaded."
    marker_map = getattr(app, "_marker_map", {}) or {}
    lines = []
    for short in app.fcs.channel_names:
        marker = marker_map.get(short, "")
        lines.append(f"{short} -> {marker or '(no marker)'}")
    return "\n".join(lines)


def _tool_list_gates(app, args):
    return _format_gate_summary(app)


def _tool_get_channel_range(app, args):
    if app.fcs is None:
        raise ToolError("No FCS file loaded.")
    idx, short = _resolve_channel(app, args["channel"])
    import numpy as np
    col = app.fcs.data[:, idx]
    return json.dumps({
        "channel": short,
        "min": float(np.min(col)),
        "max": float(np.max(col)),
        "p1": float(np.percentile(col, 1)),
        "p99": float(np.percentile(col, 99)),
        "median": float(np.median(col)),
    })


def _tool_summarise_state(app, args):
    # build_system_prompt produces a clean summary already.
    return build_system_prompt(app)


def _tool_remove_gate(app, args):
    g = app.find_gate_by_name(args["name"])
    if g is None:
        raise ToolError(f"No gate named '{args['name']}'.")
    app.gate_mgr.remove_gate(g.uid)
    app.refresh_plot()
    return f"Removed gate '{g.name}'."


def _tool_clear_all_gates(app, args):
    count = len(app.gate_mgr.gates)
    app.gate_mgr.clear()
    app.refresh_plot()
    return f"Cleared {count} gate(s)."


def _tool_export_csv(app, args):
    filepath = args.get("filepath") or None
    path = app.export_csv(filepath)
    return f"Exported gated events to {path}."


def _parent_uid_from_name(app, name) -> str | None:
    if name in (None, "", "null", "None"):
        return None
    g = app.find_gate_by_name(name)
    if g is None:
        raise ToolError(f"Parent gate '{name}' not found.")
    return g.uid


# Registry of tool name -> dispatcher function.
DISPATCH: dict[str, Callable] = {
    "create_polygon_gate":   _tool_create_polygon_gate,
    "create_rectangle_gate": _tool_create_rectangle_gate,
    "create_ellipse_gate":   _tool_create_ellipse_gate,
    "create_quadrant_gate":  _tool_create_quadrant_gate,
    "create_threshold_gate": _tool_create_threshold_gate,
    "select_channels":       _tool_select_channels,
    "set_parent_gate":       _tool_set_parent_gate,
    "set_axis_scale":        _tool_set_axis_scale,
    "rename_gate":           _tool_rename_gate,
    "list_channels":         _tool_list_channels,
    "list_gates":            _tool_list_gates,
    "get_channel_range":     _tool_get_channel_range,
    "summarise_state":       _tool_summarise_state,
    "remove_gate":           _tool_remove_gate,
    "clear_all_gates":       _tool_clear_all_gates,
    "export_csv":            _tool_export_csv,
}


# ---------------------------------------------------------------------------- #
#  Agentic loop
# ---------------------------------------------------------------------------- #

MAX_TOOL_ITERATIONS = 6


def run_chat_turn(
    app,
    client: DeepSeekClient,
    user_message: str,
    history: list[dict[str, Any]],
    on_event: Callable[[dict[str, Any]], None],
    confirm_destructive: Callable[[str, str, dict], bool],
) -> dict[str, Any]:
    """Run a single user turn through DeepSeek, handling tool calls.

    Mutates ``history`` in place — appends the new user message and every
    assistant + tool message produced during this turn.

    ``on_event`` is invoked with structured dicts so the chat window can
    render them:
        {"kind": "user",      "content": str}
        {"kind": "assistant", "content": str}
        {"kind": "tool_call", "name": str, "arguments": dict}
        {"kind": "tool_result","name": str, "content": str, "is_error": bool}
        {"kind": "info",      "content": str}

    ``confirm_destructive(tool_name, summary, args)`` is called when the
    model invokes a destructive tool; must return ``True`` to proceed,
    ``False`` to decline.

    Returns ``{"cost_usd": float, "tokens_in": int, "tokens_out": int}``
    for the cumulative cost of this turn (sum across all DeepSeek calls).
    """
    history.append({"role": "user", "content": user_message})
    on_event({"kind": "user", "content": user_message})

    # The system prompt is always fresh at the head so the model has
    # up-to-date GUI state every turn.
    system_msg = {"role": "system", "content": build_system_prompt(app)}
    tools = all_tools()

    total_cost = 0.0
    total_in = 0
    total_out = 0

    for _ in range(MAX_TOOL_ITERATIONS):
        messages = [system_msg] + history
        try:
            resp = client.chat(messages, tools=tools)
        except DeepSeekError as e:
            on_event({"kind": "info", "content": f"DeepSeek error: {e}"})
            return {"cost_usd": total_cost, "tokens_in": total_in, "tokens_out": total_out}

        usage = resp.get("usage") or {}
        total_cost += estimate_cost(client.model, usage)
        total_in += int(usage.get("prompt_tokens") or 0)
        total_out += int(usage.get("completion_tokens") or 0)

        choice = (resp.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        tool_calls = msg.get("tool_calls") or []
        content = msg.get("content") or ""

        # Persist the assistant message (with any tool_calls) into history
        # so subsequent turns see them in context.
        history.append({
            "role": "assistant",
            "content": content,
            **({"tool_calls": tool_calls} if tool_calls else {}),
        })

        if content and not tool_calls:
            on_event({"kind": "assistant", "content": content})
            return {"cost_usd": total_cost, "tokens_in": total_in, "tokens_out": total_out}

        if content:
            on_event({"kind": "assistant", "content": content})

        if not tool_calls:
            return {"cost_usd": total_cost, "tokens_in": total_in, "tokens_out": total_out}

        # Execute each requested tool call, then loop back to the model.
        for call in tool_calls:
            call_id = call.get("id") or ""
            fn = (call.get("function") or {})
            name = fn.get("name") or ""
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}

            on_event({"kind": "tool_call", "name": name, "arguments": args})

            if name not in DISPATCH:
                result = f"Error: unknown tool '{name}'."
                is_error = True
            elif name in DESTRUCTIVE_TOOL_NAMES:
                summary = _describe_destructive(name, args)
                approved = confirm_destructive(name, summary, args)
                if not approved:
                    result = "User declined to perform this action."
                    is_error = False
                else:
                    try:
                        result = DISPATCH[name](app, args)
                        is_error = False
                    except ToolError as e:
                        result = f"Error: {e}"
                        is_error = True
                    except Exception as e:
                        logger.exception("Tool '%s' crashed", name)
                        result = f"Error: {e}"
                        is_error = True
            else:
                try:
                    result = DISPATCH[name](app, args)
                    is_error = False
                except ToolError as e:
                    result = f"Error: {e}"
                    is_error = True
                except Exception as e:
                    logger.exception("Tool '%s' crashed", name)
                    result = f"Error: {e}"
                    is_error = True

            on_event({
                "kind": "tool_result", "name": name,
                "content": result, "is_error": is_error,
            })

            history.append({
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": result,
            })

    on_event({
        "kind": "info",
        "content": f"Stopped after {MAX_TOOL_ITERATIONS} tool iterations.",
    })
    return {"cost_usd": total_cost, "tokens_in": total_in, "tokens_out": total_out}


def _describe_destructive(name: str, args: dict) -> str:
    if name == "remove_gate":
        return f"Remove gate '{args.get('name')}'."
    if name == "clear_all_gates":
        return "Delete every defined gate."
    if name == "export_csv":
        fp = args.get("filepath")
        return f"Export gated events to {fp or 'auto-generated path'}."
    return f"Execute destructive action: {name}({args})"
