"""Shared pytest fixtures for FreeFlow tests.

All fixtures build their inputs from scratch (small synthetic FCS files and
XML settings), so the suite needs no committed binary data and never imports
the matplotlib-backed GUI - keeping CI fast and headless-safe.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import numpy as np
import pytest

from flowcyt.writer import write_fcs


class _Src:
    """Minimal stand-in for FCSData accepted by ``write_fcs``."""

    def __init__(self, channel_names, data, labels=None, extra_meta=None):
        self.channel_names = list(channel_names)
        self.channel_labels = list(labels) if labels else list(channel_names)
        self.data = np.asarray(data, dtype=np.float64)
        meta: dict[str, str] = {}
        for i, (n, lbl) in enumerate(
            zip(self.channel_names, self.channel_labels), start=1
        ):
            meta[f"$P{i}N"] = n
            if lbl and lbl != n:
                meta[f"$P{i}S"] = lbl
        if extra_meta:
            meta.update(extra_meta)
        self.metadata = meta


@pytest.fixture
def make_fcs(tmp_path):
    """Factory: write a synthetic FCS file and return its path.

    ``make_fcs(name, channel_names, data, labels=None, extra_meta=None)``
    """
    def _make(name, channel_names, data, labels=None, extra_meta=None):
        src = _Src(channel_names, data, labels, extra_meta)
        path = str(tmp_path / name)
        write_fcs(src, src.data, path)
        return path
    return _make


@pytest.fixture
def bdfacs_xml(tmp_path):
    """Factory: write a minimal BD FACSDiva settings XML.

    ``bdfacs_xml(name, channels, matrix, scatter=(...), tube_matrix=None)``

    * ``channels`` / ``matrix`` populate the experiment-level (global)
      Cytometer Settings.
    * ``tube_matrix`` (optional) adds a per-tube instrument_settings with a
      *different* matrix, to prove the global one is preferred.
    """
    def _settings(parent, channels, matrix, scatter):
        settings = ET.SubElement(parent, "instrument_settings",
                                 name="Cytometer Settings")
        for sc in scatter:
            p = ET.SubElement(settings, "parameter", name=sc)
            ET.SubElement(p, "can_be_compensated").text = "false"
        for i, ch in enumerate(channels):
            p = ET.SubElement(settings, "parameter", name=ch)
            ET.SubElement(p, "can_be_compensated").text = "true"
            comp = ET.SubElement(p, "compensation")
            for val in matrix[i]:
                ET.SubElement(comp, "compensation_coefficient").text = \
                    repr(float(val))
        return settings

    def _make(name, channels, matrix, scatter=("FSC-A", "SSC-A"),
              tube_matrix=None):
        matrix = np.asarray(matrix, dtype=float)
        root = ET.Element("bdfacs", version="Version 9.2")
        exp = ET.SubElement(root, "experiment", name="UnitTest")
        ET.SubElement(exp, "is_use_global_settings").text = "true"
        _settings(exp, channels, matrix, scatter)
        if tube_matrix is not None:
            specimen = ET.SubElement(exp, "specimen", name="S")
            tube = ET.SubElement(specimen, "tube", name="T1")
            _settings(tube, channels, np.asarray(tube_matrix, float), scatter)
        path = str(tmp_path / name)
        ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
        return path
    return _make


@pytest.fixture
def sony_xml(tmp_path):
    """Factory: write a minimal Sony ID7000-style unmixing-matrix XML."""
    def _make(name, channels, matrix, with_matrix=True):
        matrix = np.asarray(matrix, dtype=float)
        root = ET.Element("ID7000Experiment", vendor="Sony")
        if with_matrix:
            um = ET.SubElement(root, "UnmixingMatrix")
            for i, ch in enumerate(channels):
                row = ET.SubElement(um, "Row", name=ch)
                for val in matrix[i]:
                    ET.SubElement(row, "value").text = repr(float(val))
        else:
            ET.SubElement(root, "notes").text = "spectral unmixing performed"
        path = str(tmp_path / name)
        ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
        return path
    return _make
