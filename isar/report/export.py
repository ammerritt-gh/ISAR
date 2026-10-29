"""`isar export`: one timestamped folder of the stored results, for use outside ISAR.

``export_workspace(root)`` writes ``report/exports/export-<YYYYMMDD-HHMMSS>[-N]/`` holding
``modes.csv`` (one row per mode per pass, each labelled ``fast``, ``deep`` or ``inter``), ``fits.json`` (one
record per ledger spectrum, with the stored pass records verbatim), ``spectra/*.csv`` (the current
stored data of each spectrum), ``resolution/*.csv`` (each curve a fit used) and ``README.txt``.

It reads stored results only: the ledger, the unit-data files and pooled blobs through the read
facade, and each fit's recorded resolution curve. Nothing is refitted, reparsed or saved. All passes are
exported side by side; the authoritative one of each spectrum (``isar.session.inter_spectra.authority``, the
one resolver) is marked, never picked here. The folder is built under a ``.partial-*`` name and renamed into
place, so a failed export leaves no folder that reads as finished, and an earlier export is never
overwritten.
"""
from __future__ import annotations

import csv
import errno
import importlib.metadata
import json
import logging
import math
import os
import shutil
import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from isar.analyze.resolution import curve_from_record
from isar.intake import scans as intake_scans
from isar.session import outcome as outcome_mod
from isar.session.inter_spectra import authority, records as inter_records
from isar.session.workspace import Workspace
from isar.workspace_api import Workspace as ReadWorkspace
from isar.workspace_api.inter_spectra import inter_overview, is_flagged
from isar.workspace_api.scan_keys import split_key

log = logging.getLogger(__name__)

EXPORT_SCHEMA = "isar.export-fits/1"
EXPORTS_DIRNAME = "exports"
_MAX_SUFFIX = 100

# modes.csv: the results writer's columns unchanged, then ten for the export (the bounds empty on
# fast rows; inputs_current empty where the row's pass was not judged), then four for the authoritative
# fit (authoritative: the row's fit is the one in use; fit_source: the spectrum's plain-words label; fit_id:
# the row's own fit; flagged: inter rows only).
MODES_COLUMNS = outcome_mod._MODES_COLUMNS + [
    "pass", "trust", "scan_label", "spectrum_file", "fit_data_mismatch",
    "omega0_lo", "omega0_hi", "gamma_lo", "gamma_hi", "inputs_current",
    "authoritative", "fit_source", "fit_id", "flagged"]
SPECTRUM_COLUMNS = ["x_raw", "energy_meV", "counts", "sigma", "mask"]
FAST_TRUST = "not assessed"   # the fast pass stores no trust flag; absence is not a verdict
CONDITIONAL = "hesse_conditional"   # a refit's errors hold the neighbours fixed

README_TEXT = """\
ISAR export
===========

This folder is a copy of the stored results of one ISAR experiment, made by `isar export`.
Nothing was refitted to make it. Each export is a new folder; an earlier one is never changed.

Files
-----
modes.csv     one row per fitted mode per pass (fast, deep and inter), mode_index 0 the lowest energy
fits.json     one record per spectrum: its state, the stored pass records in full, provenance, and
              what each pass's inputs were, and its inter-spectra fits
spectra/      one CSV per spectrum: the data ISAR holds now
resolution/   one CSV per measured resolution curve a fit used
README.txt    this note

Units and conventions
---------------------
- Energies in meV. Counts are raw counts. T in K. Q_nm is |Q| in nm^-1, as the data files give it.
  h, k, l in rlu where present.
- Stokes side E >= 0 is energy loss.
- omega0 is the undamped DHO energy E0, the fitted parameter. peak_position is the damped frequency
  sqrt(E0^2 - Gamma^2), empty when the mode is overdamped (Gamma >= E0).
- gamma is the damping Gamma (HWHM). height is the convolved height in counts at the spectrum's
  exposure; amplitude is the fitted convolved height h at the fit's reference exposure;
  amplitude_intrinsic is the DHO's intrinsic narrow-limit amplitude A. See docs/STATS_PRIMER.md
  section 8 for the full DHO form and the height coordinate.
- uncertainty_kind says what an uncertainty is: hesse (the *_err cells, symmetric, fast rows), minos
  (the *_lo / *_hi cells, absolute MINOS bounds, deep rows), hesse_conditional (the *_err cells of an
  inter-spectra row: the error with the neighbours' modes held fixed, so smaller than a solo error and
  not comparable to it), or empty when the mode has none.
- Empty means missing, never zero. A missing uncertainty stays missing.

Passes, side by side
--------------------
Every pass is exported; the one ISAR uses for a spectrum (its authoritative fit) is marked, the others stay.
Tell them apart by:
- the pass column of modes.csv (fast / deep / inter), and the fast and deep blocks of each fits.json record;
- count_state, which on each row is that row's own pass's state;
- trust: on deep rows it is the stored deep record's own flag (trusted / untrusted); fast rows read
  "not assessed", because the fast pass stores no trust flag.
Deep rows leave amplitude, amplitude_intrinsic, height, area_stokes, peak_position, the *_err cells and
the per-peak status cells empty: the deep record stores none of them, and they are not derived here.
The deep refit's full parameters (each mode's convolved height <prefix>.h among them) and every error
block are in fits.json under deep_pass.chosen.
A deep pass that contributes no rows (refused, no chosen model, or no successful refit) says why in its
fits.json deep block (state_reason, chosen_failure). A spectrum whose deep block is null never had a
deep pass.
The inter-spectra fit is the second pass that couples neighbouring spectra. An inter row is written only for
a spectrum whose authoritative fit is a refit; the spectrum's other refits are listed in its fits.json
inter_spectra block (history). Four trailing columns of modes.csv say which fit is in use:
- authoritative: true on the row whose fit ISAR uses for that spectrum, false on the others, empty on deep
  rows (the deep pass is a second opinion on the fast solo fit);
- fit_source: the spectrum's authoritative fit in plain words, for example Fast solo, Batch 2 · sweep 3,
  Your pick: ..., Locked: ..., or Fast solo (Batch 2 · ring 1 is out of date: ...);
- fit_id: the row's own fit, fast or the refit's id;
- flagged: on inter rows, true when ISAR sent the spectrum to a person for review.
On an inter row inputs_current is false when the refit is out of date (a locked refit whose inputs changed),
true when judged and current, empty when not judged; trust reads not assessed.
The deep pass's sampler settings are provisional: while its state reason ends "provisional sampler
settings: untrusted until calibration", read its preferred count as a second opinion, not a verdict.

State words
-----------
Fast pass: resolved, close_call, unresolved, search_limited; and for spectra without a fast-pass
result: held (the reason names the hold), fitting (ingested, not yet fitted), not fitted (excluded,
undersampled, or the fit failed), not recorded (a fit stored before the fast pass existed).
Deep pass: resolved, close_call, search_limited, refused.
See docs/USING_ISAR.md, "Reading the peak count" and "The deep pass".
fits.json holds one record for every spectrum, fitted or not, so a spectrum with no mode row (zero
modes, held, never fitted) is still listed with its state.

Spectrum files
--------------
Lines starting with # are header lines; skip them when reading (for example pandas comment="#").
They name the spectrum, its fast-pass state and deep-pass state, its exposure, its energy zero and
the data revisions. The exposure is the one value the stored fit used for the whole spectrum; the
spectrum files carry no per-point exposure column. "exposure: not stored" gives the reason.
Columns:
- x_raw       the stored raw abscissa (for a pooled master, its pooled grid)
- energy_meV  x_raw minus the fit's energy zero; x_raw itself when the axis is already absolute;
              empty when no zero is known (ISAR never assumes a zero of 0)
- counts      as stored
- sigma       sqrt(variance), 0 at 0 counts (no floor)
- mask        1 kept, 0 masked by QC; the mask is recorded, never applied

Data revisions
--------------
fit_input is the revision of the data the stored fit was made against; current_data the revision
ISAR holds now, which the spectrum CSV carries. fit_data_mismatch true means the file was read again
with changed content after the fit, so the stored fit and its modes.csv rows describe earlier data
than the spectrum CSV holds; it is refitted when it is added to the queue again. "fitted-against revision not recorded"
means ISAR stored no input revision for that fit; the current revision is not a substitute.
@CURRENTNESS@

Resolution curves
-----------------
Each curve a fit used is written once, columns energy_meV and value, normalised to unit area. A curve
whose file is gone or changed since the fit is not written; its spectra name the problem in fits.json.

fits.json
---------
Values are copied verbatim from the stored records; a non-finite stored number appears as NaN.
"""


@dataclass(frozen=True)
class ExportSummary:
    folder: Path
    n_spectra: int
    n_modes: int
    n_modes_fast: int
    n_modes_deep: int
    n_curves: int
    problems: tuple
    n_modes_inter: int = 0


def _sanitize(part) -> str:
    """One file-name component, the rule of ``isar.session.unit_data._sanitize``."""
    s = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(part))
    return s or "x"


def _finite_number(value):
    """A finite float, else None (a missing value stays missing, never zero)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


# ---------------------------------------------------------------------------
# the ledger walk
# ---------------------------------------------------------------------------
def _spectra(session) -> list:
    """``(entry, detector, outcome or None)`` per ledger spectrum, in spectrum order."""
    out = []
    for e in session.scans.values():
        if e.detectors:
            out.extend((e, det, oc) for det, oc in e.detectors.items())
        else:
            parts = e.scan_id.split(":")
            out.append((e, parts[-1] if len(parts) >= 3 else None, None))

    def order(item):
        e, det, _oc = item
        label = (e.provenance or {}).get("scan_label")
        return (split_key(e.scan_id)[0] or "", str(label) if label is not None else str(e.scan_number),
                e.scan_number, det or "")
    return sorted(out, key=order)


def _fit_input(e, oc) -> dict:
    """What the stored fit was made against, from the entry's measurement record, never the current hash."""
    mr = e.measurement_record if isinstance(e.measurement_record, dict) else None
    if oc is not None and mr is not None and mr.get("content_hash"):
        return {"recorded": True, "content_hash": mr["content_hash"],
                "guidelines_hash": e.guidelines_hash}
    return {"recorded": False, "content_hash": None, "guidelines_hash": None,
            "note": "fitted-against revision not recorded"}


def _row_base(e, det, oc) -> dict:
    q = oc.get("q") or [None, None, None]
    q_nm = (((e.provenance or {}).get("analyzer_facts") or {}).get("Q_nm") or {}).get("value")
    return {"scan_id": e.scan_id, "scan": e.scan_number, "detector": det, "sample": oc.get("sample"),
            "T": oc.get("T"), "h": q[0], "k": q[1], "l": q[2], "Q_nm": q_nm}


def _has_deep_rows(dp: dict) -> bool:
    return bool(dp) and dp.get("state") != "refused" and isinstance(dp.get("chosen"), dict) \
        and dp["chosen"].get("params") is not None


def _deep_rows(e, det, oc) -> list:
    """The deep pass's mode rows from its stored chosen refit: E0 and Gamma per mode, the MINOS
    bounds when valid; every cell the deep record does not store stays empty."""
    dp = outcome_mod.deep_pass_of(oc)
    if not _has_deep_rows(dp):
        return []
    chosen = dp["chosen"]
    params = chosen["params"]
    minos = (chosen.get("errors") or {}).get("minos") or {}
    modes = []
    for name, value in params.items():
        if not name.endswith(".omega0"):
            continue
        prefix = name[:-len(".omega0")]
        if f"{prefix}.Gamma" not in params:
            continue
        e0 = outcome_mod._finite(value)
        if e0 is None:
            continue
        modes.append((float(e0), prefix))
    modes.sort()

    def bound(key):
        b = minos.get(key)
        if isinstance(b, (list, tuple)) and len(b) == 3 and b[2] is True:
            return outcome_mod._finite(b[0]), outcome_mod._finite(b[1])
        return None, None

    base = _row_base(e, det, oc)
    trust = "trusted" if dp.get("trusted") is True else "untrusted"
    rows = []
    for index, (e0, prefix) in enumerate(modes):
        w_lo, w_hi = bound(f"{prefix}.omega0")
        g_lo, g_hi = bound(f"{prefix}.Gamma")
        written = any(v is not None for v in (w_lo, w_hi, g_lo, g_hi))
        rows.append({**base, "count_state": dp.get("state"), "best_model": chosen.get("label"),
                     "n_modes": len(modes), "mode_index": index, "omega0": e0,
                     "gamma": outcome_mod._finite(params[f"{prefix}.Gamma"]),
                     "omega0_lo": w_lo, "omega0_hi": w_hi, "gamma_lo": g_lo, "gamma_hi": g_hi,
                     "uncertainty_kind": "minos" if written else None,
                     "pass": "deep", "trust": trust})
    return rows


def _fast_state(e, oc) -> tuple:
    words = outcome_mod._fast_pass_columns(oc) if oc is not None else outcome_mod._unfitted_count(e)
    return words["count_state"], words["count_state_reason"]


def _deep_block(oc, n_rows: int):
    dp = outcome_mod.deep_pass_of(oc)
    if not dp:
        return None
    chosen = dp.get("chosen") if isinstance(dp.get("chosen"), dict) else None
    if chosen is not None:
        failure = chosen.get("errors_failure")
    elif dp.get("state") != "refused":
        failure = "the deep pass stored no chosen model"
    else:
        failure = None
    return {"state": dp.get("state"), "state_reason": dp.get("reason"),
            "trust": "trusted" if dp.get("trusted") is True else "untrusted",
            "trusted": dp.get("trusted"), "calibration": dp.get("calibration"),
            "chosen_label": chosen.get("label") if chosen else None,
            "chosen_failure": failure, "n_modes_exported": n_rows}


def _qualifications(e, oc) -> dict:
    keys = ("count_state_reason", "flags", "probabilities_conditional", "numerically_marginal",
            "search_limit_reason", "deep_pass_candidate")
    if oc is None:
        words = outcome_mod._unfitted_count(e)
        out = {k: words.get(k) for k in keys}
        out.update(gamma_provisional=None, interval_reliable=None, delivery_withheld_reason=None)
    else:
        words = outcome_mod._fast_pass_columns(oc)
        out = {k: words.get(k) for k in keys}
        out.update(gamma_provisional=oc.get("gamma_provisional"),
                   interval_reliable=oc.get("interval_reliable"),
                   delivery_withheld_reason=oc.get("delivery_withheld_reason"))
    out["ledger_reasons"] = list(e.reasons or [])
    return out


# ---------------------------------------------------------------------------
# spectrum and curve files
# ---------------------------------------------------------------------------
def _read_arrays(api, e, det, oc):
    """``(x, counts, sigma, mask, axis)`` of the current stored data through the read facade, or a
    problem string. A pooled master reads its pooled blob; its per-point monitor is not exported."""
    if oc is not None and oc.get("pooled") and oc.get("spectrum_ref"):
        try:
            blob = api.pooled_spectrum(oc["spectrum_ref"])
        except (OSError, ValueError) as exc:
            return f"pooled spectrum unreadable ({type(exc).__name__}: {exc})"
        if blob is None:
            return "no stored pooled spectrum"
        return blob.x, blob.counts, blob.yerr, blob.mask, None
    data = api.unit_data(e.scan_id, det)
    if data is None:
        return "no readable unit data"
    return data.x, data.counts, data.yerr, data.mask, data.axis


def _cell(value):
    f = _finite_number(value)
    return "" if f is None else repr(f)


def _write_spectrum(path: Path, header: list, x, counts, sigma, mask, center, absolute: bool) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        for line in header:
            f.write(f"# {line}\r\n")
        w = csv.writer(f)
        w.writerow(SPECTRUM_COLUMNS)
        for xi, ni, si, mi in zip(x, counts, sigma, mask):
            xf = _finite_number(xi)
            if xf is not None and center is not None:
                energy = xf - center
            elif xf is not None and absolute:
                energy = xf
            else:
                energy = None
            w.writerow([_cell(xi), _cell(energy), _cell(ni), _cell(si), "1" if bool(mi) else "0"])


def _spectrum_header(record: dict, oc, center) -> list:
    deep = record["deep"]
    lines = [f"spectrum_id: {record['spectrum_id']}", f"detector: {record['detector'] or ''}",
             f"scan_label: {record['scan_label'] or ''}", f"state: {record['fast']['state']}",
             f"deep_state: {deep['state'] if deep else 'not run'}"]
    exposure = _finite_number((oc or {}).get("effective_exposure"))
    prov = (oc or {}).get("exposure_provenance") or {}
    if exposure is not None:
        lines += [f"exposure: {oc['effective_exposure']}", f"exposure_source: {prov.get('source')}",
                  f"exposure_normalization: {prov.get('normalization_source')}"]
    else:
        reason = "no stored fit" if oc is None else (prov.get("source") or "not recorded")
        lines += ["exposure: not stored", f"exposure_reason: {reason}"]
    lines.append(f"energy_zero: {center if center is not None else 'none'}")
    fit_input = record["fit_input"]
    mismatch = record["fit_data_mismatch"]
    lines += [f"data: current stored data, content_hash {record['current_data']['content_hash']}",
              f"fitted_against: {fit_input['content_hash'] if fit_input['recorded'] else 'not recorded'}",
              "fit_data_mismatch: " + ("unknown" if mismatch is None else str(mismatch).lower())]
    return lines


class _Curves:
    """Each distinct curve a fit used, written once; a failure kept per curve, never substituted."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.by_key: dict = {}        # (folder, file, stamp) -> (curve_id | None, problem | None)
        self.table: dict = {}         # curve_id -> {"file", "reference"}

    def resolve(self, ref: dict):
        try:
            key = (str(ref["folder"]), str(ref["file"]), tuple(ref["stamp"]))
        except (KeyError, TypeError):
            key = ("", json.dumps(ref, sort_keys=True, default=str), ())
        if key not in self.by_key:
            self.by_key[key] = self._write(ref)
        return self.by_key[key]

    def _write(self, ref: dict):
        try:
            curve = curve_from_record(ref)
        except (OSError, ValueError) as exc:
            return None, str(exc)
        stem = _sanitize(Path(str(ref["file"])).name)
        curve_id, n = stem, 2
        while curve_id in self.table:
            curve_id, n = f"{stem}-{n}", n + 1
        self.folder.mkdir(exist_ok=True)
        rel = f"resolution/{curve_id}.csv"
        with open(self.folder / f"{curve_id}.csv", "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["energy_meV", "value"])
            for e_i, v_i in zip(curve.energy, curve.values):
                w.writerow([repr(float(e_i)), repr(float(v_i))])
        self.table[curve_id] = {"file": rel, "reference": ref}
        return curve_id, None


# ---------------------------------------------------------------------------
# the export
# ---------------------------------------------------------------------------
def _isar_version():
    try:
        return importlib.metadata.version("isar")
    except importlib.metadata.PackageNotFoundError:
        log.warning("export: isar package metadata not found; isar_version recorded as null")
        return None


NOT_ASSESSED_TEXT = """\
Currentness is not assessed: this export does not check whether each fit's other inputs (resolution,
settings) changed after the fit.
"""
BL43_CURRENTNESS_TEXT = """\
Currentness: each fits.json record has an `inputs` block with one entry per pass, fast and deep (null where
the spectrum has no such pass). `current` is true when every input the pass used still has the value it had,
false when one changed after the fit was made, and null when it could not be judged (a fit stored before ISAR
recorded its inputs, or a pass still running). `used` holds the inputs the pass used, as recorded. `changed`
lists each input that differs now: `input`, `from` (used), `to` (now) and, for temperature and slit, `to_source`
(user, file or missing). The inputs are: data, temperature, slit, q, excluded, dead, dead_in_scan,
counts_accepted, resolution_folder, curve, resolution_library, calibration_setting, calibration_basis,
calibration_zero, calibration_zero_source, calibration_held_reason. The `inputs_current` column of modes.csv
is that `current` for the row's own pass (true / false; empty when not judged). A fit whose inputs changed
keeps its stored result until it is added to the queue again.
"""


def _write_readme(folder: Path, bl43: bool) -> None:
    text = README_TEXT.replace("@CURRENTNESS@", (BL43_CURRENTNESS_TEXT if bl43 else NOT_ASSESSED_TEXT).rstrip("\n"))
    with open(folder / "README.txt", "w", encoding="utf-8", newline="\r\n") as f:
        f.write(text)


def _pass_record(p) -> dict:
    """One pass's currentness from the backend's read: whether the inputs it used still hold, what it used and
    what differs now. A pass the backend has no finished record of (fitted before inputs were recorded, or a
    refit still running, whose inputs are not the stored fit's) is not judged."""
    if p is None or p.state != "finished":
        return {"current": None, "used": None, "changed": []}
    return {"current": not p.changed, "used": p.inputs,
            "changed": [{"input": c.field, "from": c.used, "to": c.now, "to_source": c.now_source}
                        for c in p.changes]}


def _inputs_current(record) -> str:
    """A modes.csv cell: ``true`` / ``false``, empty where the pass was not judged."""
    return "" if record is None or record["current"] is None else str(record["current"]).lower()


def _null_nonfinite(value):
    """``value`` with every non-finite float (NaN, inf) replaced by None, recursively: JSON has no
    token for them, and a stored BL43 value can be NaN."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _null_nonfinite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_null_nonfinite(v) for v in value]
    return value


def _write_fits(folder: Path, doc: dict) -> None:
    with open(folder / "fits.json", "w", encoding="utf-8", newline="\n") as f:
        # allow_nan=False: a bare NaN token can never be written again, whatever the sanitiser misses
        f.write(json.dumps(_null_nonfinite(doc), indent=1, sort_keys=True, allow_nan=False))
        f.write("\n")


def _currentness(bl43: bool, has_records: bool) -> dict:
    if bl43 and has_records:
        return {"assessed": True,
                "note": "each fit's inputs are checked against the intake records; see inputs on each record"}
    if bl43:
        return {"assessed": False,
                "note": "this workspace has no recorded fit inputs, so no fit is judged; see inputs on each record"}
    return {"assessed": False,
            "note": "this export does not check whether each fit's inputs changed after the fit"}


def _inter_state(root: Path, card, session) -> tuple:
    """``(effective, overview, log)`` of the inter-spectra fits: the resolver's answers (a spectrum absent from
    ``effective`` is its solo fit), the facade's labels and flags, and the stored log. Empty when none exist."""
    outcomes = {e.scan_id: next(iter(e.detectors.values())) for e in session.scans.values()
                if len(e.detectors or {}) == 1}
    eff = authority.effective(root, outcomes, card.state.tabulated_calibration)
    if not eff:
        return {}, None, None
    return eff, inter_overview(root), inter_records.read_log(root)


def _inter_rows(e, det, ent, ov) -> list:
    """The modes.csv rows of the authoritative refit of one spectrum; none when it is on its solo fit."""
    if ent is None or ent.line is None:
        return []
    res = ent.resolution
    judged = not any("not judged" in n for n in res.notes)
    current = str(res.stale is None).lower() if judged else ""
    rows = []
    for row in outcome_mod._mode_rows(e, det, ent.outcome):
        row.update({"pass": "inter", "trust": FAST_TRUST, "inputs_current": current, "fit_id": ent.line.fit,
                    "authoritative": "true", "flagged": str(ov is not None and is_flagged(ov.flag)).lower(),
                    "uncertainty_kind": CONDITIONAL if row.get("omega0_err") is not None else None})
        rows.append(row)
    return rows


def _fit_source(ent, ov) -> str:
    """The spectrum's authoritative fit in plain words (the facade's label, from the same resolver)."""
    if ov is None:
        return "Fast solo"
    if ov.in_use and (ent is None or ent.line is None):     # the stored refit cannot be read: the solo fit stands in
        return "Fast solo (the stored refit cannot be read)"
    return ov.label


def _inter_block(unit, ent, ov, log, has_fast: bool):
    """The fits.json ``inter_spectra`` block of one spectrum, null when it has no inter-spectra entry."""
    if ov is None:
        return None
    history = [{"fit": "fast", "label": "Fast solo", "status": "fast", "flagged": False}] if has_fast else []
    refits = sorted((r for r in log.refits if r.unit == unit and r.status in inter_records.ELIGIBLE),
                    key=lambda r: log.batches[r.batch].number if r.batch in log.batches else 0)
    history += [{"fit": r.fit, "label": authority.fit_label(r, log.batches), "status": r.status,
                 "flagged": r.flag is not None and any(x["severity"] in ("warn", "serious") for x in r.flag.reasons)}
                for r in refits]
    line = ent.line if ent is not None else None
    drawn = {k: ent.outcome[k] for k in authority.OVERLAY_KEYS if k in ent.outcome} if line else None
    return {"authoritative": {"fit": ov.fit_id, "source": ov.source, "label": _fit_source(ent, ov),
                              "stale": ov.stale or ov.lock_stale, "locked": ov.locked},
            "flag": dataclasses.asdict(ov.flag) if ov.flag else None,
            "why_changed": line.why_changed if line else None, "fit": drawn, "history": history}


def _build(root: Path, stage: Path, progress) -> tuple:
    """Write everything but fits.json into ``stage``; return (the fits.json body, the summary counts)."""
    ws = Workspace.open(root)
    card, session = ws.load()
    api = ReadWorkspace.open(root)
    bl43 = intake_scans.applies(card)      # only BL43 has fit-input records
    fit_inputs = {u.unit: u for u in api.fit_inputs()} if bl43 else {}
    spectra_dir = stage / "spectra"
    spectra_dir.mkdir()
    curves = _Curves(stage / "resolution")
    records, rows, problems, used_names = [], [], [], set()
    n_fast = n_deep = n_inter = 0
    eff, overview, inter_log = _inter_state(root, card, session)
    by_fast: dict = {}
    by_deep: dict = {}
    items = _spectra(session)
    for i, (e, det, oc) in enumerate(items, 1):
        prov = e.provenance or {}
        label = prov.get("scan_label")
        fast_state, fast_reason = _fast_state(e, oc)
        fit_input = _fit_input(e, oc)
        mismatch = (fit_input["content_hash"] != e.content_hash) if fit_input["recorded"] else None
        q = (oc or {}).get("q") if oc is not None else prov.get("q")
        record = {
            "spectrum_id": e.scan_id, "detector": det, "experiment_id": split_key(e.scan_id)[0],
            "scan_number": e.scan_number, "scan_label": label, "file_set": prov.get("file_set"),
            "ledger_status": e.status, "fit_input": fit_input,
            "current_data": {"content_hash": e.content_hash}, "fit_data_mismatch": mismatch,
            "T": oc.get("T") if oc is not None else prov.get("T"),
            "Q_nm": ((prov.get("analyzer_facts") or {}).get("Q_nm") or {}).get("value"),
            "hkl": q,
            "exposure": ({"value": oc.get("effective_exposure"),
                          "provenance": oc.get("exposure_provenance")} if oc is not None else None),
            "qualifications": _qualifications(e, oc),
            "outcome": ({k: v for k, v in oc.items() if k not in ("fast_pass", "deep_pass")}
                        if oc is not None else None),
            "fast_pass": outcome_mod.fast_pass_of(oc) or None,
            "deep_pass": outcome_mod.deep_pass_of(oc) or None,
            "provenance": prov, "spectrum_file": None, "spectrum_axis": None,
            "resolution_curve": None,
        }

        # spectrum file name, unique in spectrum order
        base = f"{_sanitize(label if label is not None else e.scan_number)}__{_sanitize(det or 'none')}"
        name, n = base, 2
        while name in used_names:
            name, n = f"{base}__{n}", n + 1
        used_names.add(name)
        spectrum_file = f"spectra/{name}.csv"

        fast_rows = []
        if oc is not None:
            for row in outcome_mod._mode_rows(e, det, oc):
                row.update({"pass": "fast", "trust": FAST_TRUST})
                fast_rows.append(row)
        deep_rows = _deep_rows(e, det, oc) if oc is not None else []
        ent = eff.get(e.scan_id)
        ov = overview.by_key.get(e.scan_id) if overview is not None else None
        inter_rows = _inter_rows(e, det, ent, ov)
        source = _fit_source(ent, ov)
        fast_authoritative = str(ent is None or ent.line is None).lower()      # the resolver's answer, not the rows
        for row in fast_rows:
            row.update({"authoritative": fast_authoritative, "fit_id": "fast"})
        shared = {"content_hash": fit_input["content_hash"], "fit_data_mismatch": mismatch,
                  "scan_label": label, "spectrum_file": spectrum_file}
        for row in fast_rows + deep_rows + inter_rows:
            row.update(shared)
            if row["pass"] != "deep":
                row["fit_source"] = source
        rows.extend(fast_rows + deep_rows + inter_rows)
        n_fast += len(fast_rows)
        n_deep += len(deep_rows)
        n_inter += len(inter_rows)
        record["fast"] = {"state": fast_state, "state_reason": fast_reason,
                          "record": (None if oc is None else
                                     "fast_pass" if outcome_mod.fast_pass_of(oc) else
                                     "fit_without_fast_pass"),
                          "n_modes_exported": len(fast_rows)}
        record["deep"] = _deep_block(oc, len(deep_rows)) if oc is not None else None
        record["inter_spectra"] = _inter_block(e.scan_id, ent, ov, inter_log,
                                               ov is not None and authority.has_fast_solo(e, e.scan_id))
        fit = fit_inputs.get(e.scan_id)
        record["inputs"] = {
            "fast": _pass_record(fit.fast if fit else None) if oc is not None else None,
            "deep": _pass_record(fit.deep if fit else None) if record["deep"] is not None else None}
        for row in fast_rows:
            row["inputs_current"] = _inputs_current(record["inputs"]["fast"])
        for row in deep_rows:
            row["inputs_current"] = _inputs_current(record["inputs"]["deep"])
        by_fast[fast_state] = by_fast.get(fast_state, 0) + 1
        if record["deep"] is not None:
            by_deep[record["deep"]["state"]] = by_deep.get(record["deep"]["state"], 0) + 1

        # resolution curve the fit used
        ref = ((oc or {}).get("calib") or {}).get("tabulated")
        if ref:
            curve_id, problem = curves.resolve(ref)
            record["resolution_curve"] = curve_id
            if problem is not None:
                record["resolution_problem"] = problem
                problems.append({"spectrum_id": e.scan_id, "detector": det, "problem": problem})

        # the current stored data
        arrays = _read_arrays(api, e, det, oc)
        if isinstance(arrays, str):
            record["spectrum_problem"] = arrays
            problems.append({"spectrum_id": e.scan_id, "detector": det, "problem": arrays})
            for row in fast_rows + deep_rows + inter_rows:
                row["spectrum_file"] = None
        else:
            x, counts, sigma, mask, axis = arrays
            center = _finite_number(((oc or {}).get("calib") or {}).get("center"))
            absolute = (axis or {}).get("calibration_status") == "absolute"
            _write_spectrum(stage / spectrum_file, _spectrum_header(record, oc, center),
                            x, counts, sigma, mask, center, absolute)
            record["spectrum_file"] = spectrum_file
            record["spectrum_axis"] = axis
        records.append(record)
        if progress is not None:
            # Everything the analysis queue's results writer writes (ledger, card state, fit-input records) is
            # read before the first progress call: the GUI releases its write hold there.
            progress(i, len(items))

    outcome_mod._replace_csv(stage / "modes.csv", MODES_COLUMNS, rows)
    _write_readme(stage, bl43)
    doc = {
        "schema": EXPORT_SCHEMA, "workspace": str(Path(root).resolve()),
        "experiment": Path(root).resolve().name, "isar_version": _isar_version(),
        "card": {"facility": card.policy.facility, "instrument": card.policy.instrument,
                 "plugin": card.policy.plugin},
        "passes": {"exported": ["fast", "deep", "inter"],
                   "selection": "all passes are exported side by side, each labelled; the authoritative "
                                "one per spectrum is marked (authoritative / fit_source)"},
        "currentness": _currentness(bl43, bool(fit_inputs)),
        "counts": {"spectra": len(records), "modes": n_fast + n_deep + n_inter, "modes_fast": n_fast,
                   "modes_deep": n_deep, "modes_inter": n_inter, "by_state": {"fast": by_fast, "deep": by_deep},
                   "with_deep_pass": sum(by_deep.values())},
        "resolution_curves": curves.table, "problems": problems, "spectra": records,
    }
    return doc, (len(records), n_fast, n_deep, len(curves.table), tuple(problems), n_inter)


def _promote(stage: Path, exports: Path, ts: str, doc: dict) -> Path:
    """Rename ``stage`` to the first free ``export-<ts>[-N]``, naming that folder in fits.json."""
    for n in range(1, _MAX_SUFFIX + 1):
        name = f"export-{ts}" if n == 1 else f"export-{ts}-{n}"
        target = exports / name
        if target.exists():
            continue
        _write_fits(stage, {**doc, "folder": name})
        try:
            os.rename(stage, target)   # rename, not replace: an existing target is refused, never replaced
        except FileExistsError:
            continue
        except OSError as exc:
            if exc.errno in (errno.EEXIST, errno.ENOTEMPTY):
                continue
            raise
        return target
    raise FileExistsError(f"no free export folder name for export-{ts} (tried {_MAX_SUFFIX})")


def export_workspace(root, *, now: datetime | None = None, progress=None) -> ExportSummary:
    """Write one new export folder under ``<root>/report/exports/`` from stored results only.

    ``now`` (local time) names the folder; ``progress(i, n)`` is called after each spectrum.
    Raises on failure, leaving no new folder."""
    root = Path(root)
    now = now or datetime.now()
    ts = now.strftime("%Y%m%d-%H%M%S")
    exports = root / "report" / EXPORTS_DIRNAME
    exports.mkdir(parents=True, exist_ok=True)
    stage = exports / f".partial-{ts}-{os.getpid()}"
    stage.mkdir()
    try:
        doc, (n_spectra, n_fast, n_deep, n_curves, problems, n_inter) = _build(root, stage, progress)
        doc["exported_at"] = now.astimezone(timezone.utc).isoformat()
        folder = _promote(stage, exports, ts, doc)
    except BaseException:
        try:
            shutil.rmtree(stage)
        except OSError as exc:
            log.error("export: could not remove the staging folder %s: %s", stage, exc)
        raise
    return ExportSummary(folder=folder, n_spectra=n_spectra, n_modes=n_fast + n_deep + n_inter,
                         n_modes_fast=n_fast, n_modes_deep=n_deep, n_curves=n_curves,
                         problems=problems, n_modes_inter=n_inter)
