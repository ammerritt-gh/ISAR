"""Gate B - file smoke for the BL43LXU reader on the committed artificial copies (`artificial_data/SP8 BL43LXU/`).

The copies keep the facility's layout (the CsV3Sb5 per-analyzer files and their `.var`, the Ba122
export) with every data-row number replaced, so what is asserted is structure: detection, one record per
set with the right analyzers, each fact with its source, Q by rung, a set without its `.var`, a member
lacking the column family. Values the reader takes from a data column (energy, monitor, counts) are
asserted against the copy's truth file, i.e. what the copy tool wrote; header and name values (the
`302K`, the `0p5_0p5_3`, `A6`, `4_0p075_0`) are the facility's own and stay literal.
"""
from __future__ import annotations

import shutil

import numpy as np
import pytest

from artificial import (BA122, BL43, BL43_A03, BL43_A04, BL43_STEM, BL43_VAR, SARF_20, SARF_60,
                        scan_truth, tokens, values)
from isar.parsers import registry, sp8_bl43
from isar.schema import axes, scan
from isar.session import ingest

A03, A04 = scan_truth(BL43_A03, 0), scan_truth(BL43_A04, 0)
BA = scan_truth(BA122, 0)
DETECTED = ("SP8", "BL43LXU", "processed")


def parse_sets(folder):
    card = sp8_bl43.card_template()
    plan = ingest.discover([folder], card)
    return {fs.key: sp8_bl43.parse_set(fs, card)[0] for fs in plan.file_sets}, plan


@pytest.fixture(scope="module")
def sets():
    return parse_sets(BL43)


def test_both_layouts_are_detected_and_routed_to_the_plugin():
    assert registry.detect(BL43_A03) == registry.detect(BL43_A04) == registry.detect(BA122) == DETECTED
    assert registry.default_plugin_name(*DETECTED) == "sp8_bl43"
    assert registry.plugin_by_name("sp8_bl43") is sp8_bl43 and registry.plugin_for(BL43_A03) is sp8_bl43
    assert sp8_bl43.matches(BL43_A03) and sp8_bl43.matches(BA122)
    for other in (BL43_VAR, SARF_60, SARF_20):                    # sidecar and resolution files: not scans
        with pytest.raises(ValueError):
            registry.detect(other)


def test_card_template_is_valid_and_pins_the_plugin():
    card = sp8_bl43.card_template()
    assert card.validate() == []
    pol = card.policy
    assert (pol.facility, pol.instrument, pol.plugin, pol.energy_status) == (
        "SP8", "BL43LXU", "sp8_bl43", "needs_zero")
    assert pol.reader == {"intensity_family": "_i"}
    assert len(pol.detectors) == 28 and pol.detectors[0].detector_id == "a03"
    assert pol.calibration["method"] == "tabulated_elastic"        # written now, consumed by slice `resolution`


def test_ingest_groups_each_layout_into_one_set_and_attaches_the_sidecar(sets):
    _, plan = sets
    by_key = {fs.key: fs for fs in plan.file_sets}
    assert set(by_key) == {BL43_STEM, "spec1_T26K__scan_58"}
    assert [p.name for p, _ in by_key[BL43_STEM].measurements] == [f"{BL43_STEM}_a03", f"{BL43_STEM}_a04"]
    assert [p.name for p in by_key[BL43_STEM].sidecars] == [f"{BL43_STEM}.var"]
    assert by_key["spec1_T26K__scan_58"].sidecars == []
    assert {p.name for p in plan.sidecar_files} == {f"{BL43_STEM}.var"}


def test_csv3sb5_set_is_one_record_with_a_spectrum_per_analyzer(sets):
    rec = sets[0][BL43_STEM]
    assert (rec.scan_type, rec.experiment_id) == ("measurement", "bl43lxu")
    assert sorted(rec.spectra) == ["a03", "a04"]
    assert rec.scan_number >= 1_000_000_000                       # hashed: the layout has no scan number
    assert rec.source.plugin == "sp8_bl43" and rec.source.file == BL43_STEM
    # Union of the two analyzers' bins (the copy's own energy tokens), each nothing resampled.
    ea, eb = values(A03, "meV"), values(A04, "meV")
    union = np.unique(np.round(np.array(ea + eb) / 0.2).astype(int))
    grid = axes.scanned_axes(rec)[0].values
    assert np.allclose(grid, union * 0.2, atol=1e-6) and len(ea) == 182 and len(eb) == 183
    for det, e in (("a03", ea), ("a04", eb)):
        sp = rec.spectra[det]
        present = np.isin(np.round(grid / 0.2).astype(int), np.round(np.array(e) / 0.2).astype(int))
        assert present.sum() == len(e)
        assert sorted(p.index for p in sp.point_flags if p.type == "bin_absent") == np.nonzero(~present)[0].tolist()
        assert np.array_equal(sp.mask[~present], np.zeros((~present).sum(), bool))
        assert np.all(sp.monitor[~present] == 0) and np.all(sp.time[~present] == 0)
        # monitor, time and counts at the present bins are the copy's tokens, in axis order
        order = np.argsort(e)
        assert np.allclose(sp.monitor[present], np.array(values(scan_truth(BL43 / f"{BL43_STEM}_{det}", 0), "norm_i"))[order])
        assert np.allclose(sp.time[present], np.array(values(scan_truth(BL43 / f"{BL43_STEM}_{det}", 0), "secs"))[order])
        t = scan_truth(BL43 / f"{BL43_STEM}_{det}", 0)
        n = (np.array(values(t, "y_i")) / np.array(values(t, "ye_i"))) ** 2
        assert np.array_equal(sp.intensity[present], np.round(n)[order])      # raw counts, rounded for `_i`
        assert np.array_equal(sp.variance, sp.intensity)                       # sigma^2 = N, no floor
        assert sp.facts["points_per_bin"]["value"] == [0 if not p else int(round(v)) for p, v in zip(
            present, _scatter(present, np.array(values(t, "npts"))[order]))]
    assert np.isnan(rec.normalization.monitor).all() and np.isnan(rec.normalization.time).all()
    assert rec.energy_calibration.status == "needs_zero" and rec.energy_calibration.elastic_zero_offset is None


def _scatter(present, vals):
    out = np.zeros(len(present))
    out[present] = vals
    return out


def test_csv3sb5_facts_carry_their_sources_and_the_nominal_hkl_is_no_spectrums_q(sets):
    rec = sets[0][BL43_STEM]
    f = rec.facts
    assert f["T_K"] == {"value": 302.0, "source": "filename"}
    assert f["hkl_nominal"] == {"value": [0.5, 0.5, 3.0], "source": "filename"}
    assert f["scan_label"] == {"value": BL43_STEM, "source": "filename"}
    assert f["si_order"] == {"value": 12, "source": "sidecar"}
    assert f["bin_meV"] == {"value": 0.2, "source": "sidecar"}
    assert f["n_merged_scans"] == {"value": 2, "source": "sidecar"}
    assert f["energy_keV"] == {"value": pytest.approx(12 * 1.977), "source": "inferred"}
    for missing in ("scan_numbers", "slit", "lattice", "time"):
        assert f[missing] == {"value": None, "source": "missing"}
    for sp in rec.spectra.values():
        assert (sp.q, sp.q_source) == (None, "none") and scan.Q_SOURCE_RUNG[sp.q_source] == 4
        assert "hkl_nominal" not in sp.facts and sp.facts["hkl"]["source"] == "missing"
        assert sp.facts["efficiency"]["source"] == sp.facts["elastic_area"]["source"] == "sidecar"
    # the .var lists all 28 analyzers; only two have files, and the flag says which are gone
    flag = [x for x in rec.scan_flags if x.type == "missing_analyzer"]
    assert len(flag) == 1 and flag[0].severity == "warning"
    assert "a05: no file" in flag[0].reason and "a41: no file" in flag[0].reason
    assert "a03:" not in flag[0].reason and "a04:" not in flag[0].reason
    assert not [x for x in rec.scan_flags if x.type == "missing_sidecar"]


def test_ba122_export_is_rung_one_with_missing_exposure(sets):
    rec = sets[0]["spec1_T26K__scan_58"]
    assert sorted(rec.spectra) == ["a06"]                          # `A6` in the name
    sp = rec.spectra["a06"]
    assert (sp.q, sp.q_source) == ([4.0, 0.075, 0.0], "filename")  # `Q_4_0p075_0` in the name
    assert scan.Q_SOURCE_RUNG[sp.q_source] == 1
    assert rec.scan_number == 58 and rec.facts["scan_numbers"] == {"value": [58], "source": "filename"}
    assert rec.facts["T_K"] == {"value": 26.0, "source": "filename"}
    assert "hkl_nominal" not in rec.facts
    assert np.isnan(sp.monitor).all() and np.isnan(sp.time).all() and len(sp.monitor) == BA["points"]
    assert sp.facts["monitor"] == {"value": None, "source": "missing"}
    assert sp.facts["time"] == {"value": None, "source": "missing"}
    assert np.isnan(scan.exposure_for(rec, "a06").monitor).all()
    # No sidecar and no expected-analyzer set in this layout, so nothing to report as missing.
    assert not [x for x in rec.scan_flags if x.type in ("missing_analyzer", "missing_sidecar")]
    e = np.array(values(BA, "col0"))                               # irregular per-analyzer energies, as written
    assert np.allclose(axes.scanned_axes(rec)[0].values, np.sort(e))
    n = (np.array(values(BA, "col1")) / np.array(values(BA, "col2"))) ** 2
    assert np.array_equal(sp.intensity, np.round(n)[np.argsort(e)])


def test_a_set_without_its_var_still_parses_and_says_so(tmp_path):
    shutil.copy(BL43_A03, tmp_path)
    rec = parse_sets(tmp_path)[0][BL43_STEM]
    assert sorted(rec.spectra) == ["a03"]
    flag = [x for x in rec.scan_flags if x.type == "missing_sidecar"]
    assert len(flag) == 1 and BL43_STEM in flag[0].reason
    assert rec.facts["bin_meV"] == {"value": None, "source": "missing"}
    assert rec.facts["energy_keV"]["source"] == "missing"          # no silicon order to derive it from
    assert rec.spectra["a03"].facts["efficiency"] == {"value": None, "source": "missing"}
    assert not [x for x in rec.scan_flags if x.type == "missing_analyzer"]   # no expected set without a .var
    assert len(axes.scanned_axes(rec)[0].values) == A03["points"]   # own median spacing: the lattice holds


def test_a_member_lacking_the_column_family_is_left_out_and_named(tmp_path):
    shutil.copy(BL43_A03, tmp_path)
    shutil.copy(BL43_VAR, tmp_path)
    bad = tmp_path / BL43_A04.name
    text = BL43_A04.read_text(encoding="utf-8").replace("ye_i", "xx_i", 1)
    bad.write_text(text, encoding="utf-8", newline="\n")
    rec = parse_sets(tmp_path)[0][BL43_STEM]
    assert sorted(rec.spectra) == ["a03"]
    reason = [x for x in rec.scan_flags if x.type == "missing_analyzer"][0].reason
    assert "a04: lacks the _i column family (no ye_i column)" in reason
    # ...while the same file is a full member of the `_lr` family, which the card can select
    card = sp8_bl43.card_template()
    card.policy.reader = {"intensity_family": "_lr"}
    plan = ingest.discover([BL43], card)
    lr = sp8_bl43.parse_set(next(fs for fs in plan.file_sets if fs.key == BL43_STEM), card)[0]
    assert sorted(lr.spectra) == ["a03", "a04"]
    assert any(f.type == "counts_not_integer" and f.severity == "warning" for f in lr.spectra["a03"].spectrum_flags)
    assert not np.array_equal(lr.spectra["a03"].intensity, np.round(lr.spectra["a03"].intensity))


def test_the_record_survives_the_canonical_json_store(sets):
    for rec in sets[0].values():
        back = scan.ScanRecord.from_dict(rec.to_dict())
        assert scan.records_close(rec, back)
        assert back.spectra[sorted(back.spectra)[0]].facts == rec.spectra[sorted(rec.spectra)[0]].facts
