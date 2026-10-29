"""The writer's inputs hash includes the gpi entries in force (S1U15; C-records, C-save-inter).

A spectrum whose gpi hkl is in force is stamped by ``file_fit`` with the hash of its gpi-bearing inputs; the real
writer must recompute the same hash under the lock, or every such refit is declined as "inputs changed". Every
number is made up (hard rule 10)."""
from __future__ import annotations

from pathlib import Path

import isar
from inter_q_fixtures import spectrum_keys, workspace_with_gpi
from isar.session import file_fit, writer
from isar.session.inter_spectra import records
from isar.session.workspace import Workspace

REPO = Path(__file__).resolve().parents[2]
STEM = "Smp_100K_0p5_0p5_3_A"
HKL = {"a03": (0.5, 0.5, 3.0), "a04": (0.5, 0.6, 3.0), "a05": (0.5, 0.7, 3.0)}


def test_import_root_is_the_worktree():
    assert Path(isar.__file__).resolve().parents[1] == REPO


def _stamped(root, unit):
    """The inputs hash the runner stamps on a refit of ``unit``: ``file_fit``'s own preparation, as it fits. The fast
    pass saves the scan's tabulated calibration on the card before any refit, so it is saved here too."""
    frozen = file_fit.freeze(root)
    scan, _det = unit.rsplit(":", 1)
    exp, scan_no = scan.rsplit(":", 1)
    scan_prep = file_fit.prepare_scan(frozen, exp, int(scan_no), frozen.session.get(unit))
    prep = file_fit.prepare_unit(frozen, scan_prep, unit, ("fast",))
    frozen.card.state.tabulated_calibration.update(prep.sp.recalibrated)
    Workspace.open(root).save(frozen.card, frozen.session)
    return prep.now.inputs_hash


def _payload(batch, unit, fit, inputs_hash):
    refit = {"fit": fit, "unit": unit, "status": "accepted", "reason": None, "seed": "fast", "inputs": inputs_hash,
             "conditioned_on": [], "flag": {"score": 0.5, "flagged": False, "reasons": []}, "why_changed": None,
             "payload": {"came_in": {"unit": unit}, "came_out": {"unit": unit}, "overlay": {"n_modes": 1, "unit": unit}}}
    head = {"schema": records.SCHEMA_SAVE, "unit": f"inter:{batch}:batch", "pass": "inter", "batch": batch}
    start = {**head, "event": "start",
             "start": {"batch_kind": "anchored", "scope": {"direction": "q", "members": []}, "anchors": [],
                       "rings": {}, "seams": [], "settings": {"version": 1, "values": {}}, "preview": "test",
                       "fixed": {}, "unreached": {}, "terms": [], "metric": {}, "core": "test", "inputs_at": 0,
                       "intake_seq": 0, "epochs": {}}}
    stage = {**head, "event": "stage",
             "stage": {"stage": "ring", "index": 1, "round": None, "refits": [refit], "seams": [],
                       "progress": {"done": 1, "failed": 0, "not_reached": 0, "fixed": 0, "total": 1},
                       "not_reached": {}}}
    end = {**head, "event": "end", "end": {"state": "completed", "reason": None, "done": 1, "failed": 0,
                                           "not_reached": [], "rings_done": 1, "sweeps": None, "rounds": None,
                                           "timing": {}}}
    return [start, stage, end]


def test_a_refit_of_a_spectrum_whose_gpi_hkl_is_in_force_is_authoritative(tmp_path):
    root = workspace_with_gpi(tmp_path, {STEM: HKL})
    unit = next(k for k in spectrum_keys(root) if k.endswith(":a04"))
    stamped = _stamped(root, unit)
    batch, fit = records.new_batch_id(), records.new_fit_id()
    verdict = writer.save_inter(root, _payload(batch, unit, fit, stamped))
    assert verdict[1] == {"saved": True, "accepted": {unit: fit}, "refused": {}}, verdict
    (line,) = records.read_log(root).refits
    assert line.accept.decision == "authoritative" and line.inputs == stamped
