"""Phase 5b-live Part A — the session persists the cross-q branch into card.state so the drive can
read the prediction durably from card.state.json (crash-safe, no session-only object)."""
from types import SimpleNamespace

from isar.schema.card import CardState
from isar.schema.hypotheses import resolve_hypothesis
from isar.session import branch_model
from isar.session.evidence_update import (_rebuild_tracked_prior, _store_branch_beliefs,
                                          _store_branch_models, _update_hypothesis_mismatch)


def _pt(sample, T, w, g, a, *, fwhm=0.5, fit_kind="unconstrained"):
    """A clean dispersion-prior anchor (mirrors session_prior.add_point's stored shape)."""
    return {"scan_id": f"{sample}-{w}", "sample": sample, "T": T, "t_bin": float(T),
            "q": [w, 0.0, 0.0], "omega0": float(w), "modality": "fold", "verdict": "resolved",
            "gamma": float(g), "fit_kind": fit_kind,
            "intensity": {"value": float(a), "gauss_fwhm": fwhm, "lorentz_fwhm": 0.1,
                          "exposure": 1e7, "counting_mode": "monitor", "instrument": "x"}}


def test_store_branch_models_populates_and_roundtrips():
    state = CardState(dispersion_prior=[
        _pt("A", 300, 4.0, 0.30, 0.25), _pt("A", 300, 5.0, 0.30, 0.20),
        _pt("A", 300, 6.0, 0.30, 0.16)])
    _store_branch_models(state, t_round=5.0)

    assert len(state.branch_models) == 1
    bm = state.branch_models[0]
    assert bm["sample"] == "A" and bm["branch_id"] == branch_model.BRANCH_ID
    assert bm["w_min"] == 4.0 and bm["w_max"] == 6.0 and bm["n_anchors"] == 3
    # The persisted dict is exactly what the drive will read + predict from.
    assert branch_model.predict(bm, 5.0).gamma > 0
    # Round-trips through card.state.json (the durable file the drive reads).
    assert CardState.from_dict(state.to_dict()).branch_models == state.branch_models


def test_store_branch_models_replaces_by_key_not_accretes():
    state = CardState(dispersion_prior=[
        _pt("A", 300, 4.0, 0.30, 0.25), _pt("A", 300, 5.0, 0.30, 0.20),
        _pt("A", 300, 6.0, 0.30, 0.16)])
    _store_branch_models(state, t_round=5.0)
    _store_branch_models(state, t_round=5.0)     # a re-run must refresh, not duplicate
    assert len(state.branch_models) == 1


def test_store_branch_models_skips_when_too_sparse():
    # Fewer than MIN_ANCHORS clean anchors → build_branch returns None → nothing stored (graceful).
    state = CardState(dispersion_prior=[_pt("A", 300, 4.0, 0.30, 0.25)])
    _store_branch_models(state, t_round=5.0)
    assert state.branch_models == []


def test_store_branch_models_separates_samples():
    state = CardState(dispersion_prior=[
        _pt("A", 300, 4.0, 0.30, 0.25), _pt("A", 300, 5.0, 0.30, 0.20),
        _pt("A", 300, 6.0, 0.30, 0.16),
        _pt("B", 300, 4.0, 0.40, 0.25), _pt("B", 300, 5.0, 0.40, 0.20),
        _pt("B", 300, 6.0, 0.40, 0.16)])
    _store_branch_models(state, t_round=5.0)
    samples = sorted(bm["sample"] for bm in state.branch_models)
    assert samples == ["A", "B"]


def test_branch_beliefs_and_wrong_preset_warning_persist_in_machine_state():
    hypothesis = resolve_hypothesis(
        "optic", {"zc_energy": {"mode": 10.0, "sigma": 0.2}, "confidence": 1.0}).to_dict()
    points = []
    for i, (u, energy) in enumerate(((0.0, 3.0), (0.1, 3.1), (0.2, 3.2))):
        points.append({"scan_id": f"O:{i}", "sample": "A", "T": 300.0, "t_bin": 300.0,
                       "q": [2.0, u, 0.0], "omega0": energy, "branch_id": "optic",
                       "weight": 1.0, "modality": "fold", "verdict": "resolved"})
    state = CardState(dispersion_prior=points,
                      hypothesis_snapshot={"target_hypothesis": "optic",
                                           "fingerprint": "frozen",
                                           "steering": {"mismatch_z_threshold": 3.0}})
    _store_branch_beliefs(state, 5.0, period=2.0, hypotheses=[hypothesis])
    _update_hypothesis_mismatch(state, 5.0, period=2.0, hypotheses=[hypothesis])
    optic = next(row for row in state.branch_belief_models if row["branch_id"] == "optic")
    assert optic["n_anchors"] == 3
    flat = next(row for row in state.branch_belief if row["branch_id"] == "optic")
    assert "label_probabilities" in flat and "p_is_target" in flat
    assert state.hypothesis_mismatch["active"] is True
    assert state.hypothesis_mismatch["comparison"] == "prior_vs_data_only_posterior_predictive"
    assert CardState.from_dict(state.to_dict()).hypothesis_mismatch["active"] is True
    first = dict(state.hypothesis_mismatch)
    state.dispersion_prior = list(reversed(state.dispersion_prior))
    _update_hypothesis_mismatch(state, 5.0, period=2.0, hypotheses=[hypothesis])
    assert state.hypothesis_mismatch == first


def test_branch_belief_preserves_complete_posterior_and_legacy_fallback():
    complete = _pt("A", 300, 4.0, 0.3, 0.2)
    complete.update({"branch_id": "optic", "weight": 0.7,
                     "branch_posterior": {"acoustic": 0.2, "optic": 0.7, "new": 0.1},
                     "assignment_entropy": 123.0})
    legacy = _pt("A", 300, 5.0, 0.3, 0.2)
    legacy.update({"branch_id": "optic", "weight": 0.75})
    state = CardState(
        dispersion_prior=[complete, legacy],
        hypothesis_snapshot={"target_hypothesis": "optic"})
    _store_branch_beliefs(state, 5.0, period=2.0)
    by_scan = {row["scan_id"]: row for row in state.branch_belief}
    exact = by_scan[complete["scan_id"]]
    assert exact["label_probabilities"] == complete["branch_posterior"]
    assert exact["p_is_target"] == 0.7
    assert exact["assignment_entropy"] != 123.0  # derived from the normalized complete vector
    old = by_scan[legacy["scan_id"]]
    assert old["label_probabilities"] == {"optic": 0.75, "new": 0.25}


def test_rebuild_tracked_prior_leaves_foreign_tracked_points_at_their_own_revision():
    # A tracked point sourced outside this campaign survives the source-id filter by design. It was
    # not rebuilt in this call, so relabelling it with the current revision would advertise evidence
    # freshness that never happened — the stamp belongs only to the points just added. The confirmed
    # assignment below exercises the add path, so this test also fails if the stamp loop is deleted.
    foreign = {"scan_id": "foreign_scan#tracked:optic", "track_revision": 1,
               "sample": "x", "T": 300.0, "q": [0.5, 0.0, 0.0], "omega0": 4.0,
               "modality": "fold", "verdict": "resolved"}
    card = SimpleNamespace(state=CardState(dispersion_prior=[foreign]))
    outcome = {"pooled": True, "modality": "fold", "verdict": "resolved",
               "sample": "x", "T": 300.0, "q": [0.25, 0.0, 0.0], "fit_kind": "dho"}
    refs = {"master:s100:D": (SimpleNamespace(provenance={}), "D", outcome)}
    track = {"revision": 7, "assignments": [{
        "track_state": "confirmed", "optic_candidate_index": 0, "assignment_entropy": 0.1,
        "candidates": [{"source_scan_id": "s100", "energy_meV": 4.2, "gamma": 0.3,
                        "probabilities": {"optic": 1.0}}]}]}

    _rebuild_tracked_prior(card, None, track,
                           [{"authority": "master:s100:D"}], refs, t_round=0.0)

    by_id = {p["scan_id"]: p for p in card.state.dispersion_prior}
    assert by_id["s100#tracked:optic"]["track_revision"] == 7
    assert foreign["track_revision"] == 1

