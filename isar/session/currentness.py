"""Which stored passes of one analyzer file are current, and the plain words of a result's warning.

One predicate, :func:`judge`, serves every caller: the queue item's start (:mod:`isar.session.file_fit`,
on freshly prepared inputs) and the read side (``isar.workspace_api.currentness.current_passes``, on the
stored registration through :func:`stored_now`, in :func:`current_passes`). It is the engine's own carry
rule: ``fast`` is current when ``isar run`` would carry the unit (no force, no deep request) and, for BL43,
its fit-inputs hash is unchanged; ``deep`` when, on top of that, the stored deep record is current for the
queue's request.

Heavy imports (the driver, the deep pass) happen inside the functions: the read side imports this
module at its own import and must stay light.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class UnitNow:
    """What a unit's result would be made from now."""
    content_hash: str
    guidelines_hash: str
    calibration_fingerprint: str | None     # None: the method has none, or the unit is declared out
    inputs_hash: str | None                 # BL43 fit-inputs hash; None outside BL43


class _ScanRef(NamedTuple):
    """The two attributes ``calibrations._scan_state`` reads from a record."""
    experiment_id: str
    scan_number: int


def queue_deep_request():
    """The deep request of the queue and of ``isar run --deep-pass`` without options: ``DeepRequest()``."""
    from isar.session.deep_delivery import DeepRequest
    return DeepRequest()


def deep_record_counts_as_current(record: dict) -> bool:
    """Whether a stored, non-refused deep record under the request's signature counts as current. True:
    the engine's behaviour, search-limited and untrusted records included (operator, 2026-10-27, answer 2:
    no retry, the fixed seed would reproduce them)."""
    return True


def judge(prev, det: str, now: UnitNow | None, request) -> frozenset[str]:
    """The subset of ``{"fast", "deep"}`` with a current stored result for ``prev`` (the ledger entry) and
    analyzer ``det``, against ``now``; ``frozenset()`` for no entry or no ``now``."""
    if prev is None or now is None:
        return frozenset()
    # shortcut: imports the whole driver for one predicate; move _unit_is_fresh here when driver.py is free (it is S1U3's in this slice)
    from isar.session import deep_delivery, driver
    stamp = ((prev.provenance or {}).get("fit_inputs") or {}).get("hash")
    if (driver._unit_is_fresh(prev, det, chash=now.content_hash, ghash=now.guidelines_hash,
                              calfp=now.calibration_fingerprint, force=False, deep_pass=None)
            or (now.inputs_hash is not None and stamp != now.inputs_hash)):
        return frozenset()
    oc = (prev.detectors or {}).get(det)
    record = (oc or {}).get("deep_pass")
    if (isinstance(record, dict) and deep_delivery.is_current(oc, request)
            and deep_record_counts_as_current(record)):
        return frozenset({"fast", "deep"})
    return frozenset({"fast"})


def _state_stands(state: dict, det: str, curves_now) -> bool:
    """Whether the stored scan calibration is, for ``det``'s fit, what a start would compute from the curves
    now (``curves_now``, a LoadedCurves; None or unusable: not judged): the same library, or for a declared
    slit the same folder and ``det``'s own curve file, all ``calibration_fingerprint`` and the inputs hash
    read of the curves there (another analyzer's curve changes nothing)."""
    if curves_now is None or curves_now.fingerprint is None or state.get("library") == curves_now.fingerprint:
        return True
    curve = ((state.get("detectors") or {}).get(det) or {}).get("curve")       # None when held
    folder = None if curves_now.folder is None else str(curves_now.folder)
    return (state.get("setting_basis") == "declared" and curve is not None and state.get("folder") == folder
            and curves_now.identity.get((det, state.get("setting"))) == curve)


def stored_now(prev, det: str, *, g, runcal, row: dict | None, scan_inputs, curves_now) -> UnitNow | None:
    """:class:`UnitNow` from what is stored, without parsing the data file: the import row's data revision
    (BL43; else the ledger's own content hash), the stamped q and T in the guidelines hash, the stored scan
    calibration. ``row``/``scan_inputs`` are the scan's import row and inputs (BL43), else None. None when
    the stored scan calibration no longer stands for ``det`` against the curves now (``curves_now``, already
    narrowed to the scan's named set by the caller)."""
    if prev is None:
        return None
    from isar.intake import inputs
    from isar.session import calibrations, guidelines as guide_mod
    from isar.session.state import canonical_hash
    exp = prev.scan_id.rsplit(":", 2)[0]
    scan = prev.scan_number
    state = runcal.tabulated.get(f"{exp}:{scan}") if runcal.method == "tabulated_elastic" else None
    if state is not None and scan_inputs is not None:
        # a switch between two named sets of one folder leaves its fingerprint as it was: judged first
        if state.get("named") != scan_inputs.named:
            return None
    if state is not None and not _state_stands(state, det, curves_now):
        return None
    prov = prev.provenance or {}
    T = prov.get("T")
    declared_out = False
    inputs_hash = None
    chash = prev.content_hash
    if row is not None:
        chash = row.get("data_revision")
        user_T = scan_inputs.facts["temperature"].user_value
        T = user_T if user_T is not None else T       # the fit's T; a dead analyzer's stamp keeps the file's
        declared_out = scan_inputs.excluded is not None or det in scan_inputs.dead
        inputs_hash = inputs.inputs_hash(inputs.unit_inputs(row, scan_inputs, det, state))
    ghash = canonical_hash(guide_mod.guidelines_signature(g, scan_number=scan, q=prov.get("q"), T=T))
    calfp = None if declared_out else calibrations.calibration_fingerprint(runcal, _ScanRef(exp, scan), det)
    return UnitNow(chash, ghash, calfp, inputs_hash)


def current_passes(workspace_root: Path | str, unit_keys: Iterable[str] | None = None) -> dict[str, frozenset[str]]:
    """``unit_key -> {"fast", "deep"} subset`` with a current stored result, for ``unit_keys`` (None: every
    analyzer file in the ledger); an unknown key gives ``frozenset()``. Reads the workspace once and never
    writes; ``FileNotFoundError`` when there is no workspace at ``workspace_root``. Re-exported, Qt-free, as
    ``isar.workspace_api.currentness.current_passes``."""
    from isar.intake import curves, gpi, inputs, scans as intake_scans, store
    from isar.session import calibrations, guidelines as guide_mod
    from isar.session.workspace import Workspace

    card, session = Workspace.open(workspace_root).load()
    root = Path(workspace_root)
    keys = list(session.scans) if unit_keys is None else list(unit_keys)
    g = guide_mod.resolve(card)
    runcal = calibrations.calibration_view(card)
    request = queue_deep_request()
    bl43 = intake_scans.applies(card)
    rows, index, curves_now, gpi_now = {}, None, None, {}
    if bl43:
        rows = intake_scans.read_scans(root)
        gpi_now = gpi.entries_in_force(root)
        read = store.read_all(root, store.ENTRIES)
        index = inputs.index_entries(read.entries, lines=read.lines)
        folder, pattern, _source = curves.resolution_location(card, root)
        curves_now = curves.load_curves(folder, pattern)          # logs why when unusable; then not judged
    inputs_of: dict = {}
    curves_of: dict = {}
    out: dict[str, frozenset[str]] = {}
    for key in keys:
        prev = session.get(key)
        if prev is None:
            out[key] = frozenset()
            continue
        det = key.rsplit(":", 1)[1]
        row = si = scan_curves = None
        if bl43:
            scan = key.rsplit(":", 1)[0]
            row = rows.get(scan)
            if row is None:              # a BL43 unit with no import row has nothing to compare with
                out[key] = frozenset()
                continue
            si = inputs_of.get(scan) or inputs_of.setdefault(
                scan, inputs.scan_inputs(row, index, gpi_now.get(scan), data_dir=root / "data"))
            if scan not in curves_of:    # narrowed once per scan: each narrowing reads the named set's curve files
                curves_of[scan] = curves.with_named(curves_now, si.named)
            scan_curves = curves_of[scan]
        now = stored_now(prev, det, g=g, runcal=runcal, row=row, scan_inputs=si, curves_now=scan_curves)
        out[key] = judge(prev, det, now, request)
    return out


def _models(n: int) -> str:
    return f"{n} model" if n == 1 else f"{n} models"


def _names(text: str) -> int:
    return len([s for s in text.split(",") if s.strip()])


def _deep_parts(record: dict) -> list[str]:
    parts = []
    limits = [s.strip() for s in (record.get("search_limited") or "").split(";") if s.strip()]
    suspect = sum(_names(s[len("suspect after restarts:"):]) for s in limits
                  if s.startswith("suspect after restarts:"))
    if suspect:
        parts.append(f"comparison incomplete, {_models(suspect)} unresolved after restarts")
    unrun = sum(_names(s[len("budget ended before "):].removeprefix("promotion of "))
                for s in limits if s.startswith("budget ended before "))
    if unrun:
        parts.append(f"comparison incomplete, an earlier time limit left {_models(unrun)} unrun")
    for s in limits:
        if s.startswith("fast pass search limited:"):
            why = s.split(":", 1)[1].strip()
            parts.append("comparison incomplete, the fast pass reached its peak-count ceiling" if why == "ceiling"
                         else f"comparison incomplete, the fast pass search was limited ({why})")
    known = ("suspect after restarts:", "budget ended before ", "fast pass search limited:")
    parts += [f"comparison incomplete ({s})" for s in limits if not s.startswith(known)]
    if record.get("state") == "search_limited" and not limits:
        parts.append("comparison incomplete")
    if (record.get("close_call") or {}).get("numerical") is True:
        parts.append("count probabilities are numerically marginal")
    chosen = record.get("chosen")
    if isinstance(chosen, dict):
        if chosen.get("errors_failure"):
            parts.append(f"error bars missing ({chosen['errors_failure']})")
    else:
        parts.append("no chosen model stored")
    return parts


def result_warning(outcome: dict | None, pass_kind: str) -> str | None:
    """The one plain sentence naming what limits a stored result (``outcome`` is the analyzer's stored
    outcome dict), or None. Built only from stored fields, so the words are the same after a reopen.
    ``trusted`` False never counts: it is global, said once by :func:`deep_calibration_note`."""
    outcome = outcome or {}
    if pass_kind == "fast":
        record = outcome.get("fast_pass") or {}
        why = record.get("search_limit_reason") or ""
        if why == "ceiling":
            n = (record.get("settings") or {}).get("mode_ceiling")
            return f"Fast pass done: the search reached the peak-count ceiling ({n}); more peaks were not tried."
        if why == "safety_budget":
            return "Fast pass done: an earlier time limit stopped the search before every count was priced."
        return f"Fast pass done: the search stopped early ({why})." if why else None
    record = outcome.get("deep_pass")
    if not isinstance(record, dict) or record.get("state") == "refused":
        return None
    parts = _deep_parts(record)
    return "Deep pass done: " + "; ".join(parts) if parts else None


def deep_calibration_note() -> str | None:
    """The one standing note while the deep pass's evidence calibration is not accepted, else None."""
    from isar.analyze import deep_pass
    if deep_pass.CALIBRATION_STATUS.startswith("accepted"):
        return None
    return "Deep-pass evidence calibration is provisional: all deep results carry this"
