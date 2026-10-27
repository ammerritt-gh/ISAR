"""The BL43LXU SARF reader: curve files -> the shared ResolutionLibrary.

Uses the artificial copies of two real SARF files (every number replaced, structure kept) under
`artificial_data/SP8 BL43LXU/`; nothing here reads `real_data/`.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
from pathlib import Path

import numpy as np
import pytest

from isar.analyze.resolution import TabulatedResolution
from isar.parsers import metadata, sarf


def _trapz(y, x):
    """Trapezoid area without np.trapezoid, which NumPy 1.x (allowed by pyproject) lacks."""
    y, x = np.asarray(y, dtype=float), np.asarray(x, dtype=float)
    return float(np.sum((y[1:] + y[:-1]) * np.diff(x)) / 2.0)

ART = Path(__file__).resolve().parents[2] / "artificial_data" / "SP8 BL43LXU"
PATTERN = r"sarf_.+_s\d+_(?P<setting>\d+_\d+)_(?P<analyzer>a\d{2})"
CURVES = ("sarf_24Nov10_s48_60_60_a03", "sarf_24Nov10_s52_20_20_a03")


@pytest.fixture()
def folder(tmp_path):
    out = tmp_path / "rf"
    out.mkdir()
    for name in CURVES:
        for suffix in ("", ".gpi"):
            shutil.copy(ART / (name + suffix), out / (name + suffix))
    return out


def test_library_holds_each_analyzer_at_each_setting(folder):
    lib = sarf.load_library(folder, PATTERN)
    assert len(lib) == 2 and lib.settings() == ["20x20", "60x60"]
    curve = lib.get("a03", "60x60")
    assert curve.detector_id == "a03" and curve.setting == "60x60"
    assert curve.source == "sarf_24Nov10_s48_60_60_a03"
    assert lib.get("a04", "60x60") is None and lib.get("a03", "30x30") is None


def test_curve_grid_is_the_files_and_area_is_one(folder):
    curve = sarf.load_library(folder, PATTERN).get("a03", "60x60")
    table = np.loadtxt(folder / "sarf_24Nov10_s48_60_60_a03", comments="#", usecols=(0, 1))
    assert np.array_equal(curve.energy, table[:, 0])        # the grid is the file's, not resampled
    assert _trapz(curve.values, curve.energy) == pytest.approx(1.0, rel=1e-12)
    assert np.allclose(curve.values * _trapz(table[:, 1], table[:, 0]), table[:, 1])
    assert curve.fwhm > 0 and curve.covers(0.0, 0.0)


def test_gpi_values_ride_as_facts_on_the_curve(folder):
    curve = sarf.load_library(folder, PATTERN).get("a03", "60x60")
    text = (folder / "sarf_24Nov10_s48_60_60_a03.gpi").read_text(encoding="utf-8")
    assert set(curve.facts) == set(sarf.GPI_KEYS)
    for key in sarf.GPI_KEYS:
        want = float(re.search(rf"^\s*{key}\s*=\s*(\S+)", text, re.M).group(1))
        assert curve.facts[key] == {"value": want, "source": "sidecar"}


def test_a_curve_without_gpi_has_no_facts(folder):
    (folder / "sarf_24Nov10_s48_60_60_a03.gpi").unlink()
    assert sarf.load_library(folder, PATTERN).get("a03", "60x60").facts == {}


def test_only_matching_curve_files_are_read(folder):
    # .gpi and .lines are never curves; a .lines file full of garbage cannot matter.
    (folder / "sarf_24Nov10_s48_60_60_a03.lines").write_text("not a table\n", encoding="utf-8")
    (folder / "notes.txt").write_text("hello\n", encoding="utf-8")
    assert len(sarf.load_library(folder, PATTERN)) == 2


def test_unreadable_file_is_logged_and_left_out_never_fatal(folder, caplog):
    (folder / "sarf_24Nov10_s48_60_60_a04").write_text("# header\nnot numbers here\n", encoding="utf-8")
    (folder / "sarf_24Nov10_s48_60_60_a05").write_text("# header\n 1.0 2.0\n 0.5 1.0\n 2.0 1.0\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="isar.parsers.sarf"):
        lib = sarf.load_library(folder, PATTERN)
    assert len(lib) == 2 and lib.get("a04", "60x60") is None and lib.get("a05", "60x60") is None
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "a04" in logged and "a05" in logged


def test_a_second_file_for_one_analyzer_and_setting_is_logged_not_swapped_in(folder, caplog):
    shutil.copy(folder / "sarf_24Nov10_s48_60_60_a03", folder / "sarf_25Nov10_s49_60_60_a03")
    with caplog.at_level(logging.WARNING, logger="isar.parsers.sarf"):
        lib = sarf.load_library(folder, PATTERN)
    assert len(lib) == 2 and lib.get("a03", "60x60").source == "sarf_24Nov10_s48_60_60_a03"
    assert any("sarf_25Nov10_s49_60_60_a03" in r.getMessage() for r in caplog.records)


def test_settings_use_the_one_shared_label_the_slit_fact_uses(tmp_path):
    # The reader writes the slit fact with metadata.slit_label(h, v); a curve file of the same opening,
    # however its digits are spelled, must land on exactly that string.
    out = tmp_path / "rf"
    out.mkdir()
    table = "# t\n-2 0.0\n-1 0.5\n0 1.0\n1 0.5\n2 0.0\n"
    for name in ("sarf_d_s1_0p5_0p5_a06", "sarf_d_s2_60_60_a06", "sarf_d_s3_0.50_0.5_a07"):
        (out / name).write_text(table, encoding="utf-8")
    lib = sarf.load_library(out, r"sarf_.+_s\d+_(?P<setting>[\d.p]+_[\d.p]+)_(?P<analyzer>a\d{2})")
    assert lib.get("a06", metadata.slit_label(0.5, 0.5)) is not None
    assert lib.get("a07", metadata.slit_label(0.5, 0.5)) is not None
    assert lib.get("a06", metadata.slit_label(60.0, 60.0)) is not None
    assert metadata.slit_label(60.0, 60.0) == "60x60"
    assert metadata.normalise_slit("0.50 x 0.5") == metadata.normalise_slit("0p5_0p5") == "0.5x0.5"
    assert metadata.normalise_slit("sixty") is None and metadata.normalise_slit("60x60x60") is None


def test_missing_folder_and_bad_pattern_are_errors_naming_themselves(folder, tmp_path):
    with pytest.raises(FileNotFoundError, match="not found"):
        sarf.load_library(tmp_path / "nope", PATTERN)
    with pytest.raises(ValueError, match="analyzer"):
        sarf.load_library(folder, r"sarf_.+_(?P<setting>\d+_\d+)")


def test_fingerprint_follows_the_files_and_a_stale_expectation_is_refused(folder):
    fp = sarf.fingerprint(folder, PATTERN)
    assert fp == sarf.fingerprint(folder, PATTERN)
    lib = sarf.library_for(folder, PATTERN, fp)
    assert sarf.library_for(folder, PATTERN, fp) is lib                  # cached per process
    path = folder / "sarf_24Nov10_s48_60_60_a03"
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))  # replaced file: new mtime
    fp2 = sarf.fingerprint(folder, PATTERN)
    assert fp2 != fp
    sarf._LIBRARIES.clear()                                              # a fresh process reads the disk
    with pytest.raises(ValueError, match="changed"):
        sarf.library_for(folder, PATTERN, fp)                            # a result made under fp is not rebuilt
    assert sarf.library_for(folder, PATTERN, fp2) is not lib


def test_fingerprint_follows_the_content_at_the_same_size_and_mtime(folder):
    fp = sarf.fingerprint(folder, PATTERN)
    path = folder / "sarf_24Nov10_s48_60_60_a03"
    st = path.stat()
    data = path.read_bytes()
    i = next(k for k in range(200, len(data)) if chr(data[k]).isdigit())   # one digit, changed in place
    path.write_bytes(data[:i] + bytes([48 + (data[i] - 47) % 10]) + data[i + 1:])
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert path.stat().st_size == st.st_size and path.stat().st_mtime_ns == st.st_mtime_ns
    assert sarf.fingerprint(folder, PATTERN) != fp


def test_an_unreadable_curve_file_is_marked_in_the_fingerprint_and_the_rest_still_load(folder, monkeypatch, caplog):
    fp = sarf.fingerprint(folder, PATTERN)
    locked = folder / "sarf_24Nov10_s48_60_60_a03"
    real_read_bytes = Path.read_bytes

    def locked_read_bytes(self):
        if self == locked:
            raise PermissionError(f"[Errno 13] locked: {self.name}")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", locked_read_bytes)
    with caplog.at_level(logging.WARNING, logger="isar.parsers.sarf"):
        locked_fp = sarf.fingerprint(folder, PATTERN)                    # does not raise
    assert locked_fp != fp and "sarf_24Nov10_s48_60_60_a03" in caplog.text
    assert sarf.load_library(folder, PATTERN).get("a03", "20x20") is not None
    monkeypatch.undo()
    assert sarf.fingerprint(folder, PATTERN) == fp                       # readable again: the hash returns


def test_curve_identity_names_the_bytes_each_curve_was_loaded_from(folder):
    lib = sarf.load_library(folder, PATTERN)
    assert [(c.detector_id, c.setting) for c in lib] == [("a03", "20x20"), ("a03", "60x60")]   # sorted
    ident = sarf.curve_identity(folder, lib)
    assert ident[("a03", "60x60")] == {"file": "sarf_24Nov10_s48_60_60_a03",
                                       "sha256": hashlib.sha256((folder / CURVES[0]).read_bytes()).hexdigest()}
    assert set(ident) == {("a03", "20x20"), ("a03", "60x60")}


def test_fingerprint_follows_the_pattern_as_well_as_the_files(folder):
    # A corrected pattern that still matches the same files can capture other groups: a different library.
    corrected = r"sarf_.+_s\d+_(?P<setting>\d+_\d+)_(?P<analyzer>a\d+)"
    assert sarf.fingerprint(folder, PATTERN) != sarf.fingerprint(folder, corrected)


def test_curve_is_rebuilt_from_its_reference_only_while_the_files_are_unchanged(folder):
    fp = sarf.fingerprint(folder, PATTERN)
    ref = {"folder": str(folder), "pattern": PATTERN, "fingerprint": fp, "detector": "a03", "setting": "20x20"}
    curve = sarf.curve_from_reference(ref)
    assert isinstance(curve, TabulatedResolution) and curve.setting == "20x20"
    with pytest.raises(ValueError, match="no resolution curve"):
        sarf.curve_from_reference({**ref, "detector": "a09"})
    (folder / "sarf_24Nov10_s52_20_20_a03").write_text((ART / CURVES[1]).read_text(encoding="utf-8") + "\n",
                                                       encoding="utf-8")
    sarf._LIBRARIES.clear()                                              # a fresh process reads the disk
    with pytest.raises(ValueError, match="changed"):
        sarf.curve_from_reference(ref)
