"""Acceptance tests for the phenomenon belief layer (SCENE-5 slice 5.0).

Fixtures are hand-rolled ``isar.scene-revision/1``-shaped dicts, built from the real DTOs
(``SceneObservation``, ``CandidateRow``, ...) so their ``to_dict()`` shape is exactly what a real
revision persists, then assembled into ``clusters``/``alternatives`` payload dicts mirroring
``isar.scene.build``'s writer (never running ``build_scene`` itself — this module is offline and
does not need a real scene builder to exercise).
"""
import math

import numpy as np
import pytest

from isar.scene.model import (CandidateRow, ComponentDescriptor, Footprint, ModeInterval,
                              SampleKnowledge, SceneObservation)
from isar.scene.phenomena import (CURVE, FAMILIES, PhenomenonBelief, Anchor,
                                  ContinuumRefusesPrediction, Design, PhenomenonFamily,
                                  build_beliefs, family_manifest, predict, predicted_omega,
                                  predicted_sigma, registry_fingerprint)


# ----------------------------------------------------------------------------
# Fixture builders
# ----------------------------------------------------------------------------
def _mode_row(label: str, omega: float, sigma: float, identity: str | None = None) -> CandidateRow:
    interval = ModeInterval(
        low=omega - sigma, center=omega, high=omega + sigma, nominal_level=0.68,
        interval_kind="hesse_delta", inference_paradigm="frequentist_asymptotic",
        source_quantity="freq_interval", authority={"authority_basis": "producer_sound"},
        conditioning_fingerprint=None)
    return CandidateRow(
        label=label, k=1, n_modes=1, log_evidence=0.0, evidence_method="test",
        posterior_probability=1.0, usable=True, identity=identity, variant=None,
        omegas=(omega,), mode_intervals=(interval,),
        components=(ComponentDescriptor(kind="mode", slot=0),))


def _elastic_row(label: str) -> CandidateRow:
    return CandidateRow(
        label=label, k=0, n_modes=0, log_evidence=0.0, evidence_method="test",
        posterior_probability=1.0, usable=True, identity=None, variant=None,
        omegas=(), mode_intervals=(), components=(ComponentDescriptor(kind="elastic"),))


def _observation(scan_ref: str, row: CandidateRow, gauss_fwhm: float = 0.4) -> SceneObservation:
    return SceneObservation(
        scan_ref=scan_ref, q=(0.0, 0.0, 0.0), temperature_K=None,
        energy_window=(-20.0, 20.0), energy_bands=((-20.0, 20.0),),
        gauss_fwhm=gauss_fwhm, lorentz_fwhm=None, effective_exposure=1.0, background=0.0,
        best_label=row.label, candidates=(row,), evidence_comparison_status="complete",
        selection_fallback=None, arena_fingerprint=None, evidence_sink=None, sample="fcc-test",
        conditions=(), count_trust="trusted", lattice_epoch=None, fingerprint="test")


def _footprint(scan_ref: str, axis_coordinate: float) -> Footprint:
    return Footprint(scan_ref=scan_ref, q=(axis_coordinate, 0.0, 0.0), bands=(),
                     temperature_K=None, background=0.0, gauss_fwhm=0.4,
                     axis_coordinate=axis_coordinate)


def _alt_entry(assignment, links) -> dict:
    return {"assignment": [list(pair) for pair in assignment], "links": [list(p) for p in links],
           "base": 0.0, "factor_terms": {}, "prior_terms": {}, "reasons": {}, "total": 0.0,
           "posterior": 1.0}


def _payload(obs_rows: list[tuple[str, CandidateRow, float]], links: list[tuple],
            assignment: list[tuple]) -> dict:
    """One cluster/one alternative payload from ``[(scan_ref, row, axis_coordinate), ...]``."""
    observations = [_observation(ref, row) for ref, row, _ in obs_rows]
    footprints = [_footprint(ref, x) for ref, _, x in obs_rows]
    return {
        "observations": [o.to_dict() for o in observations],
        "footprints": [fp.to_dict() for fp in footprints],
        "clusters": [{"cluster_id": "c1", "scan_refs": [ref for ref, _, _ in obs_rows],
                     "alternatives": [_alt_entry(assignment, links)]}],
    }


# TRUE FCC geometry along [h00], conventional indexing: the dispersion folds at EVERY integer
# (Γ at even h, the X boundary at odd h — both mirror planes of ω(q)), while Bragg reflections
# exist ONLY at the even integers. (3,0,0) is integer-indexed, sits on a fold, and is extinct —
# the acoustic branch reaches its MAXIMUM there, never zero. A test geometry with folds only at
# multiples of 3 would hide exactly that trap, which is why the period here is 1.0.
FCC_PERIOD = 1.0
FCC_REFLECTIONS = (2.0, 4.0)


def _ascending_track_payload(identity=None):
    """Two arenas, a linked mode ascending toward the extinct boundary fold at q=3.0
    (the (3,0,0)-analog): anchors strictly inside the (2,0,0)→(3,0,0) half-zone."""
    row_a = _mode_row("m", 8.0, 0.3, identity=identity)
    row_b = _mode_row("m", 9.0, 0.3, identity=identity)
    payload = _payload(
        obs_rows=[("s1", row_a, 2.2), ("s2", row_b, 2.6)],
        links=[("s1#m#0", "s2#m#0")],
        assignment=[("s1", "m"), ("s2", "m")])
    return payload


def _sample(zone_period=FCC_PERIOD, dispersion_slope_max=None, reflections=()):
    return SampleKnowledge(zone_period=zone_period, dispersion_slope_max=dispersion_slope_max,
                           reflections=reflections)


ACOUSTIC_HYP = {"acoustic": {"preset": "acoustic",
                             "zc_energy": {"mode": 0.0, "sigma": 0.3},
                             "confidence": 0.8}}


# ----------------------------------------------------------------------------
# 1. FCC fold: the boundary mirrors; descent happens ONLY at a declared-reflection fold
# ----------------------------------------------------------------------------
def test_fold_reflects_without_gapless_anchor():
    payload = _ascending_track_payload()
    sample = _sample()
    beliefs = build_beliefs(payload, sample, hypotheses=None)
    assert len(beliefs) == 1
    belief = beliefs[0]
    assert belief.family == "curve"

    at_fold_minus = predicted_omega(belief, 2.6)          # the last real anchor
    beyond_fold = predicted_omega(belief, 3.4)             # its mirror image across fold 3.0

    # Reflection, not descent: the mirrored point matches the anchor's own VALUE (9.0 — a
    # constant-ω stub would pass a bare equality check, so the value is pinned too).
    assert math.isclose(at_fold_minus, 9.0, abs_tol=1e-9)
    assert math.isclose(beyond_fold, at_fold_minus, abs_tol=1e-9)
    # Delta-advisory-A test-strength fix (5.1): pin the OTHER anchor's value too, so a
    # constant-9.0 stub of predicted_omega (which would pass the two assertions above unchanged)
    # also fails here.
    assert math.isclose(predicted_omega(belief, 2.2), 8.0, abs_tol=1e-9)


def test_gapless_anchor_plants_only_at_declared_reflection_fold():
    """THE FCC trap, pinned. An acoustic (gapless) declaration descends to zero at the declared
    Γ fold (2.0) — and NEVER at the extinct boundary fold (3.0), which stays the branch maximum
    even though it is the nearest fold beyond the ascending track."""
    payload = _ascending_track_payload(identity="acoustic")
    sample = _sample(reflections=FCC_REFLECTIONS)
    beliefs = build_beliefs(payload, sample, ACOUSTIC_HYP)
    assert len(beliefs) == 1
    belief = beliefs[0]

    # Descent at the declared reflection's fold: (2,0,0) is a real zone centre.
    assert math.isclose(predicted_omega(belief, 2.0), 0.0, abs_tol=1e-6)
    # No phantom descent at the extinct boundary: ω(3.0) extrapolates the rise past the last
    # anchor and stays the maximum of the branch.
    at_boundary = predicted_omega(belief, 3.0)
    assert at_boundary > predicted_omega(belief, 2.6)
    # The mirror machinery generates the far half-zone with no anchor ever planted there: a
    # query at 3.8 folds across the boundary onto 2.2, approaching zero toward the NEXT zone
    # centre at (4,0,0).
    assert math.isclose(predicted_omega(belief, 3.8), predicted_omega(belief, 2.2),
                        abs_tol=1e-9)


def test_gapless_without_declared_reflections_plants_nothing():
    payload = _ascending_track_payload(identity="acoustic")
    sample = _sample()  # gapless hypothesis, but NO reflections declared
    beliefs = build_beliefs(payload, sample, ACOUSTIC_HYP)
    assert len(beliefs) == 1
    belief = beliefs[0]

    # No guessed zone centre: nothing descends, the degradation names why, and the boundary
    # still mirrors (fold geometry is declared independently of the reflection list). Values
    # pinned, not just equalities: the downward extrapolation of the 2.5 meV/rlu track reaches
    # 7.5 at q=2.0 — descent to ~0 there is exactly what must NOT happen.
    assert "no_declared_reflections_for_gapless_anchor" in belief.degradations
    assert math.isclose(predicted_omega(belief, 2.0), 7.5, abs_tol=1e-9)
    assert math.isclose(predicted_omega(belief, 3.4), 9.0, abs_tol=1e-9)


# ----------------------------------------------------------------------------
# 2. Bose weighting
# ----------------------------------------------------------------------------
def _single_mode_belief(omega=8.0, sigma=0.4):
    sample = SampleKnowledge()
    return PhenomenonBelief(cluster_id="c", alternative_index=0, family="curve",
                            anchors=(Anchor(0.0, omega, sigma),), sample=sample)


def _anti_stokes_total(prediction) -> float:
    return sum(c for g, c in zip(prediction.omega_grid, prediction.expected_counts) if g < 0.0)


def test_bose_weight_collapses_anti_stokes_at_low_temperature():
    belief = _single_mode_belief(omega=8.0, sigma=0.4)
    design = Design(axis_coordinate=0.0, window=(-15.0, 15.0), n_points=121, ncount=1_000_000.0)

    low = predict(belief, Design(design.axis_coordinate, design.window, design.n_points,
                                 design.ncount, temperature_K=5.0), seed=1)
    high = predict(belief, Design(design.axis_coordinate, design.window, design.n_points,
                                  design.ncount, temperature_K=300.0), seed=1)
    none = predict(belief, Design(design.axis_coordinate, design.window, design.n_points,
                                  design.ncount, temperature_K=None), seed=1)

    assert _anti_stokes_total(low) < _anti_stokes_total(high)
    assert _anti_stokes_total(none) < _anti_stokes_total(low)
    assert "no_declared_temperature_for_bose" in none.degradations
    assert "no_declared_temperature_for_bose" not in low.degradations


# ----------------------------------------------------------------------------
# 3. Degradations: undeclared zone_period / slope each disable exactly their own constraint
# ----------------------------------------------------------------------------
def test_undeclared_zone_period_reflects_nothing_and_records_its_own_reason():
    sample = SampleKnowledge(zone_period=None, dispersion_slope_max=1.0)
    belief = PhenomenonBelief(cluster_id="c", alternative_index=0, family="curve",
                              anchors=(Anchor(2.0, 8.0, 0.3), Anchor(2.5, 9.0, 0.3)),
                              sample=sample)
    degrade: list = []
    predicted_omega(belief, 4.0, _degrade=degrade)
    assert "no_declared_zone_period" in degrade
    assert "no_declared_dispersion_slope_max" not in degrade


def test_undeclared_slope_widens_and_records_its_own_reason():
    sample = SampleKnowledge(zone_period=FCC_PERIOD, dispersion_slope_max=None)
    belief = PhenomenonBelief(cluster_id="c", alternative_index=0, family="curve",
                              anchors=(Anchor(2.0, 8.0, 0.3), Anchor(2.5, 9.0, 0.3)),
                              sample=sample)
    degrade: list = []
    predicted_sigma(belief, 0.5, _degrade=degrade)   # extrapolates below lo, no fold nearby
    assert "no_declared_dispersion_slope_max" in degrade
    assert "no_declared_zone_period" not in degrade


# ----------------------------------------------------------------------------
# 4. Seeded determinism
# ----------------------------------------------------------------------------
def test_seeded_draws_are_deterministic_and_seed_sensitive():
    belief = _single_mode_belief()
    design = Design(axis_coordinate=0.0, window=(-10.0, 10.0), n_points=41, ncount=2_000_000.0,
                    temperature_K=200.0)

    a = predict(belief, design, seed=7)
    b = predict(belief, design, seed=7)
    c = predict(belief, design, seed=8)

    assert a.draws == b.draws
    assert a.expected_counts == b.expected_counts
    assert a.draws != c.draws


# ----------------------------------------------------------------------------
# 5. Count-scale traceability
# ----------------------------------------------------------------------------
def test_count_scale_traces_to_declared_prior_only():
    belief = _single_mode_belief(omega=0.0, sigma=0.5)
    design = Design(axis_coordinate=0.0, window=(-3.0, 3.0), n_points=25, ncount=500_000.0)

    baseline = predict(belief, design, seed=3)

    retuned = dict(FAMILIES)
    retuned["curve"] = PhenomenonFamily(
        name="curve", version="2",
        config=(("default_gamma_mev", 0.3), ("diffuse_sigma_growth_per_rlu", 5.0),
                ("reference_counts", 200.0), ("reference_ncount", 1_000_000.0)))
    changed = predict(belief, design, families=retuned, seed=3)
    assert changed.expected_counts != baseline.expected_counts
    assert max(changed.expected_counts) > max(baseline.expected_counts)
    # Scale-ratio: reference_counts is linear (doubling it exactly doubles every grid rate, same
    # sigma so the density shape is unchanged).
    assert np.allclose(changed.expected_counts, 2.0 * np.array(baseline.expected_counts))

    unrelated = dict(FAMILIES)
    unrelated["curve"] = PhenomenonFamily(
        name="curve", version="2",
        config=(("default_gamma_mev", 0.3), ("diffuse_sigma_growth_per_rlu", 99.0),
                ("reference_counts", 100.0), ("reference_ncount", 1_000_000.0)))
    same = predict(belief, design, families=unrelated, seed=3)
    assert same.expected_counts == baseline.expected_counts


def test_count_scale_is_linear_in_ncount():
    belief = _single_mode_belief(omega=0.0, sigma=0.5)
    base_design = Design(axis_coordinate=0.0, window=(-3.0, 3.0), n_points=25, ncount=500_000.0)
    tripled_design = Design(axis_coordinate=0.0, window=(-3.0, 3.0), n_points=25,
                            ncount=1_500_000.0)

    baseline = predict(belief, base_design, seed=3)
    tripled = predict(belief, tripled_design, seed=3)
    assert np.allclose(tripled.expected_counts, 3.0 * np.array(baseline.expected_counts))


def test_spectral_weight_conserved_under_linewidth_retune():
    """Delta advisory B, resolved: ``reference_counts`` is integrated spectral weight, so
    Sum(expected_counts * grid_spacing) is invariant when ``default_gamma_mev`` is retuned at
    fixed weight — a wider declared linewidth redistributes the same counts instead of minting
    more of them. A wide window/fine grid keeps grid-truncation loss small on both sides."""
    belief = _single_mode_belief(omega=0.0, sigma=0.5)
    design = Design(axis_coordinate=0.0, window=(-20.0, 20.0), n_points=4001, ncount=500_000.0)
    dx = (design.window[1] - design.window[0]) / (design.n_points - 1)

    baseline = predict(belief, design, seed=3)
    total_base = sum(baseline.expected_counts) * dx

    broadened_families = dict(FAMILIES)
    broadened_families["curve"] = PhenomenonFamily(
        name="curve", version="2",
        config=(("default_gamma_mev", 3.0), ("diffuse_sigma_growth_per_rlu", 5.0),
                ("reference_counts", 100.0), ("reference_ncount", 1_000_000.0)))
    broadened = predict(belief, design, families=broadened_families, seed=3)
    total_broad = sum(broadened.expected_counts) * dx

    assert math.isclose(total_base, total_broad, rel_tol=0.02)


def test_linewidth_prior_is_consulted_not_dead_config():
    """Every declared prior must be LIVE (verifier advisory A1): the declared linewidth rides
    the predictive in quadrature with the position uncertainty, so retuning it changes the
    predicted spectrum — a config key whose retuning moved the registry fingerprint while
    changing no prediction would be an untestable assumption wearing a declaration's clothes."""
    belief = _single_mode_belief(omega=0.0, sigma=0.5)
    design = Design(axis_coordinate=0.0, window=(-3.0, 3.0), n_points=25, ncount=500_000.0)
    baseline = predict(belief, design, seed=3)

    wider = dict(FAMILIES)
    wider["curve"] = PhenomenonFamily(
        name="curve", version="2",
        config=(("default_gamma_mev", 2.0), ("diffuse_sigma_growth_per_rlu", 5.0),
                ("reference_counts", 100.0), ("reference_ncount", 1_000_000.0)))
    broadened = predict(belief, design, families=wider, seed=3)
    assert broadened.expected_counts != baseline.expected_counts
    # A wider peak at the same INTEGRATED weight spreads mass outward: off-center rate rises
    # even though total weight is conserved (see test_spectral_weight_conserved_under_...).
    assert broadened.expected_counts[0] > baseline.expected_counts[0]

    ela = PhenomenonBelief(cluster_id="c", alternative_index=0, family="elastic",
                           anchors=(Anchor(0.0, 0.0, 0.4),), sample=SampleKnowledge())
    ela_base = predict(ela, design, seed=3)
    wider_e = dict(FAMILIES)
    wider_e["elastic"] = PhenomenonFamily(
        name="elastic", version="2",
        config=(("default_width_mev", 1.5), ("reference_counts", 500.0),
                ("reference_ncount", 1_000_000.0)))
    ela_broadened = predict(ela, design, families=wider_e, seed=3)
    assert ela_broadened.expected_counts != ela_base.expected_counts


# ----------------------------------------------------------------------------
# 6. Width pinning
# ----------------------------------------------------------------------------
def test_unknown_family_is_flat_over_its_declared_domain():
    belief = PhenomenonBelief(cluster_id="c", alternative_index=0, family="unknown",
                              anchors=(), sample=SampleKnowledge())
    design = Design(axis_coordinate=0.0, window=(-40.0, 40.0), n_points=17, ncount=1_000_000.0)
    prediction = predict(belief, design, seed=1)
    assert len(set(prediction.expected_counts)) == 1
    assert prediction.expected_counts[0] > 0.0


def test_curve_predictive_width_is_strictly_wider_beyond_the_anchored_span():
    sample = SampleKnowledge(zone_period=None, dispersion_slope_max=2.0)
    belief = PhenomenonBelief(cluster_id="c", alternative_index=0, family="curve",
                              anchors=(Anchor(0.0, 5.0, 0.3), Anchor(1.0, 6.0, 0.3)),
                              sample=sample)
    at_anchor = predicted_sigma(belief, 0.0)
    far_beyond = predicted_sigma(belief, 5.0)
    assert far_beyond > at_anchor


def test_within_span_midpoint_between_distant_anchors_is_strictly_wider_than_at_either_anchor():
    """5.1 disposition of the 5.0 delta-verifier's advisory A2: within-span width is no longer
    flat. A node exactly midway between two SPARSE anchors reads as strictly less certain than a
    node sitting at either anchor, once a slope is declared — the term that makes the MI engine
    prefer densifying a sparse track over re-measuring an anchored point."""
    sample = SampleKnowledge(zone_period=None, dispersion_slope_max=2.0)
    belief = PhenomenonBelief(cluster_id="c", alternative_index=0, family="curve",
                              anchors=(Anchor(0.0, 5.0, 0.3), Anchor(10.0, 6.0, 0.3)),
                              sample=sample)
    at_left = predicted_sigma(belief, 0.0)
    at_right = predicted_sigma(belief, 10.0)
    midpoint = predicted_sigma(belief, 5.0)
    assert midpoint > at_left
    assert midpoint > at_right


# ----------------------------------------------------------------------------
# 7. Registry manifest
# ----------------------------------------------------------------------------
def test_registry_fingerprint_is_order_free_and_config_sensitive():
    a = {"curve": CURVE, "elastic": FAMILIES["elastic"]}
    b = {"elastic": FAMILIES["elastic"], "curve": CURVE}
    assert registry_fingerprint(a) == registry_fingerprint(b)

    retuned = PhenomenonFamily(name="curve", version="1",
                               config=(("default_gamma_mev", 0.3),
                                       ("diffuse_sigma_growth_per_rlu", 5.0),
                                       ("reference_counts", 999.0),
                                       ("reference_ncount", 1_000_000.0)))
    c = {"curve": retuned, "elastic": FAMILIES["elastic"]}
    assert registry_fingerprint(a) != registry_fingerprint(c)
    assert family_manifest(a) == family_manifest(b)


# ----------------------------------------------------------------------------
# Extras: continuum refuses, elastic component reaches belief construction
# ----------------------------------------------------------------------------
def test_continuum_refuses_prediction():
    belief = PhenomenonBelief(cluster_id="c", alternative_index=0, family="continuum",
                              anchors=(), sample=SampleKnowledge())
    design = Design(axis_coordinate=0.0, window=(-1.0, 1.0), n_points=3, ncount=1.0)
    with pytest.raises(ContinuumRefusesPrediction):
        predict(belief, design, seed=0)


def test_elastic_component_becomes_its_own_belief():
    row = _elastic_row("e")
    payload = {
        "observations": [_observation("s1", row).to_dict()],
        "footprints": [_footprint("s1", 3.0).to_dict()],
        "clusters": [{"cluster_id": "c1", "scan_refs": ["s1"],
                     "alternatives": [_alt_entry([("s1", "e")], [])]}],
    }
    beliefs = build_beliefs(payload, SampleKnowledge())
    assert len(beliefs) == 1
    assert beliefs[0].family == "elastic"
    assert beliefs[0].anchors[0].omega == 0.0
