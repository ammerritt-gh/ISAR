"""P0-3 / P0-4 (GUI Phase 0): additive persistence of the delivered fit's linear amplitudes and
the model-selection candidate table on ``FitOutcome``.

Covers: (1) ``fit_one`` populates ``amplitudes`` (linear param names) and ``candidates`` (one row
per scored structure, monotone Δaic≥0, winner at Δ==0 == ``best_label``, weights sum ~1);
(2) stored amplitudes reproduce ``report.audit.reconstruct_curve`` (the point of P0-3 — rebuild
without re-solving NNLS); (3) results.csv round-trips both as JSON columns and an OLD csv without
them reads with None; (4) session.json (ScanEntry.detectors) carries both through save/load.

The fitting tests reuse the single-peak pattern from ``test_driver_multimode`` and are ``slow``
(they run iminuit); the CSV/ledger tests are fast (hand-built outcomes, no fitting).
"""
import json

import numpy as np
import pytest

from isar.analyze.components import CompositeModel, ConstantBackground, Elastic, PhononDHO
from isar.analyze.context import ScanContext
from isar.drive.isar_io import readers
from isar.report.audit import reconstruct_curve
from isar.schema import card as card_mod
from isar.session import driver
from isar.session.state import ScanEntry, SessionState
from isar.session.workspace import Workspace
from isar.synth._ff import convolute_voigt_dho


def _card():
    pol = card_mod.CardPolicy(
        facility="APS", instrument="S30", format_version="2025", plugin="spec_s30",
        detectors=[card_mod.DetectorSpec("ana7", 10, "Ana7_3std", True)],
        energy_column="HerixE", monitor_column="ICO-C", counting_mode="time",
        normalize_by="monitor", raw_abscissa_kind="energy", energy_status="needs_zero",
        measurement_pattern="fscan", alignment_pattern="ascan",
        temperature_rule={}, collation={}, fitting={})
    return card_mod.Card(pol, card_mod.CardState())


def _single_peak_ctx():
    x = np.linspace(-8.0, 16.0, 81)
    counts = convolute_voigt_dho(x, 5.0, 0.0, 1.0, 0.0, 0.4, 500.0, 0.0, 0.0, 50.0,
                                 peak="stokes+astokes") + 8.0
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=50.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


# --------------------------------------------------------------------------- amplitudes + candidates
@pytest.mark.slow
def test_fit_one_populates_amplitudes_and_candidates():
    ctx = _single_peak_ctx()
    fit = driver.fit_one(ctx, _card(), prior=None, M=20, seed=1)

    # amplitudes: non-empty dict of the winning composite's linear param names, plain floats.
    amps = fit["amplitudes"]
    assert isinstance(amps, dict) and amps
    assert all(isinstance(v, float) for v in amps.values())
    # the phonon area must be present (the winner is a phonon structure) and positive.
    assert any(k.endswith(".A") for k in amps)
    assert amps[next(k for k in amps if k.endswith(".A"))] > 0.0

    # candidates: >=1 row, each with the compact schema, plain scalar types (JSON-clean).
    cands = fit["candidates"]
    assert isinstance(cands, list) and cands
    keys = {"label", "k", "ndof", "cost", "cost_per_dof", "n_modes",
            "delta_aic", "akaike_weight", "usable"}
    for row in cands:
        assert set(row) == keys
        assert isinstance(row["label"], str) and isinstance(row["k"], int)
        assert isinstance(row["ndof"], int) and isinstance(row["n_modes"], int)
        assert isinstance(row["cost"], float) and isinstance(row["delta_aic"], float)
        assert isinstance(row["akaike_weight"], float) and isinstance(row["usable"], bool)

    # delta_aic >= 0 with the winner at exactly 0, and the winner row equals best_label.
    assert all(r["delta_aic"] >= -1e-9 for r in cands)
    winners = [r for r in cands if r["delta_aic"] == min(c["delta_aic"] for c in cands)]
    assert len(winners) >= 1
    assert min(r["delta_aic"] for r in cands) == pytest.approx(0.0, abs=1e-9)
    winner = min(cands, key=lambda r: r["delta_aic"])
    assert winner["label"] == fit["best_label"]

    # Akaike weights are a normalised distribution over the scored candidates.
    assert sum(r["akaike_weight"] for r in cands) == pytest.approx(1.0, abs=1e-6)


@pytest.mark.slow
def test_stored_amplitudes_reproduce_reconstruct_curve():
    """P0-3 payoff: rebuilding the model from stored (omega0, gamma, resolution, amplitudes) via a
    direct ``CompositeModel.evaluate`` matches ``reconstruct_curve`` (which re-solves the linear
    amplitudes by NNLS). Both fix the same nonlinear values and fit the same counts, so the stored
    amplitudes ≈ the NNLS amplitudes; a few-percent tolerance covers the MIGRAD-vs-NNLS gap on the
    linear part plus any component the winner dropped (its stored amplitude is absent → treated 0,
    while NNLS may assign it a small non-zero value)."""
    ctx = _single_peak_ctx()
    fit = driver.fit_one(ctx, _card(), prior=None, M=20, seed=1)
    amps = fit["amplitudes"]
    omega0, gamma = fit["omega0"], fit["gamma"]
    assert omega0 is not None

    # Rebuild on the phonon+elastic+bg basis reconstruct_curve uses, filling any component the
    # winning structure lacked with amplitude 0 (params-only, no NNLS).
    omega_d = float(np.sqrt(max(omega0 ** 2 - gamma ** 2, 1e-6)))
    comp = CompositeModel([PhononDHO(), Elastic(), ConstantBackground()])
    theta = [omega_d, float(gamma),
             float(amps.get("phonon.A", 0.0)),
             float(amps.get("elastic.E", 0.0)),
             float(amps.get("bg.BG", 0.0))]
    stored_total = comp.evaluate(ctx.x, theta, ctx)

    rec = reconstruct_curve(ctx.x, ctx.counts, omega0=omega0, gamma=gamma, center=ctx.center,
                            gauss_fwhm=ctx.gauss_fwhm, lorentz_fwhm=ctx.lorentz_fwhm, T=ctx.T)

    # Agreement over the peak region (where the model carries signal); relative to the curve scale.
    scale = float(np.max(rec.total))
    assert np.max(np.abs(stored_total - rec.total)) / scale < 0.03


# --------------------------------------------------------------------------- csv + ledger round-trips
def _outcome_with(amplitudes, candidates):
    return driver.FitOutcome(
        detector_id="ana7", verdict="resolved", disposition="resolved", confidence=0.9,
        omega0=5.0, omega0_err=0.05, interval=(4.9, 5.0, 5.1), modality="fold", gamma=0.4,
        gamma_provisional=False, best_label="stokes+bg", cost_per_dof=1.1,
        q=[3.0, 0.2, 0.0], T=50.0, sample="S", prior_used=None, calib={}, reasons=[],
        amplitudes=amplitudes, candidates=candidates)


def _cands():
    return [
        {"label": "stokes+bg", "k": 4, "ndof": 77, "cost": 80.0, "cost_per_dof": 1.04,
         "n_modes": 1, "delta_aic": 0.0, "akaike_weight": 0.9, "usable": True},
        {"label": "stokes+elastic+bg", "k": 5, "ndof": 76, "cost": 79.6, "cost_per_dof": 1.05,
         "n_modes": 1, "delta_aic": 1.6, "akaike_weight": 0.1, "usable": True},
    ]


def _write_read(tmp_path, outcome, scan_number=7):
    st = SessionState()
    st.put(ScanEntry(scan_id=f"E:{scan_number}:ana7", scan_number=scan_number,
                     scan_type="measurement", status="fitted",
                     detectors={"ana7": outcome.to_dict()}))
    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir(parents=True)
    driver._write_results_csv(ws, st)
    return readers.read_results_csv(ws.root)[0]


def test_to_dict_is_json_clean_for_new_fields():
    amps = {"phonon.A": 501.2, "bg.BG": 8.0}
    d = _outcome_with(amps, _cands()).to_dict()
    # No numpy leakage: the whole dict must json round-trip and preserve the new fields exactly.
    restored = json.loads(json.dumps(d))
    assert restored["amplitudes"] == amps
    assert restored["candidates"] == _cands()


def test_results_csv_roundtrips_amplitudes_and_candidates(tmp_path):
    amps = {"phonon.A": 501.2, "elastic.E": 12.0, "bg.BG": 8.0}
    row = _write_read(tmp_path, _outcome_with(amps, _cands()))
    assert row["amplitudes"] == amps
    assert row["candidates"] == _cands()
    # winner row inside the parsed candidate table still matches best_model.
    winner = min(row["candidates"], key=lambda r: r["delta_aic"])
    assert winner["label"] == row["best_model"]


def test_results_csv_none_when_absent(tmp_path):
    row = _write_read(tmp_path, _outcome_with(None, None))
    assert row["amplitudes"] is None
    assert row["candidates"] is None


def test_old_results_csv_without_columns_reads_none(tmp_path):
    """A results.csv written before these columns existed must still read, with both fields None."""
    ws = Workspace.open(tmp_path / "ws")
    (ws.root / "report").mkdir(parents=True)
    # Minimal legacy header/row: no amplitudes/candidates columns at all.
    (ws.root / "report" / "results.csv").write_text(
        "scan,detector,omega0,best_model\n7,ana7,5.0,stokes+bg\n", encoding="utf-8")
    row = readers.read_results_csv(ws.root)[0]
    assert row["omega0"] == pytest.approx(5.0)
    assert "amplitudes" not in row and "candidates" not in row  # column absent → key absent


def test_session_json_roundtrips_new_fields(tmp_path):
    amps = {"phonon.A": 501.2, "bg.BG": 8.0}
    st = SessionState()
    st.put(ScanEntry(scan_id="E:7:ana7", scan_number=7, scan_type="measurement", status="fitted",
                     detectors={"ana7": _outcome_with(amps, _cands()).to_dict()}))
    p = st.save(tmp_path / "session.json")
    reloaded = SessionState.load(p)
    det = reloaded.scans["E:7:ana7"].detectors["ana7"]
    assert det["amplitudes"] == amps
    assert det["candidates"] == _cands()
    # A reconstructed FitOutcome carries them as first-class fields.
    oc = driver.FitOutcome(**det)
    assert oc.amplitudes == amps and oc.candidates == _cands()
