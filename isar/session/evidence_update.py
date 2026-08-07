"""Evidence-update cluster (P2.6c) — everything that turns fit outcomes into persisted
scientific evidence, extracted verbatim from :mod:`isar.session.driver`.

Three families, in original driver order:

* cross-q persistence — ``_store_branch_models``, ``_store_branch_beliefs``,
  ``_update_hypothesis_mismatch`` write the branch/belief/mismatch views onto ``card.state``
  for the Qt-free drive and viewers;
* branch identity — candidate materialisation, mode selection, the connected tracker
  (``_materialize_branch_candidates`` … ``_apply_tracked_branch``) and prior rebuilds
  (``_rebuild_tracked_prior``, ``_rebuild_pooled_prior``);
* ``_resolve_target_projections`` — the post-triage ADR-019 target projection per pooled master.

``_master_prior_admissible`` is the SINGLE admission source for independent fit evidence
entering the running dispersion prior; no other gate may admit a master's point.

The driver keeps its two evidence-tail sequences (``_apply_and_finish`` and
``_run_master_certification``) as explicit orchestration and calls into here qualified — the
two sequences differ materially (physical-row restore, ``last_run`` shape, staged publication)
and unifying them would risk the byte-identity contract (P2.6c D2).
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path

import numpy as np

from isar.schema.steering import AUXILIARY_TRACK_NODE, CANDIDATE_NOT_DETECTED, realized_outcome_for
from isar.session import branch_assignment as BA
from isar.session import branch_model, branch_tracking, predictive_artifact, session_prior
from isar.session.master_delivery import _persist_content_addressed
from isar.session.scan_delivery import (
    FULL_TIERS, _campaign_hypotheses, _campaign_selector, _campaign_target, _dispersion_period,
    _refresh_interval_semantics, _refresh_probability_semantics,
)
from isar.session.state import ScanEntry, SessionState, canonical_hash

log = logging.getLogger(__name__)


def _store_branch_models(state, t_round: float) -> None:
    """Build the cross-q branch(es) from the (now complete) dispersion prior and persist them into
    ``state.branch_models`` — one per distinct ``(sample, t_bin)`` — so the DRIVE (a separate,
    crash-prone process that cannot import :mod:`isar.session`) reads the prediction durably from
    ``card.state.json``. Replace-by-key so a re-run refreshes rather than accretes; best-effort (a
    build failure never fails the batch). ``build_branch`` returns ``None`` below ``MIN_ANCHORS``."""
    points = state.dispersion_prior or []
    keys: list[tuple] = []
    for p in points:
        sample = p.get("sample")
        tb = p.get("t_bin")
        if tb is None and p.get("T") is not None:
            tb = round(float(p["T"]) / t_round) * t_round if t_round else float(p["T"])
        if sample is None or tb is None:
            continue
        key = (sample, float(tb))
        if key not in keys:
            keys.append(key)
    for sample, tb in keys:
        try:
            bm = branch_model.build_branch(points, sample=sample, t_bin=tb)
        except Exception:   # noqa: BLE001
            bm = None
        if bm is None:
            continue
        bid = bm.get("branch_id", branch_model.BRANCH_ID)
        state.branch_models = [
            m for m in (state.branch_models or [])
            if not (m.get("sample") == sample
                    and abs(float(m.get("t_bin", -1e18)) - float(tb)) < 1e-9
                    and m.get("branch_id") == bid)]
        state.branch_models.append(bm)


def _store_branch_beliefs(state, t_round: float, *, period: float | None,
                          hypotheses: list[dict] | None = None) -> None:
    """Persist the data/prior ingredients of every branch belief for the Qt-free drive/viewers."""
    if period is None:
        state.branch_belief = []
        state.branch_belief_models = []
        return
    points = state.dispersion_prior or []
    keys: list[tuple[str, float]] = []
    for point in points:
        sample = point.get("sample")
        tb = point.get("t_bin", point.get("T"))
        if sample is None or tb is None:
            continue
        tb = round(float(tb) / t_round) * t_round if t_round else float(tb)
        key = (str(sample), float(tb))
        if key not in keys:
            keys.append(key)
    if not keys and hypotheses:
        # A prior-only snapshot is useful but has no sample/T identity until the first scan lands.
        state.branch_belief = []
        state.branch_belief_models = []
        return
    rows: list[dict] = []
    for sample, tb in keys:
        beliefs = BA.build_beliefs(points, sample=sample, t_bin=tb, period=period,
                                   t_round=t_round, hypotheses=hypotheses)
        for branch_id, belief in sorted(beliefs.items()):
            anchors = [{"u": float(u), "omega0": float(y), "weight": float(w)}
                       for u, y, w in zip(belief.us, belief.ys, belief.ws)]
            rows.append({"sample": sample, "t_bin": tb, "branch_id": branch_id,
                         "period": float(period), "anchors": anchors,
                         "n_anchors": belief.n_anchors,
                         "parameter_prior": dict(belief.parameter_prior or {}),
                         "label_probabilities": {}})
    state.branch_belief_models = rows
    target = (getattr(state, "hypothesis_snapshot", None) or {}).get("target_hypothesis")
    point_rows: list[dict] = []
    for point in sorted(points, key=lambda item: (
            str(item.get("sample", "")), float(item.get("t_bin", item.get("T", 0.0))),
            tuple(float(value) for value in (item.get("q") or ())),
            str(item.get("scan_id", "")))):
        branch_id = str(point.get("branch_id") or BA.ACOUSTIC)
        probability = min(max(float(point.get("weight", 1.0)), 0.0), 1.0)
        complete = point.get("branch_posterior")
        probabilities = None
        if isinstance(complete, dict):
            usable = {}
            for key, value in complete.items():
                try:
                    numeric = float(value)
                except (TypeError, ValueError):
                    continue
                if np.isfinite(numeric) and numeric >= 0.0:
                    usable[str(key)] = numeric
            total = sum(usable.values())
            if usable and total > 0.0:
                probabilities = {key: value / total for key, value in usable.items()}
        if probabilities is None:
            # Compatibility for points persisted before complete vectors were additive.
            probabilities = {branch_id: probability, BA.NEW: 1.0 - probability}
        entropy = -sum(value * math.log(value) for value in probabilities.values() if value > 0.0)
        point_rows.append({
            "sample": point.get("sample"), "t_bin": point.get("t_bin", point.get("T")),
            "q": list(point.get("q") or []), "omega0": point.get("omega0"),
            "sigma": BA.SIGMA_SLACK / math.sqrt(max(probability, 1e-9)),
            "branch_id": branch_id, "label_probabilities": probabilities,
            "assignment_entropy": entropy,
            "p_is_target": float(probabilities.get(target, 0.0)) if target is not None else 0.0,
            "resolvable": None, "scan_id": point.get("scan_id")})
    state.branch_belief = point_rows


def _update_hypothesis_mismatch(state, t_round: float, *, period: float | None,
                                hypotheses: list[dict] | None = None) -> None:
    """Persist O11's prior-vs-data-only posterior-predictive mismatch assessment."""
    snapshot = getattr(state, "hypothesis_snapshot", None) or {}
    target = snapshot.get("target_hypothesis")
    policy = snapshot.get("steering") or {}
    threshold = float(policy.get("mismatch_z_threshold", 3.0))
    hypothesis = next((h for h in (hypotheses or []) if h.get("name") == target), None)
    if period is None or hypothesis is None:
        state.hypothesis_mismatch = {}
        return
    target_points = [p for p in (state.dispersion_prior or []) if p.get("branch_id") == target]
    groups: dict[tuple[str, float], list[dict]] = {}
    for point in target_points:
        sample_value = point.get("sample")
        tb_value = point.get("t_bin", point.get("T"))
        if sample_value is None or tb_value is None:
            continue
        tb = round(float(tb_value) / t_round) * t_round if t_round else float(tb_value)
        groups.setdefault((str(sample_value), float(tb)), []).append(point)
    eligible = {key: rows for key, rows in groups.items() if len(rows) >= 2}
    if not eligible:
        state.hypothesis_mismatch = {
            "active": False, "evaluated": False, "target_hypothesis": target,
            "comparison": "prior_vs_data_only_posterior_predictive",
            "reason": "fewer_than_two_real_anchors", "threshold_z": threshold,
            "fingerprint": snapshot.get("fingerprint")}
        return
    sample, tb = max(sorted(eligible), key=lambda key: (len(eligible[key]), key))
    points = sorted(eligible[(sample, tb)], key=lambda point: (
        tuple(float(value) for value in (point.get("q") or ())),
        str(point.get("scan_id", ""))))
    belief = BA.build_beliefs(points, sample=sample, t_bin=tb, period=period,
                              t_round=t_round, hypotheses=hypotheses).get(target)
    prior_only = BA.BranchBelief(target, np.array([]), np.array([]), np.array([]),
                                 parameter_prior=hypothesis)
    comparisons = []
    for u in sorted(set(float(value) for value in belief.us)):
        prior_mu, prior_sigma = prior_only.predict(u)
        data_mu, data_sigma = belief.predict_data_only(u)
        denom = math.sqrt(prior_sigma ** 2 + data_sigma ** 2)
        z = abs(prior_mu - data_mu) / max(denom, 1e-12)
        comparisons.append({"u": u, "prior_mean": prior_mu, "prior_sigma": prior_sigma,
                            "data_mean": data_mu, "data_sigma": data_sigma, "z": z})
    worst = max(comparisons, key=lambda row: row["z"])
    active = bool(worst["z"] >= threshold)
    state.hypothesis_mismatch = {
        "active": active, "evaluated": True, "target_hypothesis": target,
        "comparison": "prior_vs_data_only_posterior_predictive",
        "max_z": worst["z"], "threshold_z": threshold,
        "evaluated_points": len(comparisons), "worst": worst,
        "sample": sample, "t_bin": tb,
        "point_ids": [str(point.get("scan_id", "")) for point in points],
        "fingerprint": snapshot.get("fingerprint"),
        "message": (f"declared {target} hypothesis disagrees with data-only branch prediction"
                    if active else "declared hypothesis remains compatible with data-only prediction")}


def _tracked_selector(card) -> bool:
    return _campaign_selector(card)[0] == "tracked_branch"


def _mode_candidates(scan_id: str, detector: str, outcome: dict) -> list[dict]:
    """Materialise every fitted mode with its own uncertainty and provenance.

    Full profile/bootstrap/Bayes intervals currently belong to the fit's scalar primary mode. They
    are copied only to that matching candidate; a secondary receives its own HESSE interval, never
    the primary mode's interval (which would be numerically tidy and scientifically wrong).
    """
    # A tracking revision may have rewritten the backwards-compatible scalar omega0 to a secondary
    # mode. The previously materialised table is immutable fit evidence and remembers which mode
    # actually owned the fitter's profile/bootstrap/Bayes intervals. Reuse it on carry/no-op reruns;
    # a genuine refit replaces the whole outcome and arrives without this table.
    stored = outcome.get("branch_candidates")
    if isinstance(stored, list) and stored:
        rows = []
        for candidate in stored:
            if not isinstance(candidate, dict):
                continue
            row = dict(candidate)
            # Assignment probabilities are derived state, never fit evidence.  Keeping them in the
            # next Viterbi input would let yesterday's assignment masquerade as a new local label.
            for field in ("probabilities", "association_probability",
                          "character_probability", "trajectory_identity"):
                row.pop(field, None)
            rows.append(row)
        if rows:
            return rows
    modes = list(outcome.get("modes") or [])
    if not modes and outcome.get("omega0") is not None:
        modes = [{"omega0": outcome.get("omega0"), "omega0_err": outcome.get("omega0_err"),
                  "gamma": outcome.get("gamma"), "amplitude": None}]
    primary = outcome.get("omega0")
    rows = []
    for index, mode in enumerate(modes):
        energy = mode.get("omega0")
        if energy is None:
            continue
        try:
            energy = float(energy)
        except (TypeError, ValueError):
            continue
        if not np.isfinite(energy):
            continue
        error = mode.get("omega0_err")
        try:
            error = float(error)
        except (TypeError, ValueError):
            error = None
        if error is not None and (not np.isfinite(error) or error <= 0.0):
            error = None
        is_primary = primary is not None and abs(float(primary) - energy) <= 1e-6
        hesse = ([energy - error, energy + error] if error is not None else None)
        row = {
            "candidate_ref": f"{scan_id}:{detector}:mode:{index}", "source_scan_id": scan_id,
            "detector": detector, "mode_index": index, "energy_meV": energy,
            "omega0_err": error, "sigma_meV": error, "gamma": mode.get("gamma"),
            "gamma_err": mode.get("gamma_err"), "amplitude": mode.get("amplitude"),
            "interval": (list(outcome.get("interval")) if is_primary and outcome.get("interval") else None),
            "profile_interval": (list(outcome.get("omega0_profile"))
                                 if is_primary and outcome.get("omega0_profile") else None),
            "hesse_interval": (list(outcome.get("hesse_interval"))
                               if is_primary and outcome.get("hesse_interval") else hesse),
            "bayes_interval": (list(outcome.get("bayes_interval"))
                               if is_primary and outcome.get("bayes_interval") else None),
            "reliable": bool(outcome.get("interval_reliable", True)),
            "pooled": bool(outcome.get("pooled")), "n_members": outcome.get("n_members"),
            "fit_kind": outcome.get("fit_kind"), "model": outcome.get("best_label"),
            "verdict": outcome.get("verdict"), "modality": outcome.get("modality"),
        }
        rows.append(row)
    return rows


def _path_index(q: list, path: list[list[float]], tolerance: float = 1e-5) -> int | None:
    if not q or not path:
        return None
    distances = [math.dist([float(value) for value in q], point) for point in path]
    index = int(np.argmin(distances))
    return index if distances[index] <= tolerance else None


# A clean null answers "no rival in this window" but does not measure detection power, so its evidence
# of absence is real but not certain. Until PR3 computes a predicted detection power per probe, a
# trustworthy clean null relaxes the missing-branch pull by this bounded, conservative fraction rather
# than fully cancelling it -- the null moves the posterior without ever claiming the branch cannot exist.
CLEAN_NULL_DETECTION_SENSITIVITY = 0.5


def _clean_null_window(outcome: dict) -> tuple[float, float, float] | None:
    """The (lo, hi, sensitivity) negative-evidence window for a trustworthy clean-null auxiliary probe.

    A clean null searched [search_energy_lo, search_energy_hi] and found no mode there; the tracker
    reads this as "the branch predicted in this window is confirmed absent, not merely unmeasured"
    (:func:`branch_tracking._null_relaxation`). Returns ``None`` for anything that is not a trustworthy
    clean null -- a broken analysis, a detection, or a probe with no recorded window -- so a failure can
    never masquerade as evidence of absence.
    """
    if outcome.get("evidence_sink") != AUXILIARY_TRACK_NODE:
        return None
    if realized_outcome_for(outcome.get("disposition"),
                            has_candidate=bool(outcome.get("branch_candidates"))) != CANDIDATE_NOT_DETECTED:
        return None
    lo, hi = outcome.get("search_energy_lo"), outcome.get("search_energy_hi")
    try:
        lo, hi = float(lo), float(hi)
    except (TypeError, ValueError):
        return None
    if not (np.isfinite(lo) and np.isfinite(hi) and lo < hi):
        return None
    return (lo, hi, CLEAN_NULL_DETECTION_SENSITIVITY)


def _tracked_authorities(session: SessionState, path: list[list[float]]) -> tuple[list[dict], dict]:
    """Return one authoritative fit per path point and all outcome references.

    Only a published pooled master may be an identity authority.  Physical fits remain in ``refs`` so
    the GUI can display their candidate tables, but they never fill a gap in the authoritative track
    and never vote on a master assignment.  Internal consistency ranks masters here rather than
    excluding them — an inconsistent pool is still fitted evidence of *which modes exist*, and the
    gates that consistency does own live elsewhere (see the ``pool_consistent`` comment below).

    A scan acquired as an ``auxiliary_track_node`` probe is the one exception, and a deliberately
    narrow one.  It was fired to decide *which mode* a candidate is, not to deliver a better ω₀, so it
    is never pooled and never certification-eligible; its fitted modes join the observation at its path
    index as extra candidates tagged ``evidence_source="auxiliary_probe"``.  It can therefore change the
    identity assigned at that point and can never supply the point's ω₀ or become its ``authority``.
    It merges only where a pooled master already holds the index: alone it would be the *sole* identity
    evidence at that point, which is precisely what the rule above exists to prevent.
    """
    by_index: dict[int, list[tuple]] = {}
    auxiliary_by_index: dict[int, list[dict]] = {}
    null_windows_by_index: dict[int, list[tuple]] = {}
    refs: dict[str, tuple] = {}
    for order, entry in enumerate(session.scans.values()):
        for detector, outcome in entry.detectors.items():
            candidates = _mode_candidates(entry.scan_id, detector, outcome)
            outcome["branch_candidates"] = candidates or None
            key = f"{entry.scan_id}:{detector}"
            refs[key] = (entry, detector, outcome)
            index = _path_index(outcome.get("q") or [], path)
            # A clean null from an auxiliary probe fits no mode, so it never becomes a candidate; it is
            # negative evidence that the branch predicted in its searched window is absent, carried to
            # the tracker as a ``null_window`` that relaxes (never forces) that branch's missing pull.
            null_window = _clean_null_window(outcome)
            if index is not None and null_window is not None:
                null_windows_by_index.setdefault(index, []).append(null_window)
                continue
            if (index is not None and candidates
                    and outcome.get("evidence_sink") == AUXILIARY_TRACK_NODE):
                for candidate in candidates:
                    tagged = dict(candidate)
                    tagged["evidence_source"] = "auxiliary_probe"
                    try:
                        search_window = branch_tracking.normalize_energy_domain(
                            (outcome.get("search_energy_lo"), outcome.get("search_energy_hi")))
                    except ValueError:
                        search_window = ()
                    if search_window:
                        tagged["authorized_energy_window_meV"] = list(search_window[0])
                    auxiliary_by_index.setdefault(index, []).append(tagged)
                continue
            if index is not None and candidates and outcome.get("pooled"):
                exposure = outcome.get("effective_exposure")
                try:
                    exposure = float(exposure)
                except (TypeError, ValueError):
                    exposure = 0.0
                # A ``pool_consistent is False`` master's members disagree on ω₀, but the acoustic mode
                # is still fitted — so its candidate is valid IDENTITY/connectivity evidence and must
                # not be dropped. Dropping it emptied the q's observation, broke the tracked trajectory,
                # and cascaded every confirmed downstream point to ``blocked`` (AP8). It stays out of
                # the certifiable interval (stopping.py ``_master_hw``) and the dispersion prior
                # (``_rebuild_*_prior``) independently, so keeping it here cannot certify a bad ω₀ or
                # pollute the prior. When a q holds both a consistent and an inconsistent master the
                # rank prefers the consistent one rather than excluding the other.
                consistent = outcome.get("pool_consistent") is not False
                rank = (int(consistent), int(outcome.get("n_members") or 0), exposure, order)
                by_index.setdefault(index, []).append((rank, key, candidates))
    observations = []
    for index in range(len(path)):
        choices = by_index.get(index) or []
        null_windows = null_windows_by_index.get(index) or []
        if not choices:
            # Any auxiliary probe at this index is deliberately dropped: see the docstring. It stays
            # in ``refs`` for display and its beliefs update elsewhere, but it does not become the
            # identity evidence for a point no master has measured. A clean-null probe is the one
            # exception that leaves a trace here: its negative evidence rides on ``null_windows``.
            observation = {"candidates": [], "authority": None}
        else:
            _rank, key, candidates = max(choices, key=lambda item: item[0])
            _entry, _detector, outcome = refs[key]
            merged = [dict(candidate) for candidate in candidates]
            merged.extend(auxiliary_by_index.get(index) or [])
            observation = {"candidates": merged, "authority": key,
                           "gauss_fwhm": (outcome.get("gauss_fwhm")
                                          or (outcome.get("calib") or {}).get("gauss_fwhm"))}
        # Only present when a clean null was recorded here: a campaign that never used the auxiliary
        # channel must produce observations byte-identical to the pre-channel ones, so its identity
        # signature is unperturbed.
        if null_windows:
            observation["null_windows"] = null_windows
        observations.append(observation)
    return observations, refs


def _tracked_authority_signature(
        observations: list[dict], *, target_branch: str, period: float | None,
        hypotheses: list[dict], entropy_stop: float, maximum_bridge_steps: int,
        energy_domain_meV=None,
        scorer_revision: int = branch_tracking.BRANCH_TRACKING_SCORER_REVISION,
) -> str:
    """Stable identity input; derived probabilities are deliberately excluded."""
    authorities = []
    for observation in observations:
        candidates = []
        for candidate in observation.get("candidates") or []:
            fitted = dict(candidate)
            for field in ("probabilities", "association_probability",
                          "character_probability", "trajectory_identity"):
                fitted.pop(field, None)
            candidates.append(fitted)
        authority = {
            "authority": observation.get("authority"),
            "gauss_fwhm": observation.get("gauss_fwhm"),
            "candidates": candidates,
        }
        # Negative evidence is part of the identity basis: a new clean null changes the question being
        # asked at this q, so an unchanged basis (Increment C) cannot re-offer the same probe. Added only
        # when present, so a campaign with no probes hashes byte-identically to the pre-channel signature.
        if observation.get("null_windows"):
            authority["null_windows"] = observation["null_windows"]
        authorities.append(authority)
    return canonical_hash({
        "authorities": authorities, "target_branch": target_branch,
        "period": period, "hypotheses": hypotheses,
        "assignment_entropy_stop_nats": float(entropy_stop),
        "maximum_bridge_steps": int(maximum_bridge_steps),
        "energy_domain_meV": [
            list(interval) for interval in branch_tracking.normalize_energy_domain(energy_domain_meV)],
        "scorer_revision": int(scorer_revision),
    })


def _materialize_branch_candidates(session: SessionState) -> None:
    """Persist unordered fitted-mode evidence independently of target delivery.

    Local branch triage may deliberately clear the scalar omega0.  Keeping the immutable candidate
    table means the viewer and a later connected tracker can still see what was actually fitted.
    """
    for entry in session.scans.values():
        for detector, outcome in entry.detectors.items():
            candidates = _mode_candidates(entry.scan_id, detector, outcome)
            outcome["branch_candidates"] = candidates or None


AUXILIARY_EVIDENCE_SOURCE = "auxiliary_probe"


def _is_auxiliary_candidate(candidate: dict) -> bool:
    """Whether this fitted mode came from an auxiliary probe rather than the point's own master.

    The tag is applied in :func:`_tracked_authorities` when the probe's modes are merged onto the
    canonical observation, and absence of it means canonical.
    """
    return (candidate or {}).get("evidence_source") == AUXILIARY_EVIDENCE_SOURCE


def _select_mode(outcome: dict, candidate: dict, candidate_index: int, assignment: dict,
                 revision: int, target_branch: str = "optic") -> None:
    if _is_auxiliary_candidate(candidate):
        # Defence in depth behind _apply_tracked_branch's check. An auxiliary probe is never pooled
        # and never certification-eligible, so copying its energy and intervals onto a pooled master
        # builds a row that certifies the target from exposure the target window never spent.
        raise ValueError(
            f"auxiliary candidate {candidate.get('candidate_ref')!r} cannot supply a target "
            "delivery; withhold the delivery instead (see _withhold_tracked_delivery)")
    outcome["selected_candidate_index"] = int(candidate_index)
    outcome["track_state"] = assignment.get("track_state")
    outcome["track_revision"] = int(revision)
    outcome["candidate_state"] = assignment.get("track_state")
    outcome["candidate_id"] = candidate.get("candidate_ref")
    outcome["omega0"] = candidate.get("energy_meV")
    outcome["omega0_err"] = candidate.get("omega0_err")
    outcome["gamma"] = candidate.get("gamma")
    outcome["interval"] = candidate.get("interval")
    outcome["omega0_profile"] = candidate.get("profile_interval")
    outcome["hesse_interval"] = candidate.get("hesse_interval")
    outcome["bayes_interval"] = candidate.get("bayes_interval")
    outcome["interval_reliable"] = bool(candidate.get("reliable", False))
    probabilities = dict(candidate.get("probabilities") or {})
    outcome["branch_id"] = target_branch
    outcome["branch_posterior"] = probabilities
    outcome["branch_weight"] = float(probabilities.get(target_branch, 0.0))
    outcome["assignment_entropy"] = assignment.get("assignment_entropy")
    _carry_local_identity(outcome, assignment)
    outcome["structural_endpoint_status"] = assignment.get("structural_endpoint_status")
    outcome["is_reduced_q_origin"] = bool(assignment.get("is_reduced_q_origin", False))
    # Auxiliary withholding is a temporary delivery gate, not a durable property of the q. A later
    # canonical selection can immediately reuse the fit's preserved model delivery without a refit.
    outcome["target_delivery_eligible"] = True
    outcome["identity_branch_id"] = None
    outcome["delivery_withheld_reason"] = None
    _refresh_interval_semantics(outcome)
    outcome["probability_semantics"] = _refresh_probability_semantics(outcome)


def _carry_local_identity(outcome: dict, assignment: dict) -> None:
    """Carry the tracker's local identity verdict onto the outcome, selected or not.

    These survive :func:`_clear_tracked_selection` on purpose.  That helper withdraws the *scalar
    selection* so nothing stale is certifiable, but acquisition needs the local identity evidence
    precisely when the selection is absent — that is how it tells an undecided label (count more)
    from a decided label behind a path gap (counting cannot help).
    """
    outcome["target_probability"] = assignment.get("target_probability")
    outcome["identity_entropy"] = assignment.get("identity_entropy")
    clear = assignment.get("local_identity_clear")
    outcome["local_identity_clear"] = None if clear is None else bool(clear)


def _clear_tracked_selection(outcome: dict, assignment: dict, revision: int) -> None:
    """Persist a missing/ambiguous target without leaving an old scalar selection certifiable."""
    outcome["selected_candidate_index"] = None
    outcome["track_state"] = assignment.get("track_state")
    outcome["track_revision"] = int(revision)
    outcome["candidate_state"] = assignment.get("track_state")
    outcome["candidate_id"] = None
    outcome["branch_id"] = None
    outcome["branch_posterior"] = None
    outcome["branch_weight"] = None
    outcome["assignment_entropy"] = assignment.get("assignment_entropy")
    _carry_local_identity(outcome, assignment)
    outcome["structural_endpoint_status"] = assignment.get("structural_endpoint_status")
    outcome["is_reduced_q_origin"] = bool(assignment.get("is_reduced_q_origin", False))
    # A newly ambiguous/missing assignment also withdraws any earlier auxiliary-only identity
    # conclusion. Keep a pre-existing delivery gate closed until a canonical selection explicitly
    # re-enables it, but do not keep claiming a branch identity the tracker no longer confirms.
    outcome["identity_branch_id"] = None
    outcome["delivery_withheld_reason"] = None
    # The immutable branch_candidates table remains the complete fit evidence and can restore these
    # mode-specific values after a later reassignment.  The scalar compatibility view must abstain.
    for field in ("omega0", "omega0_err", "gamma", "interval", "omega0_profile",
                  "hesse_interval", "bayes_interval"):
        outcome[field] = None
    outcome["interval_reliable"] = False
    _refresh_interval_semantics(outcome)
    outcome["probability_semantics"] = _refresh_probability_semantics(outcome)


def _withhold_tracked_delivery(outcome: dict, candidate: dict, assignment: dict, revision: int,
                               target_branch: str) -> None:
    """Record a confirmed identity whose only supporting mode came from an auxiliary probe.

    This is neither a selection nor an ambiguity. The tracker reached a conclusion — that is what the
    probe was fired to buy, and discarding it would waste the beam time — but the mode it concluded
    from lives in a scan that is never pooled and never certification-eligible. So the *answer* is
    kept and the *number* is withheld: this point has no canonical measurement of the branch it has
    now identified, and the honest next action is an ordinary target scan at that energy.

    ``branch_id`` is deliberately NOT set. On a master that field means "this row supplies the target
    delivery", and every reader predating this state would take it at face value. The conclusion is
    carried by ``identity_branch_id`` instead, alongside the local-identity fields acquisition already
    reads when a selection is absent.
    """
    _clear_tracked_selection(outcome, assignment, revision)
    outcome["target_delivery_eligible"] = False
    outcome["identity_branch_id"] = target_branch
    outcome["delivery_withheld_reason"] = "auxiliary_only_selection"
    # Preserve the fit's model delivery. The eligibility flag gates both scalar and model paths, and a
    # later canonical selection can therefore restore certification without an unnecessary refit.
    log.warning(
        "withholding target delivery at q=%s: the tracked %s identity rests on auxiliary candidate "
        "%r, which is never pooled and never certification-eligible. Identity retained; this point "
        "still needs a canonical target measurement.",
        outcome.get("q"), target_branch, candidate.get("candidate_ref"))


def _rebuild_tracked_prior(card, session: SessionState, track: dict, observations: list[dict],
                           refs: dict, *, t_round: float) -> None:
    source_ids = {key.rsplit(":", 1)[0] for row in observations
                  for key in [row.get("authority")] if key}
    # Replace only derived points sourced from fits participating in this tracked campaign. Raw fit
    # evidence remains in the ledger/candidate tables and reassignment history remains in the track.
    card.state.dispersion_prior = [point for point in (card.state.dispersion_prior or [])
                                   if str(point.get("scan_id", "")).split("#", 1)[0] not in source_ids]
    revision = int(track.get("revision", 0))
    added = set()
    for assignment, observation in zip(track.get("assignments") or [], observations):
        if assignment.get("track_state") != "confirmed" or not observation.get("authority"):
            continue
        _entry, _detector, outcome = refs[observation["authority"]]
        if (not outcome.get("pooled") or outcome.get("modality") != "fold"
                or not _master_prior_admissible(_entry, outcome)
                or outcome.get("pool_consistent") is False):
            continue
        for branch in ("optic", "acoustic"):
            candidate_index = assignment.get(f"{branch}_candidate_index")
            if candidate_index is None:
                continue
            candidate = assignment["candidates"][candidate_index]
            if _is_auxiliary_candidate(candidate):
                # The prior accepts only clean single-answer folds from pooled masters. This candidate
                # was fitted by a probe that is not pooled at all, and the outcome gating this loop is
                # the *canonical* authority — so without this check the auxiliary energy would enter
                # the running dispersion prior wearing the master's fold provenance.
                continue
            probability = float((candidate.get("probabilities") or {}).get(branch, 0.0))
            if probability < branch_tracking.CONFIRMED_PROBABILITY:
                continue
            scan_id = f"{candidate['source_scan_id']}#tracked:{branch}"
            added.add(scan_id)
            session_prior.add_point(
                card.state,
                scan_id=scan_id,
                sample=outcome.get("sample"), T=outcome.get("T"), q=outcome.get("q"),
                omega0=candidate.get("energy_meV"), modality="fold", verdict="resolved",
                gamma=candidate.get("gamma"), gamma_err=candidate.get("gamma_err"),
                intensity=(outcome.get("intensity") if branch == "optic" else None),
                fit_kind=outcome.get("fit_kind"), branch_id=branch, weight=probability,
                branch_posterior=candidate.get("probabilities"),
                assignment_entropy=assignment.get("assignment_entropy"), t_round=t_round,
                require_fold=True)
    # Only the points this call rebuilt carry the current revision. A tracked point from another
    # campaign survives the source-id filter untouched, and stamping it here would advertise it as
    # evidence of this revision.
    for point in card.state.dispersion_prior:
        if point.get("scan_id") in added:
            point["track_revision"] = revision


def _authorized_tracking_domain(target: dict, observations: list[dict]):
    """Normalized union of policy- and provenance-authorized tracking windows.

    Candidate energies never contribute to this union. An auxiliary candidate with missing or malformed
    acquisition provenance remains visible evidence, but gains no support outside the declared target
    and identity bands.
    """
    intervals = []
    target_band = target.get("energy_search_meV")
    if target_band is None:
        intervals.extend(branch_tracking.normalize_energy_domain(None))
    else:
        try:
            intervals.extend(branch_tracking.normalize_energy_domain(target_band))
        except ValueError as exc:
            raise ValueError(
                f"malformed persisted energy_search_meV tracking authorization: {target_band!r}"
            ) from exc
    identity_band = target.get("identity_search_meV")
    if identity_band is not None:
        try:
            intervals.extend(branch_tracking.normalize_energy_domain(identity_band))
        except ValueError:
            log.warning("ignoring malformed persisted identity_search_meV tracking authorization: %r",
                        identity_band)
    for observation in observations:
        for candidate in observation.get("candidates") or []:
            if not _is_auxiliary_candidate(candidate):
                continue
            window = candidate.get("authorized_energy_window_meV")
            if window is None:
                continue
            try:
                intervals.extend(branch_tracking.normalize_energy_domain(window))
            except ValueError:
                log.warning("ignoring malformed auxiliary candidate tracking authorization: %r", window)
        for null_window in observation.get("null_windows") or []:
            try:
                intervals.extend(branch_tracking.normalize_energy_domain(null_window[:2]))
            except (TypeError, ValueError):
                log.warning("ignoring malformed auxiliary null-window tracking authorization: %r",
                            null_window)
    return branch_tracking.normalize_energy_domain(intervals)


def _apply_tracked_branch(session: SessionState, card, *, t_round: float) -> dict | None:
    if not _tracked_selector(card):
        return None
    target = getattr(card.state, "campaign_target", None) or {}
    path = target.get("path") or []
    if not path:
        log.warning("tracked_branch requested without a persisted objective path; assignment deferred")
        return None
    path = [[float(value) for value in q] for q in path]
    observations, refs = _tracked_authorities(session, path)
    policy = (getattr(card.state, "hypothesis_snapshot", None) or {}).get("steering") or {}
    previous = getattr(card.state, "branch_tracks", None) or {}
    target_branch = _campaign_target(card)[0]
    period = _dispersion_period(card)
    hypotheses = _campaign_hypotheses(card)
    entropy_stop = float(policy.get("assignment_entropy_stop_nats", 0.2))
    maximum_bridge_steps = int(policy.get("maximum_bridge_steps", 1))
    energy_domain = _authorized_tracking_domain(target, observations)
    authority_signature = _tracked_authority_signature(
        observations, target_branch=target_branch, period=period, hypotheses=hypotheses,
        entropy_stop=entropy_stop, maximum_bridge_steps=maximum_bridge_steps,
        energy_domain_meV=energy_domain,
        scorer_revision=branch_tracking.BRANCH_TRACKING_SCORER_REVISION)
    if (previous.get("authoritative_evidence_signature") == authority_signature
            and previous.get("target_branch") == target_branch
            and previous.get("assignments") is not None):
        track = previous
    else:
        track = branch_tracking.track_path(
            path, observations, period=period, hypotheses=hypotheses,
            target_branch=target_branch, previous=previous,
            assignment_entropy_stop_nats=entropy_stop,
            maximum_bridge_steps=maximum_bridge_steps,
            energy_domain_meV=energy_domain)
        track["authoritative_evidence_signature"] = authority_signature
    revision = int(track["revision"])
    for assignment, observation in zip(track["assignments"], observations):
        key = observation.get("authority")
        selected = assignment.get("selected_candidate_index")
        if not key:
            continue
        _entry, _detector, outcome = refs[key]
        outcome["branch_candidates"] = assignment["candidates"]
        # A MAP state is still only an assignment hypothesis.  Ambiguous/provisional/blocked target
        # rows remain candidate evidence and must not publish a scalar target delivery.
        if selected is None or assignment.get("track_state") != "confirmed":
            _clear_tracked_selection(outcome, assignment, revision)
            for _other_key, (_other_entry, _other_detector, other) in refs.items():
                if other is outcome or _path_index(other.get("q") or [], path) != assignment["path_index"]:
                    continue
                local_assignment = dict(assignment)
                local_assignment["track_state"] = "individual"
                _clear_tracked_selection(other, local_assignment, revision)
            continue
        candidate = assignment["candidates"][selected]
        if _is_auxiliary_candidate(candidate):
            _withhold_tracked_delivery(outcome, candidate, assignment, revision, target_branch)
        else:
            _select_mode(outcome, candidate, selected, assignment, revision, target_branch)
        # Individual fits at the same q remain muted evidence but follow the current identity for
        # backwards-compatible scalar columns, using their own nearest fitted mode. Referenced against
        # the selected candidate's energy rather than ``outcome["omega0"]``: identical on the delivery
        # path, and still the identity the tracker concluded when the delivery was withheld.
        reference_energy = float(candidate["energy_meV"])
        for _other_key, (_other_entry, _other_detector, other) in refs.items():
            if other is outcome or _path_index(other.get("q") or [], path) != assignment["path_index"]:
                continue
            candidates = other.get("branch_candidates") or []
            if not candidates:
                continue
            nearest = min(range(len(candidates)), key=lambda index: abs(
                float(candidates[index]["energy_meV"]) - reference_energy))
            local_assignment = dict(assignment)
            local_assignment["track_state"] = "individual"
            _select_mode(other, candidates[nearest], nearest, local_assignment, revision,
                         target_branch)
    _rebuild_tracked_prior(card, session, track, observations, refs, t_round=t_round)
    card.state.branch_tracks = track
    return track


def _rebuild_pooled_prior(card, session: SessionState, session_prior, *, t_round: float) -> None:
    """Rebuild the non-tracked running dispersion prior from pooled masters only."""
    if _tracked_selector(card):
        return
    source_ids = set(session.scans)
    card.state.dispersion_prior = [
        point for point in (card.state.dispersion_prior or [])
        if str(point.get("scan_id", "")).split("#", 1)[0] not in source_ids]
    for entry in sorted(session.scans.values(), key=lambda item: item.scan_number):
        for outcome in entry.detectors.values():
            if (not outcome.get("pooled") or outcome.get("modality") != "fold"
                    or not _master_prior_admissible(entry, outcome)
                    or outcome.get("pool_consistent") is False):
                continue
            session_prior.add_point(
                card.state, scan_id=entry.scan_id, sample=outcome.get("sample"),
                T=outcome.get("T"), q=outcome.get("q"), omega0=outcome.get("omega0"),
                modality="fold", verdict=outcome.get("verdict"), gamma=outcome.get("gamma"),
                intensity=outcome.get("intensity"), fit_kind=outcome.get("fit_kind"),
                branch_id=outcome.get("branch_id"), weight=outcome.get("branch_weight"),
                branch_posterior=outcome.get("branch_posterior"),
                assignment_entropy=outcome.get("assignment_entropy"), t_round=t_round,
                require_fold=True)


def _master_prior_admissible(entry: ScanEntry, outcome: dict) -> bool:
    """Full errors or a trusted HESSE master may seed the prior; cheap/untrusted fits may not."""
    provenance = entry.provenance or {}
    tier = provenance.get("errors_tier_effective") or provenance.get(
        "analysis_tier_effective") or provenance.get("analysis_tier")
    # Additive compatibility: ledgers predating tier provenance already passed the resolved-fold
    # gate and must not silently lose their prior points merely by being reopened on newer code.
    if tier is None:
        return True
    if tier in FULL_TIERS:
        return True
    return tier == "fast" and bool(outcome.get("hesse_trusted"))


def _resolve_target_projections(session: SessionState, data_dir) -> int:
    """Resolve and publish one ``isar.target_projection/2`` per pooled master (ADR-019).

    Called ONLY after branch triage, ``_materialize_branch_candidates`` and the connected tracker
    have run, because all three can still move the target: triage can withhold the delivery, the
    tracker can reassign the selected candidate or clear it entirely, and neither is visible at fit
    time. Each projection is bound to the authority basis that decided it, so a later track revision
    produces a NEW projection against the unchanged ``/2`` — no refit is involved.
    """
    if data_dir is None:
        return 0
    published = 0
    for key in sorted(session.scans):
        if not key.startswith("master:"):
            continue
        entry = session.scans[key]
        authority = (entry.provenance or {}).get("target_projection_authority") or {}
        for outcome in entry.detectors.values():
            ref = outcome.get("predictive_ref")
            if not outcome.get("pooled") or not ref:
                continue
            try:
                artifact = json.loads(
                    (Path(data_dir).parent / str(ref)).read_text(encoding="utf-8"))
                projection = predictive_artifact.build_target_projection(
                    artifact, outcome,
                    master_revision=authority.get("master_revision")
                    or outcome.get("master_revision"),
                    calibration_fingerprint=authority.get("calibration_fingerprint"),
                    policy_fingerprint=authority.get("policy_fingerprint"),
                    action_role=authority.get("action_role") or "master")
                projection_ref = _persist_content_addressed(
                    data_dir, "target_projection", projection)
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                # A projection is steering input, never a scientific number: record why it is
                # missing and leave the master's fit and every other artifact untouched.
                log.warning("%s: target projection unavailable (%s): %s",
                            key, type(error).__name__, error)
                entry.provenance["target_projection_unavailable"] = {
                    "reason_code":
                        predictive_artifact.PROJECTION_UNAVAILABLE_CONSTRUCTION_FAILED,
                    "error_type": type(error).__name__, "message": str(error)}
                continue
            outcome["target_projection_ref"] = projection_ref
            entry.provenance["target_projection_ref"] = projection_ref
            entry.provenance.pop("target_projection_unavailable", None)
            published += 1
    return published
