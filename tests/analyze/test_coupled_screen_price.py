"""The screen price (C-depth): a screen cuts each split integral to its first node set, and a full price says
what its screen would have been. The three worked examples of the plan's C-depth, on the synthetic hidden-pair
case of ``tests/inter_hidden_case.py`` (counts generated at test time, hard rule 10).
"""
import math
from dataclasses import replace

import numpy as np
import pytest

from inter_hidden_case import hidden_snapshot
from isar.analyze.coupled import engine as cc
from isar.analyze.coupled import refit as R
from isar.analyze.coupled.types import PricingJob, Start

SLOTS = ("b1", "b2")
JOB1 = ("b1",)


def _job(snap, slots):
    """One start per job: the incoming mode for b1, the neighbours' mean prediction for b2 (as the hidden tests)."""
    spec = R._Spec(snap)
    own = spec.own.modes[0]
    nb = next(m for m in spec.held_all["x"].modes if m.branch == "b2")
    at = {"b1": (own.omega0, own.gamma, own.height), "b2": (float(np.mean(spec.pred["b2"])), nb.gamma, nb.height)}
    return PricingJob(snap.unit, snap.snapshot_id, len(slots), tuple(slots), f"{slots}",
                      (Start("own", tuple(at[b] for b in slots)),))


def test_a_screen_is_the_first_node_set_alone():
    # worked example 1: 2f is linear, so the 6-node set is exact, and the screen stops after that set
    res = cc.integrate_split(lambda f, near: math.log(2.0 * f), 0.5, screen=True)
    assert res.kind == "ok" and abs(res.ln_z) < 1e-9
    assert [n for n, _ in res.sets] == [6] and res.nodes == 6


def test_a_screen_takes_no_agreement_test_and_no_bail():
    # a narrow peak in f is "not_converged" in the full integral (test_coupled_hidden); the screen returns its first set
    res = cc.integrate_split(lambda f, near: -0.5 * ((f - 0.3) / 0.003) ** 2, 0.5, screen=True)
    assert res.kind == "ok" and len(res.sets) == 1 and res.nodes == 6


def test_a_job_without_a_hidden_pair_screens_to_its_full_price():
    # worked example 2: no split integral, so the screen is the full price and every basin is exact
    snap = hidden_snapshot(3.0, asimov=True)
    job = _job(snap, JOB1)
    full = R.price(snap, job)
    scr = R.price(snap, replace(job, depth="screen"))
    assert (full.depth, scr.depth) == ("full", "screen")
    assert scr.ln_z == pytest.approx(full.ln_z, abs=1e-9)
    assert [b[0] for b in scr.basins] == pytest.approx([b[0] for b in full.basins], abs=1e-9)
    assert full.screen_terms == scr.screen_terms == (None,) * len(full.basins)


def test_a_hidden_pair_full_price_carries_the_terms_its_screens_would_have_had(monkeypatch):
    # worked example 3: each split integral's screen is its first node set alone. The fixture has two split
    # integrals; the second does not converge in the full price, so it falls back to Laplace there, yet its screen
    # term is still carried.
    snap = hidden_snapshot(3.0, asimov=True)
    job = _job(snap, SLOTS)
    solves, orig = [], cc.integrate_split

    def spy(*a, **k):
        res = orig(*a, **k)
        solves.append(res.nodes)
        return res
    monkeypatch.setattr(cc, "integrate_split", spy)
    full = R.price(snap, job)
    full_solves = list(solves)
    solves.clear()
    scr = R.price(snap, replace(job, depth="screen"))
    assert (full.depth, scr.depth) == ("full", "screen")
    assert solves == [6, 6] and min(full_solves) >= 18      # the screens: set 1 alone; the full ladders: sets 1 and 2
    carried = sorted(t for t in full.screen_terms if t is not None)
    screened = sorted(b[0] for b in scr.basins if b[1].endswith("|split"))
    assert len(carried) == len(screened) == 2
    assert carried == pytest.approx(screened, abs=1e-9)
    for t, b in zip(scr.screen_terms, scr.basins):          # a screen's own terms are its terms
        assert t is None or t == pytest.approx(b[0], abs=1e-9)


def test_an_unknown_depth_is_refused():
    snap = hidden_snapshot(3.0, asimov=True)
    with pytest.raises(ValueError, match="depth"):
        R.price(snap, replace(_job(snap, JOB1), depth="half"))
