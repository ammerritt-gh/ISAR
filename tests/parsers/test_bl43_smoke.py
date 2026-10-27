"""Gate B - file smoke for the BL43LXU reader on the committed artificial copies (`artificial_data/SP8 BL43LXU/`).

The copies keep the facility's layout (the CsV3Sb5 per-analyzer files and their `.var`, the Ba122
export) with every data-row number replaced, so what is asserted is structure: detection, one record per
set with the right analyzers, each fact with its source, Q by rung, a set without its `.var`, a member
lacking the column family. Values the reader takes from a data column (energy, monitor, counts) are
asserted against the copy's truth file, i.e. what the copy tool wrote; header and name values (the
`302K`, the `0p5_0p5_3`, `A6`, `4_0p075_0`) are the facility's own and stay literal.
"""
from __future__ import annotations

import logging
import shutil
import tomllib
from argparse import Namespace
from contextlib import nullcontext
from dataclasses import fields
from pathlib import Path

import numpy as np
import pytest

from artificial import (BA122, BL43, BL43_A03, BL43_A04, BL43_STEM, BL43_VAR, SARF_20, SARF_60,
                        scan_truth, tokens, values)
from isar.cli import init_cmd
from isar.parsers import registry, sp8_bl43
from isar.schema import axes, card as card_mod, scan
from isar.schema.card import Card, CardPolicy, validate_policy_text
from isar.session import ingest
from isar.workspace_api import Workspace

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
    assert (rec.scan_type, rec.experiment_id) == ("measurement", BL43_STEM)   # no SPEC file, no card label: the stem
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
        assert sp.facts["points_per_bin"]["source"] == "inferred"      # from the npts data column, not a header
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
    # The copies' #T= numbers are replaced, so the header (first analyzer's) disagrees with the 302K in the
    # name: the header wins and the name's value is kept as a conflict.
    assert f["T_K"] == {"value": float(A03["header_values"]["#T="][0].split()[0]), "source": "header",
                        "conflicts": [{"source": "filename", "value": 302.0}]}
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
        q_nm = float(scan_truth(BL43 / f"{BL43_STEM}_{sp.detector_id}", 0)["header_values"]["#Q="][0].split()[0])
        assert sp.facts["Q_nm"] == {"value": q_nm, "source": "header"}     # its own |Q|, never its q
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
    assert rec.experiment_id == "spec1"                            # the SPEC file name in the file name
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


# ---------------------------------------------------------------------------
# the thin split (C-bl43-format-split): format keys come from the shipped file, the workspace card keeps settings
# ---------------------------------------------------------------------------
SHIPPED = sp8_bl43._CARD_PATH


def _shipped_text() -> str:
    return SHIPPED.read_text(encoding="utf-8")


def _region() -> str:
    """What `isar init` writes into a workspace: the lines between the shipped file's two markers."""
    region = init_cmd._settings_region(_shipped_text())
    assert region is not None
    return region


def _load(tmp_path, text, caplog=None):
    """``(policy, notes)`` of a workspace card written from `text`, loaded as every workspace load does, and
    the same text through the GUI's validator; the two must agree."""
    path = tmp_path / "card.policy.toml"
    path.write_text(text, encoding="utf-8", newline="\n")
    with caplog.at_level(logging.WARNING, logger=card_mod.log.name) if caplog else nullcontext():
        policy = Card.load(path).policy
    checked = validate_policy_text(text)
    assert checked.is_valid and checked.policy == policy
    notes = [r.getMessage() for r in caplog.records if r.name == card_mod.log.name] if caplog else []
    return policy, notes, checked.warnings


def test_the_init_region_loads_with_the_shipped_format(tmp_path, caplog):
    policy, notes, warnings = _load(tmp_path, _region(), caplog)
    shipped = tomllib.loads(_shipped_text())
    assert (policy.energy_status, policy.variance_convention) == ("needs_zero", "none")    # hard rules 3 and 4
    assert policy.files == shipped["files"] and "sarf_*" in policy.files["ignore"]
    assert len(policy.detectors) == 28 and all(d.enabled for d in policy.detectors)
    assert policy.calibration == {"method": "tabulated_elastic", "zero_fallback": "file_axis",
                                  "resolution_pattern": shipped["calibration"]["resolution_pattern"]}
    assert policy.facts == {} and policy.reader == {"intensity_family": "_i"}
    assert policy.collation == {"key": ["sample", "q", "T"], "merge": "separate"}
    assert Card(policy, card_mod.CardState()).validate() == []
    assert notes == [] and warnings == ()


def test_format_keys_in_a_workspace_card_are_ignored_and_named(tmp_path, caplog):
    text = ('energy_status = "absolute"\n' + _region()
            + '\n[facts]\nslit = "60x60"\n\n[calibration]\nresolution_folder = "rf"\n')
    policy, notes, warnings = _load(tmp_path, text, caplog)
    base = _load(tmp_path, _region())[0]
    assert policy == base and policy.energy_status == "needs_zero"
    assert "resolution_folder" not in policy.calibration and policy.facts == {}
    assert len(notes) == len(warnings) == 3
    named = sorted(w.split("'")[1] for w in warnings)
    assert named == ["calibration", "energy_status", "facts"]
    assert all("not used" in w for w in warnings)
    assert any("not read for this instrument" in w or "does not read it" in w for w in warnings if "'facts'" in w)
    _, plain = card_mod._with_shipped_format({"plugin": "sp8_bl43", "miniscans_per_point": 3})   # only facts is entered in ISAR
    assert len(plain) == 1 and "'miniscans_per_point'" in plain[0] and "ISAR" not in plain[0]


def test_a_workspace_setting_wins_over_the_shipped_default(tmp_path, caplog):
    policy, notes, warnings = _load(tmp_path, _region().replace('merge = "separate"', 'merge = "merge"'), caplog)
    assert policy.collation["merge"] == "merge" and notes == [] and warnings == ()


def test_the_full_shipped_file_as_a_workspace_card_is_no_change_and_no_note(tmp_path, caplog):
    policy, notes, warnings = _load(tmp_path, _shipped_text(), caplog)
    assert policy == sp8_bl43.card_template().policy and notes == [] and warnings == ()


def test_another_facilitys_card_is_unchanged(tmp_path):
    data = {"plugin": "tavi", "facts": {"a": 1}, "detectors": []}
    assert card_mod._with_shipped_format(data) == (data, [])
    assert card_mod._with_shipped_format({"plugin": ["unhashable"]}) == ({"plugin": ["unhashable"]}, [])
    tavi = Path(registry.plugin_by_name("tavi")._CARD_PATH)
    assert Card.load(tavi).policy.plugin == "tavi" and Card.load(tavi).validate() == []


def test_discovery_groups_by_the_shipped_files_table_whatever_the_workspace_card_says(tmp_path):
    text = _region() + '\n[files]\nmeasurement = ["nothing_(?P<set>x)_(?P<analyzer>a..)"]\n'
    path = tmp_path / "card.policy.toml"
    path.write_text(text, encoding="utf-8", newline="\n")
    card = Card.load(path)
    plan = ingest.discover([BL43], card)
    assert {fs.key for fs in plan.file_sets} == {BL43_STEM, "spec1_T26K__scan_58"}
    by_key = {fs.key: fs for fs in plan.file_sets}
    assert [p.name for p, _ in by_key[BL43_STEM].measurements] == [f"{BL43_STEM}_a03", f"{BL43_STEM}_a04"]


def test_the_shipped_file_layout_keeps_format_and_settings_apart():
    text = _shipped_text()
    assert text.count(init_cmd.REGION_START) == text.count(init_cmd.REGION_END) == 1
    shipped = tomllib.loads(text)
    region = tomllib.loads(_region())
    assert set(region) & card_mod.FORMAT_KEYS == {"plugin"}
    outside = set(shipped) - set(region)
    assert outside <= card_mod.FORMAT_KEYS and card_mod.FORMAT_KEYS <= {f.name for f in fields(CardPolicy)}
    assert "facts" not in shipped and "resolution_folder" not in shipped["calibration"]
    assert len(shipped["detectors"]) == 28 and all("enabled" not in d for d in shipped["detectors"])
    assert card_mod.SHIPPED_FORMATS["sp8_bl43"] == SHIPPED == sp8_bl43._CARD_PATH


def test_init_writes_only_the_analysis_settings_and_the_workspace_opens(tmp_path, capsys):
    ws_dir = tmp_path / "ws"
    assert init_cmd.run(Namespace(data=str(BL43), out=str(ws_dir), json=False)) == 0
    assert "analysis settings" in capsys.readouterr().out
    written = (ws_dir / "card.policy.toml").read_text(encoding="utf-8")
    assert written.replace("\r\n", "\n") == _region()
    assert not {"files", "reader", "calibration", "detectors", "facts"} & set(tomllib.loads(written))
    assert (ws_dir / "session.json").exists() and (ws_dir / "card.state.json").exists()
    card = Card.load(ws_dir / "card.policy.toml", ws_dir / "card.state.json")
    assert card.validate() == [] and len(card.policy.detectors) == 28
    view = Workspace.open(ws_dir)
    assert view.card.policy.energy_status == "needs_zero" and view.warnings == []


def test_init_refuses_a_shipped_file_whose_markers_are_wrong(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "bl43.toml"
    bad.write_text(_shipped_text().replace(init_cmd.REGION_END, "# end"), encoding="utf-8", newline="\n")
    monkeypatch.setattr(sp8_bl43, "_CARD_PATH", bad)
    assert init_cmd.run(Namespace(data=str(BL43), out=str(tmp_path / "ws"), json=False)) == 2
    assert "exactly once" in capsys.readouterr().err and not (tmp_path / "ws").exists()


def test_a_disabled_analyzer_is_still_read():
    card = sp8_bl43.card_template()
    card.policy.detectors[0].enabled = False                      # a03: now a condition entered in ISAR, not a card switch
    plan = ingest.discover([BL43], card)
    rec = sp8_bl43.parse_set(next(fs for fs in plan.file_sets if fs.key == BL43_STEM), card)[0]
    assert sorted(rec.spectra) == ["a03", "a04"]
