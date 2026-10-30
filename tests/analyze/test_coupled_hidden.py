"""Hidden-mode pricing in the coupled engine: the twin-screen guard, the split integral's numerics, the
undetermined status and the dedupe ledger, on the synthetic three-spectrum case of ``tests/inter_hidden_case.py``
(counts generated at test time, no measured value).

The refit-level proof (R1-R9 through ``refit.price``) lives in the tests named ``*proof*``.
"""
import math
import time
from dataclasses import replace
from unittest import mock

import numpy as np
import pytest
from scipy.integrate import quad
from scipy.special import logsumexp

from inter_hidden_case import STRUCT, Prepared, _ctx, _expected, hidden_snapshot
from isar.analyze import fast_pass, prior
from isar.analyze.coupled import engine as cc
from isar.analyze.coupled import refit as R
from isar.analyze.coupled.types import PricingJob

SLOTS = ("b1", "b2")


def _engines(snap, slots=SLOTS):
    """(spec, engine with the neighbour densities, engine with two unmatched entries) on one snapshot."""
    spec = R._Spec(snap)
    kw = dict(structures=spec.structures)
    return (spec, R._Labelled(spec.ctx, spec.fs, spec.slot_objects(slots), **kw),
            R._Labelled(spec.ctx, spec.fs, [spec.entry] * len(slots), **kw))


def _placed(spec, k=2, slots=SLOTS):
    return R._starts(spec, k, slots)[-1][1]


# ---- T-integrate ------------------------------------------------------------------------------------------
def test_integrate_split_is_exact_on_a_polynomial_and_counts_its_nodes():
    res = cc.integrate_split(lambda f, near: math.log(2.0 * f), 0.5)
    assert res.kind == "ok" and abs(res.ln_z) < 1e-9 and [n for n, _ in res.sets] == [6, 12]
    assert abs(res.sets[0][1]) < 1e-9 and res.nodes == 18


def test_a_narrow_peak_in_f_is_not_converged_not_wrong():
    res = cc.integrate_split(lambda f, near: -0.5 * ((f - 0.3) / 0.003) ** 2, 0.5)
    assert res.kind == "not_converged" and res.ln_z is None and len(res.sets) == 2    # bailed before set 3


def test_a_node_without_a_positive_definite_hessian_makes_the_integral_undetermined():
    res = cc.integrate_split(lambda f, near: None if f > 0.9 else 0.0, 0.5)
    assert res.kind == "undetermined" and res.ln_z is None
    assert res.reason.startswith("Hessian not positive definite") and "f=0.9" in res.reason


def test_a_node_failing_for_another_reason_keeps_that_reason_as_no_evidence():
    res = cc.integrate_split(lambda f, near: "non-finite Hessian (finite differences)" if f > 0.9 else 0.0, 0.5)
    assert res.kind == "no_evidence" and res.ln_z is None
    assert res.reason.startswith("non-finite Hessian") and "f=0.9" in res.reason


def test_a_kink_converges_within_tolerance_or_says_not_converged():
    ln_exact = math.log(quad(lambda f: math.exp(-abs(f - 0.37)), 0, 1, points=[0.37])[0])
    res = cc.integrate_split(lambda f, near: -abs(f - 0.37), 0.5)
    assert res.kind in ("ok", "not_converged")
    if res.kind == "ok":                                   # never "ok" and silently wrong
        assert abs(res.ln_z - ln_exact) < 0.15, (res.ln_z, ln_exact)
    wide = cc.integrate_split(lambda f, near: -abs(f - 0.37), 0.5, sets=(24, 48), tol=0.1)
    assert wide.kind == "ok" and abs(wide.ln_z - ln_exact) < 0.05


def test_continuation_runs_outward_from_the_joint_split():
    seen = []
    cc.integrate_split(lambda f, near: seen.append((f, near)) or 0.0, 0.5, sets=(6, 12))
    first = seen[:6]                                       # set 1: right chain ascending, then left descending
    assert [n for _, n in first] == [None, first[0][0], first[1][0], None, first[3][0], first[4][0]]
    assert first[0][0] > 0.5 > first[3][0] and first[2][0] > first[0][0] and first[5][0] < first[3][0]


def test_the_centre_of_a_matched_density_is_its_peak():
    spec = R._Spec(hidden_snapshot(3.0))
    d = spec.slot_objects(SLOTS)[1]
    assert isinstance(d, cc.MatchedDensity) and abs(d.centre - 8.0) < 0.2
    assert d.centre is d.centre                            # cached


# ---- T-guard ----------------------------------------------------------------------------------------------
def test_a_matched_pair_is_not_merged_but_the_same_pair_unmatched_is():
    spec, eng, plain = _engines(hidden_snapshot(3.0))
    start = _placed(spec)
    kept, _ = eng.fit(eng.variant(2, True, True), start, "neighbour")
    a, g = kept.model.modes[0][0], kept.model.modes[1][0]
    assert abs(kept.theta[a] - kept.theta[g]) < 0.5 * eng.fwhm                    # the premise: they coincide
    assert kept.status == "pending" and kept.count == 2 and kept.twin is None
    # the other direction of the rule: no matched slot, the inherited screen merges as today
    merged, _ = plain.fit(plain.variant(2, True, True), start, "neighbour")
    assert merged.status == "degenerate_twin" and merged.twin is not None


def test_a_vanished_matched_mode_with_a_far_partner_is_kept_but_an_unmatched_one_is_merged():
    spec, eng, plain = _engines(hidden_snapshot(6.0, b2_at=14.0))
    # a basin placed by hand: the second mode has no height (its energy is arbitrary), the partner is far
    for e, expect in ((eng, "pending"), (plain, "degenerate_twin")):
        model = e.model(e.variant(2, True, True), ())
        theta = e._start(model, [(8.0, 0.4, 46.0, False), (14.0, 0.4, 0.0, False)], {"elastic": 100.0, "bg": 2.0})
        assert theta[model.modes[1][2]] <= e.s.twin_height_frac * e.sp.height_hi        # the premise
        b = e._place(model, theta, e.cost(model, theta), "hand", 0, "")
        assert b.status == expect and (expect != "degenerate_twin" or b.limit.startswith("phonon2.h"))


def test_a_vanished_matched_mode_beside_another_is_not_merged_by_the_coincident_screen():
    spec, eng, _ = _engines(hidden_snapshot(6.0, b2_at=14.0))
    model = eng.model(eng.variant(2, True, True), ())
    theta = eng._start(model, [(8.0, 0.4, 46.0, False), (8.1, 0.4, 0.0, False)], {"elastic": 100.0, "bg": 2.0})
    assert theta[model.modes[1][2]] <= eng.s.twin_height_frac * eng.sp.height_hi      # the premise: it has vanished
    b = eng._place(model, theta, eng.cost(model, theta), "hand", 0, "")               # its centre is 14 meV: far
    assert b.status == "pending" and b.twin is None


def test_repeated_starts_reaching_one_solution_leave_one_basin():
    spec, eng, _ = _engines(hidden_snapshot(3.0))
    v, start = eng.variant(2, True, True), _placed(spec)
    first, _ = eng.fit(v, start, "a")
    again, _ = eng.fit(v, [(e + 0.05, g, h * 1.05, d) for e, g, h, d in start], "b")
    assert again is first and len(first.model.basins) == 1
    assert eng.s is eng._s0                                # the guard restored the settings


# ---- T-equivalence ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("start", [[(8.0, 0.4, 30.0, False), (8.0, 0.4, 15.0, False)],
                                   [(6.0, 0.4, 20.0, False), (13.0, 0.4, 20.0, False)]])
def test_with_no_matched_slot_the_screens_act_as_in_the_fast_pass(start):
    snap = hidden_snapshot(3.0)
    spec, _, plain = _engines(snap)
    base = fast_pass._Engine(spec.ctx, spec.fs, structures=STRUCT)
    b0, _ = base.fit(base.variant(2, True, True), start, "x")
    b1, _ = plain.fit(plain.variant(2, True, True), start, "x")
    assert (b1.status, b1.limit, b1.reason) == (b0.status, b0.limit, b0.reason)
    assert (b1.twin.label if b1.twin else None) == (b0.twin.label if b0.twin else None)


def test_the_coincident_start_does_merge_in_the_base_engine():       # the equivalence above is not vacuous
    spec, _, plain = _engines(hidden_snapshot(3.0))
    b, _ = plain.fit(plain.variant(2, True, True), [(8.0, 0.4, 30.0, False), (8.0, 0.4, 15.0, False)], "x")
    assert b.status == "degenerate_twin"


# ---- T-undetermined (engine part) -------------------------------------------------------------------------
def _one_mode_basin():
    spec = R._Spec(hidden_snapshot(3.0))
    eng = R._Labelled(spec.ctx, spec.fs, [spec.entry], structures=spec.structures)
    b, _ = eng.fit(eng.variant(1, True, True), [(8.0, 0.4, 43.0, False)], "x")
    return eng, b


@pytest.mark.parametrize("reason", cc.UNDETERMINED_REASONS)
def test_an_undetermined_laplace_reads_undetermined(monkeypatch, reason):
    eng, b = _one_mode_basin()

    def fake(self, basin):
        basin.status, basin.reason = "no_evidence", reason + " (made up)"
    monkeypatch.setattr(fast_pass._Engine, "price", fake)
    eng.price(b)
    assert b.status == "undetermined" and b.reason.startswith(reason) and b.ln_z is None


def test_a_failure_is_not_undetermined(monkeypatch):
    eng, b = _one_mode_basin()

    def fake(self, basin):
        basin.status, basin.reason = "no_evidence", "posterior optimiser: ABNORMAL"
    monkeypatch.setattr(fast_pass._Engine, "price", fake)
    eng.price(b)
    assert b.status == "no_evidence"


def test_a_split_node_failure_reads_no_evidence_with_its_reason(monkeypatch):
    spec, eng, _ = _engines(hidden_snapshot(3.0, asimov=True))
    model = eng.model(eng.variant(2, True, True), ())
    theta = eng._start(model, [(8.0, 0.4, 40.0, False), (8.0, 0.4, 3.0, False)], {"elastic": 100.0, "bg": 2.0})
    b = eng._place(model, theta, eng.cost(model, theta), "x", 0, "")
    monkeypatch.setattr(cc, "integrate_split", lambda *a, **k: cc.SplitResult(
        None, "no_evidence", "posterior optimiser: ABNORMAL: split node f=0.5000", (), 1))
    eng.price(b)
    assert b.status == "no_evidence" and b.reason.startswith("posterior optimiser: ABNORMAL")


def test_a_split_that_does_not_converge_keeps_both_reasons(monkeypatch):
    spec, eng, _ = _engines(hidden_snapshot(3.0, asimov=True))
    model = eng.model(eng.variant(2, True, True), ())
    theta = eng._start(model, [(8.0, 0.4, 40.0, False), (8.0, 0.4, 3.0, False)], {"elastic": 100.0, "bg": 2.0})
    b = eng._place(model, theta, eng.cost(model, theta), "x", 0, "")
    monkeypatch.setattr(cc, "integrate_split", lambda *a, **k: cc.SplitResult(
        None, "not_converged", "node sets differ by 2.00 nats", (), 1))

    def fake(self, basin):
        basin.status, basin.reason = "no_evidence", "posterior optimiser: ABNORMAL"
    monkeypatch.setattr(fast_pass._Engine, "price", fake)
    eng.price(b)
    assert b.status == "no_evidence"
    assert "posterior optimiser: ABNORMAL" in b.reason
    assert "split integral not converged: node sets differ by 2.00 nats" in b.reason


def test_the_two_reason_strings_are_pinned_on_prior():
    lo, hi, x0 = np.array([-1.0, -1.0]), np.array([1.0, 1.0]), np.array([0.1, 0.1])
    concave = prior.laplace_log_evidence(lambda x: -float(x @ x), x0, lo, hi)
    assert concave.status == "no_evidence" and concave.reason.startswith(cc.UNDETERMINED_REASONS[0])


def test_the_box_reason_string_is_pinned_on_prior(monkeypatch):
    lo, hi, x0 = np.array([-1.0, -1.0]), np.array([1.0, 1.0]), np.array([0.1, 0.1])

    def singular(*a, **k):
        raise np.linalg.LinAlgError("singular")
    monkeypatch.setattr(prior, "_ln_box_mass", singular)
    res = prior.laplace_log_evidence(lambda x: float(x @ x), x0, lo, hi)
    assert res.status == "no_evidence" and res.reason.startswith(cc.UNDETERMINED_REASONS[1])


# ---- the split integral through the engine ----------------------------------------------------------------
def test_a_hidden_pair_is_priced_by_the_split_integral():
    spec, eng, _ = _engines(hidden_snapshot(3.0, asimov=True))
    start = _placed(spec)
    for v in eng.variants[2]:
        eng.fit(v, start, "own")
    bs = [b for b in eng.basins() if b.status == "pending"]
    assert bs
    for b in bs:
        eng.price(b)
    split = [b for b in bs if b.boundary_method == "split"]
    assert split and all(b.status == "ok" and math.isfinite(b.ln_z) for b in split)
    b = split[0]
    assert b.hidden_pair == (0, 1) and b.cov_free is None
    assert eng.n_split_basins == len(split) and eng.n_split_nodes >= 18
    assert b.theta_map is not b.theta and not np.array_equal(b.theta_map, b.theta)    # a solved node, not the start
    assert math.isfinite(eng.cost(b.model, b.theta_map))
    h = [b.theta_map[c] for _a, _g, c in b.model.modes]
    assert h[0] + h[1] == pytest.approx(b.theta[b.model.modes[0][2]] + b.theta[b.model.modes[1][2]], rel=0.05)


# ---- T-dedupe (engine part) -------------------------------------------------------------------------------
def _three_mode_snapshot():
    """The hidden pair at 8 meV plus one separate unmatched peak at 20 meV (a count-3 case)."""
    counts = np.random.default_rng(3).poisson(_expected([(8.0, 0.4, 43.0), (20.0, 0.4, 25.0)])).astype(float)
    return replace(hidden_snapshot(3.0), context=Prepared(_ctx(counts), 3))


def _placed3(eng, model, third, split):
    theta = eng._start(model, [(8.0, 0.4, split[0], False), (8.0, 0.4, split[1], False), (third, 0.4, 25.0, False)],
                       {"elastic": 100.0, "bg": 2.0})
    return theta


def test_one_hidden_solution_is_priced_once():
    spec, eng, _ = _engines(_three_mode_snapshot(), ("b1", "b2", None))
    model = eng.model(eng.variant(3, True, True), ())
    b1, b2 = (eng._place(model, t, eng.cost(model, t), "x", 0, "")
              for t in (_placed3(eng, model, 20.0, (40.0, 3.0)), _placed3(eng, model, 20.1, (30.0, 13.0))))
    assert b1 is not b2 and b1.status == b2.status == "pending"      # two starts, two basins (the split differs)
    eng.price(b1)
    eng.price(b2)
    assert b1.status == "ok" and b1.boundary_method == "split" and math.isfinite(b1.ln_z)
    assert b2.status == "merged_hidden" and b2.ln_z is None and eng.n_split_basins == 1


def test_the_ledger_tells_solutions_apart_by_every_other_coordinate():
    spec, eng, _ = _engines(_three_mode_snapshot(), ("b1", "b2", None))
    model = eng.model(eng.variant(3, True, True), ())
    key = lambda t: eng._pair_point(model, t, (0, 1))                      # noqa: E731
    base = _placed3(eng, model, 20.0, (40.0, 3.0))
    eng._split_done[model] = {0: [key(base)]}
    assert eng._seen_split(model, key(_placed3(eng, model, 20.1, (30.0, 13.0))))   # another split, one solution
    assert eng._seen_split(model, key(_placed3(eng, model, 20.0, (3.0, 40.0))))    # the other slot order
    assert not eng._seen_split(model, key(_placed3(eng, model, 13.0, (40.0, 3.0))))    # a third mode elsewhere
    other = base.copy()
    other[model.el] = 160.0
    assert not eng._seen_split(model, key(other))              # same_basin ignores the elastic height; this does not
    other = base.copy()
    other[model.bg] = 4.0
    assert not eng._seen_split(model, key(other))


# ---- D6 the constructed seed --------------------------------------------------------------------------------
def test_the_seed_splits_the_pair_when_the_low_fit_crosses_its_labels():
    """A third start off its branch (2 meV): the low fit's labels cross, so its last mode is not the merged one.
    Every seed must still carry the pair at 8 meV and the third mode at its branch, 20 meV."""
    spec, eng, _ = _engines(_three_mode_snapshot(), ("b1", "b2", None))
    start = [(8.0, 0.4, 40.0, False), (8.0, 0.4, 3.0, False), (2.0, 0.4, 25.0, False)]
    eng.seed_hidden(3, [("neighbour", start)])
    seeds = [b for b in eng.basins() if b.family == "hidden"]
    assert seeds
    for b in seeds:
        e = [m[0] for m in eng.modes_of(b.model, b.theta)]
        assert abs(e[0] - 8.0) < 1.0 and abs(e[1] - 8.0) < 1.0 and abs(e[2] - 20.0) < 1.0, e


# ======================================================================================================
# The refit level: the proof (R1-R9 through ``refit.price``), the undetermined status, the record and the
# held-by-neighbours rule. Counts are generated at test time (hard rule 10).
# ======================================================================================================
JOB1, JOB12, JOB21 = ("b1",), ("b1", "b2"), ("b2", "b1")
_RUNS = {}


def _job(snap, slots):
    spec = R._Spec(snap)
    return PricingJob(snap.unit, snap.snapshot_id, len(slots), slots, spec.key(len(slots), slots))


def _price(snap, slots):
    return R.price(snap, _job(snap, slots))


def _run(w, *, seed=3, asimov=True, slots=JOB12):
    """One pricing of the synthetic case, shared by every routine test that reads it (the pricing is
    deterministic): (Priced, wall seconds, the split integrals it ran). Keeps the R1, R3, R4, R5 and R9 reads
    from each pricing the same count-2 job again."""
    key = (w, seed, asimov, slots)
    if key not in _RUNS:
        splits, orig = [], cc.integrate_split

        def spy(*a, **k):
            res = orig(*a, **k)
            splits.append(res)
            return res
        with mock.patch.object(cc, "integrate_split", spy):
            t = time.perf_counter()
            p = _price(hidden_snapshot(w, seed=seed, asimov=asimov), slots)
            _RUNS[key] = (p, time.perf_counter() - t, splits)
    return _RUNS[key]


def _split_terms(p):
    """The terms of the priced basins that carry background and were priced by the split integral."""
    return [b[0] for b in p.basins if b[1].endswith("+bg|split")]


def _d(w):
    """d(w) = ln Z(2, (b1, b2)) - ln Z(1, (b1,)) of the noise-free case of weight w, plus the two-mode price."""
    p2, p1 = _run(w)[0], _price(hidden_snapshot(w, asimov=True), JOB1)
    return p2, p1, p2.ln_z - p1.ln_z


def _say(*a):
    print("[proof]", *a)


# ---- R6 first: a wrong normalisation would pollute every other number -------------------------------------
@pytest.mark.parametrize("scale,seed", [(0.2, 1), (0.1, 2)])      # seed 1 at 0.1 rails a width: a twin, not a pair
def test_proof_r6_normalisation_on_a_resolved_pair(scale, seed):
    modes = [(6.0, 0.4, 40.0), (9.0, 0.4, 40.0)]
    counts = np.random.default_rng(seed).poisson(_expected(modes) * scale).astype(float)
    ctx = _ctx(counts)
    s = fast_pass.FastPassSettings(mode_ceiling=2)
    sp = prior.resolve_prior_for(ctx, s.prior or prior.PriorSettings())
    eng = cc.CoupledEngine(ctx, s, [cc.Entry(sp.energy, band=1.0, b_edge=0.0, b_mid=0.0)] * 2)
    b, _ = eng.fit(eng.variant(2, True, True), [(6.0, 0.4, 8.0, False), (9.0, 0.4, 8.0, False)], "x")
    eng.price(b)
    assert b.status in ("ok", "ok_boundary"), b.reason
    res = eng.split_evidence(b, b.theta_map, (0, 1), sets=(24, 48))
    _say(f"R6 scale {scale}: split {res.ln_z:.3f}, Laplace {b.ln_z:.3f}, diff {res.ln_z - b.ln_z:+.3f}")
    assert res.kind == "ok" and b.boundary_method != "split"
    assert abs(res.ln_z - b.ln_z) <= 0.25


# ---- R1 finite evidence -------------------------------------------------------------------------------------
@pytest.mark.parametrize("w,seed,asimov", [(0.5, 3, True), (3.0, 3, True), (12.0, 3, True), (6.0, 2, False)])
def test_proof_r1_the_hidden_composition_has_a_finite_split_evidence(w, seed, asimov):
    p = _run(w, seed=seed, asimov=asimov)[0]
    _say(f"R1 w={w} seed={seed} asimov={asimov}: {p.status} ln_z={p.ln_z} split terms={_split_terms(p)}")
    assert p.ln_z is not None and math.isfinite(p.ln_z) and p.status in ("ok", "ok_boundary")
    assert _split_terms(p)


# ---- R2 grid agreement --------------------------------------------------------------------------------------
def test_proof_r2_the_node_sets_agree_and_an_independent_set_lands_on_them():
    spec, eng, _ = _engines(hidden_snapshot(3.0, asimov=True))
    start = _placed(spec)
    for v in eng.variants[2]:
        eng.fit(v, start, "own")
    for b in [b for b in eng.basins() if b.count == 2 and b.status == "pending"]:
        eng.price(b)
    split = [b for b in eng.basins() if b.boundary_method == "split"]
    assert split
    for b in split:
        a = eng._anchor(b.model, b.theta)
        res = eng.split_evidence(b, a, b.hidden_pair)
        alt = eng.split_evidence(b, a, b.hidden_pair, sets=(7, 14))
        (n1, z1), (n2, z2) = res.sets[-2:]
        _say(f"R2 {b.model.variant.label}: sets {res.sets}, last two differ {abs(z1 - z2):.4f}, "
             f"independent (7, 14) {alt.ln_z:.4f} vs {res.ln_z:.4f} ({alt.ln_z - res.ln_z:+.4f})")
        assert res.kind == "ok" and abs(z1 - z2) <= 0.1
        assert alt.kind == "ok" and abs(alt.ln_z - res.ln_z) <= 0.15


# ---- R3 start independence ----------------------------------------------------------------------------------
def _alt_starts(spec, k, slots):
    return [("own", [(7.6, 0.9, 12.0, False), (8.4, 0.9, 30.0, False)]),
            ("neighbour", [(8.3, 0.2, 30.0, False), (7.9, 0.2, 12.0, False)])]


def test_proof_r3_other_starts_reach_the_same_split_basin(monkeypatch):
    snap = hidden_snapshot(3.0, asimov=True)
    base = _run(3.0)[0]
    monkeypatch.setattr(R, "_starts", _alt_starts)
    alt = _price(snap, JOB12)
    t0, t1 = _split_terms(base), _split_terms(alt)
    _say(f"R3 model level: {t0} vs {t1}; composition {base.ln_z:.3f} vs {alt.ln_z:.3f} "
         f"({alt.ln_z - base.ln_z:+.3f})")
    assert len(t0) == len(t1) == 1 and abs(t0[0] - t1[0]) <= 0.1
    assert abs(alt.ln_z - base.ln_z) <= 1.5          # mask-discovery spread (inherited screen 5), reported not hidden


# ---- R4 smoothness and R7 magnitude (the wide sweep is slow; three weights are routine) ---------------------
def _sweep(weights):
    d = []
    for w in weights:
        _p2, _p1, dw = _d(w)
        d.append(dw)
        _say(f"R4/R7 w={w}: d(w)={dw:+.3f}")
    return d


def test_proof_r4_r7_three_weights():
    d = _sweep([0.5, 3.0, 12.0])
    assert all(-5.0 <= x <= 1.0 for x in d)                                            # R7
    assert max(d) - min(d) <= 0.5                                                      # R4 (range)


@pytest.mark.slow       # wide synthetic sweep (about 25 pricing jobs, 90 s); the routine subset covers three weights
def test_proof_slow_r1_r4_r7_sweeps_and_the_table():
    weights = [0.25, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 9.0, 12.0]
    d = _sweep(weights)
    steps = [abs(b - a) for a, b in zip(d, d[1:])]
    _say(f"R4 consecutive differences {[round(x, 3) for x in steps]}, range {max(d) - min(d):.3f}")
    _say(f"R7 d(w) range [{min(d):+.3f}, {max(d):+.3f}]")
    fails = 0
    for w, seeds in ((1.5, (1, 2, 5, 9)), (6.0, (1, 2, 5, 9))):
        for seed in seeds:
            p = _run(w, seed=seed, asimov=False)[0]
            ok = p.ln_z is not None and math.isfinite(p.ln_z) and p.status in ("ok", "ok_boundary") and bool(_split_terms(p))
            fails += not ok
            _say(f"R1 w={w} seed={seed}: {p.status} ln_z={p.ln_z} split={bool(_split_terms(p))}")
    for w in (0.5, 3.0, 12.0):
        p = _run(w)[0]
        fails += not (p.ln_z is not None and math.isfinite(p.ln_z) and bool(_split_terms(p)))
    _say(f"R1 failures: {fails} of 11")
    assert fails == 0                                                                  # R1
    assert max(steps) <= 0.1 and max(d) - min(d) <= 0.5                                # R4
    assert all(-5.0 <= x <= 1.0 for x in d)                                            # R7


# ---- R5 order independence and one count -------------------------------------------------------------------
def test_proof_r5_the_two_slot_orders_of_a_hidden_pair_count_once():
    snap = hidden_snapshot(3.0, asimov=True)
    a, b = _run(3.0)[0], _run(3.0, slots=JOB21)[0]
    _say(f"R5 orders: {a.ln_z:.4f} vs {b.ln_z:.4f} ({a.ln_z - b.ln_z:+.4f})")
    assert abs(a.ln_z - b.ln_z) <= 0.15
    dec = R.assign_decide(snap, [_price(snap, JOB1), a, b])
    lz = {tuple(c["slots"]): c["ln_z"] for c in dec.candidates if tuple(c["slots"]) in (JOB12, JOB21)}
    assert set(lz) == {JOB12, JOB21}, lz
    both = float(logsumexp([v for v in lz.values() if v is not None]))
    one = max(v for v in lz.values() if v is not None)
    _say(f"R5 decision: candidates {lz}, lse(both) - one = {both - one:.4f}")
    assert both - one <= 0.05


# ---- R8 the opposite case -----------------------------------------------------------------------------------
def test_proof_r8_a_vanishing_branch_never_beats_the_real_count_by_evidence_alone():
    snap = hidden_snapshot(6.0, b2_at=14.0)
    p2, p1 = _price(snap, JOB12), _price(snap, JOB1)
    _say(f"R8: {p2.status} ln_z2={p2.ln_z} ln_z1={p1.ln_z} reason={p2.reason}")
    assert p2.status in ("ok", "ok_boundary", "undetermined")
    if p2.ln_z is not None and math.isfinite(p2.ln_z):
        assert p2.ln_z - p1.ln_z <= 0.0


# ---- R9 cost ------------------------------------------------------------------------------------------------
def test_proof_r9_one_hidden_job_costs_seconds_not_minutes():
    p, wall, splits = _run(3.0)                             # the counters of the pricing itself, not a second one
    _say(f"R9: one count-2 job {wall:.2f} s ({p.starts} fits); split solves {sum(r.nodes for r in splits)} in "
         f"{sum(r.kind == 'ok' for r in splits)} split basins")
    assert wall < 60.0


# ---- T-undetermined (refit part) ---------------------------------------------------------------------------
def test_a_job_whose_only_basins_are_undetermined_says_so(monkeypatch):
    snap = hidden_snapshot(3.0, asimov=True)

    def fake(self, basin):
        basin.status, basin.reason = "undetermined", "Hessian not positive definite (made up)"
    monkeypatch.setattr(cc.CoupledEngine, "price", fake)
    p = _price(snap, JOB1)
    assert (p.status, p.ln_z, p.mode) == ("undetermined", None, None)
    assert p.reason.startswith("Hessian not positive definite")
    spec = R._Spec(snap)
    jobs, _cov, _lim = spec.jobs()
    dec = R.assign_decide(snap, [R.price(snap, j) for j in jobs])
    assert dec.candidates and all(c["ln_z"] is None and c["status"] == "undetermined" for c in dec.candidates)
    assert (dec.count, dec.slots) == (spec.n0, spec.incoming) and not dec.changed        # nothing priced: incoming stays


def test_all_twins_stay_degenerate_twins_not_undetermined(monkeypatch):
    snap = hidden_snapshot(3.0, asimov=True)

    def fake(self, basin):
        basin.status, basin.reason = "degenerate_twin", "made up"
    monkeypatch.setattr(cc.CoupledEngine, "price", fake)
    assert _price(snap, JOB1).status == "degenerate_twin"


# ---- T-dedupe (refit part) ---------------------------------------------------------------------------------
def test_the_hidden_basin_is_one_record_under_either_slot_order():
    snap = hidden_snapshot(3.0, asimov=True)
    priced = {s: _run(3.0, slots=s)[0] for s in (JOB12, JOB21)}
    recs = [[r for r in priced[s].basins if r[1].endswith("|split")] for s in (JOB12, JOB21)]
    assert [len(x) for x in recs] == [1, 1]
    (a,), (b,) = recs
    spec = R._Spec(snap)
    for rec in (a, b):                                       # the pair at its mean energy, H/2 each
        assert rec[2][0][1] == rec[2][1][1] and rec[2][0][3] == rec[2][1][3]
    assert a[1] == b[1] and sorted(m[0] for m in a[2]) == sorted(m[0] for m in b[2])
    jobs = [_job(snap, JOB12), _job(snap, JOB21)]
    rows = R._basin_rows(spec, jobs, {j.key: priced[j.slots] for j in jobs})[0]
    top = sorted((z for _c, _s, z in rows if z is not None), reverse=True)
    assert top[0] - top[1] > 5.0                             # the hidden basin is one row, not one per slot order
    canon = lambda r: (*r[:2], tuple(sorted(r[2], key=lambda m: (m[1], str(m[0])))), *r[3:])      # noqa: E731
    assert R._same_basin(spec, canon(a), canon(b))           # the base rule's closeness: the same physical basin


# ---- T-representative (record part) ------------------------------------------------------------------------
def _record_of_displaced_start(energies):
    spec, eng, _ = _engines(hidden_snapshot(3.0, asimov=True))
    model = eng.model(eng.variant(2, True, True), ())
    theta = eng._start(model, [(energies[0], 0.4, 40.0, False), (energies[1], 0.4, 3.0, False)],
                       {"elastic": 100.0, "bg": 2.0})
    b = eng._place(model, theta, eng.cost(model, theta), "x", 0, "")
    assert b.status == "pending"
    eng.price(b)
    return eng, b, R._basin_record(b, SLOTS, b.ln_z)


def test_the_record_carries_the_solved_pair_not_the_start():
    eng, b, rec = _record_of_displaced_start((8.0, 7.7))
    assert b.boundary_method == "split" and not np.array_equal(b.theta_map, b.theta)
    assert math.isfinite(eng.cost(b.model, b.theta_map))
    (ai, gi, ci), (aj, gj, cj) = b.model.modes
    assert b.theta_map[ci] + b.theta_map[cj] == pytest.approx(b.theta[ci] + b.theta[cj], rel=0.05)
    term, label, modes, qe, mode = rec
    assert label.endswith("|split") and [m[0] for m in modes] == ["b1", "b2"]
    assert modes[0][1] == modes[1][1] == pytest.approx((b.theta_map[ai] + b.theta_map[aj]) / 2)
    assert modes[0][3] == modes[1][3] == pytest.approx((b.theta_map[ci] + b.theta_map[cj]) / 2)


def test_a_repriced_split_basin_is_not_a_duplicate_of_itself():
    eng, b, _ = _record_of_displaced_start((8.0, 7.7))
    assert b.status == "ok" and b.boundary_method == "split" and eng.n_split_basins == 1
    b.status, b.ln_z, b.boundary_method = "pending", None, ""     # what _place does when an improving refit moves it
    eng.price(b)
    assert b.status == "ok" and b.boundary_method == "split"
    assert eng.n_split_basins == 1


def test_other_starts_record_the_pair_in_the_same_order():
    _e, _b, r1 = _record_of_displaced_start((8.3, 7.9))
    _e, _b, r2 = _record_of_displaced_start((7.9, 8.3))
    assert [m[0] for m in r1[2]] == [m[0] for m in r2[2]] == ["b1", "b2"]
    assert r1[2][0][1] == pytest.approx(r2[2][0][1], abs=0.2)


# ---- T-continuous ------------------------------------------------------------------------------------------
def test_the_weaker_of_two_overlapped_modes_is_held_by_neighbours():
    snap = hidden_snapshot(6.0, asimov=True)
    a, _ = R.assign(snap)
    spec = R._Spec(snap)
    forced = replace(a, count=2, slots=JOB12, factors=spec.incident(JOB12), changed=True, selected_model=None)
    res = R.refit(snap, stage="continuous", assignment=forced)
    assert len(res.modes) == 2
    e = [m.omega0 for m in res.modes]
    assert abs(e[0] - e[1]) < spec.fs.twin_coincide_fwhm * spec.fwhm            # the premise: the pair overlaps
    by = {m.branch: m for m in res.modes}                                       # b1 is the strong branch, b2 the hidden one
    assert by["b1"].height > by["b2"].height, (by["b1"].height, by["b2"].height)
    assert by["b2"].status == "held_by_neighbours" and by["b1"].status != "held_by_neighbours"
