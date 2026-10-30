"""A shared coupling term is one definition in every block that holds it (PR #70 entry ad145c659301b-1).

Three spectra x (100 K), y (200 K), z (300 K) in a T-chain, synthetic and generated at test time. x holds one
broad mode and no branch y carries, so the term x|y is formed only once x is on branch b. Helpers come from
test_coupled_refit (basename import, as the other coupled tests do).
"""
from dataclasses import replace
from types import SimpleNamespace

from test_coupled_refit import S, held, record, spectrum

from isar.analyze.coupled import engine as cc
from isar.analyze.coupled import refit as R
from isar.analyze.coupled.types import Snapshot, TermInput
from isar.session.inter_spectra import runner
from isar.session.inter_spectra.graph import Term

TRUE_Y = [(6.0, 0.4, 40.0), (12.0, 0.4, 40.0)]
TRUE_X = [(9.0, 1.5, 30.0)]
PREP_Y = spectrum(TRUE_Y, ceiling=2)
XY, YZ = ("T-change", ("x", "y")), ("T-change", ("y", "z"))
XY_IDS = {"T-change:x|y:a", "T-change:x|y:b"}
FAST_X = held("x", TRUE_X, ["fast:x#0"], T=100.0)
HELD_X = held("x", TRUE_Y, ["a", "b"], T=100.0)
OWN_Y = held("y", TRUE_Y, ["a", "b"], T=200.0)
HELD_Z = held("z", TRUE_Y, ["a", "b"], T=300.0)


def _term(km):
    return Term(km[0], km[1], (100.0,), None, None)


def _stage1():
    """The stage-1 snapshots of x, y and z: y's neighbour x is still on its fast fit, z already carries a and b."""
    sx = Snapshot("b:round1", "b:round1", "x", "fx", spectrum(TRUE_X), FAST_X, record(TRUE_X),
                  (TermInput(_term(XY), (OWN_Y,)),), S, "simultaneous")
    sy = Snapshot("b:round1", "b:round1", "y", "fy", PREP_Y, OWN_Y, record(TRUE_Y),
                  (TermInput(_term(XY), (FAST_X,)), TermInput(_term(YZ), (HELD_Z,))), S, "simultaneous")
    sz = Snapshot("b:round1", "b:round1", "z", "fz", PREP_Y, HELD_Z, record(TRUE_Y),
                  (TermInput(_term(YZ), (OWN_Y,)),), S, "simultaneous")
    return {"x": sx, "y": sy, "z": sz}


def _assignments(snaps):
    """The hand-built stage decision: every spectrum on count 2, slots (a, b), each with its own incident factors."""
    return {u: replace(R._Spec(s).skipped(), count=2, slots=("a", "b"), origins=("matched",) * 2,
                       factors=R._Spec(s).incident(("a", "b")), discrete="priced", changed=True)
            for u, s in snaps.items()}


def _canonical_and_rebuilt(snaps):
    A = _assignments(snaps)
    refs = {u: [(u, f"f{u}", m) for m in range(2)] for u in snaps}
    ids = SimpleNamespace(ids={r: "ab"[r[2]] for u in snaps for r in refs[u]}, alternatives={"aliases": {}})
    A, rename = runner.canonical_factors(A, ids, refs, {})
    return runner.rebuilt_factors(R, A, snaps, rename)


def test_the_chain_fixture_reproduces_the_pr_state():
    A = _assignments(_stage1())
    assert not any(f.factor_id.startswith("T-change:x|y:") for f in A["y"].factors)
    assert XY_IDS <= {f.factor_id for f in A["x"].factors}


def test_a_shared_term_is_one_definition_in_every_block_that_holds_it():
    A = _canonical_and_rebuilt(_stage1())
    copies = [f for a in A.values() for f in a.factors]
    assert all(g in A[m].factors for g in copies for m in g.members)
    assert len(set(copies)) == len({g.factor_id for g in copies})     # one definition per id
    every = {g.factor_id: g for g in copies}
    assert XY_IDS <= set(every)
    E = {(m, fid): (6.0 if f.branch_id == "a" else 12.0) for fid, f in every.items() for m in f.members}
    _J, P = R.total_objective(copies, E, {}, {})
    assert set(P) == set(every)


def test_y_refit_includes_the_shared_term_once_x_carries_its_branches():
    A = _canonical_and_rebuilt(_stage1())
    sweep = Snapshot("b:sweep1", "b:round1", "y", "fy", PREP_Y, OWN_Y, record(TRUE_Y),
                     (TermInput(_term(XY), (HELD_X,)), TermInput(_term(YZ), (HELD_Z,))), S, "simultaneous")
    r = R.refit(sweep, stage="continuous", assignment=A["y"])
    assert XY_IDS <= set(r.P) and XY_IDS <= set(r.U)
    f = next(f for f in A["y"].factors if f.factor_id == "T-change:x|y:b")
    assert cc.potential(f, 1.0) > 0.1       # the term is loose enough that its absence would show


def test_a_neighbour_not_yet_on_the_branch_drops_the_term_for_that_sweep():
    A = _canonical_and_rebuilt(_stage1())
    first = Snapshot("b:sweep1", "b:round1", "y", "fy", PREP_Y, OWN_Y, record(TRUE_Y),
                     (TermInput(_term(XY), (FAST_X,)), TermInput(_term(YZ), (HELD_Z,))), S, "simultaneous")
    r = R.refit(first, stage="continuous", assignment=A["y"])
    assert not XY_IDS & set(r.P)
    assert any(x["code"] == "incomplete_comparison" and "x carries no branch b" in x["text"] for x in r.flag["reasons"])


def test_a_lone_spectrum_stage_is_left_as_it_is():
    snaps = {"y": _stage1()["y"]}
    A = _assignments(snaps)
    assert runner.rebuilt_factors(R, A, snaps, {})["y"].factors == A["y"].factors
