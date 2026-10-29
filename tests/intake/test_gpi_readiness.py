"""Readiness and the intake views with gpi files in force (C-gpi-summary, C-effective-gpi's read side).

The state is built the way a run builds it: generated BL43 files with `#Q=` trailers are ingested (the real
import record), then `check_gpi` judges the generated `gpi.<set>` macro against it. No run is needed; every
number is generated here (hard rule 10)."""
from __future__ import annotations

import intake_fixtures as fx
import pytest
from isar.intake import curves, inputs, readiness, scans, settings, values
from isar.intake.gpi import check_gpi, entries_in_force, read_gpi_check
from isar.session import ingest
from isar.workspace_api import Workspace, import_summary, needs_attention

SET = "Smp_100K_0p5_0p5_3_A"
Q = {"a03": 10.00, "a04": 11.46, "a05": 12.92}
HKL = {"a03": (0.5, 0.5, 3.0), "a04": (0.5, 0.5, 3.5), "a05": (0.5, 0.5, 4.0)}
NAMED = "sarf_z_s1_40_40"


class Exp:
    """One generated scan set, its `sarf_z` curves for a03 and a04 only, and (optionally) its gpi file."""

    def __init__(self, tmp_path, *, gpi=True, hkl=HKL, gpi_q=Q):
        self.ws, self.data = fx.bl43_workspace(tmp_path, stems=("A",))
        self.root = self.ws.root
        fx.write_library(self.root / "rf", {"40x40": 1.3}, ("a03", "a04"), prefix="sarf_z")
        self.scan_files(Q)
        if gpi:
            fx.write_gpi(self.data, SET, q_nm=gpi_q, hkl=hkl)
        self.ingest()
        self.key = next(iter(scans.read_scans(self.root)))

    def scan_files(self, q_nm):
        fx.write_scan(self.data, stem=SET, analyzers=fx.ANALYZERS, seed=1, energy=fx.ENERGY_120, q_nm=q_nm)

    def ingest(self):
        card, session = self.ws.load()
        ingest.ingest_batch(card, session, [self.data], workspace_root=self.root)
        self.ws.save(card, session)

    def check(self, by="import"):
        card, _session = self.ws.load()
        return check_gpi(self.root, card, [self.data], by=by)

    def na(self):
        return needs_attention(self.root)


def grp(na, kind):
    return next((g for g in na.groups if g.kind == kind), None)


def q_missing(na):
    g = grp(na, "q_missing")
    return [] if g is None else sorted(i.analyzer for i in g.items)


@pytest.fixture
def exp(tmp_path):
    return Exp(tmp_path)


def test_import_root_is_the_worktree():
    from pathlib import Path

    import isar
    assert Path(isar.__file__).resolve().parents[1] == Path(__file__).resolve().parents[2]


def test_without_a_check_nothing_changes_and_q_is_missing_everywhere(exp):
    assert q_missing(exp.na()) == ["a03", "a04", "a05"]
    assert grp(exp.na(), "gpi_rejected") is None and grp(exp.na(), "gpi_folder_changed") is None


def test_an_accepted_file_clears_q_missing_and_holds_only_the_analyzer_without_its_named_curve(exp):
    assert exp.check().scans[exp.key].accepted
    na = exp.na()
    assert q_missing(na) == [] and grp(na, "gpi_rejected") is None
    held = grp(na, "curve_missing")                       # sarf_z has no a05 curve; the other set has one, not used
    assert [(i.analyzer, NAMED + "_a05" in i.reason) for i in held.items] == [("a05", True)]
    steer = {a: s.uses["steer"].state for a, s in na.scans[exp.key].analyzers.items()}
    assert set(steer.values()) == {"ready"}


def test_one_rejected_file_is_one_item_and_nothing_from_it_is_taken(tmp_path):
    e = Exp(tmp_path, gpi_q={**Q, "a04": 5.0})
    assert not e.check().scans[e.key].accepted
    na = e.na()
    g = grp(na, "gpi_rejected")
    assert len(g.items) == 1 and g.items[0].file.endswith(f"gpi.{SET}") and "a04" in g.items[0].reason
    assert q_missing(na) == ["a03", "a04", "a05"]
    assert grp(na, "curve_missing") is None                 # the named set is not in force: ordinary slit matching
    fx.write_gpi(e.data, SET, q_nm=Q, hkl=HKL)               # the file is corrected and re-read
    e.check()
    na = e.na()
    assert grp(na, "gpi_rejected") is None and q_missing(na) == []


def test_one_analyzers_data_q_moving_withdraws_everything_from_an_unchanged_file(exp):
    exp.check()
    assert q_missing(exp.na()) == []
    exp.scan_files({**Q, "a04": 11.90})                       # only the data moved, past 0.15 /nm
    exp.ingest()
    exp.check(by="run")
    na = exp.na()
    assert len(grp(na, "gpi_rejected").items) == 1 and q_missing(na) == ["a03", "a04", "a05"]


def test_a_removed_file_withdraws_its_values_and_a_restored_one_returns_them(exp):
    exp.check()
    path = exp.data / "gpi_files" / f"gpi.{SET}"
    text = path.read_bytes()
    path.unlink()
    exp.check()
    na = exp.na()
    assert q_missing(na) == ["a03", "a04", "a05"] and grp(na, "gpi_rejected") is None
    path.write_bytes(text)
    exp.check()
    assert q_missing(exp.na()) == []


def test_one_hkl_triplet_removed_leaves_that_analyzer_without_q_and_uses_the_rest(tmp_path):
    e = Exp(tmp_path, hkl={**HKL, "a04": None})
    assert e.check().scans[e.key].accepted
    assert q_missing(e.na()) == ["a04"]


def test_a_user_slit_that_differs_from_the_named_set_drops_it_with_a_note(exp):
    exp.check()
    assert values.save_value(exp.root, "slit", "60x60", [exp.key]).ok
    na = exp.na()
    note = grp(na, "gpi_curves_dropped")
    assert len(note.items) == 1 and "60x60" in note.items[0].reason and note.state == "ready"
    assert grp(na, "curve_missing") is None                  # a05 follows the user's slit, not the dropped set
    assert q_missing(na) == []                               # the Q from the file is still taken
    assert values.save_value(exp.root, "slit", "40x40", [exp.key]).ok
    na = exp.na()
    assert grp(na, "gpi_curves_dropped") is None and [i.analyzer for i in grp(na, "curve_missing").items] == ["a05"]


def test_no_gpi_folder_is_what_it_was(tmp_path):
    e = Exp(tmp_path, gpi=False)
    before = e.na()
    check = e.check()
    assert check.folder is None and check.scans == {}
    after = e.na()
    assert after.groups == before.groups and after.scans == before.scans
    assert not any(g.kind.startswith("gpi_") for g in after.groups)


def test_a_folder_chosen_since_the_check_withdraws_the_values_and_says_so(exp, tmp_path):
    exp.check()
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert settings.save_gpi_folder(exp.root, other).ok
    assert entries_in_force(exp.root) == {}
    na = exp.na()
    g = grp(na, "gpi_folder_changed")
    assert g is not None and g.state == "fallback" and q_missing(na) == ["a03", "a04", "a05"]
    exp.check(by="run")                                      # the next check judges the new folder
    assert grp(exp.na(), "gpi_folder_changed") is None


def test_the_calibration_pending_judgement_uses_the_run_s_key(exp):
    """A calibration stored under the key the run computes (named set included) stands; any other is pending."""
    exp.check()
    row = scans.read_scans(exp.root)[exp.key]
    si = inputs.scan_inputs(row, inputs.index_entries([], lines=[]), entries_in_force(exp.root)[exp.key])
    cc = curves.read_curve_check(exp.root)
    pattern = cc.pattern

    def store(key):
        card, _session = exp.ws.load()
        card.state.tabulated_calibration[exp.key] = {
            "content_hash": row["data_revision"], "inputs_key": key, "library": cc.library, "folder": cc.folder,
            "pattern": pattern, "declared": "40x40", "setting": "40x40", "setting_source": "declared",
            "setting_basis": "declared",
            "detectors": {a: {"zero": 0.1, "zero_source": "elastic_fit"} for a in ("a03", "a04")}}
        card.save_state(exp.ws.state_path)

    def key(named):
        return inputs.calibration_inputs_key(
            data_revision=row["data_revision"], folder=cc.folder, pattern=pattern, library=cc.library,
            declared_slit=inputs.calibration_slit(si), analyzers=sorted(row["analyzers"]), counts_accepted=[],
            named=named)

    named = inputs.named_key(si.named, si.gpi.curves)
    assert named is not None and inputs.calibration_slit(si) == "40x40"
    store(key(None))                                          # a calibration made without the named set
    assert grp(exp.na(), "calibration_pending") is not None
    store(key(named))
    assert grp(exp.na(), "calibration_pending") is None


def test_the_import_summary_carries_the_check_and_reads_a_broken_one_as_none(exp):
    assert import_summary(exp.root).gpi is None
    check = exp.check()
    assert import_summary(exp.root).gpi == check == read_gpi_check(exp.root)
    (exp.root / "report" / "gpi.json").write_text("{not json", encoding="utf-8", newline="\n")
    assert import_summary(exp.root).gpi is None and entries_in_force(exp.root) == {}


def test_the_workspace_reads_the_check_and_the_q_row_comes_from_the_gpi(exp):
    ws = Workspace(exp.root)
    assert ws.gpi_check is None
    exp.check()
    ws.refresh()
    assert ws.gpi_check.scans[exp.key].accepted
    q = {r.analyzer: r for r in ws.fact_rows() if r.fact == "q"}
    assert q["a03"].used_value == [0.5, 0.5, 3.0] and q["a03"].used_source == "gpi"
    slit = next(r for r in ws.fact_rows() if r.fact == "slit")
    assert (slit.used_value, slit.used_source) == ("40x40", "gpi") and slit.fallback is None


def test_the_gpi_check_is_a_watched_artifact():
    from isar.workspace_api import watch
    assert watch._WATCHED["gpi"] == ("report", "gpi.json")


def test_readiness_kinds_carry_the_fixed_names_targets_states_and_actions():
    k = readiness.KINDS
    assert (k["gpi_rejected"].target, k["gpi_rejected"].state, k["gpi_rejected"].action) == ("scan", "fallback", "import_again")
    assert (k["gpi_curves_dropped"].target, k["gpi_curves_dropped"].state, k["gpi_curves_dropped"].action) == ("scan", "ready", "none")
    assert (k["gpi_folder_changed"].target, k["gpi_folder_changed"].state, k["gpi_folder_changed"].action) == ("experiment", "fallback", "import_again")
