"""The loss price (S2U2): a branch that disappears pays what a branch that appears pays.

Every eligible in-window branch a neighbour carries that a candidate does not name costs the candidate
``L / 2`` nats, ``L = birth_edge + B(E_exp)`` from the entry price's own ``Entry``, once per spectrum, inside
every basin term, frozen per snapshot and absent from the continuous objective. Synthetic spectra generated at
test time (hard rule 10); helpers come from ``test_coupled_refit``. The expensive cases print the margins the
slice report quotes (run with ``-s``).
"""
import math
from dataclasses import replace

import pytest
from test_coupled_refit import S, _short, held, spectrum, t_pair

from isar.analyze.coupled import refit as R
from isar.analyze.coupled.types import Dormant, PricingJob, TermInput
from isar.session.inter_spectra.graph import Term

ZERO = replace(S, birth_edge=0.0, birth_mid=0.0)      # entry and loss prices off
ONE = [(8.0, 0.4, 40.0)]
BOTH = ONE + [(15.0, 0.4, 40.0)]


def test_runs_from_the_worktree():
    import isar
    here = __file__.replace("\\", "/").rsplit("/tests/", 1)[0]
    print("import root:", isar.__file__)
    assert isar.__file__.replace("\\", "/").startswith(here)


def _job(slots):
    return PricingJob("y", "b1:round1", len(slots), slots, "k")


def _with_neighbours(snap, x, z, *, guard=0.05):
    """``snap`` with neighbours x and z replaced by ``(modes, branches)`` pairs."""
    return replace(snap, terms=(TermInput(snap.terms[0].term, (held("x", *x, guard=guard, T=100.0),)),
                                TermInput(snap.terms[1].term, (held("z", *z, guard=guard, T=300.0),))))


# 1 ---------------------------------------------------------------------------------------------------------
def test_a_lost_branch_pays_the_entry_price_it_would_have_cost_to_appear():
    prep = spectrum(ONE, seed=9)
    snap = t_pair(prep, ONE, ["b1"], BOTH, ["b1", "b2"])
    spec = R._Spec(snap)
    L = S.birth_edge + spec.entry.B(15.0)
    assert L == S.birth_edge + S.birth_mid                                  # mid-window: the full price
    assert spec.loss_terms(("b1",)) == (("b2", 15.0, L),)
    assert spec.loss_terms(("b1", "b2")) == () and spec.loss_terms(("b1", None)) == spec.loss_terms(("b1",))
    lost, free = R.price(snap, _job(("b1",))), R.price(replace(snap, settings=ZERO), _job(("b1",)))
    assert lost.loss == (("b2", 15.0, L),) and free.loss == (("b2", 15.0, 0.0),)
    assert lost.ln_z - free.ln_z == pytest.approx(-L / 2, abs=1e-6)         # the price, exactly once
    named = R.price(snap, _job(("b1", "b2"))), R.price(replace(snap, settings=ZERO), _job(("b1", "b2")))
    assert named[0].loss == () and named[0].ln_z == pytest.approx(named[1].ln_z, abs=1e-9)
    # every candidate carries its record, and the decision's agrees with the price's
    a = R.assign_decide(snap, [lost] + [R.price(snap, _job(s)) for s in (("b1", "b2"),)])
    c = next(c for c in a.candidates if tuple(c["slots"]) == ("b1",))
    assert c["loss"] == [["b2", 15.0, L]] and c["loss_nats"] == pytest.approx(-L / 2)
    unpriced = [c for c in a.candidates if c["status"] == "unpriced"]
    assert unpriced and all(c["loss"] == [list(t) for t in spec.loss_terms(c["slots"])] for c in unpriced)


def test_births_and_deaths_mirror_at_the_window_edges():
    nb = [(8.0, 0.4, 40.0)]
    for prep, physical in ((spectrum(nb, seed=8), False), (_short(18.0), True)):
        spec = R._Spec(t_pair(prep, nb, ["b1"], nb, ["b1"]))
        assert spec.entry.physical_hi is physical
        near_hi, near_lo = spec.hi - 0.3 * spec.band, spec.lo + 0.3 * spec.band
        for e in (near_hi, near_lo, 12.0):
            snap = t_pair(prep, nb, ["b1"], [(8.0, 0.4, 40.0), (e, 0.4, 40.0)], ["b1", "b2"])
            (name, e_exp, L), = R._Spec(snap).loss_terms(("b1",))
            assert (name, e_exp) == ("b2", pytest.approx(e)) and L == pytest.approx(spec.cost(e, None))
        edge = R._Spec(t_pair(prep, nb, ["b1"], [(8.0, 0.4, 40.0), (near_lo, 0.4, 40.0)], ["b1", "b2"]))
        assert 0 < edge.loss_terms(("b1",))[0][2] - S.birth_edge < S.birth_mid          # cheap near a physical edge
        cap = R._Spec(t_pair(prep, nb, ["b1"], [(8.0, 0.4, 40.0), (near_hi, 0.4, 40.0)], ["b1", "b2"]))
        full = cap.loss_terms(("b1",))[0][2] == S.birth_edge + S.birth_mid
        assert full is (not physical)                    # an artificial cap is not an edge, for births or deaths


# 2 ---------------------------------------------------------------------------------------------------------
def test_which_branches_are_eligible_and_what_they_are_expected_to_cost():
    prep = spectrum(ONE, seed=9)
    base = t_pair(prep, ONE, ["b1"], ONE, ["b1"])
    spec0 = R._Spec(base)
    two = lambda e: ([(8.0, 0.4, 40.0), (e, 0.4, 40.0)], ["b1", "b2"])
    # (a) predicted only outside the window: nothing, above or below
    assert R._Spec(_with_neighbours(base, two(spec0.hi + 1.0), two(spec0.hi + 1.0))).loss_terms(("b1",)) == ()
    assert R._Spec(_with_neighbours(base, two(spec0.lo - 1.0), two(spec0.lo - 1.0))).loss_terms(("b1",)) == ()
    # (b) a branch a neighbour only remembers
    x = held("x", ONE, ["b1"], T=100.0)
    dormant = replace(x, dormant=(Dormant("b2", "f1", "x"),))
    snap = replace(base, terms=(TermInput(base.terms[0].term, (dormant,)), base.terms[1]))
    assert R._Spec(snap).loss_terms(("b1",)) == ()
    # (c) carried by one of two neighbours only (one coupling term, none with z): eligible at that one's energy
    one_side = _with_neighbours(base, two(14.0), (ONE, ["b1"]))
    spec = R._Spec(one_side)
    assert len(spec.factors["b2"]) == 1 and spec.loss_terms(("b1",)) == (("b2", 14.0, S.birth_edge + S.birth_mid),)
    # (d) two neighbours naming it at 14.8 and 15.2: one entry at the mean, charged once
    both = R._Spec(_with_neighbours(base, two(14.8), two(15.2)))
    (name, e, L), = both.loss_terms(("b1",))
    assert (name, e) == ("b2", pytest.approx(15.0)) and L == S.birth_edge + S.birth_mid
    # (e) two raw ids merged by the batch are one branch: one entry, and naming either raw id pays nothing
    aliased = replace(_with_neighbours(base, two(14.0), ([(8.0, 0.4, 40.0), (14.0, 0.4, 40.0)], ["b1", "b2x"])),
                      aliases={"b2x": "b2"})
    spec = R._Spec(aliased)
    assert [t[0] for t in spec.loss_terms(("b1",))] == ["b2"]
    assert spec.loss_terms(("b1", "b2x")) == spec.loss_terms(("b1", "b2")) == ()
    # (f) predictions split inside and outside the window: the mean is over those inside
    split = R._Spec(_with_neighbours(base, two(15.0), two(spec0.hi + 2.0)))
    assert split.loss_terms(("b1",))[0][1] == pytest.approx(15.0)


def test_each_neighbour_votes_once_however_many_terms_it_sits_in():
    prep = spectrum(ONE, seed=9)
    base = t_pair(prep, ONE, ["b1"], ONE, ["b1"])
    x = held("x", [(8.0, 0.4, 40.0), (14.0, 0.4, 40.0)], ["b1", "b2"], T=100.0)
    z = held("z", [(8.0, 0.4, 40.0), (15.0, 0.4, 40.0)], ["b1", "b2"], T=300.0)
    # x sits in two terms with y (a T pair and a q pair), z in one: x's 14.0 counts once, not twice
    q = Term("q-change", ("x", "y"), (0.1,), None, None, "hkl")
    snap = replace(base, terms=(TermInput(base.terms[0].term, (x,)), TermInput(q, (x,)),
                                TermInput(base.terms[1].term, (z,))))
    spec = R._Spec(snap)
    assert len(spec.factors["b2"]) == 3
    assert spec.loss_terms(("b1",))[0][1] == pytest.approx(14.5, abs=1e-9)     # a per-factor mean gives 14.333
    # a curvature triple: the extrapolated centre is each neighbour's prediction
    tri = Term("curvature", ("x", "y", "z"), (0.1, 0.1), 0.5, "row 1")
    spec = R._Spec(replace(base, terms=(TermInput(tri, (x, z)),), batch_kind="simultaneous"))
    assert spec.loss_terms(("b1",))[0][1] == pytest.approx(14.5, abs=1e-9)


# 3 ---------------------------------------------------------------------------------------------------------
# The hidden mode (geometry of the twin screen's premise): y holds a strong mode at 8.0 and a weak one (height
# 8) at 8.5, 0.33 FWHM away, where both T-neighbours (recorded guard exactly 0.0, which keeps the T-factor
# width at its base whatever the guard widening does) carry b1 and b2. Poisson, seed 1, scale 1. Measured at
# zero birth settings, ln Z(b1,b2) - ln Z(b1,) = g = -1.95 nats (heights 4 and 16 give -2.31 and -1.43): the
# data alone prefer dropping it by less than L/2 = 7.
HIDDEN_W, HIDDEN_E2 = 8.0, 8.5


def _three(own_modes, own_true_modes, e2, w_nb, *, scale=1.0, settings=S, seed=1):
    prep = spectrum(own_true_modes, scale=scale, seed=seed, ceiling=2)
    nb = [(8.0, 0.4, 40.0), (e2, 0.4, w_nb)]
    return t_pair(prep, own_modes, ["b1"], nb, ["b1", "b2"], guard=0.0, settings=settings)


def _hidden(settings=S):
    true = [(8.0, 0.4, 40.0), (HIDDEN_E2, 0.4, HIDDEN_W)]
    return _three([(8.0, 0.4, 40.0 + HIDDEN_W)], true, HIDDEN_E2, HIDDEN_W, settings=settings)


def _masses(snap, sets):
    """The candidate masses the decision weighs (pooled over slot orders) of just the listed compositions."""
    jobs = [j for j in R._Spec(snap).jobs()[0] if tuple(j.slots) in sets]
    z = {tuple(c["slots"]): c["ln_z"] for c in R.assign_decide(snap, [R.price(snap, j) for j in jobs]).candidates}
    return {k: v for k, v in z.items() if k in sets}


PAIR_SETS = {("b1",), ("b1", "b2"), ("b2", "b1")}


def test_a_hidden_mode_survives_the_merge_and_is_recovered_by_the_loss_price():
    snap, free = _hidden(), _hidden(ZERO)
    spec = R._Spec(snap)
    assert not [n for n in spec.notes if n["code"] == "neighbour_precision_unavailable"]
    L = sum(L for _b, _e, L in spec.loss_terms(("b1",)))
    assert L == S.birth_edge + S.birth_mid
    a, priced = R.assign(snap)
    pair = next(p for p in priced if p.slots == ("b1", "b2"))
    assert pair.status in ("ok", "ok_boundary") and math.isfinite(pair.ln_z) and pair.basins   # not merged away
    z = {tuple(c["slots"]): c["ln_z"] for c in a.candidates}
    assert z[("b1", "b2")] is not None and math.isfinite(z[("b1", "b2")])
    zf = _masses(free, PAIR_SETS)
    g = zf[("b1", "b2")] - zf[("b1",)]
    assert -L / 2 < g < 0                         # at zero prices the data alone drop it: (b1,) wins
    margin = z[("b1", "b2")] - z[("b1",)]
    assert margin == pytest.approx(g + L / 2, abs=1e-6)                       # the loss is the only difference
    assert (a.count, tuple(a.slots), a.changed) == (2, ("b1", "b2"), True)    # recovery by the loss price
    rivals = sorted(v for k, v in z.items() if v is not None and k != ("b1", "b2"))
    print(f"\nHIDDEN: g at zero prices {g:.3f} nats; L/2 {L / 2:.1f}; margin at default prices of (b1,b2) over "
          f"(b1,) {margin:+.3f} nats, over the best other candidate {z[('b1', 'b2')] - rivals[-1]:+.3f}")


# 4 ---------------------------------------------------------------------------------------------------------
# The counterpart: y's data are the strong mode alone and the neighbours predict b2 at 14.0 (mid-window), a
# branch whose intensity genuinely vanishes. Measured at zero birth settings (Poisson, seed 1) g0 =
# ln Z(b1,b2) - ln Z(b1,) is -3.96 at scale 1, -7.45 at 16 and -18.9 at 256. Put under the strong mode instead
# (b2 at 8.5) g0 stops at -4.2 (scale 256) and -5.4 (scale 4096): a phantom hidden there costs only a few nats at
# any statistics, so the loss price keeps it; that geometry cannot show the rejection, this one can.
def _vanished(scale, settings=S):
    return _three([(8.0, 0.4, 40.0)], [(8.0, 0.4, 40.0)], 14.0, 8.0, scale=scale, settings=settings)


def test_a_vanishing_branch_is_rejected_on_strong_statistics_and_kept_as_a_phantom_on_weak():
    snap = _vanished(256.0)
    L = sum(L for _b, _e, L in R._Spec(snap).loss_terms(("b1",)))
    zs = _masses(_vanished(256.0, ZERO), PAIR_SETS)
    g0 = zs[("b1", "b2")] - zs[("b1",)]
    assert g0 < -(L / 2 + 2)
    a, _ = R.assign(snap)
    z = {tuple(c["slots"]): c["ln_z"] for c in a.candidates}
    assert (a.count, tuple(a.slots)) == (1, ("b1",))                          # no phantom
    margin = z[("b1",)] - z[("b1", "b2")]
    assert margin == pytest.approx(-g0 - L / 2, abs=1e-6) and margin > -g0 - L        # L/2 less than at zero
    print(f"\nVANISHING strong statistics (scale 256): g0 {g0:.3f}; margin of (b1,) over (b1,b2) {margin:+.3f} "
          f"nats at default prices ({-g0:+.3f} at zero prices)")
    weak = _vanished(1.0)
    zw0 = _masses(_vanished(1.0, ZERO), PAIR_SETS)
    g1 = zw0[("b1", "b2")] - zw0[("b1",)]
    assert g1 > -L / 2
    a, _ = R.assign(weak)
    zw = {tuple(c["slots"]): c["ln_z"] for c in a.candidates}
    assert (a.count, tuple(a.slots)) == (2, ("b1", "b2"))                    # the accepted consequence
    print(f"VANISHING weak statistics (scale 1): g0 {g1:.3f}; the phantom (b1,b2) leads (b1,) by "
          f"{zw[('b1', 'b2')] - zw[('b1',)]:+.3f} nats")


# 5 ---------------------------------------------------------------------------------------------------------
def test_slot_orders_of_one_composition_carry_the_loss_once():
    true = [(8.0, 0.3, 30.0), (9.0, 0.3, 30.0)]
    nb = [(8.45, 0.3, 30.0), (8.55, 0.3, 30.0), (15.0, 0.3, 30.0)]

    def rows(settings):
        snap = t_pair(spectrum(true, scale=0.3, seed=3), true, ["fast:y#0", "fast:y#1"], nb, ["b1", "b2", "b3"],
                      kind="simultaneous", guard=0.5, settings=settings)
        spec = R._Spec(snap)
        jobs = [j for j in spec.jobs()[0] if j.count == 2 and set(j.slots) == {"b1", "b2"}]
        assert len(jobs) == 2
        by_key = {j.key: R.price(snap, j) for j in jobs}
        out, _modes = R._basin_rows(spec, jobs, by_key)
        return {(c, tuple(s)): z for c, s, z in out}, spec.loss_terms(("b1", "b2"))
    with_loss, loss = rows(S)
    without, none = rows(ZERO)
    (_b, _e, L), = loss
    assert L == S.birth_edge + S.birth_mid and none[0][2] == 0.0
    assert set(with_loss) == set(without) and with_loss
    for key, z in with_loss.items():                  # the pooled basin of either order, one shift each
        assert z - without[key] == pytest.approx(-L / 2, abs=1e-6), key


# 6 ---------------------------------------------------------------------------------------------------------
def test_a_simultaneous_batch_charges_a_shared_missing_branch_once_and_refines_one_objective():
    prep = spectrum(ONE, seed=9)
    snap = t_pair(prep, ONE, ["b1"], BOTH, ["b1", "b2"], kind="simultaneous")
    spec = R._Spec(snap)
    (name, e, L), = spec.loss_terms(("b1",))                  # both neighbours predict b2: one entry
    assert (name, e) == ("b2", pytest.approx(15.0))
    free = replace(snap, settings=ZERO)
    assert R.price(snap, _job(("b1",))).ln_z - R.price(free, _job(("b1",))).ln_z == pytest.approx(-L / 2, abs=1e-6)
    # the composition is fixed and the loss depends on it alone: the shared objective has no gradient from it
    a = spec.skipped()
    assert a.slots == ("b1",)
    r_on, r_off = (R.refit(s, stage="continuous", assignment=a) for s in (snap, free))
    assert r_on.J == pytest.approx(r_off.J, abs=1e-9) and r_on.D == pytest.approx(r_off.D, abs=1e-9)
    assert r_on.unary == pytest.approx(r_off.unary, abs=1e-9) and r_on.P == pytest.approx(r_off.P, abs=1e-9)
    # frozen: the sweep snapshots of the round price it the same
    sweep = replace(snap, snapshot_id="b1:sweep1")
    assert R._Spec(sweep).loss_terms(("b1",)) == spec.loss_terms(("b1",))


# 7 ---------------------------------------------------------------------------------------------------------
def test_the_cache_key_hashes_every_term_field_and_the_aliases():
    prep = spectrum(ONE, seed=9)
    snap = t_pair(prep, ONE, ["b1"], BOTH, ["b1", "b2"])
    key = R._Spec(snap).key(1, ("b1",))
    assert R._Spec(replace(snap)).key(1, ("b1",)) == key
    assert R._Spec(replace(snap, aliases={"b2x": "b2"})).key(1, ("b1",)) != key
    t0 = snap.terms[0]
    moved = replace(snap, terms=(replace(t0, term=replace(t0.term, line="row 2")), snap.terms[1]))
    assert R._Spec(moved).key(1, ("b1",)) != key                # a field the old payload did not read
