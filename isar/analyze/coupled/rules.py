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


def ln_w0(k: int, r: int, M: int) -> float:
    """ln w0(k,S,u) = -ln(min(k,M)+1) - ln C(M,r): the prior of one subset of r matched branches at count k, out
    of a pool of M, normalised once within the count (no count prior: a constant over counts cancels in every
    ratio, so no count's weight depends on which counts were visited; the extra-mode tail is summable only through
    the evidence, design build contract 7). No q_U^u here: each unmatched slot's Entry.ln_weight carries it inside
    the integral (charged once)."""
    if not 0 <= r <= min(k, M):
        raise ValueError("composition size out of range")
    return -math.log(min(k, M) + 1) - math.log(math.comb(M, r))


def ln_w_label(k: int, r: int, M: int) -> float:
    """The prior of one labelling (which mode, in energy order, carries which branch) realising a subset: the
    subset's ``ln_w0`` split evenly over its C(k, r) r! labellings, so a count's total cannot grow because a search
    found more orderings of one subset (build contract 1)."""
    return ln_w0(k, r, M) - math.log(math.comb(k, r) * math.factorial(r))


def pinned_entry_charge(entry, e_pin: float) -> float:
    """Outside-the-integral log mass of an unmatched slot whose energy is pinned (ln_prior skips a
    pinned coordinate): -(B_edge + B(E_pin))/2, once. A matched pinned mode pays nothing."""
    return -0.5 * (entry.b_edge + entry.B(e_pin))


def _lse(xs):
    m = max(xs)
    return m + math.log(sum(math.exp(x - m) for x in xs))


def decide(priced, incoming, *, eps=EPS_LOGZ, conf=CONFIDENCE, close=CLOSE_CALL):
    """Count first, then the labelling, then the model (design change 4).

    priced: [(count, slots, delta, ln_z or None[, variant])], ln_z already including the labelling weight and
    the variant weight; one row per distinct basin; delta = ascending slot positions whose width is held at the
    floor (() = every width free); variant = (elastic, background, qe) or absent. incoming: (count, slots,
    delta[, variant]). Rows of one (count, slots) are summed over every model, of one count over its labellings.
    The count with the highest total wins, but a change of count must beat the incoming count's total by ``eps``;
    within the count the best labelling wins (at the incoming count a change must beat the incoming labelling by
    ``eps`` when it was scored); a model (the width mask, and the variant when the incoming variant is known) is
    chosen after it, so a model never moves a label or count probability. With the incoming model priced, another
    replaces it only beyond ``eps``; with no priced row for it (a free width superseded by its delta twin, a QE
    line superseded by its QE-free twin) the best priced model is taken without a comparison. Ties between
    models go to the incoming one, then to fewer widths held at the floor, then lexicographically.
    Returns dict: winner (count, slots), delta, variant (None when untracked), changed (count, labelling or
    model), mask_only (changed with the incoming count and labelling), weights {(count, slots): p}, mask_weights
    {(count, slots, delta): p}, count_p {count: p}, label_p (the winner's probability within its count),
    count_confident, label_confident, ambiguous, close_calls [(count, slots)] within the winner's count,
    close_counts, reasons. A None ln_z is unpriced: never zero evidence, listed in reasons."""
    inc_k, inc_s, inc_d = incoming[0], tuple(incoming[1]), tuple(incoming[2])
    inc_v = tuple(incoming[3]) if len(incoming) > 3 and incoming[3] is not None else None
    track = inc_v is not None
    ksm, reasons = defaultdict(list), []
    for row in priced:
        k, s, d, z = row[:4]
        if z is None or not math.isfinite(z):
            reasons.append(f"unpriced:{k}:{s}")
            continue
        v = tuple(row[4]) if len(row) > 4 and row[4] is not None else None
        ksm[(k, tuple(s), ((v, tuple(d)) if track else tuple(d)))].append(z)
    if not ksm:
        return {"winner": None, "delta": (), "variant": None, "changed": False, "mask_only": False, "weights": {},
                "mask_weights": {}, "count_p": {}, "label_p": 0.0, "count_confident": False,
                "label_confident": False, "ambiguous": True, "close_calls": [], "close_counts": [],
                "reasons": reasons + ["no_priced_candidate"]}
    lz_ksm = {c: _lse(v) for c, v in ksm.items()}
    ks, kd, kk = defaultdict(list), defaultdict(list), defaultdict(list)
    for (k, s, m), v in lz_ksm.items():
        ks[(k, s)].append(v)
        kd[(k, s, m[1] if track else m)].append(v)
    lz_ks = {c: _lse(v) for c, v in ks.items()}
    for (k, _s), v in lz_ks.items():
        kk[k].append(v)
    lz_k = {k: _lse(v) for k, v in kk.items()}
    tot = _lse(list(lz_k.values()))
    weights = {c: math.exp(v - tot) for c, v in lz_ks.items()}
    mask_weights = {c: math.exp(_lse(v) - tot) for c, v in kd.items()}
    count_p = {k: math.exp(v - tot) for k, v in lz_k.items()}
    if inc_k not in lz_k:          # missing incoming evidence is not automatically a win (spec 5)
        reasons.append("incomplete_comparison")
        kw, s_w = inc_k, inc_s
    else:
        k_best = max(lz_k, key=lambda k: (lz_k[k], k == inc_k, -k))
        kw = k_best if k_best != inc_k and lz_k[k_best] > lz_k[inc_k] + eps else inc_k
        labels = {s: v for (k, s), v in lz_ks.items() if k == kw}
        s_w = max(labels, key=lambda s: (labels[s], s == inc_s))
        if kw == inc_k and inc_s in labels and not labels[s_w] > labels[inc_s] + eps:
            s_w = inc_s
    w, inc = (kw, s_w), (inc_k, inc_s)
    label_p = math.exp(lz_ks[w] - lz_k[kw]) if w in lz_ks else 0.0
    close_calls = [(k, s) for (k, s), v in lz_ks.items() if k == kw and s != s_w and w in lz_ks
                   and v >= lz_ks[w] - close]
    close_counts = [k for k, v in lz_k.items() if k != kw and kw in lz_k and v >= lz_k[kw] - close]
    # the model of the winner: sorted, so a full tie goes to the lexicographically first
    inc_m = (inc_v, inc_d) if track else inc_d
    m_w = inc_m
    models = {m: v for (k, s, m), v in lz_ksm.items() if (k, s) == w}
    if models:
        keep = inc_m if w == inc else None       # another labelling has no incoming model to keep
        dl = (lambda m: m[1]) if track else (lambda m: m)
        order = sorted(models, key=(lambda m: (str(m[0]), m[1])) if track else None)
        top = max(order, key=lambda m: (models[m], m == keep, -len(dl(m))))
        if w != inc:
            m_w = top
        elif inc_m in models:
            m_w = top if models[top] > models[inc_m] + eps else inc_m
        else:                      # no incoming mass to compare with: not an ambiguity, a record
            m_w = top
            reasons.append(f"mask_unpriced:{kw}:{s_w}:{inc_d}" + (f":{inc_v}" if track else ""))
    delta, variant = (m_w[1], m_w[0]) if track else (m_w, None)
    mask_only = w == inc and m_w != inc_m
    changed = w != inc or mask_only
    count_confident, label_confident = count_p.get(kw, 0.0) >= conf, label_p >= conf
    ambiguous = (not count_confident or not label_confident
                 or any(not r.startswith("mask_unpriced:") for r in reasons))
    return {"winner": w, "delta": tuple(delta), "variant": variant, "changed": changed, "mask_only": mask_only,
            "weights": weights, "mask_weights": mask_weights, "count_p": count_p, "label_p": label_p,
            "count_confident": count_confident, "label_confident": label_confident, "ambiguous": ambiguous,
            "close_calls": close_calls, "close_counts": close_counts, "reasons": reasons}


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
