"""CampaignViewModel — the live campaign feed and status, reduced from the session event log.

Three deliberately separate concerns (the honesty rules of the campaign dashboard):

* **State** comes from a pure reducer over ``ws.session_events()`` scoped to the LATEST campaign
  segment (events after the last ``decide``/``action == "campaign_start"`` marker — the loop logs
  one per invocation, so an append-only log holds many segments). Only ledger-supported
  aggregates are exposed: iteration, converged-of-n-grid, observation count, stop state. Spend is
  summed over the WHOLE log's ``run`` events, matching the engine's own crash-resume accounting
  (``_resume_state`` reads all run events; an operator resuming a campaign keeps spending the
  same budget).
* **Feed rows** are appended via a record-count cursor so each parsed record is rendered exactly
  once — the facade drops a truncated trailing line, so the completed line arrives on a later
  tick and must not double-deliver. A SHRUNKEN log (fresh file) resets the cursor; a same-size
  replacement is indistinguishable by count, outside the append-only single-writer contract, and
  deliberately not detected.
* **Process liveness** comes ONLY from the coordinator's drive_started/drive_finished signals —
  never inferred from the log (a crashed driver leaves no terminal event).

Scan-verdict counts are results.csv-derived (through the facade), labeled as SCAN verdicts —
they are not per-q objective-grid state, which today's event contract cannot honestly supply.

* **Scoring fallback** (SCENE-5 5.2, D7) is read the same way: the latest persisted
  ``report/decisions.jsonl`` line's ``decision_basis.fallback`` block, or ``None`` on a scored
  decision. ``decisions.jsonl`` IS the alert source — no second channel, and the durable ledger
  stays authoritative even if the GUI is never watching.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace

from PySide6.QtCore import Signal

from isar.workspace_api import latest_decision
from isar_gui.log_render import RenderedEvent as FeedItem
from isar_gui.log_render import render_event, scan_text
from isar_gui.viewmodels.base import ViewModel

log = logging.getLogger(__name__)

#: Held statuses on results rows (mirror of the engine's exception vocabulary, display-only).
_HELD_PREFIX = "held"

__all__ = ["CampaignState", "CampaignViewModel", "FeedItem", "analysis_readout", "budget_readout",
           "current_activity_text", "reduce_campaign", "render_feed_item", "terminal_status_text"]


@dataclass(frozen=True)
class CampaignState:
    """Aggregates the current (latest) campaign segment supports, plus whole-log spend."""

    has_campaign: bool = False
    objective: dict | None = None
    source: dict | None = None            # {"kind": goal|spec|resume, "sha256": ...}
    resumed: dict | None = None           # campaign_start's resume echo
    iteration: int | None = None          # max iter seen in the segment (per-invocation numbering)
    last_decision: str | None = None
    n_observations: int | None = None
    converged: int | None = None
    n_grid: int | None = None
    stop_state: str | None = None
    stop_reasons: tuple[str, ...] = ()
    #: The segment's latest propose/run line, rendered identically to the live feed — state-derived
    #: so a consumer built mid-campaign (the lazily-created compact monitor) shows the current scan
    #: immediately instead of waiting for the next feed tick.
    last_scan: str | None = None
    #: The honest grid snapshot behind an operator stop — the loop writes StopDecision.to_dict()
    #: ({state, reasons, per_q}), never a bare string (isar/drive/loop.py's stop payload).
    grid_state: dict | None = None
    per_q: tuple[dict, ...] = ()
    active_activity: dict | None = None
    exception_count: int = 0
    stage_seconds: dict = field(default_factory=dict)
    # Whole-log spend (all segments — the engine's resume accounting reads the same way).
    spent_neutrons: float = 0.0
    spent_seconds: float = 0.0
    scan_count: int = 0
    budget_kind: str | None = None
    budget_amount: float | None = None
    analysis_spent_seconds: float = 0.0
    analysis_limit_seconds: float | None = None
    certification_status: dict | None = None
    certification_end_status: str | None = None


def _is_campaign_start(rec: dict) -> bool:
    return (rec.get("kind") == "decide"
            and (rec.get("payload") or {}).get("action") == "campaign_start")


def reduce_campaign(events: list[dict]) -> CampaignState:
    """Pure reduction of the full event list into a :class:`CampaignState` (unit-testable)."""
    start_idx = None
    for i, rec in enumerate(events):
        if _is_campaign_start(rec):
            start_idx = i

    # Whole-log spend from every run event, mirroring isar.drive.loop._resume_state (do not
    # import it — the sums are trivial and the GUI must not couple to loop internals).
    neutrons = seconds = 0.0
    analysis_operations: dict[str, float] = {}
    scans = 0
    for rec in events:
        if rec.get("kind") == "analysis_clock":
            p = rec.get("payload") or {}
            operation_id = p.get("operation_id")
            if operation_id:
                try:
                    elapsed = max(float(p.get("elapsed_seconds") or 0.0), 0.0)
                    key = str(operation_id)
                    analysis_operations[key] = max(analysis_operations.get(key, 0.0), elapsed)
                except (TypeError, ValueError):
                    log.debug("analysis clock carried non-numeric elapsed %r — skipped",
                              p.get("elapsed_seconds"))
        if rec.get("kind") != "run":
            continue
        p = rec.get("payload") or {}
        scans += 1
        # Malformed ledger values are skipped but never silently: the reducer re-runs per watch
        # tick over the whole log, so a bad record repeats at debug level — deliberate (it would
        # take an engine bug to produce one, and the repetition is the trail).
        try:
            neutrons += float(p.get("cost") or 0.0)
        except (TypeError, ValueError):
            log.debug("run event carried a non-numeric cost %r — skipped in the spend sum", p.get("cost"))
        eta = p.get("eta")
        if isinstance(eta, dict) and eta.get("estimated_seconds") is not None:
            try:
                seconds += float(eta["estimated_seconds"])
            except (TypeError, ValueError):
                log.debug("run event carried a non-numeric estimated_seconds %r — skipped",
                          eta.get("estimated_seconds"))

    if start_idx is None:
        return CampaignState(spent_neutrons=neutrons, spent_seconds=seconds, scan_count=scans,
                             analysis_spent_seconds=sum(analysis_operations.values()))

    start = (events[start_idx].get("payload") or {})
    objective = start.get("objective")
    budget = (objective or {}).get("budget") or {}
    iteration = None
    last_decision = None
    n_obs = converged = n_grid = None
    stop_state = None
    stop_reasons: tuple[str, ...] = ()
    grid_state = None
    per_q: tuple[dict, ...] = ()
    activities: dict[str, dict] = {}
    certification_end_status = None
    last_scan = None
    exceptions = 0
    stage_seconds: dict = {}

    for rec in events[start_idx:]:
        kind = rec.get("kind")
        p = rec.get("payload") or {}
        it = p.get("iter", p.get("iteration"))
        if isinstance(it, (int, float)):
            iteration = int(it) if iteration is None else max(iteration, int(it))
        if kind in ("propose", "run"):
            last_scan = scan_text(kind, p)
        if kind == "activity_start" and p.get("activity_id"):
            activities[str(p["activity_id"])] = {**p, "started_ts": rec.get("ts")}
            if p.get("stage") == "final_certification":
                certification_end_status = None
        elif kind == "activity_progress" and p.get("activity_id"):
            activity_id = str(p["activity_id"])
            if activity_id in activities:
                activities[activity_id].update(p)
        elif kind == "activity_end" and p.get("activity_id"):
            ended = activities.pop(str(p["activity_id"]), None)
            if p.get("stage") == "final_certification" or (
                    ended and ended.get("stage") == "final_certification"):
                certification_end_status = str(p.get("status") or "completed")
        if kind == "decide":
            action = p.get("action")
            if action == "model_update":
                n_obs = p.get("n_observations", n_obs)
                converged = p.get("converged", converged)
                n_grid = p.get("n_grid", n_grid)
                if isinstance(p.get("per_q"), list):
                    per_q = tuple(dict(item) for item in p["per_q"] if isinstance(item, dict))
                last_decision = (f"model update: {'?' if converged is None else converged}"
                                 f"/{'?' if n_grid is None else n_grid} converged, "
                                 f"{'?' if n_obs is None else n_obs} obs")
            elif action == "operator_stop":
                last_decision = "operator stop acknowledged"
            elif action and action != "campaign_start":
                last_decision = str(action).replace("_", " ")
        elif kind == "exception":
            exceptions += 1
            last_decision = f"exception: {p.get('action', '?')}"
        elif kind == "stop":
            stop_state = p.get("state")
            reasons = p.get("reasons")
            stop_reasons = tuple(str(r) for r in reasons) if isinstance(reasons, list) else ()
            grid_state = p.get("grid_state")
            if isinstance(grid_state, dict) and isinstance(grid_state.get("per_q"), list):
                per_q = tuple(dict(item) for item in grid_state["per_q"]
                              if isinstance(item, dict))
        elif kind == "timing":
            stage = p.get("stage")
            try:
                stage_seconds[stage] = stage_seconds.get(stage, 0.0) + float(p.get("seconds") or 0.0)
            except (TypeError, ValueError):
                log.debug("timing event for stage %r carried non-numeric seconds %r — skipped",
                          stage, p.get("seconds"))

    return CampaignState(
        has_campaign=True, objective=objective, source=start.get("source"),
        resumed=start.get("resumed"), iteration=iteration, last_decision=last_decision,
        n_observations=n_obs, converged=converged, n_grid=n_grid, stop_state=stop_state,
        stop_reasons=stop_reasons, grid_state=grid_state, per_q=per_q,
        active_activity=(list(activities.values())[-1] if activities else None),
        last_scan=last_scan,
        exception_count=exceptions,
        stage_seconds=stage_seconds, spent_neutrons=neutrons, spent_seconds=seconds,
        scan_count=scans,
        budget_kind=budget.get("kind"), budget_amount=budget.get("amount"),
        analysis_spent_seconds=sum(analysis_operations.values()),
        analysis_limit_seconds=(objective or {}).get("analysis_limit_seconds"),
        certification_end_status=certification_end_status,
        )


def _fmt_hkl(hkl) -> str:
    try:
        return "(" + ", ".join(f"{float(x):g}" for x in hkl) + ")"
    except (TypeError, ValueError):
        return str(hkl)


def render_feed_item(rec: dict) -> FeedItem | None:
    """One-line operator rendering of a session-log record; ``None`` = keep out of the feed.

    The whole kind→sentence mapping, and the list of audit-trail/profiling kinds the dock hides,
    live in :mod:`isar_gui.log_render`. This is the viewmodel-side name the docks already use.
    """
    return render_event(rec)


def budget_readout(state: CampaignState) -> tuple[int, str]:
    """(permille filled, bar text) for a budget progress bar — shared by dock and monitor.

    Mirrors the engine's own budget dichotomy (isar/drive/loop.py ``budget_state``): kind
    ``"neutrons"`` is the neutron ledger and EVERY other kind (canonically ``counting_seconds``)
    is the seconds ledger — a second GUI-side kind list would just drift from the engine's."""
    if state.budget_amount and state.budget_kind == "neutrons":
        frac = min(state.spent_neutrons / float(state.budget_amount), 1.0)
        return int(frac * 1000), f"budget: {state.spent_neutrons:.3g} / {state.budget_amount:.3g} n"
    if state.budget_amount and state.budget_kind:
        amount_s = float(state.budget_amount)
        frac = min(state.spent_seconds / amount_s, 1.0) if amount_s else 0.0
        if amount_s >= 3600.0:
            return int(frac * 1000), (f"budget: {state.spent_seconds / 3600.0:.2f} / "
                                      f"{amount_s / 3600.0:.2f} h")
        return int(frac * 1000), f"budget: {state.spent_seconds:.0f}s / {amount_s:.0f}s"
    return 0, "budget: —"


def analysis_readout(state: CampaignState) -> tuple[int, str]:
    limit = state.analysis_limit_seconds
    if limit is None:
        return 0, "analysis: no limit"
    limit = float(limit)
    fraction = min(state.analysis_spent_seconds / limit, 1.0) if limit > 0 else 0.0
    return int(fraction * 1000), (f"analysis: {state.analysis_spent_seconds / 60.0:.1f} / "
                                  f"{limit / 60.0:.1f} min")


def terminal_status_text(state: CampaignState) -> str:
    """Stable operator-facing terminal outcome, separate from low-level reason detail."""
    reasons = "; ".join(state.stop_reasons)
    lowered = reasons.lower()
    if state.stop_state == "incomplete":
        if "analysis limit" in lowered:
            return "Incomplete — analysis limit exhausted"
        if "budget" in lowered:
            return "Incomplete — measurement budget exhausted"
        if "iteration" in lowered:
            return "Incomplete — iteration limit exhausted"
        return "Incomplete" + (f" — {reasons}" if reasons else "")
    if state.stop_state == "infeasible":
        return "Infeasible" + (f" — {reasons}" if reasons else "")
    if state.stop_state == "met":
        return "Met — every requested master certified"
    return f"{state.stop_state}: {reasons}" if state.stop_state else "campaign idle"


def current_activity_text(state: CampaignState, *, now: float | None = None) -> str:
    activity = state.active_activity
    if not activity and state.certification_status and not state.certification_status.get("published"):
        activity = {"stage": "final_certification", **state.certification_status}
    if not activity:
        return "Now: " + state.last_scan if state.last_scan else "Now: campaign idle"
    stage = str(activity.get("stage") or "activity")
    labels = {"scan": "Scanning", "fast_fit": "Fast fit", "full_fit": "Full fit",
              "final_certification": "Final certification", "pooled_update": "Pooling"}
    text = labels.get(stage, stage.replace("_", " ").title())
    if activity.get("q") is not None:
        try:
            coords = [float(value) for value in activity["q"]]
            q_text = "(" + ", ".join("0" if value == 0 else f"{value:.2f}"
                                      for value in coords) + ")"
        except (TypeError, ValueError):
            q_text = _fmt_hkl(activity["q"])
        text += f" at {q_text}"
    completed, total = activity.get("completed"), activity.get("total")
    unit = activity.get("progress_unit")
    if stage == "final_certification" and completed is not None and total is not None:
        phase = activity.get("phase")
        if phase == "interrupted":
            return (f"Final certification interrupted — {completed}/{total} "
                    "checkpoints retained")
        text += f" — {completed}/{total} points checkpointed"
        if phase == "publishing":
            text += " — publishing"
        elif activity.get("active"):
            text += f" — {activity['active']} active"
        remaining = activity.get("remaining_analysis_seconds")
        if remaining is not None and phase != "publishing":
            try:
                remaining = max(int(float(remaining)), 0)
            except (TypeError, ValueError):
                log.debug("certification progress carried non-numeric remaining allowance %r — omitted",
                          remaining)
            else:
                text += f" — {remaining // 60:02d}:{remaining % 60:02d} analysis left"
    elif completed is not None and total is not None and unit:
        text += f" — {completed}/{total} {unit}"
    if activity.get("started_ts") is not None:
        elapsed = max(0, int((time.time() if now is None else now)
                             - float(activity["started_ts"])))
        text += f" — {elapsed // 60:02d}:{elapsed % 60:02d} elapsed"
    return "Now: " + text


class CampaignViewModel(ViewModel):
    """Feed + status over the workspace's campaign event log (read-only, facade-backed)."""

    feed_appended = Signal(list)   # list[FeedItem], only records not yet delivered
    feed_reset = Signal()          # the log shrank (fresh file) — clear the view first
    status_changed = Signal()
    #: Emitted only when the scoring-fallback block actually changes (set, cleared, or reason
    #: differs) — a banner listens to this rather than status_changed's noisier every-poll firing.
    scoring_fallback_changed = Signal()
    #: Emitted when the active ``campaign_alert`` changes (raised, escalated, or cleared by the
    #: next successful analysis). Policy A (owner 2026-09-05): the campaign never stops on a
    #: failure it survived; it yells. This is the GUI's end of that yell.
    alert_changed = Signal()

    def __init__(self, ws, parent=None):
        super().__init__(ws, parent)
        self._seen = 0
        self._alive = False
        self.state = CampaignState()
        self.scan_verdicts: dict[str, int] = {}
        self.scoring_fallback: dict | None = None
        self.alert: dict | None = None
        self.on_workspace_changed({"events", "results"})   # initial read of whatever exists

    # ---- process liveness (coordinator-fed, never log-inferred) ------------
    def set_process_alive(self, alive: bool) -> None:
        if self._alive != bool(alive):
            self._alive = bool(alive)
            self.status_changed.emit()

    @property
    def process_alive(self) -> bool:
        return self._alive

    @property
    def terminal_summary(self):
        """The terminal campaign summary view, only once the campaign is truly over.

        ``campaign_summary.json`` is written AFTER the loop ends — it is never a live progress
        source, so it surfaces only when no process is alive and the segment ended in a stop."""
        if self._alive or self.state.stop_state is None:
            return None
        return self._ws.campaign

    # ---- watch fan-in -------------------------------------------------------
    def on_workspace_changed(self, changed=None) -> None:
        changed = set(changed or ())
        if changed and not (changed & {"events", "results", "card_state"}):
            return
        if not changed or "events" in changed or "card_state" in changed:
            self._consume_events()
            self._join_certification_status()
            self._sync_scoring_fallback()
            self._sync_alert()
        if not changed or "results" in changed:
            self._recount_verdicts()
        self.status_changed.emit()
        self.refreshed.emit()

    def _sync_scoring_fallback(self) -> None:
        """Read the latest decision's ``fallback`` block; emit only on an actual change (D7).

        ``latest_decision`` raises loudly on a corrupt last line (a torn write mid-append) — the
        GUI poll must not crash on that, so it degrades to "unknown, keep the prior banner state"
        and logs, matching how the watcher itself treats a misbehaving read (isar/workspace_api/
        watch.py's callback guard).
        """
        try:
            decision = latest_decision(self._ws)
        except ValueError as exc:
            log.warning("could not read the latest discovery decision: %s", exc)
            return
        fallback = (decision or {}).get("decision_basis", {}).get("fallback")
        if fallback != self.scoring_fallback:
            self.scoring_fallback = fallback
            self.scoring_fallback_changed.emit()

    def _sync_alert(self) -> None:
        """The ACTIVE alert: the latest ``campaign_alert`` not yet followed by a successful
        analysis (an ``analyze`` event) or a new campaign start. Cleared, not merely aged out,
        because a banner that outlives the recovery it announced teaches the operator to ignore
        banners. Emits only on change."""
        active = None
        for record in reversed(list(self._ws.session_events())):
            kind = record.get("kind")
            payload = record.get("payload") or {}
            if kind == "analyze" or (kind == "decide"
                                     and payload.get("event") == "campaign_start"):
                break
            if kind == "campaign_alert":
                active = dict(payload)
                break
        if active != self.alert:
            self.alert = active
            self.alert_changed.emit()

    def _join_certification_status(self) -> None:
        status = getattr(self._ws, "certification_status", None)
        if status is None:
            return
        durable = dict(status.raw)
        active = self.state.active_activity
        if (not durable.get("published") and not active
                and self.state.certification_end_status == "interrupted"):
            durable["phase"] = "interrupted"
        if (active and active.get("stage") == "final_certification"
                and durable.get("activity_id") == active.get("activity_id")):
            self.state = replace(
                self.state, active_activity={**active, **durable},
                certification_status=durable)
        else:
            self.state = replace(self.state, certification_status=durable)

    def _consume_events(self) -> None:
        events = list(self._ws.session_events())
        # Reset detection is by COUNT only: a shrunken log (fresh file) restarts the feed. A
        # replacement with the same or more records is indistinguishable here and outside the
        # append-only single-writer contract — deliberately not detected.
        if len(events) < self._seen:
            self._seen = 0
            self.feed_reset.emit()
        fresh = events[self._seen:]
        self._seen = len(events)           # cursor counts PARSED records (facade drops partials)
        self.state = reduce_campaign(events)
        if fresh:
            items = [item for item in map(render_feed_item, fresh) if item is not None]
            if items:
                self.feed_appended.emit(items)

    def feed_snapshot(self) -> list[FeedItem]:
        """Return the currently persisted, renderable feed in chronological order.

        A workspace rebind constructs this viewmodel before its dock.  The constructor's initial
        ``feed_appended`` signal therefore has no receiver; this snapshot lets a late subscriber
        hydrate itself without rewinding the append cursor or duplicating events for other listeners.
        """
        return [item for item in map(render_feed_item, self._ws.session_events())
                if item is not None]

    def _recount_verdicts(self) -> None:
        counts: dict[str, int] = {}
        for fv in self._ws.results:
            if fv.pooled:
                continue                   # scan verdicts: non-pooled rows only, by definition
            status = (fv.status or "").lower()
            if status.startswith(_HELD_PREFIX):
                bucket = "held"
            elif (fv.disposition or "resolved") not in ("resolved",):
                bucket = "flagged"
            else:
                bucket = "ok"
            counts[bucket] = counts.get(bucket, 0) + 1
        self.scan_verdicts = counts
