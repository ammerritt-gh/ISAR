"""Joint, gap-aware assignment of fitted modes to excitation branches.

The fitter deliberately reports unordered modes.  This module is the identity layer: it assigns
all modes on an ordered objective path jointly, so a decision at one q never becomes its own prior.
It is Qt-free and works only with JSON-shaped dictionaries at the session boundary.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

import numpy as np


MISSING_LOG_PENALTY = math.log(0.08)
UNASSIGNED_LOG_PENALTY = math.log(0.35)
TRANSITION_SIGMA_FLOOR_MEV = 0.25
DEFAULT_CANDIDATE_SIGMA_MEV = 0.35
CONFIRMED_PROBABILITY = 0.85
UNRELIABLE_EVIDENCE_FACTOR = 0.70
ORDERING_LOG_FLOOR = math.log(0.05)

# The "the mode could be anywhere" baseline every confidence-weighted term is scored against (O30).
# Used only when the campaign does not supply its authorized energy domain; it is a constant applied
# identically to every branch, so it scales how much a *declared* branch beats "anywhere" without ever
# favouring one branch over another.
DEFAULT_ENERGY_DOMAIN_MEV = (-2.0, 18.0)
BRANCH_TRACKING_SCORER_REVISION = 2
BRANCH_TRACKING_SEMANTICS_REVISION = "isar.branch_tracking/2"


def probability_semantics(probabilities: dict | None, *, target_branch: str) -> dict:
    """Producer-owned declarations for connected-tracker probability-like outputs."""
    from isar.schema.statistics import authority_scopes

    labels = sorted(str(label) for label in (probabilities or {}))
    state_space = {"kind": "tracked_branch_identity", "labels": labels}
    common = {
        "source_measure_semantics": "normalized_factor_graph_score",
        "scorer_revision": BRANCH_TRACKING_SEMANTICS_REVISION,
        "calibration_reference": None,
        "state_space": state_space,
        "inference_paradigm": "normalized_factor_graph_distribution",
        "authority_scopes": authority_scopes(
            single_fit_delivery="active",
            stopping="active",
            acquisition_selection="active",
            user_diagnostic="active",
        ),
    }
    return {
        "target_probability": {
            **common,
            "quantity_kind": "marginal_weight",
            "conditioning": f"tracked_target_branch={target_branch}",
        },
        "identity_entropy": {
            **common,
            "quantity_kind": "entropy",
            "conditioning": "known_branch",
        },
    }

# Character is a trajectory-level latent variable.  A finite floor keeps all hypotheses represented
# numerically while making a local acoustic<->optic hand-off negligible relative to reconnecting the
# same character; a full authoritative replay can still choose the opposite character everywhere.
CHARACTER_HANDOFF_LOG_PENALTY = math.log(1e-12)


@dataclass(frozen=True)
class _State:
    optic: int | None
    acoustic: int | None


def anchor_index(path: Iterable[Iterable[float]], period: float | None, *,
                 target_branch: str = "optic", observations: list[dict] | None = None,
                 acoustic_hypothesis: dict | None = None) -> int:
    """Return the deterministic identity anchor for a connected branch.

    Optic branches anchor at reduced q=0.  An acoustic mode is position-unresolvable there, so its
    acquisition seed is chosen only from the declared gap/slope hypothesis and local resolution.
    Fitted candidates are intentionally not energy-ranked or locally labeled here; the joint tracker
    owns identity.  The zone centre remains the conservative fallback.
    """
    points = [tuple(float(value) for value in q) for q in path]
    if not points:
        return 0
    spans = [max(q[axis] for q in points) - min(q[axis] for q in points)
             for axis in range(len(points[0]))]
    axis = int(np.argmax(spans)) if any(span > 0.0 for span in spans) else 0
    if period is None or not np.isfinite(period) or period <= 0.0:
        return 0
    reduced = [abs(q[axis] - round(q[axis] / period) * period) for q in points]
    zone_anchor = min(range(len(points)), key=lambda index: (reduced[index], index))
    if target_branch != "acoustic" or not observations:
        return zone_anchor
    hypothesis = dict(acoustic_hypothesis or {})
    if hypothesis:
        zc_mean, _zc_sigma = _prior_pair(hypothesis, "zc_energy", (0.0, 0.5))
        slope_mean, _slope_sigma = _prior_pair(hypothesis, "slope_zc", (12.0, 12.0))
        observed = []
        for index, row in enumerate(observations):
            if not (row.get("candidates") or []):
                continue
            try:
                width = float(row.get("gauss_fwhm"))
            except (TypeError, ValueError):
                continue
            if np.isfinite(width) and width > 0.0:
                observed.append((index, width))
        if observed:
            predicted_resolvable = [
                index for index, fwhm in observed
                if abs(zc_mean + slope_mean * reduced[index]) >= fwhm
            ]
            if predicted_resolvable:
                return min(predicted_resolvable, key=lambda index: (reduced[index], index))
            farthest = max(reduced[index] for index, _fwhm in observed)
            return min(index for index, _fwhm in observed
                       if math.isclose(reduced[index], farthest,
                                       rel_tol=0.0, abs_tol=1e-12))
    return zone_anchor


def _logsumexp(values) -> float:
    values = np.asarray(values, dtype=float)
    if values.size == 0:
        return -math.inf
    maximum = float(np.max(values))
    if not np.isfinite(maximum):
        return maximum
    return maximum + math.log(float(np.exp(values - maximum).sum()))


def _normal_compatibility(value: float, mean: float, sigma: float) -> float:
    """Dimensionless hypothesis compatibility for a discrete identity decision.

    Used where no confidence weighting applies (the transition likelihood). The normalization term of
    a probability *density* must not become a prior against a broad but explicitly declared branch:
    doing so made a 3 meV-wide ZC prior permanently favor ``new`` over an observed peak exactly at its
    mean, regardless of accumulated counts. For the confidence-weighted emission terms see
    :func:`_baselined_compatibility`, which restores normalization safely by taking a *ratio* against
    a baseline shared by every branch.
    """
    sigma = max(float(sigma), 1e-6)
    z = (float(value) - float(mean)) / sigma
    return -0.5 * z * z


def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def normalize_energy_domain(
        domain: tuple[float, float] | list[float] | Iterable[Iterable[float]] | None,
) -> tuple[tuple[float, float], ...]:
    """Return a sorted, merged union of finite energy intervals.

    A two-value interval remains the backward-compatible public input. Callers that have separately
    authorized target, identity, or probe windows may pass an iterable of intervals instead.
    """
    if domain is None:
        raw_intervals = [DEFAULT_ENERGY_DOMAIN_MEV]
    else:
        raw = list(domain)
        if len(raw) == 2 and all(isinstance(value, (int, float, np.number)) for value in raw):
            raw_intervals = [raw]
        else:
            raw_intervals = raw
    intervals = []
    for interval in raw_intervals:
        try:
            lo, hi = interval
            lo, hi = float(lo), float(hi)
        except (TypeError, ValueError):
            raise ValueError("energy domain must contain two-value intervals") from None
        if not (np.isfinite(lo) and np.isfinite(hi) and lo < hi):
            raise ValueError("energy domain intervals must be finite and increasing")
        intervals.append((lo, hi))
    if not intervals:
        raise ValueError("energy domain must contain at least one interval")
    merged: list[list[float]] = []
    for lo, hi in sorted(intervals):
        if not merged or lo > merged[-1][1]:
            merged.append([lo, hi])
        else:
            merged[-1][1] = max(merged[-1][1], hi)
    return tuple((lo, hi) for lo, hi in merged)


def _baselined_compatibility(value: float, mean: float, sigma: float, confidence: float,
                             domain: tuple[float, float] | Iterable[Iterable[float]]) -> float:
    """Log Bayes factor for "this candidate is that branch" against a branch-neutral baseline (O30).

    The declared branch is trusted with probability ``confidence``; otherwise the mode could be
    anywhere in the authorized energy domain. Scoring the resulting mixture as a *ratio* against that
    same uniform makes the number comparable across branches::

        log[ (c·N_trunc(value; mean, sigma) + (1-c)·U) / U ]

    ``c = 0`` returns exactly ``0.0`` — no evidence, identically for every candidate and every branch —
    rather than the *no penalty* the previous ``confidence * (-z²/2)`` produced, which let an untrusted
    hypothesis outscore a trusted one on the same mismatched candidate.

    Confidence is deliberately **not** monotone in the returned score: a confident hypothesis that the
    data contradicts can score below a vague one, which is the correct Bayesian reading and the price
    of a shared baseline. What is guaranteed is that no branch gains an advantage from being trusted
    less, because both are measured against the same ``U``.

    The branch density is truncated to the authorized interval union, so a prior whose mean sits near
    an edge is not charged for mass it cannot place there. Values outside every interval are impossible
    under both densities and are rejected before the zero-confidence shortcut.
    """
    intervals = normalize_energy_domain(domain)
    value = float(value)
    if not np.isfinite(value) or not any(lo <= value <= hi for lo, hi in intervals):
        return -math.inf
    width = sum(hi - lo for lo, hi in intervals)
    confidence = min(max(float(confidence), 0.0), 1.0)
    if confidence <= 0.0:
        return 0.0
    sigma = max(float(sigma), 1e-6)
    z = (value - float(mean)) / sigma
    # Mass of the declared branch that actually falls inside the authorized domain. Floored so a
    # branch predicted far outside it cannot divide by ~0 and manufacture unbounded support.
    inside = max(sum(
        _normal_cdf((hi - float(mean)) / sigma) - _normal_cdf((lo - float(mean)) / sigma)
        for lo, hi in intervals), 1e-6)
    # density_ratio = N_trunc(value) / U, both proper densities on the domain.
    log_density_ratio = (math.log(width) - math.log(sigma) - 0.5 * math.log(2.0 * math.pi)
                         - math.log(inside) - 0.5 * z * z)
    if confidence >= 1.0:
        return log_density_ratio
    return float(np.logaddexp(math.log(confidence) + log_density_ratio, math.log1p(-confidence)))


def _baselined_evidence(log_likelihood: float, weight: float) -> float:
    """The same construction for evidence that is not a density over energy (the ordering term).

    The baseline is "the hypothesis makes no ordering claim", whose likelihood ratio is 1, so a
    ``weight`` of 0 contributes exactly 0 — matching the branch that never carries an ordering term at
    all. Scaling the raw log-likelihood instead made a low-confidence acoustic hypothesis cheaper to
    assign than a trusted one.
    """
    weight = min(max(float(weight), 0.0), 1.0)
    if weight <= 0.0:
        return 0.0
    if weight >= 1.0:
        return float(log_likelihood)
    return float(np.logaddexp(math.log(weight) + float(log_likelihood), math.log1p(-weight)))


def _hypothesis(hypotheses: list[dict], name: str) -> dict:
    return next((dict(item) for item in hypotheses if item.get("name") == name), {})


def _prior_pair(hypothesis: dict, key: str, default: tuple[float, float]) -> tuple[float, float]:
    value = hypothesis.get(key) or {}
    try:
        return float(value.get("mean", default[0])), max(float(value.get("sigma", default[1])), 1e-6)
    except (TypeError, ValueError):
        return default


def _candidate_sigma(candidate: dict) -> float:
    try:
        value = float(candidate.get("sigma_meV", candidate.get("omega0_err")))
    except (TypeError, ValueError):
        value = DEFAULT_CANDIDATE_SIGMA_MEV
    return value if np.isfinite(value) and value > 0.0 else DEFAULT_CANDIDATE_SIGMA_MEV


def _candidate_energy(candidate: dict) -> float:
    return float(candidate.get("energy_meV", candidate.get("omega0")))


def _hypothesis_confidence(hypothesis: dict) -> float:
    try:
        value = float(hypothesis.get("confidence", 1.0))
    except (TypeError, ValueError):
        value = 1.0
    return min(max(value, 0.0), 1.0)


def _lowest_energy_log_likelihood(index: int, candidates: list[dict]) -> float:
    """Return soft evidence that ``index`` is lower than every resolved rival.

    A single fitted mode contains no ordering information.  With rivals, their fit uncertainties
    turn the ordering into a probability instead of a hard lowest-mode label.
    """
    if len(candidates) < 2:
        return 0.0
    selected = candidates[index]
    energy = _candidate_energy(selected)
    sigma = _candidate_sigma(selected)
    evidence = 0.0
    for rival_index, rival in enumerate(candidates):
        if rival_index == index:
            continue
        combined = math.sqrt(sigma * sigma + _candidate_sigma(rival) ** 2)
        z = (_candidate_energy(rival) - energy) / combined
        probability = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
        evidence += max(math.log(max(probability, 1e-12)), ORDERING_LOG_FLOOR)
    return evidence / (len(candidates) - 1)


def _states(n_candidates: int) -> list[_State]:
    indices = [None, *range(n_candidates)]
    return [_State(optic, acoustic) for optic in indices for acoustic in indices
            if optic is None or acoustic is None or optic != acoustic]


def _predicted_branch_mean(branch: str, hypothesis: dict, reduced_distance: float) -> float:
    """The energy at which ``branch`` is predicted at this q, from the declared gap + slope prior.

    Same computation as the assigned-candidate compatibility below, extracted so a MISSING branch --
    which the compatibility loop skips -- can still be located for the clean-null relaxation.
    """
    zc_default = (6.0, 3.0) if branch == "optic" else (0.0, 0.2)
    mean, _sigma = _prior_pair(hypothesis, "zc_energy", zc_default)
    if reduced_distance > 0.0:
        slope, _slope_sigma = _prior_pair(
            hypothesis, "slope_zc", (0.0, 8.0) if branch == "optic" else (12.0, 12.0))
        mean += slope * reduced_distance
    return mean


def _null_relaxation(branch_mean: float, null_windows) -> float:
    """Fraction in [0, 1] by which a trustworthy clean null covering ``branch_mean`` relaxes the
    missing-branch penalty.

    A clean null found no mode where the branch is predicted, so its absence there is *confirmed*
    rather than merely unmeasured, and leaving it unassigned should no longer be fully penalised. The
    fraction never exceeds the null's detection sensitivity, and it is used only to cancel
    ``MISSING_LOG_PENALTY`` -- never to reward absence, never to force it, and never on a state that
    assigns a candidate. That structural boundary is why negative evidence cannot suppress a fitted
    mode: a null with no candidate at ``branch_mean`` cannot contradict a fit that put one there.

    The evidence is deliberately branch-agnostic: "no mode in this window" confirms the absence of
    *whichever* branch happens to be predicted inside it, so a window that covers two branches'
    predicted means relaxes both. That is correct -- an empty window holds no acoustic and no optic
    mode alike -- and it is why the window carries no branch label of its own.
    """
    best = 0.0
    for window in null_windows or ():
        lo, hi, sensitivity = window
        if float(lo) <= float(branch_mean) <= float(hi):
            best = max(best, min(max(float(sensitivity), 0.0), 1.0))
    return best


def _emission(state: _State, candidates: list[dict], *, reduced_distance: float,
              period: float | None,
              optic_hypothesis: dict, acoustic_hypothesis: dict,
              null_windows=None,
              energy_domain: tuple[float, float] = DEFAULT_ENERGY_DOMAIN_MEV,
              ) -> float:
    assigned = {index for index in (state.optic, state.acoustic) if index is not None}
    score = (len(candidates) - len(assigned)) * UNASSIGNED_LOG_PENALTY
    if state.optic is None:
        relax = _null_relaxation(
            _predicted_branch_mean("optic", optic_hypothesis, reduced_distance), null_windows)
        score += MISSING_LOG_PENALTY * (1.0 - relax)
    if state.acoustic is None:
        relax = _null_relaxation(
            _predicted_branch_mean("acoustic", acoustic_hypothesis, reduced_distance), null_windows)
        score += MISSING_LOG_PENALTY * (1.0 - relax)
    for branch, index, hypothesis, default in (
            ("optic", state.optic, optic_hypothesis, (6.0, 3.0)),
            ("acoustic", state.acoustic, acoustic_hypothesis, (0.0, 0.2))):
        if index is None:
            continue
        mean, sigma = _prior_pair(hypothesis, "zc_energy", default)
        if reduced_distance > 0.0:
            slope, slope_sigma = _prior_pair(
                hypothesis, "slope_zc", (0.0, 8.0) if branch == "optic" else (12.0, 12.0))
            mean += slope * reduced_distance
            sigma = math.sqrt(sigma * sigma
                              + (slope_sigma * reduced_distance) ** 2)
        candidate = candidates[index]
        combined = math.sqrt(sigma * sigma + _candidate_sigma(candidate) ** 2)
        confidence = _hypothesis_confidence(hypothesis)
        score += _baselined_compatibility(_candidate_energy(candidate), mean, combined,
                                          confidence, energy_domain)
        if branch == "acoustic" and len(candidates) > 1:
            # Lowest-mode character is useful close to Gamma, but provides no identity evidence at
            # the zone boundary where acoustic and optic energies may approach or cross.
            half_period = (float(period) / 2.0
                           if period is not None and np.isfinite(period) and period > 0.0
                           else None)
            ordering_weight = (max(0.0, 1.0 - reduced_distance / half_period)
                               if half_period else 0.0)
            score += _baselined_evidence(_lowest_energy_log_likelihood(index, candidates),
                                         confidence * ordering_weight)
        # Fit reliability affects evidence strength, but never deletes the candidate.
        if candidate.get("reliable") is False:
            score += math.log(UNRELIABLE_EVIDENCE_FACTOR)
    return score


def _transition(previous: _State, current: _State, previous_candidates: list[dict],
                current_candidates: list[dict], radial_delta: float,
                optic_hypothesis: dict, acoustic_hypothesis: dict) -> float:
    score = 0.0
    for branch, old_index, new_index, hypothesis, default in (
            ("optic", previous.optic, current.optic, optic_hypothesis, (0.0, 8.0)),
            ("acoustic", previous.acoustic, current.acoustic, acoustic_hypothesis, (12.0, 12.0))):
        if old_index is None or new_index is None:
            continue
        slope, slope_sigma = _prior_pair(hypothesis, "slope_zc", default)
        old = previous_candidates[old_index]
        new = current_candidates[new_index]
        expected_change = slope * radial_delta
        sigma = math.sqrt(
            _candidate_sigma(old) ** 2 + _candidate_sigma(new) ** 2
            + (slope_sigma * abs(radial_delta)) ** 2
            + TRANSITION_SIGMA_FLOOR_MEV ** 2)
        # This is a discrete association likelihood.  Density normalization would reward whichever
        # declaration happened to carry the narrower slope uncertainty once per path edge, allowing
        # that prior-volume artefact to overwhelm a complete trajectory's fresh evidence.
        score += _normal_compatibility(
            _candidate_energy(new) - _candidate_energy(old), expected_change, sigma)
    # A connected physical trajectory cannot silently change acoustic/optic character merely
    # because one label is dropped and the other is introduced at the next q.
    #
    # This fires only on a TRUE hand-off: the branch is no longer assigned AND the opposite label
    # has taken up a candidate continuous with it.  It deliberately does NOT fire when the branch is
    # simply undetected, because "character cannot flip" and "a branch must be detected at every q"
    # are different propositions and only the first is physics.  At the counting statistics this
    # instrument delivers (peak heights of order 5-10 counts) a two-mode fit losing one mode is an
    # expected Poisson outcome, so charging it as evidence against the branch's identity at the
    # PREVIOUS q inverted a confirmed trajectory into an ambiguous one and deadlocked the campaign:
    # the blocked frontier then withheld the exposure that would have resolved the missing mode.
    # Merely vanishing is already priced by MISSING_LOG_PENALTY. Laundering character through an
    # unassigned state is still charged below, so deferring the invariant never waives it.
    assigned_now = {index for index in (current.optic, current.acoustic) if index is not None}
    for old_index, same_new_index, rival_new_index, rival_old_index in (
            (previous.acoustic, current.acoustic, current.optic, previous.optic),
            (previous.optic, current.optic, current.acoustic, previous.acoustic)):
        if old_index is None or same_new_index is not None or not current_candidates:
            continue
        old = previous_candidates[old_index]
        nearest_index = min(range(len(current_candidates)),
                            key=lambda index: abs(_candidate_energy(current_candidates[index])
                                                  - _candidate_energy(old)))
        nearest = current_candidates[nearest_index]
        if nearest_index not in assigned_now:
            # The branch's own continuation is sitting unassigned. Leaving it unlabelled for one q
            # is how a path launders character: pay the unassigned/missing cost once, then re-emerge
            # under the opposite label with the invariant never charged. Price it here.
            score += _handoff_penalty(old, nearest, None, radial_delta)
        elif rival_new_index is not None and nearest_index == rival_new_index:
            rival_predecessor = (previous_candidates[rival_old_index]
                                 if rival_old_index is not None else None)
            score += _handoff_penalty(old, nearest, rival_predecessor, radial_delta)
        # Otherwise the branch simply went undetected while its neighbour continued legitimately —
        # an expected Poisson outcome at these counts, and not evidence about character.
    return score


def _handoff_penalty(old: dict, rival: dict, rival_predecessor: dict | None,
                     radial_delta: float) -> float:
    """Cost of the opposite label taking up a candidate that belongs to ``old``'s trajectory.

    Energy decides, not labels: asking "did the label change?" by inspecting labels is circular.
    The rival is only a character swap if it is better explained as a continuation of the vanished
    branch than of its own.  Without that comparison the term fires on the ordinary case where one
    branch legitimately continues onto the sole surviving candidate while the other simply went
    undetected — which is how a confirmed trajectory became ambiguous and stalled a campaign.
    """
    def _continuity(predecessor: dict) -> float:
        delta = _candidate_energy(rival) - _candidate_energy(predecessor)
        sigma = math.sqrt(
            _candidate_sigma(predecessor) ** 2 + _candidate_sigma(rival) ** 2
            + TRANSITION_SIGMA_FLOOR_MEV ** 2 + (12.0 * abs(radial_delta)) ** 2)
        return math.exp(-0.5 * (delta / sigma) ** 2)

    swapped = _continuity(old)
    if rival_predecessor is not None and _continuity(rival_predecessor) >= swapped:
        # The rival is at least as consistent with its own branch, so no character changed hands.
        return 0.0
    return swapped * CHARACTER_HANDOFF_LOG_PENALTY


def _path_geometry(path: list[list[float]], anchor: int) -> tuple[list[float], list[float]]:
    distance = [0.0]
    for left, right in zip(path, path[1:]):
        distance.append(distance[-1] + math.dist(left, right))
    radial = [abs(value - distance[anchor]) for value in distance]
    return distance, radial


def _entropy(probabilities: dict[str, float]) -> float:
    return -sum(value * math.log(value) for value in probabilities.values() if value > 0.0)


def _identity_entropy(probabilities: dict[str, float]) -> float:
    """Entropy of acoustic-vs-optic identity, conditional on the candidate being a known branch.

    ``new`` remains represented in the marginal and is guarded by ``CONFIRMED_PROBABILITY``.  It is
    not allowed to double-count uncertainty once two separated candidates already have decisive,
    one-to-one acoustic/optic ownership.
    """
    known = {name: float(probabilities.get(name, 0.0)) for name in ("optic", "acoustic")}
    total = sum(known.values())
    if total <= 0.0:
        return math.inf
    return _entropy({name: value / total for name, value in known.items()})


def _frontier_states(assignments: list[dict], anchor: int, *, entropy_stop: float,
                     maximum_bridge_steps: int) -> None:
    clear = [bool(row.get("selected_candidate_index") is not None
                  and row.get("target_probability", 0.0) >= CONFIRMED_PROBABILITY
                  and row.get("identity_entropy", math.inf) <= entropy_stop)
             for row in assignments]
    # Persist the verdict rather than leaving it implicit in ``track_state``.  Acquisition needs to
    # tell "identity is undecided here" (more counting can help) from "identity is decided but this
    # q is not connected to the trajectory" (more counting cannot), and ``track_state`` alone
    # collapses both into ``ambiguous``/``blocked``.
    for row, is_clear in zip(assignments, clear):
        row["local_identity_clear"] = is_clear
    for row in assignments:
        row["track_state"] = "blocked"
    if not assignments:
        return
    assignments[anchor]["track_state"] = "confirmed" if clear[anchor] else "ambiguous"
    if not clear[anchor]:
        return
    for direction in (-1, 1):
        index = anchor + direction
        frontier_open = True
        bridges = 0
        while 0 <= index < len(assignments):
            row = assignments[index]
            if frontier_open and clear[index]:
                row["track_state"] = "confirmed"
            elif frontier_open:
                row["track_state"] = "ambiguous" if row.get("selected_candidate_index") is not None else "missing"
                frontier_open = False
            elif bridges < maximum_bridge_steps and row.get("selected_candidate_index") is not None:
                row["track_state"] = "provisional"
                bridges += 1
            else:
                row["track_state"] = "blocked"
            index += direction


def _infer_unresolved_acoustic_origin(assignments: list[dict], origin: int,
                                      reduced_distance: list[float],
                                      observations: list[dict]) -> bool:
    """Mark an unfitted Gamma endpoint supported by a connected low-q acoustic trajectory."""
    if not assignments or not math.isclose(reduced_distance[origin], 0.0, abs_tol=1e-9):
        return False
    origin_row = assignments[origin]
    acoustic_index = origin_row.get("acoustic_candidate_index")
    if acoustic_index is not None:
        # This status represents the experimentally hidden endpoint, not a fitted low-energy mode.
        # A fitted candidate remains ordinary evidence even when it lies inside the resolution.
        return False

    support = []
    for direction in (-1, 1):
        index = origin + direction
        while 0 <= index < len(assignments) and len(support) < 8:
            row = assignments[index]
            candidate_index = row.get("acoustic_candidate_index")
            if candidate_index is None:
                break
            candidate = (row.get("candidates") or [])[candidate_index]
            probability = float((candidate.get("probabilities") or {}).get("acoustic", 0.0))
            if probability < CONFIRMED_PROBABILITY:
                break
            support.append((reduced_distance[index], _candidate_energy(candidate),
                            _candidate_sigma(candidate)))
            index += direction
    if len(support) < 3:
        return False
    x = np.asarray([point[0] for point in support], dtype=float)
    y = np.asarray([point[1] for point in support], dtype=float)
    sigma = np.asarray([point[2] for point in support], dtype=float)
    design = np.column_stack((np.ones_like(x), x))
    weights = 1.0 / np.square(np.maximum(sigma, 1e-6))
    precision = design.T @ (weights[:, None] * design)
    covariance = np.linalg.pinv(precision, hermitian=True)
    intercept, slope = covariance @ (design.T @ (weights * y))
    residual_rms = math.sqrt(float(np.average(np.square(y - design @ [intercept, slope]),
                                               weights=weights)))
    intercept_limit = max(0.5, 3.0 * math.sqrt(max(float(covariance[0, 0]), 0.0)))
    residual_limit = max(0.35, 2.0 * float(np.median(sigma)))
    if slope <= 0.0 or abs(intercept) > intercept_limit or residual_rms > residual_limit:
        return False

    origin_row.update({
        "track_state": "inferred_unresolved",
        "structural_endpoint_status": "inferred_unresolved",
        "is_reduced_q_origin": True,
        "target_delivery_eligible": False,
        "precision_coverage_eligible": False,
        "inference_support_points": len(support),
        "inferred_intercept_meV": float(intercept),
        "inferred_slope_meV_per_rlu": float(slope),
    })
    return True


def _segments(assignments: list[dict], branch: str, target_branch: str) -> list[dict]:
    segments: list[dict] = []
    current: dict | None = None
    for row in assignments:
        candidate_index = row.get(f"{branch}_candidate_index")
        candidates = row.get("candidates") or []
        state = row.get("track_state", "blocked")
        drawable_state = (state if branch == target_branch
                          else ("confirmed" if candidate_index is not None else "missing"))
        if candidate_index is None or drawable_state in ("missing", "ambiguous", "blocked"):
            current = None
            continue
        candidate = candidates[candidate_index]
        point = {
            "q": row.get("path_distance"), "hkl": list(row.get("q") or []),
            "energy_meV": _candidate_energy(candidate),
            "probability": float(candidate.get("probabilities", {}).get(branch, 0.0)),
            "candidate_ref": candidate.get("candidate_ref"),
        }
        if current is None or current["state"] != drawable_state:
            current = {"state": drawable_state, "points": []}
            segments.append(current)
        current["points"].append(point)
    return segments


def track_path(path: list[list[float]], observations: list[dict], *, period: float | None,
               hypotheses: list[dict], target_branch: str = "optic", previous: dict | None = None,
               assignment_entropy_stop_nats: float = 0.2,
               maximum_bridge_steps: int = 1,
               energy_domain_meV: (tuple[float, float] | list[float]
                                   | Iterable[Iterable[float]] | None) = None) -> dict:
    """Run Viterbi plus forward/backward marginals over an ordered objective path.

    ``observations`` has one row per path point and each row has an unordered ``candidates`` list.
    Missing lists are valid observations.  The result is entirely JSON-serialisable.
    """
    if target_branch not in {"optic", "acoustic"}:
        raise ValueError("tracked branch must be 'optic' or 'acoustic'")
    # The domain the "could be anywhere" baseline is uniform over (O30). Falls back to a fixed span
    # rather than to the observed candidates, whose spread would make the baseline move with the data
    # it is supposed to be measured against.
    domain = normalize_energy_domain(energy_domain_meV)
    if len(path) != len(observations):
        raise ValueError("path and observations must have the same length")
    points = [[float(value) for value in q] for q in path]
    previous_anchor = (previous or {}).get(
        "acquisition_seed_index", (previous or {}).get("anchor_index"))
    persisted_anchor = (isinstance(previous_anchor, int)
                        and 0 <= previous_anchor < len(points)
                        and (previous or {}).get("target_branch") == target_branch)
    acoustic_hypothesis = _hypothesis(hypotheses, "acoustic")
    anchor = (previous_anchor if persisted_anchor else
              anchor_index(points, period, target_branch=target_branch,
                           observations=observations,
                           acoustic_hypothesis=acoustic_hypothesis))
    spans = [max(q[axis] for q in points) - min(q[axis] for q in points)
             for axis in range(len(points[0]))]
    axis = int(np.argmax(spans)) if any(span > 0.0 for span in spans) else 0
    # The acquisition/identity anchor may sit away from the zone centre for a gapless branch.  Its
    # location must not reverse the physical sign of dE/d|q| on the zone-centre side of that anchor.
    zone_anchor = anchor_index(points, period)
    path_distance, radial = _path_geometry(points, zone_anchor)
    reduced_distance = list(radial)
    if period is not None and np.isfinite(period) and period > 0.0:
        reduced_distance = [abs(points[index][axis]
                                - round(points[index][axis] / period) * period)
                            for index in range(len(points))]
    optic_hypothesis = _hypothesis(hypotheses, "optic")
    acoustic_hypothesis = _hypothesis(hypotheses, "acoustic")
    candidates_by_q = [list(row.get("candidates") or []) for row in observations]
    null_windows_by_q = [list(row.get("null_windows") or []) for row in observations]
    states = [_states(len(candidates)) for candidates in candidates_by_q]
    if not states:
        return {"revision": 1, "target_branch": target_branch,
                "rival_branch": "acoustic" if target_branch == "optic" else "optic",
                "anchor_index": anchor,
                "acquisition_seed_index": anchor,
                "reduced_q_origin_index": zone_anchor,
                "energy_domain_meV": [list(interval) for interval in domain],
                "scorer_revision": BRANCH_TRACKING_SCORER_REVISION,
                "assignments": [], "tracks": [], "frontiers": {}}

    emissions = [[_emission(state, candidates_by_q[index],
                            reduced_distance=reduced_distance[index], period=period,
                            optic_hypothesis=optic_hypothesis,
                            acoustic_hypothesis=acoustic_hypothesis,
                            null_windows=null_windows_by_q[index],
                            energy_domain=domain)
                  for state in states[index]] for index in range(len(points))]
    transitions: list[np.ndarray] = []
    for index in range(1, len(points)):
        matrix = np.empty((len(states[index - 1]), len(states[index])), dtype=float)
        radial_delta = radial[index] - radial[index - 1]
        for old_index, old in enumerate(states[index - 1]):
            for new_index, new in enumerate(states[index]):
                matrix[old_index, new_index] = _transition(
                    old, new, candidates_by_q[index - 1], candidates_by_q[index], radial_delta,
                    optic_hypothesis, acoustic_hypothesis)
        transitions.append(matrix)

    alpha = [np.asarray(emissions[0], dtype=float)]
    viterbi = [np.asarray(emissions[0], dtype=float)]
    backpointers: list[np.ndarray] = []
    for index in range(1, len(points)):
        combined = alpha[-1][:, None] + transitions[index - 1]
        alpha.append(np.asarray(emissions[index])
                     + np.asarray([_logsumexp(combined[:, column])
                                   for column in range(combined.shape[1])]))
        vcombined = viterbi[-1][:, None] + transitions[index - 1]
        backpointers.append(np.argmax(vcombined, axis=0))
        viterbi.append(np.asarray(emissions[index]) + np.max(vcombined, axis=0))
    beta = [np.zeros(len(states[-1]), dtype=float)]
    for index in range(len(points) - 2, -1, -1):
        combined = transitions[index] + np.asarray(emissions[index + 1])[None, :] + beta[0][None, :]
        beta.insert(0, np.asarray([_logsumexp(row) for row in combined]))
    log_evidence = _logsumexp(alpha[-1])

    map_indices = [0] * len(points)
    map_indices[-1] = int(np.argmax(viterbi[-1]))
    for index in range(len(points) - 1, 0, -1):
        map_indices[index - 1] = int(backpointers[index - 1][map_indices[index]])

    assignments: list[dict] = []
    for index, candidates in enumerate(candidates_by_q):
        state_probabilities = np.exp(alpha[index] + beta[index] - log_evidence)
        total = float(state_probabilities.sum())
        if total > 0.0:
            state_probabilities /= total
        for candidate_index, candidate in enumerate(candidates):
            optic_probability = float(sum(prob for prob, state in zip(state_probabilities, states[index])
                                          if state.optic == candidate_index))
            acoustic_probability = float(sum(prob for prob, state in zip(state_probabilities, states[index])
                                             if state.acoustic == candidate_index))
            candidate["probabilities"] = {
                "optic": optic_probability, "acoustic": acoustic_probability,
                "new": max(0.0, 1.0 - optic_probability - acoustic_probability),
            }
            known_probability = optic_probability + acoustic_probability
            character = ("acoustic" if acoustic_probability >= optic_probability else "optic")
            candidate["association_probability"] = known_probability
            candidate["character_probability"] = (
                max(optic_probability, acoustic_probability) / known_probability
                if known_probability > 0.0 else 0.0)
            candidate["trajectory_identity"] = character if known_probability > 0.0 else None
        selected_state = states[index][map_indices[index]]
        selected_index = getattr(selected_state, target_branch)
        selected_probabilities = (candidates[selected_index].get("probabilities", {})
                                  if selected_index is not None else {"missing": 1.0})
        known_probability = sum(float(selected_probabilities.get(branch, 0.0))
                                for branch in ("optic", "acoustic"))
        selected_character = (max(("optic", "acoustic"),
                                  key=lambda branch: selected_probabilities.get(branch, 0.0))
                              if selected_index is not None and known_probability > 0.0 else None)
        assignments.append({
            "path_index": index, "q": points[index], "path_distance": path_distance[index],
            "candidates": candidates, "optic_candidate_index": selected_state.optic,
            "acoustic_candidate_index": selected_state.acoustic,
            "selected_candidate_index": selected_index,
            "target_probability": float(selected_probabilities.get(target_branch, 0.0)),
            "association_probability": known_probability,
            "character_probability": (
                float(selected_probabilities.get(target_branch, 0.0)) / known_probability
                if known_probability > 0.0 else 0.0),
            "trajectory_identity": selected_character,
            "assignment_entropy": _entropy(selected_probabilities),
            "identity_entropy": _identity_entropy(selected_probabilities),
        })

    _frontier_states(assignments, anchor, entropy_stop=float(assignment_entropy_stop_nats),
                     maximum_bridge_steps=max(0, int(maximum_bridge_steps)))
    inferred_origin = (target_branch == "acoustic"
                       and period is not None and np.isfinite(period) and period > 0.0
                       and _infer_unresolved_acoustic_origin(
                           assignments, zone_anchor, reduced_distance, observations))
    old_revision = int((previous or {}).get("revision", 0))
    old_assignments = (previous or {}).get("assignments") or []
    reassignments = []
    for row, old in zip(assignments, old_assignments):
        before = old.get("selected_candidate_ref")
        selected = row.get("selected_candidate_index")
        after = ((row.get("candidates") or [])[selected].get("candidate_ref")
                 if selected is not None else None)
        row["selected_candidate_ref"] = after
        if before != after:
            reassignments.append({"path_index": row["path_index"], "from": before, "to": after})
    for row in assignments[len(old_assignments):]:
        selected = row.get("selected_candidate_index")
        row["selected_candidate_ref"] = ((row.get("candidates") or [])[selected].get("candidate_ref")
                                           if selected is not None else None)

    confirmed = [row["path_index"] for row in assignments if row["track_state"] == "confirmed"]
    blocked = [row["path_index"] for row in assignments if row["track_state"] in ("ambiguous", "missing")]
    revision = old_revision + 1
    return {
        "revision": revision, "target_branch": target_branch,
        "rival_branch": "acoustic" if target_branch == "optic" else "optic",
        "anchor_index": anchor,
        "acquisition_seed_index": anchor,
        "reduced_q_origin_index": zone_anchor,
        "energy_domain_meV": [list(interval) for interval in domain],
        "scorer_revision": BRANCH_TRACKING_SCORER_REVISION,
        "inferred_endpoint_indices": [zone_anchor] if inferred_origin else [],
        "assignments": assignments,
        "tracks": [
            {"branch_id": branch, "role": ("target" if branch == target_branch else "rival"),
             "revision": revision,
             "label": (None if branch == target_branch else
                       f"{branch} identified; retained as non-target context"),
             "segments": _segments(assignments, branch, target_branch)}
            for branch in (target_branch, "acoustic" if target_branch == "optic" else "optic")
        ],
        "frontiers": {"confirmed_indices": confirmed, "blocked_indices": blocked,
                      "frontier_blocked": bool(blocked)},
        "reassignments": reassignments,
    }
