"""Human-readable numerical export tables, built only from recorded models."""
from __future__ import annotations

import csv
import math
from pathlib import Path

import numpy as np

from isar.workspace_api.evaluate import _component_manifest, evaluate_manifest
from isar.workspace_api.spectra import _mode_rows
from isar.workspace_api.views import FitView


def cell(value):
    if isinstance(value, (float, np.floating)):
        return format(value, ".12g") if math.isfinite(value) else ""
    return "" if value is None else value


def write_table(path: Path, names, units, roles, comments, rows, *, origin=False):
    """Neutral CSV has one header; Origin has Long Name, Units, Comments, Designation rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig" if origin else "utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(names if origin else [f"{n} ({u})" if u else n for n, u in zip(names, units)])
        if origin:
            writer.writerow(units)
            writer.writerow(comments)
            writer.writerow(roles)
        writer.writerows([cell(v) for v in row] for row in rows)


def write_formats(stage, category, name, options, names, units, roles, comment, rows):
    rows = list(rows)
    files = []
    for origin, enabled in ((False, options.csv), (True, options.origin)):
        if not enabled:
            continue
        relative = Path("origin" if origin else "csv") / category / f"{name}.csv"
        write_table(stage / relative, names, units, roles, [comment] * len(names), rows, origin=origin)
        files.append(relative.as_posix())
    return files


def model_view(outcome, entry, detector):
    return FitView(row=None, outcome=outcome, scan_number=entry.scan_number,
                   detector_id=detector, unit_id=entry.scan_id)


def model_manifest(fit, qe):
    manifest = _component_manifest(fit, qe)
    if manifest is None:
        raise ValueError("No stored whole-model parameters")
    return manifest


def model_curves(fit, manifest, x, qe, split_sides):
    finite = np.asarray(x, dtype=float)
    finite = finite[np.isfinite(finite)]
    if not finite.size:
        raise ValueError("No finite energy axis for the fit")
    # At least 2000 samples: separate from the measured points, with a cap on file size.
    grid = np.linspace(float(finite.min()), float(finite.max()), 2001)
    resolution = fit.resolution_context()
    total, parts = evaluate_manifest(manifest, resolution, grid, qe, split_sides=split_sides)
    return grid, total, parts


def component_names(manifest):
    parameters = manifest["parameters"]
    modes = sorted((item for item in manifest["components"] if item["type"] == "phonon_dho"),
                   key=lambda item: (parameters[f"{item['prefix']}.omega0"], item["prefix"]))
    names = {item["prefix"]: f"Mode {i}" for i, item in enumerate(modes, 1)}
    labels = {"elastic": "Elastic", "quasielastic": "Quasielastic",
              "central_qe": "Quasielastic", "constant_background": "Background"}
    names.update({item["prefix"]: labels[item["type"]] for item in manifest["components"]
                  if item["type"] != "phonon_dho"})
    return names


def curve_name(name, labels):
    prefix, _, side = name.partition(".")
    return labels[prefix] + ({"stokes": " Stokes", "astokes": " anti-Stokes"}.get(side, ""))


PARAMETER_NAMES = ["Scan", "Analyzer", "Temperature", "h", "k", "l", "Component",
                   "Parameter", "Value", "Uncertainty", "Units", "Uncertainty type", "Fit", "Notes"]
PARAMETER_UNITS = ["", "", "K", "rlu", "rlu", "rlu", "", "", "", "", "", "", "", ""]
PARAMETER_ROLES = ["Label", "Label", "X", "X", "X", "X", "Label", "Label", "Y", "yEr",
                   "Label", "Label", "Label", "Label"]


def parameter_rows(fit, manifest, context, *, factor=1.0, intensity_unit="counts", conditional=False):
    """One readable row per parameter. Missing errors stay empty; fixed values are identified."""
    labels = component_names(manifest)
    measured = {row.prefix: row for row in _mode_rows(fit, "fast")}
    descriptions = {"omega0": ("Undamped energy E0", "meV"),
                    "Gamma": ("Damping Gamma (HWHM)", "meV"),
                    "h": ("Convolved peak height", intensity_unit),
                    "BG": ("Constant background", intensity_unit),
                    "QE": ("Quasielastic area", f"{intensity_unit} meV"),
                    "width": ("Width (FWHM)", "meV")}
    # Covariance is not persisted for the whole model. Use the saved component errors,
    # leaving the remaining uncertainties empty rather than estimating them on export.
    qe = (fit.fast_pass or {}).get("qe") or {}
    errors = {"qe.h": qe.get("sigma_height"), "qe.width": qe.get("sigma_width")}
    peaks = {peak.get("index"): peak for peak in (fit.fast_pass or {}).get("peaks") or ()}
    for prefix, mode in measured.items():
        peak = peaks.get(mode.number - 1, {})
        errors[f"{prefix}.h"] = peak.get("sigma_height")
    rows = []
    prefix_context = [context["scan"], context["detector"], context.get("temperature"),
                      *(context.get("hkl") or [None, None, None])]
    for key, value in manifest["parameters"].items():
        prefix, _, parameter = key.partition(".")
        description, unit = descriptions.get(parameter, (parameter, ""))
        scale = factor if parameter in ("h", "BG", "QE") else 1.0
        uncertainty = errors.get(key)
        mode = measured.get(prefix)
        if uncertainty is None and mode is not None:
            measure = {"omega0": mode.energy, "Gamma": mode.linewidth, "h": mode.amplitude}.get(parameter)
            if measure is not None and measure.lo is not None and measure.hi is not None:
                uncertainty = (measure.hi - measure.lo) / 2
        kind = ("Conditional standard error (neighbors held fixed)" if conditional else "Standard error")
        note = context["notes"]
        if scale is None:
            note = "; ".join(filter(None, (note, "Intensity unavailable: no reference monitor for this fit")))
        rows.append([*prefix_context, labels.get(prefix, prefix), description,
                     None if scale is None else value * scale,
                     None if uncertainty is None or scale is None else uncertainty * scale, unit,
                     kind if uncertainty is not None else "Not recorded", context["fit"], note])
    # Fixed model inputs belong alongside fitted parameters, clearly labelled as inputs.
    fixed = [("Calibration", "Energy zero", fit.energy_zero, "meV"),
             ("Resolution", "Gaussian FWHM", (fit.calib or {}).get("gauss_fwhm"), "meV"),
             ("Resolution", "Lorentzian FWHM", (fit.calib or {}).get("lorentz_fwhm"), "meV")]
    for item in manifest["components"]:
        if item.get("width") is not None:
            fixed.append((labels[item["prefix"]], "Fixed width (FWHM)", item["width"], "meV"))
    for component, description, value, unit in fixed:
        if value is not None:
            rows.append([*prefix_context, component, description, value, None, unit, "Fixed input",
                         context["fit"], context["notes"]])
    return rows
