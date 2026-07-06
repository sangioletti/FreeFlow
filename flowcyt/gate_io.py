"""
gate_io.py - Serialise / deserialise gating strategies to a JSON sidecar.

A "gating strategy" is the full list of gates currently in a
``GateManager`` — geometry, channel references, names, colours, and the
parent-child hierarchy (via ``parent_gate_uid``).  We persist it next to
the FCS file as ``<fcs_path>.gates.json`` so reopening the same file
restores everything, and a different FCS file with the same channel
short names can re-apply the same strategy directly.

The FCS file itself is never modified.

Public API:
    sidecar_path(fcs_path)
    save_gates(fcs_path, gate_mgr)              -> str (path written)
    load_gates(fcs_path, gate_mgr, replace=True) -> dict with
        {"loaded": int, "skipped": list[str], "missing_channels": list[str]}
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any

from .gating import (
    Gate, GateManager,
    PolygonGate, RectangleGate, EllipseGate, QuadrantGate, ThresholdGate,
)

logger = logging.getLogger(__name__)

SIDECAR_SUFFIX = ".gates.json"
FORMAT_VERSION = 1


def sidecar_path(fcs_path: str) -> str:
    """Return the canonical ``<fcs_path>.gates.json`` location."""
    return f"{fcs_path}{SIDECAR_SUFFIX}"


# ---------------------------------------------------------------------------- #
#  Serialisation
# ---------------------------------------------------------------------------- #

def _gate_to_dict(g: Gate) -> dict[str, Any]:
    base: dict[str, Any] = {
        "type": _type_tag(g),
        "uid": g.uid,
        "name": g.name,
        "x_channel": g.x_channel,
        "y_channel": g.y_channel,
        "color": g.color,
        "parent_gate_uid": g.parent_gate_uid,
    }
    if isinstance(g, PolygonGate):
        base["vertices"] = [list(v) for v in g.vertices]
    elif isinstance(g, RectangleGate):
        base["x_min"] = float(g.x_min)
        base["x_max"] = float(g.x_max)
        base["y_min"] = float(g.y_min)
        base["y_max"] = float(g.y_max)
    elif isinstance(g, EllipseGate):
        base["center_x"] = float(g.center_x)
        base["center_y"] = float(g.center_y)
        base["semi_x"]   = float(g.semi_x)
        base["semi_y"]   = float(g.semi_y)
        base["angle"]    = float(g.angle)
    elif isinstance(g, QuadrantGate):
        base["mid_x"]    = float(g.mid_x)
        base["mid_y"]    = float(g.mid_y)
        base["quadrant"] = g.quadrant
    elif isinstance(g, ThresholdGate):
        base["threshold"] = float(g.threshold)
        base["side"]      = g.side
        base["channel"]   = g.channel
    return base


def _type_tag(g: Gate) -> str:
    if isinstance(g, PolygonGate):   return "polygon"
    if isinstance(g, RectangleGate): return "rectangle"
    if isinstance(g, EllipseGate):   return "ellipse"
    if isinstance(g, QuadrantGate):  return "quadrant"
    if isinstance(g, ThresholdGate): return "threshold"
    raise TypeError(f"Unknown gate type for serialisation: {type(g).__name__}")


def _gate_from_dict(d: dict[str, Any]) -> Gate:
    t = d.get("type", "")
    common = dict(
        name=d.get("name", ""),
        x_channel=d.get("x_channel", ""),
        y_channel=d.get("y_channel", ""),
        color=d.get("color", "#ff0000"),
        uid=d.get("uid", ""),  # falls back to default factory if empty later
        parent_gate_uid=d.get("parent_gate_uid"),
    )
    if not common["uid"]:
        # Let the dataclass default factory assign one.
        common.pop("uid")

    if t == "polygon":
        return PolygonGate(
            **common,
            vertices=[tuple(v) for v in d.get("vertices", [])],
        )
    if t == "rectangle":
        return RectangleGate(
            **common,
            x_min=float(d.get("x_min", 0.0)),
            x_max=float(d.get("x_max", 0.0)),
            y_min=float(d.get("y_min", 0.0)),
            y_max=float(d.get("y_max", 0.0)),
        )
    if t == "ellipse":
        return EllipseGate(
            **common,
            center_x=float(d.get("center_x", 0.0)),
            center_y=float(d.get("center_y", 0.0)),
            semi_x=float(d.get("semi_x", 0.0)),
            semi_y=float(d.get("semi_y", 0.0)),
            angle=float(d.get("angle", 0.0)),
        )
    if t == "quadrant":
        return QuadrantGate(
            **common,
            mid_x=float(d.get("mid_x", 0.0)),
            mid_y=float(d.get("mid_y", 0.0)),
            quadrant=str(d.get("quadrant", "Q1")),
        )
    if t == "threshold":
        return ThresholdGate(
            **common,
            threshold=float(d.get("threshold", 0.0)),
            side=str(d.get("side", "right")),
            channel=str(d.get("channel", common["x_channel"])),
        )
    raise ValueError(f"Unknown gate type tag: {t!r}")


# ---------------------------------------------------------------------------- #
#  Public functions
# ---------------------------------------------------------------------------- #

def save_gates(fcs_path: str, gate_mgr: GateManager,
               path_override: str | None = None) -> str:
    """Write the current strategy to ``<fcs_path>.gates.json``.

    Returns the absolute path written.  If there are no gates we still
    write an empty strategy (so a previously-saved file is overwritten
    with the cleared state, rather than silently leaving stale gates on
    disk).
    """
    out_path = path_override or sidecar_path(fcs_path)
    payload: dict[str, Any] = {
        "format": "freeflow.gating-strategy",
        "version": FORMAT_VERSION,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "source_fcs": os.path.basename(fcs_path) if fcs_path else "",
        "gates": [_gate_to_dict(g) for g in gate_mgr.gates],
    }
    with open(out_path, "w") as fh:
        json.dump(payload, fh, indent=2, sort_keys=False)
    return os.path.abspath(out_path)


def load_gates(
    fcs_path: str,
    gate_mgr: GateManager,
    replace: bool = True,
    path_override: str | None = None,
    available_channels: list[str] | None = None,
) -> dict[str, Any]:
    """Load gates from the sidecar JSON and append them to *gate_mgr*.

    Parameters
    ----------
    replace : bool
        If True (default) clear the existing gates before loading.
        If False, append the loaded gates alongside whatever's already
        in the manager.
    available_channels : list[str] | None
        Optional list of channel short names present in the loaded FCS
        file.  When provided, gates whose ``x_channel`` / ``y_channel``
        aren't in this list are still loaded (so the strategy is
        preserved) but their names are reported in the result so the UI
        can warn the user.

    Returns
    -------
    dict with::
        {"loaded": int,
         "skipped": list[str],         # malformed entries we couldn't decode
         "missing_channels": list[str] # gate names whose channels are missing
        }
    """
    in_path = path_override or sidecar_path(fcs_path)
    if not os.path.exists(in_path):
        raise FileNotFoundError(f"No gating strategy at {in_path}")

    with open(in_path, "r") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError("Strategy file is not a JSON object")

    gates_raw = payload.get("gates") or []
    if not isinstance(gates_raw, list):
        raise ValueError("Strategy file is missing a 'gates' array")

    loaded: list[Gate] = []
    skipped: list[str] = []
    missing_channels: list[str] = []
    chset = set(available_channels) if available_channels else None

    for entry in gates_raw:
        if not isinstance(entry, dict):
            skipped.append(repr(entry)[:60])
            continue
        try:
            g = _gate_from_dict(entry)
        except Exception as e:
            logger.warning("Skipping malformed gate entry: %s", e)
            skipped.append(entry.get("name", "<unnamed>"))
            continue
        if chset is not None:
            if (g.x_channel and g.x_channel not in chset) or \
               (g.y_channel and g.y_channel not in chset):
                missing_channels.append(g.name)
        loaded.append(g)

    if replace:
        gate_mgr.clear()
    for g in loaded:
        gate_mgr.gates.append(g)

    return {
        "loaded": len(loaded),
        "skipped": skipped,
        "missing_channels": missing_channels,
    }
