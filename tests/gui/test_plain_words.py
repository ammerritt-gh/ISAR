"""plain_words: the sentences the window shows for the backend's labels, ids and error texts (no Qt)."""
from __future__ import annotations

from isar.intake import inputs, readiness
from isar_gui import plain_words as pw


def test_every_backend_fallback_id_and_label_has_a_sentence():
    ids = {i for i in inputs.FALLBACKS} | {"slit_pending"}
    labels = {readiness.T_FALLBACK, readiness.SLIT_INFERRED, readiness.SLIT_BROADEST, readiness.ZERO_FALLBACK,
              readiness.NO_MONITOR, readiness.NO_SIDECAR, readiness.COUNTS_ACCEPTED}
    for name in ids | labels:
        sentence = pw.stand_in(name)
        assert sentence != name and sentence[0].islower(), name        # translated, not passed through
    assert pw.stand_in("something new") == "something new" and pw.stand_in(None) == ""      # unknown ones pass through


def test_group_titles_say_scan_sets_and_name_no_pipeline_stage():
    assert pw.group_title("temperature", "Temperature: 3 scans", "scan") == "Temperature: 3 scan sets"
    assert pw.group_title("temperature", "Temperature: 1 scan", "scan") == "Temperature: 1 scan set"
    assert pw.group_title("q_missing", "Q (for steering): analyzers a03 and a04 on 2 scans", "unit") == \
        "Q missing (only campaign planning needs it): analyzers a03 and a04 on 2 scan sets"
    assert pw.group_title("calibration_pending", "Calibration pending: 3 scans", "scan").startswith("Not calibrated yet")
    problem = ("no resolution folder is saved, and no folder holding curves was found in or below C:\\x; "
               "import the folder that holds both the data and the curves, or locate the curves folder")
    assert pw.group_title("resolution_folder", f"Resolution folder: {problem}", "plain") == "Resolution curves: none chosen yet"
    assert pw.group_title("files_not_read", "Files not read: 3 files", "plain") == "Files not read: 3 files"   # not scans
    assert pw.curve_problem(problem) == pw.NO_CURVES and pw.curve_problem("something else") == "something else"
    assert pw.plain_hold(f"a03: {problem}") == "a03: no resolution curves chosen yet"


def test_an_unread_files_reason_loses_the_exception_and_the_stem():
    raw = ("ValueError: CsV3Sb5_97K_0p5_0p5_3_bad: no analyzer could be read (a03: energy column is not numeric "
           "(could not convert string to float: 'not'))")
    assert pw.plain_reason(raw) == "a03: energy column is not numeric"
    assert pw.plain_reason("ValueError: could not convert string to float: 'da3p'") == \
        "could not convert string to float: 'da3p'"                       # nothing better to say: kept, not emptied
    assert pw.plain_reason("not recognised as BL43LXU data") == "not recognised as BL43LXU data"


def test_a_result_cells_tooltip_says_the_word_once():
    from isar.workspace_api.scan_list import Status
    from isar_gui.theme import DEEP
    from isar_gui.viewmodels.scan_list import _status_cell
    cell = _status_cell(Status("resolved", True, "resolved: count 1 (P 0.91)"), DEEP)
    assert cell.text == "clear · currentness not recorded"
    assert cell.tooltip.splitlines()[:2] == [
        "currentness not recorded", "clear  (the fitting code's word: resolved)"]
    assert cell.tooltip.splitlines()[-1] == "count 1 (P 0.91)" and "resolved: count" not in cell.tooltip
    assert _status_cell(Status("not run", False), DEEP).tooltip.splitlines()[0] == "not run"   # the word is its own


def test_every_stored_hkl_source_has_a_plain_source_word():
    from isar.schema.scan import Q_SOURCE_RUNG
    for word in (*Q_SOURCE_RUNG, "file"):                  # "none" is a missing hkl: it never shows a source
        if word != "none":
            assert pw.source_words(word) != word, word
    assert pw.source_words("reported") == "reported in the file"


def test_a_status_word_is_not_said_twice():
    assert pw.drop_echo("resolved", "resolved: count 1 (P 0.91)") == "count 1 (P 0.91)"
    assert pw.drop_echo("held", "no energy zero") == "no energy zero" and pw.drop_echo("held", None) is None
    assert pw.drop_echo("held", "held: ") is None


_SET = "CsV3Sb5_302K_0p5_0p5_3_sum"
_NO_FOLDER_CURVES = {"folder": None, "n_curves": 0, "n_analyzers": 0, "error": None,
                     "problem": "no resolution folder is saved, and no rf/ folder holding curves was found beside C:/data"}


def _companions(*, read=(), missing=(), unreadable=(), gpi=None, curves=None):
    sets = [*read, *missing, *unreadable]
    gpi = gpi or {"folder": None, "found": [], "missing": sets, "not_used": [], "error": None}
    return {"sidecar": {"read": list(read), "missing": list(missing), "unreadable": list(unreadable)},
            "gpi": gpi, "curves": curves}


def test_companion_words_when_the_var_is_read_and_nothing_else_is_there():
    c = _companions(read=[_SET], curves=_NO_FOLDER_CURVES)
    lines = pw.companion_lines(c)
    assert lines == [".var: found and read for all 1 scan set.",
                     "gpi: no gpi folder is saved or found beside the data.",
                     "Resolution curves (rf): " + pw.NO_CURVES]
    assert pw.companion_clause(c) == ".var found 1/1 · no gpi · no rf folder"


def test_companion_words_when_the_var_is_missing_or_unreadable_name_the_set_and_the_fix():
    missing = pw.companion_lines(_companions(missing=[_SET], curves=_NO_FOLDER_CURVES))[0]
    assert missing.startswith(f".var: found and read for 0 of 1 scan set; missing for {_SET}. ")
    assert "Put the .var in the same folder as the analyzer files, then add the data again." in missing
    assert pw.companion_clause(_companions(missing=[_SET])) == ".var missing 1/1 · no gpi"
    bad = pw.companion_lines(_companions(unreadable=[_SET]))[0]
    assert f"could not be read for {_SET}, so treated as missing. " in bad
    assert pw.companion_clause(_companions(unreadable=[_SET])) == ".var unreadable 1/1 · no gpi"


def test_companion_words_for_gpi_and_curves_found_and_counted():
    a, b = "S_a", "S_b"
    c = {"sidecar": {"read": [a, b], "missing": [], "unreadable": []},
         "gpi": {"folder": "C:/data/gpi", "found": [], "missing": [a, b], "not_used": [], "error": None},
         "curves": {"folder": "C:/data/rf", "n_curves": 112, "n_analyzers": 2, "problem": None, "error": None}}
    assert pw.companion_clause(c) == ".var found 2/2 · gpi 0/2 · rf folder rf (112 curves)"
    assert pw.companion_lines(c) == [".var: found and read for all 2 scan sets.",
                                     "gpi: found for 0 of 2 scan sets in C:/data/gpi; missing for S_a, S_b.",
                                     "Resolution curves (rf): 112 curves for 2 analyzers in C:/data/rf."]


def test_companion_words_for_gpi_not_used_a_curve_problem_and_failed_checks():
    gpi = {"folder": "C:/g", "found": [_SET], "missing": [], "not_used": [_SET], "error": None}
    c = _companions(read=[_SET], gpi=gpi,
                    curves={"folder": "C:/g/rf", "n_curves": 3, "n_analyzers": 1, "problem": "a04 has no curve",
                            "error": None})
    assert pw.companion_clause(c) == ".var found 1/1 · gpi 1/1, 1 not used · rf folder rf has a problem"
    assert pw.companion_lines(c)[1].endswith(f"not used for {_SET} (see Needs attention).")
    assert pw.companion_lines(c)[2] == "Resolution curves (rf): a04 has no curve"
    failed = _companions(read=[_SET], gpi={"folder": None, "found": [], "missing": [], "not_used": [], "error": "boom"},
                         curves={"folder": None, "n_curves": 0, "n_analyzers": 0, "problem": None, "error": "bang"})
    assert pw.companion_clause(failed) == ".var found 1/1 · gpi check failed · rf check failed"
    assert pw.companion_lines(failed)[1:] == ["gpi: the check failed (boom).",
                                              "Resolution curves (rf): the check failed (bang)."]


def test_companion_lists_name_three_sets_and_count_the_rest():
    lines = pw.companion_lines(_companions(missing=["s1", "s2", "s3", "s4", "s5"]))
    assert "missing for s1, s2, s3 and 2 more. " in lines[0]


def test_an_import_with_no_scan_sets_says_nothing_about_companions():
    c = _companions(curves=_NO_FOLDER_CURVES)
    assert pw.companion_lines(c) == [] and pw.companion_clause(c) == ""


def test_an_import_that_expects_no_var_still_says_what_gpi_and_curves_found():
    gpi = {"folder": None, "found": [], "missing": [_SET], "not_used": [], "error": None}
    c = {"sidecar": {"read": [], "missing": [], "unreadable": []}, "gpi": gpi, "curves": _NO_FOLDER_CURVES}
    assert pw.companion_lines(c) == ["gpi: no gpi folder is saved or found beside the data.",
                                     "Resolution curves (rf): " + pw.NO_CURVES]
    assert pw.companion_clause(c) == "no gpi · no rf folder"
    c["gpi"] = {"folder": "C:/g", "found": [_SET], "missing": ["S_b"], "not_used": [], "error": None}
    assert pw.companion_lines(c)[0] == "gpi: found for 1 of 2 scan sets in C:/g; missing for S_b."
    assert pw.companion_clause(c) == "gpi 1/2 · no rf folder"
