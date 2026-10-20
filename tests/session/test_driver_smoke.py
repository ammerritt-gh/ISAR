"""Step-5 driver smoke gates (the retired unification build order §10.5, git history), kept light for CI / a slow machine.

- IN8 (generated pair, always runs): the full driver end-to-end — the well-sampled scan recovers
  omega0 within 0.5 meV of the mode energy chosen for the test, the three-point scan is
  undersampled; re-run is idempotent; a guideline exclusion refits exactly one. The same check on
  the real anchor numors is a proof (proofs/).
- S30 (synthetic, always runs): the bootstrap gate — measurements before any calibration are
  held_for_calibration (never E0 = 0), not fitted.

The full 83-scan S30 statistical comparison at M=200 is the heavyweight gate; it is deferred to a
fast machine (see the retired unification build order §10.5, git history, and the command in the module docstring below):

    isar init "real_data/APS S30/FeSe_1-xS_x_1.spec_supplementary.dat" --out ws_s30
    # set [fitting] detectors = ["ana7"]; [calibration] companion_glob = "FeSe_1-xS_x_1.spec"
    isar run "real_data/APS S30/FeSe_1-xS_x_1.spec_supplementary.dat" \
             "real_data/APS S30/FeSe_1-xS_x_1.spec" --workspace ws_s30 --M 200
    isar score --workspace ws_s30 --gold "real_data/APS S30/FeSe_1_fit_results.csv"
"""
import json
from pathlib import Path

import pytest

from isar.parsers import ill_in8, spec_s30
from isar.session import driver
from isar.session.timing import StageTimer
from isar.session.workspace import Workspace
from isar.synth import config, dispersion
from isar.synth_format import run_builder

# The generated IN8 pair. The mode energy follows from the builder's dispersion at the scan's H
# (omega0(H) = 6 |sin(pi (H-2))|), so H = 2.5 puts the mode at the zone boundary, 6.0 meV: a round
# value deliberately unlike any measured one. The thin scan is a three-point grid at another Q, so
# it neither pools with the main scan nor can be fitted.
IN8_MODE_MEV = 6.0
IN8_MAIN_Q = [2.5, 0.05, 0.0]
IN8_THIN_Q = [2.25, 0.05, 0.0]
IN8_MAIN_NUMOR = 60101
IN8_THIN_NUMOR = 60102
IN8_SEED = 4242


def write_in8_pair(dest):
    """Write the generated IN8 pair into `dest`: two numor files (one run each), `060101` a
    well-sampled constant-Q scan whose mode sits at IN8_MODE_MEV, `060102` a three-point scan."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    assert abs(float(dispersion.omega0_of_H(IN8_MAIN_Q[0])) - IN8_MODE_MEV) < 1e-9
    card = ill_in8.card_template()
    thin = config.RunConfig(x_min=-1.0, x_max=1.0, x_step=1.0)   # -1, 0, 1 meV: three points
    for numor, q, cfg in ((IN8_MAIN_NUMOR, IN8_MAIN_Q, None), (IN8_THIN_NUMOR, IN8_THIN_Q, thin)):
        run = run_builder.build_in8_run("IN8-U4", n_scans=1, seed0=IN8_SEED + numor,
                                        q_point=q, first_numor=numor, cfg=cfg)
        (dest / f"{numor:06d}").write_bytes(ill_in8.emit_file(run, card))
    return dest


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


# --------------------------------------------------------------------------- early GUI visibility
def test_run_batch_registers_ingested_before_fit_stage(tmp_path, monkeypatch):
    """A scan is parsed/reduced and its per-unit data file written within ~1s, but a fit can take
    ~70s; `run_batch` must register + save the ledger at status "ingested" before the fit stage
    runs (operator report 2026-09-03), so a GUI watcher sees the scan's counts while it fits."""
    from isar.parsers import spec_s30 as s30
    from isar.session import scan_delivery
    ws = _init_workspace(tmp_path, s30)
    run = run_builder.build_s30_run("SYN", n_scans=2, seed0=1000)
    data = tmp_path / "SYN.spec_supplementary.dat"
    data.write_bytes(s30.emit_file(run, s30.card_template()))

    captured = {}
    real_run_tasks = scan_delivery._run_tasks

    def spy(*args, **kwargs):
        if "statuses" not in captured:
            raw = json.loads(ws.session_path.read_text(encoding="utf-8"))
            captured["statuses"] = {sid: row["status"] for sid, row in raw.get("scans", {}).items()}
        return real_run_tasks(*args, **kwargs)

    monkeypatch.setattr(scan_delivery, "_run_tasks", spy)
    driver.run_batch(ws, [data], M=10, detectors="ana5")

    # BEFORE the fit stage ran, the on-disk ledger already listed both scans as "ingested".
    assert captured.get("statuses"), "fit stage (_run_tasks) was never invoked"
    assert set(captured["statuses"].values()) == {"ingested"}

    # AFTER the run, the entries reached their normal (non-"ingested") terminal status.
    _card, session = ws.load()
    assert {e.status for e in session.scans.values()} == {"held_for_calibration"}


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


# --------------------------------------------------------------------------- IN8 generated-pair driver
# The three IN8 campaign smokes are slow-marked (owner rule 2026-08-08, condition fired): under
# evidence selection their guideline-declared peaks force 2-mode offers whose railed superset
# candidates escalate to dynesty inside every master fit — ~76 s each at ESCALATION_N_LIVE=128
# (measured serial, post-cut) against the not-slow lane's ~1 min contract. The full lane still
# runs them pre-merge.
@pytest.mark.slow
def test_in8_provenance_carries_fit_seconds(tmp_path):
    """A fitted scan stamps entry.provenance['fit_seconds'] (+ two_stage/errors_attach), which rides
    home through the ledger round-trip and shows up in the fit_scans stage per_task list."""
    from isar.parsers import ill_in8
    data = write_in8_pair(tmp_path / "in8")
    ws = _init_workspace(tmp_path, ill_in8)

    timer = StageTimer(errors_tier="full", M=30)
    driver.run_batch(ws, [data], M=30, timer=timer)

    # The reloaded ledger keeps fit_seconds on the fitted scan's provenance (round-trip).
    _card, session = ws.load()
    e82 = _find(session, IN8_MAIN_NUMOR)
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
def test_in8_driver_end_to_end(tmp_path):
    from isar.parsers import ill_in8
    data = write_in8_pair(tmp_path / "in8")
    ws = _init_workspace(tmp_path, ill_in8)

    summary = driver.run_batch(ws, [data], M=30)
    _card, session = ws.load()
    e82 = _find(session, IN8_MAIN_NUMOR)
    e83 = _find(session, IN8_THIN_NUMOR)
    # The well-sampled scan: a real fit landing near the chosen mode energy (verdict may decline on cost).
    assert e82.status == "fitted"
    oc = e82.detectors["det0"]
    assert oc["omega0"] is not None and abs(oc["omega0"] - IN8_MODE_MEV) <= 0.5
    assert oc["gamma_provisional"] is True             # TAS: Γ provisional
    assert oc["calib"]["resolution_source"] == "cooper_nathans"
    # The three-point scan -> undersampled non-result (never a fit).
    assert e83.status == "non_result"

    # Idempotency: an immediate re-run fits nothing new.
    s2 = driver.run_batch(ws, [data], M=30)
    assert s2.n_fitted_this_run == 0
    assert (ws.report_dir / "results.csv").exists()


@pytest.mark.slow
def test_in8_guideline_exclusion_refits_one(tmp_path):
    from isar.parsers import ill_in8
    data = write_in8_pair(tmp_path / "in8")
    ws = _init_workspace(tmp_path, ill_in8)
    driver.run_batch(ws, [data], M=30)

    # The correction channel: exclude the main scan in the card, re-run -> exactly that scan changes.
    ws.policy_path.write_text(
        ws.policy_path.read_text(encoding="utf-8")
        + f'\n[[fitting.overrides]]\nscan = {IN8_MAIN_NUMOR}\nexclude = true\nreason = "smoke exclude"\n',
        encoding="utf-8")
    s2 = driver.run_batch(ws, [data], M=30)
    _card, session = ws.load()
    e82 = _find(session, IN8_MAIN_NUMOR)
    assert e82.status == "excluded" and "smoke exclude" in "; ".join(e82.reasons)
    # Exactly one scan re-entered the queue: the main scan (its guideline hash changed). The thin scan
    # was untouched by the edit, so it did not re-process (the correction channel is surgical).
    assert s2.n_reprocessed == 1
    assert _find(session, IN8_THIN_NUMOR).status == "non_result"


def _find(session, scan_number):
    for e in session.scans.values():
        if e.scan_number == scan_number:
            return e
    return None
