"""The Processing dock's inter-spectra box as plain state (C-gui-state, C-gui-words). Qt-free.

``box_state`` says what each control shows and why it is greyed; the box widget only applies it. A Start is built
by ``anchored_request`` / ``simultaneous_request``, the same requests the previews were asked for, so the controller
re-reads exactly what the user was shown.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from isar.workspace_api.inter_spectra import BatchPreview, BatchRequest
from isar.workspace_api.scan_keys import scan_prefix

if TYPE_CHECKING:               # the queue controller imports Qt; the annotation alone needs it here
    from isar_gui.queue_controller import LinkStatus

DIRECTIONS = ("q", "T", "qT")
NO_EXPERIMENT = "Open an experiment first."
NO_ANCHORS = "Add at least one anchor first: select scans in the scan list, then add them here."
TOO_FEW_SELECTED = "A simultaneous batch needs at least two selected spectra."
ADD_OFF = "Select scans in the scan list that are not anchors yet and have a fast-pass result."
REMOVE_OFF = "Select one or more anchors in the list first."
WORKING = "Working out what this batch would do…"
PREVIEW_FAILED = "This batch's preview could not be read"
CANNOT_START = "This batch cannot start."
NO_SCANS_TO_LINK = "Select scans in the scan list first."


@dataclass(frozen=True)
class Control:
    enabled: bool
    reason: str | None           # why it is greyed; None when enabled


@dataclass(frozen=True)
class BoxState:
    anchored: Control            # Start anchored batch
    simultaneous: Control        # Start simultaneous batch on the selection
    add: Control                 # Add selected scans as anchors
    remove: Control              # Remove selected anchors
    anchored_line: str           # the preview's line and notes, verbatim ("" until there is a preview)
    simultaneous_line: str
    note: str                    # a failed read, in plain words ("" when none)
    link: Control = Control(False, None)   # Link modes
    link_line: str = ""          # what the last link-modes item did, in the service's words ("" when none)


def anchored_request(direction: str, anchors: Sequence[str]) -> BatchRequest:
    return BatchRequest(kind="anchored", direction=direction, anchors=tuple(anchors), selection=())


def simultaneous_request(selection: Sequence[str]) -> BatchRequest:
    return BatchRequest(kind="simultaneous", direction=None, anchors=(), selection=tuple(selection))


def link_scans(selection: Sequence[str]) -> tuple[str, ...]:
    """The scans the selected spectra belong to, each once: what one Link modes item names."""
    return tuple(dict.fromkeys(scan_prefix(key) for key in selection))


def link_state(*, bound: bool, blocked: str | None, n_scans: int) -> Control:
    if not bound:
        return Control(False, NO_EXPERIMENT)
    if blocked:
        return Control(False, blocked)
    return Control(True, None) if n_scans else Control(False, NO_SCANS_TO_LINK)


def link_text(status: LinkStatus | None) -> str:
    """The link line: items waiting and running, then the newest settled item's words, as the service gave them."""
    if status is None:
        return ""
    lines = []
    if status.n_running or status.n_waiting:
        lines.append(f"Link modes: {status.n_running} running, {status.n_waiting} waiting.")
    if status.last:
        lines.append(f"Last link modes: {status.last}")
    return "\n".join(lines)


def wanted_requests(*, bound: bool, direction: str, anchors: Sequence[str],
                    selection: Sequence[str]) -> tuple[BatchRequest, ...]:
    """The previews the box shows: an anchored one once there are anchors, a simultaneous one from two spectra."""
    if not bound:
        return ()
    wanted = []
    if anchors:
        wanted.append(anchored_request(direction, anchors))
    if len(selection) >= 2:
        wanted.append(simultaneous_request(selection))
    return tuple(wanted)


def preview_text(preview: BatchPreview | None) -> str:
    if preview is None:
        return ""
    return "\n".join(line for line in (preview.line, *preview.notes) if line)


def _start(*reasons: str | None, preview: BatchPreview | None, error: str | None = None) -> Control:
    """The first reason that holds says why Start is greyed; the preview's own refusal comes after the rules."""
    for reason in reasons:
        if reason:
            return Control(False, reason)
    if preview is None:
        return Control(False, f"{PREVIEW_FAILED}: {error}" if error else WORKING)
    if not preview.can_start:
        return Control(False, preview.reason or CANNOT_START)
    return Control(True, None)


def _gate(bound: bool, ok: bool, off: str) -> Control:
    if not bound:
        return Control(False, NO_EXPERIMENT)
    return Control(True, None) if ok else Control(False, off)


def box_state(*, bound: bool, blocked: str | None, n_anchors: int, n_selected: int, n_addable: int,
              n_selected_anchors: int, anchored: BatchPreview | None, simultaneous: BatchPreview | None,
              note: str | None, anchored_error: str | None = None,
              simultaneous_error: str | None = None, n_selected_scans: int = 0,
              link_status: LinkStatus | None = None) -> BoxState:
    """``anchored`` / ``simultaneous`` are the previews for the requests ``wanted_requests`` names, else None;
    ``*_error`` is the text of a read of that request that raised, shown in place of the working text."""
    unbound = None if bound else NO_EXPERIMENT
    return BoxState(
        anchored=_start(unbound, blocked, NO_ANCHORS if n_anchors == 0 else None, preview=anchored,
                        error=anchored_error),
        simultaneous=_start(unbound, blocked, TOO_FEW_SELECTED if n_selected < 2 else None, preview=simultaneous,
                            error=simultaneous_error),
        add=_gate(bound, n_addable > 0, ADD_OFF),
        remove=_gate(bound, n_selected_anchors > 0, REMOVE_OFF),
        anchored_line=preview_text(anchored),
        simultaneous_line=preview_text(simultaneous),
        note=note or "",
        link=link_state(bound=bound, blocked=blocked, n_scans=n_selected_scans),
        link_line=link_text(link_status),
    )
