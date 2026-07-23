"""
compensation.py - External-XML compensation for FlowCyt.

Compensation matrices are read *exclusively* from a BD FACSDiva-style XML
settings file that must sit in the same folder as the ``.fcs`` files - the
embedded ``$SPILLOVER`` / ``SPILL`` keywords inside FCS files are never
consulted (see reader.py).

Workflow (see :func:`prepare_directory`):
  1. Locate the single settings ``.xml`` in a directory.
  2. Parse the *global* (experiment-level) compensation matrix from it.
  3. For every raw ``*.fcs`` that is compatible with the matrix (i.e. it
     contains all of the matrix's channels), write a compensated twin
     ``*_compensated.fcs`` if one does not already exist.

The matrix stored under each ``<parameter>``'s ``<compensation>`` element
is the *already-inverted* compensation matrix (diagonal ~1, off-diagonal
terms small and signed), so it is applied directly - not inverted.
"""

from __future__ import annotations

import glob
import logging
import os
import xml.etree.ElementTree as ET

import numpy as np

from .reader import FCSData
from .writer import write_fcs

logger = logging.getLogger(__name__)

COMPENSATED_SUFFIX = "_compensated.fcs"


class CompensationError(Exception):
    """Raised when the XML is present but its matrix cannot be used."""


class CompMatrix:
    """A named N×N compensation matrix parsed from the settings XML."""

    def __init__(self, channels: list[str], matrix: np.ndarray, source: str):
        self.channels = channels            # row/column parameter names
        self.matrix = matrix                # (N, N) float64, row = output param
        self.source = source                # XML basename it came from

    def __repr__(self) -> str:
        return f"CompMatrix({len(self.channels)} channels from {self.source!r})"


# ---------------------------------------------------------------------- #
#  XML format detection
# ---------------------------------------------------------------------- #
FORMAT_BDFACS = "bdfacs"
FORMAT_SONY_ID7000 = "sony_id7000"
FORMAT_UNKNOWN = "unknown"

# Case-insensitive substrings that identify a Sony ID7000 export when the
# root element is not BD FACSDiva's ``<bdfacs>``.  The ID7000 is a spectral
# analyser, so its settings revolve around unmixing / spectral references
# rather than a classic spillover matrix.
_SONY_SIGNATURES = (
    "id7000", "sony", "spectralreference", "unmixing", "unmix", "wlsm",
)


def detect_xml_format(xml_path: str) -> str:
    """Classify a settings XML as BD FACSDiva, Sony ID7000, or unknown.

    Detection is by the root element (``<bdfacs>`` ⇒ BD FACSDiva) and, for
    anything else, by scanning a leading chunk of the document for Sony
    ID7000 signature strings.  Reads only the head of the file, so it is
    cheap even on large exports.
    """
    # Root element first - the most reliable discriminator for BD.
    try:
        for _event, elem in ET.iterparse(xml_path, events=("start",)):
            root_tag = elem.tag.lower()
            break
        else:
            root_tag = ""
    except ET.ParseError:
        return FORMAT_UNKNOWN

    if root_tag == "bdfacs":
        return FORMAT_BDFACS
    if "id7000" in root_tag or "sony" in root_tag:
        return FORMAT_SONY_ID7000

    # Fall back to a content sniff over the first 64 KB.
    try:
        with open(xml_path, "r", encoding="utf-8", errors="ignore") as fh:
            head = fh.read(65536).lower()
    except OSError:
        return FORMAT_UNKNOWN
    if any(sig in head for sig in _SONY_SIGNATURES):
        return FORMAT_SONY_ID7000
    return FORMAT_UNKNOWN


# ---------------------------------------------------------------------- #
#  XML discovery & parsing
# ---------------------------------------------------------------------- #
def find_settings_xml(directory: str) -> str | None:
    """Return the path to a recognised settings XML in *directory*, or None.

    A settings XML is any ``*.xml`` that :func:`detect_xml_format` classifies
    as a known machine format (BD FACSDiva or Sony ID7000).  If several
    qualify, the first (sorted) one is used and a warning logged.
    """
    candidates = sorted(glob.glob(os.path.join(directory, "*.xml"))
                        + glob.glob(os.path.join(directory, "*.XML")))
    valid = [p for p in candidates if detect_xml_format(p) != FORMAT_UNKNOWN]
    if not valid:
        return None
    if len(valid) > 1:
        logger.warning("Multiple settings XML files found; using %s",
                       os.path.basename(valid[0]))
    return valid[0]


def _matrix_from_settings(settings: ET.Element):
    """Extract (channels, matrix) from one <instrument_settings>, or None."""
    channels: list[str] = []
    rows: list[list[float]] = []
    for param in settings.findall("parameter"):
        comp = param.find("compensation")
        if comp is None:
            continue
        coeffs = comp.findall("compensation_coefficient")
        if not coeffs:
            continue
        name = param.get("name")
        if not name:
            continue
        channels.append(name)
        rows.append([float(c.text) for c in coeffs])
    if not rows:
        return None
    n = len(rows)
    if any(len(r) != n for r in rows):
        raise CompensationError(
            f"Compensation matrix is not square: {n} parameters but rows "
            f"have lengths {sorted({len(r) for r in rows})}"
        )
    return channels, np.array(rows, dtype=np.float64)


def parse_compensation(xml_path: str) -> CompMatrix:
    """Parse a compensation matrix from *xml_path*, dispatching on format.

    Recognises BD FACSDiva and Sony ID7000 exports; raises
    :class:`CompensationError` for anything else.
    """
    fmt = detect_xml_format(xml_path)
    if fmt == FORMAT_BDFACS:
        return _parse_bdfacs(xml_path)
    if fmt == FORMAT_SONY_ID7000:
        return _parse_sony_id7000(xml_path)
    raise CompensationError(
        f"Unrecognised settings XML format: {os.path.basename(xml_path)}. "
        "Expected BD FACSDiva (<bdfacs> root) or Sony ID7000."
    )


def _parse_bdfacs(xml_path: str) -> CompMatrix:
    """Parse the authoritative global compensation matrix from a BD FACSDiva XML.

    The matrix used is the one on the ``<instrument_settings>`` that is a
    direct child of ``<experiment>`` (the global "Cytometer Settings").
    With ``$is_use_global_settings = true`` these global settings apply to
    every tube, so per-tube/per-worksheet matrices are ignored.

    Falls back to a unique matrix if the global one is absent, and raises
    if no matrix exists or several irreconcilable ones do.
    """
    root = ET.parse(xml_path).getroot()
    source = os.path.basename(xml_path)

    experiment = root.find("experiment")
    if experiment is not None:
        for settings in experiment.findall("instrument_settings"):
            result = _matrix_from_settings(settings)
            if result is not None:
                channels, matrix = result
                logger.info("Using experiment-level compensation matrix "
                            "(%d channels) from %s", len(channels), source)
                return CompMatrix(channels, matrix, source)

    # Fallback: gather every matrix in the file and require agreement on
    # the channel set (values may differ negligibly between tubes).
    found = []
    for settings in root.iter("instrument_settings"):
        result = _matrix_from_settings(settings)
        if result is not None:
            found.append(result)
    if not found:
        raise CompensationError(
            f"No compensation matrix found in {source} "
            "(no <parameter><compensation> blocks)."
        )
    channel_sets = {tuple(ch) for ch, _ in found}
    if len(channel_sets) > 1:
        raise CompensationError(
            f"{source} contains compensation matrices over different "
            f"channel sets and no global experiment-level matrix to "
            f"disambiguate: {channel_sets}"
        )
    channels, matrix = found[0]
    return CompMatrix(channels, matrix, source)


def _parse_sony_id7000(xml_path: str) -> CompMatrix:
    """Best-effort parse of a Sony ID7000 spectral settings XML.

    The ID7000 is a spectral analyser that uses *unmixing* rather than a
    classic spillover matrix, and Sony does not publish a documented XML
    schema for exporting that matrix.  We therefore look for a generic
    square, named coefficient matrix and use it directly; if none can be
    found we raise an explicit, actionable error instead of guessing (a
    wrong matrix would silently corrupt the data).
    """
    source = os.path.basename(xml_path)
    root = ET.parse(xml_path).getroot()
    result = _find_generic_matrix(root)
    if result is None:
        raise CompensationError(
            f"{source} looks like a Sony ID7000 export, but no recognisable "
            "unmixing/compensation matrix could be extracted from it. The "
            "ID7000 XML matrix layout is not yet supported - please share a "
            "sample file so it can be added."
        )
    channels, matrix = result
    logger.info("Using Sony ID7000 unmixing matrix (%d channels) from %s",
                len(channels), source)
    return CompMatrix(channels, matrix, source)


def _find_generic_matrix(root: ET.Element):
    """Search an XML tree for a square, named coefficient matrix.

    Looks for a container whose children each carry a name/parameter and an
    equal-length list of numeric coefficients, forming an N×N matrix over N
    parameters.  Returns (channel_names, matrix) or None.  Deliberately
    conservative: it only accepts an unambiguous square result.
    """
    name_attrs = ("name", "parameter", "param", "fluor", "channel", "id")
    coeff_tag_hints = ("coefficient", "coeff", "value", "spillover", "factor")

    def row_from(elem: ET.Element):
        # A row's name may be an attribute or a child element.
        name = None
        for attr in name_attrs:
            if elem.get(attr):
                name = elem.get(attr)
                break
        if name is None:
            for child in elem:
                if child.tag.lower() in name_attrs and (child.text or "").strip():
                    name = child.text.strip()
                    break
        # Collect numeric coefficients from child elements that look numeric.
        coeffs: list[float] = []
        for child in elem:
            tag = child.tag.lower()
            txt = (child.text or "").strip()
            if not txt:
                continue
            if any(h in tag for h in coeff_tag_hints) or tag in name_attrs:
                try:
                    coeffs.append(float(txt))
                except ValueError:
                    continue
        return name, coeffs

    best = None
    for container in root.iter():
        children = list(container)
        if len(children) < 2:
            continue
        names: list[str] = []
        rows: list[list[float]] = []
        for child in children:
            name, coeffs = row_from(child)
            if name is None or not coeffs:
                names = []
                break
            names.append(name)
            rows.append(coeffs)
        if not rows:
            continue
        n = len(rows)
        if all(len(r) == n for r in rows) and len(set(names)) == n and n >= 2:
            best = (names, np.array(rows, dtype=np.float64))
            break
    return best


# ---------------------------------------------------------------------- #
#  Compatibility & application
# ---------------------------------------------------------------------- #
def is_compatible(fcs: FCSData, comp: CompMatrix) -> bool:
    """True if *fcs* contains every channel referenced by the matrix."""
    return all(fcs._find_channel_index(ch) is not None for ch in comp.channels)


def apply_compensation(fcs: FCSData, comp: CompMatrix) -> np.ndarray:
    """Return a compensated copy of ``fcs.data``.

    Only the matrix's channels are altered.  For each event the vector of
    those channels ``v`` becomes ``M @ v`` (the ``<compensation>`` block of
    parameter *j* is row *j* of ``M``), i.e. ``out = v @ M.T``.
    """
    col_indices = [fcs._find_channel_index(ch) for ch in comp.channels]
    if any(idx is None for idx in col_indices):
        missing = [ch for ch, idx in zip(comp.channels, col_indices)
                   if idx is None]
        raise CompensationError(f"Channels missing from FCS: {missing}")

    out = fcs.data.copy()
    subset = out[:, col_indices]
    out[:, col_indices] = subset @ comp.matrix.T
    return out


def compensated_path(fcs_path: str) -> str:
    """Map ``foo.fcs`` (any case) to ``foo_compensated.fcs``."""
    base = fcs_path
    for ext in (".fcs", ".FCS"):
        if base.endswith(ext):
            base = base[: -len(ext)]
            break
    return base + COMPENSATED_SUFFIX


def is_compensated_file(path: str) -> bool:
    return path.lower().endswith(COMPENSATED_SUFFIX)


def generate_compensated(fcs_path: str, comp: CompMatrix,
                         overwrite: bool = False) -> str | None:
    """Write ``<name>_compensated.fcs`` for *fcs_path* if compatible.

    Returns the output path if a file was written, None if the source is
    incompatible with the matrix.  Existing outputs are kept unless
    *overwrite* is set.
    """
    out_path = compensated_path(fcs_path)
    if os.path.exists(out_path) and not overwrite:
        return out_path

    fcs = FCSData(fcs_path)
    if not is_compatible(fcs, comp):
        return None

    data = apply_compensation(fcs, comp)
    write_fcs(
        fcs, data, out_path,
        extra_keywords={
            "FREEFLOW_COMPENSATED": "1",
            "FREEFLOW_COMP_SOURCE": comp.source,
        },
    )
    logger.info("Wrote compensated file %s", os.path.basename(out_path))
    return out_path


# ---------------------------------------------------------------------- #
#  Directory-level orchestration
# ---------------------------------------------------------------------- #
class DirectoryResult:
    """Outcome of preparing a directory for viewing."""

    def __init__(self):
        self.xml_path: str | None = None
        self.comp: CompMatrix | None = None
        self.generated: list[str] = []     # newly written or existing twins
        self.incompatible: list[str] = []  # raw files the matrix didn't fit
        self.messages: list[str] = []      # human-readable notes for the UI

    @property
    def has_usable_files(self) -> bool:
        return bool(self.generated)


def _raw_fcs_in(directory: str) -> list[str]:
    found: set[str] = set()
    for pat in ("*.fcs", "*.FCS"):
        found.update(glob.glob(os.path.join(directory, pat)))
    return sorted(p for p in found if not is_compensated_file(p))


def _existing_compensated_in(directory: str) -> list[str]:
    return sorted(
        p for p in glob.glob(os.path.join(directory, "*" + COMPENSATED_SUFFIX))
    )


def prepare_directory(directory: str) -> DirectoryResult:
    """Ensure *directory* has compensated files ready to view.

    Rules (per project spec):
      * If a settings XML exists, generate a ``*_compensated.fcs`` for every
        compatible raw ``*.fcs``.
      * If no XML exists but ``*_compensated.fcs`` files already do, use them.
      * If neither exists, the result has no usable files - the caller is
        expected to warn and quit.
    """
    directory = os.path.abspath(directory)
    result = DirectoryResult()

    xml_path = find_settings_xml(directory)
    existing = _existing_compensated_in(directory)

    if xml_path is None:
        if existing:
            result.generated = existing
            result.messages.append(
                f"No settings XML in this folder - using "
                f"{len(existing)} existing *_compensated.fcs file(s)."
            )
        return result

    result.xml_path = xml_path
    fmt_label = {
        FORMAT_BDFACS: "BD FACSDiva",
        FORMAT_SONY_ID7000: "Sony ID7000",
    }.get(detect_xml_format(xml_path), "unknown")
    try:
        comp = parse_compensation(xml_path)
    except CompensationError as exc:
        result.messages.append(f"WARNING: {exc}")
        # Fall back to any already-compensated files.
        result.generated = existing
        return result

    result.comp = comp
    result.messages.append(
        f"{fmt_label} matrix loaded from {os.path.basename(xml_path)} "
        f"({len(comp.channels)} channels): {', '.join(comp.channels)}"
    )

    produced: list[str] = []
    for raw in _raw_fcs_in(directory):
        try:
            out = generate_compensated(raw, comp)
        except Exception as exc:  # noqa: BLE001 - keep going for other files
            result.messages.append(
                f"WARNING: could not compensate {os.path.basename(raw)}: {exc}"
            )
            continue
        if out is None:
            result.incompatible.append(raw)
        else:
            produced.append(out)

    # Include any pre-existing compensated files that we didn't just make.
    result.generated = sorted(set(produced) | set(existing))

    if result.incompatible:
        names = ", ".join(os.path.basename(p) for p in result.incompatible)
        result.messages.append(
            f"{len(result.incompatible)} file(s) incompatible with the "
            f"matrix (missing channels), skipped: {names}"
        )
    result.messages.append(
        f"{len(result.generated)} compensated file(s) available."
    )
    return result
