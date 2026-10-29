"""The Scan(s) inter-spectra fit menu and bar: grouping, the greyed-button rules and the info line.

Qt-free and read-only: it turns the facade's ``FitEntry`` list into what the menu and bar show. The
page's view model calls the writers on the user's clicks, never this module (AGENTS.md hard rule 7).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from isar.workspace_api.inter_spectra import FitEntry, is_flagged
from isar_gui.inter_words import CONDITIONAL_ERRORS, ERRORS_UNAVAILABLE, SEVERITY_WORDS, reason_sentence


@dataclass(frozen=True)
class BatchGroup:
    label: str                       # "Batch 1"
    changed: tuple[FitEntry, ...]
    unchanged: tuple[FitEntry, ...]  # refits that changed nothing: collapsed inside the batch's menu


@dataclass(frozen=True)
class BarState:
    entries: tuple[FitEntry, ...] | None      # None: the history is still being read
    viewed: str                               # the fit id on show
    locked: bool
    menu_off: str | None                      # why the fit menu is disabled; None: enabled
    authoritative_off: str | None             # why Make authoritative is disabled; None: enabled
    lock_text: str                            # "Lock this fit" | "Unlock"
    lock_off: str | None
    waterfall_off: str | None
    waterfall_on: bool
    info: str                                 # one line, elided by the view
    info_tip: str                             # the full text, with the reasons


def group_fits(entries: Sequence[FitEntry]) -> tuple[tuple[FitEntry, ...], tuple[BatchGroup, ...]]:
    """Fast solo entries, then one group per batch in the order the batches were run."""
    solo = tuple(e for e in entries if e.batch_id is None)
    batches: dict[str, tuple[str, list[FitEntry], list[FitEntry]]] = {}
    for entry in entries:
        if entry.batch_id is None:
            continue
        label, changed, unchanged = batches.setdefault(entry.batch_id, (entry.batch_label or entry.batch_id, [], []))
        (changed if entry.changed else unchanged).append(entry)
    return solo, tuple(BatchGroup(label, tuple(c), tuple(u)) for label, c, u in batches.values())


def entry_tip(entry: FitEntry) -> str:
    """Why the fit changed and why it is flagged, one sentence each (the menu tooltip)."""
    lines = [reason_sentence(r.code, r.detail) for r in entry.why_changed]
    if entry.flag is not None:
        lines += [reason_sentence(r.code, r.detail) for r in entry.flag.reasons]
    if entry.stale_text:
        lines.append(entry.stale_text)
    if not lines:
        lines.append("Fitted on its own, with no neighbours." if entry.batch_id is None else "No reason recorded.")
    return "\n".join(lines)


VIEWED = "viewed"    # ``drawn`` default: the viewed fit is the one on the plot
NOT_ON_PLOT = "This fit is not drawn now: set Fit to Fast (Offset layout with several spectra) to act on it."


def bar_state(entries: Sequence[FitEntry] | None, viewed: str, *, drawn: str | None = VIEWED,
              locked: bool = False, locked_fit: str | None = None, waterfall_off: str | None = None,
              waterfall_on: bool = False) -> BarState:
    """``drawn``: the fit id on the plot for this spectrum, 'deep' for its deep solo curve, None for no curve."""
    if drawn == VIEWED:
        drawn = viewed
    if entries is None:
        reading = "Reading this spectrum's fits…"
        return BarState(None, viewed, locked, reading, reading, "Lock this fit", reading,
                        waterfall_off, waterfall_on, reading, reading)
    entry = next((e for e in entries if e.fit_id == viewed), None)
    menu_off = ("This spectrum has no inter-spectra fits yet. Fast solo is the only fit."
                if len(entries) == 1 else None)
    if entry is None:
        authoritative_off = lock_off = "This fit is not recorded for this spectrum."
        info = info_tip = ""
    else:
        on_plot = drawn == entry.fit_id    # the actions act on the viewed fit, so it must be the curve on show
        lock_off = None if locked or on_plot else NOT_ON_PLOT
        if entry.authoritative:
            authoritative_off = "This fit is already the one ISAR uses for this spectrum."
        elif entry.stale:
            authoritative_off = "This fit is stale: its inputs changed since it was made; run a new batch."
        elif locked:
            authoritative_off = f"Locked to {locked_fit}; unlock first."
        else:
            authoritative_off = None if on_plot else NOT_ON_PLOT
        info, info_tip = _info(entry, locked, drawn)
    return BarState(tuple(entries), viewed, locked, menu_off, authoritative_off,
                    "Unlock" if locked else "Lock this fit", lock_off, waterfall_off, waterfall_on, info, info_tip)


def _info(entry: FitEntry, locked: bool, drawn: str | None) -> tuple[str, str]:
    if drawn is None:    # the viewed fit is not on the plot, so no line may describe it as shown
        return "No fit is shown", "No fit is shown"
    if drawn == "deep":
        return "Showing the Deep solo fit", "Showing the Deep solo fit"
    parts = [f"Showing {entry.label}", "authoritative" if entry.authoritative else "not authoritative"]
    if locked:
        parts.append("locked")
    if entry.stale:
        parts.append("stale")
    if is_flagged(entry.flag):
        parts.append(f"flagged {SEVERITY_WORDS[entry.flag.severity]}")
    if not entry.errors_available:
        parts.append(ERRORS_UNAVAILABLE)
    elif entry.conditional_errors:
        parts.append(CONDITIONAL_ERRORS)
    info = " · ".join(parts)
    return info, f"{info}\n{entry_tip(entry)}"
