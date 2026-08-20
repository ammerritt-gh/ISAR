"""Predicate evaluators: authority-aware answers from a scene revision (SCENE-4 PR 4.1).

Every evaluator is a pure function of ``(spec, scene revision payload, prior assessments)``
returning a :class:`~isar.scene.questions.QuestionAssessment` — no module constant is read at
evaluation time (thresholds arrive frozen on the spec's predicate declaration), no scene object
is held (everything is the persisted payload dict), and no answer is manufactured from a
variable the scene does not represent: structural presence needs no identity semantics, but
identity, mismatch, parameter precision, and ZC confidence stay OPEN with a typed blocker when
their explicit hypotheses or persisted uncertainties are absent.

**Bound arithmetic is log-space and adversarial.** A truncated cluster's posteriors are
retained-normalized; the recorded ``log_discarded_mass_bound`` is the log of the dropped mass
relative to the retained leader. With retained proposition mass ``m``, retained leader ``p*``
and ``log_q = log_discarded_mass_bound + log(p*)``, the true mass lies in
``[m/(1+q), (m+q)/(1+q)]`` — everything omitted allocated against the claim. The linear ``q``
is never formed when it could overflow: a huge finite ``log_q`` yields the honest vacuous
``[0, 1]``, and a vacuous interval answers nothing (it records
``bound_vacuous_for_threshold`` and the question stays open).

**Cross-cluster claims multiply.** Clusters are independently normalized factors, so within one
partition an existential claim combines as ``1 - prod(1 - m_i)`` and a universal claim as
``prod(m_i)``, on the interval endpoints. Twenty tiles at 0.95 do NOT satisfy a 0.9 universal
threshold (0.95**20 ~ 0.36); one qualifying cluster remains a sufficient fast path for an
existential YES only because the product bound already implies it.
"""
from __future__ import annotations

import math
from typing import Mapping

from .model import partition_id, partition_key
from .questions import (
    Blocker, QuestionAssessment, QuestionSpec, SubjectMatch, match_subject)


def tile_contains(tile, coordinate) -> bool:
    """Half-open tile membership ``[lo, hi)`` — a grid CELL, not a declared interval.

    ``snap_tile`` defines cells half-open, so a scan exactly at a shared boundary belongs to
    exactly ONE cell; evaluating cells as closed intervals put such a scan in both adjacent
    endpoint sets, and a component present at the boundary could satisfy ``track_connected``
    without traversing anything (PR #31 finding J). Declared ``region_rlu`` intervals stay
    closed — they are declarations, not grid cells.
    """
    return tile[0] <= coordinate < tile[1]

# Above this, exp(log_q) has no float meaning worth forming; the interval is honestly vacuous.
_LOG_Q_CAP = 700.0


# ----------------------------------------------------------------------------
# Cluster authority and the omitted-mass interval
# ----------------------------------------------------------------------------
def cluster_authority(cluster: Mapping) -> tuple[str, float | None]:
    """``(lane, log_q)`` for one persisted cluster payload.

    ``exact``: enumeration was not truncated — the posteriors are the whole comparison.
    ``bounded``: truncated with a recorded finite bound; ``log_q`` prices the omitted mass.
    ``unresolved``: no factor bounded the truncation (``truncation_unbounded``), the bound is
    missing despite truncation, linkage variants were capped, or the retained leader has no
    mass to anchor the bound — the cluster can never answer, only contribute blockers.
    """
    if cluster.get("truncation_unbounded") or cluster.get("linkage_truncated"):
        return "unresolved", None
    if not cluster.get("truncated"):
        return "exact", None
    bound = cluster.get("log_discarded_mass_bound")
    if bound is None:
        return "unresolved", None
    leader = max((a.get("posterior") or 0.0) for a in cluster.get("alternatives") or [{}])
    if not (isinstance(leader, (int, float)) and leader > 0.0 and math.isfinite(leader)):
        return "unresolved", None
    return "bounded", float(bound) + math.log(leader)


def mass_interval(retained_proposition_mass: float, log_q: float | None) -> tuple[float, float]:
    """The true-mass interval under adversarial allocation of the omitted mass.

    ``log_q is None`` means exact authority: the retained mass IS the mass. The linear ``q`` is
    only formed under the cap; beyond it the interval is the honest ``[0, 1]`` — never an
    overflow, never an exception.
    """
    m = min(1.0, max(0.0, retained_proposition_mass))
    if log_q is None:
        return (m, m)
    if log_q > _LOG_Q_CAP:
        return (0.0, 1.0)
    q = math.exp(log_q)
    return (m / (1.0 + q), min(1.0, (m + q) / (1.0 + q)))


def combine_existential(intervals) -> tuple[float, float]:
    """P(any cluster satisfies): [1 - prod(1-l_i), 1 - prod(1-u_i)] — independent factors."""
    lo, hi = 1.0, 1.0
    for l, u in intervals:
        lo *= (1.0 - l)
        hi *= (1.0 - u)
    return (1.0 - lo, 1.0 - hi)


def combine_universal(intervals) -> tuple[float, float]:
    """P(every cluster satisfies): [prod(l_i), prod(u_i)] — independent factors."""
    lo, hi = 1.0, 1.0
    for l, u in intervals:
        lo *= l
        hi *= u
    return (lo, hi)


def decide(interval: tuple[float, float], threshold: float) -> str:
    """``yes`` when the lower end clears the threshold, ``no`` when the upper end misses it,
    ``open`` when the interval straddles — a bound that decides nothing answers nothing."""
    lo, hi = interval
    if lo >= threshold:
        return "yes"
    if hi < threshold:
        return "no"
    return "open"


# ----------------------------------------------------------------------------
# Payload access helpers (persisted shapes only — never live objects)
# ----------------------------------------------------------------------------
def _candidates_by_ref(scene_payload: Mapping) -> dict[str, dict[str, Mapping]]:
    out: dict[str, dict[str, Mapping]] = {}
    for obs in scene_payload.get("observations") or ():
        out[obs["scan_ref"]] = {c["label"]: c for c in obs.get("candidates") or ()}
    return out


def _in_band(omega, band) -> bool:
    if omega is None or not (isinstance(omega, (int, float)) and math.isfinite(omega)):
        # An unplaceable mode exists but cannot be placed IN the subject's support; a positive
        # claim about a band may not lean on it.
        return False
    if band is None:
        return True
    return band[0] <= abs(omega) <= band[1]


def _matched_mode_omegas(alternative: Mapping, matched_refs: set,
                         candidates: Mapping, band) -> list[float]:
    """Every placeable in-band mode omega this alternative asserts on the matched arenas."""
    out = []
    for scan_ref, label in alternative.get("assignment") or ():
        if scan_ref not in matched_refs:
            continue
        row = candidates.get(scan_ref, {}).get(label)
        if row is None:
            continue
        for component in row.get("components") or ():
            if component.get("kind") != "mode":
                continue
            omegas = row.get("omegas") or []
            slot = component["slot"]
            omega = omegas[slot] if 0 <= slot < len(omegas) else None
            if _in_band(omega, band):
                out.append(float(omega))
    return out


def _proposition_intervals(match: SubjectMatch, scene_payload: Mapping, qualifies,
                           requirement: str):
    """Per matched cluster: the adversarial interval on the mass of alternatives satisfying
    ``qualifies(alternative)``, plus harvested blockers.

    An ``unresolved`` cluster — and, under an ``exact`` authority requirement, a bounded one —
    contributes the vacuous ``[0, 1]``: it can veto a universal claim and never carry an
    existential one, which is exactly "never answers".
    """
    clusters = scene_payload.get("clusters") or ()
    intervals, lanes, blockers = [], [], []
    for index in match.cluster_indices:
        cluster = clusters[index]
        lane, log_q = cluster_authority(cluster)
        if requirement == "exact" and lane == "bounded":
            lane = "unresolved"
        lanes.append(lane)
        if lane == "unresolved":
            intervals.append((0.0, 1.0))
            blockers.append(Blocker("posterior_truncation_unresolved",
                                    f"cluster {cluster.get('cluster_id', index)}"))
            continue
        mass = sum((a.get("posterior") or 0.0) for a in cluster.get("alternatives") or ()
                   if qualifies(a))
        intervals.append(mass_interval(mass, log_q))
    return intervals, lanes, tuple(blockers)


def _weakest(lanes) -> str | None:
    for lane in ("unresolved", "bounded", "exact"):
        if lane in lanes:
            return lane
    return None


def _vacuous_note(lanes, threshold, combined) -> tuple:
    if "bounded" in lanes and decide(combined, threshold) == "open":
        return (("bound_vacuous_for_threshold",
                 f"interval [{combined[0]:.6g}, {combined[1]:.6g}] straddles {threshold}"),)
    return ()


# ----------------------------------------------------------------------------
# Family evaluators
# ----------------------------------------------------------------------------
def _coverage_complete(spec: QuestionSpec, scene_payload: Mapping, prior) -> QuestionAssessment:
    """DiscoverRegion: no in-region axis gap wider than the frozen spacing, and the frozen
    omega domain band-covered at every surveyed node. Geometry, not posterior — authority is
    exact. Quarantine (count-untrusted) arenas are transient blockers, never coverage."""
    params = dict(spec.predicate.parameters)
    spacing = float(params["maximum_initial_spacing_rlu"])
    domain = params.get("omega_domain_meV")
    match = match_subject(spec.subject, scene_payload)
    region = spec.subject.region_rlu
    notes, blockers = [], []
    for entry in scene_payload.get("non_scoring") or ():
        if entry.get("reason") == "count_untrusted":
            blockers.append(Blocker("quarantined_scan", entry.get("scan_ref", "")))

    coords = sorted(fp["axis_coordinate"] for fp in match.footprints
                    if fp.get("axis_coordinate") is not None)
    if region is None or not coords:
        return QuestionAssessment(question_id=spec.question_id, status="open",
                                  authority="exact", blockers=tuple(blockers),
                                  notes=(("reason", "no_surveyed_nodes_in_region"),))
    edges = [region[0], *coords, region[1]]
    widest = max(b - a for a, b in zip(edges, edges[1:]))
    if widest > spacing:
        notes.append(("widest_unsurveyed_gap_rlu", round(widest, 9)))
        return QuestionAssessment(question_id=spec.question_id, status="open",
                                  authority="exact", blockers=tuple(blockers),
                                  notes=tuple(notes))
    if domain is not None:
        lo, hi = float(domain[0]), float(domain[1])
        for fp in match.footprints:
            covered = [(b["lo"], b["hi"]) for b in fp.get("bands") or ()]
            cursor = lo
            for b_lo, b_hi in sorted(covered):
                if b_lo > cursor:
                    break
                cursor = max(cursor, b_hi)
            if cursor < hi:
                notes.append(("uncovered_omega_at", fp["scan_ref"]))
                return QuestionAssessment(question_id=spec.question_id, status="open",
                                          authority="exact", blockers=tuple(blockers),
                                          notes=tuple(notes))
    return QuestionAssessment(question_id=spec.question_id, status="answered",
                              authority="exact", answer=(("complete", True),),
                              blockers=tuple(blockers), notes=tuple(notes))


def _structural_presence(spec, scene_payload, prior) -> QuestionAssessment:
    """ConfirmPresence: mass on alternatives asserting a placeable physical mode in the
    subject's q/omega support, versus phonon-less/null candidates. Requires NO identity
    semantics."""
    params = dict(spec.predicate.parameters)
    threshold = float(params["min_mass"])
    match = match_subject(spec.subject, scene_payload)
    if not match.cluster_indices:
        return QuestionAssessment(question_id=spec.question_id, status="open",
                                  notes=(("reason", "no_matched_evidence"),))
    candidates = _candidates_by_ref(scene_payload)
    refs, band = set(match.scan_refs), spec.subject.omega_band_meV

    def qualifies(alternative):
        return bool(_matched_mode_omegas(alternative, refs, candidates, band))

    intervals, lanes, blockers = _proposition_intervals(
        match, scene_payload, qualifies, spec.predicate.authority_requirement)
    combined = combine_existential(intervals)
    verdict = decide(combined, threshold)
    notes = _vacuous_note(lanes, threshold, combined)
    if verdict == "open":
        return QuestionAssessment(question_id=spec.question_id, status="open",
                                  authority=_weakest(lanes), mass_interval=combined,
                                  blockers=blockers, notes=notes)
    return QuestionAssessment(question_id=spec.question_id, status="answered",
                              authority=_weakest(lanes), mass_interval=combined,
                              answer=(("present", verdict == "yes"),), blockers=blockers)


def _alternative_matched_identities(alternative: Mapping, refs: set, candidates: Mapping,
                                    band) -> set:
    """Identities this ONE retained alternative asserts on the matched arenas — the single walk
    both :func:`identity_hypothesis_classes` and :func:`_identity_resolved` share (SCENE-4.2a
    P1-G/A6). A row counts only when it carries a placeable, in-band mode component (mirrors
    :func:`_matched_mode_omegas`); its ``identity`` (possibly ``None``, meaning "no identity
    semantics assigned") is then contributed. Rows the alternative never assigns, or whose only
    modes sit outside the subject's band, contribute nothing — pruned/non-retained candidates
    never reach here because the caller walks retained ``alternatives``, not raw candidate rows.
    """
    out = set()
    for scan_ref, label in alternative.get("assignment") or ():
        if scan_ref not in refs:
            continue
        row = candidates.get(scan_ref, {}).get(label)
        if row is None:
            continue
        omegas = row.get("omegas") or []
        for component in row.get("components") or ():
            if component.get("kind") != "mode":
                continue
            slot = component["slot"]
            omega = omegas[slot] if 0 <= slot < len(omegas) else None
            if _in_band(omega, band):
                out.add(row.get("identity"))
                break
    return out


def identity_hypothesis_classes(subject, scene_payload: Mapping) -> frozenset:
    """The identity classes ClassifyIdentity could answer from for ``subject``, derived from
    identities actually present in RETAINED alternatives — the evidence the evaluator can use
    (SCENE-4.2a P1-G). Walks the same matched clusters' retained ``alternatives`` (never raw
    candidate rows: a pruned or non-retained candidate carries an explicit identity that no
    assignment ever selects, and must not open the gate), via the SAME per-alternative walk
    :func:`_identity_resolved` uses for ``matched_identities`` (one definition of
    "representable", shared, not two answers that could disagree — SCENE-4.2a A6). A class
    counts only with an explicit, non-``None`` ``identity`` on a placeable, in-band mode. An
    empty result means ClassifyIdentity would open and never close.
    """
    match = match_subject(subject, scene_payload)
    if not match.cluster_indices:
        return frozenset()
    candidates = _candidates_by_ref(scene_payload)
    refs, band = set(match.scan_refs), subject.omega_band_meV
    clusters = scene_payload.get("clusters") or ()
    classes = set()
    for index in match.cluster_indices:
        for alternative in clusters[index].get("alternatives") or ():
            classes.update(_alternative_matched_identities(alternative, refs, candidates, band))
    classes.discard(None)
    return frozenset(classes)


def _identity_resolved(spec, scene_payload, prior) -> QuestionAssessment:
    """ClassifyIdentity: answers ONLY from explicit identity alternatives. ``identity=None``
    means "no identity semantics were assigned", never the physical unknown — it accrues to
    ``unclassified_mass``, an authority limitation that can answer nothing."""
    params = dict(spec.predicate.parameters)
    threshold = float(params["min_mass"])
    match = match_subject(spec.subject, scene_payload)
    if not match.cluster_indices:
        return QuestionAssessment(question_id=spec.question_id, status="open",
                                  notes=(("reason", "no_matched_evidence"),))
    candidates = _candidates_by_ref(scene_payload)
    refs, band = set(match.scan_refs), spec.subject.omega_band_meV

    def matched_identities(alternative):
        return _alternative_matched_identities(alternative, refs, candidates, band)

    classes = identity_hypothesis_classes(spec.subject, scene_payload)
    if not classes:
        return QuestionAssessment(
            question_id=spec.question_id, status="open",
            blockers=(Blocker("identity_hypotheses_unavailable",
                              "no candidate in the matched support declares an identity"),),
            notes=(("reason", "identity_hypotheses_unavailable"),))

    best = None
    for identity in sorted(classes):
        def qualifies(alternative, _identity=identity):
            return matched_identities(alternative) == {_identity}
        intervals, lanes, blockers = _proposition_intervals(
            match, scene_payload, qualifies, spec.predicate.authority_requirement)
        combined = combine_universal(intervals)
        if best is None or combined[0] > best[1][0]:
            best = (identity, combined, lanes, blockers)

    unclassified = 0.0
    clusters = scene_payload.get("clusters") or ()
    for index in match.cluster_indices:
        for alternative in clusters[index].get("alternatives") or ():
            if None in matched_identities(alternative):
                unclassified += alternative.get("posterior") or 0.0

    identity, combined, lanes, blockers = best
    notes = (("unclassified_mass", round(unclassified, 9)),) + _vacuous_note(
        lanes, threshold, combined)
    if decide(combined, threshold) == "yes":
        return QuestionAssessment(question_id=spec.question_id, status="answered",
                                  authority=_weakest(lanes), mass_interval=combined,
                                  answer=(("identity", identity),), blockers=blockers,
                                  notes=notes)
    return QuestionAssessment(question_id=spec.question_id, status="open",
                              authority=_weakest(lanes), mass_interval=combined,
                              blockers=blockers, notes=notes)


def _track_connected(spec, scene_payload, prior) -> QuestionAssessment:
    """ConnectTrack: mass on alternatives whose linkage connects the two endpoint tiles.

    Endpoints match footprints by tile; components are matched to endpoints by the scan_ref
    embedded in the persisted ``component_id`` (``scan_ref#label#slot``) — an identifier used
    for GROUPING only, never for semantics. Endpoints in disjoint clusters stay OPEN: absent
    adjacency may be an unsurveyed middle, and a scene may not charge an absence to a hole.
    """
    params = dict(spec.predicate.parameters)
    threshold = float(params["min_mass"])
    subject = spec.subject
    tiles = (subject.endpoint_a_rlu, subject.endpoint_b_rlu)
    if tiles[0] is None:
        return QuestionAssessment(question_id=spec.question_id, status="open",
                                  notes=(("reason", "no_endpoint_tiles_declared"),))
    footprints = [fp for fp in scene_payload.get("footprints") or ()
                  if fp.get("partition_id") == subject.partition_id
                  and fp.get("axis_coordinate") is not None]
    ends = []
    for tile in tiles:
        ends.append({fp["scan_ref"] for fp in footprints
                     if tile_contains(tile, fp["axis_coordinate"])})
    if not ends[0] or not ends[1]:
        return QuestionAssessment(question_id=spec.question_id, status="open",
                                  notes=(("reason", "endpoint_unmeasured"),))

    clusters = scene_payload.get("clusters") or ()
    joint = [i for i, cluster in enumerate(clusters)
             if ends[0].intersection(cluster.get("scan_refs") or ())
             and ends[1].intersection(cluster.get("scan_refs") or ())]
    if not joint:
        return QuestionAssessment(question_id=spec.question_id, status="open",
                                  notes=(("reason", "endpoints_in_disjoint_clusters"),))

    def scan_of(component_id: str) -> str:
        return component_id.rsplit("#", 2)[0]

    def qualifies(alternative):
        adjacency: dict[str, set] = {}
        for a, b in alternative.get("links") or ():
            adjacency.setdefault(a, set()).add(b)
            adjacency.setdefault(b, set()).add(a)
        starts = [c for c in adjacency if scan_of(c) in ends[0]]
        goal = ends[1]
        seen = set(starts)
        frontier = list(starts)
        while frontier:
            node = frontier.pop()
            if scan_of(node) in goal:
                return True
            for neighbour in adjacency.get(node, ()):
                if neighbour not in seen:
                    seen.add(neighbour)
                    frontier.append(neighbour)
        return False

    match = SubjectMatch(scan_refs=tuple(sorted(ends[0] | ends[1])), footprints=(),
                         cluster_indices=tuple(joint))
    intervals, lanes, blockers = _proposition_intervals(
        match, scene_payload, qualifies, spec.predicate.authority_requirement)
    combined = combine_existential(intervals)
    verdict = decide(combined, threshold)
    notes = _vacuous_note(lanes, threshold, combined)
    if verdict == "open":
        return QuestionAssessment(question_id=spec.question_id, status="open",
                                  authority=_weakest(lanes), mass_interval=combined,
                                  blockers=blockers, notes=notes)
    return QuestionAssessment(question_id=spec.question_id, status="answered",
                              authority=_weakest(lanes), mass_interval=combined,
                              answer=(("connected", verdict == "yes"),), blockers=blockers)


def _trusted_matched_interval(alternative, refs, candidates, band):
    """The alternative's ONE trusted, matched per-mode interval, or ``None`` when the match is
    absent, ambiguous, or the writer's own record is not producer-sound (P0-C / SCENE-4.2a A2) —
    no trust minted in the scene layer.

    Trusted means the ``authority`` dict read VERBATIM off the persisted :class:`ModeInterval`
    carries ``authority_basis == "producer_sound"`` — the writer's own fit-conditional verdict
    on whether this candidate's bootstrap record is a converged, unimodal, genuinely frequentist
    sampling distribution. This is deliberately NOT about which candidate delivery selected: a
    non-winner survivor with a sound record is trusted exactly like a winner with one, and a
    winner with an unsound record is not trusted just because it won. Two indistinguishable
    in-band matches are as unqualifying as zero — nothing in the subject names which slot is
    meant, so guessing would mint identity the writer never asserted.
    """
    matches = []
    for scan_ref, label in alternative.get("assignment") or ():
        if scan_ref not in refs:
            continue
        row = candidates.get(scan_ref, {}).get(label)
        if row is None:
            continue
        for component in row.get("components") or ():
            if component.get("kind") != "mode":
                continue
            omegas = row.get("omegas") or []
            slot = component["slot"]
            omega = omegas[slot] if 0 <= slot < len(omegas) else None
            if _in_band(omega, band):
                matches.append((row, slot, scan_ref, label))
    if len(matches) != 1:
        return None
    row, slot, scan_ref, label = matches[0]
    mode_intervals = row.get("mode_intervals") or []
    interval = mode_intervals[slot] if 0 <= slot < len(mode_intervals) else None
    if interval is None:
        return None
    authority = interval.get("authority") or {}
    if authority.get("authority_basis") != "producer_sound":
        return None
    return {"low": float(interval["low"]), "high": float(interval["high"]),
            "nominal_level": interval.get("nominal_level"),
            "interval_kind": interval.get("interval_kind"),
            "inference_paradigm": interval.get("inference_paradigm"),
            "conditioning_fingerprint": interval.get("conditioning_fingerprint"),
            "scan_ref": scan_ref, "label": label, "slot": slot}


def _acceptance_window(interval, tolerance):
    """``(hi - tol, lo + tol)`` when the interval's full width fits inside ``2*tolerance``
    (half-width semantics: an interval of full width exactly ``2*tolerance`` still supports its
    one point); ``None`` when it is too wide to ever support any center."""
    if interval["high"] - interval["low"] > 2.0 * tolerance:
        return None
    return (interval["high"] - tolerance, interval["low"] + tolerance)


def _parameter_within_tolerance(spec, scene_payload, prior) -> QuestionAssessment:
    """EstimateParameter: closes on BOTH within-candidate uncertainty (the A1 per-mode
    ``ModeInterval``) and between-candidate disagreement (retained posterior support, priced
    through the shared omitted-mass bound machinery) — SCENE-4.2a A2, retiring the v1
    never-closes ruling (docs/design/scene.md §5).

    The search set for the estimated center is the finite, order-independent list of
    acceptance-window endpoints over every TRUSTED, support-capable interval; ties break to the
    smallest candidate center, so replay is byte-identical regardless of iteration order. Every
    non-qualifying mass — untrusted, unpopulated, ambiguously matched, or simply outside the
    band — counts ADVERSARIALLY: it can never be retained. Closure additionally requires every
    CONTRIBUTING interval (the ones supporting the chosen center, drawn from the SAME
    positive-posterior support the mass calculation used — a zero-mass alternative can neither
    block nor join closure) to agree on the exact ``(interval_kind, inference_paradigm,
    nominal_level)`` triple; a mismatch on any of the three refuses closure rather than average
    across incompatible statements of confidence.

    ``envelope_meV`` is the supported-center interval itself — the intersection of every
    contributing acceptance window at the chosen center — and ``center_meV`` is honestly its
    midpoint representative, not a re-derived tolerance witness of its own.
    """
    params = dict(spec.predicate.parameters)
    tolerance = float(params["tolerance_meV"])
    floor = float(params["statistical_floor_meV"])
    min_mass = float(params["parameter_precision_min_mass"])
    match = match_subject(spec.subject, scene_payload)
    candidates = _candidates_by_ref(scene_payload)
    refs, band = set(match.scan_refs), spec.subject.omega_band_meV
    clusters = scene_payload.get("clusters") or ()

    trusted_any = False
    capable = []
    for index in match.cluster_indices:
        for alternative in clusters[index].get("alternatives") or ():
            interval = _trusted_matched_interval(alternative, refs, candidates, band)
            if interval is None:
                continue
            trusted_any = True
            window = _acceptance_window(interval, tolerance)
            if window is not None:
                capable.append(window)

    notes = [("tolerance_meV", tolerance)]
    blockers = []
    if not trusted_any:
        blockers.append(Blocker("within_candidate_uncertainty_unavailable",
                                "no trusted per-mode interval in the matched partition"))
    if tolerance < floor:
        blockers.append(Blocker("tolerance_below_representable_precision",
                                f"tolerance {tolerance} < floor {floor}"))
    if blockers:
        return QuestionAssessment(question_id=spec.question_id, status="open",
                                  blockers=tuple(blockers), notes=tuple(notes))
    if not capable:
        return QuestionAssessment(question_id=spec.question_id, status="open",
                                  notes=tuple(notes + [("reason",
                                                        "no_support_capable_interval")]))

    # Unrounded: the search set must be the EXACT float endpoints ``qualifies`` recomputes
    # per-candidate (same deterministic arithmetic on the same inputs), or a boundary case could
    # round to a center just outside the very window that produced it (a real float trap, not a
    # hypothetical one — ``5.07 - 0.1`` and ``4.97`` differ in the last bit).
    centers = sorted({lo for lo, hi in capable} | {hi for lo, hi in capable})

    best = None      # (combined_mass_interval, c, lanes, cluster_blockers)
    for c in centers:
        def qualifies(alternative, _c=c):
            interval = _trusted_matched_interval(alternative, refs, candidates, band)
            if interval is None:
                return False
            window = _acceptance_window(interval, tolerance)
            return window is not None and window[0] <= _c <= window[1]

        intervals, lanes, cluster_blockers = _proposition_intervals(
            match, scene_payload, qualifies, spec.predicate.authority_requirement)
        combined = combine_universal(intervals)
        if best is None or combined[0] > best[0][0]:
            best = (combined, c, lanes, cluster_blockers)

    combined, c, lanes, cluster_blockers = best
    contributing = set()
    triples = set()   # (interval_kind, inference_paradigm, nominal_level)
    # Conditioning fingerprints are PROVENANCE, never compatibility: every candidate's
    # conditioning differs by construction (that is what a per-candidate record is), so
    # gating the triple on them would refuse every multi-candidate closure. They ride the
    # answer as an audit trail instead.
    conditioning = set()
    envelope_lo, envelope_hi = None, None
    for index in match.cluster_indices:
        for alternative in clusters[index].get("alternatives") or ():
            # P1-I: metadata is derived from the SAME positive-posterior support the mass
            # calculation used — a zero-mass alternative must neither block closure (mixed
            # semantics) nor appear in contributors/envelope.
            if (alternative.get("posterior") or 0.0) <= 0.0:
                continue
            interval = _trusted_matched_interval(alternative, refs, candidates, band)
            if interval is None:
                continue
            window = _acceptance_window(interval, tolerance)
            if window is None or not (window[0] <= c <= window[1]):
                continue
            contributing.add((interval["scan_ref"], interval["label"], interval["slot"]))
            triples.add((interval["interval_kind"], interval["inference_paradigm"],
                        interval["nominal_level"]))
            if interval.get("conditioning_fingerprint"):
                conditioning.add(interval["conditioning_fingerprint"])
            envelope_lo = window[0] if envelope_lo is None else max(envelope_lo, window[0])
            envelope_hi = window[1] if envelope_hi is None else min(envelope_hi, window[1])

    notes += list(_vacuous_note(lanes, min_mass, combined))
    if len(triples) > 1:
        notes.append(("mixed_interval_semantics",
                      tuple(sorted(triples, key=lambda t: (t[0] or "", t[1] or "",
                                                            repr(t[2]))))))
        return QuestionAssessment(question_id=spec.question_id, status="open",
                                  authority=_weakest(lanes), mass_interval=combined,
                                  blockers=cluster_blockers, notes=tuple(notes))
    if not triples or combined[0] < min_mass:
        return QuestionAssessment(question_id=spec.question_id, status="open",
                                  authority=_weakest(lanes), mass_interval=combined,
                                  blockers=cluster_blockers, notes=tuple(notes))
    interval_kind, inference_paradigm, nominal_level = triples.pop()
    answer = (("center_meV", round((envelope_lo + envelope_hi) / 2.0, 9)),
             ("contributing", tuple(sorted(contributing))),
             ("envelope_meV", (round(envelope_lo, 9), round(envelope_hi, 9))),
             ("nominal_level", nominal_level),
             ("interval_kind", interval_kind),
             ("inference_paradigm", inference_paradigm),
             ("conditioning_fingerprints", tuple(sorted(conditioning))))
    return QuestionAssessment(question_id=spec.question_id, status="answered",
                              authority=_weakest(lanes), mass_interval=combined,
                              answer=answer, blockers=cluster_blockers, notes=tuple(notes))


def _zc_linear_compatibility(spec, scene_payload, prior) -> QuestionAssessment:
    """EstimateParameter/endpoint: a bounded COMPATIBILITY assessment, never a numeric
    confidence — ``maximum_justifiable_zc_confidence`` is 0.0 in v1 because no probabilistic
    extrapolation model or persisted uncertainties exist. Under the declared linear-near-ZC
    prior (slope <= ``dispersion_slope_max``), each alternative's nearest matched mode
    extrapolates to an interval at the declared fold coordinate; mass whose interval contains
    omega = 0 is compatible with the branch reaching the zone centre."""
    params = dict(spec.predicate.parameters)
    threshold = float(params["requested_zc_confidence"])
    anchor = spec.subject.anchor
    if anchor is None:
        return QuestionAssessment(question_id=spec.question_id, status="open",
                                  notes=(("reason", "no_declared_anchor"),))
    slope = (scene_payload.get("sample_knowledge") or {}).get("dispersion_slope_max")
    if slope is None:
        return QuestionAssessment(
            question_id=spec.question_id, status="open",
            blockers=(Blocker("dispersion_support_undeclared",
                              "no dispersion_slope_max declared; linear extrapolation has no "
                              "declared support"),),
            notes=(("reason", "dispersion_support_undeclared"),))
    match = match_subject(spec.subject, scene_payload)
    if not match.cluster_indices:
        return QuestionAssessment(question_id=spec.question_id, status="open",
                                  notes=(("reason", "no_matched_evidence"),))
    candidates = _candidates_by_ref(scene_payload)
    band = spec.subject.omega_band_meV
    coord_of = {fp["scan_ref"]: fp["axis_coordinate"] for fp in match.footprints
                if fp.get("axis_coordinate") is not None}
    spans = []

    def nearest_interval(alternative):
        """(lo, hi) at the anchor for this alternative's nearest matched mode, or None."""
        best = None
        for scan_ref, label in alternative.get("assignment") or ():
            coord = coord_of.get(scan_ref)
            if coord is None:
                continue
            row = candidates.get(scan_ref, {}).get(label)
            if row is None:
                continue
            for component in row.get("components") or ():
                if component.get("kind") != "mode":
                    continue
                omegas = row.get("omegas") or []
                slot = component["slot"]
                omega = omegas[slot] if 0 <= slot < len(omegas) else None
                if not _in_band(omega, band):
                    continue
                distance = abs(coord - anchor)
                if best is None or distance < best[0]:
                    best = (distance, float(omega))
        if best is None:
            return None
        distance, omega = best
        return (max(0.0, abs(omega) - slope * distance), abs(omega) + slope * distance)

    def compatible(alternative):
        span = nearest_interval(alternative)
        if span is not None:
            spans.append(span)
        return span is not None and span[0] <= 0.0

    intervals, lanes, blockers = _proposition_intervals(
        match, scene_payload, compatible, spec.predicate.authority_requirement)
    combined = combine_existential(intervals)
    notes = [("maximum_justifiable_confidence", 0.0),
             ("confidence_ceiling",
              "no probabilistic model or persisted uncertainties; compatibility only")]
    if spans:
        notes.append(("extrapolated_interval_meV",
                      (round(min(s[0] for s in spans), 9),
                       round(max(s[1] for s in spans), 9))))
    verdict = decide(combined, threshold)
    notes += list(_vacuous_note(lanes, threshold, combined))
    if verdict == "open":
        return QuestionAssessment(question_id=spec.question_id, status="open",
                                  authority=_weakest(lanes), mass_interval=combined,
                                  blockers=blockers, notes=tuple(notes))
    return QuestionAssessment(
        question_id=spec.question_id, status="answered", authority=_weakest(lanes),
        mass_interval=combined,
        answer=(("zc_compatibility", "compatible" if verdict == "yes" else "incompatible"),),
        blockers=blockers, notes=tuple(notes))


def _mismatch_assessed(spec, scene_payload, prior) -> QuestionAssessment:
    """TestModelMismatch: answers only when an explicit mismatch/unknown-model competitor
    exists among the matched candidates (identity == "unknown"). A fitless arena or an absent
    identity classifier is never posterior evidence FOR mismatch."""
    params = dict(spec.predicate.parameters)
    threshold = float(params["min_mass"])
    match = match_subject(spec.subject, scene_payload)
    if not match.cluster_indices:
        return QuestionAssessment(question_id=spec.question_id, status="open",
                                  notes=(("reason", "no_matched_evidence"),))
    candidates = _candidates_by_ref(scene_payload)
    refs = set(match.scan_refs)
    explicit = any(row.get("identity") == "unknown"
                   for ref in refs for row in candidates.get(ref, {}).values())
    if not explicit:
        return QuestionAssessment(
            question_id=spec.question_id, status="open",
            blockers=(Blocker("identity_hypotheses_unavailable",
                              "no explicit unknown-model competitor in the matched support"),),
            notes=(("reason", "no_explicit_mismatch_competitor"),))

    def qualifies(alternative):
        for scan_ref, label in alternative.get("assignment") or ():
            if scan_ref in refs:
                row = candidates.get(scan_ref, {}).get(label)
                if row is not None and row.get("identity") == "unknown":
                    return True
        return False

    intervals, lanes, blockers = _proposition_intervals(
        match, scene_payload, qualifies, spec.predicate.authority_requirement)
    combined = combine_existential(intervals)
    verdict = decide(combined, threshold)
    notes = _vacuous_note(lanes, threshold, combined)
    if verdict == "open":
        return QuestionAssessment(question_id=spec.question_id, status="open",
                                  authority=_weakest(lanes), mass_interval=combined,
                                  blockers=blockers, notes=notes)
    return QuestionAssessment(question_id=spec.question_id, status="answered",
                              authority=_weakest(lanes), mass_interval=combined,
                              answer=(("mismatch_present", verdict == "yes"),),
                              blockers=blockers)


def _completeness_relative(spec, scene_payload, prior) -> QuestionAssessment:
    """VerifyCompleteness: complete RELATIVE to the frozen coverage grid, exposure floor,
    represented hypotheses, and closed agenda questions — never metaphysical absence.

    Stays OPEN while: any depended-on question is open or unassessed (including the
    auto-opened EstimateParameter questions that never close in v1 — the stated consequence of
    the owner ruling), an in-partition arena is non-scoring or trusted-but-uninterpreted, a
    matched cluster is unresolved, or a matched exposure sits under the frozen floor. The
    persisted visibility annotations are leader-only and are deliberately NOT read — only
    propositions reconstructable over the stored alternatives may answer.
    """
    params = dict(spec.predicate.parameters)
    exposure_floor = float(params["min_effective_exposure"])
    match = match_subject(spec.subject, scene_payload)
    notes, blockers = [], []
    prior = prior or {}

    open_dependencies = []
    for dependency in spec.dependencies:
        assessment = prior.get(dependency)
        if assessment is None or assessment.status == "open":
            open_dependencies.append(dependency)
    if open_dependencies:
        notes.append(("open_dependencies", tuple(open_dependencies)))

    partition_members = set(match.scan_refs)
    trusted_fitless = []
    for obs in scene_payload.get("observations") or ():
        if obs["scan_ref"] in partition_members:
            usable = any(c.get("usable") for c in obs.get("candidates") or ())
            if obs.get("count_trust") == "trusted" and not usable:
                trusted_fitless.append(obs["scan_ref"])
    # Non-scoring arenas block only THIS partition (PR #31 finding K): the arena's exact
    # partition identity is recomputed from its embedded observation with the same
    # implementation the builder stamps (isar.scene.model.partition_key), so a quarantined
    # scan at one temperature never blocks another condition's completeness. An arena with no
    # embedded observation cannot be attributed and stays blocking, conservatively, with the
    # reason visible in the notes.
    t_round = scene_payload.get("condition_t_round")
    non_scoring_here = []
    for entry in scene_payload.get("non_scoring") or ():
        ref = entry.get("scan_ref")
        obs = next((o for o in scene_payload.get("observations") or ()
                    if o["scan_ref"] == ref), None)
        if obs is None:
            non_scoring_here.append(f"{ref} (unattributable: no embedded observation)")
            continue
        pid = partition_id(partition_key(
            obs.get("sample"), tuple((k, v) for k, v in obs.get("conditions") or ()),
            obs.get("lattice_epoch"), str(ref), t_round,
            obs.get("topology_role"), obs.get("path_fingerprint")))
        if pid == spec.subject.partition_id:
            non_scoring_here.append(ref)
    if trusted_fitless:
        notes.append(("uninterpreted_measurements", tuple(sorted(trusted_fitless))))
    if non_scoring_here:
        notes.append(("non_scoring_arenas", tuple(sorted(non_scoring_here))))

    clusters = scene_payload.get("clusters") or ()
    for index in match.cluster_indices:
        lane, _ = cluster_authority(clusters[index])
        if lane == "unresolved":
            blockers.append(Blocker("posterior_truncation_unresolved",
                                    f"cluster {clusters[index].get('cluster_id', index)}"))
    underexposed = [fp["scan_ref"] for fp in match.footprints
                    if fp.get("effective_exposure") is None
                    or fp["effective_exposure"] < exposure_floor]
    if underexposed:
        blockers.append(Blocker("detection_authority_unavailable",
                                f"exposure under the frozen floor at {sorted(underexposed)}"))

    frame_note = ("relative_to", tuple(sorted(str(k) for k, _ in spec.subject.frame)))
    if open_dependencies or trusted_fitless or non_scoring_here or blockers:
        return QuestionAssessment(question_id=spec.question_id, status="open",
                                  blockers=tuple(blockers), notes=(*notes, frame_note))
    return QuestionAssessment(question_id=spec.question_id, status="answered",
                              authority="exact", answer=(("complete", True),),
                              notes=(*notes, frame_note))


# ----------------------------------------------------------------------------
# Dispatch
# ----------------------------------------------------------------------------
#: kind -> (evaluator version, function). The versions feed the agenda derivation manifest —
#: bumping one re-derives assessments without minting new question identities.
EVALUATORS = {
    "coverage_complete": (1, _coverage_complete),
    "structural_presence": (1, _structural_presence),
    "identity_resolved": (1, _identity_resolved),
    "track_connected": (1, _track_connected),
    "parameter_within_tolerance": (2, _parameter_within_tolerance),
    "zc_linear_compatibility": (1, _zc_linear_compatibility),
    "mismatch_assessed": (1, _mismatch_assessed),
    "completeness_relative": (1, _completeness_relative),
}


def evaluate_predicate(spec: QuestionSpec, scene_payload: Mapping, *,
                       prior_assessments: Mapping[str, QuestionAssessment] | None = None
                       ) -> QuestionAssessment:
    """Evaluate one spec against one scene revision payload. Unknown kinds refuse loudly —
    an unevaluable predicate silently reported ``open`` would read as honest patience."""
    entry = EVALUATORS.get(spec.predicate.kind)
    if entry is None:
        raise ValueError(f"no evaluator for predicate kind {spec.predicate.kind!r}; known: "
                         f"{sorted(EVALUATORS)}")
    return entry[1](spec, scene_payload, prior_assessments)
