"""
markers.py - Per-file fluorophore -> protein marker mapping.

Each FCS file may have a sidecar JSON named ``<fcs_path>.markers.json`` that
maps fluorophore short names (FCS PnN keyword) to user-supplied protein
markers (e.g. ``"FL1-A": "CD4"``).  When the FCS file itself carries useful
PnS labels (long/marker names), those are used as defaults.  User overrides
in the sidecar JSON always win.

Public API:
    load_markers(fcs_path)
    save_markers(fcs_path, mapping)
    effective_channel_label(short_name, marker_map)
"""

from __future__ import annotations

import json
import logging
import os
from typing import Iterable

logger = logging.getLogger(__name__)


def _sidecar_path(fcs_path: str) -> str:
    """Return the sidecar JSON path for an FCS file."""
    return f"{fcs_path}.markers.json"


def _pns_defaults_from_fcs(fcs) -> dict[str, str]:
    """Extract PnS-based marker defaults from a loaded ``FCSData``.

    ``FCSData.channel_labels[i]`` falls back to ``channel_names[i]`` when PnS
    is empty, so we filter those out — they're not a real marker assignment.
    """
    defaults: dict[str, str] = {}
    for short, label in zip(fcs.channel_names, fcs.channel_labels):
        if label and label != short:
            defaults[short] = label
    return defaults


def load_markers(fcs_path: str, fcs=None) -> dict[str, str]:
    """Load merged marker map for a given FCS file.

    Resolution order:
        1. Defaults from the FCS file's PnS labels (when ``fcs`` is passed).
        2. Overrides from ``<fcs_path>.markers.json`` (wins on conflict).

    Returns ``{fluorophore_short_name: marker}``. Empty dict if nothing
    available.
    """
    merged: dict[str, str] = {}
    if fcs is not None:
        merged.update(_pns_defaults_from_fcs(fcs))

    sidecar = _sidecar_path(fcs_path)
    if os.path.exists(sidecar):
        try:
            with open(sidecar, "r") as fh:
                overrides = json.load(fh)
            if isinstance(overrides, dict):
                for k, v in overrides.items():
                    if isinstance(k, str) and isinstance(v, str) and v:
                        merged[k] = v
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Failed to read marker sidecar %s: %s", sidecar, e)

    return merged


def save_markers(
    fcs_path: str,
    mapping: dict[str, str],
    fcs=None,
) -> None:
    """Persist marker overrides to ``<fcs_path>.markers.json``.

    Only entries that differ from the FCS PnS defaults (when ``fcs`` is
    provided) are written, keeping the sidecar minimal. Empty marker
    strings are treated as "delete this entry".
    """
    defaults = _pns_defaults_from_fcs(fcs) if fcs is not None else {}
    to_save: dict[str, str] = {}
    for short, marker in mapping.items():
        if not isinstance(short, str) or not isinstance(marker, str):
            continue
        marker = marker.strip()
        if not marker:
            continue
        # Skip entries that match the PnS default — no need to override.
        if defaults.get(short) == marker:
            continue
        to_save[short] = marker

    sidecar = _sidecar_path(fcs_path)
    try:
        if to_save:
            with open(sidecar, "w") as fh:
                json.dump(to_save, fh, indent=2, sort_keys=True)
        elif os.path.exists(sidecar):
            # Nothing to save and file exists -> remove it for cleanliness.
            os.remove(sidecar)
    except OSError as e:
        logger.error("Failed to write marker sidecar %s: %s", sidecar, e)
        raise


def effective_channel_label(short_name: str, marker_map: dict[str, str]) -> str:
    """Return the user-facing label for a channel.

    ``"SHORT (marker)"`` when a marker exists; just ``"SHORT"`` otherwise.
    """
    marker = (marker_map or {}).get(short_name, "").strip()
    if marker:
        return f"{short_name} ({marker})"
    return short_name


def channel_label_lookup(
    short_names: Iterable[str], marker_map: dict[str, str]
) -> list[str]:
    """Vectorised helper — returns labels for a list of fluorophore names."""
    return [effective_channel_label(n, marker_map) for n in short_names]
