"""The gpi files (C-gpi-reader, C-gpi-check, C-gpi-settings): the reader, the |Q| check and the check report.

Every number is generated here; nothing is measured (hard rule 10)."""
import json
import logging

import pytest

from intake_fixtures import bl43_workspace, write_gpi, write_library
from isar.intake import gpi, settings, store
from isar.intake.gpi import check_gpi, entries_in_force, judge, read_gpi, read_gpi_check

SET = "Smp_100K_0p5_0p5_3_A"
Q = {"a03": 10.00, "a04": 11.46, "a05": 12.92}
HKL = {"a03": (0.5, 0.5, 3.0), "a04": (0.5, 0.5, 3.5), "a05": (0.5, 0.5, 4.0)}


def _macro(tmp_path, text):
    p = tmp_path / "gpi.X"
    p.write_text(text + "\n", encoding="utf-8", newline="\n")
    return read_gpi(p)


# --- reader: C-gpi-reader's worked examples ------------------------------------------------------------
def test_hkl_comes_from_the_numeric_triplet_never_the_tuple_string(tmp_path):
    m = _macro(tmp_path, 'hkl_a03 = "(9, 9, 9)"; hkl_a03_H = 0.5; hkl_a03_K = 0.5; hkl_a03_L = 3.1')
    assert m.hkl == {"a03": [0.5, 0.5, 3.1]} and m.unread == ()


def test_a_partial_triplet_is_no_hkl(tmp_path):
    assert _macro(tmp_path, "hkl_a04_H = 0.5; hkl_a04_K = 0.5").hkl == {"a04": None}


def test_q_reads_the_quoted_per_nm_string_and_a_bare_number(tmp_path):
    m = _macro(tmp_path, 'Q_a03 = " 23.22/nm"\nQ_a4 = 1.5e1\nQ_a05 = " 3.0/A"')
    assert m.q_nm == {"a03": 23.22, "a04": 15.0, "a05": None}           # another unit does not read
    assert len(m.unread) == 1 and "Q_a05" in m.unread[0]


def test_the_slit_is_agaph_by_agapv(tmp_path):
    assert _macro(tmp_path, "agaph = 40 #mm\nagapv = 40 #mm").slit == "40x40"
    assert _macro(tmp_path, "agaph = 60 #mm").slit is None               # one dimension: no slit
    assert _macro(tmp_path, "agaph = 0\nagapv = 40").slit is None         # finite and positive only


def test_the_last_assignment_decides_and_an_unreadable_last_one_never_falls_back(tmp_path):
    m = _macro(tmp_path, "agapv = 40\nagaph = 40\nagaph = 2*20")
    assert m.slit is None and len(m.unread) == 1 and "agaph" in m.unread[0] and "line 3" in m.unread[0]
    assert _macro(tmp_path, "agaph = 2*20\nagaph = 30\nagapv = 30").slit == "30x30"


def test_sarf_name_is_read_and_resolution_name_is_not(tmp_path):
    m = _macro(tmp_path, "resolution_name = 'rf_x'\nsarf_name = 'sarf_24Nov10_s50_40_40'")
    assert m.sarf_name == "sarf_24Nov10_s50_40_40"
    assert _macro(tmp_path, "sarf_name = ''").sarf_name is None


def test_statements_that_are_not_literal_assignments_are_ignored(tmp_path):
    m = _macro(tmp_path, 'sarf=1; if (resolution_name eq "") sarf=0\nset xlabel "E"\nplot x')
    assert (m.hkl, m.q_nm, m.slit, m.sarf_name, m.problem_name, m.unread) == ({}, {}, None, None, None, ())


def test_a_hash_inside_quotes_is_not_a_comment(tmp_path):
    m = _macro(tmp_path, "title = 'a # not a comment'; agapv = 30\nagaph = 20")
    assert m.slit == "20x30"


def test_problem_name_and_file_identity(tmp_path):
    m = _macro(tmp_path, "problem_name1='Other'")
    assert m.problem_name == "Other" and len(m.sha256) == 64 and m.path.endswith("gpi.X")
    with pytest.raises(OSError):
        read_gpi(tmp_path / "gpi.missing")


def test_the_generated_macro_reads_back_what_it_was_given(tmp_path):
    m = read_gpi(write_gpi(tmp_path, SET, q_nm={**Q, "a05": None}, hkl={**HKL, "a04": None}))
    assert m.q_nm == {"a03": 10.0, "a04": 11.46}
    assert m.hkl == {"a03": [0.5, 0.5, 3.0], "a05": [0.5, 0.5, 4.0]}
    assert (m.slit, m.sarf_name, m.problem_name, m.unread) == ("40x40", "sarf_z_s1_40_40", SET, ())


# --- judge -----------------------------------------------------------------------------------------------
def _gpi_q(q):
    return gpi.GpiMacro("D:/x/gpi.SET", "0" * 64, None, {}, q, None, None, ())


def test_judge_accepts_within_tolerance_and_ignores_analyzers_without_data():
    assert judge(_gpi_q({"a03": 23.22, "a04": 24.68, "a05": 26.1}), {"a03": 23.22, "a04": 24.70}) is None


def test_judge_rejects_beyond_tolerance_naming_the_analyzer_and_both_values():
    line = judge(_gpi_q({"a03": 23.22, "a04": 24.68}), {"a03": 23.22, "a04": 24.90})
    assert line.startswith("gpi.SET:") and "a04 (data 24.90, gpi 24.68 /nm)" in line and "a03" not in line


def test_judge_rejects_a_missing_value_on_either_side_and_lists_it_first():
    assert "a03" in judge(_gpi_q({"a03": 23.22}), {"a03": None})
    line = judge(_gpi_q({"a03": 23.22, "a04": 20.0, "a05": 21.0, "a06": 22.0}),
                 {"a03": 23.22, "a04": 20.2, "a05": 21.9, "a06": 22.5, "a07": 1.0})
    assert line.index("a07") < line.index("a05") < line.index("a06")   # missing, then 0.9, then 0.5
    assert "a04" not in line and "and 1 more" in line                  # three named, worst first


# --- the check -------------------------------------------------------------------------------------------
@pytest.fixture
def exp(tmp_path):
    """A BL43 workspace with the sarf_t library plus a `sarf_z_s1_40_40` set for a03 and a04 only, and one row."""
    ws, data = bl43_workspace(tmp_path, stems=("A",))
    write_library(ws.root / "rf", {"40x40": 1.3}, ("a03", "a04"), prefix="sarf_z")
    card, _session = ws.load()
    rows = {"Smp:1": {"scan": "Smp:1", "set": SET, "analyzers": {d: {"Q_nm": q} for d, q in Q.items()}}}
    return ws.root, card, data, rows


def test_check_worked_example_accepts_then_a_changed_data_q_rejects_the_unchanged_file(exp):
    root, card, data, rows = exp
    write_gpi(data, SET, q_nm=Q, hkl=HKL)
    check = check_gpi(root, card, [data], by="import", rows=rows)
    s = check.scans["Smp:1"]
    assert s.accepted and s.problem is None and sorted(s.q) == ["a03", "a04", "a05"]
    assert (s.slit, s.sarf_name, s.sarf_setting) == ("40x40", "sarf_z_s1_40_40", "40x40")
    assert s.curves["a03"]["file"] == "sarf_z_s1_40_40_a03" and s.curves["a04"]["file"] == "sarf_z_s1_40_40_a04"
    assert s.curves["a05"] is None and any("a05" in n and "missing curve" in n for n in s.notes)
    assert read_gpi_check(root) == check and entries_in_force(root) == check.scans

    rows["Smp:1"]["analyzers"]["a04"]["Q_nm"] = 11.70                  # only the data moved
    s = check_gpi(root, card, [data], by="run", rows=rows).scans["Smp:1"]
    assert (s.accepted, s.q, s.slit, s.sarf_name, s.curves) == (False, {}, None, None, {})
    assert "a04 (data 11.70, gpi 11.46 /nm)" in s.problem
    assert read_gpi_check(root).scans["Smp:1"] == s                   # the report is replaced, never merged


def test_one_rejected_file_is_one_line_however_many_scans_share_it(exp):
    root, card, data, rows = exp
    write_gpi(data, SET, q_nm={**Q, "a03": 5.0}, hkl=HKL)
    rows["Smp:2"] = {**rows["Smp:1"], "scan": "Smp:2"}
    lines = check_gpi(root, card, [data], by="import", rows=rows).lines()
    assert sum("nothing from this file is used" in ln for ln in lines) == 1


def test_discovery_saves_only_when_none_is_saved_and_never_switches(exp, tmp_path):
    root, card, data, rows = exp
    assert check_gpi(root, card, [data], by="import", rows=rows).folder is None   # no gpi_files/ yet
    write_gpi(data, SET, q_nm=Q, hkl=HKL)
    other = tmp_path / "other"
    write_gpi(other, SET, q_nm=Q, hkl=HKL)
    check = check_gpi(root, card, [data, other], by="import", rows=rows)
    found = str((data / "gpi_files").resolve())
    assert (check.discovered, check.folder, check.setting_source) == (found, found, "discovered")
    assert check.found_beside == str(data.resolve()) and any("found" in ln for ln in check.lines())
    check = check_gpi(root, card, [other], by="import", rows=rows)
    assert (check.discovered, check.folder) == (None, found)            # a saved folder is never switched
    assert settings.save_gpi_folder(root, other / "gpi_files").ok        # the user may point it elsewhere
    assert check_gpi(root, card, [data], by="import", rows=rows).setting_source == "user"
    assert len([e for e in store.read(root, store.SETTINGS) if e["kind"] == "gpi_folder"]) == 2


def test_a_removed_file_is_withdrawn_and_a_restored_one_returns(exp):
    root, card, data, rows = exp
    path = write_gpi(data, SET, q_nm=Q, hkl=HKL)
    assert check_gpi(root, card, [data], by="import", rows=rows).scans["Smp:1"].accepted
    text = path.read_bytes()
    path.unlink()
    assert check_gpi(root, card, [data], by="run", rows=rows).scans == {} and entries_in_force(root) == {}
    path.write_bytes(text)
    assert entries_in_force(root) == {}                                 # nothing until the next check
    assert check_gpi(root, card, [data], by="run", rows=rows).scans["Smp:1"].accepted


def test_a_folder_chosen_since_the_check_contributes_nothing_and_a_malformed_report_is_logged(exp, tmp_path, caplog):
    root, card, data, rows = exp
    write_gpi(data, SET, q_nm=Q, hkl=HKL)
    check_gpi(root, card, [data], by="import", rows=rows)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    settings.save_gpi_folder(root, elsewhere)
    assert entries_in_force(root) == {}
    (root / gpi.GPI_CHECK).write_text(json.dumps({"schema": "nope"}), encoding="utf-8", newline="\n")
    with caplog.at_level(logging.WARNING, logger="isar.intake.gpi"):
        assert entries_in_force(root) == {}
    assert any("gpi.json" in r.getMessage() for r in caplog.records)
    with pytest.raises(ValueError):
        read_gpi_check(root)


def test_accepted_file_notes_problem_name_unread_lines_and_analyzers_without_hkl(exp):
    root, card, data, rows = exp
    path = write_gpi(data, SET, q_nm=Q, hkl={**HKL, "a05": None}, problem_name="Elsewhere")
    path.write_text(path.read_text(encoding="utf-8") + "agaph = 2*20\n", encoding="utf-8", newline="\n")
    s = check_gpi(root, card, [data], by="import", rows=rows).scans["Smp:1"]
    assert s.accepted and sorted(s.q) == ["a03", "a04"] and s.slit is None
    assert any("problem_name1" in n for n in s.notes) and any("agaph" in n for n in s.notes)
    assert any("a05" in n and "HKL" in n for n in s.notes)


def test_gpi_folder_setting_refuses_like_the_resolution_folder(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    r = settings.save_gpi_folder(ws, tmp_path / "nope")
    assert (r.ok, r.reason) == (False, f"gpi folder {tmp_path / 'nope'} does not exist")
    found = tmp_path / "a" / "gpi_files"
    found.mkdir(parents=True)
    assert settings.save_gpi_folder(ws, found, source="discovered", found_beside=str(found.parent)).ok
    again = settings.save_gpi_folder(ws, found, source="discovered", found_beside=str(found.parent))
    assert not again.ok and again.reason.startswith("a gpi folder is already saved")
    s = settings.current_settings(ws)
    assert s.gpi_folder.path == str(found.resolve()) and s.resolution_folder is None


def test_a_named_curve_is_mapped_for_every_scan_of_the_check_not_just_the_first(exp):
    # the named set loads once per check; a later scan with an analyzer the first lacks still gets its curve
    root, card, data, rows = exp
    write_library(root / "rf", {"40x40": 1.3}, ("a05",), prefix="sarf_z")
    write_gpi(data, SET, q_nm=Q, hkl=HKL)
    rows["Smp:1"]["analyzers"].pop("a05")
    rows["Smp:2"] = {**rows["Smp:1"], "scan": "Smp:2", "analyzers": {d: {"Q_nm": q} for d, q in Q.items()}}
    s = check_gpi(root, card, [data], by="import", rows=rows).scans
    assert s["Smp:1"].accepted and s["Smp:2"].accepted
    assert s["Smp:2"].curves["a05"]["file"] == "sarf_z_s1_40_40_a05"
