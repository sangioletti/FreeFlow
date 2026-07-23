"""Tests for compensation.py: format detection, parsing, application, flow."""

from __future__ import annotations

import os

import numpy as np
import pytest

from flowcyt import compensation as C
from flowcyt.reader import FCSData


# A small, well-conditioned 2-channel compensation matrix reused across tests.
CH = ["FITC-A", "PE-A"]
MATRIX = [[1.02, -0.10], [-0.05, 1.01]]


# ----------------------------- detection ------------------------------ #
def test_detect_bdfacs(bdfacs_xml):
    path = bdfacs_xml("bd.xml", CH, MATRIX)
    assert C.detect_xml_format(path) == C.FORMAT_BDFACS


def test_detect_sony(sony_xml):
    path = sony_xml("sony.xml", CH, MATRIX)
    assert C.detect_xml_format(path) == C.FORMAT_SONY_ID7000


def test_detect_unknown(tmp_path):
    p = tmp_path / "u.xml"
    p.write_text('<?xml version="1.0"?><whatever><a>1</a></whatever>')
    assert C.detect_xml_format(str(p)) == C.FORMAT_UNKNOWN


# ------------------------------ parsing ------------------------------- #
def test_parse_bdfacs_channels_and_matrix(bdfacs_xml):
    comp = C.parse_compensation(bdfacs_xml("bd.xml", CH, MATRIX))
    assert comp.channels == CH
    assert np.allclose(comp.matrix, MATRIX)


def test_bdfacs_prefers_experiment_level_over_tube(bdfacs_xml):
    """The global (experiment-level) matrix wins over a per-tube one."""
    tube = [[9.0, 9.0], [9.0, 9.0]]
    comp = C.parse_compensation(
        bdfacs_xml("bd.xml", CH, MATRIX, tube_matrix=tube))
    assert np.allclose(comp.matrix, MATRIX)  # not the tube's 9s


def test_parse_sony_generic_matrix(sony_xml):
    comp = C.parse_compensation(sony_xml("s.xml", CH, MATRIX))
    assert comp.channels == CH
    assert np.allclose(comp.matrix, MATRIX)


def test_parse_sony_without_matrix_raises(sony_xml):
    path = sony_xml("s.xml", CH, MATRIX, with_matrix=False)
    with pytest.raises(C.CompensationError, match="Sony ID7000"):
        C.parse_compensation(path)


def test_parse_unknown_format_raises(tmp_path):
    p = tmp_path / "u.xml"
    p.write_text('<?xml version="1.0"?><nope/>')
    with pytest.raises(C.CompensationError, match="Unrecognised"):
        C.parse_compensation(str(p))


# --------------------------- application ------------------------------ #
def test_apply_compensation_orientation(make_fcs):
    """out = v @ M.T recovers true signal from spilled observations."""
    comp = C.CompMatrix(CH, np.array(MATRIX, float), "x")
    rng = np.random.default_rng(1)
    true = rng.uniform(0, 1000, size=(2000, 2))
    spill = np.linalg.inv(comp.matrix)
    observed = true @ spill.T
    # Full data has extra scatter columns the matrix must leave untouched.
    data = np.column_stack([np.arange(2000.0), observed])  # FSC-A, FITC, PE
    path = make_fcs("o.fcs", ["FSC-A", "FITC-A", "PE-A"], data)
    fcs = FCSData(path)
    out = C.apply_compensation(fcs, comp)
    assert np.allclose(out[:, 1:], true, atol=1e-3)
    assert np.allclose(out[:, 0], data[:, 0])  # scatter unchanged


def test_is_compatible(make_fcs):
    comp = C.CompMatrix(CH, np.array(MATRIX, float), "x")
    ok = FCSData(make_fcs("a.fcs", ["FSC-A", "FITC-A", "PE-A"], np.ones((2, 3))))
    bad = FCSData(make_fcs("b.fcs", ["FSC-A", "FITC-A", "APC-A"], np.ones((2, 3))))
    assert C.is_compatible(ok, comp) is True
    assert C.is_compatible(bad, comp) is False


# ------------------------- path helpers ------------------------------- #
def test_compensated_path():
    assert C.compensated_path("/d/foo.fcs") == "/d/foo_compensated.fcs"
    assert C.compensated_path("/d/foo.FCS") == "/d/foo_compensated.fcs"


def test_is_compensated_file():
    assert C.is_compensated_file("foo_compensated.fcs") is True
    assert C.is_compensated_file("FOO_COMPENSATED.FCS") is True
    assert C.is_compensated_file("foo.fcs") is False


# ------------------------- generate twin ------------------------------ #
def test_generate_compensated_writes_twin(make_fcs, bdfacs_xml):
    comp = C.parse_compensation(bdfacs_xml("bd.xml", CH, MATRIX))
    raw = make_fcs("t.fcs", ["FSC-A", "FITC-A", "PE-A"],
                   np.array([[1.0, 100.0, 200.0], [2.0, 300.0, 400.0]]))
    out = C.generate_compensated(raw, comp)
    assert out.endswith("_compensated.fcs")
    assert os.path.exists(out)
    twin = FCSData(out)
    assert twin.is_compensated is True
    assert twin.metadata.get("FREEFLOW_COMPENSATED") == "1"
    # Fluor columns changed, scatter column preserved.
    raw_fcs = FCSData(raw)
    assert not np.allclose(twin.data[:, 1], raw_fcs.data[:, 1])
    assert np.allclose(twin.data[:, 0], raw_fcs.data[:, 0], rtol=1e-4)


def test_generate_incompatible_returns_none(make_fcs, bdfacs_xml):
    comp = C.parse_compensation(bdfacs_xml("bd.xml", CH, MATRIX))
    raw = make_fcs("t.fcs", ["FSC-A", "APC-A"], np.ones((2, 2)))
    assert C.generate_compensated(raw, comp) is None


def test_generate_idempotent(make_fcs, bdfacs_xml):
    comp = C.parse_compensation(bdfacs_xml("bd.xml", CH, MATRIX))
    raw = make_fcs("t.fcs", ["FITC-A", "PE-A"], np.ones((2, 2)))
    first = C.generate_compensated(raw, comp)
    mtime = os.path.getmtime(first)
    second = C.generate_compensated(raw, comp)
    assert first == second
    assert os.path.getmtime(second) == mtime  # not rewritten


# ------------------------ prepare_directory --------------------------- #
def test_prepare_directory_generates(tmp_path, make_fcs, bdfacs_xml):
    bdfacs_xml("settings.xml", CH, MATRIX)
    make_fcs("tubeA.fcs", ["FITC-A", "PE-A"], np.ones((3, 2)))
    make_fcs("tubeB.fcs", ["FITC-A", "PE-A"], np.ones((3, 2)))
    res = C.prepare_directory(str(tmp_path))
    assert res.comp is not None
    names = {os.path.basename(p) for p in res.generated}
    assert names == {"tubeA_compensated.fcs", "tubeB_compensated.fcs"}
    assert res.has_usable_files is True


def test_prepare_directory_reports_incompatible(tmp_path, make_fcs, bdfacs_xml):
    bdfacs_xml("settings.xml", CH, MATRIX)
    make_fcs("weird.fcs", ["FSC-A", "APC-A"], np.ones((3, 2)))
    res = C.prepare_directory(str(tmp_path))
    assert res.has_usable_files is False
    assert len(res.incompatible) == 1


def test_prepare_directory_no_xml_uses_existing_twin(tmp_path, make_fcs):
    make_fcs("y_compensated.fcs", ["FITC-A", "PE-A"], np.ones((2, 2)))
    res = C.prepare_directory(str(tmp_path))
    assert res.xml_path is None
    assert res.has_usable_files is True


def test_prepare_directory_empty_is_unusable(tmp_path):
    res = C.prepare_directory(str(tmp_path))
    assert res.has_usable_files is False
    assert res.xml_path is None
