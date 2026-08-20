"""Step-5 driver smoke gates (the retired unification build order §10.5, git history), kept light for CI / a slow machine.

- IN8 (real data, skipped if absent): the full driver end-to-end on the anchor pair — 055182
  recovers omega0 within 0.5 meV of the vetted ~4.2 (verdict aside — the gold check gates on
  omega0), 055183 is undersampled; re-run is idempotent; a guideline exclusion refits exactly one.
- S30 (synthetic, always runs): the bootstrap gate — measurements before any calibration are
  held_for_calibration (never E0 = 0), not fitted.

The full 83-scan S30 statistical comparison at M=200 is the heavyweight gate; it is deferred to a
fast machine (see the retired unification build order §10.5, git history, and the command in the module docstring below):

    isar init "test_data/APS S30/FeSe_1-xS_x_1.spec_supplementary.dat" --out ws_s30
    # set [fitting] detectors = ["ana7"]; [calibration] companion_glob = "FeSe_1-xS_x_1.spec"
    isar run "test_data/APS S30/FeSe_1-xS_x_1.spec_supplementary.dat" \
             "test_data/APS S30/FeSe_1-xS_x_1.spec" --workspace ws_s30 --M 200
    isar score --workspace ws_s30 --gold "test_data/APS S30/FeSe_1_fit_results.csv"
"""
import shutil
from pathlib import Path

import pytest

from isar.parsers import spec_s30
from isar.session import driver
from isar.session.timing import StageTimer
from isar.session.workspace import Workspace
from isar.synth_format import run_builder

REPO = Path(__file__).resolve().parents[2]
IN8_DIR = REPO / "test_data" / "ILL IN8"
_have_in8 = (IN8_DIR / "055182").exists() and (IN8_DIR / "055183").exists()


def _init_workspace(tmp_path, plugin_module):
    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir(parents=True, exist_ok=True)
    ws.policy_path.write_text(Path(plugin_module._CARD_PATH).read_text(encoding="utf-8"),
                              encoding="utf-8")
    return ws


# --------------------------------------------------------------------------- S30 synthetic: held gate
def test_s30_without_calibration_holds_never_zero(tmp_path):
    """A measurement before any elastic/zero is held_for_calibration — never E0 = 0 (bootstrap gate)."""
    from isar.parsers import spec_s30 as s30
    ws = _init_workspace(tmp_path, s30)
    run = run_builder.build_s30_run("SYN", n_scans=3, seed0=1000)
    data = tmp_path / "SYN.spec_supplementary.dat"
    data.write_bytes(s30.emit_file(run, s30.card_template()))
    # No companion .spec present -> no elastics -> no zero.
    summary = driver.run_batch(ws, [data], M=10, detectors="ana5")
    statuses = {e.status for e in Workspace.open(ws.root).load()[1].scans.values()}
    assert statuses == {"held_for_calibration"}
    # Held scans carry a reason and no fitted omega.
    _card, session = ws.load()
    for e in session.scans.values():
        assert e.reasons and "zero" in e.reasons[0].lower()


def test_s30_held_is_idempotent(tmp_path):
    from isar.parsers import spec_s30 as s30
    ws = _init_workspace(tmp_path, s30)
    run = run_builder.build_s30_run("SYN", n_scans=2, seed0=1000)
    data = tmp_path / "SYN.spec_supplementary.dat"
    data.write_bytes(s30.emit_file(run, s30.card_template()))
    driver.run_batch(ws, [data], M=10, detectors="ana5")
    # Held scans re-enter the queue every run (bootstrap re-examination) — still held here.
    s2 = driver.run_batch(ws, [data], M=10, detectors="ana5")
    assert all(e.status == "held_for_calibration" for e in ws.load()[1].scans.values())


# --------------------------------------------------------------------------- timing instrumentation
def test_run_batch_emits_timer_stages(tmp_path):
    """A StageTimer threaded into run_batch records the pipeline stages (mighty-starfish §3). Uses the
    always-run S30 held path: scans still traverse the fitter as tasks, so fit_scans is emitted."""
    from isar.parsers import spec_s30 as s30
    ws = _init_workspace(tmp_path, s30)
    run = run_builder.build_s30_run("SYN", n_scans=3, seed0=1000)
    data = tmp_path / "SYN.spec_supplementary.dat"
    data.write_bytes(s30.emit_file(run, s30.card_template()))

    timer = StageTimer(errors_tier="full", M=10)
    driver.run_batch(ws, [data], M=10, detectors="ana5", timer=timer)

    d = timer.to_dict()
    names = [s["stage"] for s in d["stages"]]
    for expected in ("workspace_load", "ingest_parse", "reduce_qc", "route_context", "fit_scans",
                     "collate", "fit_masters", "write_results", "session_save"):
        assert expected in names, f"missing stage {expected} in {names}"
    ingest = next(s for s in d["stages"] if s["stage"] == "ingest_parse")
    assert ingest["n_records"] >= 1
    fit = next(s for s in d["stages"] if s["stage"] == "fit_scans")
    assert fit["n_tasks"] == 3 and "use_pool" in fit and "boot_workers" in fit
    assert isinstance(d["total_seconds"], float) and d["total_seconds"] >= 0.0


# --------------------------------------------------------------------------- IN8 real-data driver
# The three IN8 campaign smokes are slow-marked (owner rule 2026-08-08, condition fired): under
# evidence selection their guideline-declared peaks force 2-mode offers whose railed superset
# candidates escalate to dynesty inside every master fit — ~76 s each at ESCALATION_N_LIVE=128
# (measured serial, post-cut) against the not-slow lane's ~1 min contract. The full lane still
# runs them pre-merge.
@pytest.mark.slow
@pytest.mark.skipif(not _have_in8, reason="IN8 test_data not present (gitignored)")
def test_in8_provenance_carries_fit_seconds(tmp_path):
    """A fitted scan stamps entry.provenance['fit_seconds'] (+ two_stage/errors_attach), which rides
    home through the ledger round-trip and shows up in the fit_scans stage per_task list."""
    from isar.parsers import ill_in8
    data = tmp_path / "in8"
    data.mkdir()
    for n in ("055182", "055183"):
        shutil.copy(IN8_DIR / n, data / n)
    ws = _init_workspace(tmp_path, ill_in8)

    timer = StageTimer(errors_tier="full", M=30)
    driver.run_batch(ws, [data], M=30, timer=timer)

    # The reloaded ledger keeps fit_seconds on the fitted scan's provenance (round-trip).
    _card, session = ws.load()
    e82 = session.get("397:55182:det0") or _find(session, 55182)
    assert e82.status == "fitted"
    prov = e82.provenance
    assert isinstance(prov.get("fit_seconds"), float) and prov["fit_seconds"] >= 0.0
    assert "two_stage_seconds" in prov  # attached from the fit dict

    # The fit_scans stage aggregates the per-task timing for the fitted scan.
    fit = next(s for s in timer.to_dict()["stages"] if s["stage"] == "fit_scans")
    per_task = fit.get("per_task") or []
    assert any(t["scan_id"] == e82.scan_id and isinstance(t["seconds"], float)
               for t in per_task)



@pytest.mark.slow
@pytest.mark.skipif(not _have_in8, reason="IN8 test_data not present (gitignored)")
def test_in8_driver_end_to_end(tmp_path):
    from isar.parsers import ill_in8
    data = tmp_path / "in8"
    data.mkdir()
    for n in ("055182", "055183"):
        shutil.copy(IN8_DIR / n, data / n)
    ws = _init_workspace(tmp_path, ill_in8)

    summary = driver.run_batch(ws, [data], M=30)
    _card, session = ws.load()
    e82 = session.get("397:55182:det0") or _find(session, 55182)
    e83 = session.get("397:55183:det0") or _find(session, 55183)
    # 055182: a real fit landing near the vetted ~4.2 meV (verdict may decline on cost, like the check).
    assert e82.status == "fitted"
    oc = e82.detectors["det0"]
    assert oc["omega0"] is not None and abs(oc["omega0"] - 4.2) <= 0.5
    assert oc["gamma_provisional"] is True             # TAS: Γ provisional
    assert oc["calib"]["resolution_source"] == "cooper_nathans"
    # 055183: 3 points -> undersampled non-result (never a fit).
    assert e83.status == "non_result"

    # Idempotency: an immediate re-run fits nothing new.
    s2 = driver.run_batch(ws, [data], M=30)
    assert s2.n_fitted_this_run == 0
    assert (ws.report_dir / "results.csv").exists()


@pytest.mark.slow
@pytest.mark.skipif(not _have_in8, reason="IN8 test_data not present (gitignored)")
def test_in8_guideline_exclusion_refits_one(tmp_path):
    from isar.parsers import ill_in8
    data = tmp_path / "in8"
    data.mkdir()
    for n in ("055182", "055183"):
        shutil.copy(IN8_DIR / n, data / n)
    ws = _init_workspace(tmp_path, ill_in8)
    driver.run_batch(ws, [data], M=30)

    # The correction channel: exclude 055182 in the card, re-run -> exactly that scan changes.
    ws.policy_path.write_text(
        ws.policy_path.read_text(encoding="utf-8")
        + '\n[[fitting.overrides]]\nscan = 55182\nexclude = true\nreason = "smoke exclude"\n',
        encoding="utf-8")
    s2 = driver.run_batch(ws, [data], M=30)
    _card, session = ws.load()
    e82 = _find(session, 55182)
    assert e82.status == "excluded" and "smoke exclude" in "; ".join(e82.reasons)
    # Exactly one scan re-entered the queue: 055182 (its guideline hash changed). 055183 was
    # untouched by the edit, so it did not re-process (the correction channel is surgical).
    assert s2.n_reprocessed == 1
    assert _find(session, 55183).status == "non_result"


def _find(session, scan_number):
    for e in session.scans.values():
        if e.scan_number == scan_number:
            return e
    return None
