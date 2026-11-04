"""One fit of the fixed mode set (S1U6): the refit's three steps, the close-pair restarts, the hard cases of a fixed set and the
three new flags. Spectra are Poisson counts generated at test time (``test_coupled_refit.spectrum``) and the links are
made up (``_search_harness``); no measured value. The real engine fits every case, so a refit takes a few seconds.
"""
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest
from _search_harness import add, links_for, snapshot_for, split_of, tie
from test_coupled_refit import codes, spectrum

from isar.analyze import prior
from isar.analyze.context import ScanContext
from isar.analyze.coupled import engine as cc
from isar.analyze.coupled import refit as R
from isar.analyze.coupled import rules
from isar.analyze.fast_pass import FastPassSettings
from isar.schema.steering import ParamConstraints


def test_runs_from_the_worktree():
    import isar
    here = __file__.replace("\\", "/").rsplit("/tests/", 1)[0]
    print("import root:", isar.__file__)
    assert isar.__file__.replace("\\", "/").startswith(here)


def snap(true, own, branches, neighbours=None, *, links=None, seed=3, scale=1.0):
    """The spectrum y drawn from ``true``, its incoming fit ``own`` named ``branches``, T-neighbours as
    ``snapshot_for`` makes them, and the link ``links`` (default: no changes, no ties)."""
    s = snapshot_for(own, branches, neighbours, links=links if links is not None else links_for(len(own)))
    return replace(s, context=spectrum(true, seed=seed, scale=scale, ceiling=3))


def fixed(s, apply_changes=True):
    return R.fixed_assignment(s, apply_changes=apply_changes)


def peaks_by_branch(r):
    return {p["branch"]: p for p in r.fit["peaks"]}


# ---- the three steps ---------------------------------------------------------------------------------------------
CLOSE_TRUE = [(8.0, 0.4, 50.0), (10.5, 0.4, 20.0)]
COINCIDENT = [(9.25, 0.4, 35.0), (9.25, 0.4, 35.0)]       # both modes on top of each other at the mean energy


def coincident_snapshot():
    """Two unmatched modes started coincident: every main start (incoming, selected, placed) is the same point."""
    return snap(CLOSE_TRUE, COINCIDENT, ["fast:y#0", "fast:y#1"])


@pytest.fixture
def stuck(monkeypatch):
    """The main starts' TRF ends where it began, as a fit stuck with two identical modes on top of each other does;
    the restarts and the release fit run the real optimiser. (A real TRF's one-sided difference steps usually
    break the symmetry by themselves, so a stuck main fit is made, not waited for.)"""
    real = R._Labelled._trf

    def trf(self, model, theta0, family):
        if family != "coupled":
            return real(self, model, theta0, family)
        zm = self.zmap(model)
        return SimpleNamespace(x=np.clip(zm.to_z(theta0), zm.lo, zm.hi))
    monkeypatch.setattr(R._Labelled, "_trf", trf)


def test_the_inline_refit_is_fit_then_restarts_then_finish(stuck):
    s = coincident_snapshot()
    a = fixed(s)
    stage = R.refit_step("fit", s, a)
    assert stage.failure is None and len(stage.restarts) == 3       # (d) of a coincident pair equals the solution
    results = [R.refit_step("restart", s, a, stage.model_key, x) for x in stage.restarts]
    stepped = R.refit_step("finish", s, a, stage, results)
    inline = R.refit(s, stage="continuous", assignment=a)
    assert inline.J == stepped.J and inline.status == stepped.status and inline.reason == stepped.reason
    assert inline.fit["models"][0]["params"] == stepped.fit["models"][0]["params"]
    assert codes(inline) == codes(stepped) and inline.starts == stepped.starts
    assert "restart_s" in inline.timing and stepped.timing["restart_s"] == 0.0
    with pytest.raises(ValueError, match="unknown refit step"):
        R.refit_step("search", s, a)


def test_the_restarts_escape_a_coincident_start_and_keep_the_count(stuck):
    s = coincident_snapshot()
    a = fixed(s)
    stage = R.refit_step("fit", s, a)
    assert stage.theta[0] == stage.theta[3]                          # the main solution: both modes at one energy
    r = R.refit(s, stage="continuous", assignment=a)
    assert r.J < stage.J - 1.0
    assert a.count == 2 and len(r.modes) == 2 and len(r.fit["peaks"]) == 2      # no twin screen merged them
    lo, hi = sorted(m.omega0 for m in r.modes)
    assert hi - lo > 1.5 and lo == pytest.approx(8.0, abs=0.3) and hi == pytest.approx(10.5, abs=0.3)


def _pinned_engine():
    x = np.linspace(-6.0, 24.0, 241)
    counts = 1.0 + 200.0 * np.exp(-0.5 * ((x - 9.0) / 1.0) ** 2)
    ctx = ScanContext(x=x, counts=counts, mask=np.ones(x.size, bool), T=300.0, center=0.0, gauss_fwhm=1.5,
                      lorentz_fwhm=0.0, instrument="S30", energy_status="absolute",
                      constraints=ParamConstraints(hard={"phonon.omega0": 9.0}))
    s = FastPassSettings(mode_ceiling=2)
    sp = prior.resolve_prior_for(ctx, s.prior or prior.PriorSettings())
    slot = cc.Entry(sp.energy, band=1.0, b_edge=0.0, b_mid=0.0)
    eng = cc.CoupledEngine(ctx, s, [slot, slot], continuous=True)
    model = eng.model(eng.variant(2, True, True), ())
    theta = np.zeros(len(model.names))
    for (a, g, c), (en, gam, h) in zip(model.modes, [(9.0, 0.5, 50.0), (9.3, 0.5, 50.0)]):
        theta[a], theta[g], theta[c] = en, gam, h
    theta[model.el], theta[model.bg] = 100.0, 2.0
    return eng, model, theta


def test_a_restart_never_moves_a_pinned_energy():
    eng, model, theta = _pinned_engine()
    (a0, _g0, c0), (a1, _g1, c1) = model.modes
    assert a0 in model.fixed_pos and eng.close_pairs(model, theta) == [(0, 1)]
    starts = R._restart_starts(eng, model, theta)
    assert len(starts) == 4
    assert all(x[a0] == 9.0 for x in starts)
    (ha, hb), (hb2, ha2) = (starts[0][[c0, c1]], starts[1][[c1, c0]])
    assert (ha, hb) == pytest.approx((75.0, 25.0)) and (hb2, ha2) == pytest.approx((75.0, 25.0))
    assert starts[2][a1] == pytest.approx(9.3 + 0.25 * eng.fwhm)            # wider: the free one moves alone
    assert starts[3][a1] == pytest.approx(9.15 + 0.5 * 0.15)                # narrower: its half-separation halved


# ---- acceptance and the record of a linked change ----------------------------------------------------------------
TWO = [(8.0, 0.4, 40.0), (10.0, 0.4, 40.0)]
NEIGH_TWO = {"x": (TWO, ["b1", "b2"]), "z": (TWO, ["b1", "b2"])}


def test_a_linked_split_is_accepted_as_linked_with_no_evidence_wording():
    split = split_of(0, "b1", 8.0, "b2", 10.0)
    links = links_for(1, changes=[split], labels=("b1",), ties=[tie("g1", "competition", ("x", "y"), ("b2",))])
    s = snap(TWO, [(9.0, 1.0, 60.0)], ["b1"], NEIGH_TWO, links=links)
    r = R.refit(s, stage="continuous", assignment=fixed(s))
    [amb] = [x for x in r.flag["reasons"] if x["code"] == "connection_ambiguous"]     # the link's near tie
    assert amb["ids"] == ["b1", "b2"] and amb["severity"] == "warn" and r.flag["flagged"]
    assert (r.status, r.reason, r.assignment.count) == ("accepted", "linked", 2)
    assert r.why_changed == "The link changed the number of modes from 1 to 2."
    assert "evidence" not in r.why_changed
    assert not {"neighbour_supported_split", "count_changed_by_neighbours"} & set(codes(r))
    assert r.fit["count_probabilities"] == {} and r.fit["models"][0]["probability"] is None
    assert r.fit["state"] == "resolved" and r.fit["probabilities_conditional"] is False


def test_the_linked_acceptance_rule_never_needs_evidence():
    base = dict(any_finite=True, evidence_missing_for_change=True, hard_violation=False, topology_changed=True,
                evidence_won=False, J_old=None, J_new=1.0, materially_moved=True)
    assert rules.acceptance(**base, linked=True) == ("accepted", "linked")
    assert rules.acceptance(**base)[0] == "failed"                            # the priced path is unchanged
    assert rules.acceptance(**{**base, "hard_violation": True}, linked=True) == ("rejected", "hard_constraint")
    assert rules.acceptance(**{**base, "any_finite": False}, linked=True)[0] == "failed"


# ---- the new flags ---------------------------------------------------------------------------------------------------
def test_a_width_on_its_floor_or_ceiling_is_at_its_limit():
    eng, model, theta = _pinned_engine()
    (_a0, g0, _c0), (_a1, g1, _c1) = model.modes
    zm = eng.zmap(model)
    t = theta.copy()
    t[g0] = model.lo[g0]
    t[g1] = zm.ceiling(t, 1)
    assert R._width_limits(eng, model, t) == [(0, "floor"), (1, "ceiling")]
    assert R._width_limits(eng, model, theta) == []


def test_the_connection_reason_names_the_branches_the_tie_moves():
    links = links_for(3, labels=("b1", "b2", "b3"), ties=[tie("g2", "competition", ("x", "y"), ("b1", "b4", "b3"))])
    [r] = R._connection_reasons(links)
    assert (r["code"], r["ids"], r["values"]) == ("connection_ambiguous", ["b2", "b4"],
                                                   {"group": "g2", "kind": "competition"})
    assert R._connection_reasons(links_for(1)) == [] and R._connection_reasons(None) == []


# ---- the fixed set's hard questions: a crossing, a hinted mode, a width at its floor -------------------------------
TODAY_STATUSES = {"determined_by_own_data", "held_by_neighbours", "poorly_determined"}


@pytest.mark.parametrize("seed", [3, 4])
def test_an_unresolvable_crossing_keeps_both_modes_with_their_own_intensities(seed):
    """The middle of a crossing: b1 runs 8 -> 10 meV and b2 10 -> 8 meV across its T-neighbours, so here one line at
    9 meV holds both. Both modes keep the link's labels, none is dropped, each reports its own fitted intensity (and
    its error bar where the fit has them), and no group is reported."""
    line = [(9.0, 1.0, 60.0)]
    nb = [(8.0, 1.0, 30.0), (10.0, 1.0, 30.0)]
    neigh = {"x": (nb, ["b1", "b2"]), "z": (nb, ["b2", "b1"])}
    links = links_for(2, labels=("b1", "b2"), ties=[tie("g1", "swap", ("x", "y"), ("b2", "b1"))])
    s = snap(line, [(8.9, 1.0, 30.0), (9.1, 1.0, 30.0)], ["b1", "b2"], neigh, links=links, seed=seed)
    r = R.refit(s, stage="continuous", assignment=fixed(s, apply_changes=False))
    peaks = peaks_by_branch(r)
    assert r.status in ("accepted", "unchanged") and r.assignment.slots == ("b1", "b2")
    assert r.assignment.count == 2 and sorted(peaks) == ["b1", "b2"] and len(r.modes) == 2
    assert r.fit["grouped_intensity"] == [] and {m.status for m in r.modes} <= TODAY_STATUSES
    assert all(p["area_stokes"] is not None for p in peaks.values())
    if r.fit["errors"] == "conditional":
        assert all(p["sigma_area"] is not None for p in peaks.values())


def _hinted(seed):
    """The link adds b2 at 15 meV where the spectrum (one line at 8 meV) shows nothing; the neighbours hold both."""
    one = [(8.0, 0.4, 40.0)]
    both = one + [(15.0, 0.4, 40.0)]
    neigh = {"x": (both, ["b1", "b2"]), "z": (both, ["b1", "b2"])}
    s = snap(one, one, ["b1"], neigh, links=links_for(1, changes=[add("b2", 15.0)]), scale=8.0, seed=seed)
    return R.refit(s, stage="continuous", assignment=fixed(s))


@pytest.mark.parametrize("seed", [3, 4])
def test_a_hinted_mode_the_counts_contradict_is_kept_with_its_label(seed):
    r = _hinted(seed)
    peaks = peaks_by_branch(r)
    assert r.status == "accepted" and r.assignment.count == 2 and len(r.modes) == 2
    assert sorted(peaks) == ["b1", "b2"] and peaks["b2"]["area_stokes"] is not None
    assert {m.status for m in r.modes} <= TODAY_STATUSES
