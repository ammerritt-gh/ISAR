"""Readiness and the needs-attention list (C-needs-attention, C-import-summary, C-read-conditions).

The worked examples R1-R8 and the import-summary examples run on hand-built import rows (equal to the
C-scan-import examples S3U1 writes) with a hand-written curve check and calibration state. The parity tests
run the real stages on generated BL43 data (``tests/intake_fixtures.py``) beside the readiness read: the
curve gap, the calibration inputs key and the count hold must agree. Nothing reads ``real_data/``.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


import intake_fixtures as fx
import isar
from isar.intake import curves, inputs, readiness, scans, settings, store, values
from isar.intake.curves import CurveCheck, CurveFile
from isar.intake.readiness import UnitMarks
from isar.session import calibrations, driver, ingest, scan_delivery
from isar.session.state import ScanEntry
from isar.workspace_api import Workspace, import_summary, needs_attention

NO_CURVE_20 = "a04: no resolution curve for a04 at slit 20x20"
COUNTS = {"origin": "y_over_e_squared", "family": "_i", "bins_tested": 120, "bins_off": 40,
          "max_deviation": 0.31, "level": "warning"}
CURVES = (CurveFile("a03", "20x20", "sarf_t_s3_20_20_a03", "c1"), CurveFile("a03", "60x60", "sarf_t_s1_60_60_a03", "c2"),
          CurveFile("a04", "60x60", "sarf_t_s1_60_60_a04", "c3"))


def test_import_root_is_the_worktree():
    # A sibling checkout on sys.path would make every assertion below test the wrong code.
    assert Path(isar.__file__).resolve().parents[1] == Path(__file__).resolve().parents[2]


def row(n, *, T=None, slit=None, analyzers=("a03", "a04"), counts=None, sidecar="read", left_out=None,
        exposure="monitor", lines=(), flags=()):
    """An import row as ``scans.scan_row`` and S3U1's ``import`` block make it (C-scan-import)."""
    side = {"state": sidecar, "reason": None if sidecar == "read" else f"no Smp_{n}.var beside the analyzer files"}
    return {
        "scan": f"Smp:{n}", "experiment_id": "Smp", "scan_number": n, "set": f"Smp_{n}", "data_revision": f"d{n}",
        "imported_at": f"2026-11-03T09:12:{n % 60:02d}+00:00",
        "files": {a: f"Smp_{n}_{a}" for a in analyzers}, "sidecar": None if sidecar == "missing" else f"Smp_{n}.var",
        "facts": {"T_K": {"value": T, "source": "header" if T is not None else "missing"},
                  "slit": {"value": slit, "source": "header" if slit else "missing"}},
        "analyzers": {a: {"q": None, "q_source": "none", "counts": (counts or {}).get(a), "exposure": exposure}
                      for a in analyzers},
        "flags": list(flags),
        "import": {"sidecar": side, "left_out": left_out or {}, "unrecognised_lines": list(lines),
                   "unparsed_lines": []}}


class World:
    """A BL43 workspace with an import record, a saved (discovered) curve folder and its curve check."""

    def __init__(self, tmp_path, rows, *, folder=True):
        self.ws = fx.workspace(tmp_path)
        self.root = self.ws.root
        self.pattern = self.ws.load()[0].policy.calibration["resolution_pattern"]
        self.beside = tmp_path / "set1"
        self.folder = self.beside / "rf"
        self.folder.mkdir(parents=True)
        if folder:
            assert settings.save_resolution_folder(self.root, self.folder, source="discovered",
                                                   found_beside=str(self.beside)).ok
        scans.record_import(self.root, rows)
        self.rows = {r["scan"]: r for r in rows}
        if folder:
            self.check()

    @property
    def path(self) -> str:
        return settings.current_settings(self.root).resolution_folder.path

    def check(self, *, library="L1", found=CURVES, problem=None, folder="saved", source="setting"):
        curves._write(self.root, CurveCheck(
            checked_at="2026-11-03T09:12:44.120511+00:00", checked_by="import",
            folder=self.path if folder == "saved" else folder, pattern=self.pattern, source=source,
            setting_source="discovered" if source == "setting" else None,
            found_beside=str(self.beside) if source == "setting" else None,
            library=library if folder else None, problem=problem, curves=tuple(found), other_folders=(),
            discovered=None))

    def key(self, scan, *, library="L1", declared=None, dead=(), accepted=()):
        live = [a for a in self.rows[scan]["analyzers"] if a not in dead]
        return inputs.calibration_inputs_key(
            data_revision=self.rows[scan]["data_revision"], folder=self.path, pattern=self.pattern, library=library,
            declared_slit=declared, analyzers=live, counts_accepted=[a for a in live if a in accepted])

    def calibrate(self, scan, *, setting, basis, declared=None, library="L1", pipeline=(), held=None, dead=(),
                  key=None):
        """The state entry ``refresh_tabulated`` stores for a scan calibrated from the inputs in force."""
        dets = {}
        for a in self.rows[scan]["analyzers"]:
            if a in dead:
                continue
            dets[a] = ({"held_reason": held[a], "flags": []} if a in (held or {}) else
                       {"zero": 0.1, "zero_source": "pipeline_zero" if a in pipeline else "elastic_fit"})
        card, _ = self.ws.load()
        card.state.tabulated_calibration[scan] = {
            "content_hash": self.rows[scan]["data_revision"], "inputs_key": key or self.key(scan, library=library, declared=declared, dead=dead),
            "library": library, "folder": self.path, "pattern": self.pattern, "declared": declared,
            "setting": setting, "setting_source": "declared" if declared else "inferred", "setting_basis": basis,
            "detectors": dets}
        card.save_state(self.ws.state_path)

    def files(self, files):
        """The top-level ``files`` section S3U1 adds to the record."""
        path = self.root / scans.INTAKE_SCANS
        path.parent.mkdir(exist_ok=True)
        doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"schema": scans.SCHEMA, "scans": {}}
        path.write_text(json.dumps({**doc, "files": files}), encoding="utf-8", newline="\n")


def r1(tmp_path):
    """The R1 setup: Smp:1001 has T 302 K and no slit; Smp:1002 no T, a saved slit 20x20 and no sidecar."""
    w = World(tmp_path, [row(1001, T=302.0), row(1002, sidecar="missing")])
    assert values.save_value(w.root, "slit", "20x20", ["Smp:1002"]).ok
    return w


def kinds(na):
    return [g.kind for g in na.groups]


def grp(na, kind):
    return next((g for g in na.groups if g.kind == kind), None)


def items(na, kind):
    return [(i.scan, i.analyzer, i.reason) for i in grp(na, kind).items]


def uses(na, scan, a, *names):
    u = na.scans[scan].analyzers[a].uses
    return tuple(u[n].state for n in names)


# --- the worked examples ---------------------------------------------------------------------------------
def test_r1_before_any_run(tmp_path):
    w = r1(tmp_path)
    na = readiness.evaluate(w.root)
    assert na.evaluated and na.note is None
    assert kinds(na) == ["curve_missing", "temperature", "slit", "calibration_pending", "candidate_coverage",
                         "sidecar", "q_missing"]
    g = grp(na, "curve_missing")
    assert (g.summary, g.state, g.action, g.can_start) == (
        "Resolution curves: analyzer a04 on 1 scan", "held", "locate_folder", False)
    assert items(na, "curve_missing") == [("Smp:1002", "a04", NO_CURVE_20)]
    g = grp(na, "temperature")
    assert (g.summary, g.action, g.can_start, g.fallback_in_use) == ("Temperature: 1 scan", "enter_value", True, True)
    assert [(i.scan, i.fallback) for i in g.items] == [("Smp:1002", readiness.T_FALLBACK)]
    g = grp(na, "slit")
    assert (g.summary, g.state, g.action) == ("Slit: 1 scan", "pending", "enter_value")
    assert items(na, "slit") == [("Smp:1001", None, readiness.SLIT_PENDING)]
    g = grp(na, "calibration_pending")
    assert ([i.scan for i in g.items], g.action, g.can_start, g.state) == (["Smp:1001", "Smp:1002"], "start_analysis", True, "pending")
    assert items(na, "candidate_coverage") == [("Smp:1001", "a04", "no curve at 20x20; held only if 20x20 is chosen")]
    g = grp(na, "sidecar")
    assert ([i.scan for i in g.items], g.action) == (["Smp:1002"], "import_again")
    g = grp(na, "q_missing")
    assert (g.summary, g.action, g.analyzers) == ("Q (for steering): analyzers a03 and a04 on 2 scans", "none", ("a03", "a04"))
    # per use
    assert uses(na, "Smp:1002", "a04", "calibrate", "fit") == ("held", "held")
    assert na.scans["Smp:1002"].analyzers["a04"].can_start is False
    assert uses(na, "Smp:1002", "a03", "calibrate", "fit") == ("pending", "pending")
    assert na.scans["Smp:1002"].analyzers["a03"].can_start is True
    assert na.scans["Smp:1002"].can_start is True and na.scans["Smp:1002"].action == "locate_folder"
    assert na.scans["Smp:1001"].action == "start_analysis"
    assert list(na.scans) == ["Smp:1001", "Smp:1002"]


def test_r2_after_a_run(tmp_path):
    w = r1(tmp_path)
    w.calibrate("Smp:1001", setting="60x60", basis="inferred", pipeline=("a04",))
    w.calibrate("Smp:1002", setting="20x20", basis="declared", declared="20x20", held={"a04": NO_CURVE_20})
    na = readiness.evaluate(w.root)
    assert kinds(na) == ["curve_missing", "temperature", "slit", "candidate_coverage", "zero_fallback", "sidecar",
                         "q_missing"]
    g = grp(na, "slit")
    assert (g.state, [(i.scan, i.fallback) for i in g.items]) == ("fallback", [("Smp:1001", readiness.SLIT_INFERRED)])
    assert grp(na, "zero_fallback").summary == "Energy zero from the file axis: analyzer a04 on 1 scan"
    assert items(na, "candidate_coverage")[0][:2] == ("Smp:1001", "a04")
    assert items(na, "curve_missing") == [("Smp:1002", "a04", NO_CURVE_20)]
    a03 = na.scans["Smp:1002"].analyzers["a03"]
    assert (a03.uses["calibrate"].state, a03.uses["fit"].state, a03.uses["fit"].fallback) == (
        "ready", "fallback", readiness.T_FALLBACK)
    assert a03.action == "enter_value"          # nothing blocks it; the optional gap is what is left


def test_r3_a_replaced_curve_puts_both_scans_back_in_calibration_pending(tmp_path):
    w = r1(tmp_path)
    w.calibrate("Smp:1001", setting="60x60", basis="inferred")
    w.calibrate("Smp:1002", setting="20x20", basis="declared", declared="20x20", held={"a04": NO_CURVE_20})
    w.check(library="L2")
    na = readiness.evaluate(w.root)
    g = grp(na, "calibration_pending")
    assert [(i.scan, i.reason) for i in g.items] == [
        ("Smp:1001", readiness.CURVES_CHANGED), ("Smp:1002", readiness.CURVES_CHANGED)]
    assert g.can_start is True and g.action == "start_analysis"


def test_r4_a_dead_analyzer_leaves_its_curve_gap(tmp_path):
    w = r1(tmp_path)
    assert values.save_condition(w.root, "dead", ["Smp:1002:a04"], reason="no counts").ok
    na = readiness.evaluate(w.root)
    assert "curve_missing" not in kinds(na)
    g = grp(na, "declared")
    assert (g.summary, g.action, g.can_start, g.state) == (
        "Declared dead or excluded: analyzer a04 on 1 scan", "reverse_declaration", False, "held")
    assert items(na, "declared") == [("Smp:1002", "a04", "declared dead (no counts)")]
    assert uses(na, "Smp:1002", "a04", "read", "calibrate") == ("ready", "held")
    assert na.scans["Smp:1002"].uses["calibrate"].state == "pending"      # the worst over a03 only
    assert na.scans["Smp:1002"].analyzers["a04"].action == "reverse_declaration"
    assert kinds(na).count("candidate_coverage") == 1 and items(na, "candidate_coverage")[0][0] == "Smp:1001"


def test_an_excluded_scan_is_one_declared_item_and_holds_every_analyzer(tmp_path):
    w = r1(tmp_path)
    assert values.save_condition(w.root, "excluded", ["Smp:1001"]).ok
    na = readiness.evaluate(w.root)
    assert items(na, "declared") == [("Smp:1001", None, "scan excluded")]
    assert grp(na, "declared").summary == "Declared dead or excluded: 1 scan"
    s = na.scans["Smp:1001"]
    assert (s.can_start, s.action, s.uses["calibrate"].state) == (False, "reverse_declaration", "held")
    assert all(i.scan != "Smp:1001" for g in na.groups if g.kind != "declared" for i in g.items)


def test_r5_a_running_analyzer(tmp_path):
    w = r1(tmp_path)
    na = readiness.evaluate(w.root, marks={"Smp:1001:a03": UnitMarks(running=True)})
    g = na.groups[0]
    assert (g.kind, g.summary, g.action, g.can_start, g.state) == (
        "running", "Analysis running: analyzer a03 on 1 scan", "wait", None, "running")
    assert na.scans["Smp:1001"].analyzers["a03"].can_start is None
    assert na.scans["Smp:1001"].analyzers["a03"].action == "wait"
    qa = [i for i in grp(na, "q_missing").items if (i.scan, i.analyzer) == ("Smp:1001", "a03")]
    assert qa and qa[0].running is True
    assert not any(i.running for i in grp(na, "q_missing").items if i.analyzer == "a04")


def test_values_changed_marks_become_a_group(tmp_path):
    w = r1(tmp_path)
    na = readiness.evaluate(w.root, marks={"Smp:1001:a03": UnitMarks(changed_fast="temperature: 100.0 -> 120.0 (user)")})
    g = grp(na, "values_changed")
    assert (g.state, g.action, g.can_start) == ("ready", "start_analysis", True)
    assert g.items[0].values_changed == ("temperature: 100.0 -> 120.0 (user)",)
    assert na.scans["Smp:1001"].analyzers["a03"].values_changed == ("temperature: 100.0 -> 120.0 (user)",)


def test_r6_no_resolution_folder(tmp_path):
    problem = "no resolution folder is saved, and no rf/ folder holding curves was found beside D:\\bt\\set3"
    w = World(tmp_path, [row(1001, T=302.0), row(1002, T=302.0)], folder=False)
    w.check(found=(), problem=problem, folder=None, source="none")
    na = readiness.evaluate(w.root)
    assert kinds(na)[0] == "resolution_folder"
    g = na.groups[0]
    assert (g.summary, g.n_scans, g.action, g.can_start) == (f"Resolution folder: {problem}", 2, "locate_folder", False)
    assert "curve_missing" not in kinds(na) and "calibration_pending" not in kinds(na)
    for s in na.scans.values():
        for a, ar in s.analyzers.items():
            assert ar.uses["calibrate"].state == "held" and ar.uses["calibrate"].reason == f"{a}: {problem}"
    # saved before the next check: the group is gone and both scans are pending
    assert settings.save_resolution_folder(w.root, w.folder).ok
    na = readiness.evaluate(w.root)
    assert "resolution_folder" not in kinds(na)
    g = grp(na, "calibration_pending")
    assert [(i.scan, i.reason) for i in g.items] == [("Smp:1001", readiness.FOLDER_CHANGED), ("Smp:1002", readiness.FOLDER_CHANGED)]
    assert g.can_start is True


def test_a_folder_that_holds_no_curves_is_the_experiment_hold(tmp_path):
    w = World(tmp_path, [row(1001, T=302.0)])
    w.check(found=())
    na = readiness.evaluate(w.root)
    assert kinds(na)[0] == "resolution_folder" and "holds no resolution curves" in na.groups[0].summary
    assert "curve_missing" not in kinds(na)


def test_r7_counts_that_may_not_be_raw(tmp_path):
    w = World(tmp_path, [row(1001, T=302.0, counts={"a04": COUNTS}), row(1002, T=302.0)])
    na = readiness.evaluate(w.root)
    g = grp(na, "counts_unaccepted")
    assert (g.summary, g.state, g.action, g.can_start) == (
        "Counts may not be raw: analyzer a04 on 1 scan", "held", "accept_counts", False)
    assert items(na, "counts_unaccepted") == [("Smp:1001", "a04", (
        "a04: counts may not be raw: 40 of 120 bins are off an integer after N = (y/e)^2 (largest deviation "
        "0.31); not fitted until the counts are accepted"))]
    assert uses(na, "Smp:1001", "a04", "calibrate", "fit") == ("held", "held")
    assert ("Smp:1001", "a04") not in [(i.scan, i.analyzer) for i in grp(na, "candidate_coverage").items]   # it left the comparison
    assert values.save_condition(w.root, "counts_accepted", ["Smp:1001:a04"], reason="checked").ok
    na = readiness.evaluate(w.root)
    assert "counts_unaccepted" not in kinds(na)
    assert uses(na, "Smp:1001", "a04", "calibrate") == ("pending",)
    w.calibrate("Smp:1001", setting="60x60", basis="inferred", key=w.key("Smp:1001", accepted=("a04",)))
    a04 = readiness.evaluate(w.root).scans["Smp:1001"].analyzers["a04"]
    assert (a04.uses["fit"].state, a04.uses["fit"].fallback) == ("fallback", readiness.COUNTS_ACCEPTED)
    assert values.save_condition(w.root, "counts_accepted", ["Smp:1001:a04"], declared=False).ok
    assert items(readiness.evaluate(w.root), "counts_unaccepted")[0][:2] == ("Smp:1001", "a04")


def test_lr_counts_are_never_released_by_accepting_them(tmp_path):
    w = World(tmp_path, [row(1001, T=302.0, counts={"a04": {**COUNTS, "family": "_lr"}})])
    assert values.save_condition(w.root, "counts_accepted", ["Smp:1001:a04"]).ok
    na = readiness.evaluate(w.root)
    g = grp(na, "not_raw")
    assert (g.title, g.action, g.can_start) == ("Counts not raw", "tell_operator", False)
    assert items(na, "not_raw") == [("Smp:1001", "a04", "non-raw counts (_lr) are not fitted")]
    assert "counts_unaccepted" not in kinds(na)


def test_r8_a_damaged_intake_line(tmp_path):
    w = r1(tmp_path)
    with open(w.root / store.ENTRIES, "ab") as f:
        f.write(b"[1, 2]\n")
    na = readiness.evaluate(w.root)
    g = grp(na, "damaged_lines")
    assert (g.summary, g.state, g.action, g.can_start, g.fallback_in_use) == (
        "Intake lines skipped: 1 line", "fallback", "tell_operator", True, True)
    assert [(i.file, i.reason, i.fallback) for i in g.items] == [
        (store.ENTRIES, inputs.damage_lines(w.root)[0], readiness.LINE_SKIPPED)]
    assert "intake/entries.jsonl line 2 skipped: " in g.items[0].reason
    assert kinds(na) == ["curve_missing", "damaged_lines", "temperature", "slit", "calibration_pending",
                         "candidate_coverage", "sidecar", "q_missing"]       # nothing else changes


# --- other gaps -------------------------------------------------------------------------------------------
def test_missing_files_unreadable_files_and_lines_not_read(tmp_path):
    left = {"a05": {"file": None, "cause": "no file"},
            "a06": {"file": "Smp_1001_a06", "cause": "holds no data rows"}}
    lines = [{"analyzer": "a03", "position": "trailer", "key": "Temp", "line": "#Temp=  416.000 K"}]
    w = World(tmp_path, [row(1001, T=302.0, left_out=left, lines=lines, exposure="missing")])
    w.files({"D:\\bt\\Bad_a03": {"class": "failed", "reason": "ValueError: no analyzer could be read", "set": "Bad", "seen_at": "t"},
             "D:\\bt\\x_sum": {"class": "ignored", "reason": "ignored by the BL43 format's ignore list", "set": None, "seen_at": "t"}})
    na = readiness.evaluate(w.root)
    assert items(na, "analyzer_file") == [("Smp:1001", "a05", "a05: no file")]
    assert items(na, "analyzer_unreadable") == [("Smp:1001", "a06", "a06: holds no data rows")]
    assert grp(na, "analyzer_unreadable").state == "unreadable"
    assert [(i.file, i.scan) for i in grp(na, "files_not_read").items] == [("D:\\bt\\Bad_a03", None)]
    assert uses(na, "Smp:1001", "a06", "read", "reduce") == ("unreadable", "held")
    assert uses(na, "Smp:1001", "a03", "read") == ("fallback",)
    assert [i.analyzer for i in grp(na, "unrecognised_line").items] == ["a03"]
    assert {i.analyzer for i in grp(na, "exposure_missing").items} == {"a03", "a04"}
    assert grp(na, "exposure_missing").items[0].fallback == readiness.NO_MONITOR
    # an analyzer with no file never holds its siblings
    assert na.scans["Smp:1001"].analyzers["a03"].can_start is True and na.scans["Smp:1001"].analyzers["a05"].can_start is False


def test_values_that_disagree_are_listed_and_marked(tmp_path):
    w = World(tmp_path, [row(1001, T=302.0)])
    assert values.save_value(w.root, "temperature", 100.0, ["Smp:1001"]).ok
    na = readiness.evaluate(w.root)
    g = grp(na, "disagreement")
    assert (g.state, g.action) == ("ready", "none")
    assert all(i.values_disagree for i in g.items) and all(i.values_disagree for i in grp(na, "slit").items)


def test_no_group_carries_a_fixed_can_start(tmp_path):
    """A scan whose analyzers are all held for a curve cannot start, whatever gap group lists it."""
    w = World(tmp_path, [row(1001, slit="40x40", sidecar="missing"), row(1002, T=302.0, slit="60x60")])
    na = readiness.evaluate(w.root)           # no 40x40 curve for either analyzer; 60x60 has a03 and a04
    assert na.scans["Smp:1001"].can_start is False and na.scans["Smp:1002"].can_start is True
    for kind in ("temperature", "sidecar"):                         # the scan-target groups listing only 1001
        g = grp(na, kind)
        assert [i.scan for i in g.items] == ["Smp:1001"] and g.can_start is False, kind
    assert grp(na, "calibration_pending").can_start is True        # 1002 can start
    assert [i.scan for i in grp(na, "calibration_pending").items] == ["Smp:1002"]
    # the other direction: once that scan has a curve, the same groups say it can start
    w.check(found=CURVES + (CurveFile("a03", "40x40", "f", "x"), CurveFile("a04", "40x40", "g", "y")))
    na = readiness.evaluate(w.root)
    assert grp(na, "temperature").can_start is True and grp(na, "sidecar").can_start is True


def test_a_failed_calibration_is_its_own_group(tmp_path):
    w = World(tmp_path, [row(1001, T=302.0, slit="60x60")])
    card, _ = w.ws.load()
    card.state.tabulated_calibration["Smp:1001"] = {"content_hash": "d1", "inputs_key": w.key("Smp:1001", declared="60x60"),
                                                    "library": "L1", "error": "ValueError: odd", "detectors": {}, "flags": []}
    card.save_state(w.ws.state_path)
    na = readiness.evaluate(w.root)
    g = grp(na, "calibration_failed")
    assert (g.action, g.can_start, [i.scan for i in g.items]) == ("tell_operator", False, ["Smp:1001"])
    assert "ValueError: odd" in g.items[0].reason


def test_evaluate_never_raises_and_says_why_it_did_not_evaluate(tmp_path):
    w = World(tmp_path, [row(1001, T=302.0)])
    (w.root / scans.INTAKE_SCANS).write_text("{not json", encoding="utf-8")
    na = readiness.evaluate(w.root)
    assert (na.evaluated, na.groups, dict(na.scans)) == (False, (), {}) and na.note
    assert needs_attention(w.root).evaluated is False
    empty = fx.workspace(tmp_path / "other")
    assert readiness.evaluate(empty.root).note == "nothing imported yet"
    assert readiness.evaluate(tmp_path / "nowhere").evaluated is False
    sp = fx.workspace(tmp_path / "s30")
    sp.policy_path.write_text(sp.policy_path.read_text(encoding="utf-8").replace('plugin = "sp8_bl43"', 'plugin = "tavi"'),
                              encoding="utf-8", newline="\n")
    assert readiness.evaluate(sp.root).note == "readiness is computed for BL43LXU workspaces only"


# --- the import summary -----------------------------------------------------------------------------------
def test_import_summary_example_1(tmp_path):
    w = r1(tmp_path)
    w.files({"D:\\bt\\set1\\CsV3Sb5_x_sum": {"class": "ignored", "reason": "ignored by the BL43 format's ignore list",
                                              "set": None, "seen_at": "t"}})
    s = import_summary(w.root)
    assert (s.n_scans, s.n_analyzers, s.record_problem) == (2, 4, None)
    (inc,) = s.incomplete_sets
    assert (inc.scan, inc.set, dict(inc.missing_analyzers), inc.sidecar) == ("Smp:1002", "Smp_1002", {}, "missing")
    assert inc.sidecar_reason
    assert [(f.path, f.kind, f.scan) for f in s.files_not_read] == [("D:\\bt\\set1\\CsV3Sb5_x_sum", "ignored", None)]
    assert dict(s.files_not_read_by_kind) == {"ignored": 1}
    c = s.curves
    assert (c.folder, c.source, c.found_beside, c.n_curves, c.analyzers, c.settings, c.problem, c.check_stale) == (
        w.path, "discovered", str(w.beside), 3, ("a03", "a04"), ("20x20", "60x60"), None, False)
    assert c.checked_at == "2026-11-03T09:12:44.120511+00:00" and c.other_folders == ()
    assert s.last_import_at == "2026-11-03T09:12:42+00:00"
    w.check(folder=str(w.beside / "elsewhere"))
    assert import_summary(w.root).curves.check_stale is True


def test_import_summary_lists_members_left_out_of_a_set(tmp_path):
    left = {"a05": {"file": None, "cause": "no file"},
            "a06": {"file": "Smp_1001_a06", "cause": "off the bin lattice (3 of 80 bins, step 0.2 meV)"}}
    w = World(tmp_path, [row(1001, left_out=left)])
    s = import_summary(w.root)
    assert dict(s.incomplete_sets[0].missing_analyzers) == {"a05": "no file", "a06": left["a06"]["cause"]}
    assert [(f.path, f.kind, f.scan, f.analyzer) for f in s.files_not_read] == [("Smp_1001_a06", "left_out", "Smp:1001", "a06")]


def test_import_summary_example_2_a_folder_holding_no_bl43_set(tmp_path):
    problem = "no resolution folder is saved, and no rf/ folder holding curves was found beside D:\\bt\\set3"
    w = World(tmp_path, [], folder=False)
    w.check(found=(), problem=problem, folder=None, source="none")
    w.files({"D:\\bt\\set3\\notes.txt": {"class": "unrecognised", "reason": "not recognised as BL43LXU data", "set": None,
                                          "seen_at": "t"}})
    s = import_summary(w.root)
    assert (s.n_scans, s.n_analyzers, s.incomplete_sets, s.last_import_at) == (0, 0, (), None)
    assert [(f.path, f.kind) for f in s.files_not_read] == [("D:\\bt\\set3\\notes.txt", "unrecognised")]
    assert (s.curves.folder, s.curves.source, s.curves.n_curves, s.curves.problem) == (None, "none", 0, problem)


def test_import_summary_without_a_record_or_check_and_with_a_malformed_one(tmp_path):
    w = World(tmp_path, [], folder=False)
    s = import_summary(w.root)
    assert (s.n_scans, s.record_problem, s.curves.checked_at, s.curves.n_curves) == (0, None, None, 0)
    assert (s.curves.folder, s.curves.source, s.curves.problem) == (str(w.root / "rf"), "card", None)   # the card's folder
    w.ws.policy_path.write_text(w.ws.policy_path.read_text(encoding="utf-8").replace('resolution_folder = "rf"', ''),
                                encoding="utf-8", newline="\n")
    s = import_summary(w.root)
    assert (s.curves.folder, s.curves.source, s.curves.problem) == (None, "none", "no resolution folder is saved")
    scans.record_import(w.root, [row(1001)])
    (w.root / scans.INTAKE_SCANS).write_text("[1]", encoding="utf-8", newline="\n")
    (w.root / curves.CURVE_CHECK).parent.mkdir(exist_ok=True)
    (w.root / curves.CURVE_CHECK).write_text("{bad", encoding="utf-8", newline="\n")
    s = import_summary(w.root)
    assert s.n_scans == 0 and s.record_problem and s.curves.checked_at is None and s.curves.problem
    assert import_summary(tmp_path / "nowhere").record_problem


def test_a_non_bl43_workspace_gives_zeros_and_no_curves(tmp_path):
    sp = fx.workspace(tmp_path)
    sp.policy_path.write_text(sp.policy_path.read_text(encoding="utf-8").replace('plugin = "sp8_bl43"', 'plugin = "tavi"'),
                              encoding="utf-8", newline="\n")
    s = import_summary(sp.root)
    assert (s.n_scans, s.curves, s.record_problem) == (0, None, None)


# --- the conditions read ----------------------------------------------------------------------------------
def test_conditions_rows(tmp_path):
    w = World(tmp_path, [row(1001, T=302.0, counts={"a04": COUNTS}), row(1002, T=302.0, counts={"a04": {**COUNTS, "level": "notice"}})])
    ws = Workspace.open(w.root)
    assert ws.conditions() == []
    assert values.save_condition(w.root, "dead", ["Smp:1001:a03"], reason="no counts").ok
    dead_id = store.read(w.root, store.ENTRIES)[-1]["id"]
    ws.refresh()
    (row_dead,) = ws.conditions(["Smp:1001"])
    assert (row_dead.scan, row_dead.analyzer, row_dead.condition, row_dead.entry, row_dead.user_reason, row_dead.lifts_hold) == (
        "Smp:1001", "a03", "dead", dead_id, "no counts", None)
    assert values.save_condition(w.root, "dead", ["Smp:1001:a03"], declared=False).ok
    ws.refresh()
    assert ws.conditions() == []
    assert values.save_condition(w.root, "counts_accepted", ["Smp:1001:a04", "Smp:1002:a04"],
                                 reason="no background subtracted").ok
    assert values.save_condition(w.root, "excluded", ["Smp:1001"]).ok
    ws.refresh()
    got = [(c.scan, c.analyzer, c.condition, c.user_reason, c.lifts_hold) for c in ws.conditions()]
    assert got == [("Smp:1001", None, "excluded", None, None),
                   ("Smp:1001", "a04", "counts_accepted", "no background subtracted", True),
                   ("Smp:1002", "a04", "counts_accepted", "no background subtracted", False)]    # notice: changes nothing
    assert [c.scan for c in ws.conditions(["Smp:1002", "nope"])] == ["Smp:1002"]


# --- parity with the stages -------------------------------------------------------------------------------
def _ingest(ws, data):
    card, session = ws.load()
    ingest.ingest_batch(card, session, [data], workspace_root=ws.root)
    ws.save(card, session)


def _no_fit(monkeypatch):
    """Stands in for the fit: resolves the unit's calibration as the real gate does and returns unfitted."""
    def worker(rec, det, q, T, sample, scan_id, card, g, runcal, exclusions, chash, ghash, **_kw):
        cal = calibrations.resolve(rec, det, card, runcal)
        return ScanEntry(scan_id=scan_id, scan_number=rec.scan_number, scan_type=rec.scan_type,
                         status="held_for_calibration" if cal.held else "non_result", content_hash=chash,
                         guidelines_hash=ghash, reasons=[cal.held_reason] if cal.held else ["controlled worker"],
                         provenance=cal.provenance())
    monkeypatch.setattr(scan_delivery, "_process_one", worker)


def _run(ws, data):
    return driver.run_batch(ws, [data], M=10, scan_M=10, errors_tier="fast", workers=1, formats=("markdown",))


def test_acceptance_an_imported_scan_with_no_calibration_can_start(tmp_path):
    ws, data = fx.bl43_workspace(tmp_path)
    _ingest(ws, data)
    na = needs_attention(ws.root)
    assert na.evaluated and len(na.scans) == 2
    g = grp(na, "calibration_pending")
    assert (g.action, g.can_start, len(g.items)) == ("start_analysis", True, 2)
    for s in na.scans.values():
        assert (s.can_start, s.action) == (True, "start_analysis")
        assert all(a.uses["calibrate"].state == "pending" and a.can_start for a in s.analyzers.values())
    assert grp(na, "curve_missing") is None


def test_curve_parity_readiness_predicts_exactly_what_the_calibration_holds(tmp_path):
    skip = [("a04", "40x40"), ("a05", "40x40")]
    ws, data = fx.bl43_workspace(tmp_path, slit=(40, 40), library_skip=skip)
    _ingest(ws, data)
    predicted = {(i.scan, i.analyzer) for i in grp(needs_attention(ws.root), "curve_missing").items}
    card, _s = ws.load()
    calibrations.refresh_tabulated(card, fx.records_for(ws, data), base_dir=ws.root)
    held = {(scan, a) for scan, e in card.state.tabulated_calibration.items() for a, d in e["detectors"].items()
            if d.get("held_reason")}
    assert held and predicted == held
    card.save_state(ws.state_path)
    after = needs_attention(ws.root)             # calibrated from the same inputs: current, and the same holds
    assert {(i.scan, i.analyzer) for i in grp(after, "curve_missing").items} == held
    assert grp(after, "calibration_pending") is None


def test_key_parity_a_run_leaves_no_scan_pending_and_a_slit_save_does(tmp_path, monkeypatch):
    ws, data = fx.bl43_workspace(tmp_path, slit=(40, 40))             # a declared slit is part of the key
    _no_fit(monkeypatch)
    _run(ws, data)
    card, _s = ws.load()
    assert len(card.state.tabulated_calibration) == 2 and all(e["inputs_key"] for e in card.state.tabulated_calibration.values())
    na = needs_attention(ws.root)
    assert grp(na, "calibration_pending") is None
    assert all(a.uses["calibrate"].state in ("ready", "fallback") for s in na.scans.values() for a in s.analyzers.values())
    first = sorted(na.scans)[0]
    assert values.save_value(ws.root, "temperature", 120.0, [first]).ok           # not a calibration input
    assert grp(needs_attention(ws.root), "calibration_pending") is None
    assert values.save_value(ws.root, "slit", "60x60", [first]).ok                # a calibration input
    assert [i.scan for i in grp(needs_attention(ws.root), "calibration_pending").items] == [first]
    assert values.save_condition(ws.root, "dead", [f"{first}:a05"]).ok            # so is the set of analyzers
    assert uses(needs_attention(ws.root), first, "a05", "calibrate") == ("held",)


def test_count_hold_parity_with_the_fit_stage(tmp_path, monkeypatch):
    ws, data = fx.bl43_workspace(tmp_path, stems=("A",), off_integer=("a04",))
    _no_fit(monkeypatch)
    _run(ws, data)
    (scan,) = needs_attention(ws.root).scans
    unit = f"{scan}:a04"

    def stage_holds():
        _card, session = ws.load()
        e = session.get(unit)
        return e.status == "held_for_calibration" and "counts may not be raw" in " ".join(e.reasons)

    def readiness_holds():
        g = grp(needs_attention(ws.root), "counts_unaccepted")
        return g is not None and any(i.analyzer == "a04" for i in g.items)
    assert stage_holds() and readiness_holds()
    assert values.save_condition(ws.root, "counts_accepted", [unit], reason="checked").ok
    assert not readiness_holds()
    _run(ws, data)
    assert not stage_holds() and not readiness_holds()
    assert grp(needs_attention(ws.root), "calibration_pending") is None     # the accepted counts are in the stored key
    assert values.save_condition(ws.root, "counts_accepted", [unit], declared=False).ok
    assert readiness_holds()
    _run(ws, data)
    assert stage_holds() and readiness_holds()


def test_marks_come_from_the_fit_inputs_read(tmp_path, monkeypatch):
    ws, data = fx.bl43_workspace(tmp_path, stems=("A",))
    _no_fit(monkeypatch)
    _run(ws, data)
    (scan,) = needs_attention(ws.root).scans
    assert grp(needs_attention(ws.root), "values_changed") is None
    assert values.save_value(ws.root, "temperature", 120.0, [scan]).ok
    g = grp(needs_attention(ws.root), "values_changed")
    assert g is not None and g.items[0].values_changed[0].startswith("temperature: 100.0")


# --- repair batch 1 ---------------------------------------------------------------------------------------
def test_a_declared_target_has_no_values_changed_item(tmp_path):
    w = r1(tmp_path)
    assert values.save_condition(w.root, "dead", ["Smp:1002:a04"], reason="no counts").ok
    assert values.save_condition(w.root, "excluded", ["Smp:1001"]).ok
    marks = {"Smp:1002:a04": UnitMarks(changed_fast="dead"), "Smp:1001:a03": UnitMarks(changed_fast="excluded"),
             "Smp:1002:a03": UnitMarks(changed_fast="live")}
    na = readiness.evaluate(w.root, marks=marks)
    assert [(i.scan, i.analyzer, i.values_changed) for i in grp(na, "values_changed").items] == [("Smp:1002", "a03", ("live",))]
    assert na.scans["Smp:1001"].analyzers["a03"].action == "reverse_declaration"
    assert na.scans["Smp:1002"].analyzers["a04"].action == "reverse_declaration"
    assert na.scans["Smp:1002"].analyzers["a04"].values_changed == ()


def test_a_malformed_curve_check_is_reported_and_a_changed_folder_is_still_pending(tmp_path):
    w = r1(tmp_path)
    w.check(folder=str(w.beside / "elsewhere"))                      # a folder saved since the last check: pending
    na = readiness.evaluate(w.root)
    assert "resolution_folder" not in kinds(na)
    assert {i.reason for i in grp(na, "calibration_pending").items} == {readiness.FOLDER_CHANGED}
    (w.root / curves.CURVE_CHECK).write_text("{bad", encoding="utf-8", newline="\n")      # an unreadable check: reported
    _, error = readiness._check(w.root)
    assert error
    na = readiness.evaluate(w.root)
    assert [i.reason for i in grp(na, "resolution_folder").items] == [error]
    assert "calibration_pending" not in kinds(na)
    assert uses(na, "Smp:1001", "a03", "calibrate") == ("held",)


def test_import_summary_reports_a_folder_saved_since_the_last_check_as_pending(tmp_path):
    w = r1(tmp_path)
    w.check(folder=str(w.beside / "elsewhere"), found=(), problem="the old folder is missing")
    c = import_summary(w.root).curves
    assert (c.folder, c.check_stale, c.problem, c.n_curves, c.analyzers, c.settings, c.checked_at, c.other_folders) == (
        w.path, True, None, 0, (), (), None, ())
    w.check(folder=str(w.beside / "elsewhere"))                      # the old check found curves: not this folder's
    assert import_summary(w.root).curves.n_curves == 0
    w.check()                                                         # checked: the figures come back
    c = import_summary(w.root).curves
    assert (c.check_stale, c.n_curves, c.checked_at is None) == (False, 3, False)


def test_analyzers_with_no_file_do_not_hold_the_scan_but_unreadable_ones_do(tmp_path):
    none = {"a05": {"file": None, "cause": "no file"}}
    bad = {"a06": {"file": "Smp_1002_a06", "cause": "off the bin lattice (3 of 80 bins, step 0.2 meV)"}}
    w = World(tmp_path, [row(1001, T=302.0, slit="60x60", left_out=none), row(1002, T=302.0, slit="60x60", left_out=bad)])
    for s in w.rows:
        w.calibrate(s, setting="60x60", basis="declared", declared="60x60")
    na = readiness.evaluate(w.root)
    ok = na.scans["Smp:1001"]
    assert [ok.uses[u].state for u in ("read", "reduce", "calibrate", "fit")] == ["ready"] * 4
    assert ok.analyzers["a05"].uses["calibrate"].state == "held"
    assert [i.analyzer for i in grp(na, "analyzer_file").items] == ["a05"]
    held = na.scans["Smp:1002"]
    assert (held.uses["read"].state, held.uses["calibrate"].state) == ("unreadable", "held")
    assert [i.analyzer for i in grp(na, "analyzer_unreadable").items] == ["a06"]


def test_status_says_why_the_list_is_missing(tmp_path, capsys):
    from isar.cli.main import main
    w = World(tmp_path, [row(1001, T=302.0)])
    (w.root / scans.INTAKE_SCANS).write_text("{not json", encoding="utf-8", newline="\n")
    assert main(["status", "--workspace", str(w.root)]) == 0
    assert "Needs attention: not evaluated (" in capsys.readouterr().out
    sp = fx.workspace(tmp_path / "s30")                               # not a BL43LXU workspace: no line at all
    sp.policy_path.write_text(sp.policy_path.read_text(encoding="utf-8").replace('plugin = "sp8_bl43"', 'plugin = "tavi"'),
                              encoding="utf-8", newline="\n")
    assert main(["status", "--workspace", str(sp.root)]) == 0
    assert "Needs attention" not in capsys.readouterr().out


# --- surfaces ---------------------------------------------------------------------------------------------
def test_status_prints_the_needs_attention_list(tmp_path, capsys):
    from isar.cli.main import main
    w = r1(tmp_path)
    assert main(["status", "--workspace", str(w.root)]) == 0
    out = capsys.readouterr().out
    assert "Needs attention:" in out
    assert "Resolution curves: analyzer a04 on 1 scan [locate_folder; analysis cannot start]" in out
    assert f"Smp:1002:a04: {NO_CURVE_20}" in out


def test_status_lists_only_what_is_left_once_calibrated(tmp_path, capsys):
    from isar.cli.main import main
    w = World(tmp_path, [row(1001, T=302.0, slit="60x60")])
    w.calibrate("Smp:1001", setting="60x60", basis="declared", declared="60x60")
    assert main(["status", "--workspace", str(w.root)]) == 0
    out = capsys.readouterr().out
    assert "Needs attention:" in out and "Q (for steering)" in out and "Calibration pending" not in out


def test_the_readiness_read_stays_scipy_and_qt_free(tmp_path):
    w = r1(tmp_path)
    script = (
        "import sys\n"
        "from isar.workspace_api import needs_attention, import_summary\n"
        f"na = needs_attention(r'{w.root}'); s = import_summary(r'{w.root}')\n"
        "assert na.evaluated and s.n_scans == 2\n"
        "bad = [m for m in ('scipy', 'PySide6', 'pyqtgraph') if m in sys.modules]\n"
        "assert not bad, bad\nprint('OK')\n")
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                         cwd=Path(__file__).resolve().parents[2])
    assert out.returncode == 0 and "OK" in out.stdout, out.stderr


# --- repair batch 2 ---------------------------------------------------------------------------------------
def _truncate(path):
    """The file as the beamline leaves it while it is still being written: a label and no data rows."""
    path.write_text("#      meV      secs   npts          norm_i             y_i            ye_i\n",
                    encoding="utf-8", newline="\n")


def test_a_file_that_failed_at_re_import_holds_its_analyzer_whatever_its_old_row_says(tmp_path):
    ws, data = fx.bl43_workspace(tmp_path, stems=("A",), analyzers=("a03",))
    _ingest(ws, data)
    (scan,) = needs_attention(ws.root).scans
    assert needs_attention(ws.root).scans[scan].can_start is True
    _truncate(data / f"{fx.STEM}_A_a03")
    _ingest(ws, data)
    na = needs_attention(ws.root)
    s = na.scans[scan]
    assert (s.can_start, s.action, s.analyzers["a03"].can_start) == (False, "import_again", False)
    assert (s.uses["read"].state, s.uses["calibrate"].state) == ("unreadable", "held")
    g = grp(na, "analyzer_unreadable")
    assert (g.action, g.can_start, [(i.scan, i.analyzer) for i in g.items]) == ("import_again", False, [(scan, "a03")])
    fx.write_scan(data, stem=f"{fx.STEM}_A", analyzers=("a03",), energy=fx.ENERGY_120)     # the file is complete now
    _ingest(ws, data)
    na = needs_attention(ws.root)
    assert (na.scans[scan].can_start, grp(na, "analyzer_unreadable"), grp(na, "files_not_read")) == (True, None, None)


def test_only_the_analyzer_whose_file_failed_is_held(tmp_path):
    w = r1(tmp_path)
    w.files({str(w.root / "data" / "Smp_1001_a03"): {"class": "failed", "reason": "ValueError: odd", "set": "Smp_1001",
                                                       "seen_at": "t"}})
    na = readiness.evaluate(w.root)
    a03, a04 = na.scans["Smp:1001"].analyzers["a03"], na.scans["Smp:1001"].analyzers["a04"]
    assert (a03.can_start, a03.action, a04.action) == (False, "import_again", "start_analysis")
    assert [(i.scan, i.analyzer) for i in grp(na, "analyzer_unreadable").items] == [("Smp:1001", "a03")]
    assert "ValueError: odd" in grp(na, "analyzer_unreadable").items[0].reason
    assert na.scans["Smp:1002"].analyzers["a03"].can_start is True
