"""
markers.py - Per-file fluorophore -> protein marker mapping + hidden channels.

Each FCS file may have a sidecar JSON named ``<fcs_path>.markers.json`` that
records:

  * a ``fluorophore -> protein marker`` mapping (overrides FCS PnS labels), and
  * an optional list of fluorophores the user has hidden from the channel
    selectors (the underlying FCS file is *not* modified — this is purely a
    per-user view filter).

Two storage formats are supported transparently so old sidecars keep working:

* Flat (legacy):   ``{"FL1-A": "CD4", "FL2-A": "CD8"}``
* Structured:     ``{"markers": {...}, "hidden": ["FSC-W", ...]}``

When ``save_markers`` is asked to persist a hidden list (or there's already a
hidden list on disk), the structured format is written.

Public API:
    load_markers(fcs_path, fcs=None)
    load_hidden_channels(fcs_path)
    save_markers(fcs_path, mapping, fcs=None, hidden=None)
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


def _read_sidecar(fcs_path: str,
                  path_override: str | None = None) -> tuple[dict[str, str], set[str]]:
    """Read the sidecar JSON and return ``(overrides, hidden)``.

    Supports both the legacy flat format and the structured format.
    Missing file → ``({}, set())``.  Malformed JSON is logged and treated
    as an empty file.

    If ``path_override`` is given it is read instead of the canonical
    ``<fcs_path>.markers.json`` sidecar — used by the marker editor's
    "Load Scheme..." button to pick an arbitrary JSON file.
    """
    sidecar = path_override or _sidecar_path(fcs_path)
    if not os.path.exists(sidecar):
        return {}, set()

    try:
        with open(sidecar, "r") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to read marker sidecar %s: %s", sidecar, e)
        return {}, set()

    if not isinstance(data, dict):
        return {}, set()

    if "markers" in data or "hidden" in data:
        # Structured format.
        raw_markers = data.get("markers", {}) or {}
        raw_hidden = data.get("hidden", []) or []
    else:
        # Legacy flat format — every string value is a marker override.
        raw_markers = data
        raw_hidden = []

    overrides: dict[str, str] = {}
    if isinstance(raw_markers, dict):
        for k, v in raw_markers.items():
            if isinstance(k, str) and isinstance(v, str) and v:
                overrides[k] = v

    hidden: set[str] = set()
    if isinstance(raw_hidden, (list, tuple, set)):
        for entry in raw_hidden:
            if isinstance(entry, str) and entry:
                hidden.add(entry)
    return overrides, hidden


def load_markers(fcs_path: str, fcs=None,
                 path_override: str | None = None) -> dict[str, str]:
    """Load merged marker map for a given FCS file.

    Resolution order:
        1. Defaults from the FCS file's PnS labels (when ``fcs`` is passed).
        2. Overrides from ``<fcs_path>.markers.json`` (wins on conflict),
           or from ``path_override`` when explicitly given (the marker
           editor's "Load Scheme..." flow).

    Returns ``{fluorophore_short_name: marker}``. Empty dict if nothing
    available.
    """
    merged: dict[str, str] = {}
    if fcs is not None:
        merged.update(_pns_defaults_from_fcs(fcs))
    overrides, _hidden = _read_sidecar(fcs_path, path_override=path_override)
    merged.update(overrides)
    return merged


def load_hidden_channels(fcs_path: str,
                         path_override: str | None = None) -> set[str]:
    """Return the set of fluorophore short names the user has hidden."""
    _overrides, hidden = _read_sidecar(fcs_path, path_override=path_override)
    return hidden


def save_markers(
    fcs_path: str,
    mapping: dict[str, str],
    fcs=None,
    hidden: Iterable[str] | None = None,
    path_override: str | None = None,
) -> str:
    """Persist marker overrides (and optionally a hidden-channel list) to
    ``<fcs_path>.markers.json``.

    Only entries that differ from the FCS PnS defaults (when ``fcs`` is
    provided) are written, keeping the sidecar minimal.  Empty marker
    strings are treated as "delete this entry".

    ``hidden=None`` preserves whatever's already on disk; pass an empty
    iterable to explicitly clear the hidden list.  When ``hidden`` ends
    up non-empty (or one already existed on disk), the structured format
    is written; otherwise the legacy flat format keeps backward
    compatibility.

    ``path_override`` lets the marker editor's "Save Scheme..." button
    write to an arbitrary file path instead of the canonical
    ``<fcs_path>.markers.json`` sidecar.  The same file format is used
    so the chosen file can be loaded back via ``load_markers`` /
    ``load_hidden_channels`` with the same override.

    Returns the absolute path written (or an empty string if the
    sidecar was deleted because both markers and hidden were empty).
    """
    defaults = _pns_defaults_from_fcs(fcs) if fcs is not None else {}

    to_save: dict[str, str] = {}
    for short, marker in mapping.items():
        if not isinstance(short, str) or not isinstance(marker, str):
            continue
        marker = marker.strip()
        if not marker:
            continue
        if defaults.get(short) == marker:
            continue
        to_save[short] = marker

    # Hidden-channel resolution: explicit arg wins, otherwise preserve
    # whatever is already on disk (only meaningful for the canonical
    # sidecar path; for path_override we treat None as "no hidden").
    if hidden is None:
        if path_override is None:
            _existing, on_disk_hidden = _read_sidecar(fcs_path)
            hidden_set = set(on_disk_hidden)
        else:
            hidden_set = set()
    else:
        hidden_set = {s for s in hidden if isinstance(s, str) and s}

    sidecar = path_override or _sidecar_path(fcs_path)
    try:
        if not to_save and not hidden_set:
            if os.path.exists(sidecar):
                os.remove(sidecar)
            return ""
        if hidden_set:
            # Structured format — necessary to carry the hidden list.
            payload = {
                "markers": dict(sorted(to_save.items())),
                "hidden": sorted(hidden_set),
            }
        else:
            # No hidden state — keep the legacy flat format for simplicity.
            payload = dict(sorted(to_save.items()))
        with open(sidecar, "w") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        return os.path.abspath(sidecar)
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
