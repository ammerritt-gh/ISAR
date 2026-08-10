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
from types import SimpleNamespace

import numpy as np
import pytest

from isar.analyze.components import CompositeModel, ConstantBackground, Elastic, PhononDHO
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
            "at_limit", "variant", "identity", "effective_constraints"}
    for row in cands:
        assert set(row) == keys
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

    # SCENE-3 observation footprint: the calibrated, masked window the cost actually saw.
    assert fit["energy_window"] == (pytest.approx(float(ctx.x_used.min())),
                                    pytest.approx(float(ctx.x_used.max())))

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


def _fit_result(*, at_limit=None, variant=None, identity=None, params=None, n_modes=1):
    return SimpleNamespace(label="x", cost_per_dof=1.04, n_modes=n_modes, analysis_usable=True,
                           at_limit=at_limit, variant=variant, identity=identity,
                           params=params)


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
    rows = {r["label"]: r for r in scan_delivery._candidate_rows(scores, fits)}

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
    row = scan_delivery._candidate_rows([score], {})[0]
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
    rows = {r["label"]: r for r in scan_delivery._candidate_rows(scores, fits)}

    assert rows["stokes+bg@a"]["identity"] == "a" and rows["stokes+bg@b"]["identity"] == "b"
    assert rows["stokes+bg@a"]["log_evidence"] == pytest.approx(-40.5)
    assert rows["stokes+bg@b"]["log_evidence"] == pytest.approx(-39.25)


def _scene1_outcome():
    outcome = _outcome_with(None, None)
    outcome.at_limit = {"phonon.Omega_d": False, "phonon.Gamma": True}
    outcome.evidence_identity = "acoustic"
    outcome.identity_ba_disagreement = {"evidence_identity": "acoustic", "branch_id": "optic"}
    outcome.interpretation_ref = "sha256:deadbeef"
    return outcome


def test_scene1_outcome_fields_roundtrip_to_dict_from_dict():
    restored = driver._coerce_fit_outcome(json.loads(json.dumps(_scene1_outcome().to_dict())))
    assert restored.at_limit == {"phonon.Omega_d": False, "phonon.Gamma": True}
    assert restored.evidence_identity == "acoustic"
    assert restored.identity_ba_disagreement == {"evidence_identity": "acoustic",
                                                 "branch_id": "optic"}
    assert restored.interpretation_ref == "sha256:deadbeef"


def test_scene1_outcome_fields_default_absent():
    """SCENE-3 owns the interpretation store; PR-c only reserves the pointer."""
    oc = _outcome_with(None, None)
    assert (oc.at_limit, oc.evidence_identity, oc.identity_ba_disagreement,
            oc.interpretation_ref) == (None, None, None, None)


# --------------------------------------------------------------- SCENE-3 (scene-layer inputs)
def test_candidate_rows_carry_every_candidates_mode_positions():
    """The scene places the modes of NON-winning candidates too, and only the winner's ``modes``
    are persisted elsewhere. Ω_d is read off the candidate's own params, ascending; a phonon-less
    structure carries [] rather than a fabricated zone-centre energy."""
    scores = [_score("stokes2+bg", log_evidence=-39.0, posterior=0.6),
              _score("bg", log_evidence=-44.0, posterior=0.4)]
    fits = {
        "stokes2+bg": _fit_result(n_modes=2, params={"phonon2.Omega_d": 9.5, "phonon2.Gamma": 0.3,
                                                     "phonon2.A": 100.0, "phonon.Omega_d": 4.25,
                                                     "phonon.Gamma": 0.4, "phonon.A": 500.0,
                                                     "bg.BG": 8.0}),
        "bg": _fit_result(n_modes=0, params={"bg.BG": 8.0}),
    }
    rows = {r["label"]: r for r in scan_delivery._candidate_rows(scores, fits)}

    assert rows["stokes2+bg"]["omegas"] == [4.25, 9.5]
    assert len(rows["stokes2+bg"]["omegas"]) == rows["stokes2+bg"]["n_modes"]
    assert rows["bg"]["omegas"] == []
    # An unscored/failed candidate has no params to read: absence, never NaN.
    assert scan_delivery._candidate_rows([_score("ghost", log_evidence=-50.0, posterior=0.0)],
                                         {})[0]["omegas"] == []
    assert json.loads(json.dumps(list(rows.values())))


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
