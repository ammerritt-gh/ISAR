"""The saved mode links as the read facade shows them (C-link-view): the worked examples of the connection, run
against the real session linking and the real link writer, on a workspace generated at test time (hard rule 10).

Prevents: a link drawn as current after the fit it names has changed (a pick, a re-run fast pass, a spectrum the
record left out that now has a fit), a link reported stale when nothing changed (every way a spectrum can be left
out of a record), the newer record of a scan not winning over an older one, a split candidate whose two names are
lost, and a mode's energy or name read against the wrong mode index.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from inter_fixtures import build_inter_workspace, scan_keys, spectrum_keys
from isar.analyze.coupled import linking as core
from isar.analyze.prior import PRIOR_RECIPE_VERSION
from isar.intake import gpi, inputs, inter_values, scans as intake_scans, store
from isar.intake.inter_spectra import read_user
from isar.schema.card import load_state
from isar.session import writer
from isar.session.inter_spectra import linking, records
from isar.session.workspace import Workspace
from isar.workspace_api import ChangeView, LinkView, TieView, fits_in_use, link_view
from isar.workspace_api.inter_spectra import LINK_WORDS

B1 = "b000000000001"
F1 = "f00000000a001"                       # a refit the batch below writes; "fast" is the solo fit


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    """Two temperatures (two scans) of one q setting, three analyzers each; each analyzer's fast record has two modes."""
    root = build_inter_workspace(tmp_path_factory.mktemp("base"), temperatures=(100, 200), setups=("0p5_0p5_3",),
                                 n_analyzers=3)
    for unit in spectrum_keys(root):
        _plain_fast(root, unit)
    return root


@pytest.fixture
def root(base, tmp_path):
    return Path(shutil.copytree(base, tmp_path / "ws"))


def _units(root, scan: str) -> list[str]:
    return [u for u in spectrum_keys(root) if u.rsplit(":", 1)[0] == scan]


def _plain_fast(root, unit, *, omega0=(4.0, 8.0), version=PRIOR_RECIPE_VERSION, resolution=True):
    """Rewrite the unit's stored fast record with plain modes, the resolution and the recipe a link reads."""
    ws = Workspace.open(root)
    card, session = ws.load()
    entry = session.scans[unit]
    det = unit.rsplit(":", 1)[1]
    peaks = [{"index": i, "omega0": o, "Gamma": 0.2, "height": 5.0, "area_stokes": 10.0, "sigma_area": 1.0,
              "sigma_omega0": 0.05, "sigma_Gamma": 0.02, "regime": "underdamped",
              "mode_status": "determined_by_own_data", "at_width_cap": False, "confident": True}
             for i, o in enumerate(omega0)]
    record = {**entry.detectors[det]["fast_pass"], "peaks": peaks,
              "flag_statistics": {"fwhm": 0.4} if resolution else {},
              "prior_provenance": {"version": version, "energy_support_meV": [0.5, 12.0]}}
    entry.detectors = {det: {**entry.detectors[det], "fast_pass": record}}
    ws.save(card, session)


def _save(root, scans) -> str:
    """Link the fits in use of ``scans`` and save the result through the real writer; returns the link id."""
    return writer.save_links(root, linking.link_scans(root, scans), scope=list(scans), batch=None, stage=None)


def _now(root, units):
    read = store.read_all(root, store.ENTRIES)
    return records.inputs_now_from(intake_scans.read_scans(root), inputs.index_entries(read.entries, lines=read.lines),
                                   load_state(root / "card.state.json").tabulated_calibration, units,
                                   gpi=gpi.entries_in_force(root), data_dir=root / "data")


def _batch_refit(root, unit: str, fit: str) -> None:
    """One accepted refit of ``unit`` through the real batch writer, fitted on the inputs now (so it is judged current)."""
    def payload(event, body):
        return {"schema": records.SCHEMA_SAVE, "unit": f"inter:{B1}:batch", "pass": "inter", "batch": B1,
                "event": event, event: body}
    start = {"batch_kind": "anchored", "scope": {"direction": "q", "members": [unit]}, "anchors": [], "rings": {},
             "seams": [], "settings": {"version": 1, "values": {}}, "preview": "t", "fixed": {}, "unreached": {},
             "terms": [], "metric": {}, "core": "t", "inputs_at": 0, "intake_seq": read_user(root).max_seq, "epochs": {}}
    refit = {"fit": fit, "unit": unit, "status": "accepted", "reason": None, "seed": "fast",
             "inputs": _now(root, [unit])[unit], "conditioned_on": [], "flag": None, "why_changed": None,
             "payload": {"came_in": {}, "came_out": {"errors": "conditional"},
                         "overlay": {"n_modes": 1, "omega0": 4.0, "best_label": "1 mode", "interval": [3.9, 4.1],
                                     "modes": [{"omega0": 4.0, "omega0_err": 0.1, "gamma": 0.2, "amplitude": 10.0}]}}}
    stage = {"stage": "ring", "index": 1, "round": None, "refits": [refit], "seams": [],
             "progress": {"done": 1, "failed": 0, "not_reached": 0, "fixed": 0, "total": 1}, "not_reached": {}}
    out = writer.save_inter(root, [payload("start", start), payload("stage", stage)])
    assert all(isinstance(v, dict) for v in out), out
    assert records.read_log(root).refits[0].accept.decision == "authoritative"


def _recalibrate(root, unit: str) -> None:
    """A new calibration zero for the unit's scan: the inputs hash now differs from the one a link stored."""
    scan, det = unit.rsplit(":", 1)
    path = Path(root) / "card.state.json"
    d = json.loads(path.read_text(encoding="utf-8"))
    d.setdefault("tabulated_calibration", {})[scan] = {"setting": "s1", "setting_basis": "declared",
                                                        "detectors": {det: {"zero": 0.25, "zero_source": "test"}}}
    path.write_text(json.dumps(d), encoding="utf-8", newline="\n")


def _leave_out(kind: str, root, unit: str) -> None:
    """Make ``unit`` one of the ways a link leaves a spectrum out (the session's own reasons)."""
    if kind == "not fitted yet":
        ws = Workspace.open(root)
        card, session = ws.load()
        session.scans[unit].detectors, session.scans[unit].status = {}, "ingested"
        ws.save(card, session)
    elif kind == "older recipe":
        _plain_fast(root, unit, version="isar.mode-prior-recipe/6")
    elif kind == "inputs changed since the fast pass":
        ws = Workspace.open(root)
        card, session = ws.load()
        entry = session.scans[unit]
        entry.provenance = {**entry.provenance, "fit_inputs": {**entry.provenance["fit_inputs"], "hash": "older-inputs"}}
        ws.save(card, session)
    elif kind == "no resolution recorded":
        _plain_fast(root, unit, resolution=False)
    elif kind == "locked fit out of date":
        _batch_refit(root, unit, F1)
        _recalibrate(root, unit)                           # the refit's inputs are no longer the ones now
        assert inter_values.set_lock(root, unit, locked=True, fit_id=F1).ok
    else:
        raise AssertionError(kind)


def test_no_saved_link_gives_the_empty_view_and_the_solo_fits_in_use(root):
    units = spectrum_keys(root)
    assert link_view(root) == LinkView("", {}, {})
    assert link_view(root, units) == LinkView("", {}, {})
    assert fits_in_use(root, units) == {u: "fast" for u in units}


def test_a_fresh_record_names_every_mode_of_every_spectrum_from_the_stored_result(root):
    scans = scan_keys(root)
    link = _save(root, scans)
    (line,) = records.read_links(root)
    result = records.read_link_payload(root, line)
    names = {b.id: b.name for b in result.branches}
    view = link_view(root)
    assert view.revision and set(view.by_unit) == set(spectrum_keys(root))
    for unit, ul in view.by_unit.items():
        assert (ul.link_id, ul.current, ul.stale, ul.stale_text, ul.fit_id) == (link, True, False, None, "fast")
        assert ul.names == tuple(names[b] if b else None for b in result.labels[unit])
        assert ul.peak_E == result.peak_E[unit] and ul.settled == result.settled[unit]
        assert len(set(ul.names)) == 2 and None not in ul.names        # the two modes of every spectrum are linked
    assert {b.name: b.members for b in result.branches} == {v.name: v.members for v in view.branches.values()}
    assert view.revision == link_view(root).revision                   # a read is repeatable


def test_a_pick_of_a_refit_makes_the_record_stale_and_the_refit_has_no_names(root):
    s1, _ = scan_keys(root)
    unit, mate = _units(root, s1)[0], _units(root, s1)[1]
    _batch_refit(root, unit, F1)
    assert inter_values.pick_fit(root, unit, "fast").ok
    _save(root, [s1])
    before = link_view(root)
    assert not any(ul.stale for ul in before.by_unit.values())
    assert before.by_unit[unit].current and before.by_unit[unit].fit_id == "fast"
    assert inter_values.pick_fit(root, unit, F1).ok
    after = link_view(root)
    assert after.revision != before.revision
    assert fits_in_use(root, [unit, mate]) == {unit: F1, mate: "fast"}
    assert after.by_unit[unit].stale and not after.by_unit[unit].current
    assert (after.by_unit[unit].names, after.by_unit[unit].markers, after.by_unit[unit].stale_text) == (
        (), (), LINK_WORDS["stale"])
    assert after.by_unit[mate].stale and after.by_unit[mate].current and after.by_unit[mate].names


def test_a_re_run_fast_pass_with_the_same_id_and_inputs_makes_the_record_stale(root):
    s1, _ = scan_keys(root)
    unit, mate = _units(root, s1)[:2]
    _save(root, [s1])
    _plain_fast(root, unit, omega0=(4.0, 8.5))            # the id stays "fast", the inputs hash and the modes' stamp do not
    view = link_view(root)
    assert view.by_unit[unit].stale and not view.by_unit[unit].current and view.by_unit[unit].names == ()
    assert view.by_unit[mate].stale and view.by_unit[mate].current


@pytest.mark.parametrize("kind", ["not fitted yet", "older recipe", "inputs changed since the fast pass",
                                  "no resolution recorded", "locked fit out of date"])
def test_a_spectrum_a_record_leaves_out_does_not_make_it_stale(root, kind):
    s1, _ = scan_keys(root)
    unit = _units(root, s1)[0]
    _leave_out(kind, root, unit)
    _save(root, [s1])
    view = link_view(root)
    assert view.by_unit[unit].current is False
    assert not any(ul.stale for ul in view.by_unit.values()), kind


def test_a_left_out_spectrum_that_gains_a_fit_makes_the_record_stale(root):
    s1, _ = scan_keys(root)
    unit = _units(root, s1)[0]
    ws = Workspace.open(root)
    card, session = ws.load()
    saved = (session.scans[unit].detectors, session.scans[unit].status)
    session.scans[unit].detectors, session.scans[unit].status = {}, "ingested"
    ws.save(card, session)
    _save(root, [s1])
    assert not link_view(root).by_unit[unit].names and not link_view(root).by_unit[unit].stale
    card, session = ws.load()
    session.scans[unit].detectors, session.scans[unit].status = saved
    ws.save(card, session)
    view = link_view(root)
    assert view.by_unit[unit].stale and view.by_unit[unit].names == ()


def test_a_calibration_change_after_the_link_makes_its_record_stale_by_inputs(root):
    s1, _ = scan_keys(root)
    unit = _units(root, s1)[0]
    _save(root, [s1])
    _recalibrate(root, unit)
    view = link_view(root)
    assert view.by_unit[unit].stale and not view.by_unit[unit].current and view.by_unit[unit].names == ()


def test_a_split_candidate_names_both_branches_and_is_not_settled(root):
    scans = scan_keys(root)
    unit = _units(root, scans[0])[0]
    result = linking.link_scans(root, scans)
    b1, b2 = sorted(result.branches, key=lambda b: b.id)[:2]
    pair = tuple(sorted((b1.id, b2.id)))
    by_id = {b.id: b.name for b in result.branches}
    split = core.Change("split", 1, pair, ())
    writer.save_links(root, replace(result, changes={**result.changes, unit: (split,)}), scope=scans, batch=None,
                      stage=None)
    ul = link_view(root).by_unit[unit]
    a, b = by_id[pair[0]], by_id[pair[1]]
    assert ul.unresolved == ((), (a, b))
    assert ul.changes == (ChangeView("split", 1, (a, b), None, f"Mode 2 may be two branches, {a} and {b}; not settled"),)
    assert ul.changes == (ChangeView("split", 1, (a, b), None, LINK_WORDS["split"].format(2, a, b)),)


def test_markers_changes_and_ties_carry_their_words_groups_and_severities(root):
    scans = scan_keys(root)
    u1, u2 = _units(root, scans[0])[:2]
    result = linking.link_scans(root, scans)
    b1, b2 = sorted(result.branches, key=lambda b: b.id)[:2]
    names = {b.id: b.name for b in result.branches}
    markers = (core.Marker(u1, "change", 0, "energy", "E"), core.Marker(u1, "at_cap", 1, None, "w"),
               core.Marker(u1, "unlinked", 1, None, "u"), core.Marker(u1, "contradiction", 0, None, "c"))
    tie = core.TieGroup("t1", "swap", (u1, u2), {u1: (0, 1), u2: (0, 1)}, (b1.id, b2.id),
                        {u1: (b2.id, b1.id), u2: (b2.id, b1.id)}, 0.2, 0.4)
    changed = replace(result, markers=markers, ties=(tie,), changes={**result.changes, u1: (core.Change("drop", 1, (), ()),)})
    writer.save_links(root, changed, scope=scans, batch=None, stage=None)
    view = link_view(root)
    ul = view.by_unit[u1]
    a, b = names[b1.id], names[b2.id]
    assert [(m.kind, m.group, m.mode, m.quantity, m.label, m.words, m.severity) for m in ul.markers] == [
        ("change", 2, 0, "energy", "energy differs from neighbours", "E", "warn"),
        ("at_cap", 2, 1, None, "at the width cap (wants to be broader)", "w", "info"),
        ("unlinked", 3, 1, None, "no link to a neighbour", "u", "warn"),
        ("contradiction", 2, 0, None, "links contradict each other", "c", "serious")]
    assert ul.changes == (ChangeView("drop", 1, (), None, "Mode 2 follows no neighbour"),)
    assert ul.ties == (TieView("t1", (a, b), (u1, u2), f"{a} and {b} are nearly tied at this crossing"),)
    assert view.by_unit[u2].ties == ul.ties and view.by_unit[u2].markers == ()


def test_each_spectrum_reads_the_newest_record_of_its_own_scan_and_branches_merge(root):
    s1, s2 = scan_keys(root)
    both = _save(root, [s1, s2])
    one = _save(root, [s1])                               # the newer record of the first scan only
    view = link_view(root)
    assert {view.by_unit[u].link_id for u in _units(root, s1)} == {one}
    assert {view.by_unit[u].link_id for u in _units(root, s2)} == {both}
    u1, u2 = _units(root, s1)[0], _units(root, s2)[0]
    assert view.by_unit[u1].names == (None, None)          # the newer lone record links nothing, and it governs u1
    name = view.by_unit[u2].names[0]
    assert {(u1, 0), (u2, 0)} <= set(view.branches[name].members)       # the older record's members stay in the branch


def test_a_spectrum_no_record_covers_is_absent_and_units_narrow_the_view(root):
    s1, s2 = scan_keys(root)
    _save(root, [s1])
    view = link_view(root)
    assert set(view.by_unit) == set(_units(root, s1))
    assert link_view(root, _units(root, s2)) == LinkView("", {}, {})
    (narrow,) = link_view(root, [_units(root, s1)[0]]).by_unit.values()
    assert narrow.unit == _units(root, s1)[0]


def test_per_mode_names_and_energies_follow_ascending_omega0(root):
    unit = spectrum_keys(root)[0]
    _plain_fast(root, unit, omega0=(8.0, 4.0))            # stored in descending order
    _save(root, scan_keys(root))
    ul = link_view(root).by_unit[unit]
    assert ul.peak_E[0] < ul.peak_E[1] and ul.peak_E[0] == pytest.approx(4.0, abs=0.1)


def test_the_newest_record_of_each_scan_wins_in_the_store_reader():
    def line(link, scope):
        return records.LinkLine(link, "t", tuple(scope), None, None, {}, {}, 1, "d", {}, ("f.jsonl", 0, 1))
    lines = [line("l1", ["exp1:12", "exp1:13"]), line("l2", ["exp1:12"])]
    assert {s: x.link for s, x in records.newest_link_by_scan(lines).items()} == {"exp1:12": "l2", "exp1:13": "l1"}
    assert {s: x.link for s, x in records.newest_link_by_scan(lines, ["exp1:13"]).items()} == {"exp1:13": "l1"}
