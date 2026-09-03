"""The stopping criterion + deterministic convergence core (LLM_HARNESS_DESIGN §2.1).

Teixeira Parente et al. (2022) name the stopping criterion as the untested gap in autonomous TAS; the
driver makes it a first-class, testable output. This module holds the *deterministic* core (no LLM):
path bookkeeping, per-grid-q convergence, and the overall ``met`` / ``incomplete`` /
``infeasible`` / ``continue``
decision. The LLM supervisor consumes the decision; it does not compute it.

**Per-grid-q convergence.** A published, internally consistent pooled master must carry a reliable
model-robust ω interval within tolerance and, for named targets, confirmed track identity. Individual
fit intervals and verdicts never vote. Γ may remain resolution-limited when ω is identifiable after
model/Γ marginalization.

**Overall decision.** ``met`` means every requested master certified. A global measurement budget,
analysis limit, or iteration limit yields ``incomplete``. ``infeasible`` is reserved for independently
established instrument/geometry/calibration impossibility or a tolerance below the declared floor.
Poor fits, low information, ambiguity, and repeated scans are diagnostics; they never abandon q.
"""
from __future__ import annotations

import math
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

MAX_DECLINES = 3              # compatibility/diagnostic threshold; never a stop condition
#: Margin an interval must clear before its first crossing counts (the certification crossing guard).
FIRST_CROSSING_MARGIN = 1.2
_HKL_TOL = 1e-4              # r.l.u. tolerance for matching an observation to a path grid point
_BIG = float("inf")

# The neutron counts of the escalation ladder (LLM_HARNESS_DESIGN §8): quick-look → production.
QUICKLOOK_NCOUNT = int(1e7)
PRODUCTION_NCOUNT = int(1e8)

PRODUCTION_KINDS = frozenset({"production", "confirm"})


# --------------------------------------------------------------------------- path geometry
def path_scalars(path) -> list[float]:
    """Cumulative arclength (r.l.u.) of each point along ``path`` — the model's monotone q-axis.

    For a straight H-segment this reduces to ``|h − h_start|``; it is monotone for any monotone segment.
    """
    out = [0.0]
    for i in range(1, len(path)):
        a, b = path[i - 1], path[i]
        out.append(out[-1] + math.dist(a, b))
    return out


def match_grid_index(hkl, path, *, tol: float = _HKL_TOL) -> int | None:
    """The index of the path grid point nearest ``hkl`` (within ``tol`` r.l.u.), or ``None``."""
    best_i, best_d = None, _BIG
    for i, p in enumerate(path):
        d = math.dist(p, hkl)
        if d < best_d:
            best_i, best_d = i, d
    if best_i is not None and best_d <= tol:
        return best_i
    return None


# --------------------------------------------------------------------------- per-q convergence core
# A fit whose ω₀ is usable for an ω₀-tolerance objective. `flagged` (model_incomplete) is INCLUDED:
# at low q the 1-D-resolution model has a structured residual (the tas4d ceiling) so the fit flags,
# but ω₀ and its interval are still good — the flag documents peak-shape imperfection, not an ω₀
# error, and the systematic floor guards accuracy. `resolved` and `flagged` both carry a usable ω₀.
USABLE_DISPOSITIONS = frozenset({"resolved", "flagged"})

# Historical strategy threshold retained for policy parsing and diagnostics only.
MAX_PROD_ATTEMPTS = 8         # compatibility/diagnostic threshold; never a stop condition


# --------------------------------------------------------------------------- grid summary
def _finite_or_none(value) -> float | None:
    """``value`` as a finite float, else ``None`` — non-numeric ledger cells never raise here."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _candidate_energies(update: dict) -> list[float] | None:
    """Sorted finite candidate energies, or ``None`` when the update carries no candidate table.

    ``None`` is not an empty set: a legacy update that predates the column says nothing about the
    candidates, and treating that as "no candidates" would make it incomparable with every update
    that has them.
    """
    candidates = update.get("branch_candidates")
    if candidates is None:
        return None
    energies = []
    for candidate in candidates:
        value = _finite_or_none((candidate or {}).get("energy_meV",
                                                      (candidate or {}).get("omega0")))
        if value is not None:
            energies.append(value)
    return sorted(energies)


def _identity_context(update: dict) -> tuple:
    """The non-candidate half of the identity question: calibration, hypotheses, slice, epoch.

    ``identity_epoch`` is the *target selection* identity, carried as a discrete counter by the
    producer. A selection flip is a changed answer rather than a changed candidate set, and it must
    still reset the saturation window — the newest revisions are about a different target — but naming
    it separately keeps "the candidate basis changed" meaning only what it says.

    ``model_selection_family`` is deliberately absent: no producer has a model-family field to supply,
    so including it was a permanently-``None`` clause that guaranteed nothing.
    """
    return (
        update.get("calibration_version"),
        update.get("hypotheses_fingerprint"),
        update.get("campaign_slice_fingerprint"),
        update.get("identity_epoch"),
    )


def _identity_comparable(update: dict, reference: dict, *, scale: float) -> bool:
    """Whether two identity updates answer the same scientific *question*, so their metrics may be
    compared across growing exposure to decide counting has stopped paying.

    Comparability is a matched predicate rather than a hashable key. Keying on a quantized energy has
    boundary instability — jitter across a bin edge changes the answer while nearly a full tolerance
    of drift inside one bin does not — so candidates are *paired* instead and each pair must have moved
    less than ``scale``. With equal counts on a line, sorted pairing is the minimum-cost matching, so
    no assignment algorithm is needed.

    Keying on the candidate *count* alone (the previous rule) let three windows of two entirely
    different modes read as one comparable question. The *answer* stays excluded — ``branch_id``,
    ``target_probability``, ``identity_entropy`` and the advancing revision ids are exactly what
    saturation measures the movement of, so keying on them would reset the window every observation.
    """
    if _identity_context(update) != _identity_context(reference):
        return False
    mine, theirs = _candidate_energies(update), _candidate_energies(reference)
    if mine is None or theirs is None:
        return True                     # legacy updates say nothing about candidates; stay comparable
    if len(mine) != len(theirs):
        return False
    tolerance = abs(float(scale)) if math.isfinite(float(scale)) else 0.0
    return all(abs(a - b) <= tolerance for a, b in zip(mine, theirs))


@dataclass
class GridQ:
    """A per-grid-point rollup the acquisition function and the stopping criterion both consume.

    The published combine-before-divide **master** is the sole scientific authority. Physical scan
    rows are retained only for acquisition cost/provenance; their fitted numbers and verdicts never
    affect ω, branch identity, convergence, or stopping.
    """

    index: int
    hkl: tuple[float, float, float]
    q: float
    tolerance: float
    n_production: int = 0          # production scans taken at this q (each grows the pool)
    n_quicklook: int = 0
    # Retained zero-valued compatibility fields for report readers predating master authority. The
    # reducer never increments them and no scientific decision reads them.
    n_decline: int = 0
    n_infeasible: int = 0
    n_unmodeled: int = 0
    n_branch_ambiguous: int = 0
    n_uncertifiable: int = 0
    saw_uncertifiable_gamma: bool = False   # ≥1 uncertifiable delivery was γ-at-bound (flagged interval)
    saw_uncertifiable_fast: bool = False    # ≥1 uncertifiable delivery stayed on the untrusted fast tier
    counts_spent: float = 0.0      # accumulated monitor-count budget (Σ proposal_cost) at this q (Phase 5)
    last_prod_cost: float | None = None  # most recent production scan's cost (ΔC proxy for the next)
    last_submitted_ncount: int | None = None
    submitted_exposure: float = 0.0
    last_fit_kind: str | None = None     # provenance of the most recent usable fit (5b grounding gate)
    best_omega0: float | None = None
    best_halfwidth: float | None = None
    best_resolved: bool = False
    best_prod_halfwidth: float | None = None  # compatibility only; never populated or consulted
    best_prod_omega0: float | None = None
    master: dict | None = None     # {"omega0","halfwidth","reliable","usable","disposition","n_members"}
    evidence_master: dict | None = None  # pooled evidence even when target identity abstains
    requires_crossing_guard: bool = False
    requires_confirmed_track: bool = False
    crossing_fingerprints: list[str] = field(default_factory=list)
    master_updates: list[dict] = field(default_factory=list)
    evidence_updates: list[dict] = field(default_factory=list)
    # Chronological positions in the durable observation stream.  These are decision-local
    # bookkeeping, not a second scientific authority: they let acquisition prove that a same-q
    # identity revisit was unlocked by a *newer* neighboring pooled-master revision.
    production_sequences: list[int] = field(default_factory=list)
    identity_revisit_sequences: list[int] = field(default_factory=list)
    evidence_update_sequences: list[int] = field(default_factory=list)
    clamped_productions: int = 0
    clamped_effective_ncounts: list[int] = field(default_factory=list)
    interval_floor_window: int = 3
    interval_floor_min_improvement: float = 0.05
    interval_floor_min_exposure_ratio: float = 2.0
    track_state: str | None = None
    track_revision: int | None = None
    structural_endpoint_status: str | None = None
    is_reduced_q_origin: bool = False
    # Whether the campaign's target branch is acoustic.  An acoustic branch has ω(q→0) → 0, so at
    # the reduced-q origin the excitation is degenerate with the elastic line and there is nothing
    # separable to measure, however many neutrons are spent.  An optic branch has a finite ω at the
    # zone centre and is perfectly measurable there, so this cannot key on the origin alone.
    acoustic_target: bool = False
    # Local branch identity, carried verbatim from the tracker's own confirmation predicate
    # (``branch_tracking._identity_states``) — whether this q's identity is decisive HERE, which is
    # what decides whether more counting can help.  Read from the tracker rather than re-derived
    # from ``branch_posterior``: the driver deliberately clears that posterior for exactly the
    # ambiguous case this has to distinguish.
    target_probability: float | None = None
    identity_entropy: float | None = None
    local_identity_clear: bool | None = None
    identity_updates: list[dict] = field(default_factory=list)
    identity_floor_window: int = 3
    identity_floor_min_improvement: float = 0.05
    identity_floor_min_exposure_ratio: float = 2.0

    @property
    def visited(self) -> bool:
        return (not self.precision_coverage_eligible or self.evidence_master is not None
                or self.n_quicklook > 0 or self.n_production > 0)

    @property
    def master_revision(self) -> str | None:
        m = self.master or {}
        value = m.get("content_hash") or m.get("production_batch_fingerprint")
        return str(value) if value else None

    @property
    def linewidth_state(self) -> str:
        return "resolution_limited" if (self.master or {}).get("gamma_at_bound") else "resolved"

    @property
    def diagnostics(self) -> list[str]:
        m = self.evidence_master or self.master or {}
        out = []
        if (self.candidate_rejected or m.get("modality") == "fork"
                or self.track_state == "ambiguous"):
            out.append("model_ambiguous")
        if m.get("pool_consistent") is False:
            out.append("pool_inconsistent")
        if m and m.get("model_robust") is False:
            out.append("model_ambiguous")
        if m.get("gamma_at_bound"):
            out.append("linewidth_resolution_limited")
        if m and not m.get("usable"):
            out.append("analysis_failed")
        return list(dict.fromkeys(out))

    @property
    def assessment_state(self) -> str:
        if not self.precision_coverage_eligible:
            return "exempt"
        if self.evidence_master is None:
            return "unmeasured"
        if self.master is None:
            return "ambiguous"
        return "certified" if self.converged else "uncertified"

    @property
    def _master_hw(self) -> float | None:
        m = self.master
        if (not m or not m.get("usable") or m.get("halfwidth") is None
                or m.get("pool_consistent") is False or m.get("modality") == "fork"):
            return None
        # S3: convergence seats only on a CERTIFIABLE interval. `certifiable` (reliable interval AND
        # interval_reliable≠False, or a trusted-HESSE fallback) is authoritative; fall back to the
        # legacy `reliable` flag for pre-S3 master rows that carry no `certifiable` field (additive
        # contract — old results.csv behaves as before).
        cert = m.get("certifiable")
        if cert is None:
            cert = m.get("reliable")
        return float(m["halfwidth"]) if cert else None

    @property
    def cert_halfwidth(self) -> float | None:
        """The certifying pooled-master half-width; physical scans never provide a fallback."""
        return self._master_hw

    @property
    def cert_omega0(self) -> float | None:
        return (self.master or {}).get("omega0") if self._master_hw is not None else None

    @property
    def converged(self) -> bool:
        """A trustworthy pooled-master ω₀ interval is within tolerance."""
        if self.requires_confirmed_track and self.track_state != "confirmed":
            return False
        if self.track_state is not None and self.track_state != "confirmed":
            return False
        hw = self.cert_halfwidth
        if hw is None or hw > self.tolerance:
            return False
        if not self.requires_crossing_guard:
            return True
        fingerprint = (self.master or {}).get("production_batch_fingerprint")
        return bool(fingerprint and self.crossing_fingerprints
                    and self.crossing_fingerprints[-1] != fingerprint)

    @property
    def precision_coverage_eligible(self) -> bool:
        """Whether this q represents a measurable target requiring a certified interval.

        The one exemption, on the geometry alone and from the first iteration: an acoustic branch at
        the reduced-q origin, whose ω(q→0) → 0 is degenerate with the elastic line.  Fitting a DHO
        there returns a centre with an error bar thousands of times larger than itself, so demanding
        a certified interval sends the campaign back forever — three quarters of the scans in the R3
        cold-start campaigns went to that single q.
        """
        return not (self.acoustic_target and self.is_reduced_q_origin)

    @property
    def signal_seen(self) -> bool:
        """Whether this q has positive signal evidence, including an unresolved branch candidate."""
        return bool((self.evidence_master or self.master or {}).get("usable"))

    @property
    def candidate_rejected(self) -> bool:
        return bool(self.master_updates and self.master_updates[-1].get("candidate_state") == "rejected")

def _logical_observations(observations: list[dict], target_branch: str) -> list[dict]:
    """Collapse physical retry/detector rows to one logical outcome per acquisition.

    Raw cost and submitted exposure are summed, while the best target-branch delivery represents the
    logical result. Legacy observations without an acquisition ID remain one-for-one.
    """
    groups: dict[tuple, list[dict]] = {}
    sequence_by_object = {id(observation): sequence
                          for sequence, observation in enumerate(observations)}
    for index, observation in enumerate(observations):
        if observation.get("kind") == "pooled_master_update":
            continue
        if observation.get("kind") == "certification_crossing":
            groups[("crossing", index)] = [observation]
            continue
        acquisition_id = observation.get("acquisition_id")
        key = (("acquisition", str(acquisition_id)) if acquisition_id is not None
               else ("legacy", index))
        groups.setdefault(key, []).append(observation)

    logical: list[dict] = []
    for group in groups.values():
        if group[0].get("kind") == "certification_crossing":
            logical.append(dict(group[0]))
            continue
        requested = [item for item in group
                     if item.get("resolved") and item.get("branch_id") in (None, target_branch)]
        certifiable = [item for item in requested if item.get("certifiable", True)]
        if certifiable:
            representative = min(
                certifiable,
                key=lambda item: (float(item["calibrated_halfwidth"])
                                  if item.get("calibrated_halfwidth") is not None else math.inf))
        elif requested:
            representative = requested[-1]
        else:
            positive = [item for item in group
                        if item.get("disposition") in {"unmodeled", "branch_continuation"}
                        or item.get("verdict") == "branch_continuation"]
            representative = positive[-1] if positive else group[-1]
        folded = dict(representative)
        folded["_observation_sequence"] = max(sequence_by_object[id(item)] for item in group)
        folded["proposal_cost"] = sum(float(item.get("proposal_cost") or 0.0) for item in group)
        folded["submitted_exposure_increment"] = sum(
            float(item.get("submitted_ncount") or 0.0) for item in group
            if float(item.get("proposal_cost") or 0.0) > 0.0)
        submitted = [item.get("submitted_ncount") for item in group
                     if item.get("submitted_ncount") is not None]
        if submitted:
            folded["submitted_ncount"] = submitted[-1]
        logical.append(folded)
    return logical


def summarize_grid(objective, observations: list[dict], master_rows: list[dict] | None = None,
                   branch_track: dict | None = None):
    """One :class:`GridQ` per objective grid point from the per-scan ``observations`` + ``master_rows``.

    Per-scan observations contribute acquisition count/cost provenance only. ``master_rows`` (the
    pooled fits, read fresh each iteration) exclusively provide ω, interval, branch, convergence, and
    diagnostics. Both match to a grid point by nearest hkl; off-grid entries are ignored.
    """
    path = objective.path
    target_branch = getattr(objective, "target_branch", "acoustic") or "acoustic"
    scalars = path_scalars(path)
    crossing_guard = bool(getattr(objective, "hypotheses", None))
    confirmed_track_required = getattr(objective, "target_selector", None) == "tracked_branch"
    def branch_matches(item: dict) -> bool:
        if "branch_id" not in item:
            return True                  # legacy event predating additive branch provenance
        if item.get("branch_id") is None and not crossing_guard:
            return True                  # static/posterior-off campaigns retain v2 behavior
        return item.get("branch_id") == target_branch
    policy = getattr(objective, "steering", None) or {}
    grid = [GridQ(index=i, hkl=tuple(path[i]), q=scalars[i], tolerance=objective.tolerance_meV,
                  requires_crossing_guard=crossing_guard,
                  requires_confirmed_track=confirmed_track_required,
                  interval_floor_window=max(1, int(policy.get("interval_floor_window", 3))),
                  interval_floor_min_improvement=float(policy.get(
                      "interval_floor_min_improvement", 0.05)),
                  interval_floor_min_exposure_ratio=float(policy.get(
                      "interval_floor_min_exposure_ratio", 2.0)),
                  acoustic_target=(target_branch == "acoustic"))
            for i in range(len(path))]
    for sequence, update in enumerate(observations):
        if update.get("kind") != "pooled_master_update" or update.get("hkl") is None:
            continue
        i = match_grid_index(tuple(update["hkl"]), path)
        if i is not None:
            grid[i].evidence_updates.append(dict(update))
            grid[i].evidence_update_sequences.append(sequence)
            # Identity progress is tracked on *evidence*, not on the target-matched master: an
            # identity-ambiguous q never passes ``branch_matches`` and so never reaches
            # ``master_updates``.  That is exactly why interval-based saturation cannot speak for it.
            if (update.get("target_probability") is not None
                    or update.get("identity_entropy") is not None):
                grid[i].identity_updates.append(dict(update))
            if branch_matches(update):
                grid[i].master_updates.append(dict(update))
    logical_observations = _logical_observations(observations, target_branch)
    for obs in logical_observations:
        # A classification probe may deliberately sit off the requested path.  Its branch evidence is
        # retained elsewhere, but it must never seat or penalize a certification point — even when an
        # unusually fine path places it within the nearest-hkl matching tolerance.
        if obs.get("certification_eligible") is False or obs.get("driver_inserted") is True:
            continue
        if obs.get("hkl") is None:
            continue
        i = match_grid_index(tuple(obs["hkl"]), path)
        if i is None:
            continue
        g = grid[i]
        kind = obs.get("kind")
        if kind == "certification_crossing":
            fingerprint = obs.get("production_batch_fingerprint")
            if fingerprint:
                g.crossing_fingerprints.append(str(fingerprint))
            continue
        # Physical observations update only acquisition accounting. Their fitted numbers, branch
        # labels, and verdicts deliberately do not enter this reducer's scientific state.
        cost = float(obs.get("proposal_cost") or 0.0)   # actual submitted scan cost (Phase 5)
        g.counts_spent += cost
        submitted_ncount = obs.get("submitted_ncount")
        if submitted_ncount is not None:
            g.last_submitted_ncount = int(submitted_ncount)
            if cost > 0.0:
                g.submitted_exposure += float(
                    obs.get("submitted_exposure_increment", submitted_ncount))
        if kind in PRODUCTION_KINDS:
            g.n_production += 1
            # ``_logical_observations`` preserves group insertion order.  Locate the latest physical
            # row for this logical acquisition so a later pooled update compares after the scan, not
            # after an arbitrary reducer pass.
            sequence = obs.get("_observation_sequence")
            if sequence is not None:
                g.production_sequences.append(int(sequence))
                if obs.get("acquisition_action_role") == "classification":
                    g.identity_revisit_sequences.append(int(sequence))
            if obs.get("ncount_clamped"):
                g.clamped_productions += 1
                if submitted_ncount is not None:
                    g.clamped_effective_ncounts.append(int(submitted_ncount))
            if cost > 0.0:
                g.last_prod_cost = cost                 # the next production scan's ΔC proxy
        elif kind == "quicklook":
            g.n_quicklook += 1
    for mr in (master_rows or []):
        if mr.get("hkl") is None:
            continue
        i = match_grid_index(tuple(mr["hkl"]), path)
        if i is not None:
            master = dict(mr)
            members = master.get("members") or []
            if members:
                encoded = json.dumps(sorted(str(member) for member in members),
                                     separators=(",", ":"), ensure_ascii=True)
                master["production_batch_fingerprint"] = hashlib.sha256(
                    encoded.encode("utf-8")).hexdigest()
            grid[i].evidence_master = master
            if branch_matches(mr):
                grid[i].master = master
            try:
                master_revision = (int(master["track_revision"])
                                   if master.get("track_revision") is not None else 0)
            except (TypeError, ValueError):
                master_revision = 0
            master_state = master.get("track_state")
            if (master_state in {"confirmed", "ambiguous"}
                    and (grid[i].track_revision is None
                         or master_revision >= grid[i].track_revision)):
                grid[i].track_state = master_state
                grid[i].track_revision = master_revision
                grid[i].structural_endpoint_status = master.get("structural_endpoint_status")
                grid[i].is_reduced_q_origin = bool(master.get("is_reduced_q_origin", False))
                grid[i].target_probability = _finite_or_none(master.get("target_probability"))
                grid[i].identity_entropy = _finite_or_none(master.get("identity_entropy"))
                clear = master.get("local_identity_clear")
                # A ledger predating these columns leaves identity unknown.  Unknown is treated as
                # unclear so the controller counts rather than stops — the safe direction here.
                grid[i].local_identity_clear = None if clear is None else bool(clear)
    track = dict(branch_track or {})
    # The tracker publishes the zone anchor as plain geometry from its first revision, so an
    # acoustic campaign knows on iteration one which q has nothing separable to measure, rather
    # than discovering it after spending most of its beam there.
    origin_index = track.get("reduced_q_origin_index")
    if isinstance(origin_index, int) and 0 <= origin_index < len(grid):
        grid[origin_index].is_reduced_q_origin = True
    return grid


def model_points(grid: list[GridQ]) -> list[tuple[float, float, float]]:
    """Authoritative pooled-master ``(q, omega0, halfwidth)`` triples for the ω(q) model."""
    pts = []
    for g in grid:
        master = g.master or {}
        halfwidth = master.get("halfwidth")
        track_ok = (g.track_state == "confirmed" if g.requires_confirmed_track
                    else g.track_state in (None, "confirmed"))
        if (track_ok and master.get("usable")
                and master.get("omega0") is not None and halfwidth is not None):
            try:
                omega0 = float(master["omega0"])
                numeric_halfwidth = float(halfwidth)
            except (TypeError, ValueError):
                continue
            if not (math.isfinite(omega0) and math.isfinite(numeric_halfwidth)
                    and numeric_halfwidth >= 0.0):
                continue
            pts.append((g.q, omega0, numeric_halfwidth))
    return pts


# --------------------------------------------------------------------------- the decision
@dataclass
class StopDecision:
    """Campaign verdict: ``continue`` | ``met`` | ``incomplete`` | ``infeasible``.

    ``terminal_cause`` is an *additive* refinement that names *why* a terminal was reached, decided by
    the post-acquisition inventory rather than by any stopping-side approximation. In this increment
    the only causes are the inventory's own (``action_inventory_exhausted`` vs ``controller_fault``),
    so it is set only on the ``incomplete`` no-proposal terminal and left ``None`` everywhere else; a
    later taxonomy (see the plan's PR3) pairs further causes with other states. It is omitted from
    ``to_dict`` when ``None`` so existing (cause-less) stop payloads are byte-unchanged.
    """

    state: str
    reasons: list[str] = field(default_factory=list)
    per_q: list[dict] = field(default_factory=list)
    terminal_cause: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "state": self.state, "reasons": list(self.reasons), "per_q": list(self.per_q)}
        if self.terminal_cause is not None:
            payload["terminal_cause"] = self.terminal_cause
        return payload


def systematic_floor(calibration=None, floor: float | None = None) -> float:
    """The tolerance floor: an explicit ``floor``, else the calibration's ``|center_pull_mean|``, else 0.

    Below this the resolution-asymmetry systematic swamps the statistical half-width, so a tighter
    tolerance is physically infeasible by construction.
    """
    if floor is not None:
        return float(floor)
    if calibration is not None:
        cp = getattr(calibration, "center_pull_mean", None)
        if cp is not None and math.isfinite(cp):
            return abs(float(cp))
    return 0.0


def evaluate_stop(objective, grid: list[GridQ], budget_state: dict, *,
                  calibration=None, floor: float | None = None,
                  max_declines: int = MAX_DECLINES) -> StopDecision:
    """Decide ``incomplete`` / ``infeasible`` / ``continue``, and roll the per-q summary up.

    ``met`` is deliberately NOT decided here. The objective is "the hypothesis map at the requested
    precision", and only the EIG planner's prize over the mapped candidates answers that question;
    a stopping-side approximation of it (all eligible q converged) both duplicated the answer and
    could disagree with it. ``isar.drive.loop.run_campaign`` produces ``met`` from
    ``Decision.stop``. What remains here is the tolerance floor, the budget, and the per-q rows
    certification and the report consume.

    Args:
        objective: the :class:`~isar.drive.objective.ObjectiveSpec` (path + tolerance).
        grid: the per-q rollup from :func:`summarize_grid`.
        budget_state: ``{"kind", "amount", "spent"}`` — neutrons or counting-seconds consumed.
        calibration: the loaded σ-calibration (its ``center_pull_mean`` supplies the default floor).
        floor: explicit systematic floor override (meV).
        max_declines: retained only for call compatibility; scan-decline abandonment no longer exists.
    """
    tol = objective.tolerance_meV
    fl = systematic_floor(calibration, floor)
    per_q = []
    for g in grid:
        m = g.master or {}
        evidence = g.evidence_master or m
        if not g.precision_coverage_eligible:
            reason = ("acoustic endpoint inferred at physical reduced-q origin; unresolved by "
                      "construction and excluded from precision coverage")
        elif g.converged:
            reason = "certified master interval meets tolerance"
        elif g.master is None and g.evidence_master is not None:
            reason = "continue — pooled fit evidence exists but target branch identity is unresolved"
        elif g.master is None:
            reason = "continue — no pooled master published"
        elif (g.master or {}).get("pool_consistent") is False:
            reason = "continue — pooled members are inconsistent; review and acquire more evidence"
        elif (g.master or {}).get("model_robust") is False:
            reason = "continue — model/branch uncertainty is unresolved"
        else:
            reason = "continue — master interval remains above tolerance"
        per_q.append({"hkl": list(g.hkl), "q": g.q, "tolerance": g.tolerance,
                      "converged": g.converged,
                      "assessment_state": g.assessment_state,
                      "calibrated_halfwidth": g.cert_halfwidth, "omega0": g.cert_omega0,
                      "master_halfwidth": m.get("halfwidth"), "master_n_members": m.get("n_members"),
                      "master_members": m.get("members"),
                      "master_revision": g.master_revision,
                      "source": "master" if g.master is not None else None,
                      "evidence_master": g.evidence_master is not None,
                      "evidence_branch_id": evidence.get("branch_id"),
                      "evidence_candidate_state": evidence.get("candidate_state"),
                      "track_state": g.track_state, "track_revision": g.track_revision,
                      "structural_endpoint_status": g.structural_endpoint_status,
                      "is_reduced_q_origin": g.is_reduced_q_origin,
                      "precision_coverage_eligible": g.precision_coverage_eligible,
                      "linewidth_state": g.linewidth_state,
                      "pool_consistent": m.get("pool_consistent"),
                      "consistency_pvalue": m.get("consistency_pvalue"),
                      "uncertainty_method": m.get("interval_method"),
                      "expected_next_information": m.get("expected_next_information"),
                      "diagnostics": g.diagnostics,
                      "n_production": g.n_production,
                      "clamped_productions": g.clamped_productions,
                      # The identity trio belongs in the per-q summary, not only in results.csv:
                      # a branch_ambiguity stop is unreadable without knowing whether the label was
                      # undecided (count more) or decided-but-disconnected (counting cannot help).
                      "target_probability": g.target_probability,
                      "identity_entropy": g.identity_entropy,
                      "local_identity_clear": g.local_identity_clear,
                      "n_master_updates": len(g.master_updates),
                      "diagnostic_halfwidth": (g.master_updates[-1].get("diagnostic_halfwidth")
                                               if g.master_updates else None),
                      "reason": reason})

    # Tolerance below the systematic floor is infeasible by construction.
    if tol < fl:
        for status in per_q:
            status["assessment_state"] = "infeasible"
            status["reason"] = "requested tolerance is below the declared systematic floor"
        return StopDecision("infeasible",
                            [f"tolerance {tol:g} meV is below the resolution-asymmetry systematic "
                             f"floor {fl:g} meV (calibration center_pull_mean) — unreachable"], per_q)

    eligible = [g for g in grid if g.precision_coverage_eligible]

    reasons: list[str] = []
    spent = float(budget_state.get("spent", 0.0))
    amount = budget_state.get("amount")
    if amount is not None and spent >= float(amount):
        n_unconverged = sum(1 for g in eligible if not g.converged)
        reasons.append(f"budget exhausted ({spent:g}/{amount:g} {budget_state.get('kind', '')}) "
                       f"with {n_unconverged} grid q still under-determined")
        return StopDecision("incomplete", reasons, per_q)

    return StopDecision("continue", [], per_q)
