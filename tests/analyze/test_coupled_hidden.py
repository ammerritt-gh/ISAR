"""Hidden-mode pricing in the coupled engine (S1U1, packet P1): the twin-screen guard, the split integral's
numerics, the undetermined status and the dedupe ledger, on the synthetic three-spectrum case of
``tests/inter_hidden_case.py`` (counts generated at test time, no measured value).

The refit-level proof (R1-R9 through ``refit.price``) belongs to packet P2 and lives in tests named ``*proof*``.
"""
import math

import numpy as np
import pytest
from scipy.integrate import quad

from inter_hidden_case import STRUCT, Prepared, _ctx, _expected, hidden_snapshot
from isar.analyze import fast_pass, prior
from isar.analyze.coupled import engine as cc
from isar.analyze.coupled import refit as R

SLOTS = ("b1", "b2")


def _engines(snap, slots=SLOTS):
    """(spec, engine with the neighbour densities, engine with two unmatched entries) on one snapshot."""
    spec = R._Spec(snap)
    kw = dict(structures=spec.structures)
    return (spec, R._Labelled(spec.ctx, spec.fs, spec.slot_objects(slots), **kw),
            R._Labelled(spec.ctx, spec.fs, [spec.entry] * len(slots), **kw))


def _placed(spec, k=2, slots=SLOTS):
    return R._starts(spec, k, slots)[-1][1]


def test_runs_from_the_worktree():
    import isar
    here = __file__.replace("\\", "/").rsplit("/tests/", 1)[0]
    print("import root:", isar.__file__)
    assert isar.__file__.replace("\\", "/").startswith(here)


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


def test_the_split_integral_matches_laplace_on_a_resolved_pair():           # R6: Jacobian and prior bookkeeping
    modes = [(6.0, 0.4, 40.0), (9.0, 0.4, 40.0)]
    counts = np.random.default_rng(1).poisson(_expected(modes) * 0.2).astype(float)
    ctx = _ctx(counts)
    s = fast_pass.FastPassSettings(mode_ceiling=2)
    sp = prior.resolve_prior_for(ctx, s.prior or prior.PriorSettings())
    eng = cc.CoupledEngine(ctx, s, [cc.Entry(sp.energy, band=1.0, b_edge=0.0, b_mid=0.0)] * 2)
    b, _ = eng.fit(eng.variant(2, True, True), [(6.0, 0.4, 8.0, False), (9.0, 0.4, 8.0, False)], "x")
    eng.price(b)
    assert b.status in ("ok", "ok_boundary") and b.boundary_method != "split"
    res = eng.split_evidence(b, b.theta_map, (0, 1), sets=(24, 48))
    assert res.kind == "ok", res.reason
    assert res.ln_z == pytest.approx(b.ln_z, abs=0.25)


# ---- T-dedupe (engine part) -------------------------------------------------------------------------------
def _three_mode_snapshot():
    """The hidden pair at 8 meV plus one separate unmatched peak at 20 meV (a count-3 case)."""
    from dataclasses import replace
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
    eng._split_done[model] = [key(base)]
    assert eng._seen_split(model, key(_placed3(eng, model, 20.1, (30.0, 13.0))))   # another split, one solution
    assert eng._seen_split(model, key(_placed3(eng, model, 20.0, (3.0, 40.0))))    # the other slot order
    assert not eng._seen_split(model, key(_placed3(eng, model, 13.0, (40.0, 3.0))))    # a third mode elsewhere
    other = base.copy()
    other[model.el] = 160.0
    assert not eng._seen_split(model, key(other))              # same_basin ignores the elastic height; this does not
    other = base.copy()
    other[model.bg] = 4.0
    assert not eng._seen_split(model, key(other))
