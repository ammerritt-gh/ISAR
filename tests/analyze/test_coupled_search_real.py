"""The adaptive search and the count-first decision on the real engine (plan S3U2, tests 13-18 and 20).

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
from _search_harness import job_for, snapshot_for
from test_coupled_refit import STRUCT, S, _label, held, record, spectrum, t_pair

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
    snap = Snapshot("s", "s", "y", "f1", spectrum(own, seed=21), held("y", own, ["fast:y#0", "fast:y#1"]),
                    record(own), (term,), S, "anchored")
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


def _seven(own=None, branches=None):
    own = SEVEN if own is None else own
    p = prep(SEVEN, 8, seed=7)
    return t_pair(p, own, branches or [f"fast:y#{i}" for i in range(len(own))], SEVEN,
                  [f"b{i + 1}" for i in range(7)], kind="simultaneous")


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
    # the search asks for count 7 in its seed round (the neighbour carries seven); the whole walk on to 8 and 9
    # from six modes takes more than ten minutes and is not repeated here
    snap6 = _seven(SEVEN[:6])
    first = R.open_search(snap6).next_jobs()
    j7 = next(j for j in first if j.count == 7)
    p7 = R.price(snap6, j7)
    assert p7.status in ("ok", "ok_boundary") and math.isfinite(p7.ln_z), (p7.status, p7.reason)
    print(f"\n14 count 7: direct price {priced_s:.1f} s ({p.status}); seed round from six modes asks "
          f"{sorted(j.count for j in first)}, its count-7 seed priced ({p7.status}); "
          f"{time.perf_counter() - t0:.1f} s total")


# 15 --------------------------------------------------------------------------------------------------------
FOUR = [(3.0, 0.3, 40.0), (6.0, 0.3, 40.0), (10.0, 0.3, 40.0), (15.0, 0.3, 40.0)]
EIGHT = [(2.5 + 2.2 * i, 0.3, 40.0) for i in range(8)]


EIGHT_RUN = pytest.mark.skipif(not os.environ.get("ISAR_SLOW_EIGHT"),
                               reason="far over ten minutes: set ISAR_SLOW_EIGHT=1 to run the eight-mode case")


@pytest.mark.parametrize("truth", [FOUR, pytest.param(EIGHT, marks=EIGHT_RUN)], ids=["four", "eight"])
def test_several_modes_added_at_once_are_reached_through_the_neighbour_seed(truth):
    t0 = time.perf_counter()
    n = len(truth)
    snap = t_pair(prep(truth, n + 1, seed=15), truth[:1], ["fast:y#0"], truth, [f"b{i + 1}" for i in range(n)],
                  kind="simultaneous")
    a, priced = R.assign(snap)
    names = tuple(f"b{i + 1}" for i in range(n))
    assert (a.count, a.slots) == (n, names) and a.changed, (a.count, a.slots, decision(a)["count_p"])
    seeds = {(sd["count"], tuple(sd["slots"])) if isinstance(sd, dict) and "count" in sd else None
             for sd in a.search["seeds"]}
    seed_jobs = [c for c in a.candidates if c["source"].startswith("seed:neighbour")]
    assert any(c["count"] == n and c["ln_z"] is not None for c in seed_jobs), (seeds, a.search["seeds"])
    assert all(c["ln_z"] is not None for c in a.candidates if c["source"].startswith("seed:"))      # every seed priced
    first = next(c for c in a.candidates if (c["count"], tuple(c["slots"])) == (n, names))
    assert first["source"].startswith("seed:")                    # not reached by stepping 1 -> 2 -> ... -> n
    print(f"\n15 {n} modes at once: {len(priced)} jobs, count_p {decision(a)['count_p'].get(str(n))}, "
          f"stopped {a.coverage['stopped']}; {time.perf_counter() - t0:.1f} s")


# 16 --------------------------------------------------------------------------------------------------------
def test_the_adaptive_search_agrees_with_a_full_enumeration_and_prices_fewer_jobs():
    t0 = time.perf_counter()
    true = [(8.0, 0.4, 40.0), (12.0, 0.4, 40.0)]
    snap = t_pair(prep(true, 3, seed=16), true[:1], ["fast:y#0"], true, ["b1", "b2"], kind="simultaneous")
    ta = time.perf_counter()
    adaptive, p_adaptive = R.assign(snap)
    t_adaptive = time.perf_counter() - ta
    full_snap = replace(snap, settings=replace(S, full_search=True, full_search_max_count=3))
    tf = time.perf_counter()
    full, p_full = R.assign(full_snap)
    t_full = time.perf_counter() - tf
    assert full.search["domain"] == "full" and not full.coverage["enumeration_truncated"]
    assert (adaptive.count, adaptive.slots) == (full.count, full.slots) == (2, ("b1", "b2"))
    pa, pf = decision(adaptive)["count_p"], decision(full)["count_p"]
    for c in ("1", "2", "3"):
        assert pa.get(c, 0.0) == pytest.approx(pf.get(c, 0.0), abs=0.05), (c, pa, pf)
    assert len(p_adaptive) < len(p_full), (len(p_adaptive), len(p_full))
    print(f"\n16 adaptive {len(p_adaptive)} jobs {t_adaptive:.1f} s vs full {len(p_full)} jobs {t_full:.1f} s; "
          f"count_p adaptive {pa} full {pf}; {time.perf_counter() - t0:.1f} s")


# 17 --------------------------------------------------------------------------------------------------------
# S2U2's fixtures, copied: y holds a strong mode at 8.0 and the two T-neighbours (guard exactly 0.0) carry b1 and
# b2. Hidden: the weak mode (height 8) sits at 8.5 in the data and is folded into the incoming mode. Vanishing: the
# data are the strong mode alone and b2 sits at 14.0.
def _three(own_modes, own_true_modes, e2, w_nb, *, scale=1.0, settings=S, seed=1):
    p = spectrum(own_true_modes, scale=scale, seed=seed, ceiling=2)
    nb = [(8.0, 0.4, 40.0), (e2, 0.4, w_nb)]
    return t_pair(p, own_modes, ["b1"], nb, ["b1", "b2"], guard=0.0, settings=settings)


def _hidden():
    true = [(8.0, 0.4, 40.0), (8.5, 0.4, 8.0)]
    return _three([(8.0, 0.4, 48.0)], true, 8.5, 8.0)


def _vanished(scale):
    return _three([(8.0, 0.4, 40.0)], [(8.0, 0.4, 40.0)], 14.0, 8.0, scale=scale)


def test_a_hidden_mode_is_kept_and_a_vanishing_branch_is_dropped_under_the_count_first_decision():
    t0 = time.perf_counter()
    r = R.refit(_hidden(), stage="both")
    a = r.assignment
    z = {tuple(c["slots"]): c["ln_z"] for c in a.candidates if c["ln_z"] is not None}
    assert (a.count, a.slots, a.changed) == (2, ("b1", "b2"), True), (a.count, a.slots, a.changed, decision(a))
    assert r.status == "accepted", (r.status, r.reason)
    print(f"\n17 hidden: count_p {decision(a)['count_p']}; (b1,b2) over (b1,) "
          f"{z[('b1', 'b2')] - z[('b1',)]:+.2f} nats; {time.perf_counter() - t0:.1f} s")
    t1 = time.perf_counter()
    strong, _ = R.assign(_vanished(256.0))
    assert (strong.count, strong.slots) == (1, ("b1",)), (strong.count, strong.slots, decision(strong)["count_p"])
    print(f"17 vanishing, strong statistics: count_p {decision(strong)['count_p']}; {time.perf_counter() - t1:.1f} s")
    t2 = time.perf_counter()
    weak, _ = R.assign(_vanished(1.0))
    assert (weak.count, weak.slots) == (2, ("b1", "b2")), (weak.count, weak.slots, decision(weak)["count_p"])
    print(f"17 vanishing, weak statistics: count_p {decision(weak)['count_p']} (the phantom stays, as S2U2 documents); "
          f"{time.perf_counter() - t2:.1f} s")


# 18 --------------------------------------------------------------------------------------------------------
def test_the_extra_unmatched_mode_tail_is_summable_with_prices_off_and_on():
    t0 = time.perf_counter()
    true = [(8.0, 0.4, 40.0), (13.0, 0.4, 40.0)]
    base = t_pair(prep(true, 5, seed=18), true, ["fast:y#0", "fast:y#1"], true, ["b1", "b2"], kind="simultaneous")
    out = {}
    for name, settings in (("zero", ZERO), ("default", S)):
        snap = replace(base, settings=settings)
        out[name] = {k: R.price(snap, job_for(snap, k, (None,) * k)) for k in range(2, 6)}
        assert all(p.ln_z is not None and math.isfinite(p.ln_z) for p in out[name].values()), \
            {k: (p.status, p.reason) for k, p in out[name].items()}
    lz = {n: {k: p.ln_z for k, p in d.items()} for n, d in out.items()}
    step = {n: {k: lz[n][k + 1] - lz[n][k] for k in range(2, 5)} for n in lz}
    tail = sum(math.exp(lz["zero"][k] - lz["zero"][2]) for k in range(3, 6))
    print(f"\n18 steps at zero prices {step['zero']}; at defaults {step['default']}; tail beyond the answer {tail:.4f}; "
          f"{time.perf_counter() - t0:.1f} s")
    assert all(d <= -1.0 for d in step["zero"].values()), step["zero"]
    assert all(d <= -5.0 for d in step["default"].values()), step["default"]
    assert tail < 0.1


# 20 --------------------------------------------------------------------------------------------------------
@pytest.mark.xfail(strict=True, reason="W8's fixture is not count-stable: its 1.6x wide elastic line is fitted by extra "
                   "modes (count 4 for seed 1, 3 for seed 2, 30-60 nats ahead of count 2), so the variant cannot be "
                   "decided at unchanged count; reported to the coordinator, data not tuned")
@pytest.mark.parametrize("seed", [1, 2])
def test_an_evidence_selected_qe_free_variant_replaces_the_incoming_qe_fit_at_unchanged_labels(seed):
    # W8's fixture with the incoming modes already matched to b1 and b2, so only the model variant can change
    import qe_rail_recipe as recipe
    from test_coupled_width_floor import Prepared
    t0 = time.perf_counter()
    lo, _ = recipe.qe_floor()
    truth = [(5.0, 0.4, 60.0), (11.0, 0.8, 40.0)]
    ctx = recipe.widened(seed, gamma1=0.4, qe_h=0.0, qe_width=lo)
    snap = t_pair(Prepared(ctx, 2), truth, ["b1", "b2"], truth, ["b1", "b2"], rec={"count_probabilities": {"2": 0.9}})
    spec = R._Spec(snap)
    eng = R._Labelled(spec.ctx, spec.fs, spec.slot_objects(("b1", "b2")), structures=spec.structures)
    model = eng.model(eng.variant(2, True, True, True), ())
    values = {"phonon.omega0": 5.0, "phonon.Gamma": 0.4, "phonon.h": 60.0, "phonon2.omega0": 11.0,
              "phonon2.Gamma": 0.8, "phonon2.h": 40.0, "elastic.h": 200.0, "qe.h": 0.0, "qe.width": lo, "bg.BG": 2.0}
    rec = record(truth)
    rec["models"][0].update(qe=True, map_params={n: values[n] for n in model.names})
    r = R.refit(replace(snap, own_record=rec), stage="both")
    a = r.assignment
    print(f"\n20 seed {seed}: status {r.status}/{r.reason}; changed {a.changed} mask_only {a.mask_only} "
          f"variant {decision(a)['variant']}; why: {r.why_changed}; {time.perf_counter() - t0:.1f} s")
    assert (a.count, a.slots) == (2, ("b1", "b2")) and a.changed and a.mask_only
    assert (r.status, r.reason) == ("accepted", "evidence")
    assert not R._selected_model(r.fit)["qe"]
    assert "QE" in r.why_changed or "model" in r.why_changed.lower()
