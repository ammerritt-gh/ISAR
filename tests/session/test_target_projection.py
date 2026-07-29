"""The separated, authority-bound target projection (``isar.target_projection/2``, ADR-019).

The defect these tests pin: the projection used to be computed inside ``/2`` at fit time, against
the fit's own LOCAL best ω₀, and marked ``complete=true`` — before the ``signal_unmodeled``
conversion, before branch triage could withhold a delivery, and before the connected tracker
reassigned or cleared the target. Every one of those three can move the target, so a projection
resolved before them can name the wrong mode with full confidence.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from isar.analyze.context import ScanContext
from isar.session import driver, predictive_artifact
from isar.session.predictive_artifact import build_master_predictive, build_target_projection

_TWO_MODE_MANIFEST = {
    "schema": "isar.model_manifest/1",
    "components": [{"type": "phonon_dho", "prefix": "phonon", "peak": "stokes+astokes"},
                   {"type": "phonon_dho", "prefix": "phonon2", "peak": "stokes+astokes"},
                   {"type": "constant_background", "prefix": "bg"}],
    "parameters": {"phonon.Omega_d": 2.0, "phonon.Gamma": 0.3, "phonon.A": 10.0,
                   "phonon2.Omega_d": 5.0, "phonon2.Gamma": 0.3, "phonon2.A": 8.0,
                   "bg.BG": 4.0},
}
_TWO_MODE_NAMES = ["phonon.Omega_d", "phonon.Gamma", "phonon.A",
                   "phonon2.Omega_d", "phonon2.Gamma", "phonon2.A", "bg.BG"]
_TWO_MODE_DRAWS = [[2.0, 0.3, 10.0, 5.0, 0.3, 8.0, 4.0],
                   [2.1, 0.3, 10.5, 5.1, 0.3, 8.2, 4.2]]

_AUTHORITY = {"master_revision": "chash-1", "calibration_fingerprint": "cal-1",
              "policy_fingerprint": "pol-1"}


def _pool_context(n_points=5):
    monitor = np.full(n_points, 1e9)
    reference = float(np.median(monitor))
    return ScanContext(x=np.linspace(-2.0, 8.0, n_points), counts=np.zeros(n_points),
                       mask=np.ones(n_points, bool), T=100.0, center=0.0, gauss_fwhm=0.4,
                       lorentz_fwhm=0.1, instrument="IN8", energy_status="absolute",
                       exposure_scale=monitor / reference, exposure_reference=reference)


def _two_mode_artifact(monkeypatch, *, local_best: float):
    """A real ``/2`` produced by the real producer, whose fit-local best is ``local_best``."""
    monkeypatch.setattr(predictive_artifact, "model_manifest_of",
                        lambda result, by_hyp: _TWO_MODE_MANIFEST)
    monkeypatch.setattr(predictive_artifact, "bounded_parameter_draws", lambda result, **kwargs: (
        tuple(_TWO_MODE_NAMES), np.asarray(_TWO_MODE_DRAWS, dtype=float), "laplace_hesse",
        {"draw_weighting": "equal_weight_resampled", "draw_weights": None,
         "draw_ids": ["draw-0", "draw-1"], "source_draw_count": 2, "retained_draw_count": 2,
         "dropped_draws": [], "draw_mass_retained": 1.0, "draw_cap": 64,
         "thinned_from_draw_count": 2}))
    fits = {"two-mode": SimpleNamespace(label="two-mode", omega0=local_best)}
    return build_master_predictive(
        SimpleNamespace(omega0=local_best), [SimpleNamespace(label="two-mode", weight=1.0)],
        fits, {"two-mode": object()}, _pool_context(), seed=11, n_members=1)


def _hand_artifact(mode_energies, *, label="m", weight=1.0, gauss_fwhm=0.4):
    # The evaluation context is not decoration: the nearest-mode rule is resolution-relative, and a
    # published /2 always carries a positive gaussian_fwhm_meV (the strict validator refuses one
    # that does not). ``gauss_fwhm=None`` builds the artifact that omits it, on purpose.
    context = (None if gauss_fwhm is None else
               {"center_meV": 0.0, "gaussian_fwhm_meV": gauss_fwhm, "lorentzian_fwhm_meV": 0.1,
                "temperature_K": 300.0})
    return {"schema": "isar.master_predictive/2", "predictive_basis_fingerprint": "fp",
            "evaluation_context": context,
            "models": [{"label": label, "identity_label": label, "weight": weight,
                        "mode_energies": mode_energies}]}


def _outcome(**overrides):
    """A delivered, tracker-confirmed pooled master row."""
    base = {"pooled": True, "verdict": "resolved", "fit_kind": "resolved", "omega0": 5.0,
            "target_delivery_eligible": True, "delivery_withheld_reason": None,
            "track_revision": 3, "track_state": "confirmed", "candidate_state": "confirmed",
            "candidate_id": "cand-b", "selected_candidate_index": 1}
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# The core defect: locally nearest is not the authoritative target
# ---------------------------------------------------------------------------
def test_the_projection_follows_the_tracked_target_not_the_fits_local_best(monkeypatch):
    payload = _two_mode_artifact(monkeypatch, local_best=2.0)
    # /2 is unchanged and still fit-local: omega0_draws is the mode nearest the fit's own ω₀, which
    # is what the outer action scorer consumes.
    modes = payload["models"][0]["mode_energies"]
    assert [len(row) for row in modes] == [2, 2]
    assert payload["models"][0]["omega0_draws"] == pytest.approx([row[0] for row in modes])
    assert "target_projection" not in payload

    projection = build_target_projection(payload, _outcome(omega0=5.0), **_AUTHORITY)
    assert projection["schema"] == "isar.target_projection/2"
    assert projection["complete"] is True and projection["reason"] is None
    assert projection["reference_energy_meV"] == 5.0
    model = projection["models"][0]
    assert model["target_present_by_draw"] == [True, True]
    assert model["target_qoi_draws"] == pytest.approx([row[1] for row in modes])
    # The whole point: the old in-/2 projection would have published the 2.0 mode here.
    assert model["target_qoi_draws"] != pytest.approx(payload["models"][0]["omega0_draws"])


def test_mode_identity_can_switch_between_draws_without_switching_the_target():
    # Composite ORDER is not identity: one draw's second phonon can be the other draw's first.
    artifact = _hand_artifact([[2.0, 5.0], [5.1, 1.9], [4.9, 2.05]])
    projection = build_target_projection(artifact, _outcome(omega0=5.0), **_AUTHORITY)
    assert projection["complete"] is True
    assert projection["models"][0]["target_qoi_draws"] == pytest.approx([5.0, 5.1, 4.9])


def test_two_equidistant_modes_have_no_unique_target_and_refuse_to_guess():
    artifact = _hand_artifact([[4.0, 6.0]])
    projection = build_target_projection(artifact, _outcome(omega0=5.0), **_AUTHORITY)
    assert projection["complete"] is False
    assert projection["reason"] == "no_unique_mode"
    assert projection["models"][0]["target_present_by_draw"] == [False]


# ---------------------------------------------------------------------------
# Nearest-mode selection is resolution-relative, and bounded
# ---------------------------------------------------------------------------
def test_a_reference_far_from_every_mode_is_not_projected_onto_the_least_far_one():
    """Nearest-energy selection with no distance gate answers confidently from arbitrarily far away.

    An authoritative ω₀ of 99.0 meV against modes at {2.0, 5.0} was published as ``complete=true``
    naming the 5.0 mode — a QoI about a target the model does not contain, bound to a full authority
    basis and therefore trusted downstream.
    """
    projection = build_target_projection(
        _hand_artifact([[2.0, 5.0]]), _outcome(omega0=99.0), **_AUTHORITY)
    assert projection["complete"] is False
    assert projection["reason"] == "reference_beyond_mode_support"
    assert projection["models"][0]["target_present_by_draw"] == [False]
    assert projection["models"][0]["target_qoi_draws"] == [None]


@pytest.mark.parametrize("reference,complete", [
    (5.0, True),          # on the mode
    (6.1, True),          # 1.1 meV away, inside 3 x 0.4 meV FWHM
    (6.19, True),         # just inside
    (6.21, False),        # just outside
    (6.3, False),         # 1.3 meV away
])
def test_the_support_distance_is_three_resolution_fwhm(reference, complete):
    projection = build_target_projection(
        _hand_artifact([[2.0, 5.0]]), _outcome(omega0=reference), **_AUTHORITY)
    assert projection["complete"] is complete
    assert projection["reason"] == (None if complete else "reference_beyond_mode_support")
    # The gate scales with the declared resolution: a coarser instrument reaches further.
    wide = build_target_projection(
        _hand_artifact([[2.0, 5.0]], gauss_fwhm=4.0), _outcome(omega0=reference), **_AUTHORITY)
    assert wide["complete"] is True


def test_the_rule_the_projection_ran_under_is_recorded_in_the_projection():
    projection = build_target_projection(
        _hand_artifact([[2.0, 5.0]]), _outcome(omega0=5.0), **_AUTHORITY)
    assert projection["mode_selection"] == {
        "resolution_gaussian_fwhm_meV": 0.4,
        "support_fwhm_multiple": 3.0,
        "max_reference_distance_meV": pytest.approx(1.2),
        "ambiguity_fwhm_fraction": 1e-6,
        "ambiguity_tolerance_meV": pytest.approx(4e-7),
    }


def test_the_ambiguity_tolerance_is_resolution_relative_not_a_bare_float_comparison():
    # 2e-7 meV of asymmetry at a 0.4 meV resolution is not a measurable preference for either mode;
    # the old absolute 1e-9 tolerance declared it decisive and published one of them as the QoI.
    tie = build_target_projection(
        _hand_artifact([[5.0 - 1.0, 5.0 + 1.0 + 2e-7]]), _outcome(omega0=5.0), **_AUTHORITY)
    assert tie["complete"] is False and tie["reason"] == "no_unique_mode"
    # A separation an order of magnitude above the tolerance still resolves.
    resolved = build_target_projection(
        _hand_artifact([[5.0 - 1.0, 5.0 + 1.0 + 1e-5]]), _outcome(omega0=5.0), **_AUTHORITY)
    assert resolved["complete"] is True
    assert resolved["models"][0]["target_qoi_draws"] == pytest.approx([4.0])


def test_an_artifact_declaring_no_resolution_cannot_establish_mode_support():
    # Fail closed. A published /2 always carries a positive gaussian_fwhm_meV, so this state means
    # the basis is invalid — and disabling the gate would restore the unconditional nearest-mode
    # rule for exactly the artifacts least entitled to it.
    projection = build_target_projection(
        _hand_artifact([[2.0, 5.0]], gauss_fwhm=None), _outcome(omega0=5.0), **_AUTHORITY)
    assert projection["complete"] is False
    assert projection["reason"] == "no_resolution_basis_for_mode_support"
    assert projection["mode_selection"]["resolution_gaussian_fwhm_meV"] is None


# ---------------------------------------------------------------------------
# Every state that must NOT produce a complete projection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("outcome,reason", [
    # The tracker cleared the selection: this q is not connected to the confirmed trajectory.
    (_outcome(candidate_state="ambiguous", track_state="ambiguous", omega0=None,
              selected_candidate_index=None), "ambiguous_track"),
    # Confirmed identity, but the only supporting mode came from an auxiliary probe.
    (_outcome(target_delivery_eligible=False, delivery_withheld_reason="auxiliary_only_selection",
              omega0=None, selected_candidate_index=None), "auxiliary_only_selection"),
    # Branch triage declined the delivery before the tracker ever saw the row.
    (_outcome(verdict="branch_continuation", omega0=None, track_revision=None,
              track_state=None, candidate_state=None), "withheld_delivery"),
    # Descriptor-only: real signal the phonon model cannot fit, so there is no ω₀ at all.
    (_outcome(verdict="signal_unmodeled", fit_kind="descriptor_only", omega0=None,
              track_revision=None, track_state=None, candidate_state=None), "descriptor_only"),
    # No tracker at all and no delivered ω₀.
    (_outcome(omega0=None, track_revision=None, track_state=None, candidate_state=None,
              selected_candidate_index=None), "no_reference_target_energy"),
])
def test_a_target_that_is_not_authoritative_yields_an_incomplete_projection(outcome, reason):
    artifact = _hand_artifact([[2.0, 5.0], [2.1, 5.1]])
    projection = build_target_projection(artifact, outcome, **_AUTHORITY)
    assert projection["complete"] is False and projection["reason"] == reason
    assert not any(projection["models"][0]["target_present_by_draw"])
    assert projection["models"][0]["target_qoi_draws"] == [None, None]


def test_a_null_model_alone_carries_no_target_mode():
    projection = build_target_projection(_hand_artifact([[], []]), _outcome(), **_AUTHORITY)
    assert projection["complete"] is False
    assert projection["reason"] == "no_retained_model_carries_the_target_mode"


def test_a_null_model_beside_a_phonon_model_is_a_legitimate_target_absent_state():
    artifact = _hand_artifact([[2.0, 5.0], [2.1, 5.1]], label="two-mode", weight=0.7)
    artifact["models"].append({"label": "null", "identity_label": "null", "weight": 0.3,
                               "mode_energies": [[], []]})
    projection = build_target_projection(artifact, _outcome(omega0=5.0), **_AUTHORITY)
    assert projection["complete"] is True
    assert projection["models"][0]["target_present_by_draw"] == [True, True]
    assert projection["models"][1]["target_present_by_draw"] == [False, False]


# ---------------------------------------------------------------------------
# Authority binding
# ---------------------------------------------------------------------------
def test_a_new_track_revision_reprojects_the_same_2_without_a_refit(monkeypatch):
    payload = _two_mode_artifact(monkeypatch, local_best=2.0)
    fingerprint = payload["predictive_basis_fingerprint"]

    first = build_target_projection(
        payload, _outcome(omega0=2.0, track_revision=3, candidate_id="cand-a",
                          selected_candidate_index=0), **_AUTHORITY)
    # The tracker later reassigns this q to the other branch. No fit has run in between.
    second = build_target_projection(
        payload, _outcome(omega0=5.0, track_revision=4, candidate_id="cand-b",
                          selected_candidate_index=1), **_AUTHORITY)

    assert payload["predictive_basis_fingerprint"] == fingerprint
    assert first["predictive_basis_fingerprint"] == second["predictive_basis_fingerprint"]
    assert first["authority"]["track_revision"] == 3
    assert second["authority"]["track_revision"] == 4
    assert first["authority"]["candidate_id"] == "cand-a"
    assert second["authority"]["candidate_id"] == "cand-b"
    modes = payload["models"][0]["mode_energies"]
    assert first["models"][0]["target_qoi_draws"] == pytest.approx([row[0] for row in modes])
    assert second["models"][0]["target_qoi_draws"] == pytest.approx([row[1] for row in modes])


def test_the_projection_records_the_full_authority_basis_it_was_decided_under():
    projection = build_target_projection(
        _hand_artifact([[2.0, 5.0], [2.1, 5.1]]), _outcome(), action_role="master", **_AUTHORITY)
    assert projection["authority"] == {
        "action_role": "master",
        "master_revision": "chash-1", "track_revision": 3, "track_state": "confirmed",
        "candidate_id": "cand-b", "candidate_state": "confirmed",
        "selected_candidate_index": 1,
        "calibration_fingerprint": "cal-1", "policy_fingerprint": "pol-1"}
    assert projection["qoi"] == "omega0"
    assert projection["projection_version"] == "isar.omega0_authoritative_mode/2"
    # ``action_role`` lives INSIDE the authority block, and nowhere else: published beside it, the
    # documented "any authority field differs ⇒ stale" test could not see a role change.
    assert "action_role" not in projection


def test_a_role_change_alone_makes_a_stored_projection_stale():
    # The staleness test a consumer is documented to run is a comparison of the authority block. A
    # projection resolved for the master and one resolved for a rival probe are different bindings
    # of the same fit, and that comparison has to say so.
    artifact = _hand_artifact([[2.0, 5.0], [2.1, 5.1]])
    master = build_target_projection(artifact, _outcome(), action_role="master", **_AUTHORITY)
    probe = build_target_projection(
        artifact, _outcome(), action_role="identity_rival_probe", **_AUTHORITY)
    assert master["authority"] != probe["authority"]
    assert probe["authority"]["action_role"] == "identity_rival_probe"


# ---------------------------------------------------------------------------
# Producer isolation: a predictive failure never damages the science
# ---------------------------------------------------------------------------
def test_a_predictive_construction_fault_leaves_a_valid_fit(monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("synthetic predictive fault")

    monkeypatch.setattr(predictive_artifact, "build_master_predictive", explode)
    fit = {"predictive_inputs": {"best": None, "scores": [], "fits": {}, "by_hyp": {},
                                 "seed": 1, "n_members": 1}}
    payload, unavailable = driver._build_predictive_payload(fit, _pool_context(), "master:X")
    assert payload is None
    assert unavailable["reason_code"] == "predictive_construction_failed"
    assert unavailable["error_type"] == "RuntimeError"
    assert "synthetic predictive fault" in unavailable["message"]


def test_no_retained_models_is_a_structured_reason_not_a_silent_absence(monkeypatch):
    monkeypatch.setattr(predictive_artifact, "build_master_predictive",
                        lambda *args, **kwargs: None)
    fit = {"predictive_inputs": {"best": None, "scores": [], "fits": {}, "by_hyp": {},
                                 "seed": 1, "n_members": 1}}
    payload, unavailable = driver._build_predictive_payload(fit, _pool_context(), "master:X")
    assert payload is None and unavailable["reason_code"] == "no_retained_models"


@pytest.mark.slow
def test_a_real_campaign_still_fits_when_the_predictive_producer_always_fails(tmp_path,
                                                                             monkeypatch):
    """End-to-end: the scientific fit path is provably unaffected by a predictive failure."""
    from isar.parsers import ill_in8
    from isar.session.workspace import Workspace
    from isar.synth_format import run_builder

    def explode(*args, **kwargs):
        raise RuntimeError("synthetic predictive fault")

    monkeypatch.setattr(predictive_artifact, "build_master_predictive", explode)
    run = run_builder.build_in8_run("PREDFAULT", n_scans=2, seed0=54500, first_numor=54500,
                                    q_point=[2.25, 0.05, 0.0])
    data = tmp_path / "PREDFAULT.numor"
    data.write_bytes(ill_in8.emit_file(run, ill_in8.card_template()))
    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir(parents=True, exist_ok=True)
    ws.policy_path.write_text(Path(ill_in8._CARD_PATH).read_text(encoding="utf-8"),
                              encoding="utf-8")
    driver.run_batch(ws, [data], M=25, detectors="det0", formats=(), workers=1,
                     errors_tier="fast")
    _card, session = ws.load()
    masters = {key: entry for key, entry in session.scans.items() if key.startswith("master:")}
    assert masters, "the master must still be fitted"
    for entry in masters.values():
        assert entry.status == "fitted"
        assert entry.provenance["predictive_unavailable"]["reason_code"] == (
            "predictive_construction_failed")
        for outcome in entry.detectors.values():
            assert outcome.get("predictive_ref") is None
            assert outcome["verdict"]


@pytest.mark.slow
def test_a_real_campaign_publishes_a_projection_reachable_through_the_facade(tmp_path):
    from isar.parsers import ill_in8
    from isar.session.workspace import Workspace
    from isar.synth_format import run_builder
    from isar.workspace_api import Workspace as ReadOnlyWorkspace

    run = run_builder.build_in8_run("PROJ", n_scans=2, seed0=54500, first_numor=54500,
                                    q_point=[2.25, 0.05, 0.0])
    data = tmp_path / "PROJ.numor"
    data.write_bytes(ill_in8.emit_file(run, ill_in8.card_template()))
    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir(parents=True, exist_ok=True)
    ws.policy_path.write_text(Path(ill_in8._CARD_PATH).read_text(encoding="utf-8"),
                              encoding="utf-8")
    driver.run_batch(ws, [data], M=25, detectors="det0", formats=(), workers=1,
                     errors_tier="fast")
    _card, session = ws.load()
    refs = [outcome["target_projection_ref"]
            for key, entry in session.scans.items() if key.startswith("master:")
            for outcome in entry.detectors.values() if outcome.get("target_projection_ref")]
    assert refs, "a pooled master must publish a target projection"
    payload = json.loads((ws.root / refs[0]).read_text(encoding="utf-8"))
    assert payload["schema"] == "isar.target_projection/2"
    assert payload["authority"]["master_revision"]
    assert payload["complete"] in (True, False)
    # Reachable through the read-only facade, which is what an EIG-3/4 consumer will use.
    facade = ReadOnlyWorkspace.open(ws.root)
    assert facade.target_projection(refs[0]) == payload
    assert facade.target_projection(None) is None


@pytest.mark.parametrize("ambiguous_first", [True, False])
def test_one_ambiguous_model_blanks_the_whole_projection_regardless_of_order(ambiguous_first):
    # Blanking as the loop went would have left the models AHEAD of the ambiguous one carrying real
    # draws — an order-dependent artifact, and a partially-populated one invites a consumer to score
    # the half it happens to like.
    clean = {"label": "clean", "identity_label": "clean", "weight": 0.5,
             "mode_energies": [[2.0, 5.0]]}
    ambiguous = {"label": "ambiguous", "identity_label": "ambiguous", "weight": 0.5,
                 "mode_energies": [[4.0, 6.0]]}
    models = [ambiguous, clean] if ambiguous_first else [clean, ambiguous]
    artifact = {**_hand_artifact([]), "models": models}
    projection = build_target_projection(artifact, _outcome(omega0=5.0), **_AUTHORITY)
    assert projection["complete"] is False
    assert projection["reason"] == "no_unique_mode"
    assert all(not any(model["target_present_by_draw"]) for model in projection["models"])
    assert all(model["target_qoi_draws"] == [None] for model in projection["models"])
