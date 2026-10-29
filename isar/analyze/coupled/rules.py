"""Pure decision rules of the coupled refit (spec 3, 4, 5): composition weight, pinned entry charge,
marginalisation and the evidence rule, acceptance order, the settle rule, total_objective, flag score.
Expert hand-over for job j1c46f8949fcf, unit S1U2 (home: isar/analyze/coupled/rules.py, called by
refit.py behind the C-refit signatures). No engine, no numpy: each rule is checkable alone
(check_refit_rules.py). Every threshold is the spec's uncalibrated default unless the caller passes one.
"""
from __future__ import annotations

import math
from collections import defaultdict

EPS_LOGZ = 0.1          # nat, evidence hysteresis (spec 5)
CONFIDENCE = 0.80       # spec 3 ambiguity
CLOSE_CALL = 2.0        # nats, spec 3


def ln_w0(k: int, r: int, M: int, n_counts: int) -> float:
    """ln w0(k,S,u) = ln p0(k) - ln(min(k,M)+1) - ln C(M,r), p0 uniform over the allowed counts.
    No q_U^u here: each unmatched slot's Entry.ln_weight carries it inside the integral (charged once)."""
    if not 0 <= r <= min(k, M):
        raise ValueError("composition size out of range")
    return -math.log(n_counts) - math.log(min(k, M) + 1) - math.log(math.comb(M, r))


def pinned_entry_charge(entry, e_pin: float) -> float:
    """Outside-the-integral log mass of an unmatched slot whose energy is pinned (ln_prior skips a
    pinned coordinate): -(B_edge + B(E_pin))/2, once. A matched pinned mode pays nothing."""
    return -0.5 * (entry.b_edge + entry.B(e_pin))


def _lse(xs):
    m = max(xs)
    return m + math.log(sum(math.exp(x - m) for x in xs))


def decide(priced, incoming, *, eps=EPS_LOGZ, conf=CONFIDENCE, close=CLOSE_CALL):
    """priced: [(count, slots, ln_z or None)], ln_z already including ln w0 and variant weight; one row
    per distinct labelled basin (same (count, slots) rows are summed). incoming: (count, slots).
    Returns dict: winner (count, slots), changed, weights {(count, slots): p}, count_p {count: p},
    ambiguous, close_calls, reasons. A None ln_z is unpriced: never zero evidence, listed in reasons."""
    mass, reasons = defaultdict(list), []
    for k, s, z in priced:
        if z is None or not math.isfinite(z):
            reasons.append(f"unpriced:{k}:{s}")
        else:
            mass[(k, tuple(s))].append(z)
    if not mass:
        return {"winner": None, "changed": False, "weights": {}, "count_p": {}, "ambiguous": True,
                "close_calls": [], "reasons": reasons + ["no_priced_candidate"]}
    lz = {c: _lse(v) for c, v in mass.items()}
    tot = _lse(list(lz.values()))
    weights = {c: math.exp(v - tot) for c, v in lz.items()}
    by_k = defaultdict(list)
    for (k, s), v in lz.items():
        by_k[k].append(v)
    count_p = {k: math.exp(_lse(v) - tot) for k, v in by_k.items()}
    inc = (incoming[0], tuple(incoming[1]))
    best = max(lz, key=lambda c: (lz[c], c == inc))
    if inc in lz:
        changed = best != inc and lz[best] > lz[inc] + eps
        winner = best if changed else inc
    else:                          # missing incoming evidence is not automatically a win (spec 5)
        reasons.append("incomplete_comparison")
        winner, changed = inc, False
    kw = winner[0]
    within = [v for (k, s), v in lz.items() if k == kw]
    assoc_p = math.exp(lz[winner] - _lse(within)) if winner in lz else 0.0
    close_calls = [c for c in lz if c != winner and winner in lz and lz[c] >= lz[winner] - close]
    ambiguous = count_p.get(kw, 0.0) < conf or assoc_p < conf or bool(reasons)
    return {"winner": winner, "changed": changed, "weights": weights, "count_p": count_p,
            "ambiguous": ambiguous, "close_calls": close_calls, "reasons": reasons}


def eps_J(J_old: float) -> float:
    return max(1e-6, 1e-8 * max(1.0, abs(J_old)))


def acceptance(*, any_finite: bool, evidence_missing_for_change: bool, hard_violation: bool,
               topology_changed: bool, evidence_won: bool, J_old: float | None, J_new: float | None,
               materially_moved: bool):
    """Spec 5 order -> (status, reason). evidence_won: decide(...)['changed'] for a topology change."""
    if not any_finite or (topology_changed and evidence_missing_for_change):
        return "failed", "no_finite_candidate" if not any_finite else "evidence_unavailable"
    if hard_violation:
        return "rejected", "hard_constraint"
    if topology_changed:
        return ("accepted", "evidence") if evidence_won else ("rejected", "no_supported_improvement")
    if J_old is not None and J_new is not None and J_old - J_new > eps_J(J_old):
        return "accepted", "objective"
    if not materially_moved:
        return "unchanged", None
    return "rejected", "material_change_without_gain"


def settled(history, *, tol_mev=0.01, rel_J=1e-5, cap=20):
    """history: per full sweep {"topology": hashable, "max_dE": float, "J": float, "failed": int}.
    Settled when the last TWO sweeps each have the previous topology, max_dE <= tol, relative J gain
    <= rel_J and no failed block. Returns "settled" | "running" | "did_not_settle"."""
    ok = lambda a, b: (a["topology"] == b["topology"] and b["max_dE"] <= tol_mev and not b["failed"]
                       and (a["J"] - b["J"]) <= rel_J * max(1.0, abs(a["J"])))
    if len(history) >= 3 and ok(history[-3], history[-2]) and ok(history[-2], history[-1]):
        return "settled"
    return "did_not_settle" if len(history) >= cap else "running"


def total_objective(factors, energies, data, unary, potential):
    """Spec 4: sum D_i + each physical factor ONCE (by factor_id; two different definitions under one
    id raise) + unary (2 ramp + B) per block. energies: {(unit, factor_id): E of unit's member}.
    potential: engine.potential. Returns (J, {factor_id: P})."""
    seen = {}
    for f in factors:
        if f.factor_id in seen and seen[f.factor_id] != f:
            raise ValueError(f"two definitions under one factor id: {f.factor_id}")
        seen[f.factor_id] = f
    P = {fid: potential(f, sum(a * energies[(m, fid)] for m, a in zip(f.members, f.a)))
         for fid, f in seen.items()}
    return sum(data.values()) + sum(P.values()) + sum(unary.values()), P


def flag_score(R, U):
    """F = R + max U (U unlevelled, spec 6); None when the release fit failed (never 0)."""
    return None if R is None else R + max(U.values(), default=0.0)


def flag_threshold(C: float, z: float = 3.0) -> float:
    """Calibration proposal (record: opus/calib_T2.out.txt): the threshold on U = C z^2/2 expressed as
    a significance, threshold = (C/2) z^2; z = 3 gives 54 at C = 12."""
    return 0.5 * C * z * z
