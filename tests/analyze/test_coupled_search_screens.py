"""Screen first, full price for contenders (plan S1U2, checks 1-7) against a scripted pricer, milliseconds each.

Every mass is a made-up table value (``_search_harness``); a split labelling's screen sits at its full term plus a
scripted offset. Mode "on" asks steps and moves as screens and promotes them only when a decision needs it; mode
"shadow" prices everything in full and records where the screened search would have decided otherwise.
"""
import math
import random
from dataclasses import replace

import pytest
from _search_harness import Scripted, run, snapshot_for
from test_coupled_search import B12, EX1, OWN, SIX, TABLE9, X12, happy

from isar.analyze.coupled import rules
from isar.analyze.coupled import search as SE
from isar.analyze.coupled.types import Priced, PricingJob, Start

NB2 = (None, "b2")   # a "call it new" move of B12 whose split pair keeps its order in the ledger


def test_runs_from_the_worktree():
    import isar
    here = __file__.replace("\\", "/").rsplit("/tests/", 1)[0]
    print("import root:", isar.__file__)
    assert isar.__file__.replace("\\", "/").startswith(here)


def by_cand(s, purpose):
    return [j for j, _p in s.outcomes if s.meta[j.key]["purpose"] == purpose]


# 1 the aggregate remainder (lens 1), on a hand-built ledger -----------------------------------------------------
def test_many_small_screened_terms_hold_the_count_decision_until_promoted_largest_first():
    s, sc = SE.Search(happy(), screens="on"), Scripted()
    s.M = sc.M = 200                    # a pool wide enough to name 100 labellings (masses below include the weight)
    X = -100.0

    def put(key, labels, mass, depth, screen_term=None):
        k = len(labels)
        b = sc.basin(k, labels, tuple(5.0 * (i + 1) for i in range(k)), mass)
        s.meta.setdefault(key, {"purpose": "move", "source": "test"})
        job = PricingJob("y", "b:ring1", k, labels, key, (), depth)
        p = Priced(key, k, labels, mass, "ok", b[4], None, 1, 0.0, (b,), (), None, depth=depth,
                   screen_terms=(None if screen_term is None else b[0],))
        s.current[key] = (job, p)

    put("a", B12, X, "full")                                           # incoming count 2
    put("b", ("b1", "b2", None), X - 0.05, "full")                     # count 3 on exact terms: 0.05 behind
    many = {f"c{i:03d}": ("b1", "b2", f"q{i:03d}") for i in range(100)}
    for i, (key, lab) in enumerate(many.items()):
        put(key, lab, X - 10.1 - 0.001 * i, "screen", True)            # screen + allowance = X - 5.1: behind the margin
    s._rebuild()
    f = s._final(s.view)
    assert f["d"]["winner"] == (2, B12) and not f["settled"]          # on exact terms A wins, but not settled
    asked = []
    while not f["settled"]:
        wave = s._rank(s.view, f["requests"])
        assert wave and len(wave) <= SE.PROMOTE_WAVE
        assert wave == sorted(set(many) - set(asked))[:SE.PROMOTE_WAVE]   # largest optimistic contribution first
        for key in wave:
            s.promoted.add(key)
            put(key, many[key], X - 5.2, "full")                       # each underrated by 4.9, inside the allowance
        asked += wave
        s._rebuild()
        f = s._final(s.view)
    assert f["d"]["winner"][0] == 3                                    # the 100 small terms carry count 3 past A
    assert all(key not in asked for key in many if key not in s.promoted)
    if len(asked) < len(many):                                         # settled with screens left: marked partial
        assert f["partial"] and f["remainder"] is not None


# 2 a full price supersedes its screen ----------------------------------------------------------------------------
class Moved(Scripted):
    """A screen of a split labelling lands at other heights: a basin the full price does not repeat."""

    def __call__(self, job):
        p = super().__call__(job)
        if job.depth != "screen" or not p.basins:
            return p
        basins = tuple((b[0], b[1], tuple((m[0], m[1], m[2], 3.0 * m[3], m[4]) for m in b[2]), *b[3:])
                       for b in p.basins)
        return Priced(p.key, p.count, p.slots, p.ln_z, p.status, p.mode, p.reason, p.starts, p.seconds, basins,
                      p.loss, p.residual_peak, depth=p.depth, screen_terms=p.screen_terms)


def test_a_promoted_candidate_s_mass_is_its_full_price_alone():
    T = {(2, B12): -100.0, (2, NB2): -101.0}
    a, _u, s = run(happy(), Moved(T, split={(2, NB2)}, screen={(2, NB2): -1.0}), screens="on")
    promoted = [c for c in s.promoted if s.current[c][0].count == 2]
    assert len(promoted) == 1 and s.current[promoted[0]][0].depth == "full"
    row = next(c for c in a.candidates if (c["count"], tuple(c["slots"])) == (2, NB2))
    assert row["ln_z"] == pytest.approx(-101.0, abs=1e-9) and row["price"] == "full"   # not the logsumexp of both
    assert len(s.ledger[(2, NB2)]["basins"]) == 1


# 3 a failed screen is promoted at once; a quiet one is final ---------------------------------------------------------
def test_a_failed_screen_is_promoted_in_the_same_pass_and_a_quiet_one_is_not():
    sc = Scripted({(2, B12): -100.0, (3, ("b1", "b2", None)): -101.0},
                  outcomes={1: ("no_evidence", "Hessian not positive definite"), 4: ("no_structure", "no model")})
    s = SE.Search(happy(), screens="on")
    sc.M = s.M
    passes = []
    while not s.done:
        jobs = s.next_jobs()
        passes.append(jobs)
        s.feed([sc(j) for j in jobs])
    failed = [(i, j) for i, js in enumerate(passes) for j in js if j.depth == "screen" and j.count == 1]
    assert failed
    for i, j in failed:
        full = s.meta[j.key]["of"]
        assert any(x.key == full and x.depth == "full" for x in passes[i + 1])     # the very next pass
    quiet = [j for js in passes for j in js if j.count == 4]
    assert quiet and all(j.depth == "screen" for j in quiet)                       # never asked in full
    assert s.result().coverage["stopped"] == {"down": "unresolved:1", "up": "no_structure:4"}


# 4 seeds full, screen keys distinct, a promotion carries the full key ----------------------------------------------
def test_seeds_are_full_steps_and_moves_are_screens_under_their_own_keys():
    T = {(2, B12): -100.0, (2, NB2): -101.0}
    _a, _u, s = run(happy(), Scripted(T, split={(2, NB2)}, screen={(2, NB2): -1.0}), screens="on")
    seeds = by_cand(s, "seed")
    assert seeds and all(j.depth == "full" for j in seeds)
    screens = [j for j, _p in s.outcomes if s.meta[j.key]["purpose"] in SE._SCREENED and j.key not in s.promoted]
    assert screens and all(j.depth == "screen" and j.key != s.meta[j.key]["of"] for j in screens)
    promos = [j for j, _p in s.outcomes if j.key in s.promoted]
    assert promos and all(j.depth == "full" and "of" not in s.meta[j.key] for j in promos)
    assert len({j.key for j, _p in s.outcomes}) == len(s.outcomes)                 # no key asked twice


# 5 far-behind screens stay screens -----------------------------------------------------------------------------------
def test_a_screen_behind_the_margin_even_at_its_allowance_is_never_promoted():
    T = {(2, B12): -100.0, (2, NB2): -112.0}
    a, _u, s = run(happy(), Scripted(T, split={(2, NB2)}), screens="on")
    row = next(c for c in a.candidates if (c["count"], tuple(c["slots"])) == (2, NB2))
    assert row["price"] == "screen" and row["ln_z"] is None and row["ln_z_screen"] == pytest.approx(-112.0)
    assert not s.promoted and a.search["screens"]["promoted"] == 0
    dec = a.coverage["decision"]
    assert dec["partial"] and dec["label_p_range"][0] <= dec["label_p"] <= dec["label_p_range"][1] + 1e-12
    assert a.search["screens"]["remainder_nats"] == pytest.approx(-112.0 + SE.SCREEN_ALLOWANCE_NATS, abs=1e-9)


# 6 the screened search reaches the full search's decision while the allowance holds -------------------------------
def three_branches():
    return snapshot_for(OWN, ["b1", "b2"], {"x": X12, "z": ([(15.0, 0.5, 20.0)], ["b3"])})


SORTED = [lab for lab in SIX if str(lab[0]) <= str(lab[1])]   # a split pair files in energy-then-label order
CASES = {
    "ex1": (happy, EX1, {(2, NB2): -4.9, (3, ("b1", "b2", None)): -2.0}),
    "table9": (happy, TABLE9, {(2, NB2): -3.0, (3, ("b1", "b2", None)): -4.9}),
    "six": (happy, {(2, B12): -100.0} | {(3, lab): -99.5 - math.log(6) for lab in SIX},
            {(3, lab): -2.0 * i for i, lab in enumerate(SORTED)}),
    "three": (three_branches, {(2, B12): -100.0, (3, ("b1", "b2", "b3")): -101.0, (2, NB2): -100.05},
              {(3, ("b1", "b2", "b3")): -4.0, (2, NB2): -4.9}),
}


def decision(a):
    return a.count, a.slots, a.coverage["decision"]["mask"]["chosen"], a.coverage["decision"]["variant"]["chosen"]


@pytest.mark.parametrize("case", sorted(CASES))
def test_mode_on_and_shadow_decide_alike_when_every_screen_is_within_the_allowance(case):
    snap, table, offsets = CASES[case]
    split = set(offsets)
    on, _u, son = run(snap(), Scripted(table, split=split, screen=offsets), screens="on")
    sh, _u, ssh = run(snap(), Scripted(table, split=split, screen=offsets), screens="shadow")
    assert decision(on) == decision(sh)
    assert sh.search["shadow"]["first_divergence"] is None, sh.search["shadow"]["decisions"]
    assert son.tried["screens"] > 0 and sh.search["shadow"]["basins"]
    assert all(full is not None and screen <= full <= screen + SE.SCREEN_ALLOWANCE_NATS
               for _k, _l, full, screen in sh.search["shadow"]["basins"])


# 7 the shadow catches a screen that underrates past the allowance -------------------------------------------------
def test_a_screen_underrated_past_the_allowance_on_the_winner_is_a_recorded_divergence():
    T = {(2, B12): -100.0, (2, NB2): -99.0}                           # the new-mode move wins under full prices
    a, _u, _s = run(happy(), Scripted(T, split={(2, NB2)}, screen={(2, NB2): -16.0}), screens="shadow")
    assert a.slots == NB2
    sh = a.search["shadow"]
    i = sh["first_divergence"]
    assert i is not None
    first = sh["decisions"][i]
    assert first["screen"] is not None and first["screen"] != first["full"]
    assert any(d["kind"] == "final" and d["screen"] is False for d in sh["decisions"])
    assert a.search["screens"]["mode"] == "shadow" and a.search["version"] == 2


# 8 the shadow's final choice is the whole choice: a mask change is a divergence ----------------------------------------
class Held(Scripted):
    """NB2's price holds two basins of one labelling: the width free (mask ()), a split basin a screen moves by ``off``,
    and the width held at the floor (mask (0, 1)), exact under either price."""

    def __init__(self, *, free, held, off):
        super().__init__({(2, NB2): free}, split={(2, NB2)}, screen={(2, NB2): off})
        self.held = held

    def __call__(self, job):
        p = super().__call__(job)
        if tuple(p.slots) != NB2:
            return p
        term, _label, modes, qq, mode, _ = p.basins[0]
        w = rules.ln_w_label(2, 1, self.M)
        held = (self.held - w, f"held{mode[:3]}", tuple((b, e, g, h, True) for b, e, g, h, _d in modes), qq, mode, mode)
        return replace(p, ln_z=max(term, self.held - w) + w, basins=(p.basins[0], held),
                       screen_terms=(p.screen_terms[0], None))


def test_the_shadow_final_choice_is_the_whole_choice_so_a_mask_change_is_a_divergence():
    # the free-width basin wins under full prices (-98.5 against -99.0); a screen 17 nats low leaves it at most -110.5
    # in the shadow, so the screened search settles on the held width: same winner and labelling, another mask
    a, _u, _s = run(happy(), Held(free=-98.5, held=-99.0, off=-17.0), screens="shadow")
    assert a.slots == NB2 and a.coverage["decision"]["mask"]["chosen"] == []
    sh = a.search["shadow"]
    assert sh["first_divergence"] is not None
    assert [d for d in sh["decisions"] if d["kind"] == "final"] == [
        {"kind": "final", "at": "decision", "full": True, "screen": False}]


# 9 an exact price that replaces a screened basin takes its origin ------------------------------------------------------
def test_an_exact_price_that_replaces_a_screened_basin_takes_its_peak_source_and_key():
    s = SE.Search(happy(), screens="on")
    start = Start("step", ((5.0, 0.5, None), (10.0, 0.5, None)))
    screen = PricingJob("y", "b:ring1", 2, NB2, "screen-key", (start,), "screen")
    full = PricingJob("y", "b:ring1", 2, NB2, "full-key", (start,), "full")
    s.meta["screen-key"] = {"purpose": "step_up", "source": "step_up", "of": "A"}
    s.meta["full-key"] = {"purpose": "move", "source": "move"}
    scr = Scripted({NB2: -100.0}, split={(2, NB2)}, screen={(2, NB2): -1.0}, peak=(20.0, 1.0))
    exact = Scripted({NB2: -100.0}, split={(2, NB2)}, screen={(2, NB2): -1.0}, peak=(30.0, 2.0))
    scr.M = exact.M = s.M
    s.current = {"A": (screen, scr(screen))}
    s._rebuild()
    e = s.view.ledger[(2, NB2)]
    assert e["scr"] == [True] and (e["key"], e["source"]) == ("screen-key", "step_up")
    s.current["B"] = (full, exact(full))                       # another candidate's exact price of the same basin
    s._rebuild()
    e = s.view.ledger[(2, NB2)]
    assert e["scr"] == [False] and len(e["basins"]) == 1
    assert (e["key"], e["source"], e["peak"]) == ("full-key", "move", (30.0, 2.0))


# 10 the shadow's count totals are the screened view's, so a basin only a screen filed counts -----------------------
class Relabelled(Scripted):
    """NB2's screen files its split basin under another branch than its full price does: its ledger key is its own."""

    def __call__(self, job):
        p = super().__call__(job)
        if job.depth != "screen" or tuple(job.slots) != NB2 or not p.basins:
            return p
        basins = tuple((b[0], b[1], tuple(("b3" if m[0] == "b2" else m[0],) + m[1:] for m in b[2]), *b[3:])
                       for b in p.basins)
        return replace(p, basins=basins)


def test_the_shadow_count_total_includes_a_basin_only_a_screen_filed():
    T = {(2, B12): -100.0, (2, NB2): -101.0}
    a, _u, s = run(happy(), Relabelled(T, split={(2, NB2)}, screen={(2, NB2): -1.0}), screens="shadow")
    sh = a.search["shadow"]
    (screen_only,) = [b for b in sh["basins"] if b[2] is None]          # the screen's basin: no full basin matches it
    b_row = next(c for c in a.candidates if (c["count"], tuple(c["slots"])) == (2, B12))
    screened = math.log(math.exp(b_row["ln_z"]) + math.exp(screen_only[3] + rules.ln_w_label(2, 1, s.M)))
    assert sh["counts"]["2"] == pytest.approx(screened, abs=1e-6)       # the seed's full mass and the screen's, nothing else


# S1U2 check 1: likely_next changes no pass and no result; () once done ------------------------------------------------
def test_likely_next_before_every_feed_changes_no_pass_and_no_result():
    rng = random.Random(1)               # made-up prices whose screens wait for a later feed (as check 2's seeds)
    keys = [(3, lab) for lab in SIX] + [(2, B12), (2, NB2)]
    T = {k: rng.uniform(-106.0, -96.0) for k in keys}
    offsets = {k: rng.uniform(-8.0, 0.0) for k in keys}

    def walk(probe):
        s = SE.Search(happy(), screens="on")
        sc = Scripted(T, split=set(offsets), screen=offsets)
        sc.M = s.M
        passes, named = [], 0
        while not s.done:
            jobs = s.next_jobs()
            passes.append([j.key for j in jobs])
            if probe:
                named += len(s.likely_next(1000))
                assert s.next_jobs() is jobs
            s.feed([sc(j) for j in jobs])
        assert s.likely_next(1000) == ()
        return passes, s.result(), named

    plain, probed = walk(False), walk(True)
    assert probed[:2] == plain[:2]
    assert probed[2] > 0                         # the probe named promotions, so the comparison is not vacuous


# S1U2 check 2: each promotion _rank asks of an earlier pass's screen was named by likely_next just before that feed
@pytest.mark.parametrize("seed", [1, 8, 21])   # random made-up prices whose screens are decided only by a later feed
def test_every_rank_promotion_of_an_earlier_screen_was_named_by_likely_next(seed, monkeypatch):
    ranked, rank = [], SE.Search._rank

    def spy(self, view, cands):
        out = rank(self, view, cands)
        ranked.append(out)
        return out
    monkeypatch.setattr(SE.Search, "_rank", spy)
    rng = random.Random(seed)
    keys = [(3, lab) for lab in SIX] + [(2, B12), (2, NB2)]
    table = {k: rng.uniform(-106.0, -96.0) for k in keys}
    offsets = {k: rng.uniform(-8.0, 0.0) for k in keys}
    s = SE.Search(happy(), screens="on")
    sc = Scripted(table, split=set(offsets), screen=offsets)
    sc.M = s.M
    asked_total = 0
    while not s.done:
        jobs = s.next_jobs()
        named = {j.key: j for j in s.likely_next(10 ** 6)}
        earlier = {c for c, (j, _p) in s.current.items() if j.depth == "screen"}
        ranked.clear()
        s.feed([sc(j) for j in jobs])
        pending = {j.key: j for j in s.next_jobs()}
        asked = {c for out in ranked for c in out} & earlier
        for c in asked:
            assert c in named and c in pending and named[c] == pending[c], c
        asked_total += len(asked)
    assert asked_total > 0


# S1U2 check 3: likely_next lists the screened candidates in _rank's order, promoted ones left out ---------------------
def test_likely_next_lists_screened_candidates_in_rank_order():
    s, sc = SE.Search(happy(), screens="on"), Scripted()
    s.M = sc.M = 200
    for key, mass in (("c0", -10.0), ("c1", -12.0), ("c2", -11.0)):    # optimistic contributions -5, -7, -6
        labels = ("b1", "b2", "q" + key)
        b = sc.basin(3, labels, (5.0, 10.0, 15.0), mass)
        s.meta[key] = {"purpose": "move", "source": "test"}
        job = PricingJob("y", "b:ring1", 3, labels, key, (), "screen")
        s.current[key] = (job, Priced(key, 3, labels, mass, "ok", b[4], None, 1, 0.0, (b,), (), None,
                                      depth="screen", screen_terms=(b[0],)))
    s._rebuild()
    assert [j.key for j in s.likely_next(10)] == ["c0", "c2", "c1"]
    assert [j.key for j in s.likely_next(2)] == ["c0", "c2"]
    s.promoted.add("c0")
    assert [j.key for j in s.likely_next(10)] == ["c2", "c1"]
    assert s._rank(s.view, ["c0", "c1", "c2"]) == ["c2", "c1"]


# S1U3 (amendment 2): likely_next names only promotions a later pass can ask -------------------------------------------
def test_likely_next_leaves_out_a_quiet_screen_and_one_with_no_screened_basin():
    s, sc = SE.Search(happy(), screens="on"), Scripted()
    s.M = sc.M = 200
    labels = ("b1", "b2", "q")
    job = PricingJob("y", "b:ring1", 3, labels, "ok1", (), "screen")
    b = sc.basin(3, labels, (5.0, 10.0, 15.0), -10.0)
    s.meta["ok1"] = {"purpose": "move", "source": "test"}
    s.current["ok1"] = (job, Priced("ok1", 3, labels, -10.0, "ok", b[4], None, 1, 0.0, (b,), (), None,
                                    depth="screen", screen_terms=(b[0],)))
    for key, status in (("quiet", "degenerate_twin"), ("bare", "no_evidence")):   # no price, no screened basin
        s.meta[key] = {"purpose": "move", "source": "test"}
        s.current[key] = (replace(job, key=key), Priced(key, 3, labels, None, status, None, "x", 1, 0.0, depth="screen"))
    s._rebuild()
    assert "quiet" not in s.view.opt and "bare" not in s.view.opt
    assert [j.key for j in s.likely_next(10)] == ["ok1"]
