"""The numerical core's boundary types (C-snapshot, C-refit), data only.

Stdlib only: ``Factor`` (engine) and ``Term`` (graph) are string annotations, so importing this module
pulls no numerics and no engine. S1U1 writes the first version with the fields the plan fixes; S1U2 fills
the core that produces and reads them.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from isar.analyze.coupled.settings import InterSettings

if TYPE_CHECKING:
    from isar.analyze.coupled.engine import Factor
    from isar.session.inter_spectra.graph import Term

FLAG_CODES = ("score_over_threshold", "count_changed_by_neighbours", "neighbour_supported_split",
              "unmatched_branch_mid_window", "branch_lost_mid_window", "identity_ambiguous", "loop_inconsistent",
              "unresolved_return", "did_not_settle", "seam_disagreement", "stale_locked_fit", "linewidth_change",
              "overdamped_entry", "t_drift_cumulative", "neighbour_precision_unavailable", "incomplete_comparison",
              "release_fit_failed", "errors_unavailable")
SEVERITIES = ("info", "warn", "serious")
SEVERITY_OF = {
    "score_over_threshold": "serious", "count_changed_by_neighbours": "warn",
    "neighbour_supported_split": "info", "unmatched_branch_mid_window": "warn",
    "branch_lost_mid_window": "warn", "identity_ambiguous": "serious", "loop_inconsistent": "serious",
    "unresolved_return": "serious", "did_not_settle": "serious", "seam_disagreement": "warn",
    "stale_locked_fit": "serious", "linewidth_change": "warn", "overdamped_entry": "warn",
    "t_drift_cumulative": "warn", "neighbour_precision_unavailable": "info", "incomplete_comparison": "warn",
    "release_fit_failed": "warn", "errors_unavailable": "info",
}


@dataclass(frozen=True)
class HeldMode:                    # one mode of a held fit
    branch: str                    # persistent branch id (S1U3); never the energy order
    omega0: float; gamma: float; height: float
    guard: float | None            # frozen uncertainty guard of omega0, meV (spec 1); None = unavailable
    guard_source: str              # "release" | "max" | "solo" | "unavailable" (a conditional sigma alone is never a guard)
    status: str                    # "determined_by_own_data" | "held_by_neighbours" | "poorly_determined"
    regime: str                    # the fast pass's Regime word: "near_elastic" | "underdamped" | "overdamped"
    pinned: bool                   # an explicit user energy pin (existing fixed-coordinate mechanism); a lock never implies one


@dataclass(frozen=True)
class Dormant:                     # a branch carried by the fit's history but not observed in it ("not observed here")
    branch: str; last_seen_fit: str; last_seen_unit: str


@dataclass(frozen=True)
class Held:                        # a fit as a neighbour (or the spectrum's own incoming state) is read
    unit: str; fit: str; inputs: str | None        # spectrum key; "fast" or an f-id; inputs_hash (N1)
    window: tuple[float, float]                    # (L, H) = that spectrum's engine.sp.bounds("omega0"), meV
    modes: tuple[HeldMode, ...]                    # ascending omega0
    dormant: tuple[Dormant, ...]
    T: float | None                                # K, the node's temperature in force


@dataclass(frozen=True)
class TermInput:
    term: Term                     # C-graph: kind, members, spacings, t_frac
    held: tuple[Held, ...]         # every member of the term other than the spectrum, in term.members order


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str               # runner-minted: "<batch>:ring<r>", "<batch>:round<n>" (stage 1) or "<batch>:sweep<s>" (stage 2)
    stage_id: str                  # the stage-1 or ring snapshot whose Assignment governs this refit (== snapshot_id in stage 1 and in rings)
    unit: str
    fit_id: str                    # the id this refit will be saved under, minted by the runner (records.new_fit_id())
    context: object                # file_fit.fit_context(prepared): the spectrum's own prepared fast-pass context
    own: Held                      # its incoming fit (the one it starts from), with branch ids
    own_record: dict               # that fit's stored fast-pass-shaped record (start modes, saved solo alternatives)
    terms: tuple[TermInput, ...]   # every active term containing unit (anchored: its conditioner change terms only)
    settings: InterSettings
    batch_kind: str                # "anchored" | "simultaneous"
    fast: Held | None = None       # the fast solo fit, what delta_fast and the width/regime flags compare with (default: own)
    aliases: Mapping[str, str] = field(default_factory=dict)   # the batch's merged ids so far, alias -> canonical name (identity.merged_aliases)


@dataclass(frozen=True)
class Assignment:                  # the frozen discrete decision of one spectrum for one stage (spec 0, 3)
    unit: str; snapshot_id: str    # the stage-1 (or ring) snapshot it was decided on
    count: int
    slots: tuple[str | None, ...]  # per mode, ascending start energy: matched branch id, or None = unmatched
    origins: tuple[str, ...]       # per mode: "matched" | "new_branch" | "returning" | "unresolved"
    factors: tuple[Factor, ...]    # frozen definitions of every factor incident on unit (engine.Factor); the runner replaces them with the stage's canonical set before the continuous stage (S1U6 E18)
    discrete: str                  # "priced" | "skipped"
    triggers: tuple[str, ...]      # trigger codes that fired, e.g. "a:missing_branch", empty when skipped
    candidates: tuple[dict, ...]   # evaluated {count, slots, ln_z, status, weight, loss, loss_nats} (loss: [[branch, expected_meV, deviance], ...], loss_nats = -sum(deviance)/2, 0.0 if none): ln_z the deduplicated mass the decision weighed, weights conditional on the evaluated set
    coverage: dict                 # {"counts_tried","counts_untried","enumeration_truncated","missing_candidate_history","full_search"}
    search_limited: bool           # spec 3 assignment_search_limited
    changed: bool                  # count or association differs from snapshot.own AND won by the evidence rule (spec 5)
    reasons: tuple[str, ...]       # missing-evidence and failure reasons of the comparison
    selected_model: tuple | None = None   # the winner's Priced.mode (elastic, background, qe, delta mask, theta in slot order); None: the incoming fit's


@dataclass(frozen=True)
class PricingJob:                  # one composition at one count, with all its starts (the expensive unit of work)
    unit: str; snapshot_id: str; count: int; slots: tuple[str | None, ...]; key: str   # key: hash of everything the price depends on


@dataclass(frozen=True)
class Priced:
    key: str; count: int; slots: tuple[str | None, ...]
    ln_z: float | None             # Laplace log evidence incl. the composition log weight, the variant weight and the loss price of the branches the candidate does not name; None = no_evidence
    status: str                    # base engine status words ("ok", "ok_boundary", "no_evidence", "degenerate_twin", ...); "undetermined": no valid price because the parameters stay undetermined (ln_z None, reason names the cause)
    mode: tuple | None             # posterior-mode parameters, labelled
    reason: str | None; starts: int; seconds: float
    basins: tuple = ()             # (term, model, modes, qe, mode) per priced basin (refit._basin_record): slot orders deduplicated; a basin priced by the split integral has the model label suffix "|split" and records its pair at the mean energy and width with H/2 each
    loss: tuple = ()               # ((branch, expected_meV, deviance), ...) sorted by branch: every eligible in-window neighbour branch the candidate does not name, its expected energy and its price L (-L/2 nats, already inside ln_z and every basin term); () = none missing. A function of the snapshot and the slot multiset only; set even when the candidate is unpriced


@dataclass(frozen=True)
class RefitResult:
    unit: str; snapshot_id: str
    status: str; reason: str | None                # "accepted" | "unchanged" | "rejected" | "failed" (spec 5 order)
    assignment: Assignment
    fit: dict | None                                # the fitted model in the fast-pass record shape
    modes: tuple[HeldMode, ...]                     # with branch ids and this refit's guards
    D: float | None; J: float | None                # data deviance; block objective J_i
    P: Mapping[str, float]; U: Mapping[str, float]  # paid potential / unlevelled mismatch, per factor_id
    unary: float                                    # this block's 2*ramp + B at the solution
    energies: Mapping[str, float]                   # branch id -> omega0 at the solution
    R: float | None; R_signed: float | None; F: float | None   # release regret (clipped, signed), flag score F = R + max U
    delta_fast: dict                                # signed change from the fast solo: {"count", "omega0": {branch: dE}}
    flag: dict                                      # {"score","flagged","reasons":[{"code","severity","text","ids","values"}]}  (N2)
    why_changed: str                                # one sentence (N2)
    overlay: dict | None; overlay_unavailable: str | None      # N3; reason when no fitted curve exists
    covariance: object | None; errors: str          # "conditional" | "unavailable"
    normalisers: Mapping[str, tuple[float, str]]    # "ln_z:<branch>" -> (value, "ln meV"), "q_U:<m>" -> (value, "1")
    starts: int; timing: Mapping[str, float]        # assign_s, likelihood_s, continuous_s, uncertainty_s, release_s
    priced: tuple[Priced, ...]                      # the prices this refit used or made (the runner's cache, remedy 1)
    cache_hits: int
