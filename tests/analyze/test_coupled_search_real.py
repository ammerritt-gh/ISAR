"""The listed-candidate search and the count-first decision on the real engine (plan S3U2 tests 13-17 and 20,
S4U2 test (i)).

Every spectrum is Poisson counts from DHO modes convolved with the made-up Gaussian resolution of
``test_coupled_refit`` (hard rule 10: nothing measured, nothing read from ``real_data``). Each case prices tens of
jobs or a count above 6, so the module is ``slow``. Run with ``-s`` to see the margins and wall times the slice
report quotes.
"""
import math
import os
import time
from dataclasses import replace

import pytest
from _search_harness import add, job_for, links_for, tie
from test_coupled_refit import S, _label, held, record, spectrum, t_pair

from isar.analyze.coupled import identity
from isar.analyze.coupled import refit as R
from isar.analyze.coupled.types import Snapshot, TermInput
from isar.session.inter_spectra.graph import Term

pytestmark = pytest.mark.slow       # reason: each case prices tens of jobs or a count above 6

ZERO = replace(S, birth_edge=0.0, birth_mid=0.0)      # entry and loss prices off


def structs(n):
    """The card's structure whitelist up to ``n`` modes (the shared ``STRUCT`` stops at 3)."""
    return tuple(_label(k) for k in range(n + 1))


def prep(modes, n, **kw):
    p = spectrum(modes, ceiling=2, **kw)
    p.structures = structs(n)
    return p


def decision(a):
    return a.coverage["decision"]


def test_runs_from_the_worktree():
    import isar
    here = __file__.replace("\\", "/").rsplit("/tests/", 1)[0]
    print("import root:", isar.__file__)
    assert isar.__file__.replace("\\", "/").startswith(here)


# 13 --------------------------------------------------------------------------------------------------------
def test_a_label_swap_of_two_close_modes_loses_through_the_whole_refit():
    t0 = time.perf_counter()
    # the a35 geometry with made-up numbers: the neighbour carries b1 and b2 2.4 meV apart low in the window, the
    # spectrum's modes sit 0.1 meV above them; a Welsch tolerance of 2 meV at this spacing
    nb = [(2.4, 0.3, 40.0), (4.8, 0.3, 40.0)]
    own = [(2.5, 0.3, 40.0), (4.9, 0.3, 40.0)]
    spacing = 2.0 / S.q_slope_meV_per_rlu
    term = TermInput(Term("q-change", ("x", "y"), (spacing,), None, None, "hkl"), (held("x", nb, ["b1", "b2"]),))
    ties = (tie("t0", "swap", ("y", "w"), ("b2", "b1")),)       # the link reports the crossing: both labellings priced
    snap = Snapshot("s", "s", "y", "f1", spectrum(own, seed=21), held("y", own, ["b1", "b2"]),
                    record(own), (term,), S, "anchored", links=links_for(2, ties=ties, labels=("b1", "b2")))
    r = R.refit(snap, stage="both")
    a = r.assignment
    z = {tuple(c["slots"]): c["ln_z"] for c in a.candidates if c["count"] == 2 and c["ln_z"] is not None}
    assert (a.count, a.slots) == (2, ("b1", "b2")) and r.status in ("accepted", "unchanged")
    assert ("b2", "b1") in z, sorted(z)                       # the swapped labelling was scored, not skipped
    behind = z[("b1", "b2")] - z[("b2", "b1")]
    assert behind >= 3.0
    d = decision(a)
    assert d["label_confident"] and d["count_confident"] and not d["ambiguous"]
    assert [identity._slot_support(a, m, S)[0] for m in range(2)] == [True, True]
    print(f"\n13 swap: straight leads swapped by {behind:.2f} nats; label_p {d['label_p']:.4f}; "
          f"{a.search['tried']['jobs']} jobs; {time.perf_counter() - t0:.1f} s")


# 14 --------------------------------------------------------------------------------------------------------
SEVEN = [(3.0, 0.3, 40.0), (5.5, 0.3, 40.0), (8.0, 0.3, 40.0), (10.5, 0.3, 40.0), (13.0, 0.3, 40.0),
         (15.5, 0.3, 40.0), (18.0, 0.3, 40.0)]


def _seven(own=None, links=None):
    own = SEVEN if own is None else own
    p = prep(SEVEN, 8, seed=7)
    snap = t_pair(p, own, [f"b{i + 1}" for i in range(len(own))], SEVEN, [f"b{i + 1}" for i in range(7)],
                  kind="simultaneous")
    return replace(snap, links=links)


def test_a_count_above_six_is_priced_refined_released_and_stored():
    t0 = time.perf_counter()
    snap = _seven()
    names = tuple(f"b{i + 1}" for i in range(7))
    p = R.price(snap, job_for(snap, 7, names))                  # the old engine ceiling: no_finite_candidate
    assert p.status in ("ok", "ok_boundary") and p.ln_z is not None and math.isfinite(p.ln_z), (p.status, p.reason)
    priced_s = time.perf_counter() - t0
    # the search reaches count 7 and the continuous stage keeps it, with the release fit and the stored record
    a = R._Spec(snap).skipped()
    assert a.count == 7
    r = R.refit(snap, stage="continuous", assignment=a)
    assert r.status in ("accepted", "unchanged"), (r.status, r.reason)
    assert r.R is not None                                       # the release fit exists
    sel = R._selected_model(r.fit)
    assert sel["n_peaks"] == 7 and len(r.fit["peaks"]) == 7
    assert len(r.modes) == 7 and all(m.branch for m in r.modes)
    back = R.held_from_fit(r.fit, unit="y", fit="f1", inputs=None, context=snap.context, T=200.0)
    assert len(back.modes) == 7 and all(m.branch for m in back.modes)
    # from six modes the link lists the seventh branch: the search asks for count 7 in its one pass
    snap6 = _seven(SEVEN[:6], links_for(6, changes=[add("b7", 18.0, w=1.7)], labels=tuple(f"b{i + 1}" for i in range(6))))
    first = R.open_search(snap6).next_jobs()
    j7 = next(j for j in first if j.count == 7)
    p7 = R.price(snap6, j7)
    assert p7.status in ("ok", "ok_boundary") and math.isfinite(p7.ln_z), (p7.status, p7.reason)
    print(f"\n14 count 7: direct price {priced_s:.1f} s ({p.status}); seed round from six modes asks "
          f"{sorted(j.count for j in first)}, its count-7 candidate priced ({p7.status}); "
          f"{time.perf_counter() - t0:.1f} s total")


# 15 --------------------------------------------------------------------------------------------------------
FOUR = [(3.0, 0.3, 40.0), (6.0, 0.3, 40.0), (10.0, 0.3, 40.0), (15.0, 0.3, 40.0)]
EIGHT = [(2.5 + 2.2 * i, 0.3, 40.0) for i in range(8)]


EIGHT_RUN = pytest.mark.skipif(not os.environ.get("ISAR_SLOW_EIGHT"),
                               reason="far over ten minutes: set ISAR_SLOW_EIGHT=1 to run the eight-mode case")


@pytest.mark.parametrize("truth", [FOUR, pytest.param(EIGHT, marks=EIGHT_RUN)], ids=["four", "eight"])
def test_several_modes_added_at_once_are_reached_through_the_listed_adds(truth):
    t0 = time.perf_counter()
    n = len(truth)
    L = links_for(1, changes=[add(f"b{i + 1}", truth[i][0], w=1.7) for i in range(1, n)], labels=("b1",))
    snap = replace(t_pair(prep(truth, n + 1, seed=15), truth[:1], ["b1"], truth, [f"b{i + 1}" for i in range(n)],
                          kind="simultaneous"), links=L)
    a, priced = R.assign(snap)
    names = tuple(f"b{i + 1}" for i in range(n))
    assert (a.count, a.slots) == (n, names) and a.changed, (a.count, a.slots, decision(a)["count_p"])
    assert a.search["applied"] == list(range(n - 1))              # every listed add was taken, by its index
    assert all(c["ln_z"] is not None for c in a.candidates if c["count"] == n)
    print(f"\n15 {n} modes at once: {len(priced)} jobs, count_p {decision(a)['count_p'].get(str(n))}, "
          f"chosen {a.search['chosen']}; {time.perf_counter() - t0:.1f} s")


# 17 --------------------------------------------------------------------------------------------------------
# S2U2's fixtures, copied: y holds a strong mode at 8.0 and the two T-neighbours (guard exactly 0.0) carry b1 and
# b2. Hidden: the weak mode (height 8) sits at 8.5 in the data and is folded into the incoming mode. Vanishing: the
# data are the strong mode alone and b2 sits at 14.0.
def _three(own_modes, own_true_modes, e2, w_nb, *, scale=1.0, settings=S, seed=1):
    p = spectrum(own_true_modes, scale=scale, seed=seed, ceiling=2)
    nb = [(8.0, 0.4, 40.0), (e2, 0.4, w_nb)]
    return t_pair(p, own_modes, ["b1"], nb, ["b1", "b2"], guard=0.0, settings=settings)


def _listed(snap, e2):
    """``snap`` with the link listing the add of b2 at the energy its neighbours carry it (width: 0.4 in 1.5 meV)."""
    return replace(snap, links=links_for(1, changes=[add("b2", e2, w=1.7)], labels=("b1",)))


def _hidden():
    true = [(8.0, 0.4, 40.0), (8.5, 0.4, 8.0)]
    return _listed(_three([(8.0, 0.4, 48.0)], true, 8.5, 8.0), 8.5)


def _vanished(scale):
    return _listed(_three([(8.0, 0.4, 40.0)], [(8.0, 0.4, 40.0)], 14.0, 8.0, scale=scale), 14.0)


def test_a_hidden_mode_is_kept_and_a_vanishing_branch_is_dropped_under_the_count_first_decision():
    t0 = time.perf_counter()
    r = R.refit(_hidden(), stage="both")
    a = r.assignment
    z = {tuple(c["slots"]): c["ln_z"] for c in a.candidates if c["ln_z"] is not None}
    assert (a.count, a.slots, a.changed) == (2, ("b1", "b2"), True), (a.count, a.slots, a.changed, decision(a))
    assert r.status == "accepted", (r.status, r.reason)
    assert (a.search["chosen"], a.search["applied"]) == ("add:b2", [0])      # the listed add beat the incoming fit
    print(f"\n17 hidden: count_p {decision(a)['count_p']}; (b1,b2) over (b1,) "
          f"{z[('b1', 'b2')] - z[('b1',)]:+.2f} nats; {time.perf_counter() - t0:.1f} s")
    t1 = time.perf_counter()
    strong, _ = R.assign(_vanished(256.0))
    assert (strong.count, strong.slots) == (1, ("b1",)), (strong.count, strong.slots, decision(strong)["count_p"])
    print(f"17 vanishing, strong statistics: count_p {decision(strong)['count_p']}; {time.perf_counter() - t1:.1f} s")
    t2 = time.perf_counter()
    # S2U2 kept the phantom here: its height prior let b2 sit at zero for a few nats. The link's intensity prior (S4U5)
    # centres b2's intrinsic intensity on the neighbours' (A 10, near their height 8, about 11 at y's 200 K), so a
    # zero-height b2 pays the prior's tail and the add no longer wins; with A unknown (0) the phantom still stays
    # (test_coupled_loss.py 4).
    weak, _ = R.assign(_vanished(1.0))
    assert (weak.count, weak.slots) == (1, ("b1",)), (weak.count, weak.slots, decision(weak)["count_p"])
    print(f"17 vanishing, weak statistics: count_p {decision(weak)['count_p']} (the intensity prior drops the "
          f"phantom); {time.perf_counter() - t2:.1f} s")


# 20 --------------------------------------------------------------------------------------------------------
# The search cannot reach this variant on W8's fixture (its elastic line is fitted by extra modes, so the count moves),
# so the decision is given: the incoming fit carries the QE, the Assignment names the QE-free variant the evidence
# chose, and the continuous stage must accept it at the unchanged labels. One real refit, no search.
@pytest.mark.parametrize("seed", [1, 2])
def test_an_evidence_chosen_qe_free_variant_replaces_the_incoming_qe_fit_at_unchanged_labels(seed):
    # W8's fixture with the incoming modes already matched to b1 and b2, so only the model variant can change
    import qe_rail_recipe as recipe
    from test_coupled_width_floor import Prepared
    t0 = time.perf_counter()
    lo, _ = recipe.qe_floor()
    truth = [(5.0, 0.4, 60.0), (11.0, 0.8, 40.0)]
    ctx = recipe.widened(seed, gamma1=0.4, qe_h=0.0, qe_width=lo)
    values = {"phonon.omega0": 5.0, "phonon.Gamma": 0.4, "phonon.h": 60.0, "phonon2.omega0": 11.0,
              "phonon2.Gamma": 0.8, "phonon2.h": 40.0, "elastic.h": 200.0, "qe.h": 0.0, "qe.width": lo, "bg.BG": 2.0}
    snap = t_pair(Prepared(ctx, 2), truth, ["b1", "b2"], truth, ["b1", "b2"])
    spec = R._Spec(snap)
    eng = R._Labelled(spec.ctx, spec.fs, spec.slot_objects(("b1", "b2")), structures=spec.structures)
    rec = record(truth)                                       # the incoming fit: its selected model carries the QE
    rec["models"][0].update(qe=True, map_params={n: values[n] for n in eng.model(eng.variant(2, True, True, True), ()).names})
    snap = replace(snap, own_record=rec)
    model = eng.model(eng.variant(2, True, True, False), ())
    a = replace(R._Spec(snap).skipped(), changed=True, mask_only=True,
                selected_model=(True, True, False, (), tuple(values[n] for n in model.names)))
    r = R.refit(snap, stage="continuous", assignment=a)
    print(f"\n20 seed {seed}: status {r.status}/{r.reason}; why: {r.why_changed}; {time.perf_counter() - t0:.1f} s")
    assert (r.status, r.reason) == ("accepted", "evidence")
    assert not R._selected_model(r.fit)["qe"]
    assert "QE" in r.why_changed or "model" in r.why_changed.lower()
