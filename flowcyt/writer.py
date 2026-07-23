"""
writer.py - Minimal FCS 3.1 writer.

Serialises an in-memory data array (plus the metadata of the FCSData it
was derived from) back to a valid FCS 3.1 list-mode file.  Used to
persist compensated data as ``<name>_compensated.fcs``.

Design choices (kept deliberately simple and robust):
  * Output is always ``$DATATYPE = F`` (32-bit float, little-endian).
  * ``$MODE = L`` (list mode), one $NEXTDATA-less dataset.
  * Every ``$PnB`` is rewritten to 32 to match the float payload.
  * Any embedded spillover keywords ($SPILLOVER / SPILL) are dropped -
    compensation now lives exclusively in the external XML, and the data
    we write is *already* compensated, so leaving a spillover matrix in
    the file would be misleading and is explicitly unwanted.
  * Offset keywords are written at a fixed width so the TEXT segment
    length does not depend on the numeric value of the offsets it
    contains (avoids the classic FCS offset/length circular dependency).
"""

from __future__ import annotations

import numpy as np

# Keys we manage ourselves and therefore strip from the copied metadata
# before re-emitting (they are all rewritten with correct values below).
_MANAGED_KEYS = {
    "$BEGINDATA", "$ENDDATA", "$BEGINANALYSIS", "$ENDANALYSIS",
    "$BEGINSTEXT", "$ENDSTEXT", "$NEXTDATA", "$DATATYPE", "$MODE",
    "$BYTEORD", "$PAR", "$TOT",
    # Spillover must never be carried into a compensated file.
    "$SPILLOVER", "SPILL", "$COMP",
}

# Fixed field width (characters) for the data-offset keywords in TEXT.
_OFFSET_WIDTH = 12
# TEXT segment starts at this byte offset, mirroring typical BD/Sony files
# and leaving a comfortable header gap.
_TEXT_START = 256

# Candidate TEXT delimiters, in preference order.  We pick the first one
# that appears in no key or value so we never need delimiter escaping.
_DELIM_CANDIDATES = ("\x0c", "\x1e", "|", "/", "\\", "!", "~", "\n")


def _choose_delimiter(pairs: dict[str, str]) -> str:
    blob = "".join(pairs.keys()) + "".join(pairs.values())
    for cand in _DELIM_CANDIDATES:
        if cand not in blob:
            return cand
    raise ValueError("Could not find an unused TEXT delimiter for FCS output")


def write_fcs(src, data: np.ndarray, out_path: str,
              extra_keywords: dict[str, str] | None = None) -> None:
    """Write *data* to *out_path* as an FCS 3.1 file.

    Parameters
    ----------
    src :
        The :class:`~flowcyt.reader.FCSData` the data was derived from -
        its ``metadata``, ``channel_names`` and ``channel_labels`` are the
        template for the output TEXT segment.
    data :
        ``(n_events, n_par)`` array.  Written as little-endian float32.
    out_path :
        Destination path.
    extra_keywords :
        Optional non-standard keywords to add (e.g. a compensation marker).
    """
    data = np.ascontiguousarray(data, dtype="<f4")
    if data.ndim != 2:
        raise ValueError(f"data must be 2-D, got shape {data.shape}")
    n_events, n_par = data.shape

    # ---- Build the keyword dictionary ----
    meta: dict[str, str] = {}
    for key, val in src.metadata.items():
        if key.upper() in _MANAGED_KEYS:
            continue
        meta[key] = val

    # Rewrite the parameters we control.
    meta["$PAR"] = str(n_par)
    meta["$TOT"] = str(n_events)
    meta["$MODE"] = "L"
    meta["$DATATYPE"] = "F"
    meta["$BYTEORD"] = "1,2,3,4"
    meta["$NEXTDATA"] = "0"
    meta["$BEGINANALYSIS"] = "0"
    meta["$ENDANALYSIS"] = "0"
    meta["$BEGINSTEXT"] = "0"
    meta["$ENDSTEXT"] = "0"
    for i in range(1, n_par + 1):
        meta[f"$P{i}B"] = "32"

    if extra_keywords:
        meta.update(extra_keywords)

    # Placeholder offsets (fixed width so TEXT length is stable).
    meta["$BEGINDATA"] = "0" * _OFFSET_WIDTH
    meta["$ENDDATA"] = "0" * _OFFSET_WIDTH

    delim = _choose_delimiter(meta)

    def render_text(m: dict[str, str]) -> bytes:
        body = delim + delim.join(f"{k}{delim}{v}" for k, v in m.items()) + delim
        return body.encode("latin-1")

    # First render tells us the TEXT length (stable because offset fields
    # are fixed width), from which the DATA offsets follow.
    text_bytes = render_text(meta)
    text_start = _TEXT_START
    text_end = text_start + len(text_bytes) - 1
    data_start = text_end + 1
    data_bytes = data.tobytes()
    data_end = data_start + len(data_bytes) - 1

    # Fill the real offset values (same width -> TEXT length unchanged).
    meta["$BEGINDATA"] = str(data_start).zfill(_OFFSET_WIDTH)
    meta["$ENDDATA"] = str(data_end).zfill(_OFFSET_WIDTH)
    text_bytes = render_text(meta)
    assert text_start + len(text_bytes) - 1 == text_end, "TEXT length drifted"

    # ---- HEADER (58 bytes) ----
    # 8-char right-justified ASCII offsets.  If an offset does not fit in
    # 8 digits, the spec permits writing 0 in the header (the true value
    # still lives in $BEGINDATA / $ENDDATA inside TEXT).
    def hdr(n: int) -> bytes:
        s = str(n)
        return (s if len(s) <= 8 else "0").rjust(8).encode("ascii")

    header = (
        b"FCS3.1"
        + b"    "                       # 4 spaces (bytes 6-9)
        + str(text_start).rjust(8).encode("ascii")
        + str(text_end).rjust(8).encode("ascii")
        + hdr(data_start)
        + hdr(data_end)
        + b"       0"                    # analysis start
        + b"       0"                    # analysis end
    )
    assert len(header) == 58, len(header)

    # ---- Assemble ----
    out = bytearray(data_start + len(data_bytes))
    out[0:58] = header
    # gap between header and TEXT is space-filled
    for i in range(58, text_start):
        out[i] = 0x20
    out[text_start:text_start + len(text_bytes)] = text_bytes
    out[data_start:data_start + len(data_bytes)] = data_bytes

    with open(out_path, "wb") as fh:
        fh.write(out)
