"""Tests for the FCS writer (writer.py) and reader (reader.py)."""

from __future__ import annotations

import numpy as np

from flowcyt.reader import FCSData


def test_write_read_roundtrip(make_fcs):
    """Data, channel names/labels and shape survive a write/read cycle."""
    rng = np.random.default_rng(0)
    data = rng.uniform(-1000, 50000, size=(500, 4))
    path = make_fcs("rt.fcs", ["FSC-A", "SSC-A", "FITC-A", "PE-A"], data,
                    labels=["FSC-A", "SSC-A", "CD4", "CD8"])

    fcs = FCSData(path)
    assert fcs.num_events == 500
    assert fcs.num_channels == 4
    assert fcs.channel_names == ["FSC-A", "SSC-A", "FITC-A", "PE-A"]
    assert fcs.channel_labels == ["FSC-A", "SSC-A", "CD4", "CD8"]
    # float32 storage -> compare with a tolerance scaled to magnitude.
    assert np.allclose(fcs.data, data, rtol=1e-4, atol=1e-2)


def test_writer_is_float32_fcs31(make_fcs):
    path = make_fcs("t.fcs", ["A", "B"], np.zeros((3, 2)))
    with open(path, "rb") as fh:
        header = fh.read(6)
    assert header == b"FCS3.1"
    fcs = FCSData(path)
    assert fcs.metadata["$DATATYPE"] == "F"
    assert fcs.metadata["$BYTEORD"] == "1,2,3,4"
    assert fcs.metadata["$P1B"] == "32"


def test_writer_extra_keywords_and_spill_dropped(tmp_path):
    """Spillover keywords are never written; extra keywords are preserved."""
    from flowcyt.writer import write_fcs

    class Src:
        channel_names = ["A", "B"]
        channel_labels = ["A", "B"]
        data = np.ones((2, 2))
        metadata = {"$P1N": "A", "$P2N": "B",
                    "$SPILLOVER": "2,A,B,1,0,0,1", "SPILL": "junk"}

    out = str(tmp_path / "x.fcs")
    write_fcs(Src(), Src().data, out,
              extra_keywords={"FREEFLOW_COMPENSATED": "1"})
    fcs = FCSData(out)
    assert fcs.metadata.get("$SPILLOVER") is None
    assert fcs.metadata.get("SPILL") is None
    assert fcs.metadata.get("FREEFLOW_COMPENSATED") == "1"


def test_is_compensated_is_name_based(make_fcs):
    """is_compensated depends only on the filename suffix."""
    raw = make_fcs("sample.fcs", ["A", "B"], np.ones((2, 2)))
    comp = make_fcs("sample_compensated.fcs", ["A", "B"], np.ones((2, 2)))
    assert FCSData(raw).is_compensated is False
    assert FCSData(comp).is_compensated is True


def test_reader_ignores_embedded_spillover(make_fcs):
    """A SPILL keyword in the file must NOT change the data or flags."""
    data = np.array([[10.0, 20.0], [30.0, 40.0]])
    path = make_fcs("s.fcs", ["A", "B"], data,
                    extra_meta={"SPILL": "2,A,B,1,0.5,0.5,1"})
    fcs = FCSData(path)
    assert fcs.spillover_matrix is None
    assert fcs.spillover_channels == []
    # Data is returned exactly as written - no compensation applied.
    assert np.allclose(fcs.data, data, rtol=1e-4, atol=1e-2)
