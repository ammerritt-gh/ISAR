"""The plain words the inter-spectra views show (C-gui-words). Qt-free; no logic beyond ``reason_sentence``.

The GUI shows these sentences, never the backend's codes. A code missing here passes through as
``"<code>: <detail>"``, so a new flag code is visible rather than silent.
"""
from __future__ import annotations

COLUMN_TITLES = {"anchor": "Anchor", "flag": "Flag", "lock": "Lock", "fit": "Inter-spectra fit"}
COLUMN_TIPS = {
    "anchor": "Spectra you trust. An anchored batch grows outward from them.",
    "flag": "Spectra flagged for a person to review. Hover for the reason.",
    "lock": "Spectra whose chosen fit is locked: no batch will refit them.",
    "fit": "The fit ISAR uses for this spectrum: the fast solo fit or an inter-spectra refit.",
}
SEVERITY_WORDS = {"info": "repaired", "warn": "look", "serious": "serious"}
SOURCE_WORDS = {"fast": "Fast solo", "batch": "Batch", "pick": "Your pick", "lock": "Locked"}
CONDITIONAL_ERRORS = "Error bars are conditional on the neighbours and the chosen model."
ERRORS_UNAVAILABLE = "Error bars unavailable for this fit."
UNCALIBRATED = "uncalibrated"
CALIBRATED_SYNTHETIC = "calibrated on synthetic tracks, not on real data"

REASON_WORDS = {
    "score_over_threshold": "The coupling cost passes the flag threshold.",
    "count_changed_by_neighbours": "The number of modes changed because the neighbours favour it.",
    "neighbour_supported_split": "One mode was split in two, because the neighbours show both.",
    "unmatched_branch_mid_window": "A mode appeared in the middle of the energy window with no neighbour to match it.",
    "branch_lost_mid_window": "A mode the neighbours predict in the window is not seen here.",
    "identity_ambiguous": "Which mode is which is ambiguous where two branches cross.",
    "loop_inconsistent": "Mode identities disagree around a loop of neighbours.",
    "unresolved_return": "A mode that faded has come back, and its identity could not be settled.",
    "did_not_settle": "The batch stopped at its sweep cap before the fits settled.",
    "seam_disagreement": "Two spectra, each fitted from a different anchor, disagree where their fronts meet.",
    "stale_locked_fit": "This locked fit is out of date: an input it was made from has changed.",
    "linewidth_change": "The damping of a mode changed a lot.",
    "overdamped_entry": "A mode has become overdamped or near elastic.",
    "t_drift_cumulative": "A mode drifts a lot across the temperature range.",
    "neighbour_precision_unavailable": "A neighbour has no error bar, so its prediction is taken as less certain.",
    "incomplete_comparison": "The comparison with the fast solo fit is incomplete.",
    "release_fit_failed": "The fit with the neighbours removed did not converge, so the cost of coupling is unknown.",
    "errors_unavailable": "Error bars could not be calculated for this fit.",
}


def reason_sentence(code: str, detail: str = "") -> str:
    sentence = REASON_WORDS.get(code)
    if sentence is None:
        return f"{code}: {detail}"
    return f"{sentence} ({detail})" if detail else sentence


# ---- the batch marks on the Analyzer array dock -------------------------------------------------------------
BATCH_WORDS = {"waiting": "waiting", "fitting": "fitting now", "done": "done", "refused": "refused",
               "failed": "failed", "not_reached": "not reached"}
BATCH_TIPS = {
    "waiting": "Queued for this ring or sweep; a worker does not have it yet.",
    "fitting": "A worker has this spectrum now and has not returned it.",
    "done": "Refitted and saved; the new fit replaces the drawn one once ISAR reloads it.",
}
KIND_WORDS = {"anchored": "Anchored batch", "simultaneous": "Simultaneous batch"}
DIRECTION_WORDS = {"q": "spread in q", "T": "spread in T", "qT": "spread in q and T"}
ENDED_WORDS = {"completed": "finished", "stopped": "stopped", "failed": "failed"}


def batch_line(kind: str, direction: str | None, stage: str | None, index: int | None, done: int, total: int,
               ended: str | None, elsewhere: int = 0) -> str:
    """The dock header's sentence about the batch: kind, stage, done of total, members in other scans, how it ended.

    ``stage`` None is the controller's placeholder before the batch's first members event: no stage and no counts
    are read then. The runner reports stage "sweep" with index 0 while a simultaneous batch assigns its first
    sweep, and a ring or sweep never has a number 0 for a person, so it reads as the start of number 1."""
    head = KIND_WORDS.get(kind, kind)
    if kind == "anchored" and direction:
        head += f", {DIRECTION_WORDS.get(direction, direction)}"
    parts = [head]
    if stage is None:
        if ended is None:
            parts.append("starting")
    else:
        if stage in ("ring", "sweep"):
            parts.append(f"{stage} {index}" if index else f"{stage} 1 starting")
        else:
            parts.append("planning" if stage == "plan" else stage)
        parts.append(f"{done} of {total} done")
    if elsewhere:
        parts.append(f"{elsewhere} in other scans")
    if ended is not None:
        parts.append(ENDED_WORDS.get(ended, ended))
    return " · ".join(parts)


def batch_tip(states, ended_reason: str | None = None) -> str:
    """The counts per state for the header's hover; ``states`` maps spectrum key to the runner's state word
    (``held`` is an anchor or a lock the batch never refits)."""
    counts: dict[str, int] = {}
    for state in states.values():
        counts[state] = counts.get(state, 0) + 1
    words = [f"{BATCH_WORDS[s]} {counts[s]}" for s in BATCH_WORDS if counts.get(s)]
    if counts.get("held"):
        words.append(f"{counts['held']} anchors and locks not refitted")
    text = " · ".join(words) or "no members yet"
    return f"{text}\n{ended_reason}" if ended_reason else text
