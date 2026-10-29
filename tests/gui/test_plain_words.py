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
    long = "Resolution folder: no resolution folder is saved, and no rf/ folder holding curves was found beside C:\\x"
    assert pw.group_title("resolution_folder", long, "plain") == "Resolution curves: none chosen yet"
    assert pw.group_title("files_not_read", "Files not read: 3 files", "plain") == "Files not read: 3 files"   # not scans
    assert pw.curve_problem("no resolution folder is saved, and no rf/ folder holding curves was found beside C:\\x") \
        == pw.NO_CURVES and pw.curve_problem("something else") == "something else"


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
