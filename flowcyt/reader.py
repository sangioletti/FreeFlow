"""
reader.py - Pure-Python FCS 3.x file reader.

Reads the HEADER, TEXT, and DATA segments of an FCS file without
any external dependencies beyond numpy.  Supports:
  * FCS 2.0 / 3.0 / 3.1
  * $DATATYPE = F (float), D (double), I (integer)
  * $MODE = L (list mode)
  * $BYTEORD = little-endian and big-endian
  * Embedded compensation / spillover matrices ($SPILLOVER, SPILL)
"""

from __future__ import annotations

import struct
import sys
import warnings

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

        # ---------- Compensation (spillover) ----------
        self.is_compensated = False
        self.spillover_matrix: np.ndarray | None = None
        self.spillover_channels: list[str] = []
        # Human-readable compensation notes surfaced to the UI (e.g. no
        # matrix present, or a diagonal matrix that applies no spillover).
        self.compensation_warnings: list[str] = []
        self._apply_compensation()

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
    #  Compensation / Spillover
    # ------------------------------------------------------------------ #
    def _apply_compensation(self):
        """Detect embedded spillover matrices, validate, and compensate.

        Checks the standard FCS keywords ``$SPILLOVER`` and the common
        vendor variant ``SPILL``.  If exactly one unique matrix is found
        the data is compensated in-place (spillover matrix inverted and
        applied to the relevant channels).  If more than one *distinct*
        matrix is found the programme aborts with an error.
        """
        # Collect all spillover keyword values present in the metadata
        spill_keywords = ("$SPILLOVER", "SPILL")
        found: dict[str, str] = {}  # keyword → raw value
        for key in spill_keywords:
            val = self.metadata.get(key)
            if val is not None and val.strip():
                found[key] = val.strip()

        if not found:
            # No embedded spillover matrix — data is left uncompensated.
            self.compensation_warnings.append(
                "No compensation/spillover matrix found in this file "
                "($SPILLOVER / SPILL absent). Data is NOT compensated."
            )
            return

        # De-duplicate by value — different keywords may carry the same matrix
        unique_values = list(set(found.values()))
        if len(unique_values) > 1:
            keywords_str = ", ".join(found.keys())
            msg = (
                f"FATAL: Multiple distinct compensation matrices found in "
                f"{self.filepath} (keywords: {keywords_str}).  Cannot "
                f"determine which one to use — aborting."
            )
            print(f"\n{'=' * 70}", file=sys.stderr)
            print(msg, file=sys.stderr)
            print(f"{'=' * 70}\n", file=sys.stderr)
            sys.exit(1)

        # We have exactly one unique spillover matrix
        source_keyword = list(found.keys())[0]
        raw_value = unique_values[0]

        try:
            n, channel_names, matrix = self._parse_spillover(raw_value)
        except Exception as exc:
            warnings.warn(
                f"Could not parse compensation matrix from {source_keyword}: "
                f"{exc}.  Proceeding without compensation."
            )
            return

        # A diagonal spillover matrix has no off-diagonal terms, i.e. no
        # spillover between channels — compensating with it is a no-op.
        off_diagonal = matrix - np.diag(np.diag(matrix))
        if np.allclose(off_diagonal, 0.0):
            self.compensation_warnings.append(
                f"Compensation matrix from {source_keyword} is diagonal "
                "(no off-diagonal spillover terms). Compensation has no "
                "effect on the data."
            )

        # Map spillover channel names to column indices in self.data
        col_indices: list[int] = []
        for ch in channel_names:
            idx = self._find_channel_index(ch)
            if idx is None:
                warnings.warn(
                    f"Compensation matrix references channel '{ch}' which "
                    f"is not in the file.  Proceeding without compensation."
                )
                return
            col_indices.append(idx)

        # Invert the spillover matrix to obtain the compensation matrix
        try:
            comp_matrix = np.linalg.inv(matrix)
        except np.linalg.LinAlgError:
            warnings.warn(
                "Spillover matrix is singular — cannot invert.  "
                "Proceeding without compensation."
            )
            return

        # Apply compensation: for each event, multiply the relevant
        # channel values by the inverse spillover (compensation) matrix.
        # data[:, cols] = data[:, cols] @ comp_matrix
        subset = self.data[:, col_indices].copy()
        self.data[:, col_indices] = subset @ comp_matrix

        self.is_compensated = True
        self.spillover_matrix = matrix
        self.spillover_channels = list(channel_names)

        # Print terminal warning so the user knows compensation was applied
        kw_str = (
            " & ".join(found.keys()) if len(found) > 1
            else list(found.keys())[0]
        )
        print(f"\n{'=' * 70}", file=sys.stderr)
        print(
            f"  WARNING — Compensation matrix detected ({kw_str})",
            file=sys.stderr,
        )
        print(
            f"  {n} channels: {', '.join(channel_names)}",
            file=sys.stderr,
        )
        print(
            f"  Data has been compensated in-place.  All subsequent",
            file=sys.stderr,
        )
        print(
            f"  analyses will use the compensated values.",
            file=sys.stderr,
        )
        print(f"{'=' * 70}\n", file=sys.stderr)

    @staticmethod
    def _parse_spillover(raw: str) -> tuple[int, list[str], np.ndarray]:
        """Parse a ``$SPILLOVER`` / ``SPILL`` value string.

        Format: ``n,Name1,Name2,...,NameN,S11,S12,...,SNN``

        Returns (n, channel_names, matrix) where *matrix* is n×n
        (row-major, dtype float64).
        """
        parts = [p.strip() for p in raw.split(",")]
        n = int(parts[0])
        if n <= 0:
            raise ValueError(f"Invalid channel count in spillover: {n}")

        expected_parts = 1 + n + n * n
        if len(parts) < expected_parts:
            raise ValueError(
                f"Spillover string too short: expected {expected_parts} "
                f"comma-separated values, got {len(parts)}"
            )

        channel_names = parts[1 : 1 + n]
        coeffs = [float(v) for v in parts[1 + n : 1 + n + n * n]]
        matrix = np.array(coeffs, dtype=np.float64).reshape(n, n)
        return n, channel_names, matrix

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
            lines.append(
                f"Compensation: applied ({len(self.spillover_channels)} channels)"
            )
        else:
            lines.append("Compensation: none")
        return "\n".join(lines)
