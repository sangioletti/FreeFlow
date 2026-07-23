"""
reader.py - Pure-Python FCS 3.x file reader.

Reads the HEADER, TEXT, and DATA segments of an FCS file without
any external dependencies beyond numpy.  Supports:
  * FCS 2.0 / 3.0 / 3.1
  * $DATATYPE = F (float), D (double), I (integer)
  * $MODE = L (list mode)
  * $BYTEORD = little-endian and big-endian

Compensation is deliberately NOT read from the FCS file.  The embedded
``$SPILLOVER`` / ``SPILL`` keywords are ignored; compensation comes only
from an external settings XML (see compensation.py).  A file is treated
as already-compensated purely by its name ending in ``_compensated.fcs``.
"""

from __future__ import annotations

import numpy as np


class FCSData:
    """Container for parsed FCS file data."""

    def __init__(self, filepath: str):
        self.filepath: str = filepath
        self.metadata: dict[str, str] = {}
        self.data: np.ndarray | None = None
        self.channel_names: list[str] = []
        self.channel_labels: list[str] = []
        self.num_events: int = 0
        self.num_channels: int = 0
        self._load()

    # ------------------------------------------------------------------ #
    #  Internal: parse the binary FCS file
    # ------------------------------------------------------------------ #
    def _load(self):
        with open(self.filepath, "rb") as fh:
            raw = fh.read()

        # ---------- HEADER (first 58 bytes) ----------
        version = raw[0:6].decode("ascii").strip()
        if version not in ("FCS2.0", "FCS3.0", "FCS3.1"):
            raise ValueError(f"Unsupported FCS version: {version!r}")

        text_start = int(raw[10:18].decode("ascii").strip())
        text_end = int(raw[18:26].decode("ascii").strip())
        data_start = int(raw[26:34].decode("ascii").strip())
        data_end = int(raw[34:42].decode("ascii").strip())

        # ---------- TEXT segment ----------
        text_raw = raw[text_start : text_end + 1].decode("latin-1")
        self.metadata = self._parse_text(text_raw)

        # If offsets in header are 0 the real offsets are in TEXT
        if data_start == 0:
            data_start = int(self.metadata.get("$BEGINDATA", 0))
            data_end = int(self.metadata.get("$ENDDATA", 0))

        # ---------- Channel info ----------
        n_par = int(self.metadata["$PAR"])
        n_events = int(self.metadata["$TOT"])
        self.num_channels = n_par
        self.num_events = n_events

        self.channel_names = []
        self.channel_labels = []
        for i in range(1, n_par + 1):
            name = self.metadata.get(f"$P{i}N", f"p{i}")
            label = self.metadata.get(f"$P{i}S", "")
            self.channel_names.append(name)
            self.channel_labels.append(label if label else name)

        # ---------- DATA segment ----------
        data_raw = raw[data_start : data_end + 1]
        datatype = self.metadata.get("$DATATYPE", "F").upper()
        byteord = self.metadata.get("$BYTEORD", "1,2,3,4")
        endian = "<" if byteord.startswith("1") else ">"

        if datatype == "F":
            dt = np.dtype(f"{endian}f4")
        elif datatype == "D":
            dt = np.dtype(f"{endian}f8")
        elif datatype == "I":
            # Integer mode – bit widths per parameter
            dt = self._build_int_dtype(endian, n_par)
        else:
            raise ValueError(f"Unsupported $DATATYPE: {datatype!r}")

        if datatype in ("F", "D"):
            flat = np.frombuffer(data_raw, dtype=dt)
            expected = n_events * n_par
            if len(flat) < expected:
                raise ValueError(
                    f"DATA segment too short: got {len(flat)} values, "
                    f"expected {expected} ({n_events} events × {n_par} params)"
                )
            self.data = flat[:expected].reshape(n_events, n_par).astype(np.float64)
        else:
            # Integer: read row-by-row with heterogeneous widths
            self.data = self._read_int_data(data_raw, endian, n_par, n_events)

        # ---------- Compensation status (name-based only) ----------
        # Embedded spillover is intentionally never read.  A file counts as
        # compensated iff its name ends in ``_compensated.fcs``; the actual
        # compensation is done up-front from the external XML.
        self.is_compensated = self.filepath.lower().endswith("_compensated.fcs")
        # Retained for API compatibility with older callers; always empty
        # now that spillover is not sourced from the FCS itself.
        self.spillover_matrix: np.ndarray | None = None
        self.spillover_channels: list[str] = []
        self.compensation_warnings: list[str] = []

    def _build_int_dtype(self, endian: str, n_par: int):
        """Build a numpy structured dtype for integer-mode data."""
        formats = []
        for i in range(1, n_par + 1):
            bits = int(self.metadata.get(f"$P{i}B", "16"))
            if bits <= 8:
                formats.append(f"{endian}u1")
            elif bits <= 16:
                formats.append(f"{endian}u2")
            elif bits <= 32:
                formats.append(f"{endian}u4")
            else:
                formats.append(f"{endian}u8")
        return np.dtype([(f"p{i+1}", f) for i, f in enumerate(formats)])

    def _read_int_data(
        self, data_raw: bytes, endian: str, n_par: int, n_events: int
    ) -> np.ndarray:
        dt = self._build_int_dtype(endian, n_par)
        structured = np.frombuffer(data_raw, dtype=dt, count=n_events)
        out = np.empty((n_events, n_par), dtype=np.float64)
        for i in range(n_par):
            out[:, i] = structured[f"p{i+1}"].astype(np.float64)
        return out

    @staticmethod
    def _parse_text(text: str) -> dict[str, str]:
        """Parse the TEXT segment key/value pairs."""
        if not text:
            return {}
        delim = text[0]
        # Strip leading/trailing delimiters
        text = text.strip(delim)
        parts = text.split(delim)
        meta: dict[str, str] = {}
        i = 0
        while i < len(parts) - 1:
            key = parts[i].strip()
            val = parts[i + 1].strip()
            if key:
                meta[key.upper()] = val
            i += 2
        return meta

    # ------------------------------------------------------------------ #
    #  Channel lookup
    # ------------------------------------------------------------------ #
    def _find_channel_index(self, name: str) -> int | None:
        """Find the column index for a channel name (case-insensitive).

        Tries exact match on ``$PnN`` names first, then on labels.
        """
        # Exact match on short names
        for i, ch in enumerate(self.channel_names):
            if ch == name:
                return i
        # Case-insensitive match on short names
        name_lower = name.lower()
        for i, ch in enumerate(self.channel_names):
            if ch.lower() == name_lower:
                return i
        # Match on labels
        for i, lbl in enumerate(self.channel_labels):
            if lbl == name or lbl.lower() == name_lower:
                return i
        return None

    # ------------------------------------------------------------------ #
    #  Convenience helpers
    # ------------------------------------------------------------------ #
    def display_names(self) -> list[str]:
        """Return labels combining short name + label when available."""
        names = []
        for short, label in zip(self.channel_names, self.channel_labels):
            if label and label != short:
                names.append(f"{short} ({label})")
            else:
                names.append(short)
        return names

    def get_channel(self, name_or_index) -> np.ndarray:
        """Return a 1-D array for a given channel by name or index."""
        if isinstance(name_or_index, int):
            return self.data[:, name_or_index]
        for idx, (n, lbl) in enumerate(
            zip(self.channel_names, self.channel_labels)
        ):
            if name_or_index in (n, lbl):
                return self.data[:, idx]
        raise KeyError(f"Channel '{name_or_index}' not found.")

    def get_channel_index(self, name: str) -> int:
        """Return column index for a channel name or label."""
        for idx, (n, lbl) in enumerate(
            zip(self.channel_names, self.channel_labels)
        ):
            if name in (n, lbl):
                return idx
        for idx, dn in enumerate(self.display_names()):
            if name == dn:
                return idx
        raise KeyError(f"Channel '{name}' not found.")

    def summary(self) -> str:
        lines = [
            f"File  : {self.filepath}",
            f"Events: {self.num_events:,}",
            f"Channels ({self.num_channels}):",
        ]
        for i, (n, lbl) in enumerate(
            zip(self.channel_names, self.channel_labels)
        ):
            lines.append(f"  {i}: {n:20s}  {lbl}")
        if self.is_compensated:
            lines.append("Compensation: applied (file is a _compensated.fcs)")
        else:
            lines.append(
                "Compensation: none (raw file — compensate via settings XML)"
            )
        return "\n".join(lines)
