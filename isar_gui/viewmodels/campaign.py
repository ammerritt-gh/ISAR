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
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from PySide6.QtCore import Signal

from isar_gui.viewmodels.base import ViewModel

log = logging.getLogger(__name__)

#: Held statuses on results rows (mirror of the engine's exception vocabulary, display-only).
_HELD_PREFIX = "held"


@dataclass(frozen=True)
class FeedItem:
    """One operator-facing line of the campaign feed."""

    ts: float
    kind: str
    text: str
    severity: str   # "info" | "notice" | "warn" | "terminal"


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
    scans = 0
    for rec in events:
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
        return CampaignState(spent_neutrons=neutrons, spent_seconds=seconds, scan_count=scans)

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
            last_scan = _scan_text(kind, p)
        if kind == "activity_start" and p.get("activity_id"):
            activities[str(p["activity_id"])] = {**p, "started_ts": rec.get("ts")}
        elif kind == "activity_progress" and p.get("activity_id"):
            activity_id = str(p["activity_id"])
            if activity_id in activities:
                activities[activity_id].update(p)
        elif kind == "activity_end" and p.get("activity_id"):
            activities.pop(str(p["activity_id"]), None)
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
        budget_kind=budget.get("kind"), budget_amount=budget.get("amount"))


def _fmt_hkl(hkl) -> str:
    try:
        return "(" + ", ".join(f"{float(x):g}" for x in hkl) + ")"
    except (TypeError, ValueError):
        return str(hkl)


def _fmt_count(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    for scale, suffix in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
        if abs(number) >= scale:
            return f"{number / scale:.3g}{suffix}"
    return f"{number:g}"


def _scan_text(kind: str, p: dict) -> str | None:
    """The operator line for a propose/run payload — shared by the live feed and the reducer's
    ``last_scan`` so both surfaces show the identical text for the same record."""
    it = p.get("iter")
    tail = f"  [iter {it}]" if it is not None else ""
    if kind == "propose":
        return (f"propose q={_fmt_hkl(p.get('hkl'))} E∈[{p.get('e_lo')}, {p.get('e_hi')}] "
                f"n={_fmt_count(p.get('ncount'))} ({p.get('kind', '?')}){tail}")
    if kind == "run":
        return f"scan {p.get('job_id', '?')} submitted (cost {_fmt_count(p.get('cost'))} n){tail}"
    return None


def render_feed_item(rec: dict) -> FeedItem | None:
    """One-line operator rendering of a session-log record; ``None`` = keep out of the feed."""
    kind = rec.get("kind", "?")
    p = rec.get("payload") or {}
    ts = float(rec.get("ts") or 0.0)
    it = p.get("iter")
    tail = f"  [iter {it}]" if it is not None else ""
    if kind == "timing":
        return None                       # ~16/iteration profiling spans — noise in a feed
    if kind in ("propose", "run"):
        return FeedItem(ts, kind, _scan_text(kind, p), "info")
    if kind == "validate":
        ok = p.get("would_queue")
        text = "validate: ok" if ok else f"validate: blocked {p.get('blockers') or '(unspecified)'}"
        return FeedItem(ts, kind, text + tail, "info" if ok else "warn")
    if kind == "analyze":
        text = f"analyzed {p.get('n_rows', 0)} row(s): {p.get('dispositions')}{tail}"
        return FeedItem(ts, kind, text, "info")
    if kind == "decide":
        action = p.get("action", "?")
        if action == "campaign_start":
            src = (p.get("source") or {}).get("kind")
            resumed = (p.get("resumed") or {}).get("n_observations")
            text = f"campaign started ({src or 'unknown source'}"
            text += f", resuming {resumed} observation(s))" if resumed else ")"
            return FeedItem(ts, kind, text, "notice")
        if action == "operator_stop":
            return FeedItem(ts, kind, f"stop requested — honoring at the iteration boundary{tail}",
                            "notice")
        if action == "model_update":
            text = (f"model update: {p.get('converged')}/{p.get('n_grid')} converged, "
                    f"{p.get('n_observations')} obs{tail}")
            return FeedItem(ts, kind, text, "info")
        return FeedItem(ts, kind, str(action).replace("_", " ") + tail, "info")
    if kind == "escalate_fit":
        q = f" at {_fmt_hkl(p.get('hkl'))}" if p.get("hkl") is not None else ""
        text = (f"fit escalated {p.get('from_tier')}→{p.get('to_tier')} "
                f"{q} ({p.get('phase', p.get('outcome', '?'))})")
        return FeedItem(ts, kind, text + tail, "info")
    if kind == "activity_start":
        stage = str(p.get("stage", "activity")).replace("_", " ")
        q = f" at {_fmt_hkl(p.get('q'))}" if p.get("q") is not None else ""
        return FeedItem(ts, kind, f"{stage} started{q}", "notice")
    if kind == "activity_progress":
        return None
    if kind == "activity_end":
        stage = str(p.get("stage", "activity")).replace("_", " ")
        return FeedItem(ts, kind, f"{stage}: {p.get('status', 'completed')}",
                        "warn" if p.get("status") in ("failed", "interrupted") else "info")
    if kind == "exception":
        return FeedItem(ts, kind, f"{p.get('action', 'exception')}{tail}", "warn")
    if kind == "stop":
        reasons = p.get("reasons")
        text = f"campaign {p.get('state', '?')}: " + "; ".join(
            str(r) for r in (reasons if isinstance(reasons, list) else []))
        return FeedItem(ts, kind, text, "terminal")
    return FeedItem(ts, kind, str(kind), "info")   # unknown kinds surface, never vanish silently


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


def current_activity_text(state: CampaignState, *, now: float | None = None) -> str:
    activity = state.active_activity
    if not activity:
        return "Now: campaign idle"
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
    if completed is not None and total is not None and unit:
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

    def __init__(self, ws, parent=None):
        super().__init__(ws, parent)
        self._seen = 0
        self._alive = False
        self.state = CampaignState()
        self.scan_verdicts: dict[str, int] = {}
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
        if not changed or "results" in changed:
            self._recount_verdicts()
        self.status_changed.emit()
        self.refreshed.emit()

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
