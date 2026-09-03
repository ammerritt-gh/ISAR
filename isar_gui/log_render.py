"""log_render — session-log events as operator-facing one-liners (presentation only).

The driver's ``report/session_log.jsonl`` is an audit trail first: most of its records exist so a
decision can be reconstructed months later from fingerprints, candidate ids and artifact refs. An
operator watching a beamtime needs the opposite — a short sentence per event saying what the machine
is doing right now. This module is the mapping between the two, and nothing else:

* it **renders**, it never interprets science. Every number here is copied out of a payload the
  engine already wrote; no value is recomputed, rescaled or fitted (G19 / the Qt-free boundary — the
  GUI reads science through ``workspace_api`` and never writes it).
* it **hides** the audit-trail and profiling kinds from the dock (:data:`HIDDEN_KINDS`). The JSONL on
  disk is untouched and complete; hiding is a view filter, so an operator who needs the detail still
  has the file, and every displayed row can expand to its own raw payload.
* it **never raises**. A malformed or unknown record renders a fallback line rather than taking the
  campaign dock down mid-run; anything skipped is logged at debug level.

The vocabulary of ``kind`` values lives with the emitter (``isar/drive/session_log.py``); this module
deliberately treats it as open — a kind added upstream shows up as ``event: <kind>`` until someone
writes it a sentence here.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: Audit-trail kinds: the machinery behind a decision (fingerprints, candidate ids, artifact refs).
#: Real, kept on disk, and useless as a running commentary — so they stay out of the dock.
AUDIT_KINDS = frozenset({
    "branch_track_update", "branch_reassignment", "physical_submission",
    "pooled_master_update", "candidate_update", "certification_crossing", "recovery_pending",
    "recovery_completed", "submission_intent",
})

#: Profiling kinds — ~16 spans per iteration. They are how the run is tuned, not what it is doing.
PROFILING_KINDS = frozenset({"timing", "analysis_clock", "activity_progress"})

#: Everything the dock drops.
HIDDEN_KINDS = AUDIT_KINDS | PROFILING_KINDS

#: Kinds that are always shown, always styled loud, and never removed by a feed filter: the two
#: events that mean "a human may need to look at this".
LOUD_KINDS = frozenset({"exception", "escalate_fit"})


@dataclass(frozen=True)
class RenderedEvent:
    """One operator-facing line, plus the record it came from (for click-to-expand)."""

    ts: float
    kind: str
    text: str
    severity: str                       # "info" | "notice" | "warn" | "terminal"
    loud: bool = False
    payload: dict = field(default_factory=dict)

    def raw_json(self) -> str:
        """The originating payload, pretty-printed — what an expanded row shows."""
        return format_payload(self.payload)


def format_payload(payload) -> str:
    """Pretty-print a payload for the expanded view; never raises on odd values."""
    try:
        return json.dumps(payload, indent=2, sort_keys=True, default=str, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        log.debug("session-log payload could not be serialized for display: %s", exc)
        return repr(payload)


# ---- small formatters ---------------------------------------------------------------
def _num(value, spec: str = "g"):
    try:
        return format(float(value), spec)
    except (TypeError, ValueError):
        return None


def q_text(hkl) -> str:
    """A reciprocal-space point as an operator reads it.

    A path that varies in one index only (the common case) reads as ``H=2.15``; anything else keeps
    the full triple so the line can never be mistaken for a different point.
    """
    try:
        coords = [float(value) for value in hkl]
    except (TypeError, ValueError):
        return str(hkl)
    if len(coords) == 3:
        moving = [i for i, value in enumerate(coords) if value != 0.0]
        if len(moving) == 1:
            return f"{'HKL'[moving[0]]}={coords[moving[0]]:g}"
    return "(" + ", ".join(f"{value:g}" for value in coords) + ")"


def count_text(value) -> str:
    """A neutron count in operator units: ``10M``, ``1.2G``."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    for scale, suffix in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
        if abs(number) >= scale:
            return f"{number / scale:.3g}{suffix}"
    return f"{number:g}"


def duration_text(seconds) -> str:
    """A duration an operator can act on: ``45 s``, ``12 min``, ``3 h 05 m``."""
    # ``OverflowError`` is caught alongside the conversion errors because ``int(inf)`` raises it,
    # and this runs inside an unguarded Qt slot: an infinite budget must degrade to a printed
    # "inf", never stop the campaign feed.
    try:
        total = max(float(seconds), 0.0)
        if total < 1.0:
            return "<1 s"                 # never round a real wait down to a bare "0 s"
        if total < 90.0:
            return f"{total:.0f} s"
        if total < 3600.0:
            return f"{total / 60.0:.0f} min"
        hours, rest = divmod(int(total), 3600)
    except (TypeError, ValueError, OverflowError):
        return str(seconds)
    return f"{hours} h {rest // 60:02d} m"


def _phrase(value, fallback: str = "?") -> str:
    """``branch_track_update`` -> ``branch track update``; a machine token read aloud."""
    if value is None:
        return fallback
    return str(value).replace("_", " ").strip() or fallback


def _iter_tail(payload: dict) -> str:
    it = payload.get("iter", payload.get("iteration"))
    return f"  [iter {it}]" if it is not None else ""


def _clip(text, limit: int = 120) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---- per-kind renderings ------------------------------------------------------------
def scan_text(kind: str, payload: dict) -> str | None:
    """The operator line for a ``propose`` / ``run`` payload.

    Shared with the reducer's ``last_scan`` so the "Now:" panel and the feed cannot disagree about
    the same record.
    """
    tail = _iter_tail(payload)
    if kind == "propose":
        bits = []
        points = payload.get("n_points")
        if points is not None:
            bits.append(f"{points} points")
        lo, hi = _num(payload.get("e_lo"), ".2f"), _num(payload.get("e_hi"), ".2f")
        if lo is not None and hi is not None:
            bits.append(f"E {lo}…{hi} meV")
        if payload.get("ncount") is not None:
            bits.append(f"{count_text(payload['ncount'])} neutrons")
        detail = " — " + ", ".join(bits) if bits else ""
        at = f" at {q_text(payload['hkl'])}" if payload.get("hkl") is not None else ""
        return f"Planning a {_phrase(payload.get('kind'), 'scan')} scan{at}{detail}{tail}"
    if kind == "run":
        bits = []
        if payload.get("submitted_ncount") is not None or payload.get("ncount") is not None:
            bits.append(count_text(payload.get("submitted_ncount", payload.get("ncount")))
                        + " neutrons")
        eta = payload.get("eta")
        if isinstance(eta, dict) and eta.get("estimated_seconds") is not None:
            bits.append("expected " + duration_text(eta["estimated_seconds"]))
        detail = " — " + ", ".join(bits) if bits else ""
        state = str(payload.get("state") or "submitted")
        verb = "Submitted" if state == "queued" else _phrase(state).capitalize()
        at = (f" at {q_text(payload['hkl'])}" if payload.get("hkl") is not None else "")
        return f"{verb} scan {payload.get('job_id', '?')}{at}{detail}{tail}"
    return None


def _render_validate(payload: dict, tail: str) -> tuple[str, str]:
    role = payload.get("candidate_role")
    alternative = role is not None and role != "legacy_uniform"
    blockers = payload.get("blockers") or []
    joined = ", ".join(_phrase(b) for b in blockers) if isinstance(blockers, list) else _phrase(blockers)
    if payload.get("would_queue"):
        feasible = payload.get("feasible_points")
        requested = payload.get("requested_points")
        detail = (f" — {feasible} of {requested} points reachable"
                  if feasible is not None and requested is not None else "")
        if alternative:
            return f"Alternative window ({_phrase(role)}) feasible{detail}{tail}", "info"
        return f"Instrument check passed{detail}{tail}", "info"
    if alternative:
        return (f"Alternative window ({_phrase(role)}) not available — "
                f"{joined or 'reason unspecified'}{tail}", "info")
    return f"Instrument check blocked — {joined or 'reason unspecified'}{tail}", "warn"


def _render_analyze(payload: dict, tail: str) -> tuple[str, str]:
    rows = payload.get("n_rows", 0)
    dispositions = payload.get("dispositions")
    verdicts = (", ".join(_phrase(d) for d in dispositions)
                if isinstance(dispositions, list) and dispositions else "no verdict")
    job = f" from scan {payload['job_id']}" if payload.get("job_id") else ""
    plural = "" if rows == 1 else "s"
    return f"Analysed {rows} result{plural}{job} — {verdicts}{tail}", "info"


def _render_decide(payload: dict, tail: str) -> tuple[str, str]:
    action = payload.get("action")
    if action == "campaign_start":
        objective = payload.get("objective") or {}
        source = (payload.get("source") or {}).get("kind")
        budget = objective.get("budget") or {}
        bits = []
        path = objective.get("path")
        if isinstance(path, list) and path:
            bits.append(f"{len(path)} points")
        if budget.get("amount") is not None:
            amount = (count_text(budget["amount"]) if budget.get("kind") == "neutrons"
                      else duration_text(budget["amount"]))
            bits.append(f"budget {amount} {_phrase(budget.get('kind'), '')}".strip())
        resumed = (payload.get("resumed") or {}).get("n_observations")
        if resumed:
            bits.append(f"resuming {resumed} observation(s)")
        detail = " — " + ", ".join(bits) if bits else ""
        return f"Campaign started from a {_phrase(source, 'saved objective')}{detail}", "notice"
    if action == "operator_stop":
        return f"Stop requested — finishing the current scan first{tail}", "notice"
    if action == "model_update":
        return (f"Dispersion model updated — {_phrase(payload.get('converged'), '—')} of "
                f"{_phrase(payload.get('n_grid'), '—')} points certified, "
                f"{_phrase(payload.get('n_observations'), '—')} observation(s){tail}"), "info"
    return f"Decision: {_phrase(action)}{tail}", "info"


def _render_escalate_fit(payload: dict, tail: str) -> tuple[str, str]:
    at = f" at {q_text(payload['hkl'])}" if payload.get("hkl") is not None else ""
    if payload.get("phase") == "outcome" or payload.get("outcome") is not None:
        # The outcome record carries only the tier that was attempted (no ``from_tier``).
        outcome = _phrase(payload.get("outcome"), "finished")
        severity = "warn" if outcome in ("failed", "superseded") else "info"
        tier = _phrase(payload.get("to_tier"), "")
        which = f" {tier}" if tier else ""
        return f"The{which} refit{at} {outcome}{tail}", severity
    tiers = f"{_phrase(payload.get('from_tier'))} → {_phrase(payload.get('to_tier'))}"
    reason = _phrase(payload.get("reason"), "")
    because = f" — {reason}" if reason else ""
    return f"Refitting more carefully{at}: {tiers}{because}{tail}", "notice"


def _render_exception(payload: dict, tail: str) -> tuple[str, str]:
    action = _phrase(payload.get("action"), "exception")
    # Most emitters write ``error``, a minority ``reason``; without both the operator sees the
    # action slug and nothing about what actually failed.
    reason = payload.get("reason") or payload.get("error")
    because = f" — {_clip(reason)}" if reason else ""
    if payload.get("severity") == "low":
        return f"Note: {action}{because}{tail}", "info"
    return f"Problem: {action}{because}{tail}", "warn"


def _render_stop(payload: dict) -> tuple[str, str]:
    reasons = payload.get("reasons")
    joined = "; ".join(_clip(r) for r in reasons) if isinstance(reasons, list) else ""
    detail = f" — {joined}" if joined else ""
    return f"Campaign stopped: {_phrase(payload.get('state'))}{detail}", "terminal"


def _render_scan_design(payload: dict, tail: str) -> tuple[str, str]:
    basis = _phrase(payload.get("selection_basis"), "unrecorded basis")
    shadow = payload.get("shadow_status")
    note = f" (shadow scoring {_phrase(shadow)})" if shadow else ""
    return f"Scan window settled — {basis}{note}{tail}", "info"


def _render_inventory(payload: dict, tail: str) -> tuple[str, str]:
    cause = payload.get("terminal_cause")
    if cause:
        return f"No usable next action — {_phrase(cause)}{tail}", "warn"
    evaluated = payload.get("n_evaluations")
    eligible = payload.get("n_eligible")
    at = (f" at {q_text(payload['selected_hkl'])}" if payload.get("selected_hkl") is not None
          else "")
    chosen = _phrase(payload.get("selected_action_class"), "an action")
    by = (f" via {_phrase(payload.get('selected_generator'))}"
          if payload.get("selected_generator") else "")
    return (f"Weighed {_phrase(evaluated, '—')} candidate action(s), {_phrase(eligible, '—')} "
            f"usable — chose {chosen}{at}{by}{tail}"), "info"


def _render_eig_plan(payload: dict, tail: str) -> tuple[str, str]:
    """The one line that says why this scan and not another.

    Everything is copied from the ``eig_plan`` payload the planner seam writes: how well the chosen
    point's target energy is known now, the design that was bought, the runner-up it beat, and how
    much of the prize is already inside the bar.
    """
    tolerance = _num(payload.get("tolerance_meV"), ".3g")
    bar = f" \u00b1{tolerance} meV" if tolerance is not None else ""
    # The planner sets a note when no design is expected to complete a point and it is taking the
    # cheapest probe. That is the operator\'s cue that the map or the exposure ladder is wrong, so it
    # rides on the line and is loud.
    note = str(payload.get("note") or "")
    suffix = f" \u2014 {note}" if note else ""
    severity = "warn" if note else "notice"
    scale = _intensity_scale_text(payload)
    if payload.get("stop"):
        return (f"EIG: hypothesis map at{bar} at every point \u2014 done{tail}", "notice")
    chosen = payload.get("chosen")
    if not isinstance(chosen, dict):
        return f"EIG \u2014 no design was chosen{suffix}{tail}", "warn"
    at = q_text(chosen.get("hkl")) if chosen.get("hkl") is not None else None
    lo, hi = _num(chosen.get("e_lo"), ".2f"), _num(chosen.get("e_hi"), ".2f")
    points, ncount = chosen.get("n_points"), chosen.get("ncount")
    if at is None or lo is None or hi is None or points is None or ncount is None:
        # Say so plainly rather than assemble a line out of "?" pieces: a design record missing its
        # own window or exposure is a record to go and look at, not a scan to describe.
        return (f"EIG \u2014 a design was chosen but its record carries no window or exposure"
                f"{suffix}{tail}", "warn")
    known, identified = _chosen_point(payload, chosen)
    known = _num(known, ".2f")
    prefix = f"\u00b1{known} \u2192 " if known is not None else ""
    # A MEASURED point can sit inside the bar on width and still not be answered, because the
    # mass is spread over rival identities, and saying only the half-width there reads as done
    # when it is not. A point with no candidate yet is not held back by identity -- it has no
    # identity to be unsure of -- and 'identified 0.00' on every opening scan trains the operator
    # to ignore the clause exactly where it matters.
    held = _num(identified, ".2f")
    if (known is not None and held is not None and float(identified) < 0.9
            and _chosen_n_candidates(payload, chosen)):
        prefix = f"\u00b1{known} but identified {held} \u2192 "
    score = _num(_per_million(chosen.get("score")), ".3g")
    rate = f", {score} nats/1e6 n" if score is not None else ""
    ranked = [row for row in (payload.get("ranked") or []) if isinstance(row, dict)]
    runner_up = ""
    if len(ranked) > 1:
        second = ranked[1]
        second_score = _num(_per_million(second.get("score")), ".3g")
        runner_up = (f" \u2014 runner-up {q_text(second.get('hkl'))}"
                     + (f" {second_score}" if second_score is not None else ""))
    total = payload.get("prize_points_total")
    within = payload.get("prize_points_within_bar")
    prize = (f" \u2014 prize {within}/{total} within{bar}"
             if total is not None and within is not None else "")
    # The dose the instrument is asked for and the price the planner ranked on are different
    # numbers: the price carries the fixed cost of taking a scan at all, and without it a
    # cheap-looking design that lost looks like a ranking bug.
    dose = _num(chosen.get("neutrons"), ".2g")
    price = _num(chosen.get("price"), ".2g")
    cost = f" = {dose} n" if dose is not None else ""
    cost += f", price {price}" if price is not None else ""
    return (f"EIG \u2192 {prefix}{at}: {lo}\u2013{hi} meV, {points} pts \u00d7 "
            f"{count_text(ncount)}{cost}{rate}{runner_up}{prize}{scale}{suffix}{tail}", severity)


def _intensity_scale_text(payload: dict) -> str:
    """The intensity map, said once when it changes: what the forward model thinks a scan will see.

    Only on change — it is the same three numbers every iteration otherwise, and a feed that repeats
    them teaches an operator to stop reading the line.
    """
    scale = payload.get("intensity_scale")
    if not payload.get("intensity_scale_changed") or not isinstance(scale, dict):
        return ""
    reference = _num(scale.get("per_ncount"), ".0e") or "1e7"
    bits = [f"{name} {_num(value, '.3g')}"
            for name, value in sorted((scale.get("peak_counts_meV") or {}).items())
            if _num(value, ".3g") is not None]
    for name, key in (("elastic", "elastic_counts_meV"), ("bg", "background_per_point")):
        value = _num(scale.get(key), ".3g")
        if value is not None:
            bits.append(f"{name} {value}")
    return ("  |  intensity map: " + ", ".join(bits) + f" /{reference}"
            if bits else "")


def _chosen_point(payload: dict, chosen: dict):
    """``(half-width, identified mass)`` for the chosen point, from the per-point rollup."""
    point = _chosen_per_point(payload, chosen)
    if point is None:
        return None, None
    return point.get("halfwidth_meV"), point.get("identified_mass")


def _chosen_per_point(payload: dict, chosen: dict):
    """The chosen design's own row in the per-point rollup, or ``None``."""
    index = chosen.get("grid_index")
    for point in (payload.get("per_point") or []):
        if isinstance(point, dict) and point.get("grid_index", index) == index:
            if point.get("hkl") == chosen.get("hkl"):
                return point
    return None


def _chosen_n_candidates(payload: dict, chosen: dict) -> int:
    """How many modes have been fitted at the chosen point; 0 means never measured."""
    point = _chosen_per_point(payload, chosen) or {}
    try:
        return int(point.get("n_candidates") or 0)
    except (TypeError, ValueError):
        return 0


def _per_million(score):
    """Information per neutron read in the unit an operator can hold: nats per million neutrons."""
    try:
        return float(score) * 1e6
    except (TypeError, ValueError):
        return None


def _render_activity(kind: str, payload: dict) -> tuple[str, str]:
    stage = _phrase(payload.get("stage"), "activity")
    at = f" at {q_text(payload['q'])}" if payload.get("q") is not None else ""
    if kind == "activity_start":
        return f"Started {stage}{at}", "notice"
    status = _phrase(payload.get("status"), "completed")
    severity = "warn" if status in ("failed", "interrupted") else "info"
    return f"Finished {stage}{at} — {status}", severity


def render_event(record) -> RenderedEvent | None:
    """One operator line for a session-log record; ``None`` means "not for the dock".

    Never raises: an unrenderable record degrades to ``event: <kind>`` and is logged at debug level,
    because a campaign dock that dies on an unexpected payload is worse than a vague line.
    """
    if not isinstance(record, dict):
        log.debug("session-log record was not an object (%r) — skipped", type(record))
        return None
    kind = str(record.get("kind", "?"))
    if kind in HIDDEN_KINDS:
        return None
    payload = record.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    try:
        ts = float(record.get("ts") or 0.0)
    except (TypeError, ValueError):
        log.debug("session-log record for kind %r carried a non-numeric ts — shown as 0", kind)
        ts = 0.0

    try:
        text, severity = _render(kind, payload)
    except Exception as exc:                      # presentation must not kill the dock
        log.warning("could not render session-log event %r (%s: %s) — showing the raw kind",
                    kind, type(exc).__name__, exc)
        text, severity = f"event: {kind}", "info"
    return RenderedEvent(ts=ts, kind=kind, text=text, severity=severity,
                         loud=kind in LOUD_KINDS, payload=payload)


def _render(kind: str, payload: dict) -> tuple[str, str]:
    tail = _iter_tail(payload)
    if kind in ("propose", "run"):
        return scan_text(kind, payload) or f"event: {kind}", "info"
    if kind == "validate":
        return _render_validate(payload, tail)
    if kind == "analyze":
        return _render_analyze(payload, tail)
    if kind == "decide":
        return _render_decide(payload, tail)
    if kind == "escalate_fit":
        return _render_escalate_fit(payload, tail)
    if kind == "exception":
        return _render_exception(payload, tail)
    if kind == "stop":
        return _render_stop(payload)
    if kind == "scan_design":
        return _render_scan_design(payload, tail)
    if kind == "inventory":
        return _render_inventory(payload, tail)
    if kind == "eig_plan":
        return _render_eig_plan(payload, tail)
    if kind in ("activity_start", "activity_end"):
        return _render_activity(kind, payload)
    if kind == "feasibility_trim":
        return _render_feasibility_trim(payload, tail)
    if kind == "scan_landed":
        return _render_scan_landed(payload, tail)
    # Unknown kinds surface rather than vanish: the log vocabulary grows upstream, and an operator
    # seeing "event: something_new" can still expand the row for the payload.
    return f"event: {kind}", "info"


def _render_scan_landed(payload: dict, tail: str) -> tuple[str, str]:
    """The scan is on disk and in the ledger; the fit has not started. Read before any
    "Started … fit at q" line for the same q — that ordering is the point of the event."""
    at = f" at {q_text(payload['q'])}" if payload.get("q") is not None else ""
    n = payload.get("n_points")
    lo, hi = _num(payload.get("e_lo"), ".2f"), _num(payload.get("e_hi"), ".2f")
    window = f", E {lo}…{hi} meV" if lo is not None and hi is not None else ""
    points = f", {int(n)} points" if isinstance(n, (int, float)) else ""
    return f"Scan landed{at}{points}{window}{tail}", "notice"


def _render_feasibility_trim(payload: dict, tail: str) -> tuple[str, str]:
    dropped = payload.get("dropped_point_count")
    lo, hi = _num(payload.get("new_e_lo"), ".2f"), _num(payload.get("new_e_hi"), ".2f")
    window = (f"; scanning E {lo}…{hi} meV" if lo is not None and hi is not None else "")
    plural = "" if dropped == 1 else "s"
    count = dropped if dropped is not None else "some"
    return (f"Trimmed {count} unreachable point{plural} from the scan window{window}{tail}",
            "notice")


# ---- synthesized budget status ------------------------------------------------------
#: The synthetic kind stamped on budget lines (they come from campaign state, not from the log).
BUDGET_KIND = "budget_status"


def budget_status_line(state) -> str | None:
    """A one-line budget readout, or ``None`` when nothing is bounded.

    ``state`` is any object carrying the campaign-state attributes (``budget_kind``,
    ``budget_amount``, ``spent_neutrons``, ``spent_seconds``, ``analysis_limit_seconds``,
    ``analysis_spent_seconds``) — duck-typed so this presentation module stays independent of the
    viewmodel that owns them.

    Time remaining is always shown when a time ledger is bounded; neutrons appear only under a
    neutron budget, because an unbounded neutron count has no "remaining" to report.
    """
    parts: list[str] = []
    kind = getattr(state, "budget_kind", None)
    amount = getattr(state, "budget_amount", None)
    try:
        if amount:
            amount = float(amount)
            if kind == "neutrons":
                spent = float(getattr(state, "spent_neutrons", 0.0) or 0.0)
                parts.append(f"{count_text(max(amount - spent, 0.0))} of {count_text(amount)} "
                             "neutrons left")
            else:
                spent = float(getattr(state, "spent_seconds", 0.0) or 0.0)
                parts.append(f"{duration_text(max(amount - spent, 0.0))} of "
                             f"{duration_text(amount)} measurement time left")
        limit = getattr(state, "analysis_limit_seconds", None)
        if limit:
            limit = float(limit)
            spent = float(getattr(state, "analysis_spent_seconds", 0.0) or 0.0)
            parts.append(f"{duration_text(max(limit - spent, 0.0))} of {duration_text(limit)} "
                         "analysis time left")
    except (TypeError, ValueError, OverflowError) as exc:
        # ``OverflowError``: a non-finite budget reaches ``int()`` inside ``duration_text``. This is
        # called from an unguarded Qt slot, so an escape here freezes the campaign feed mid-run.
        log.debug("budget status could not be formed from the campaign state: %s", exc)
        return None
    if not parts:
        return None
    return "Budget — " + "; ".join(parts)


class BudgetAnnouncer:
    """Decides *when* a budget line is worth repeating into the feed.

    Two triggers, whichever comes first: wall-clock ``interval_seconds`` since the last line, or
    ``every_rows`` feed rows appended since. An unchanged line is suppressed, so a campaign sitting
    idle on a neutron budget does not repeat the same sentence forever.
    """

    def __init__(self, interval_seconds: float = 120.0, every_rows: int = 20):
        self.interval_seconds = float(interval_seconds)
        self.every_rows = int(every_rows)
        self._last_ts: float | None = None
        self._rows_since = 0
        self._last_text: str | None = None

    def reset(self) -> None:
        self._last_ts = None
        self._rows_since = 0
        self._last_text = None

    def maybe_line(self, state, *, now: float, rows: int = 0) -> str | None:
        """Register ``rows`` newly appended feed rows and return a budget line when one is due."""
        self._rows_since += max(int(rows), 0)
        due = (self._last_ts is None
               or (now - self._last_ts) >= self.interval_seconds
               or self._rows_since >= self.every_rows)
        if not due:
            return None
        text = budget_status_line(state)
        self._last_ts = now
        self._rows_since = 0
        if text is None or text == self._last_text:
            return None
        self._last_text = text
        return text
