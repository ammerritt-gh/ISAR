"""The evidence-priced mode set (S1U1): ``refit.evidence_assignment`` enters a listed add or split only when the spectrum's
bounded-Laplace ln Z beats the incumbent's by more than ``evidence_hysteresis_nats``. Spectra are Poisson counts generated
at test time (``test_coupled_refit.spectrum``) and the links are made up (``_search_harness``); no measured value. The
real engine fits every set, a fraction of a second each.
"""
from dataclasses import replace

import pytest
from _search_harness import add, drop, links_for, snapshot_for, split_of
from test_coupled_refit import FWHM, STRUCT, spectrum

from isar.analyze import fast_pass
from isar.analyze.coupled import engine as cc
from isar.analyze.coupled import refit as R

THREE = [(5.0, 0.5, 30.0), (9.0, 0.5, 30.0), (13.0, 0.5, 30.0)]
TWO = THREE[:2]
B123 = ["b1", "b2", "b3"]
NEIGH = {"x": (THREE, B123), "z": (THREE, B123)}


def snap(true, own, branches, neighbours, changes, *, seed=3, scale=1.0):
    """The spectrum y drawn from ``true``, its incoming fit ``own`` named ``branches`` (the link's labels too), its
    T-neighbours and the link listing ``changes``."""
    s = snapshot_for(own, branches, neighbours, links=links_for(len(own), changes=changes, labels=tuple(branches)))
    return replace(s, context=spectrum(true, seed=seed, scale=scale, ceiling=3))


@pytest.fixture
def no_split_integral(monkeypatch):
    """The split integral must never run on this path: calling it fails the test."""
    def boom(*_a, **_k):
        raise AssertionError("the split integral ran")
    monkeypatch.setattr(cc.CoupledEngine, "split_evidence", boom)


def tried(a):
    return a.search["pricing"]["tried"]


def test_a_supported_add_enters():
    a = R.evidence_assignment(snap(THREE, TWO, ["b1", "b2"], NEIGH, [add("b3", 13.0)]))
    [row] = tried(a)
    assert (row["id"], row["outcome"], row["count"]) == ("add:b3", "entered", 3) and row["delta_ln_z"] > 0.1
    assert a.search["applied"] == [0] and a.search["domain"] == "evidence" and a.search["version"] == 2
    assert (a.count, a.slots, a.changed, a.unlifted, a.discrete) == (3, ("b1", "b2", "b3"), True, ("b3",), "fixed")
    p = a.search["pricing"]
    assert p["final_count"] == 3 and p["final_ln_z"] == row["ln_z"]
    assert row["delta_ln_z"] == pytest.approx(row["ln_z"] - p["incumbent"]["ln_z"])
    assert [round(a.selected_model[4][3 * i]) for i in range(3)] == [5, 9, 13]      # the posterior mode, slot order


def test_an_add_the_counts_do_not_support_stays_out():
    a = R.evidence_assignment(snap(TWO, TWO, ["b1", "b2"], NEIGH, [add("b3", 13.0)]))
    [row] = tried(a)
    assert row["outcome"] == "kept_out" and row["delta_ln_z"] <= 0.1 and row["why"] is None
    assert a.search["applied"] == [] and (a.count, a.changed, a.unlifted) == (2, False, ())
    assert a.search["pricing"]["unresolved_splits"] == []


def test_the_hysteresis_is_strict_and_read_from_the_settings():
    s = snap(THREE, TWO, ["b1", "b2"], NEIGH, [add("b3", 13.0)])
    d = tried(R.evidence_assignment(s))[0]["delta_ln_z"]
    high = replace(s, settings=replace(s.settings, evidence_hysteresis_nats=d + 1.0))
    a = R.evidence_assignment(high)
    assert tried(a)[0]["outcome"] == "kept_out" and a.count == 2
    assert a.search["pricing"]["hysteresis_nats"] == d + 1.0


def _pair(sep, A, *, true=None, scale=1.0):
    """One own mode at 9 meV on b4 and a listed split into b4 + b5 ``sep`` meV apart, predicted intensities A."""
    e1, e2 = 9.0 - sep / 2.0, 9.0 + sep / 2.0
    nb = [(5.0, 0.5, 30.0), (e1, 0.5, 15.0), (e2, 0.5, 15.0)]
    neigh = {"x": (nb, ["b1", "b4", "b5"]), "z": (nb, ["b1", "b4", "b5"])}
    own = [(5.0, 0.5, 30.0), (9.0, 0.5, 30.0)]
    return snap(true or own, own, ["b1", "b4"], neigh, [split_of(1, "b4", e1, "b5", e2, A1=A, A2=A)], scale=scale)


def test_a_split_the_counts_leave_flat_has_no_answer_and_is_listed(no_split_integral):
    """No intensity prior (A unknown) and the pair 0.1 meV apart: the trade between the two heights is flat, the Hessian
    is not positive definite, so the split does not enter and is listed unresolved."""
    a = R.evidence_assignment(_pair(0.1, 0.0))
    [row] = tried(a)
    assert row["outcome"] == "no_answer" and row["ln_z"] is None and row["delta_ln_z"] is None
    assert row["why"].startswith("undetermined")
    [u] = a.search["pricing"]["unresolved_splits"]
    assert (u["id"], u["mode"], u["branches"], u["why"]) == ("split:1:b4+b5", 1, ["b4", "b5"], row["why"])
    assert (a.count, a.slots, a.changed) == (2, ("b1", "b4"), False) and a.search["applied"] == []


def test_a_close_pair_the_counts_resolve_is_priced_not_refused(no_split_integral):
    """Two true peaks 0.6 meV apart (inside the coincident distance) at high counts: Laplace answers."""
    assert 0.6 < fast_pass.FastPassSettings().twin_coincide_fwhm * FWHM
    true = [(5.0, 0.3, 30.0), (8.7, 0.3, 150.0), (9.3, 0.3, 150.0)]
    a = R.evidence_assignment(_pair(0.6, 0.0, true=true, scale=4.0))
    [row] = tried(a)
    assert row["outcome"] in ("entered", "kept_out") and row["ln_z"] is not None
    assert a.search["pricing"]["unresolved_splits"] == []


def test_a_resolution_limited_add_enters_on_the_delta_twin():
    """The true peak at 13 meV has no width: the added mode's free width rails, and the engine's own delta twin of that
    one mode is priced; the incoming modes keep their free widths."""
    true = TWO + [(13.0, 0.0, 60.0)]
    a = R.evidence_assignment(snap(true, TWO, ["b1", "b2"], NEIGH, [add("b3", 13.0, w=0.1)]))
    assert tried(a)[0]["outcome"] == "entered" and a.count == 3
    assert a.selected_model[3] == (2,)
    assert a.selected_model[4][3 * 2 + 1] == pytest.approx(fast_pass.GAMMA_FLOOR)


@pytest.mark.parametrize("changes", [[], [drop(1)]])
def test_with_nothing_to_price_the_set_is_the_incoming_one(changes):
    s = snap(TWO, TWO, ["b1", "b2"], NEIGH, changes)
    a, f = R.evidence_assignment(s), R.fixed_assignment(s, apply_changes=False)
    assert (a.count, a.slots, a.selected_model, a.changed) == (f.count, f.slots, f.selected_model, False)
    p = a.search["pricing"]
    assert p["incumbent"] is None and p["tried"] == [] and a.search["applied"] == []


def test_an_incumbent_without_an_answer_keeps_the_incoming_set():
    """One narrow own mode whose free width rails to the floor: the incumbent's own evidence has no answer."""
    s = snap(THREE, THREE[:1], ["b1"], NEIGH, [add("b2", 9.0), add("b3", 13.0)])
    a, f = R.evidence_assignment(s), R.fixed_assignment(s, apply_changes=False)
    inc = a.search["pricing"]["incumbent"]
    assert inc["ln_z"] is None and inc["why"].startswith("degenerate_twin")
    assert tried(a) == [] and {x["why"] for x in a.search["skipped"]} == {"incumbent has no answer"}
    assert (a.count, a.slots, a.selected_model) == (f.count, f.slots, f.selected_model)


def test_changes_are_tried_strongest_predicted_intensity_first():
    wide = [(e, 1.0, h) for e, _g, h in THREE]
    s = snap(wide, wide[:1], ["b1"], {"x": (wide, B123), "z": (wide, B123)},
             [add("b2", 9.0, A=3.0), add("b3", 13.0, A=9.0)])
    a = R.evidence_assignment(s)
    assert [(r["id"], r["intensity"]) for r in tried(a)] == [("add:b3", 9.0), ("add:b2", 3.0)]
    assert [r["outcome"] for r in tried(a)] == ["entered", "entered"] and a.count == 3
    assert a.search["applied"] == [0, 1] and a.unlifted == ("b2", "b3")
    assert tried(a)[1]["delta_ln_z"] == pytest.approx(tried(a)[1]["ln_z"] - tried(a)[0]["ln_z"])


B1234 = ["b1", "b2", "b3", "b4"]
FOUR = THREE + [(17.0, 0.5, 30.0)]


def test_an_entered_delta_mode_stays_delta_in_the_later_candidates(monkeypatch):
    """The add at 13 meV enters on its delta twin; the candidate priced after it (b4 at 17) must keep b3 at its floor
    width as a delta mode, so the incumbent and the candidate differ only by the change priced."""
    seen = []
    orig = R._Labelled.model

    def model(self, variant, delta):
        seen.append((variant.k, tuple(delta)))
        return orig(self, variant, delta)
    monkeypatch.setattr(R._Labelled, "model", model)
    neigh = {"x": (FOUR, B1234), "z": (FOUR, B1234)}
    true = TWO + [(13.0, 0.0, 60.0)]
    s = snap(true, TWO, ["b1", "b2"], neigh, [add("b3", 13.0, w=0.1), add("b4", 17.0)])
    prep = spectrum(true, seed=3, ceiling=4)
    prep.structures = STRUCT + ("elastic+stokes4+bg",)                                    # the card's four-mode structure
    a = R.evidence_assignment(replace(s, context=prep))
    assert [r["outcome"] for r in tried(a)][0] == "entered" and tried(a)[1]["count"] == 4
    assert (4, (2,)) in seen and (4, ()) not in seen


def test_a_new_mode_railed_at_the_floor_with_no_twin_has_no_answer(monkeypatch):
    """The delta twin of the added mode cannot be fitted, so its free width at the floor is no answer and it stays out."""
    orig = R._Labelled.fit

    def fit(self, variant, modes, family, linear=None):
        return None if any(m[3] for m in modes) else orig(self, variant, modes, family, linear)
    monkeypatch.setattr(R._Labelled, "fit", fit)
    a = R.evidence_assignment(snap(TWO + [(13.0, 0.0, 60.0)], TWO, ["b1", "b2"], NEIGH, [add("b3", 13.0, w=0.1)]))
    [row] = tried(a)
    assert row["outcome"] == "no_answer" and a.count == 2 and a.search["applied"] == []


def test_the_linked_count_wording_says_evidence_only_when_evidence_priced():
    args = ("accepted", "linked", 1, 2, ("b1", "b2"), ("b1",), None, 0.0)
    assert R._why(*args) == "The link's listed changes changed the number of modes from 1 to 2."
    assert R._why(*args, priced=True) == "The evidence accepted the link's listed changes: the number of modes went from 1 to 2."


def test_a_priced_linked_add_is_reported_as_evidence_backed():
    s = snap(THREE, TWO, ["b1", "b2"], NEIGH, [add("b3", 13.0)])
    r = R.refit(s, stage="continuous", assignment=R.evidence_assignment(s))
    assert (r.status, r.reason) == ("accepted", "linked")
    assert r.why_changed == "The evidence accepted the link's listed changes: the number of modes went from 2 to 3."
