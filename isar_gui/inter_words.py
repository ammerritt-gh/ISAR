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
