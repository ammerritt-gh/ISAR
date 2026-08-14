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
import dataclasses
import json
import logging
from types import SimpleNamespace

import numpy as np
import pytest

from isar.analyze import errors
from isar.analyze.components import (CompositeModel, ConstantBackground, Elastic, PhononDHO,
                                     QuasiElastic)
from isar.analyze.structures import Hypothesis
from isar.analyze.context import ScanContext
from isar.drive.isar_io import readers
from isar.report.audit import reconstruct_curve
from isar.schema import card as card_mod
from isar.schema.statistics import (
    FIT_SELECTION_SCHEMA,
    INTERVAL_SCHEMA,
    PROBABILITY_SCHEMA,
    SHADOW_MODEL_SELECTION_SCHEMA,
    authority_scopes,
    semantics_fingerprint,
)
from isar.analyze.shadow_selection import unavailable_shadow_selection
from isar.session import driver, evidence_update, outcome as outcome_mod, scan_delivery
from isar.session.state import ScanEntry, SessionState
from isar.session.workspace import Workspace
from isar.analyze.lineshapes import convolute_voigt_dho


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
    fit = scan_delivery.fit_one(ctx, _card(), prior=None, M=20, seed=1)

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
    keys = {"label", "k", "ndof", "cost", "cost_per_dof", "n_modes", "omegas",
            "delta_aic", "akaike_weight", "posterior_probability", "log_evidence",
            "evidence_method", "usable", "estimator_kind", "fit_kind",
            "constraint_origin", "selection_eligibility", "conditioning_fingerprint",
            "data_likelihood_fingerprint", "model_parameterization_fingerprint",
            "constraint_fingerprint", "comparison_context_fingerprint",
            # SCENE-1 PR-c (commit D)
            "at_limit", "variant", "identity", "effective_constraints",
            # SCENE-3 (review finding 9)
            "components",
            # SCENE-4 PR 4.2a
            "mode_intervals"}
    for row in cands:
        assert set(row) == keys
        assert len(row["mode_intervals"]) == row["n_modes"] == len(row["omegas"])
        for cell in row["mode_intervals"]:
            assert cell is None or {"low", "center", "high", "nominal_level", "interval_kind",
                                    "inference_paradigm", "source_quantity", "authority",
                                    "conditioning_fingerprint"} == set(cell)
        assert isinstance(row["label"], str) and isinstance(row["k"], int)
        assert isinstance(row["ndof"], int) and isinstance(row["n_modes"], int)
        assert isinstance(row["cost"], float) and isinstance(row["delta_aic"], float)
        assert isinstance(row["akaike_weight"], float) and isinstance(row["usable"], bool)
        assert row["estimator_kind"] in {
            "data_likelihood_mle", "soft_penalized_likelihood", "hard_conditioned_likelihood"}
        assert row["selection_eligibility"]
        assert len(row["conditioning_fingerprint"]) == 64

    # SCENE-1 PR-c: the production catalogue declares no identity or variant, so every row is
    # identity-agnostic and unconstrained — but each still records the pin map it was priced with,
    # and the WINNER's map is lifted onto the fit dict for FitOutcome.at_limit.
    assert all(r["variant"] is None and r["identity"] is None for r in cands)
    assert all(r["effective_constraints"] == [] for r in cands)
    assert all(isinstance(r["at_limit"], dict) and r["at_limit"] for r in cands)
    assert fit["at_limit"] == next(r["at_limit"] for r in cands if r["label"] == fit["best_label"])
    assert fit["evidence_identity"] is None

    # SCENE-3: every candidate — winner or not — carries its own mode positions, so the scene can
    # place a rejected structure's modes without re-fitting. Phonon-less candidates carry [].
    for row in cands:
        assert isinstance(row["omegas"], list)
        assert all(isinstance(w, float) and np.isfinite(w) for w in row["omegas"])
        assert row["omegas"] == sorted(row["omegas"])
        assert len(row["omegas"]) == row["n_modes"]
    assert any(row["omegas"] for row in cands)
    assert all(row["omegas"] == [] for row in cands if row["n_modes"] == 0)
    # …on the DERIVED ω₀ scale everything else publishes: the winner's single mode is the delivered
    # omega0 itself, not the raw fitted Ω_d (which is lower for any damped mode).
    winner_row = next(r for r in cands if r["label"] == fit["best_label"])
    assert winner_row["omegas"] == [pytest.approx(fit["omega0"])]

    # SCENE-3 observation footprint: the masked window the cost actually saw, on the ω scale
    # (this fixture's zero is 0.0; the shift itself is pinned by the nonzero-centre test below).
    assert fit["energy_window"] == (pytest.approx(float(ctx.x_used.min()) - ctx.center),
                                    pytest.approx(float(ctx.x_used.max()) - ctx.center))

    # The AIC diagnostics lane still travels: delta_aic >= 0 with a row at exactly 0.
    assert all(r["delta_aic"] >= -1e-9 for r in cands)
    assert min(r["delta_aic"] for r in cands) == pytest.approx(0.0, abs=1e-9)
    # Selection is evidence-driven: the winner is the max-posterior row and equals best_label.
    assert all(r["posterior_probability"] is not None for r in cands)
    winner = max(cands, key=lambda r: r["posterior_probability"])
    assert winner["label"] == fit["best_label"]
    assert all(r["evidence_method"] is not None for r in cands)

    # Both weight lanes are normalised distributions over the scored candidates.
    assert sum(r["akaike_weight"] for r in cands) == pytest.approx(1.0, abs=1e-6)
    assert sum(r["posterior_probability"] for r in cands) == pytest.approx(1.0, abs=1e-6)
    semantics = fit["fit_selection_semantics"]
    assert semantics["selection_method"] == "laplace_evidence_selection"
    assert semantics["model_weight_kind"] == "posterior_probability"
    assert semantics["selection_fallback"] is None
    assert len(semantics["evidence_arena_fingerprint"]) == 64
    route = semantics["candidate_generation_route"]
    assert route["route"] == "full_two_stage"
    assert route["supplied_hint"] is None
    assert route["hint_accepted"] is False
    assert route["offered_labels"]
    assert route["scored_labels"] == [row["label"] for row in cands]
    assert all(
        semantics[name]
        for name in (
            "offered_model_set_fingerprint",
            "scored_model_set_fingerprint",
            "weight_denominator_fingerprint",
            "error_attachment_set_fingerprint",
            "legacy_pool_set_fingerprint",
            "model_selection_fingerprint",
        )
    )


@pytest.mark.slow
def test_energy_window_is_on_the_omega_scale_not_the_raw_abscissa():
    """The raw abscissa and the ω scale differ by the calibration zero, which ISAR carries in
    ``ctx.center`` and applies inside the model (``x - center``) rather than by shifting ``ctx.x``.
    Persisting the raw extent would put the footprint on a different scale from the ω₀/omegas it has
    to contain — invisible whenever the zero happens to be 0.0, as in the fixture above."""
    center = 2.0
    x = np.linspace(-8.0, 16.0, 81)
    counts = convolute_voigt_dho(x, 5.0, center, 1.0, 0.0, 0.4, 500.0, 0.0, 0.0, 50.0,
                                 peak="stokes+astokes") + 8.0
    ctx = ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=50.0,
                      center=center, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                      energy_status="needs_zero")
    fit = scan_delivery.fit_one(ctx, _card(), prior=None, M=20, seed=1)

    assert fit["energy_window"] == (pytest.approx(-10.0), pytest.approx(14.0))
    # …and the fitted mode it must place lies inside it, which the raw (-8, 16) would also satisfy
    # here only by accident of the peak sitting mid-window.
    lo, hi = fit["energy_window"]
    assert lo < fit["omega0"] < hi
    winner = next(r for r in fit["candidates"] if r["label"] == fit["best_label"])
    assert all(lo < w < hi for w in winner["omegas"])


@pytest.mark.slow
def test_stored_amplitudes_reproduce_reconstruct_curve():
    """P0-3 payoff: rebuilding the model from stored (omega0, gamma, resolution, amplitudes) via a
    direct ``CompositeModel.evaluate`` matches ``reconstruct_curve`` (which re-solves the linear
    amplitudes by NNLS). Both fix the same nonlinear values and fit the same counts, so the stored
    amplitudes ≈ the NNLS amplitudes; a few-percent tolerance covers the MIGRAD-vs-NNLS gap on the
    linear part plus any component the winner dropped (its stored amplitude is absent → treated 0,
    while NNLS may assign it a small non-zero value)."""
    ctx = _single_peak_ctx()
    fit = scan_delivery.fit_one(ctx, _card(), prior=None, M=20, seed=1)
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
    return outcome_mod.FitOutcome(
        detector_id="ana7", verdict="resolved", disposition="resolved", confidence=0.9,
        omega0=5.0, omega0_err=0.05, interval=(4.9, 5.0, 5.1), modality="fold", gamma=0.4,
        gamma_provisional=False, best_label="stokes+bg", cost_per_dof=1.1,
        q=[3.0, 0.2, 0.0], T=50.0, sample="S", prior_used=None, calib={}, reasons=[],
        amplitudes=amplitudes, candidates=candidates)


def _cands():
    return [
        {"label": "stokes+bg", "k": 4, "ndof": 77, "cost": 80.0, "cost_per_dof": 1.04,
         "n_modes": 1, "delta_aic": 0.0, "akaike_weight": 0.9,
         "posterior_probability": 0.93, "usable": True},
        {"label": "stokes+elastic+bg", "k": 5, "ndof": 76, "cost": 79.6, "cost_per_dof": 1.05,
         "n_modes": 1, "delta_aic": 1.6, "akaike_weight": 0.1,
         "posterior_probability": 0.07, "usable": True},
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


def test_evidence_sink_survives_to_results_csv(tmp_path):
    """PR #10 review: the column was declared (PR2.6) but never populated by the row builder, so
    auxiliary-probe rows were indistinguishable from canonical evidence in the CSV projection."""
    ordinary = _outcome_with(None, None)
    assert _write_read(tmp_path / "a", ordinary)["evidence_sink"] == "canonical_master"

    probe = _outcome_with(None, None)
    probe.evidence_sink = "auxiliary_track_node"
    assert _write_read(tmp_path / "b", probe)["evidence_sink"] == "auxiliary_track_node"


def test_to_dict_is_json_clean_for_new_fields():
    amps = {"phonon.A": 501.2, "bg.BG": 8.0}
    d = _outcome_with(amps, _cands()).to_dict()
    # No numpy leakage: the whole dict must json round-trip and preserve the new fields exactly.
    restored = json.loads(json.dumps(d))
    assert restored["amplitudes"] == amps
    assert restored["candidates"] == _cands()


@pytest.mark.parametrize("writer", [evidence_update._select_mode, evidence_update._clear_tracked_selection])
def test_local_identity_survives_the_writer_to_results_csv_path(tmp_path, writer):
    """Declaring a column is not populating it: the OP10 repair's second bug class.

    ``results.csv`` is the Stage II/III frozen contract and the only route by which the driver's
    GridQ learns local branch identity.  The first repair attempt declared the three columns and
    wired the writers, but the row builder maps every column explicitly, so the campaign ran with
    identity permanently unknown — the fields were present in the ledger and blank in the CSV.
    """
    outcome = _outcome_with(None, None)
    written = outcome.to_dict()
    assignment = dict(_tracked_assignment())
    assignment.update(target_probability=0.7373147601858216,
                      identity_entropy=0.5614679859005164,
                      local_identity_clear=False)
    if writer is evidence_update._select_mode:
        writer(written, {"candidate_ref": "acoustic:0", "energy_meV": 1.35,
                         "probabilities": {"acoustic": 0.9}}, 0, assignment, 1, "acoustic")
    else:
        writer(written, assignment, 1)
    outcome = driver._outcome_from_entry(
        ScanEntry(scan_id="E:7:ana7", scan_number=7, scan_type="measurement",
                  status="fitted", detectors={"ana7": written}), "ana7")

    row = _write_read(tmp_path, outcome)
    assert row["target_probability"] == pytest.approx(0.7373147601858216)
    assert row["identity_entropy"] == pytest.approx(0.5614679859005164)
    assert row["local_identity_clear"] is False


def test_results_csv_roundtrips_amplitudes_and_candidates(tmp_path):
    amps = {"phonon.A": 501.2, "elastic.E": 12.0, "bg.BG": 8.0}
    row = _write_read(tmp_path, _outcome_with(amps, _cands()))
    assert row["amplitudes"] == amps
    assert row["candidates"] == _cands()
    # winner row inside the parsed candidate table still matches best_model.
    winner = max(row["candidates"], key=lambda r: r["posterior_probability"])
    assert winner["label"] == row["best_model"]
    # the additive projection carries the winner's posterior as a plain float.
    assert row["best_model_posterior"] == pytest.approx(0.93)


def test_results_csv_roundtrips_exposure_and_candidate_provenance(tmp_path):
    outcome = _outcome_with(None, None)
    outcome.effective_exposure = 125_000_000.0
    outcome.exposure_provenance = {
        "source": "target_window", "fallback": False, "center": 5.0, "half_width": 1.0,
        "value": 125_000_000.0}
    outcome.branch_posterior = {"acoustic": 0.15, "optic": 0.8, "new": 0.05}
    outcome.candidate_id = "optic:0"
    outcome.candidate_state = "provisional"
    outcome.acquisition_id = "acq-7"
    row = _write_read(tmp_path, outcome)
    assert row["effective_exposure"] == pytest.approx(125_000_000.0)
    assert row["exposure_provenance"] == outcome.exposure_provenance
    assert row["branch_posterior"] == outcome.branch_posterior
    assert row["candidate_id"] == "optic:0"
    assert row["candidate_state"] == "provisional"
    assert row["acquisition_id"] == "acq-7"


def test_results_csv_none_when_absent(tmp_path):
    row = _write_read(tmp_path, _outcome_with(None, None))
    assert row["amplitudes"] is None
    assert row["candidates"] is None
    # the additive /2 projection columns read as honest absences, never fabricated values.
    assert row["selection_method"] is None
    assert row["best_model_posterior"] is None


def test_full_session_and_compact_csv_semantics_roundtrip(tmp_path):
    fingerprint = semantics_fingerprint("test.persistence", {"models": ["one"]})
    scopes = authority_scopes(single_fit_delivery="active")
    fit_record = {
        "schema": FIT_SELECTION_SCHEMA,
        "offered_model_set_fingerprint": fingerprint,
        "candidate_records": [{"label": "one", "offered_models": [{"verbose": True}]}],
        "authority_scopes": scopes,
    }
    interval_record = {
        "schema": INTERVAL_SCHEMA,
        "quantities": {"interval": {"availability": True, "authority_scopes": scopes}},
        "authority_scopes": scopes,
    }
    probability_record = {
        "schema": PROBABILITY_SCHEMA,
        "quantities": {
            "confidence": {
                "quantity_kind": "heuristic_score",
                "source_measure_semantics": "heuristic_triage_score",
                "authority_scopes": scopes,
            }
        },
        "authority_scopes": scopes,
    }
    shadow_record = unavailable_shadow_selection(
        "shadow_skipped_soft_deadline",
        stage="execution_policy",
        selection_context_fingerprint=fingerprint,
        candidate_generation_route={"route": "shadow_not_evaluated"},
        legacy_model_selection_fingerprint=fingerprint,
        legacy_selected_label="one",
        legacy_model_weights={"one": 1.0},
    )
    outcome = _outcome_with(None, None)
    outcome.fit_selection_semantics = fit_record
    outcome.interval_semantics = interval_record
    outcome.probability_semantics = probability_record
    outcome.shadow_model_selection = shadow_record

    row = _write_read(tmp_path, outcome)
    assert row["fit_selection_semantics"]["schema"] == FIT_SELECTION_SCHEMA
    assert row["interval_semantics"]["schema"] == INTERVAL_SCHEMA
    assert row["probability_semantics"]["schema"] == PROBABILITY_SCHEMA
    assert row["shadow_model_selection"]["schema"] == SHADOW_MODEL_SELECTION_SCHEMA
    assert (
        row["shadow_model_selection"]["unavailable_reason_code"]
        == "shadow_skipped_soft_deadline"
    )
    assert row["shadow_model_selection"]["shadow_execution_kind"] == "skipped"
    assert row["shadow_model_selection"]["reused_model_identities"] == []
    assert row["shadow_model_selection"]["freshly_fitted_model_identities"] == []

    state = SessionState()
    state.put(ScanEntry(
        scan_id="E:7:ana7", scan_number=7, scan_type="measurement", status="fitted",
        detectors={"ana7": outcome.to_dict()},
    ))
    reloaded = SessionState.load(state.save(tmp_path / "session.json"))
    full = reloaded.scans["E:7:ana7"].detectors["ana7"]
    assert full["fit_selection_semantics"] == fit_record
    assert full["interval_semantics"] == interval_record
    assert full["probability_semantics"] == probability_record
    assert full["shadow_model_selection"] == shadow_record


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
    assert "shadow_model_selection" not in row


# --------------------------------------------------------------------------- rehydration boundary
# Regression: the tracked-branch writers persist keys into ScanEntry.detectors, and the "carry"
# branch of run_batch rehydrates that dict back into a FitOutcome on every later iteration. A key
# written but never declared on the dataclass therefore survives iteration 0 and kills iteration 1
# — the whole campaign — with a TypeError from far away. Nothing exercised that path before.
def _tracked_assignment():
    """An assignment as the AP3 tracker emits it at an inferred reduced-q origin."""
    return {"track_state": "inferred_unresolved", "assignment_entropy": 0.4,
            "structural_endpoint_status": "inferred_unresolved", "is_reduced_q_origin": True}


def _written_outcome_dict(writer):
    """Run one tracked-selection writer over a real outcome dict, as the driver does."""
    outcome = _outcome_with(None, None).to_dict()
    candidate = {"candidate_ref": "acoustic:0", "energy_meV": 1.35,
                 "probabilities": {"acoustic": 0.9}}
    if writer is evidence_update._select_mode:
        writer(outcome, candidate, 0, _tracked_assignment(), 1, "acoustic")
    elif writer is evidence_update._withhold_tracked_delivery:
        writer(outcome, {**candidate, "evidence_source": "auxiliary_probe"},
               _tracked_assignment(), 1, "acoustic")
    else:
        writer(outcome, _tracked_assignment(), 1)
    return outcome


@pytest.mark.parametrize("writer", [evidence_update._select_mode, evidence_update._clear_tracked_selection])
def test_carry_path_rehydrates_tracked_selection_outcome(writer):
    """The exact production sequence: writer → ledger → carry-branch rehydration."""
    entry = ScanEntry(scan_id="E:7:ana7", scan_number=7, scan_type="measurement",
                      status="fitted", detectors={"ana7": _written_outcome_dict(writer)})
    oc = driver._outcome_from_entry(entry, "ana7")
    assert oc.structural_endpoint_status == "inferred_unresolved"
    assert oc.is_reduced_q_origin is True


@pytest.mark.parametrize("writer", [evidence_update._select_mode, evidence_update._clear_tracked_selection,
                                   evidence_update._withhold_tracked_delivery])
def test_every_tracked_selection_key_is_a_declared_field(writer):
    """Writer/schema invariant: persisting a key the dataclass cannot accept is the bug class."""
    declared = {f.name for f in dataclasses.fields(outcome_mod.FitOutcome)}
    undeclared = set(_written_outcome_dict(writer)) - declared
    assert not undeclared, f"{writer.__name__} persists undeclared field(s): {sorted(undeclared)}"


@pytest.mark.parametrize("writer", [evidence_update._select_mode, evidence_update._clear_tracked_selection])
def test_is_reduced_q_origin_normalizes_to_false_when_absent(writer):
    """Dataclasses do not enforce annotations, so an absent key must not persist None under a
    bool-typed field — that would put a lie in both the ledger and results.csv."""
    outcome = _outcome_with(None, None).to_dict()
    assignment = {"track_state": "ambiguous", "assignment_entropy": 0.4}  # no AP3 endpoint keys
    if writer is evidence_update._select_mode:
        writer(outcome, {"candidate_ref": "acoustic:0", "energy_meV": 1.35,
                         "probabilities": {}}, 0, assignment, 1, "acoustic")
    else:
        writer(outcome, assignment, 1)
    assert outcome["is_reduced_q_origin"] is False


def test_coerce_fit_outcome_survives_an_unknown_additive_key(caplog):
    """An additive key from a newer writer must not kill the campaign. The raw dict still lives in
    ScanEntry.detectors, so dropping it from the dataclass view loses nothing — but it must warn."""
    payload = {**_outcome_with(None, None).to_dict(), "some_future_field": "value"}
    with caplog.at_level("WARNING"):
        oc = driver._coerce_fit_outcome(payload)
    assert oc.detector_id == "ana7"
    assert "some_future_field" in caplog.text
    # Dropped from the dataclass view only — the caller's dict is not mutated, so the ledger keeps it.
    assert "some_future_field" not in vars(oc)
    assert payload["some_future_field"] == "value"


def test_coerce_fit_outcome_normalizes_legacy_none_origin():
    """A ledger written before the field was declared persists None under a bool annotation."""
    payload = {**_outcome_with(None, None).to_dict(), "is_reduced_q_origin": None}
    assert driver._coerce_fit_outcome(payload).is_reduced_q_origin is False


def test_coerce_fit_outcome_passes_through_an_existing_instance():
    outcome = _outcome_with(None, None)
    assert driver._coerce_fit_outcome(outcome) is outcome


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
    oc = outcome_mod.FitOutcome(**det)
    assert oc.amplitudes == amps and oc.candidates == _cands()


# --------------------------------------------------------------- SCENE-1 PR-c (commit D)
def _score(label, *, log_evidence, posterior, effective_constraints=None):
    """A ModelScore-shaped stand-in: `_candidate_rows` only reads attributes."""
    return SimpleNamespace(
        label=label, k=4, n=81, cost=80.0, delta_aic=0.0, akaike_weight=0.5,
        posterior_probability=posterior, log_evidence=log_evidence, evidence_method="laplace",
        fit_semantics={"estimator_kind": "data_likelihood_mle", "fit_kind": "unconstrained",
                       "aggregate_constraint_origin": None, "selection_eligibility": "eligible",
                       "effective_constraints": effective_constraints
                       if effective_constraints is not None else []})


def _hyp(label, *, n_modes=1, extra=()):
    """The enumerated structure a candidate was fitted as — the one authority on its components.

    Spelled per candidate rather than derived from the label: that is the whole point of review
    finding 9, and a fixture that re-parsed the label here would pin the behaviour it removed.
    """
    return Hypothesis(label, (*(PhononDHO for _ in range(n_modes)), *extra, ConstantBackground))


def _fit_result(*, at_limit=None, variant=None, identity=None, modes=None, n_modes=1,
                within_model=None):
    return SimpleNamespace(label="x", cost_per_dof=1.04, n_modes=n_modes, analysis_usable=True,
                           at_limit=at_limit, variant=variant, identity=identity,
                           modes=modes, within_model=within_model)


def test_candidate_rows_carry_variant_at_limit_and_effective_constraints():
    """The near-degenerate twin (C1) must be legible in the persisted table: which parameter the
    catalogue pinned, under which origin, and that a variant produced the row at all."""
    pin = {"parameter": "phonon.Gamma", "constraint_kind": "hard",
           "origin": "catalogue_variant", "value": 0.001}
    scores = [_score("stokes+bg", log_evidence=-40.0, posterior=0.4),
              _score("stokes+bg_neardeg", log_evidence=-39.0, posterior=0.6,
                     effective_constraints=[pin])]
    fits = {
        "stokes+bg": _fit_result(at_limit={"phonon.Omega_d": False, "phonon.Gamma": False}),
        "stokes+bg_neardeg": _fit_result(at_limit={"phonon.Omega_d": False},
                                         variant="near_degenerate", identity="acoustic"),
    }
    hyps = {"stokes+bg": _hyp("stokes+bg"), "stokes+bg_neardeg": _hyp("stokes+bg_neardeg")}
    rows = {r["label"]: r for r in scan_delivery._candidate_rows(scores, fits, hyps)}

    plain, twin = rows["stokes+bg"], rows["stokes+bg_neardeg"]
    assert plain["variant"] is None and plain["identity"] is None
    assert plain["effective_constraints"] == []          # recorded, unconstrained
    assert plain["at_limit"] == {"phonon.Omega_d": False, "phonon.Gamma": False}
    assert twin["variant"] == "near_degenerate" and twin["identity"] == "acoustic"
    assert twin["effective_constraints"] == [pin]        # read back, never re-derived
    assert twin["at_limit"] == {"phonon.Omega_d": False}
    # The rows must be JSON-clean: they are persisted as one CSV cell and one session.json value.
    assert json.loads(json.dumps(list(rows.values())))


def test_candidate_rows_tolerate_a_missing_fit_and_absent_semantics():
    score = SimpleNamespace(label="ghost", k=4, n=81, cost=80.0, delta_aic=0.0,
                            akaike_weight=1.0, fit_semantics=None)
    row = scan_delivery._candidate_rows([score], {}, {"ghost": _hyp("ghost")})[0]
    assert row["at_limit"] is None and row["variant"] is None and row["identity"] is None
    # None, not [] — no semantics record is not the same claim as "recorded, unconstrained".
    assert row["effective_constraints"] is None


def test_identity_twin_rows_keep_independent_log_evidence():
    """SCENE-3's aggregation input. Two identities of one structure share the arena and the data
    likelihood; only their catalogue priors differ, so each label's lnZ must survive to the
    persisted rows as its own number (test_identity_twins_share_one_arena_with_independent_evidence
    proves the evidence layer produces them; this is the layer that stores them)."""
    scores = [_score("stokes+bg@a", log_evidence=-40.5, posterior=0.3),
              _score("stokes+bg@b", log_evidence=-39.25, posterior=0.7)]
    fits = {"stokes+bg@a": _fit_result(identity="a"), "stokes+bg@b": _fit_result(identity="b")}
    hyps = {label: _hyp(label) for label in fits}
    rows = {r["label"]: r for r in scan_delivery._candidate_rows(scores, fits, hyps)}

    assert rows["stokes+bg@a"]["identity"] == "a" and rows["stokes+bg@b"]["identity"] == "b"
    assert rows["stokes+bg@a"]["log_evidence"] == pytest.approx(-40.5)
    assert rows["stokes+bg@b"]["log_evidence"] == pytest.approx(-39.25)


def _scene1_outcome():
    outcome = _outcome_with(None, None)
    outcome.at_limit = {"phonon.Omega_d": False, "phonon.Gamma": True}
    outcome.evidence_identity = "acoustic"
    outcome.identity_ba_disagreement = {"evidence_identity": "acoustic", "branch_id": "optic"}
    return outcome


def test_scene1_outcome_fields_roundtrip_to_dict_from_dict():
    restored = driver._coerce_fit_outcome(json.loads(json.dumps(_scene1_outcome().to_dict())))
    assert restored.at_limit == {"phonon.Omega_d": False, "phonon.Gamma": True}
    assert restored.evidence_identity == "acoustic"
    assert restored.identity_ba_disagreement == {"evidence_identity": "acoustic",
                                                 "branch_id": "optic"}


def test_scene1_outcome_fields_default_absent():
    oc = _outcome_with(None, None)
    assert (oc.at_limit, oc.evidence_identity,
            oc.identity_ba_disagreement) == (None, None, None)


def test_the_reserved_interpretation_pointer_is_gone(caplog):
    """H1, no vestigial contracts: SCENE-3 embeds the candidate rows it consumed in the revision
    rather than pointing at an artifact (DECISIONS H-series), so ``interpretation_ref`` was never
    written and is deleted. A ledger that carries the old key still loads — the tolerant reader
    drops it with a warning, which is what a retired additive field is supposed to do."""
    assert not hasattr(_outcome_with(None, None), "interpretation_ref")
    payload = {**_outcome_with(None, None).to_dict(), "interpretation_ref": "sha256:deadbeef"}
    with caplog.at_level(logging.WARNING, logger="isar.session.outcome"):
        assert not hasattr(driver._coerce_fit_outcome(payload), "interpretation_ref")
    assert any("interpretation_ref" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------- SCENE-3 (scene-layer inputs)
def test_candidate_rows_carry_every_candidates_mode_positions():
    """The scene places the modes of NON-winning candidates too, and only the winner's ``modes``
    are persisted elsewhere. The values are the DERIVED ω₀ — the scale FitOutcome.omega0, the branch
    models and the dispersion prior all publish — and a phonon-less structure carries [] rather than
    a fabricated zone-centre energy."""
    scores = [_score("stokes2+bg", log_evidence=-39.0, posterior=0.6),
              _score("bg", log_evidence=-44.0, posterior=0.4)]
    fits = {
        "stokes2+bg": _fit_result(n_modes=2, modes=[{"omega0": 4.25, "gamma": 0.4},
                                                    {"omega0": 9.5, "gamma": 0.3}]),
        "bg": _fit_result(n_modes=0, modes=[]),
    }
    hyps = {"stokes2+bg": _hyp("stokes2+bg", n_modes=2), "bg": _hyp("bg", n_modes=0)}
    rows = {r["label"]: r for r in scan_delivery._candidate_rows(scores, fits, hyps)}

    assert rows["stokes2+bg"]["omegas"] == [4.25, 9.5]
    assert len(rows["stokes2+bg"]["omegas"]) == rows["stokes2+bg"]["n_modes"]
    assert rows["bg"]["omegas"] == []
    # An unscored/failed candidate has no modes to read: absence, never NaN.
    assert scan_delivery._candidate_rows([_score("ghost", log_evidence=-50.0, posterior=0.0)],
                                         {}, {"ghost": _hyp("ghost")})[0]["omegas"] == []
    assert json.loads(json.dumps(list(rows.values())))


def test_candidate_row_omegas_report_an_unusable_mode_as_none_in_its_own_slot():
    """"No number" is None, never NaN — and the slot must stay put: the list is per-mode, so a
    dropped or reordered entry would misplace the modes that ARE known."""
    scores = [_score("stokes2+bg", log_evidence=-39.0, posterior=1.0)]
    fits = {"stokes2+bg": _fit_result(n_modes=2, modes=[{"omega0": float("nan")},
                                                        {"omega0": 9.5}])}
    row = scan_delivery._candidate_rows(scores, fits,
                                        {"stokes2+bg": _hyp("stokes2+bg", n_modes=2)})[0]
    assert row["omegas"] == [None, 9.5]
    assert len(row["omegas"]) == row["n_modes"]
    assert json.loads(json.dumps(row))["omegas"] == [None, 9.5]


def test_candidate_rows_declare_their_components_from_the_enumerated_structure():
    """Review finding 9. What a candidate is MADE OF is read off the structure it was fitted as,
    never off its label — so the descriptors are right even when the label says nothing about the
    composition, and the mode slots index the row's ``omegas``."""
    scores = [_score("elastic+stokes2+qe+bg", log_evidence=-39.0, posterior=0.6),
              _score("a label with no grammar at all", log_evidence=-44.0, posterior=0.4)]
    fits = {"elastic+stokes2+qe+bg": _fit_result(n_modes=2, modes=[{"omega0": 4.25},
                                                                   {"omega0": 9.5}]),
            "a label with no grammar at all": _fit_result(n_modes=0, modes=[])}
    hyps = {"elastic+stokes2+qe+bg": _hyp("elastic+stokes2+qe+bg", n_modes=2,
                                          extra=(Elastic, QuasiElastic)),
            "a label with no grammar at all": _hyp("a label with no grammar at all", n_modes=0,
                                                   extra=(Elastic,))}
    rows = {r["label"]: r for r in scan_delivery._candidate_rows(scores, fits, hyps)}

    # Canonical order: modes ascending by slot, then the non-mode kinds by name. The background is
    # everywhere and claims nothing, so it is no scene component.
    assert rows["elastic+stokes2+qe+bg"]["components"] == [
        {"kind": "mode", "slot": 0}, {"kind": "mode", "slot": 1},
        {"kind": "elastic", "slot": None}, {"kind": "quasielastic", "slot": None}]
    assert rows["a label with no grammar at all"]["components"] == [
        {"kind": "elastic", "slot": None}]
    # The slots index omegas, which is what lets the scene place each mode.
    assert rows["elastic+stokes2+qe+bg"]["omegas"] == [4.25, 9.5]
    assert json.loads(json.dumps(list(rows.values())))


# --------------------------------------------------------------- SCENE-4 PR 4.2a
def _real_candidate_interval_semantics(interval=(4.1, 4.25, 4.4)):
    """A REAL ``isar.interval_semantics/1`` record, off the production writer
    (``errors._interval_semantics_for``) — not a hand-invented shape."""
    result = SimpleNamespace(
        label="x", fit_semantics={"estimator_kind": "data_likelihood_mle",
                                  "effective_constraints": [],
                                  "conditioning_fingerprint": "c" * 64})
    return errors._interval_semantics_for(result, {"freq_interval": interval})


def test_a_multimode_candidate_never_assumes_a_primary_slot():
    """The population rule (SCENE-4 PR 4.2a): the persisted record has no field naming which
    slot the bootstrap tracked as primary, so a multimode row stays all-``None`` even when a
    (single) interval record happens to be attached — guessing slot 0 would be minted semantics
    the writer never asserted."""
    scores = [_score("stokes2+bg", log_evidence=-39.0, posterior=1.0)]
    within = {"freq_interval": (4.1, 4.25, 4.4),
             "interval_semantics": _real_candidate_interval_semantics()}
    fits = {"stokes2+bg": _fit_result(n_modes=2, modes=[{"omega0": 4.25}, {"omega0": 9.5}],
                                      within_model=within)}
    hyps = {"stokes2+bg": _hyp("stokes2+bg", n_modes=2)}
    row = scan_delivery._candidate_rows(scores, fits, hyps)[0]
    assert row["mode_intervals"] == [None, None]


def test_a_single_mode_survivor_projects_its_own_candidate_level_record():
    """A non-winner survivor's slot carries low/center/high plus the writer's own semantics,
    read off ``within_model`` exactly as the real error-attach writer shapes it — never the
    winner's realized authority."""
    record = _real_candidate_interval_semantics()
    item = record["quantities"]["freq_interval"]
    within = {"freq_interval": (4.1, 4.25, 4.4), "interval_semantics": record}
    scores = [_score("stokes+bg", log_evidence=-39.0, posterior=1.0)]
    fits = {"stokes+bg": _fit_result(n_modes=1, modes=[{"omega0": 4.25}], within_model=within)}
    hyps = {"stokes+bg": _hyp("stokes+bg", n_modes=1)}
    # best_label deliberately names a DIFFERENT (absent) label: this row is a non-winner survivor.
    row = scan_delivery._candidate_rows(scores, fits, hyps, best_label="some-other-label")[0]
    cell = row["mode_intervals"][0]
    assert cell == {
        "low": 4.1, "center": 4.25, "high": 4.4,
        "nominal_level": item["nominal_level"], "interval_kind": item["interval_kind"],
        "inference_paradigm": item["inference_paradigm"], "source_quantity": "freq_interval",
        "authority": {"authority_scopes": item["authority_scopes"],
                     "producer_authority_scopes": item["producer_authority_scopes"]},
        "conditioning_fingerprint": item["conditioning_fingerprint"],
    }
    # Offer-only: never a realized ``selected_for_delivery``/``passed_reliability_gate`` key.
    assert "selected_for_delivery" not in cell["authority"]
    assert "passed_reliability_gate" not in cell["authority"]
    assert json.loads(json.dumps(row))["mode_intervals"][0] == cell


def test_a_non_survivor_row_with_no_error_record_carries_all_none_honestly():
    """No ``within_model`` at all (the candidate never reached error-attach) is a real, common
    state — not an error — and must project as ``None``, never a fabricated interval."""
    scores = [_score("stokes+bg", log_evidence=-39.0, posterior=1.0)]
    fits = {"stokes+bg": _fit_result(n_modes=1, modes=[{"omega0": 4.25}])}
    hyps = {"stokes+bg": _hyp("stokes+bg", n_modes=1)}
    row = scan_delivery._candidate_rows(scores, fits, hyps)[0]
    assert row["mode_intervals"] == [None]


def test_phonon_less_candidate_carries_no_mode_interval_slots():
    scores = [_score("bg", log_evidence=-44.0, posterior=1.0)]
    fits = {"bg": _fit_result(n_modes=0, modes=[])}
    hyps = {"bg": _hyp("bg", n_modes=0)}
    row = scan_delivery._candidate_rows(scores, fits, hyps)[0]
    assert row["mode_intervals"] == []
    assert len(row["mode_intervals"]) == row["n_modes"] == len(row["omegas"])


# --------------------------------------------------------------- post-slice-boundary fix:
# not every persisted interval quantity is a (lo, center, hi) triple. Confirmed by reading
# every producer directly (never trust a claimed shape):
#   errors.bootstrap._percentile_interval  -> (lo, median, hi)   TRIPLE  (freq_interval / "interval")
#   errors.bayes.BayesResult.omega0_interval -> (lo, median, hi) TRIPLE  (bayes_interval)
#   errors.profile.omega0_profile_interval -> (lo, hi)           PAIR    (omega0_profile)
#   scan_delivery._hesse_interval_and_trust -> (lo, hi)          PAIR    (hesse_interval)
@pytest.mark.parametrize("source_quantity,values,expect_center", [
    ("freq_interval", (4.1, 4.25, 4.4), 4.25),
    ("bayes_interval", (4.2, 4.3, 4.4), 4.3),
    ("omega0_profile", (4.9, 5.1), None),
    ("hesse_interval", (4.95, 5.05), None),
])
def test_mode_interval_item_projects_each_quantitys_real_shape(source_quantity, values,
                                                                expect_center):
    item = {"availability": True, "nominal_level": 0.68}
    cell = scan_delivery._mode_interval_item(item, values, source_quantity)
    assert cell["low"] == values[0] and cell["high"] == values[-1]
    assert cell["center"] == expect_center


@pytest.mark.parametrize("values", [(4.1,), (4.1, 4.2, 4.3, 4.4)])
def test_mode_interval_item_refuses_a_malformed_value_shape(values):
    item = {"availability": True, "nominal_level": 0.68}
    with pytest.raises(ValueError, match="pair|triple"):
        scan_delivery._mode_interval_item(item, values, "freq_interval")


def test_a_hesse_selected_winner_row_round_trips_with_no_center():
    """The exact shape that crashed ``tests/session/test_driver_multimode.py``: a fast-tier
    winner whose SELECTED interval quantity is ``hesse_interval`` — a bare ``(lo, hi)`` pair, no
    median. Must project honestly (``center=None``) and round-trip through the real DTO."""
    within = {"freq_interval": (float("nan"), float("nan"), float("nan")),
             "interval_semantics": _real_candidate_interval_semantics(
                 (float("nan"), float("nan"), float("nan")))}
    best = _fit_result(n_modes=1, modes=[{"omega0": 5.0}], within_model=within)
    best.label = "stokes+bg"
    best.fit_semantics = {"estimator_kind": "data_likelihood_mle", "effective_constraints": [],
                          "conditioning_fingerprint": "c" * 64}
    hesse_interval = (4.992, 5.005)
    outcome_semantics = scan_delivery._outcome_interval_semantics(
        best=best, selection=SimpleNamespace(pooled=None, fit_selection_semantics=None),
        interval=None, omega0_profile=None, hesse_interval=hesse_interval, hesse_trusted=True,
        bayes_interval=None, interval_reliable=False)
    assert outcome_semantics["selected_interval_quantity"] == "hesse_interval"

    scores = [_score("stokes+bg", log_evidence=-39.0, posterior=1.0)]
    hyps = {"stokes+bg": _hyp("stokes+bg")}
    row = scan_delivery._candidate_rows(
        scores, {"stokes+bg": best}, hyps, best_label="stokes+bg",
        interval_semantics=outcome_semantics,
        outcome_values={"interval": None, "omega0_profile": None,
                        "hesse_interval": hesse_interval, "bayes_interval": None})[0]

    cell = row["mode_intervals"][0]
    assert cell["source_quantity"] == "hesse_interval"
    assert cell["low"] == 4.992 and cell["high"] == 5.005 and cell["center"] is None
    assert cell["authority"]["selected_for_delivery"] is True

    from isar.scene.model import CandidateRow
    parsed = CandidateRow.from_dict(json.loads(json.dumps(row)))
    assert parsed.mode_intervals[0].center is None
    assert parsed.mode_intervals[0].low == pytest.approx(4.992)
    assert parsed.mode_intervals[0].high == pytest.approx(5.005)


def test_a_result_less_candidate_row_stays_aligned_despite_n_modes_defaulting_to_one():
    """Follow-up correction to Major finding 1: the real writer path for a candidate with no fit
    result at all (``r is None`` — an unscored/failed label, the existing 'ghost' shape)
    defaults ``n_modes`` to 1 while ``omegas`` is honestly ``()``. ``mode_intervals`` must be
    sized off ``omegas`` (``()``), not the defaulted ``n_modes`` (1), or the row is
    slot-misaligned and ``CandidateRow.from_dict`` refuses it."""
    row = scan_delivery._candidate_rows(
        [_score("ghost", log_evidence=-50.0, posterior=0.0)], {}, {"ghost": _hyp("ghost")})[0]
    assert row["n_modes"] == 1
    assert row["omegas"] == []
    assert row["mode_intervals"] == []
    # Round-trips through the DTO the scene adapter actually parses — the point of the fix.
    from isar.scene.model import CandidateRow
    parsed = CandidateRow.from_dict(json.loads(json.dumps(row)))
    assert parsed.omegas == () and parsed.mode_intervals == ()


def test_the_winner_row_projects_the_realized_authority_not_the_candidate_offer():
    """The winner's slot must show the REALIZED delivery decision (selected_for_delivery,
    passed_reliability_gate, realized authority_scopes, selected_interval_quantity) — built by
    the real ``_outcome_interval_semantics`` + ``_realize_interval_authority`` producers, never
    the candidate's own unrealized offer."""
    winner_within = {"freq_interval": (4.1, 4.25, 4.4),
                     "interval_semantics": _real_candidate_interval_semantics()}
    best = _fit_result(n_modes=1, modes=[{"omega0": 4.25}], within_model=winner_within)
    best.label = "stokes+bg"
    best.fit_semantics = {"estimator_kind": "data_likelihood_mle", "effective_constraints": [],
                          "conditioning_fingerprint": "c" * 64}
    outcome_semantics = scan_delivery._outcome_interval_semantics(
        best=best, selection=SimpleNamespace(pooled=None, fit_selection_semantics=None),
        interval=(4.1, 4.25, 4.4), omega0_profile=None, hesse_interval=None, hesse_trusted=False,
        bayes_interval=None, interval_reliable=True)
    assert outcome_semantics["selected_interval_quantity"] == "interval"

    survivor_within = {"freq_interval": (5.0, 5.1, 5.2),
                       "interval_semantics": _real_candidate_interval_semantics((5.0, 5.1, 5.2))}
    scores = [_score("stokes+bg", log_evidence=-39.0, posterior=0.9),
              _score("stokes+bg_alt", log_evidence=-42.0, posterior=0.1)]
    fits = {"stokes+bg": best,
            "stokes+bg_alt": _fit_result(n_modes=1, modes=[{"omega0": 5.1}],
                                         within_model=survivor_within)}
    hyps = {"stokes+bg": _hyp("stokes+bg"), "stokes+bg_alt": _hyp("stokes+bg_alt")}
    rows = {r["label"]: r for r in scan_delivery._candidate_rows(
        scores, fits, hyps, best_label="stokes+bg", interval_semantics=outcome_semantics,
        outcome_values={"interval": (4.1, 4.25, 4.4), "omega0_profile": None,
                        "hesse_interval": None, "bayes_interval": None})}

    winner_cell = rows["stokes+bg"]["mode_intervals"][0]
    assert winner_cell["source_quantity"] == "interval"
    assert winner_cell["authority"]["selected_for_delivery"] is True
    assert "passed_reliability_gate" in winner_cell["authority"]
    assert winner_cell["authority"]["authority_scopes"]["single_fit_delivery"] == "active"

    survivor_cell = rows["stokes+bg_alt"]["mode_intervals"][0]
    assert survivor_cell["source_quantity"] == "freq_interval"
    assert "selected_for_delivery" not in survivor_cell["authority"]
    assert survivor_cell["authority"]["authority_scopes"]["single_fit_delivery"] == "inactive"


def test_a_winner_with_an_unresolvable_selected_quantity_raises_not_silently_degrades():
    """PR 4.2a review, Major finding 2: a ``selected_interval_quantity`` naming something absent
    from the outcome record's own ``quantities``/``outcome_values`` is a writer wiring bug, not a
    genuinely-unavailable interval — it must raise, never quietly fall back to the candidate's
    own unrealized ``freq_interval`` offer (that would mask the defect)."""
    within = {"freq_interval": (4.1, 4.25, 4.4),
             "interval_semantics": _real_candidate_interval_semantics()}
    best = _fit_result(n_modes=1, modes=[{"omega0": 4.25}], within_model=within)
    scores = [_score("stokes+bg", log_evidence=-39.0, posterior=1.0)]
    fits = {"stokes+bg": best}
    hyps = {"stokes+bg": _hyp("stokes+bg")}
    doctored = {"schema": "isar.interval_semantics/1", "quantities": {},
               "selected_interval_quantity": "interval"}
    with pytest.raises(ValueError, match="writer wiring bug"):
        scan_delivery._candidate_rows(
            scores, fits, hyps, best_label="stokes+bg", interval_semantics=doctored,
            outcome_values={"interval": (4.1, 4.25, 4.4)})
    # Missing from outcome_values instead of quantities is the same defect.
    doctored_with_quantity = {
        "schema": "isar.interval_semantics/1",
        "quantities": {"interval": _real_candidate_interval_semantics()["quantities"][
            "freq_interval"]},
        "selected_interval_quantity": "interval"}
    with pytest.raises(ValueError, match="writer wiring bug"):
        scan_delivery._candidate_rows(
            scores, fits, hyps, best_label="stokes+bg",
            interval_semantics=doctored_with_quantity, outcome_values={})


def test_a_record_lacking_nominal_level_fails_loudly_not_a_minted_default():
    """PR 4.2a review, Minor finding 3: every writer that builds this item sets
    ``nominal_level`` unconditionally (``errors._interval_semantics_for``'s ``common`` dict,
    confirmed by reading the source) — it is structurally required, not genuinely optional, so
    strict access is correct and an absent field must fail loudly rather than mint 0.68."""
    item = dict(_real_candidate_interval_semantics()["quantities"]["freq_interval"])
    del item["nominal_level"]
    within = {"freq_interval": (4.1, 4.25, 4.4),
             "interval_semantics": {"quantities": {"freq_interval": item}}}
    best = _fit_result(n_modes=1, modes=[{"omega0": 4.25}], within_model=within)
    scores = [_score("stokes+bg", log_evidence=-39.0, posterior=1.0)]
    hyps = {"stokes+bg": _hyp("stokes+bg")}
    with pytest.raises(KeyError, match="nominal_level"):
        scan_delivery._candidate_rows(scores, {"stokes+bg": best}, hyps)


def test_a_candidate_with_no_enumerated_hypothesis_is_refused():
    """Persisting it would publish a candidate whose composition the scene reads as nothing —
    unpricing its physics silently. select_models cannot produce this; it fails loudly if it does."""
    with pytest.raises(ValueError, match="no enumerated hypothesis"):
        scan_delivery._candidate_rows([_score("orphan", log_evidence=-1.0, posterior=1.0)], {}, {})


def _bands(x, mask=None, center=0.0):
    x = np.asarray(x, dtype=float)
    return scan_delivery._energy_bands(
        x, np.ones(x.size, dtype=bool) if mask is None else np.asarray(mask, dtype=bool), center)


@pytest.mark.parametrize("x,mask,expected", [
    # A fully-kept uniform grid is ONE band, covering half a step past each end bin: the end bin was
    # measured, so its cell is real coverage rather than a point.
    (np.linspace(0.0, 10.0, 11), None, [[-0.5, 10.5]]),
    # One dropped bin opens the hole exactly at that bin's cell — an excluded region is not
    # searched ground, and the two kept runs merge internally because their cells touch.
    ([0.0, 1.0, 2.0, 3.0, 4.0, 5.0], [1, 1, 1, 0, 1, 1], [[-0.5, 2.5], [3.5, 5.5]]),
    # A lone kept bin is a CELL of its neighbours' width, not a zero-width point.
    ([0.0, 1.0, 2.0, 3.0], [0, 1, 0, 0], [[0.5, 1.5]]),
    # Nothing kept is no coverage — not one degenerate band at an arbitrary position.
    ([0.0, 1.0, 2.0], [0, 0, 0], []),
    # A one-bin axis has no neighbour to take a width from; degenerate, and honestly so.
    ([3.0], None, [[3.0, 3.0]]),
])
def test_energy_bands_are_the_merged_cells_of_the_kept_bins(x, mask, expected):
    """Review finding 4, at the writer: coverage is the measured CELLS of the kept bins, and
    adjacency is read off the original grid rather than from a spacing statistic."""
    assert [[pytest.approx(v) for v in b] for b in _bands(x, mask)] == expected


def test_a_stitched_coarse_to_fine_axis_is_fully_covered_with_no_zero_width_runs():
    """The first counterexample to a global-spacing threshold. This axis has no single spacing, so
    any factor times a median splits the coarse half into zero-width points — measured ground that
    ``covers()`` then answers False for. Grid-derived cells have no such failure mode."""
    x = np.concatenate([np.arange(0.0, 5.0, 1.0), np.arange(5.0, 7.0, 0.2)])
    bands = _bands(x)

    assert len(bands) == 1                                  # every bin kept, every cell adjacent
    assert all(hi > lo for lo, hi in bands)                 # no zero-width runs
    # ... and every measured point is inside the coverage, coarse half included.
    assert all(any(lo <= float(v) <= hi for lo, hi in bands) for v in x)


def test_a_comb_rejection_opens_a_hole_at_every_dropped_bin():
    """The second counterexample. Dropping every other bin leaves a perfectly UNIFORM kept spacing,
    so no gap threshold can see the holes at all and every dropped bin is claimed as searched."""
    x = np.arange(0.0, 10.0, 0.5)
    mask = np.zeros(x.size, dtype=bool)
    mask[::2] = True
    bands = _bands(x, mask)

    assert len(bands) == int(mask.sum())                    # one cell each, none merged
    assert all(hi - lo == pytest.approx(0.5) for lo, hi in bands)
    # Every dropped bin sits in a hole, and every kept one inside coverage.
    for v, kept in zip(x, mask):
        assert any(lo <= float(v) <= hi for lo, hi in bands) is bool(kept)


def test_a_non_finite_abscissa_is_a_delivery_defect_not_a_nan_band():
    """A NaN midpoint would persist a bare NaN token in the results.csv JSON cell and a footprint
    covering nothing. It fails here, naming the axis."""
    with pytest.raises(ValueError, match="non-finite"):
        _bands([0.0, float("nan"), 2.0])


def test_energy_bands_are_on_the_omega_scale_like_the_window():
    assert _bands([0.0, 1.0, 2.0], center=2.0) == [[pytest.approx(-2.5), pytest.approx(0.5)]]


@pytest.mark.slow
def test_fit_one_stamps_bands_that_honour_the_qc_mask():
    """End to end through the real fitter: masking out the middle of the axis leaves two runs, and
    the window (the kept POINT extent) sits inside their outer edges."""
    ctx = _single_peak_ctx()
    mask = np.asarray(ctx.mask, dtype=bool).copy()
    hole = (ctx.x > 2.0) & (ctx.x < 6.0)
    mask &= ~hole
    fit = scan_delivery.fit_one(dataclasses.replace(ctx, mask=mask), _card(), prior=None,
                                M=20, seed=1)

    bands = fit["energy_bands"]
    lo, hi = fit["energy_window"]
    assert len(bands) == 2
    assert bands[0][1] <= 6.0 and bands[1][0] >= 2.0        # the hole is between the two runs
    # The bands are CELLS, so they reach half a bin outside the kept points the window reports.
    assert bands[0][0] < lo and bands[-1][1] > hi


def test_energy_bands_roundtrip_through_the_ledger_and_results_csv(tmp_path):
    """A JSON cell, not float columns: the run list is variable-length, and reading the envelope
    instead would present the holes as searched coverage."""
    outcome = _outcome_with(None, None)
    outcome.energy_window = (-8.0, 16.0)
    outcome.energy_bands = [[-8.0, -2.0], [2.0, 16.0]]
    restored = driver._coerce_fit_outcome(json.loads(json.dumps(outcome.to_dict())))
    assert restored.energy_bands == [[-8.0, -2.0], [2.0, 16.0]]

    row = _write_read(tmp_path / "a", outcome)
    assert row["energy_bands"] == [[-8.0, -2.0], [2.0, 16.0]]
    assert _write_read(tmp_path / "b", _outcome_with(None, None))["energy_bands"] is None


def test_energy_window_roundtrips_through_the_ledger_and_results_csv(tmp_path):
    """The measured window is the scene's observation footprint — a searched-and-empty window is
    evidence, so it must survive both persistence projections."""
    outcome = _outcome_with(None, None)
    outcome.energy_window = (-8.0, 16.0)
    restored = driver._coerce_fit_outcome(json.loads(json.dumps(outcome.to_dict())))
    assert list(restored.energy_window) == [-8.0, 16.0]

    row = _write_read(tmp_path / "a", outcome)
    # Floats, not strings: a new float column also needs its _NUMERIC_COLUMNS entry (the trap).
    assert row["energy_lo"] == pytest.approx(-8.0) and row["energy_hi"] == pytest.approx(16.0)
    assert isinstance(row["energy_lo"], float) and isinstance(row["energy_hi"], float)

    absent = _write_read(tmp_path / "b", _outcome_with(None, None))
    assert absent["energy_lo"] is None and absent["energy_hi"] is None


def test_evidence_identity_survives_to_results_csv(tmp_path):
    """Declaring a column is not populating it (the OP10 bug class), and the string column needs
    no _NUMERIC_COLUMNS entry — it must read back verbatim."""
    assert _write_read(tmp_path / "a", _scene1_outcome())["evidence_identity"] == "acoustic"
    # The identity-agnostic fit every production catalogue emits today writes an empty cell.
    assert _write_read(tmp_path / "b", _outcome_with(None, None))["evidence_identity"] is None
