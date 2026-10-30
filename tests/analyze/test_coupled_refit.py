"""The coupled refit core (plan S1U2, checks 1-13) on synthetic spectra generated at test time.

Every spectrum is Poisson counts from DHO modes convolved with a made-up Gaussian resolution (FWHM 1.5 meV,
241 bins); no measured value. Neighbours are hand-made ``Held`` fits; the two-sided spectra sit at 300 K so
the anti-Stokes side is present (and derived). A ceiling of 2 or 3 and the elastic+bg structures keep each
case to a few seconds.
"""
import math
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from isar.analyze import catalogue
from isar.analyze.context import ScanContext
from isar.analyze.coupled import engine as cc
from isar.analyze.coupled import refit as R
from isar.analyze.coupled import rules
from isar.analyze.coupled.settings import InterSettings
from isar.analyze.coupled.types import Held, HeldMode, PricingJob, Snapshot, TermInput
from isar.analyze.estimators import baker_cousins_cost
from isar.analyze.fast_pass import FastPassSettings
from isar.schema.steering import ParamConstraints
from isar.session.inter_spectra.graph import Link, Term

X = np.linspace(-6.0, 24.0, 241)
FWHM = 1.5
STRUCT = ("elastic+bg", "elastic+stokes+bg", "elastic+stokes2+bg", "elastic+stokes3+bg")
S = InterSettings()


class Prepared:                    # the prepared context shape the core reads (ctx, settings, structures)
    def __init__(self, ctx, ceiling):
        self.ctx, self.settings, self.structures = ctx, FastPassSettings(mode_ceiling=ceiling), STRUCT


def _ctx(counts, constraints=None):
    return ScanContext(x=X, counts=np.asarray(counts, float), mask=np.ones(X.size, bool), T=300.0, center=0.0,
                       gauss_fwhm=FWHM, lorentz_fwhm=0.0, instrument="S30", energy_status="absolute",
                       constraints=constraints)


def _label(k):
    return "elastic+bg" if k == 0 else "elastic+stokes+bg" if k == 1 else f"elastic+stokes{k}+bg"


def _expected(modes, elastic=100.0, bg=2.0):
    ctx = _ctx(np.ones(X.size))
    hyp = next(h for h in catalogue.enumerate_count_structures(ctx, len(modes)) if h.label == _label(len(modes)))
    return hyp.build().evaluate(X, [v for m in modes for v in m] + [elastic, bg], ctx)


def spectrum(modes, *, scale=1.0, seed=1, ceiling=2, constraints=None):
    counts = np.random.default_rng(seed).poisson(_expected(modes) * scale).astype(float)
    return Prepared(_ctx(counts, constraints), ceiling)


def _names(k):
    out = []
    for i in range(k):
        p = "phonon" if i == 0 else f"phonon{i + 1}"
        out += [f"{p}.omega0", f"{p}.Gamma", f"{p}.h"]
    return out + ["elastic.h", "bg.BG"]


def record(modes, *, probs=None, elastic=100.0, bg=2.0):
    """A fast-pass-shaped record of the incoming fit (count probability 0.95 unless given)."""
    k, names = len(modes), _names(len(modes))
    params = dict(zip(names, [v for m in modes for v in m] + [elastic, bg]))
    return {"selected_model_id": f"{_label(k)}#0", "count_probabilities": probs or {str(k): 0.95},
            "models": [{"model_id": f"{_label(k)}#0", "label": _label(k), "n_peaks": k, "elastic": True,
                        "background": True, "delta_modes": [], "free": names, "map_params": params}],
            "peaks": [{"omega0": e, "Gamma": g, "height": h, "sigma_omega0": 0.1, "regime": "underdamped"}
                      for e, g, h in sorted(modes)]}


def held(unit, modes, branches, *, guard=0.05, T=None):
    hm = tuple(HeldMode(b, e, g, h, guard, "solo", "determined_by_own_data", "underdamped", False)
               for (e, g, h), b in sorted(zip(modes, branches)))
    return Held(unit=unit, fit="fast", inputs=None, window=(0.75, 24.0), modes=hm, dormant=(), T=T)


def t_pair(prep, own_modes, own_branches, nb_modes, nb_branches, *, rec=None, kind="anchored", settings=S,
           guard=0.05, sid="b1:round1"):
    """The centre y (200 K) between T-neighbours x (100 K) and z (300 K), both holding ``nb_modes``."""
    own = held("y", own_modes, own_branches, T=200.0)
    terms = [TermInput(Term("T-change", ("x", "y"), (100.0,), None, None),
                       (held("x", nb_modes, nb_branches, guard=guard, T=100.0),)),
             TermInput(Term("T-change", ("y", "z"), (100.0,), None, None),
                       (held("z", nb_modes, nb_branches, guard=guard, T=300.0),))]
    return Snapshot(snapshot_id=sid, stage_id=sid, unit="y", fit_id="f00000000000a", context=prep, own=own,
                    own_record=rec or record(own_modes), terms=tuple(terms), settings=settings, batch_kind=kind)


def codes(r):
    return [x["code"] for x in r.flag["reasons"]]


def test_runs_from_the_worktree():
    import isar
    here = __file__.replace("\\", "/").rsplit("/tests/", 1)[0]
    print("import root:", isar.__file__)
    assert isar.__file__.replace("\\", "/").startswith(here)


# 1 ---------------------------------------------------------------------------------------------------------
def test_overlap_is_recovered_without_a_birth_charge():
    # Two branches at 8 and 10 meV in both T-neighbours; the centre's fast solo holds one broad mode at 9 meV
    # on counts too weak for the data alone to prefer two (solo: (None,) beats (None, None)).
    true = [(8.0, 0.3, 30.0), (10.0, 0.3, 30.0)]
    snap = t_pair(spectrum(true, scale=0.08, seed=5), [(9.0, 1.0, 4.0)], ["fast:y#0"], true, ["b1", "b2"])
    r = R.refit(snap, stage="both")
    a = r.assignment
    assert "a:missing_branch" in a.triggers
    assert (a.count, a.slots, a.changed, r.status) == (2, ("b1", "b2"), True, "accepted")
    assert not any(k.startswith("q_U") for k in r.normalisers)
    assert "neighbour_supported_split" in codes(r)
    split = next(x for x in r.flag["reasons"] if x["code"] == "neighbour_supported_split")
    assert split["severity"] == "info"
    solo = {tuple(c["slots"]): c["ln_z"] for c in a.candidates}
    assert solo[(None,)] > solo[(None, None)]


def test_a_split_is_a_repair_not_a_review_flag_but_a_neighbour_count_change_is():
    # one rule (design, Flags): a neighbour-supported split reads "repaired" and stays off the review list;
    # a count the neighbours changed otherwise is a warn and is flagged
    spec = SimpleNamespace(n0=1, incoming=(None,), pool=(), pred={}, band=1.0, d_edge=lambda e: 0.0)
    snap, a = SimpleNamespace(settings=S, unit="y"), SimpleNamespace(changed=True, reasons=())

    def flag(slots, energies):
        model = SimpleNamespace(modes=[(m,) for m in range(len(slots))])
        return R._flags(snap, spec, a, "accepted", slots, [], model, np.array(energies), [], {}, 0.0, 0.0,
                        object(), [], [])
    split, merge = flag(("b1", "b2"), [8.0, 10.0]), flag((), [])
    assert [r["code"] for r in split["reasons"]] == ["neighbour_supported_split"] and not split["flagged"]
    assert [r["code"] for r in merge["reasons"]] == ["count_changed_by_neighbours"] and merge["flagged"]


# 2 ---------------------------------------------------------------------------------------------------------
def _entry_snapshot(settings=S):
    true = [(8.0, 0.4, 40.0), (15.0, 0.4, 60.0)]
    return t_pair(spectrum(true, seed=8), true, ["b1", "fast:y#1"], [(8.0, 0.4, 40.0)], ["b1"], settings=settings)


def test_a_strong_unmatched_peak_mid_window_is_kept_and_flagged():
    r = R.refit(_entry_snapshot(), stage="both")
    assert r.assignment.slots == ("b1", None) and r.status in ("accepted", "unchanged")
    assert "unmatched_branch_mid_window" in codes(r) and r.flag["flagged"]
    assert "q_U:1" in r.normalisers and "ln_z:b1" in r.normalisers


def test_the_entry_price_stays_in_the_composition_log_mass():
    snap = _entry_snapshot()
    spec = R._Spec(snap)
    eng = R._Labelled(spec.ctx, spec.fs, spec.slot_objects(("b1", None)), structures=STRUCT)
    model = eng.model(eng.variant(2, True, True), ())
    theta = np.array([8.0, 0.4, 40.0, 15.0, 0.4, 60.0, 100.0, 2.0])
    edge = spec.lo + 0.3 * spec.band                     # inside the physical edge band: B = B_mid H(0.3)
    at = {e: eng.ln_prior(model, np.where(np.arange(8) == 3, e, theta)) for e in (15.0, edge)}
    solo = spec.ep.log_pdf(15.0) - spec.ep.log_pdf(edge)
    B = spec.entry.B
    assert B(15.0) == S.birth_mid and 0 < B(edge) < S.birth_mid
    assert (at[15.0] - at[edge]) - solo == pytest.approx(-(B(15.0) - B(edge)) / 2, abs=1e-6)

    # a constant entry price lowers a one-unmatched composition's log mass by B_edge/2 exactly once
    job = PricingJob("y", snap.snapshot_id, 2, ("b1", None), "k")
    free = R.price(_entry_snapshot(replace(S, birth_edge=0.0, birth_mid=0.0)), job)
    priced = R.price(_entry_snapshot(replace(S, birth_edge=4.0, birth_mid=0.0)), job)
    assert priced.ln_z - free.ln_z == pytest.approx(-2.0, abs=1e-4)


# 3 ---------------------------------------------------------------------------------------------------------
def test_a_lost_branch_is_a_flag_only():
    one = [(8.0, 0.4, 40.0)]
    both = one + [(15.0, 0.4, 40.0)]
    prep = spectrum(one, seed=9)
    snap = t_pair(prep, one, ["b1"], both, ["b1", "b2"])
    r = R.refit(snap, stage="both")
    assert "a:missing_branch" in r.assignment.triggers
    assert r.assignment.slots == ("b1",) and "branch_lost_mid_window" in codes(r)
    # the kept composition's log mass differs from the same fit without b2 in the pool only by ln w0 (M = 2 vs 1)
    job = PricingJob("y", snap.snapshot_id, 1, ("b1",), "k")
    with_b2 = R.price(snap, job)
    without = R.price(t_pair(prep, one, ["b1"], one, ["b1"]), job)
    assert with_b2.ln_z - without.ln_z == pytest.approx(rules.ln_w0(1, 1, 2, 3) - rules.ln_w0(1, 1, 1, 3), abs=1e-9)


# 4 ---------------------------------------------------------------------------------------------------------
def test_a_broad_ridge_is_held_by_neighbours_until_the_counts_pin_it():
    true = [(6.0, 6.0, 10.0)]
    weak = R.refit(t_pair(spectrum(true, scale=0.5, seed=2), true, ["b1"], true, ["b1"]), stage="both")
    strong = R.refit(t_pair(spectrum(true, scale=5.0, seed=2), true, ["b1"], true, ["b1"]), stage="both")
    assert weak.modes[0].status == "held_by_neighbours"
    assert strong.modes[0].status == "determined_by_own_data"


# 5 ---------------------------------------------------------------------------------------------------------
def test_a_width_change_is_reported_without_an_energy_score():
    r = R.refit(t_pair(spectrum([(8.0, 1.0, 40.0)], seed=10), [(8.0, 0.4, 40.0)], ["b1"], [(8.0, 0.4, 40.0)],
                       ["b1"]), stage="both")
    assert r.assignment.discrete == "skipped"
    assert "linewidth_change" in codes(r) and r.F < S.flag_threshold
    r = R.refit(t_pair(spectrum([(5.0, 7.0, 60.0)], seed=10), [(5.0, 1.0, 60.0)], ["b1"], [(5.0, 1.0, 60.0)],
                       ["b1"]), stage="both")
    assert "overdamped_entry" in codes(r)


# 6 ---------------------------------------------------------------------------------------------------------
def test_a_bounded_jump_flags_without_being_dragged():
    snap = t_pair(spectrum([(12.0, 0.4, 40.0)], seed=4), [(12.0, 0.4, 40.0)], ["b1"], [(8.0, 0.4, 40.0)], ["b1"])
    kept = R._Spec(snap).skipped()                    # the matched assignment, held through the continuous stage
    r = R.refit(snap, stage="continuous", assignment=kept)
    assert abs(r.delta_fast["omega0"]["b1"]) < 0.05
    assert r.F > S.flag_threshold and "score_over_threshold" in codes(r)
    assert max(r.U.values()) > max(r.P.values())          # U unlevelled, P saturated at the cap
    flat = t_pair(spectrum([(8.3, 0.4, 40.0)], seed=4), [(8.3, 0.4, 40.0)], ["b1"], [(8.0, 0.4, 40.0)], ["b1"])
    assert R.refit(flat, stage="both").F < S.flag_threshold
    # the trigger filter prices the unmatched alternative, which wins: still flagged, by its own reasons
    r = R.refit(snap, stage="both")
    assert "b:large_mismatch" in r.assignment.triggers and r.flag["flagged"]


# 7 ---------------------------------------------------------------------------------------------------------
def _factor(dT, settings=S):
    own = held("y", [(8.0, 0.4, 40.0)], ["b1"], guard=0.0)
    term = TermInput(Term("T-change", ("x", "y"), (dT,), None, None), (held("x", [(8.0, 0.4, 40.0)], ["b1"], guard=0.0),))
    snap = Snapshot("s", "s", "y", "f1", spectrum([(8.0, 0.4, 40.0)]), own, record([(8.0, 0.4, 40.0)]), (term,),
                    settings, "simultaneous")
    return R._Spec(snap).factors["b1"][0]


def test_spacing_widens_the_tolerance_and_every_factor_caps():
    f10, f100 = _factor(10.0), _factor(100.0)
    assert f10.s / f100.s == pytest.approx((10 / 100) ** 0.25, rel=1e-12)
    for f in (f10, f100):
        assert cc.potential(f, 1e3) == pytest.approx(f.C) and cc.potential(f, 1e3) <= f.C
    div = _factor(10.0, replace(S, t_spacing_form="divide"))
    assert div.s == S.t_tolerance_meV and div.d == pytest.approx(0.1)
    assert cc.potential(div, 1.0) == pytest.approx(cc.potential(replace(div, d=0.0), 1.0) / 0.1)
    q = replace(f100, family="quartic", s=1.0, C=12.0)
    for z in (0.5, 2.0):
        assert cc.potential(q, z) == pytest.approx(12.0 * z ** 4 / (1 + z ** 4))
        assert cc.mismatch(q, z) == pytest.approx(12.0 * z ** 4)
    big = cc.potential(q, 1e80)
    assert math.isfinite(big) and big <= 12.0


# 8 ---------------------------------------------------------------------------------------------------------
def test_acceptance_order(monkeypatch):
    prep = spectrum([(9.0, 0.5, 40.0)], seed=6)
    snap = t_pair(prep, [(9.3, 0.5, 40.0)], ["b1"], [(9.0, 0.5, 40.0)], ["b1"])
    first = R.refit(snap, stage="both")
    assert (first.status, first.reason) == ("accepted", "objective")       # J fell by more than epsilon_J
    own = R.held_from_fit(first.fit, unit="y", fit="f00000000000a", inputs=None, context=prep, T=200.0)
    again = R.refit(replace(snap, own=own, own_record=first.fit), stage="both")
    assert again.status == "unchanged"
    assert again.fit["models"][0]["params"] == first.fit["models"][0]["params"]   # the incoming numbers exactly

    # a topology change the evidence did not win
    unmatched = t_pair(prep, [(9.3, 0.5, 40.0)], ["fast:y#0"], [(9.0, 0.5, 40.0)], ["b1"])
    spec = R._Spec(unmatched)
    lost = replace(spec.skipped(), slots=("b1",), origins=("matched",), factors=spec.incident(("b1",)))
    r = R.refit(unmatched, stage="continuous", assignment=lost)
    assert (r.status, r.reason) == ("rejected", "no_supported_improvement")

    monkeypatch.setattr(R._Labelled, "_trf", lambda self, model, theta0, family: None)
    r = R.refit(snap, stage="both")
    assert (r.status, r.reason, r.fit, r.F) == ("failed", "no_finite_candidate", None, None)


# 9 ---------------------------------------------------------------------------------------------------------
def test_a_shared_factor_is_one_definition_counted_once():
    true = [(8.0, 0.4, 40.0)]
    term = Term("curvature", ("l", "c", "r"), (0.1, 0.3), 0.25, "row 1")
    hs = {u: held(u, true, ["b1"], guard=0.1) for u in "lcr"}
    factors = []
    for u in "lcr":
        snap = Snapshot("s", "s", u, "f1", spectrum(true), hs[u], record(true),
                        (TermInput(term, tuple(hs[m] for m in "lcr" if m != u)),), S, "simultaneous")
        factors.append(R._Spec(snap).factors["b1"][0])
    assert factors[0] == factors[1] == factors[2]
    assert factors[0].a == (-0.75, 1.0, -0.25)
    E = {(m, factors[0].factor_id): e for m, e in (("l", 8.0), ("c", 8.6), ("r", 8.0))}
    J, P = R.total_objective(factors, E, {"l": 1.0, "c": 2.0, "r": 3.0}, {"c": 0.5})
    assert len(P) == 1 and J == pytest.approx(6.5 + cc.potential(factors[0], 0.6))
    with pytest.raises(ValueError):
        R.total_objective([factors[0], replace(factors[0], s=2.0)], E, {}, {})


def test_a_link_between_two_fast_solos_is_one_definition_once_their_ids_are_reconciled():
    # a first simultaneous batch: x and y carry their own fast ids, each assigns its mode to the other's branch;
    # the stage reconciles both to "B". The link's scale holds both frozen guards, whichever end is read first.
    from isar.session.inter_spectra import runner
    true = [(8.0, 0.4, 40.0)]
    term = Term("T-change", ("x", "y"), (100.0,), None, None)
    own = {"x": held("x", true, ["fast:x#0"], guard=0.1, T=100.0), "y": held("y", true, ["fast:y#0"], guard=1.0, T=200.0)}
    other = {"x": "y", "y": "x"}
    snaps = {u: Snapshot("b:round1", "b:round1", u, f"f{u}", spectrum(true), own[u], record(true),
                         (TermInput(term, (own[other[u]],)),), S, "simultaneous") for u in "xy"}
    A = {}
    for u, s in snaps.items():
        spec, slots = R._Spec(s), (f"fast:{other[u]}#0",)
        A[u] = replace(spec.skipped(), slots=slots, factors=spec.incident(slots))
    ids = SimpleNamespace(ids={(u, f"f{u}", 0): "B" for u in "xy"},
                          alternatives={"aliases": {"B": ["fast:x#0", "fast:y#0"]}})
    refs = {u: [(u, f"f{u}", 0)] for u in "xy"}
    A, rename = runner.canonical_factors(A, ids, refs, {})
    A = runner.rebuilt_factors(R, A, snaps, rename)
    (fx,), (fy,) = A["x"].factors, A["y"].factors
    assert fx == fy and fx.branch_id == "B"
    base = S.t_tolerance_meV * (100.0 / S.reference_T_K) ** S.step_exponent
    assert fx.s == pytest.approx(math.sqrt(base ** 2 + 0.1 ** 2 + 1.0 ** 2))
    E = {("x", fx.factor_id): 8.0, ("y", fx.factor_id): 8.5}
    assert R.total_objective([fx, fy], E, {}, {}) == R.total_objective([fy, fx], E, {}, {})


# 10 --------------------------------------------------------------------------------------------------------
def test_identical_snapshots_reuse_prices_and_any_move_reprices():
    true = [(9.0, 0.5, 40.0)]
    snap = t_pair(spectrum(true, seed=11), true, ["b1"], true, ["b1"], rec=record(true, probs={"1": 0.6, "2": 0.4}))
    a, priced = R.assign(snap)
    assert a.discrete == "priced" and priced
    cache = {p.key: p for p in priced}
    later = replace(snap, snapshot_id="b1:round2", stage_id="b1:round2")
    none, jobs = R.assign_jobs(later, cached=cache)
    assert none is None and jobs == ()
    decided = R.assign_decide(later, list(cache.values()))
    assert (decided.count, decided.slots, decided.candidates) == (a.count, a.slots, a.candidates)
    x = snap.terms[0].held[0]
    nudged = replace(x, modes=(replace(x.modes[0], omega0=x.modes[0].omega0 + 1e-9),))
    moved = replace(later, terms=(replace(snap.terms[0], held=(nudged,)), snap.terms[1]))
    _none, jobs = R.assign_jobs(moved, cached=cache)
    assert len(jobs) == len(priced)


# 11 --------------------------------------------------------------------------------------------------------
def _ring_mate(unit, e):
    return SimpleNamespace(unit=unit, modes=(HeldMode("b1", e, 0.4, 40.0, 0.0, "max", "determined_by_own_data",
                                                      "underdamped", False),))


def test_a_seam_reports_unlevelled_mismatch_beyond_the_zero_zone():
    link = Link("q", "a", "b", 0.1, None)
    far = R.seam_report(_ring_mate("a", 8.0), _ring_mate("b", 14.0), link, S)
    assert far["over"] and far["max_U"] > S.flag_threshold
    assert far["pairs"][0]["P"] < S.penalty_cap                # the Welsch potential has saturated
    near = R.seam_report(_ring_mate("a", 8.0), _ring_mate("b", 9.5), link, S)
    assert near["max_U"] == 0.0 and not near["over"]


# 12 --------------------------------------------------------------------------------------------------------
def test_raw_poisson_counts_and_a_derived_anti_stokes_amplitude():
    true = [(7.0, 0.5, 6.0)]
    prep = spectrum(true, scale=0.2, seed=12)
    y = prep.ctx.counts
    assert np.any(y == 0)
    r = R.refit(t_pair(prep, true, ["b1"], true, ["b1"]), stage="both")
    model = r.fit["models"][0]
    hyp = next(h for h in catalogue.enumerate_count_structures(prep.ctx, 1) if h.label == model["label"])
    names = hyp.build().param_names()
    mu = hyp.build().evaluate(X, [model["params"][n] for n in names], prep.ctx)
    assert r.D == pytest.approx(baker_cousins_cost(y, mu), rel=1e-9)
    assert set(model["free"]) == {"phonon.omega0", "phonon.Gamma", "phonon.h", "elastic.h", "bg.BG"}
    # the anti-Stokes peak is there, tied by the Bose factor: no coordinate of its own above
    at = {e: mu[np.argmin(abs(X - e))] for e in (-7.0, -4.0)}
    assert at[-7.0] > at[-4.0]


# 13 --------------------------------------------------------------------------------------------------------
def test_a_pinned_unmatched_energy_pays_its_entry_price_once():
    pin = ParamConstraints(hard={"phonon.omega0": 12.0})
    prep = spectrum([(12.0, 0.4, 40.0)], seed=13, constraints=pin)
    zero = replace(S, birth_edge=0.0, birth_mid=0.0)

    def ln_z(settings, slots, nb):
        snap = t_pair(prep, [(12.0, 0.4, 40.0)], ["fast:y#0"], nb, ["b1"], settings=settings)
        return R.price(snap, PricingJob("y", "s", 1, slots, "k")).ln_z

    nb = [(12.0, 0.4, 40.0)]
    assert ln_z(S, (None,), nb) - ln_z(zero, (None,), nb) == pytest.approx(-(S.birth_edge + S.birth_mid) / 2, abs=1e-9)
    assert ln_z(S, ("b1",), nb) - ln_z(zero, ("b1",), nb) == pytest.approx(0.0, abs=1e-9)


# repair batch 1 -------------------------------------------------------------------------------------------
def _short(top):
    """A spectrum measured only up to ``top`` meV (the 20 meV prior cap does not truncate it)."""
    x = np.linspace(-6.0, top, 201)
    ctx = ScanContext(x=x, counts=np.ones(x.size), mask=np.ones(x.size, bool), T=300.0, center=0.0,
                      gauss_fwhm=FWHM, lorentz_fwhm=0.0, instrument="S30", energy_status="absolute")
    return Prepared(ctx, 2)


def test_the_prior_cap_is_an_entry_edge_only_where_it_is_the_observation_edge():
    nb = [(8.0, 0.4, 40.0)]
    past = R._Spec(t_pair(spectrum(nb, seed=8), nb, ["b1"], nb, ["b1"]))      # measured to 24 meV, capped at 20
    assert past.hi == 20.0 and not past.entry.physical_hi
    near_cap = past.hi - 0.3 * past.band
    assert past.entry.B(near_cap) == S.birth_mid and past.d_edge(near_cap) > 0.3 * past.band
    edge = R._Spec(t_pair(_short(18.0), nb, ["b1"], nb, ["b1"]))                # the cap is not reached
    assert edge.hi == 18.0 and edge.entry.physical_hi
    assert edge.entry.B(edge.hi) == 0.0 and edge.d_edge(edge.hi) == 0.0


def test_a_near_tied_exchange_prices_but_a_clear_association_stays_skipped():
    own = [(9.0, 0.4, 40.0), (9.4, 0.4, 40.0)]
    near = t_pair(spectrum(own, seed=3), own, ["b1", "b2"], [(9.1, 0.4, 40.0), (9.3, 0.4, 40.0)], ["b1", "b2"])
    spec = R._Spec(near)                              # the incoming association is the cheapest; the exchange is not far
    assert spec.triggers == ("c:close_association",) and spec.counts == (2,)
    assert R.assign_jobs(near)[0] is None
    apart = [(9.0, 0.4, 40.0), (14.0, 0.4, 40.0)]
    clear = t_pair(spectrum(apart, seed=3), apart, ["b1", "b2"], [(9.1, 0.4, 40.0), (13.9, 0.4, 40.0)], ["b1", "b2"])
    assert R._Spec(clear).triggers == () and R.assign_jobs(clear)[0].discrete == "skipped"


def test_a_poorly_pinned_unmatched_mode_is_not_held_by_neighbours():
    true = [(6.0, 6.0, 10.0)]
    weak = t_pair(spectrum(true, scale=0.5, seed=2), true, ["fast:y#0"], true, ["b1"])
    r = R.refit(weak, stage="continuous", assignment=R._Spec(weak).skipped())       # slot None: no neighbour factor
    assert r.assignment.slots == (None,) and r.modes[0].status == "poorly_determined"
    held = R.refit(t_pair(spectrum(true, scale=0.5, seed=2), true, ["b1"], true, ["b1"]), stage="both")
    assert held.modes[0].status == "held_by_neighbours"                              # a matched slot still reads it


def test_a_refit_without_a_release_sigma_hands_no_conditional_precision_to_its_neighbours():
    # the refit's own sigma is conditional on its neighbours; without a release sigma its guard is unavailable,
    # so a later neighbour's factor widens by the window fallback and notes it
    true = [(9.0, 0.5, 40.0)]
    prep = spectrum(true)
    rec = {**record(true), "peaks": [{"omega0": 9.0, "Gamma": 0.5, "height": 40.0, "sigma_omega0": 0.15,
                                      "sigma_release": None, "mode_status": "held_by_neighbours", "branch": "b1"}]}
    x = R.held_from_fit(rec, unit="x", fit="f00000000000b", inputs=None, context=prep, T=100.0)
    assert (x.modes[0].guard, x.modes[0].guard_source) == (None, "unavailable")
    both = R.held_from_fit({**rec, "peaks": [{**rec["peaks"][0], "sigma_release": 0.4}]}, unit="x",
                           fit="f00000000000b", inputs=None, context=prep, T=100.0)
    assert (both.modes[0].guard, both.modes[0].guard_source) == (0.4, "max")
    snap = Snapshot("s", "s", "y", "f1", prep, held("y", true, ["b1"], T=200.0), record(true),
                    (TermInput(Term("T-change", ("x", "y"), (100.0,), None, None), (x,)),), S, "anchored")
    spec = R._Spec(snap)
    (f,) = spec.factors["b1"]
    base = S.t_tolerance_meV * (100.0 / S.reference_T_K) ** S.step_exponent
    fallback = (x.window[1] - x.window[0]) / math.sqrt(12.0)
    assert f.s == pytest.approx(math.sqrt(base ** 2 + fallback ** 2))
    assert [n["code"] for n in spec.notes] == ["neighbour_precision_unavailable"]


def test_a_missing_seam_guard_widens_the_tolerance_instead_of_vanishing():
    link = Link("q", "a", "b", 0.1, None)
    ok, lost = _ring_mate("b", 14.0), _ring_mate("b", 14.0)
    lost.modes = (replace(lost.modes[0], guard=None, guard_source="unavailable"),)
    for mate in (ok, lost):
        mate.fit = {"energy_support_meV": [0.0, 22.0]}
    known = R.seam_report(_ring_mate("a", 8.0), ok, link, S)
    unknown = R.seam_report(_ring_mate("a", 8.0), lost, link, S)
    assert known["over"] and not unknown["over"]                       # ell/sqrt(12) = 6.4 meV, not 0
    assert unknown["pairs"][0]["guard_unavailable"] and unknown["pairs"][0]["U"] < known["pairs"][0]["U"]
    # a real refit's record carries the window it was fitted in
    snap = t_pair(spectrum([(8.0, 0.4, 40.0)], seed=1), [(8.0, 0.4, 40.0)], ["b1"], [(8.0, 0.4, 40.0)], ["b1"])
    assert R._ell(R.refit(snap, stage="both")) == pytest.approx(R._Spec(snap).ell)


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_a_resolution_limited_mode_stays_delta_through_the_continuous_stage(seed):
    # the incoming fit holds its mode at the delta width; with no priced comparison the continuous stage keeps
    # that mask: a free width could only win on noise, never through the evidence comparison
    from isar.analyze.phonon import GAMMA_FLOOR
    true = [(9.0, GAMMA_FLOOR, 40.0)]
    rec = record(true)
    rec["models"][0].update(delta_modes=[0], free=[n for n in _names(1) if n != "phonon.Gamma"])
    snap = t_pair(spectrum(true, seed=seed), true, ["b1"], true, ["b1"], rec=rec)
    a = R._Spec(snap).skipped()
    r = R.refit(snap, stage="continuous", assignment=a)
    assert r.status in ("accepted", "unchanged")
    assert R._selected_model(r.fit)["delta_modes"] == [0] and r.fit["peaks"][0]["delta"]


def test_a_continuing_unmatched_mode_keeps_its_id_across_sweeps():
    snap = _entry_snapshot()
    for sweep in range(3):
        r = R.refit(snap, stage="both")
        assert r.status in ("accepted", "unchanged") and r.assignment.slots == ("b1", None)
        assert [m.branch for m in r.modes] == ["b1", "fast:y#1"]
        own = R.held_from_fit(r.fit, unit="y", fit=f"f0000000000{sweep}", inputs=None, context=snap.context, T=200.0)
        snap = replace(snap, own=own, own_record=r.fit, fit_id=f"f0000000001{sweep}")
    born = R.refit(t_pair(spectrum([(8.0, 0.4, 40.0), (15.0, 0.4, 60.0)], seed=8), [(8.0, 0.4, 40.0)], ["b1"],
                          [(8.0, 0.4, 40.0)], ["b1"], rec=record([(8.0, 0.4, 40.0)], probs={"1": 0.5, "2": 0.5})),
                   stage="both")
    new = [m.branch for m in born.modes if m.branch != "b1"]
    assert born.assignment.count == 2 and len(new) == 1 and new[0].startswith("f00000000000a#")   # a birth is minted


def test_widths_and_shifts_are_compared_with_the_fast_solo_not_the_last_sweep():
    # data 3x the fast solo's width; the incoming refit (sweep 2) already sits at 2x, so only 1.5x per sweep
    own, fast = [(8.0, 0.8, 40.0)], [(8.5, 0.4, 40.0)]
    snap = t_pair(spectrum([(8.0, 1.2, 40.0)], seed=10), own, ["b1"], [(8.0, 0.4, 40.0)], ["b1"])
    vs_own = R.refit(snap, stage="both")
    assert "linewidth_change" not in codes(vs_own)
    r = R.refit(replace(snap, fast=held("y", fast, ["b1"], T=200.0)), stage="both")
    assert "linewidth_change" in codes(r)
    assert r.delta_fast["omega0"]["b1"] == pytest.approx(r.energies["b1"] - 8.5)       # change since the fast solo
    assert r.delta_fast["count"] == 0
    # a fast solo carrying other ids is paired by energy
    other = R.refit(replace(snap, fast=held("y", fast, ["fast:y#0"], T=200.0)), stage="both")
    assert "linewidth_change" in codes(other) and other.delta_fast["omega0"]["b1"] == r.delta_fast["omega0"]["b1"]


def test_only_exchangeable_unmatched_modes_get_the_factorial():
    two = [(5.0, 0.4, 40.0), (12.0, 0.4, 40.0)]
    spec = R._Spec(t_pair(spectrum(two, seed=3), two, ["fast:y#0", "fast:y#1"], [(8.0, 0.4, 40.0)], ["b1"]))
    both = R._Labelled(spec.ctx, spec.fs, spec.slot_objects((None, None)), structures=STRUCT)
    variant = both.variant(2, True, True)
    assert both._ln_exch(both.model(variant, ())) == pytest.approx(math.log(2.0))      # two DHOs: ln 2!
    assert both._ln_exch(both.model(variant, (1,))) == 0.0                             # a delta and a DHO: none
    one = R._Labelled(spec.ctx, spec.fs, spec.slot_objects(("b1", None)), structures=STRUCT)
    assert one._ln_exch(one.model(variant, ())) == 0.0                                 # one unmatched: none


def test_a_split_has_no_fast_comparison_without_a_one_to_one_partner():
    true = [(8.0, 0.3, 30.0), (10.0, 0.3, 30.0)]
    solo = [(9.0, 1.0, 4.0)]
    snap = t_pair(spectrum(true, scale=0.08, seed=5), solo, ["fast:y#0"], true, ["b1", "b2"])
    r = R.refit(replace(snap, fast=held("y", solo, ["fast:y#0"], T=200.0)), stage="both")
    assert r.assignment.count == 2 and "neighbour_supported_split" in codes(r)
    assert "linewidth_change" not in codes(r) and "overdamped_entry" not in codes(r)
    assert r.delta_fast["omega0"] == {} and r.delta_fast["count"] == 1


def test_the_objective_reported_includes_the_soft_constraint_once():
    soft = ParamConstraints(soft={"phonon.omega0": (8.6, 0.1)})
    modes = [(8.0, 0.4, 40.0)]
    r = R.refit(t_pair(spectrum(modes, seed=8, constraints=soft), modes, ["b1"], modes, ["b1"]), stage="both")
    assert r.status in ("accepted", "unchanged")
    z = ((r.energies["b1"] - 8.6) / 0.1) ** 2
    assert z > 1.0                                              # the pull is live
    # the block's J is D + every penalty + the soft square; unary must carry the square, not drop it
    assert r.unary >= z - 1e-9
    assert r.J == pytest.approx(r.D + sum(r.P.values()) + r.unary, rel=1e-9, abs=1e-9)


# BL43LXU: the fast pass's central QE is a per-spectrum nuisance of the coupled refit -----------------------
def test_the_coupled_refit_carries_the_central_qe_as_a_nuisance():
    from isar.analyze import fast_pass, prior
    from isar.analyze.components import CentralQE, CompositeModel, ConstantBackground, Elastic, PhononDHO
    from isar.synth import bl43_curves
    x = np.arange(-5.0, 10.0, 0.25)
    ctx0 = ScanContext(x=x, counts=np.ones(x.size), mask=np.ones(x.size, bool), T=97.0, center=0.0,
                       gauss_fwhm=0.0, lorentz_fwhm=0.0, instrument="BL43LXU", energy_status="absolute",
                       resolution_curve=bl43_curves.curve("a06"))
    mean = CompositeModel([PhononDHO(), Elastic(), CentralQE(), ConstantBackground()]).evaluate(
        x, [5.0, 0.6, 40.0, 200.0, 20.0, 40.0, 3.0], ctx0)
    ctx = replace(ctx0, counts=np.random.default_rng(3).poisson(mean).astype(float))
    fs = FastPassSettings(mode_ceiling=1)
    base = fast_pass._Engine(ctx, fs)
    slot = cc.MatchedDensity(base.sp.energy, "u", [], {})
    eng = R._Labelled(ctx, fs, [slot])
    v = next(v for v in eng.variants[1] if v.qe and v.elastic and v.background)
    out = eng.fit(v, [(5.0, 0.6, 40.0, False)], "own")            # StopIteration in _start before the fix
    assert out is not None
    theta = out[0].theta
    model = eng.model(v, ())
    assert model.qe_h is not None and model.qe_w is not None and np.isfinite(theta[model.qe_w])
    # the coupled prior is the fast pass's with only the energy factor swapped for the slot's density
    bmodel = base.model(base.variant(1, True, True, True), ())
    assert bmodel.names == model.names
    energy = sum(base.sp.ln_energies([theta[a] for a in grp]) for grp in model.groups)
    ref = base.ln_prior(bmodel, theta) - energy
    assert eng.ln_prior(model, theta) - slot.ln_pdf(float(theta[model.modes[0][0]])) == pytest.approx(ref)
    sp = base.sp                                                  # and that remainder includes the QE's two factors
    assert ref == pytest.approx(sp.ln_width(theta[model.modes[0][1]]) + sp.ln_height(theta[model.modes[0][2]])
                                + sp.ln_height(theta[model.el]) + sp.ln_background(theta[model.bg])
                                + sp.ln_height(theta[model.qe_h]) + sp.ln_qe_width(theta[model.qe_w]))
