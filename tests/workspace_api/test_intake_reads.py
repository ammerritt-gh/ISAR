"""The BL43 intake reads (C-read-facts, C-read-fit-inputs): fact rows, what each pass used, and the changes since.

Workspaces are built with S2U1's writers (``values``, ``settings``, ``scans``, ``fit_inputs``) and a hand-written
``card.state.json`` calibration entry; nothing here runs the session.
"""
from __future__ import annotations

import pytest

from intake_fixtures import workspace
from isar.intake import curves, fit_inputs, inputs, scans, settings, store, values
from isar.intake.curves import CurveCheck, CurveFile
from isar.intake.inputs import Change
from isar.schema.card import load_state
from isar.workspace_api import FactRow, Workspace

U = "spec1:42:a03"
COUNTS = {"origin": "y_over_e_squared", "family": "_i", "bins_tested": 120, "bins_off": 40,
          "max_deviation": 0.31, "level": "warning"}
ROW = {"scan": "spec1:42", "experiment_id": "spec1", "scan_number": 42, "data_revision": "aa",
       "facts": {"T_K": {"value": 97.0, "source": "trailer"}, "slit": {"value": None, "source": "missing"}},
       "analyzers": {"a03": {"q": [0.5, 0.5, 3.0], "q_source": "filename", "counts": None},
                     "a05": {"q": None, "q_source": "none", "counts": COUNTS}}}
ROW_43 = {**ROW, "scan": "spec1:43", "scan_number": 43}
ROW_100 = {**ROW, "scan": "spec1:100", "scan_number": 100}


class Env:
    """A workspace with the import record, a curve check, a calibration state and the read handle."""

    def __init__(self, tmp_path, rows=(ROW, ROW_43, ROW_100)):
        self.session_ws = workspace(tmp_path)
        self.root = self.session_ws.root
        folder = tmp_path / "curves"
        folder.mkdir()
        settings.save_resolution_folder(self.root, folder)
        self.folder = str(folder.resolve())
        self.pattern = self.session_ws.load()[0].policy.calibration["resolution_pattern"]
        scans.record_import(self.root, rows)
        self.check(sha="c0")
        self.calibrate("spec1:42")
        self.ws = Workspace.open(self.root)

    def check(self, *, sha, library="f1", folder=None):
        curves._write(self.root, CurveCheck(
            checked_at="2026-10-28T09:00:00+00:00", checked_by="run", folder=folder or self.folder,
            pattern=self.pattern, source="setting", setting_source="user", found_beside=None, library=library,
            problem=None, curves=(CurveFile("a03", "40x40", "sarf_t_s2_40_40_a03", sha),), other_folders=(),
            discovered=None))

    def calibrate(self, scan, *, row=ROW, zero=0.148, basis="inferred", dead=(), key=None, **over):
        """A calibration state entry made from the inputs in force now (or `key`, to make it stale)."""
        dets = sorted(d for d in row["analyzers"] if d not in dead)
        key = key or inputs.calibration_inputs_key(
            data_revision=row["data_revision"], folder=self.folder, pattern=self.pattern, library="f1",
            declared_slit=None, analyzers=dets, counts_accepted=[])
        entry = {"content_hash": row["data_revision"], "library": "f1", "folder": self.folder,
                 "pattern": self.pattern, "declared": None, "setting": "40x40", "setting_source": "inferred",
                 "setting_basis": basis, "inputs_key": key,
                 "detectors": {"a03": {"zero": zero, "zero_source": "elastic_fit",
                                       "curve": {"file": "sarf_t_s2_40_40_a03", "sha256": "c0"}},
                               "a05": {"held_reason": "a05: counts are not integer"}}, **over}
        card, _ = self.session_ws.load()
        card.state.tabulated_calibration[scan] = entry
        card.save_state(self.session_ws.state_path)

    def pass_lines(self, unit=U, passes=("fast",), *, finish=True, end="complete", run_passes=None):
        """One run as the session writes it: a start line, started (and finished) lines frozen from the inputs
        in force now, and an end line (None for a run still going)."""
        rows = scans.read_scans(self.root)
        r = store.read_all(self.root, store.ENTRIES)
        index = inputs.index_entries(r.entries, lines=r.lines)
        scan, _, det = unit.rpartition(":")
        si = inputs.scan_inputs(rows[scan], index)
        state = load_state(self.session_ws.state_path).tabulated_calibration.get(scan)
        u = inputs.unit_inputs(rows[scan], si, det, state)
        run = fit_inputs.begin_run(self.root, passes=run_passes or passes)
        lines = [{"unit": unit, "scan": scan, "pass": p, "inputs": u, "hash": inputs.inputs_hash(u),
                  "fallbacks": list(inputs.fallbacks(u))} for p in passes]
        fit_inputs.write_started(self.root, run, lines)
        if finish:
            fit_inputs.write_finished(self.root, run, [
                {"unit": unit, "scan": scan, "pass": p, "status": "fitted", "fallbacks": ln["fallbacks"],
                 "result": {"verdict": "resolved"}} for p, ln in zip(passes, lines)])
        if end:
            fit_inputs.end_run(self.root, run, end)
        return run

    def facts(self, scan="spec1:42"):
        self.ws.refresh()
        return {(r.analyzer, r.fact): r for r in self.ws.fact_rows([scan])}

    def unit(self, unit=U):
        self.ws.refresh()
        (u,) = self.ws.fit_inputs([unit])
        return u


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def _t(r):
    return (r.file_value, r.file_source, r.user_value, r.used_value, r.used_source, r.disagreement, r.fallback)


# --- fact rows ---------------------------------------------------------------------------------
def test_fact_rows_follow_the_plan_table(env):
    f = env.facts()
    assert _t(f[(None, "temperature")]) == (97.0, "trailer", None, 97.0, "file", False, None)
    assert _t(f[(None, "slit")]) == (None, "missing", None, None, "missing", False, "slit_inferred")
    t = f[(None, "temperature")]
    assert (t.unit, t.enterable, t.file_conflicts, t.user_entry, t.user_reason) == ("K", True, (), None, None)
    assert f[(None, "slit")].unit == "mm"

    values.save_value(env.root, "temperature", 100.0, ["spec1:42"], reason="offset")
    t = env.facts()[(None, "temperature")]
    assert (t.user_value, t.user_reason, t.used_value, t.used_source, t.disagreement) == (
        100.0, "offset", 100.0, "user", True)
    assert t.user_entry == inputs.read_entries(env.root)[-1]["id"]

    values.use_file_value(env.root, "temperature", ["spec1:42"])
    assert _t(env.facts()[(None, "temperature")]) == (97.0, "trailer", None, 97.0, "file", False, None)

    values.save_value(env.root, "slit", "60x60", ["spec1:42"])           # saved after the last calibration
    assert _t(env.facts()[(None, "slit")]) == (None, "missing", "60x60", "60x60", "user", False, None)


def test_a_missing_temperature_falls_back_to_the_bose_limit_never_zero(tmp_path):
    e = Env(tmp_path, rows=({**ROW, "facts": {"T_K": {"value": None, "source": "missing"}}},))
    t = e.facts()[(None, "temperature")]
    assert (t.file_value, t.file_source, t.used_value, t.used_source, t.fallback) == (
        None, "missing", None, "missing", "bose_t0_limit")


def test_the_slit_fallback_is_pending_whenever_the_calibration_is_not_from_the_inputs_in_force(env):
    values.save_condition(env.root, "dead", ["spec1:42:a05"], reason="no counts")
    assert env.facts()[(None, "slit")].fallback == "slit_pending"        # the analyzers changed since
    env.calibrate("spec1:42", dead=("a05",))
    assert env.facts()[(None, "slit")].fallback == "slit_inferred"       # calibrated from what is in force
    env.calibrate("spec1:42", dead=("a05",), basis="broadest_curve_fallback")
    assert env.facts()[(None, "slit")].fallback == "slit_broadest_curve"
    env.check(sha="c0", library="f2")                                    # a library changed since
    assert env.facts()[(None, "slit")].fallback == "slit_pending"
    assert env.facts("spec1:43")[(None, "slit")].fallback == "slit_pending"   # never calibrated
    env.check(sha="c0")
    assert env.facts()[(None, "slit")].fallback == "slit_broadest_curve"
    (env.root / "rf2").mkdir()
    settings.save_resolution_folder(env.root, env.root / "rf2")          # a folder saved since the check
    assert env.facts()[(None, "slit")].fallback == "slit_pending"


def test_q_and_counts_rows_and_the_counts_acceptance(env):
    f = env.facts()
    q = f[("a03", "q")]
    assert (q.file_value, q.file_source, q.used_value, q.used_source, q.enterable, q.unit) == (
        [0.5, 0.5, 3.0], "filename", [0.5, 0.5, 3.0], "file", False, None)
    assert (f[("a05", "q")].file_source, f[("a05", "q")].used_source) == ("missing", "missing")
    assert ("a03", "counts") not in f                                    # no count record, no row
    c = f[("a05", "counts")]
    assert (c.file_value, c.file_source, c.enterable, c.user_value, c.used_value, c.fallback) == (
        COUNTS, "inferred", False, None, COUNTS, None)
    values.save_condition(env.root, "counts_accepted", ["spec1:42:a05"], reason="checked")
    c = env.facts()[("a05", "counts")]
    assert (c.user_value, c.user_reason, c.used_value, c.used_source, c.disagreement) == (True, "checked", COUNTS, "file", False)
    assert c.user_entry == inputs.read_entries(env.root)[-1]["id"]
    values.save_condition(env.root, "counts_accepted", ["spec1:42:a05"], declared=False)
    assert env.facts()[("a05", "counts")].user_value is None


def test_rows_are_ordered_and_filtered_by_scan_and_unknown_keys_are_skipped(env):
    env.ws.refresh()
    allrows = env.ws.fact_rows()
    assert [r.scan for r in allrows if r.fact == "temperature"] == ["spec1:42", "spec1:43", "spec1:100"]
    assert [(r.analyzer, r.fact) for r in allrows if r.scan == "spec1:42"] == [
        (None, "temperature"), (None, "slit"), ("a03", "q"), ("a05", "q"), ("a05", "counts")]
    assert {r.scan for r in env.ws.fact_rows(["spec1:43", "nope:1"])} == {"spec1:43"}
    assert env.ws.fact_rows([]) == [] and isinstance(allrows[0], FactRow)


def test_a_scan_changes_no_other_scan(env):
    values.save_value(env.root, "temperature", 120.0, ["spec1:43"])
    assert env.facts("spec1:42")[(None, "temperature")].used_value == 97.0
    assert env.facts("spec1:43")[(None, "temperature")].used_value == 120.0


def test_nothing_on_disk_reads_as_nothing(tmp_path):
    ws = Workspace.open(workspace(tmp_path).root)
    assert ws.fact_rows() == [] and ws.fit_inputs() == [] and ws.warnings == []


def test_reads_are_parsed_once_per_refresh_and_follow_a_refresh(env):
    env.ws.refresh()
    before = env.ws.fact_rows(["spec1:42"])
    values.save_value(env.root, "temperature", 100.0, ["spec1:42"])
    assert env.ws.fact_rows(["spec1:42"]) == before                      # the cached parse
    env.ws.refresh()
    assert env.ws.fact_rows(["spec1:42"]) != before


def test_damaged_intake_lines_are_reported_and_never_raise(env):
    values.save_value(env.root, "temperature", 100.0, ["spec1:42"])
    path = env.root / store.ENTRIES
    path.write_bytes(path.read_bytes() + b'{"id": "e1", "seq": 9, "at": "x", "kind": "mystery", "targets": []}\n'
                     + b'{"id": "e')
    env.ws.refresh()
    assert env.ws.fact_rows(["spec1:42"])[0].used_value == 100.0
    assert env.ws.warnings == ["intake/entries.jsonl line 2 skipped: unknown kind 'mystery'",
                               "intake/entries.jsonl line 3 skipped: does not parse as JSON"]
    env.ws.fact_rows()
    env.ws.fit_inputs()
    assert len(env.ws.warnings) == 2                                     # once per refresh


# --- fit inputs --------------------------------------------------------------------------------
def test_a_fast_pass_is_marked_when_the_user_saves_another_temperature(env):
    env.pass_lines()
    u = env.unit()
    assert (u.unit, u.scan, u.analyzer, u.deep) == (U, "spec1:42", "a03", None)
    assert (u.fast.state, u.fast.status, u.fast.changes, u.fast.changed) == ("finished", "fitted", (), False)
    assert u.fast.inputs["temperature"]["value"] == 97.0 and u.fast.fallbacks == ("slit_inferred",)
    values.save_value(env.root, "temperature", 100.0, ["spec1:42"])
    u = env.unit()
    assert u.fast.changes == (Change("temperature", 97.0, 100.0, "user"),) and u.fast.changed
    values.use_file_value(env.root, "temperature", ["spec1:42"])
    assert env.unit().fast.changes == ()                                 # judged on values, not history


def test_resaving_the_file_value_and_saving_metadata_mark_nothing(env):
    env.pass_lines()
    values.save_value(env.root, "temperature", 97.0, ["spec1:42"], reason="same as the file")
    settings.save_experiment_metadata(env.root, {"sample": "CsV3Sb5"})
    assert env.unit().fast.changes == ()


def test_changed_data_with_the_same_values_marks_only_the_data(env):
    """Acceptance: re-import with changed counts and unchanged temperature, slit and Q."""
    env.pass_lines()
    scans.record_import(env.root, [{**ROW, "data_revision": "bb"}])
    assert env.unit().fast.changes == (Change("data", "aa", "bb", None),)


def test_a_replaced_curve_marks_the_units_that_used_it(env):
    env.pass_lines()
    env.check(sha="d4")
    assert env.unit().fast.changes == (Change("curve", "c0", "d4", None),)
    env.check(sha="c0", library="f2")                                    # basis inferred: the library counts
    assert env.unit().fast.changes == (Change("resolution_library", "f1", "f2", None),)
    env.check(sha="d4", library="f3", folder=str(env.root))              # a check of another folder says nothing
    assert env.unit().fast.changes == ()


def test_a_folder_saved_since_marks_the_fit_at_once(env, tmp_path):
    env.pass_lines()
    other = tmp_path / "other"
    other.mkdir()
    settings.save_resolution_folder(env.root, other)
    assert env.unit().fast.changes == (Change("resolution_folder", env.folder, str(other.resolve()), None),)


def test_a_recalibration_that_moves_the_zero_is_marked(env):
    env.pass_lines()
    assert env.unit().fast.changes == ()                                 # an unchanged calibration shows none
    env.calibrate("spec1:42", zero=0.2)
    assert env.unit().fast.changes == (Change("calibration_zero", 0.148, 0.2, None),)


def test_a_dead_declaration_leaves_the_fit_marked_as_made_before(env):
    env.pass_lines()
    values.save_condition(env.root, "dead", [U], reason="no signal")
    assert env.unit().fast.changes == (Change("dead", False, True, None),)
    values.save_condition(env.root, "dead", ["spec1:42:a05"])
    values.save_condition(env.root, "dead", [U], declared=False)
    assert env.unit().fast.changes == (Change("dead_in_scan", [], ["a05"], None),)


def test_counts_accepted_marks_the_unit_that_was_held(env):
    unit = "spec1:42:a05"
    env.pass_lines(unit)
    values.save_condition(env.root, "counts_accepted", [unit], reason="checked")
    assert env.unit(unit).fast.changes == (Change("counts_accepted", False, True, None),)


def test_fast_and_deep_are_judged_separately(env):
    env.pass_lines(passes=("fast", "deep"))                              # a deep run takes both passes, at 97 K
    values.save_value(env.root, "temperature", 100.0, ["spec1:42"])
    env.pass_lines(passes=("fast",))                                     # a later fast run, at 100 K
    u = env.unit()
    assert (u.fast.inputs["temperature"]["value"], u.fast.changes) == (100.0, ())
    assert (u.deep.inputs["temperature"]["value"], u.deep.changes) == (97.0, (Change("temperature", 97.0, 100.0, "user"),))
    assert u.fast.run != u.deep.run


def test_a_running_pass_is_marked_too(env):
    first = env.pass_lines()
    values.save_value(env.root, "temperature", 100.0, ["spec1:42"])
    second = env.pass_lines(finish=False, end=None)                      # started, nothing finished, no end line
    u = env.unit()
    assert (u.fast.state, u.fast.status, u.fast.finished_at, u.fast.run) == ("running", None, None, second)
    assert u.fast.changes == () and second != first                      # started after the save
    values.save_value(env.root, "temperature", 120.0, ["spec1:42"])
    assert env.unit().fast.changes == (Change("temperature", 100.0, 120.0, "user"),)


def test_a_run_that_died_or_failed_never_counts(env):
    first = env.pass_lines()
    values.save_value(env.root, "temperature", 100.0, ["spec1:42"])
    env.pass_lines(end=None)                                             # no end line, and a later run starts
    env.pass_lines(end="failed")
    u = env.unit()
    assert u.fast.run == first and u.fast.changes == (Change("temperature", 97.0, 100.0, "user"),)
    env.pass_lines("spec1:43:a03", end=None)                             # a unit only a dead run touched
    env.pass_lines("spec1:100:a03")
    env.ws.refresh()
    assert [x.unit for x in env.ws.fit_inputs()] == [U, "spec1:100:a03"]


def test_units_are_ordered_by_scan_number_and_filtered_and_a_scan_without_a_row_has_no_changes(env):
    env.pass_lines("spec1:100:a03")
    env.pass_lines("spec1:42:a03")
    run = fit_inputs.begin_run(env.root, passes=["fast"])
    fit_inputs.write_started(env.root, run, [{"unit": "gone:1:a03", "scan": "gone:1", "pass": "fast",
                                              "inputs": {}, "hash": "h", "fallbacks": []}])
    fit_inputs.end_run(env.root, run, "complete")                        # no finished line: not a counting pass
    env.pass_lines("spec1:43:a03")
    env.ws.refresh()
    assert [u.unit for u in env.ws.fit_inputs()] == ["spec1:42:a03", "spec1:43:a03", "spec1:100:a03"]
    got = env.ws.fit_inputs(["spec1:100:a03", "nope:9:a01", "spec1:42:a03"])
    assert [u.unit for u in got] == ["spec1:42:a03", "spec1:100:a03"]
    run = fit_inputs.begin_run(env.root, passes=["fast"])
    fit_inputs.write_started(env.root, run, [{"unit": "gone:1:a03", "scan": "gone:1", "pass": "fast",
                                              "inputs": {}, "hash": "h", "fallbacks": []}])
    fit_inputs.write_finished(env.root, run, [{"unit": "gone:1:a03", "scan": "gone:1", "pass": "fast",
                                               "status": "fitted", "fallbacks": [], "result": None}])
    fit_inputs.end_run(env.root, run, "complete")
    env.ws.refresh()
    first = env.ws.fit_inputs()[0]
    assert (first.unit, first.fast.changes) == ("gone:1:a03", ())


def test_a_pass_this_version_cannot_compare_is_listed_without_changes(env):
    run = fit_inputs.begin_run(env.root, passes=["fast"])
    fit_inputs.write_started(env.root, run, [{"unit": U, "scan": "spec1:42", "pass": "fast",
                                              "inputs": {"temperature": 5}, "hash": "h", "fallbacks": []}])
    fit_inputs.write_finished(env.root, run, [{"unit": U, "scan": "spec1:42", "pass": "fast", "status": "fitted",
                                               "fallbacks": [], "result": None}])
    fit_inputs.end_run(env.root, run, "complete")
    assert env.unit().fast.changes == ()
