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


@dataclass(frozen=True)
class _State:
    optic: int | None
    acoustic: int | None


def anchor_index(path: Iterable[Iterable[float]], period: float | None) -> int:
    """Return the path point nearest reduced q=0, with path order as the tie-breaker."""
    points = [tuple(float(value) for value in q) for q in path]
    if not points:
        return 0
    spans = [max(q[axis] for q in points) - min(q[axis] for q in points)
             for axis in range(len(points[0]))]
    axis = int(np.argmax(spans)) if any(span > 0.0 for span in spans) else 0
    if period is None or not np.isfinite(period) or period <= 0.0:
        return 0
    reduced = [abs(q[axis] - round(q[axis] / period) * period) for q in points]
    return min(range(len(points)), key=lambda index: (reduced[index], index))


def _logsumexp(values) -> float:
    values = np.asarray(values, dtype=float)
    if values.size == 0:
        return -math.inf
    maximum = float(np.max(values))
    if not np.isfinite(maximum):
        return maximum
    return maximum + math.log(float(np.exp(values - maximum).sum()))


def _normal_logpdf(value: float, mean: float, sigma: float) -> float:
    sigma = max(float(sigma), 1e-6)
    z = (float(value) - float(mean)) / sigma
    return -0.5 * z * z - math.log(sigma)


def _normal_compatibility(value: float, mean: float, sigma: float) -> float:
    """Dimensionless hypothesis compatibility for a discrete identity decision.

    The normalization term of a probability *density* must not become a prior against a broad but
    explicitly declared branch: doing so made a 3 meV-wide ZC prior permanently favor ``new`` over
    an observed peak exactly at its mean, regardless of accumulated counts.
    """
    sigma = max(float(sigma), 1e-6)
    z = (float(value) - float(mean)) / sigma
    return -0.5 * z * z


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


def _states(n_candidates: int) -> list[_State]:
    indices = [None, *range(n_candidates)]
    return [_State(optic, acoustic) for optic in indices for acoustic in indices
            if optic is None or acoustic is None or optic != acoustic]


def _emission(state: _State, candidates: list[dict], *, at_anchor: bool,
              optic_hypothesis: dict, acoustic_hypothesis: dict) -> float:
    assigned = {index for index in (state.optic, state.acoustic) if index is not None}
    score = (len(candidates) - len(assigned)) * UNASSIGNED_LOG_PENALTY
    if state.optic is None:
        score += MISSING_LOG_PENALTY
    if state.acoustic is None:
        score += MISSING_LOG_PENALTY
    if not at_anchor:
        return score
    for branch, index, hypothesis, default in (
            ("optic", state.optic, optic_hypothesis, (6.0, 3.0)),
            ("acoustic", state.acoustic, acoustic_hypothesis, (0.0, 0.2))):
        if index is None:
            continue
        mean, sigma = _prior_pair(hypothesis, "zc_energy", default)
        candidate = candidates[index]
        combined = math.sqrt(sigma * sigma + _candidate_sigma(candidate) ** 2)
        score += _normal_compatibility(_candidate_energy(candidate), mean, combined)
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
        score += _normal_logpdf(
            _candidate_energy(new) - _candidate_energy(old), expected_change, sigma)
    return score


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


def _segments(assignments: list[dict], branch: str) -> list[dict]:
    segments: list[dict] = []
    current: dict | None = None
    for row in assignments:
        candidate_index = row.get(f"{branch}_candidate_index")
        candidates = row.get("candidates") or []
        state = row.get("track_state", "blocked")
        drawable_state = state if branch == "optic" else ("confirmed" if candidate_index is not None else "missing")
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
               maximum_bridge_steps: int = 1) -> dict:
    """Run Viterbi plus forward/backward marginals over an ordered objective path.

    ``observations`` has one row per path point and each row has an unordered ``candidates`` list.
    Missing lists are valid observations.  The result is entirely JSON-serialisable.
    """
    if target_branch != "optic":
        raise ValueError("joint tracked-branch assignment currently requires target_branch='optic'")
    if len(path) != len(observations):
        raise ValueError("path and observations must have the same length")
    points = [[float(value) for value in q] for q in path]
    anchor = anchor_index(points, period)
    path_distance, radial = _path_geometry(points, anchor)
    optic_hypothesis = _hypothesis(hypotheses, "optic")
    acoustic_hypothesis = _hypothesis(hypotheses, "acoustic")
    candidates_by_q = [list(row.get("candidates") or []) for row in observations]
    states = [_states(len(candidates)) for candidates in candidates_by_q]
    if not states:
        return {"revision": 1, "target_branch": target_branch, "anchor_index": anchor,
                "assignments": [], "tracks": [], "frontiers": {}}

    emissions = [[_emission(state, candidates_by_q[index], at_anchor=index == anchor,
                            optic_hypothesis=optic_hypothesis,
                            acoustic_hypothesis=acoustic_hypothesis)
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
        selected_state = states[index][map_indices[index]]
        selected_index = selected_state.optic
        selected_probabilities = (candidates[selected_index].get("probabilities", {})
                                  if selected_index is not None else {"missing": 1.0})
        assignments.append({
            "path_index": index, "q": points[index], "path_distance": path_distance[index],
            "candidates": candidates, "optic_candidate_index": selected_state.optic,
            "acoustic_candidate_index": selected_state.acoustic,
            "selected_candidate_index": selected_index,
            "target_probability": float(selected_probabilities.get("optic", 0.0)),
            "assignment_entropy": _entropy(selected_probabilities),
            "identity_entropy": _identity_entropy(selected_probabilities),
        })

    _frontier_states(assignments, anchor, entropy_stop=float(assignment_entropy_stop_nats),
                     maximum_bridge_steps=max(0, int(maximum_bridge_steps)))
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
        "revision": revision, "target_branch": target_branch, "anchor_index": anchor,
        "assignments": assignments,
        "tracks": [
            {"branch_id": "optic", "role": "target", "revision": revision,
             "segments": _segments(assignments, "optic")},
            {"branch_id": "acoustic", "role": "rival", "revision": revision,
             "label": "acoustic identified; retained as non-target context",
             "segments": _segments(assignments, "acoustic")},
        ],
        "frontiers": {"confirmed_indices": confirmed, "blocked_indices": blocked,
                      "frontier_blocked": bool(blocked)},
        "reassignments": reassignments,
    }
