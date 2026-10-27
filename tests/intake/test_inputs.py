"""Effective inputs (C-effective, C-entries): what is in force, the effective copy, and what changed."""
import numpy as np
import pytest

from intake_fixtures import bl43_workspace, records_for, rows_for
from isar.intake import inputs, scans, store, values
from isar.intake.inputs import (Change, Condition, apply, calibration_inputs_key, changes, damage_lines, fallbacks,
                                index_entries, inputs_hash, read_entries, scan_inputs, unit_inputs)
from isar.session.state import content_hash

ROW = {"scan": "spec1:42", "data_revision": "aa",
       "facts": {"T_K": {"value": 97.0, "source": "trailer"}, "slit": {"value": None, "source": "missing"}},
       "analyzers": {"a03": {"q": [0.5, 0.5, 3.0], "q_source": "filename", "counts": None},
                     "a05": {"q": None, "q_source": "none",
                             "counts": {"origin": "y_over_e_squared", "family": "_i", "bins_tested": 120,
                                        "bins_off": 40, "max_deviation": 0.31, "level": "warning"}}}}
CAL = {"folder": "D:/beam/rf", "pattern": "p", "library": "f1", "setting": "40x40", "setting_basis": "inferred",
       "inputs_key": "77aa", "detectors": {"a03": {"zero": 0.148, "zero_source": "elastic_fit",
                                                   "curve": {"file": "sarf_t_s2_40_40_a03", "sha256": "c0"}}}}


@pytest.fixture
def root(tmp_path):
    scans.record_import(tmp_path, [ROW, {**ROW, "scan": "spec1:43"}])
    return tmp_path


def _si(root, row=ROW):
    return scan_inputs(row, index_entries(read_entries(root)))


def _t(root):
    return _si(root).facts["temperature"]


def test_entries_in_force_follow_seq_and_name_only_their_targets(root):
    """C-entries' table: scan spec1:42, file T 97 K."""
    values.save_value(root, "temperature", 100.0, ["spec1:42", "spec1:43"], reason="thermometer offset")
    t = _t(root)
    assert (t.user_value, t.used_value, t.used_source, t.user_reason, t.disagreement) == (
        100.0, 100.0, "user", "thermometer offset", True)
    values.save_value(root, "temperature", 100.0, ["spec1:42"])
    assert _t(root).user_entry == read_entries(root)[-1]["id"] and _t(root).user_reason is None
    values.use_file_value(root, "temperature", ["spec1:42"], reason="file was right")
    t = _t(root)
    assert (t.user_value, t.used_value, t.used_source, t.disagreement) == (None, 97.0, "file", False)
    assert _si(root, {**ROW, "scan": "spec1:43"}).facts["temperature"].used_value == 100.0   # untouched
    values.save_condition(root, "dead", ["spec1:42:a14"], reason="no counts")
    dead = _si(root).dead
    assert list(dead) == ["a14"] and dead["a14"].reason == "no counts" and dead["a14"].at
    values.save_condition(root, "dead", ["spec1:42:a14"], declared=False)
    assert _si(root).dead == {} and len(read_entries(root)) == 5                    # both entries stay
    values.save_condition(root, "counts_accepted", ["spec1:42:a05"], reason="checked with the beamline")
    acc = _si(root).counts_accepted["a05"]
    assert acc == Condition(read_entries(root)[-1]["id"], "checked with the beamline", read_entries(root)[-1]["at"])
    values.save_condition(root, "excluded", ["spec1:42"], reason="sample moved")
    assert _si(root).excluded.reason == "sample moved"
    assert _si(root, {**ROW, "scan": "spec1:43"}).excluded is None


def test_missing_file_temperature_with_a_user_value_is_no_disagreement(root):
    row = {**ROW, "facts": {"T_K": {"value": None, "source": "missing"}}}
    t = _si(root, row).facts["temperature"]
    assert (t.file_value, t.file_source, t.used_value, t.used_source) == (None, "missing", None, "missing")
    values.save_value(root, "temperature", 120.0, ["spec1:42"])
    t = _si(root, row).facts["temperature"]
    assert (t.used_value, t.used_source, t.disagreement) == (120.0, "user", False)


# --- apply: the effective record is a copy --------------------------------------------------------
@pytest.fixture
def parsed(tmp_path):
    ws, data = bl43_workspace(tmp_path, stems=("A",), analyzers=("a03", "a05", "a14"))
    (rec,) = records_for(ws, data)
    (row,) = rows_for(ws, data)
    scans.record_import(ws.root, [row])
    return ws.root, rec, row


def test_apply_with_nothing_in_force_is_the_record_itself(parsed):
    root, rec, row = parsed
    assert apply(rec, scan_inputs(row, index_entries(read_entries(root)))) is rec


def test_apply_user_temperature_copies_and_never_mutates(parsed):
    root, rec, row = parsed
    before = content_hash(rec)
    values.save_value(root, "temperature", 120.0, [row["scan"]], reason="offset")
    si = scan_inputs(row, index_entries(read_entries(root)))
    eff = apply(rec, si)
    assert eff is not rec and np.all(eff.temperature.setpoint == 120.0)
    assert eff.facts["T_K"] == {"value": 120.0, "source": "user", "entry": si.facts["temperature"].user_entry,
                                "file": {"value": 100.0, "source": "filename"}}
    assert rec.facts["T_K"]["value"] == 100.0 and np.all(rec.temperature.setpoint == 100.0)
    assert content_hash(rec) == before                       # file values never changed, in memory too
    values.use_file_value(root, "temperature", [row["scan"]])
    assert apply(rec, scan_inputs(row, index_entries(read_entries(root)))) is rec


def test_apply_drops_dead_analyzers_and_names_accepted_counts(parsed):
    root, rec, row = parsed
    values.save_condition(root, "dead", [row["scan"] + ":a14"])
    values.save_condition(root, "counts_accepted", [row["scan"] + ":a05", row["scan"] + ":a14"], reason="checked")
    si = scan_inputs(row, index_entries(read_entries(root)))
    eff = apply(rec, si)
    assert sorted(eff.spectra) == ["a03", "a05"] and sorted(rec.spectra) == ["a03", "a05", "a14"]
    assert eff.facts["counts_accepted"] == {"value": ["a05"], "source": "user",
                                            "entries": {"a05": si.counts_accepted["a05"].entry}}
    assert eff.spectra["a05"] is rec.spectra["a05"] and "counts_accepted" not in rec.facts


# --- unit_inputs, fallbacks, hashes ----------------------------------------------------------------
def test_unit_inputs_record_values_sources_conditions_and_calibration(root):
    values.save_condition(root, "dead", ["spec1:42:a14"], reason="no counts")
    u = unit_inputs(ROW, _si(root), "a03", CAL)
    assert u["data"] == "aa"
    assert u["temperature"] == {"value": 97.0, "source": "file", "entry": None, "file_value": 97.0,
                                "file_source": "trailer"}
    assert u["slit"] == {"value": None, "source": "missing", "entry": None, "file_value": None, "file_source": "missing"}
    assert u["q"] == {"value": [0.5, 0.5, 3.0], "source": "filename"}
    assert u["excluded"] == {"value": False, "entry": None, "reason": None}
    assert u["dead"] == {"value": False, "entry": None, "reason": None} and u["dead_in_scan"] == ["a14"]
    assert u["counts"] is None
    assert u["calibration"] == {"folder": "D:/beam/rf", "pattern": "p", "library": "f1", "setting": "40x40",
                                "basis": "inferred", "zero": 0.148, "zero_source": "elastic_fit",
                                "curve": {"file": "sarf_t_s2_40_40_a03", "sha256": "c0"}, "held_reason": None,
                                "key": "77aa"}
    assert fallbacks(u) == ("slit_inferred",)
    assert unit_inputs(ROW, _si(root), "a05", None)["calibration"] is None
    assert unit_inputs(ROW, _si(root), "a05", CAL)["calibration"]["held_reason"] == \
        "a05: no tabulated calibration for this analyzer"


def test_missing_temperature_is_none_never_zero_and_non_finite_is_none(root):
    row = {**ROW, "facts": {"T_K": {"value": None, "source": "missing"}}}
    cal = {**CAL, "detectors": {"a03": {"zero": float("nan"), "zero_source": "pipeline_zero"}}}
    u = unit_inputs(row, _si(root, row), "a03", cal)
    assert u["temperature"]["value"] is None and u["calibration"]["zero"] is None
    assert fallbacks(u) == ("bose_t0_limit", "slit_inferred", "zero_file_axis")


def test_counts_accepted_applies_the_override_only_to_a_warning_i_spectrum(root):
    assert unit_inputs(ROW, _si(root), "a05", None)["counts"]["override_applied"] is False
    values.save_condition(root, "counts_accepted", ["spec1:42:a05"], reason="checked")
    u = unit_inputs(ROW, _si(root), "a05", None)
    assert u["counts"]["accepted"] == {"value": True, "entry": read_entries(root)[-1]["id"], "reason": "checked"}
    assert u["counts"]["override_applied"] is True and fallbacks(u) == ("counts_accepted",)
    lr = {**ROW, "analyzers": {"a05": {**ROW["analyzers"]["a05"],
                                       "counts": {**ROW["analyzers"]["a05"]["counts"], "family": "_lr"}}}}
    assert unit_inputs(lr, _si(root, lr), "a05", None)["counts"]["override_applied"] is False   # _lr stays held


def test_inputs_hash_follows_values_not_sources_or_reasons(root):
    h0 = inputs_hash(unit_inputs(ROW, _si(root), "a03", CAL))
    values.save_value(root, "temperature", 97.0, ["spec1:42"], reason="same as the file")
    assert inputs_hash(unit_inputs(ROW, _si(root), "a03", CAL)) == h0          # 97 K over 97 K marks nothing
    values.save_value(root, "temperature", 100.0, ["spec1:42"])
    assert inputs_hash(unit_inputs(ROW, _si(root), "a03", CAL)) != h0
    declared = {**CAL, "setting_basis": "declared"}
    hd = inputs_hash(unit_inputs(ROW, _si(root), "a03", declared))
    assert inputs_hash(unit_inputs(ROW, _si(root), "a03", {**declared, "library": "f2", "inputs_key": "x"})) == hd
    hi = inputs_hash(unit_inputs(ROW, _si(root), "a03", CAL))
    assert inputs_hash(unit_inputs(ROW, _si(root), "a03", {**CAL, "library": "f2"})) != hi   # inferred: hashed


def test_calibration_inputs_key_is_values_not_entries():
    k = dict(data_revision="aa", folder="D:/rf", pattern="p", library="f1", declared_slit="60x60",
             analyzers=["a05", "a03"], counts_accepted=[])
    assert calibration_inputs_key(**k) == calibration_inputs_key(**{**k, "analyzers": ["a03", "a05"]})
    assert calibration_inputs_key(**k) != calibration_inputs_key(**{**k, "analyzers": ["a03"]})
    assert calibration_inputs_key(**k) != calibration_inputs_key(**{**k, "counts_accepted": ["a05"]})


# --- changes ---------------------------------------------------------------------------------------
def _unit(**over):
    base = {"data": "aa", "temperature": {"value": 97.0, "source": "file"}, "slit": {"value": None, "source": "missing"},
            "q": {"value": [0.5, 0.5, 3.0]}, "excluded": {"value": False}, "dead": {"value": False},
            "dead_in_scan": [], "counts": None,
            "calibration": {"folder": "D:/rf", "basis": "declared", "library": "f1", "curve": {"sha256": "c0"}}}
    return {**base, **over}


@pytest.mark.parametrize("used, now, kw, expect", [
    (_unit(), _unit(temperature={"value": 100.0, "source": "user"}), {},
     (Change("temperature", 97.0, 100.0, "user"),)),
    (_unit(), _unit(temperature={"value": 97.0, "source": "user"}), {}, ()),
    (_unit(), _unit(data="bb"), {}, (Change("data", "aa", "bb", None),)),
    (_unit(), _unit(dead_in_scan=["a14"]), {}, (Change("dead_in_scan", [], ["a14"], None),)),
    (_unit(), _unit(dead={"value": True}), {}, (Change("dead", False, True, None),)),
    (_unit(), _unit(), {"curve_sha_now": "d1"}, (Change("curve", "c0", "d1", None),)),
    (_unit(), _unit(), {"curve_sha_now": "c0", "library_now": "f2"}, ()),
    (_unit(calibration={"folder": "D:/rf", "basis": "inferred", "library": "f1", "curve": {"sha256": "c0"}}),
     _unit(), {"library_now": "f2"}, (Change("resolution_library", "f1", "f2", None),)),
    (_unit(temperature={"value": None, "source": "missing"}), _unit(temperature={"value": 100.0, "source": "user"}),
     {}, (Change("temperature", None, 100.0, "user"),)),
    (_unit(counts={"level": "warning", "override_applied": False}),
     _unit(counts={"level": "warning", "override_applied": True}), {}, (Change("counts_accepted", False, True, None),)),
    (_unit(), _unit(), {"folder_now": "E:/rf"}, (Change("resolution_folder", "D:/rf", "E:/rf", None),)),
    (_unit(calibration=None), _unit(), {"folder_now": "E:/rf", "curve_sha_now": "missing"}, ()),
    (_unit(), _unit(data="bb", temperature={"value": 1.0, "source": "user"}, excluded={"value": True}),
     {"curve_sha_now": "missing"},
     (Change("data", "aa", "bb", None), Change("temperature", 97.0, 1.0, "user"),
      Change("excluded", False, True, None), Change("curve", "c0", "missing", None))),
])
def test_changes_compare_values_in_field_order(used, now, kw, expect):
    assert changes(used, now, **kw) == expect


# --- damaged lines ---------------------------------------------------------------------------------
def test_damaged_entries_are_skipped_and_every_damaged_line_is_reported(root):
    values.save_value(root, "temperature", 100.0, ["spec1:42"])
    path = root / store.ENTRIES
    path.write_bytes(path.read_bytes() + b"[1, 2]\n"
                     + b'{"id": "e000000000009", "seq": 9, "at": "x", "kind": "mystery", "targets": []}\n'
                     + b'{"id": "e00000000000a", "seq": 10, "at": "x", "kind": "value", "fact": "temperature",'
                       b' "value": -5, "targets": ["spec1:42"]}\n'
                     + b'{"kind": "valu')
    store.append(root, store.SETTINGS, {"kind": "mystery"})
    r = store.read_all(root, store.ENTRIES)
    idx = index_entries(r.entries, lines=r.lines)
    assert [d.line for d in idx.damaged] == [3, 4]
    assert _t(root).used_value == 100.0                      # the bad value entry never reaches a fit
    assert damage_lines(root) == [
        "intake/settings.jsonl line 1 skipped: unknown kind 'mystery'",
        "intake/entries.jsonl line 2 skipped: not a JSON object",
        "intake/entries.jsonl line 3 skipped: unknown kind 'mystery'",
        "intake/entries.jsonl line 4 skipped: -5 is not a temperature value",
        "intake/entries.jsonl line 5 skipped: does not parse as JSON",
    ]
    assert damage_lines(root / "nowhere") == []
    assert inputs.read_entries(root)[-1]["seq"] == 10        # the store keeps them; only the index skips
