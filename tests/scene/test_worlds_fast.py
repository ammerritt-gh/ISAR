"""Fast acceptance tests for the SCENE world suite (isar/synth/worlds.py).

These pin the *world generator*, not the analysis: every world must build, its answer key
must survive a JSON round-trip, the same seed must replay byte-identically, and each
adversarial world must actually contain the trap it advertises. A world whose physics
quietly stops being adversarial is worse than no world at all — the battery would keep
passing while testing nothing.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from isar.synth import config, dispersion, generate, noise, worlds


def _build(name):
    cfg, truth = worlds.build(name)
    return cfg, truth, generate.generate_world(truth)


# ----------------------------------------------------------------------------
# Suite-wide invariants
# ----------------------------------------------------------------------------
def test_registry_covers_the_declared_suite():
    expected = {
        "canonical", "no_acoustic", "pure_elastic", "blank", "unknown_extra",
        "endpoint_under_bragg", "crossing", "as_warm", "as_cold",
        "off_lattice_elastic", "mismatch", "faint",
    }
    assert set(worlds.WORLDS) == expected


@pytest.mark.parametrize("name", sorted(worlds.WORLDS))
def test_world_generates(name):
    cfg, truth, data = _build(name)
    assert truth.config is cfg                      # one copy of every number
    n_H, n_x = len(generate.world_h_values(cfg)), len(generate.world_energy_grid(cfg))
    assert data.lam.shape == data.counts.shape == (n_H, n_x)
    assert np.all(np.isfinite(data.lam)) and np.all(data.lam >= 0)
    assert data.counts.dtype.kind in "iu" and np.all(data.counts >= 0)
    # Convention D3: Poisson counts, no floor — zero-count channels are legal and expected
    # wherever λ is small, and nothing in the generator clamps them away.
    assert np.all(data.lam >= truth.background_counts - 1e-12)


@pytest.mark.parametrize("name", sorted(worlds.WORLDS))
def test_truth_manifest_round_trips(name):
    _cfg, truth, _data = _build(name)
    assert config.WorldTruth.from_dict(truth.to_dict()) == truth
    # ... and through JSON, where tuples would silently become lists.
    assert config.WorldTruth.from_dict(json.loads(json.dumps(truth.to_dict()))) == truth


def test_truth_manifest_rejects_a_foreign_schema():
    _cfg, truth, _data = _build("canonical")
    payload = truth.to_dict()
    payload["schema"] = "isar.scene_world_truth/99"
    with pytest.raises(ValueError):
        config.WorldTruth.from_dict(payload)


def test_from_dict_rejects_an_unexpected_key_at_every_level():
    # A dropped/extra key must fail loudly, not vanish silently on round-trip.
    _cfg, truth, _data = _build("canonical")
    payload = truth.to_dict()

    world_payload = dict(payload)
    world_payload["bogus"] = 1
    with pytest.raises(ValueError, match="bogus"):
        config.WorldTruth.from_dict(world_payload)

    config_payload = dict(payload)
    config_payload["config"] = dict(payload["config"], bogus=1)
    with pytest.raises(ValueError, match="bogus"):
        config.WorldTruth.from_dict(config_payload)

    component_payload = dict(payload)
    component_payload["components"] = [dict(payload["components"][0], bogus=1)]
    with pytest.raises(ValueError, match="bogus"):
        config.WorldTruth.from_dict(component_payload)


def test_branch_spec_rejects_an_unexpected_key():
    payload = dict(dispersion.ACOUSTIC.to_dict(), bogus=1)
    with pytest.raises(ValueError, match="bogus"):
        dispersion.BranchSpec.from_dict(payload)


def test_component_truth_rejects_incoherent_manifests():
    branch = dispersion.BranchSpec("b", 1.0, 2.0)
    with pytest.raises(ValueError, match="elastic"):
        config.ComponentTruth("elastic", "bragg", 100.0, branch=branch)
    with pytest.raises(ValueError, match="q_fwhm"):
        config.ComponentTruth("elastic", "bragg", 100.0, q_centers=(2.0,), q_fwhm=0.0)
    with pytest.raises(ValueError, match="width"):
        config.ComponentTruth("unknown", "unknown", 100.0, branch=branch, width=0.0)
    with pytest.raises(ValueError, match="asymmetry"):
        config.ComponentTruth("unknown", "unknown", 100.0, branch=branch,
                              width=2.5, asymmetry=1.5)


@pytest.mark.parametrize("name", sorted(worlds.WORLDS))
def test_same_seed_replays_identically(name):
    _cfg, truth, first = _build(name)
    second = generate.generate_world(truth)
    assert np.array_equal(first.counts, second.counts)
    assert np.array_equal(first.lam, second.lam)


def test_different_seeds_give_different_draws():
    _cfg, a = worlds.build("canonical", 7)
    _cfg, b = worlds.build("canonical", 8)
    da, db = generate.generate_world(a), generate.generate_world(b)
    assert np.array_equal(da.lam, db.lam)           # same world
    assert not np.array_equal(da.counts, db.counts)  # different realization


def test_truth_round_trip_regenerates_the_same_counts(tmp_path):
    _cfg, truth, data = _build("canonical")
    path = generate.write_world_truth(truth, tmp_path / "truth.json")
    restored = generate.read_world_truth(path)
    assert np.array_equal(generate.generate_world(restored).counts, data.counts)


# ----------------------------------------------------------------------------
# Per-world physics: each world really contains its trap
# ----------------------------------------------------------------------------
def test_blank_world_is_background_only():
    _cfg, truth, data = _build("blank")
    assert truth.components == ()
    assert np.allclose(data.lam, truth.background_counts)


def test_pure_elastic_has_no_inelastic_structure():
    # The elastic Lorentzian tail is a real (and deliberately kept) few-count confound at
    # several meV, so 'no inelastic signal' cannot mean 'flat'. What it does mean: the
    # expectation peaks at E = 0 and falls away monotonically — there is no second maximum
    # anywhere for a mode-finder to claim.
    _cfg, truth, data = _build("pure_elastic")
    positive = data.x[data.x >= 0]
    for h in data.H:
        lam = generate.world_lambda(truth, h, positive)
        assert np.all(np.diff(lam) <= 1e-9), f"non-monotonic wing at H={h}"


def test_canonical_has_a_gapless_branch_and_a_gapped_one():
    _cfg, truth, _data = _build("canonical")
    by_identity = {c.identity: c for c in truth.components}
    assert by_identity["acoustic"].branch.omega0(2.0) == 0.0
    assert by_identity["optic"].branch.omega0(2.0) >= 7.0


def test_no_acoustic_world_has_no_gapless_branch():
    _cfg, truth, _data = _build("no_acoustic")
    modes = [c for c in truth.components if c.branch is not None]
    assert modes and all(c.branch.omega0(2.0) >= 3.0 for c in modes)
    assert "acoustic" not in {c.identity for c in truth.components}


def test_unknown_extra_carries_a_dispersionless_level():
    _cfg, truth, _data = _build("unknown_extra")
    level = next(c for c in truth.components if c.identity == "level")
    assert level.branch.omega0(2.0) == level.branch.omega0(2.5) == 5.0


def test_crossing_world_branches_actually_cross():
    _cfg, truth, data = _build("crossing")
    a = next(c.branch for c in truth.components if c.identity == "branch_a")
    b = next(c.branch for c in truth.components if c.identity == "branch_b")
    diff = a.omega0(data.H) - b.omega0(data.H)
    assert diff.min() < 0 < diff.max()              # the order really swaps on the path
    H_cross = dispersion.crossing_H(a, b)
    assert data.H[0] < H_cross < data.H[-1]
    assert a.omega0(H_cross) == pytest.approx(b.omega0(H_cross))


def test_endpoint_under_bragg_buries_several_low_q_nodes():
    _cfg, truth, data = _build("endpoint_under_bragg")
    acoustic = next(c for c in truth.components if c.identity == "acoustic")
    bragg = next(c for c in truth.components if c.identity == "bragg")
    resolution = truth.config.gauss_fwhm       # ~1 meV: unresolvable from the elastic line

    nodes = (2.05, 2.10, 2.15)
    mirrors = (2.95, 2.90, 2.85)
    for h in nodes + mirrors:
        # The branch is genuinely present but unresolvable from the resolution width...
        assert acoustic.branch.omega0(h) < resolution
        # ...and the elastic line is genuinely there too: not a deleted/zeroed component.
        elastic_peak = generate.component_lambda(truth, bragg, h, data.x).max()
        acoustic_peak = generate.component_lambda(truth, acoustic, h, data.x).max()
        assert acoustic_peak > 0
        assert elastic_peak >= 0.25 * acoustic_peak

    # The mode is truly there at those H — the manifest says so even where the data cannot.
    assert acoustic.branch.omega0(2.5) > resolution


def test_off_lattice_elastic_sits_at_the_requested_q():
    _cfg, truth, data = _build("off_lattice_elastic")
    feature = truth.components[0]
    assert feature.q_centers == (2.5,)
    heights = [generate.component_lambda(truth, feature, h, data.x).max() for h in data.H]
    assert data.H[int(np.argmax(heights))] == 2.5
    # Zero-energy and resolution-limited: the maximum of the feature is at E = 0.
    at_peak = generate.component_lambda(truth, feature, 2.5, data.x)
    assert data.x[int(np.argmax(at_peak))] == 0.0
    # ... and it is NOT at a zone centre, so 'lowest feature ⇒ Bragg' has nothing to stand on.
    assert generate.q_envelope(feature, 2.0) < 1e-6


def test_mismatch_world_is_not_a_dho():
    _cfg, mm, _data = _build("mismatch")
    _cfg, canon, _d = _build("canonical")
    x = generate.world_energy_grid(mm.config)
    horn = next(c for c in mm.components if c.kind == "unknown")
    dho = next(c for c in canon.components if c.identity == "acoustic")
    assert horn.branch == dho.branch                 # same dispersion, different lineshape
    a = generate.component_lambda(mm, horn, 2.5, x)
    b = generate.component_lambda(canon, dho, 2.5, x)
    assert np.abs(a - b).max() > 20.0                # far beyond the counting noise (~10)
    # The intrinsic profile has two unequal horns — a shape no DHO can produce.
    fine = np.arange(0.0, 12.0, 0.02)
    p = noise.two_horn_profile(fine, 6.0, horn.width, horn.asymmetry)
    maxima = p[1:-1][(p[1:-1] > p[:-2]) & (p[1:-1] >= p[2:])]
    assert len(maxima) == 2 and maxima[0] > maxima[1]


def test_faint_world_sits_at_the_detection_floor():
    _cfg, truth, data = _build("faint")
    modes = [c for c in truth.components if c.branch is not None]
    assert modes and all(c.peak_counts <= 10.0 for c in modes)
    # A mode peak is only a couple of sigma over the background it stands on.
    assert truth.background_counts > 0
    assert modes[0].peak_counts / np.sqrt(truth.background_counts) < 10.0
    assert data.counts.max() > 0


# ----------------------------------------------------------------------------
# Temperature: the anti-Stokes side is a world parameter, and it is derived
# ----------------------------------------------------------------------------
def _anti_stokes_significance(truth, H=2.5):
    """Anti-Stokes mode counts over the noise of everything on the anti-Stokes side."""
    x = generate.world_energy_grid(truth.config)
    side = x < -1.5                                  # clear of the elastic line
    astokes = sum(
        (generate.component_lambda(truth, c, H, x, peak="astokes")
         for c in truth.components if c.branch is not None),
        np.zeros_like(x),
    )
    total = generate.world_lambda(truth, H, x)
    return float(astokes[side].sum() / np.sqrt(total[side].sum()))


def test_warm_world_shows_anti_stokes_and_cold_world_does_not():
    _cfg, warm, _d = _build("as_warm")
    _cfg, cold, _d = _build("as_cold")
    assert (warm.T, cold.T) == (300.0, 10.0)
    assert _anti_stokes_significance(warm) > 10.0    # must be used
    assert _anti_stokes_significance(cold) < 1.0     # Bose-dead; must not be claimed


def test_anti_stokes_amplitude_is_the_bose_factor_times_stokes():
    """Hard rule 2: never a free parameter — check it on the unknown lineshape too."""
    _cfg, truth, _d = _build("mismatch")
    x = generate.world_energy_grid(truth.config)
    horn = next(c for c in truth.components if c.kind == "unknown")
    omega0 = float(horn.branch.omega0(2.5))
    stokes = generate.component_lambda(truth, horn, 2.5, x, peak="stokes")
    astokes = generate.component_lambda(truth, horn, 2.5, x, peak="astokes")
    expected = np.exp(-omega0 / 0.086173324 / truth.T)
    assert astokes.sum() / stokes.sum() == pytest.approx(expected, rel=1e-3)


def test_zero_temperature_kills_the_anti_stokes_side():
    # At T = 0 there are no phonons to annihilate: the anti-Stokes component is exactly
    # empty (what is left on the negative side is the Stokes profile's resolution tail).
    x = np.arange(-10.0, 10.0, 0.25)
    args = (x, 6.0, 0.0, 0.961, 0.596, 2.5, 0.5, 1.0, 0.0)
    assert np.all(noise.convolute_voigt_unknown(*args, 0.0, peak="astokes") == 0.0)
    assert np.any(noise.convolute_voigt_unknown(*args, 300.0, peak="astokes") > 0.0)
    assert np.array_equal(noise.convolute_voigt_unknown(*args, 0.0),
                          noise.convolute_voigt_unknown(*args, 0.0, peak="stokes"))


def test_unknown_lineshape_rejects_a_side_it_does_not_have():
    # Silently dropping a mistyped side would return a different physical claim, quietly.
    x = np.arange(-10.0, 10.0, 0.25)
    with pytest.raises(ValueError):
        noise.convolute_voigt_unknown(x, 6.0, 0.0, 0.961, 0.596, 2.5, 0.5, 1.0, 0.0,
                                      100.0, peak="stokes+elastic")


def test_an_elastic_only_request_of_an_inelastic_component_is_empty():
    _cfg, truth, _d = _build("mismatch")
    x = generate.world_energy_grid(truth.config)
    horn = next(c for c in truth.components if c.kind == "unknown")
    assert np.all(generate.component_lambda(truth, horn, 2.5, x, peak="elastic") == 0.0)


def test_peak_all_includes_the_elastic_component():
    # peak='all' means the whole model, exactly as peak=None does — an elastic component
    # that returns zeros for 'all' would silently drop the elastic line from a full model.
    _cfg, truth, _d = _build("canonical")
    x = generate.world_energy_grid(truth.config)
    bragg = next(c for c in truth.components if c.kind == "elastic")
    whole = generate.component_lambda(truth, bragg, 2.0, x)
    assert np.any(whole > 0)
    assert np.array_equal(generate.component_lambda(truth, bragg, 2.0, x, peak="all"), whole)


def test_component_lambda_rejects_a_mistyped_peak_token_for_every_kind():
    # A silent partial model (dropping the mistyped token to zero) is worse than a crash:
    # component_lambda must name the bad token instead of quietly returning zeros.
    _cfg, truth, _d = _build("canonical")
    x = generate.world_energy_grid(truth.config)
    bragg = next(c for c in truth.components if c.kind == "elastic")
    dho = next(c for c in truth.components if c.kind == "dho")
    _cfg2, mm, _d2 = _build("mismatch")
    horn = next(c for c in mm.components if c.kind == "unknown")

    for comp, comp_truth in ((bragg, truth), (dho, truth), (horn, mm)):
        with pytest.raises(ValueError, match="stokse"):
            generate.component_lambda(comp_truth, comp, 2.5, x, peak="stokse")


# ----------------------------------------------------------------------------
# Single-scan checks under evidence selection (SCENE-1 PR-a verdicts, PR-b selection)
# ----------------------------------------------------------------------------
# These worlds are the exception to "pin the generator, not the analysis": they accept the
# analysis end-to-end. Since PR-b, selection here runs the EVIDENCE path (hyps + ctx supplied,
# posterior over structures drives) — `selection_fallback is None` is load-bearing in every
# check below: none of these tests can pass on the recorded AIC fallback.

from isar.analyze import verdict as _verdict

# The context/selection pair lives in _world_helpers so the scene world tests run the SAME path.
from ._world_helpers import run_selection as _run_selection


def _classify(name, h):
    ctx, results, sel = _run_selection(name, h)
    return _verdict.classify(results, ctx, sel)


def test_pure_elastic_world_classifies_elastic_only_at_the_zone_centre():
    v = _classify("pure_elastic", 2.0)
    assert v.verdict == "elastic_only"
    assert 0.0 < v.confidence <= 0.5 + 1e-12            # the categorical-humility cap
    assert v.components["s_scope_elastic"] == 0.5


def test_pure_elastic_world_off_centre_is_no_signal():
    # the elastic line is Bragg-localized (q_fwhm = 0.08): at H=2.5 there is nothing at all
    v = _classify("pure_elastic", 2.5)
    assert v.verdict == "no_signal"
    assert v.confidence == 0.0


def test_blank_world_classifies_no_signal():
    v = _classify("blank", 2.5)
    assert v.verdict == "no_signal"
    assert v.confidence == 0.0


def test_canonical_world_wins_both_branches_off_centre():
    # H=2.25: acoustic 4.24 meV and optic 8.41 meV, no Bragg (offset 0.25 >> q_fwhm 0.08).
    # Evidence selection must prefer the 2-mode structure and land both mode energies.
    _ctx, results, sel = _run_selection("canonical", 2.25)
    win = {r.label: r for r in results}[sel.best]
    assert win.n_modes == 2
    modes = sorted(v for n, v in win.params.items() if n.endswith(".Omega_d"))
    assert abs(modes[0] - 4.24) < 0.8
    assert abs(modes[1] - 8.41) < 0.8


def test_no_acoustic_world_keeps_the_gap_at_the_zone_centre():
    # H=2.0: Bragg + gapped 3.0 meV + optic 7.0 meV. A gapless-acoustic misreading would put
    # a mode inside the elastic resolution; the winner must not. (The identity axis is
    # deferred, so "not read as acoustic" is asserted structurally: no near-zero mode energy.)
    _ctx, results, sel = _run_selection("no_acoustic", 2.0)
    win = {r.label: r for r in results}[sel.best]
    assert win.n_modes >= 1
    modes = [v for n, v in win.params.items() if n.endswith(".Omega_d")]
    assert min(modes) > 2.0
    assert abs(min(modes) - 3.0) < 0.8


@pytest.mark.slow
def test_unknown_extra_world_extra_level_is_competitive():
    # H=2.25: acoustic 4.24 + level 5.0 (blended) + optic 8.41 — three true modes against the
    # 2-mode enumeration cap. A multimode structure must hold nonzero posterior, win or not:
    # the machine may be honest at its ceiling, but it must not read the scan as one mode.
    # Slow-marked (owner rule 2026-08-08): the genuinely ambiguous 3-vs-2 contest escalates to
    # dynesty by design — 117 s measured at ESCALATION_N_LIVE=128, over the fast-lane budget.
    _ctx, results, sel = _run_selection("unknown_extra", 2.25)
    n_modes = {r.label: r.n_modes for r in results}
    multi = [s for s in sel.scores if n_modes.get(s.label, 1) >= 2]
    assert multi
    assert max(s.posterior_probability for s in multi) > 0.0
