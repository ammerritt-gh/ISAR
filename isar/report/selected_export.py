"""Selectable data, authoritative curves and whole-model parameter exports."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import numpy as np

from isar.session.workspace import Workspace
from isar.workspace_api import Workspace as ReadWorkspace
from isar.workspace_api.spectra import normalised_points
from . import export_tables as tables

log = logging.getLogger(__name__)
SCHEMA = "isar.export/2"

README = """ISAR numerical export
=====================
Only the requested outputs are written. No fitting is performed during export.
export.json records the choices, source identities, fitted model and per-spectrum problems.

csv/: neutral CSV, one header with units. origin/: Origin-ready CSV, four header rows:
  1 Long Name; 2 Units; 3 Comments; 4 column designation (X, Y, yEr, Label).
In Origin's ASCII Import Options set these header rows, start numerical data at row 5,
and set column designations from row 4 (data: X Y yEr Label). Save the import filter
to reuse it. A designation written in a CSV is descriptive; Origin needs its import
options/filter to apply it. Fit columns are X followed by Y columns.

spectra/: one file per scan/analyzer. Included=0 preserves excluded points.
fits/: one file per scan/analyzer, its own fine X axis, total and every model component.
parameters/: one file per scan/analyzer when requested.
parameters.csv: combined whole-model parameter table when requested.
The older audit export's modes.csv is a technical, multi-pass mode table; these parameter
tables instead name every authoritative model parameter with its unit and uncertainty.

Monitor-normalized intensity is counts per one monitor count. Errors follow ISAR's
normalization convention, including monitor variance. Missing/invalid monitor values
produce blank intensities, never raw counts substituted into a normalized column.
Raw fit curves are counts at the spectrum's reference exposure, named in the comments
and export.json; measured points can have different exposures. Parameter amplitudes
use the same scale as the fit curves. Pooled measured data use summed raw counts or
counts divided by the summed monitor; pooled curves use the master reference monitor.

Energy axes use the authoritative fit's stored zero when available. Uncalibrated data
remain on their labelled file axis; no energy zero is invented. Blank means unavailable,
never zero. Uncertainties marked conditional hold neighboring spectra fixed. Missing
uncertainties remain blank; no errors are calculated by fitting during export.
"""


def _snapshot(root):
    from .export import _inter_state, _spectra, _fit_source
    card, session = Workspace.open(root).load()
    api = ReadWorkspace.open(root)
    effective, overview, _ = _inter_state(root, card, session)
    from isar.intake import scans as intake_scans
    inputs = {item.unit: item.fast for item in api.fit_inputs()} if intake_scans.applies(card) else {}
    snapshots = []
    for entry, detector, solo in _spectra(session):
        ent = effective.get(entry.scan_id)
        outcome = ent.outcome if ent is not None else solo
        ov = overview.by_key.get(entry.scan_id) if overview else None
        try:
            if (outcome or {}).get("pooled"):
                blob = api.pooled_spectrum(outcome.get("spectrum_ref"))
                unit = None if blob is None else SimpleNamespace(
                    x=blob.x, counts=blob.counts, yerr=blob.yerr, mask=blob.mask, monitor=blob.monitor,
                    axis={"calibration_status": "absolute", "units": "meV"},
                    exposure=SimpleNamespace(source="monitor", reference=blob.exposure_reference))
            else:
                unit = api.unit_data(entry.scan_id, detector)
            arrays = ("No readable stored data" if unit is None else
                      (unit.x, unit.counts, unit.yerr, unit.mask, unit.axis))
        except Exception as exc:
            log.exception("export: could not read %s/%s", entry.scan_id, detector)
            arrays, unit = f"Data unavailable: {exc}", None
        recorded = inputs.get(entry.scan_id)
        current = None if recorded is None or recorded.state != "finished" else not recorded.changed
        snapshots.append((entry, detector, outcome, ent, ov, _fit_source(ent, ov), arrays, unit, current))
    # Everything that the queue's writer changes is now captured, before first progress.
    return card, snapshots


def _spectrum(stage, options, qe, name, snapshot):
    entry, detector, outcome, ent, ov, source, arrays, unit, inputs_current = snapshot
    from .export import _finite_number
    oc = outcome or {}
    provenance = entry.provenance or {}
    context = {"scan": provenance.get("scan_label") or entry.scan_number, "detector": detector,
               "temperature": oc.get("T", provenance.get("T")),
               "hkl": oc.get("q", provenance.get("q")), "fit": source, "notes": ""}
    notes = []
    if ((ov is not None and (ov.stale or ov.lock_stale))
            or ((ent is None or ent.line is None) and inputs_current is False)):
        notes.append("Fit inputs are out of date")
    data_changed = (entry.measurement_record and
                    entry.measurement_record.get("content_hash") != entry.content_hash)
    if data_changed:
        notes.append("Current data differ from the data used for the fit")
    context["notes"] = "; ".join(notes)
    record = {"spectrum_id": entry.scan_id, "detector": detector, "context": context,
              "content_hash": entry.content_hash, "measurement_record": entry.measurement_record,
              "files": [], "problems": []}
    problems = record["problems"]
    fit = tables.model_view(oc, entry, detector)
    center = _finite_number((oc.get("calib") or {}).get("center"))
    exposure = getattr(unit, "exposure", None)
    reference = getattr(exposure, "reference", None)
    exposure_source = getattr(exposure, "source", None)
    # The model sits at its own fit's reference exposure, which a manual mask can move off the
    # unit's; the Scan(s) display rescales by the same stored basis.
    basis = fit.fast_pass.get("exposure_basis") or {}
    if basis:
        reference = (_finite_number(basis.get("reference"))
                     if basis.get("source") == exposure_source else None)
    if data_changed:
        reference = None  # the current unit's monitor cannot identify the older fit's scale
    record["reference_exposure"] = {"value": reference, "source": exposure_source}
    factor = 1.0
    intensity_unit = "counts"
    model_unit = "counts at reference exposure"  # raw data carry each point's own exposure
    if options.scale == "normalised":
        intensity_unit = model_unit = "counts/monitor"
        if (data_changed or exposure_source != "monitor" or reference is None
                or not np.isfinite(reference) or reference <= 0):
            factor = None
        else:
            factor = 1.0 / reference
    comment = (f"Scan={context['scan']}; analyzer={detector}; T={context['temperature']} K; "
               f"hkl={context['hkl']}; fit={source}; reference exposure={reference} {exposure_source}; "
               f"{context['notes']}")
    record["intensity_unit"] = intensity_unit
    x = None
    x_label, x_unit, shift = "File axis (not zeroed)", "", 0.0
    if isinstance(arrays, str):
        problems.append(arrays)
    else:
        x, counts, sigma, mask, axis = arrays
        x, counts, sigma = (np.asarray(a, dtype=float) for a in (x, counts, sigma))
        axis = axis or {}
        x_unit = axis.get("units") or ""
        status = axis.get("calibration_status")
        if center is not None:
            x_label, x_unit, shift = "Energy", "meV", center
        elif status == "absolute":
            x_label, x_unit = "Energy", "meV"
        if options.spectra:
            values, errors = counts, sigma
            if options.scale == "normalised":
                monitor = getattr(unit, "monitor", None)
                valid, values, errors = normalised_points(counts, sigma ** 2, monitor, 1.0)
                if not np.all(valid):
                    problems.append("Missing/invalid monitor: normalized intensity is blank at those points")
            record["files"] += tables.write_formats(
                stage, "spectra", name, options,
                [x_label, "Intensity", "Uncertainty", "Included"],
                [x_unit, intensity_unit, intensity_unit, ""], ["X", "Y", "yEr", "Label"],
                comment, zip(x - shift, values, errors, np.asarray(mask, dtype=int)))
    parameter_rows = []
    if options.curves or options.parameters_separate or options.parameters_combined:
        try:
            manifest = tables.model_manifest(fit, qe)
        except (ValueError, TypeError, KeyError) as exc:
            log.warning("export: %s model unavailable: %s", entry.scan_id, exc)
            problems.append(str(exc))
            manifest = None
        if manifest is not None:
            record["model_manifest"] = manifest
            record["fit_id"] = ent.line.fit if ent is not None and ent.line is not None else "fast"
            record["calibration"] = fit.calib
            if factor is None:
                problems.append("Fit cannot be monitor-normalized: no reference monitor for its data revision")
            if options.parameters_separate or options.parameters_combined:
                parameter_rows = tables.parameter_rows(
                    fit, manifest, context, factor=factor, intensity_unit=model_unit,
                    conditional=ent is not None and ent.line is not None)
                if options.parameters_separate:
                    record["files"] += tables.write_formats(
                        stage, "parameters", name, options, tables.PARAMETER_NAMES,
                        tables.PARAMETER_UNITS, tables.PARAMETER_ROLES, comment, parameter_rows)
            if options.curves and x is not None and factor is not None:
                try:
                    grid, total, parts = tables.model_curves(fit, manifest, x, qe, options.split_sides)
                    labels = tables.component_names(manifest)
                except (ValueError, OSError, TypeError, KeyError) as exc:
                    log.warning("export: %s curve unavailable: %s", entry.scan_id, exc)
                    problems.append(f"Fit curve unavailable: {exc}")
                else:
                    names = ["Energy", "Total fit", *[tables.curve_name(p, labels) for p, _ in parts]]
                    values = [total * factor, *[v * factor for _, v in parts]]
                    record["files"] += tables.write_formats(
                        stage, "fits", name, options, names,
                        ["meV", *[model_unit] * len(values)], ["X", *["Y"] * len(values)],
                        comment, zip(grid - shift, *values))
    return record, parameter_rows


def build(root, stage, progress, options):
    from .export import _sanitize
    card, snapshots = _snapshot(root)
    names, used = [], set()
    for entry, detector, *_ in snapshots:
        label = (entry.provenance or {}).get("scan_label") or entry.scan_number
        base = f"{_sanitize(label)}__{_sanitize(detector or 'none')}"
        name, n = base, 2
        while name.casefold() in used:
            name, n = f"{base}__{n}", n + 1
        names.append(name)
        used.add(name.casefold())
    qe = getattr(card.policy, "quasielastic_fwhm", None)
    records, parameters = [], []
    # Small shared-machine pool; independent spectra and NumPy convolution release the GIL.
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(snapshots)))) as pool:
        results = pool.map(lambda pair: _spectrum(stage, options, qe, *pair), zip(names, snapshots))
        for i, (record, rows) in enumerate(results, 1):
            records.append(record)
            parameters.extend(rows)
            if progress is not None:
                progress(i, len(snapshots))
    combined = []
    if options.parameters_combined:
        combined = tables.write_formats(stage, "", "parameters", options, tables.PARAMETER_NAMES,
                                       tables.PARAMETER_UNITS, tables.PARAMETER_ROLES,
                                       "Authoritative whole-model parameters", parameters)
    problems = [{"spectrum_id": r["spectrum_id"], "detector": r["detector"], "problem": p}
                for r in records for p in r["problems"]]
    text = README + "\nProblems\n--------\n" + ("\n".join(
        f"{p['spectrum_id']}: {p['problem']}" for p in problems) if problems else "None") + "\n"
    with (stage / "README.txt").open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
    return ({"schema": SCHEMA, "workspace": str(root.resolve()), "options": options.to_dict(),
             "spectra": records, "combined_files": combined, "problems": problems},
            (len(records), 0, 0, 0, tuple(problems), 0))
