"""Params-only model-curve evaluation for :mod:`isar.workspace_api` (the P0-3 payoff).

Two paths, one return shape (``SimpleNamespace(x, total, components, pure)``):

* **pure** — when the fit stored its linear ``amplitudes``: rebuild the curve directly from
  ``(omega0, gamma, resolution-context, amplitudes)`` with no NNLS and no counts, using the shared
  theta-assembly helper :func:`isar.report.audit.assemble_curve`. No NNLS solve on this path (but
  note ``assemble_curve`` is imported from ``isar.report.audit``, which transitively imports scipy —
  so even the pure path is not scipy-free once evaluation runs; only ``Workspace.open`` stays lazy).
* **fallback** — no amplitudes (e.g. the frozen MVP-16 fixture predates them): re-parse the scan's
  counts from ``data/*.json`` and call :func:`isar.report.audit.reconstruct_curve` (an NNLS amplitude
  solve — the same call the report's visual-audit path makes). ``reconstruct_curve`` is imported
  lazily so ``Workspace.open`` stays scipy-free.

The x-grid defaults to the scan's measured energy grid (so pure and fallback are directly
comparable); pass ``x_eval`` for a finer grid.
"""
from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import numpy as np

class UnsupportedFitView(ValueError):
    """The persisted delivered model cannot be reconstructed by this reader."""


def _finite_float(value, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise UnsupportedFitView(f"{label} must be numeric") from exc
    if not math.isfinite(result):
        raise UnsupportedFitView(f"{label} must be finite")
    return result


def _find_data_file(data_dir: Path, scan_number) -> Path | None:
    """Locate ``data/j-<scan>.json``. Tries the padded name first, then matches any file whose
    trailing digit run equals the scan number (the driver's ``j-{scan:04d}`` convention, robust to
    padding drift). ``None`` if none matches."""
    if scan_number is None or not data_dir.exists():
        return None
    direct = data_dir / f"j-{int(scan_number):04d}.json"
    if direct.exists():
        return direct
    for f in sorted(data_dir.glob("*.json")):
        m = re.search(r"(\d+)\s*$", f.stem)
        if m and int(m.group(1)) == int(scan_number):
            return f
    return None


def _load_scan_arrays(data_dir: Path, scan_number):
    """``(energy, counts)`` kept-finite arrays from the scan's ``data/*.json``, or ``(None, None)``.

    Mirrors the report's audit masking (``valid_mask & finite``) so the fallback curve matches the
    counts the fit itself used. Tolerant: any read/parse problem yields ``(None, None)``.
    """
    path = _find_data_file(data_dir, scan_number)
    if path is None:
        return None, None
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    # Everything downstream of the read is wrapped: a non-object JSON root (list/str -> .get
    # AttributeError), non-numeric arrays (asarray ValueError/TypeError), non-1-D arrays, or a
    # mask-shape mismatch must all degrade to (None, None), not escape as an exception.
    try:
        result = d.get("result") or {}
        energy, counts = result.get("scan_values_1"), result.get("counts")
        if energy is None or counts is None:
            return None, None
        e = np.asarray(energy, dtype=float)
        c = np.asarray(counts, dtype=float)
        if e.ndim != 1 or c.ndim != 1:
            return None, None
        if e.shape != c.shape:
            w = min(e.size, c.size)
            e, c = e[:w], c[:w]
        mask = result.get("valid_mask_1")
        if mask is not None:
            m = np.asarray(mask, dtype=bool)
            if m.size >= e.size:
                m = m[:e.size]
            else:
                # A mask shorter than the energy array leaves the tail unspecified: treat those
                # omitted positions as EXCLUDED (False), never silently valid.
                m = np.concatenate([m, np.zeros(e.size - m.size, bool)])
            keep = m & np.isfinite(e) & np.isfinite(c)
        else:
            keep = np.isfinite(e) & np.isfinite(c)
        return e[keep], c[keep]
    except (AttributeError, TypeError, ValueError):
        return None, None


def _component_manifest(fit, quasielastic_fwhm):
    """Return a model manifest, deriving the pre-manifest multi-mode form when possible."""
    manifest = getattr(fit, "model_manifest", None)
    if manifest is not None:
        if not isinstance(manifest, Mapping):
            raise UnsupportedFitView("model manifest must be an object")
        return manifest
    amplitudes = fit.amplitudes or {}
    prefixes = {str(key).split(".")[0] for key in amplitudes}
    phonons = sorted((p for p in prefixes if re.fullmatch(r"phonon\d*", p)),
                     key=lambda p: (p != "phonon", int(p[6:] or 1)))
    unknown = prefixes - set(phonons) - {"elastic", "qe", "bg"}
    if unknown:
        raise UnsupportedFitView(f"unsupported stored component prefixes: {sorted(unknown)}")
    if not phonons:
        return None
    modes = []
    for index, mode in enumerate(getattr(fit, "modes", None) or []):
        if not isinstance(mode, Mapping):
            raise UnsupportedFitView(f"stored mode {index} must be an object")
        if mode.get("omega0") is None:
            continue
        normalized = dict(mode)
        normalized["omega0"] = _finite_float(mode["omega0"], f"stored mode {index} omega0")
        if mode.get("gamma") is not None:
            normalized["gamma"] = _finite_float(mode["gamma"], f"stored mode {index} gamma")
        modes.append(normalized)
    modes.sort(key=lambda mode: mode["omega0"])
    if not modes and fit.omega0 is not None:
        modes = [{"omega0": fit.omega0, "gamma": fit.gamma}]
    if len(modes) < len(phonons):
        raise UnsupportedFitView(
            f"stored {len(phonons)} phonon components but only {len(modes)} mode parameter sets")
    components = [{"type": "phonon_dho", "prefix": prefix,
                   "peak": "stokes+astokes"} for prefix in phonons]
    components.extend({"type": kind, "prefix": prefix} for prefix, kind in (
        ("elastic", "elastic"), ("qe", "quasielastic"),
        ("bg", "constant_background")) if prefix in prefixes)
    parameters = dict(amplitudes)
    for prefix, mode in zip(phonons, modes):
        omega0 = _finite_float(mode["omega0"], f"{prefix} omega0")
        gamma = _finite_float(
            mode.get("gamma") if mode.get("gamma") is not None else fit.gamma or 0.0,
            f"{prefix} gamma",
        )
        parameters[f"{prefix}.Omega_d"] = math.sqrt(max(omega0 * omega0 - gamma * gamma, 1e-6))
        parameters[f"{prefix}.Gamma"] = gamma
    return {"schema": "isar.model_manifest/1", "components": components,
            "parameters": parameters}


def _compose_manifest(manifest: dict, quasielastic_fwhm):
    """Build a registered composite and parameter vector from declarative persisted data.

    Thin caller of the shared analysis-owned reconstruction
    (:mod:`isar.analyze.model_manifest`) so a workspace curve and a stored predictive matrix can
    never be built by two different implementations; its structured refusals are re-raised as the
    reader's own ``UnsupportedFitView``.
    """
    from isar.analyze.model_manifest import ModelManifestError, compose_manifest
    try:
        return compose_manifest(manifest, quasielastic_fwhm)
    except ModelManifestError as exc:
        raise UnsupportedFitView(str(exc)) from exc


def evaluate_model(fit, data_dir, *, quasielastic_fwhm=None, x_eval=None):
    """Evaluate the delivered model curve for one :class:`~isar.workspace_api.views.FitView`.

    ``fit`` is a FitView; ``data_dir`` the workspace ``data/`` directory (for the fallback counts).
    Returns ``SimpleNamespace(x, total, components, pure)`` — ``components`` is ``[(name, values), …]``
    that sum to ``total``; ``pure`` flags which path ran. Raises ``ValueError`` only when neither path
    can produce a curve (no amplitudes AND no re-parseable counts, or the fit lacks ω₀/Γ/resolution).
    """
    data_dir = Path(data_dir)
    energy, counts = _load_scan_arrays(data_dir, fit.scan_number)
    x = np.asarray(x_eval, dtype=float) if x_eval is not None else energy
    res = fit.resolution_context()
    omega0, gamma = fit.omega0, fit.gamma

    # --- pure path ---------------------------------------------------------
    manifest = _component_manifest(fit, quasielastic_fwhm)
    if manifest is not None and res is not None and x is not None:
        # An explicit override wins; otherwise fall back to a width the resolution context itself
        # carries, so a stored quasielastic_fwhm is not lost (which would make _compose refuse a qe
        # component present in the amplitudes).
        effective_qe = quasielastic_fwhm if quasielastic_fwhm is not None \
            else res.get("quasielastic_fwhm")
        comp, theta = _compose_manifest(manifest, effective_qe)
        res = {k: v for k, v in res.items() if k != "quasielastic_fwhm"}
        ctx = SimpleNamespace(quasielastic_fwhm=effective_qe, **res)
        total = comp.evaluate(x, theta, ctx)
        return SimpleNamespace(x=x, total=total,
                               components=comp.evaluate_components(x, theta, ctx), pure=True)

    # --- fallback path -----------------------------------------------------
    if (energy is None or counts is None or energy.size == 0
            or res is None or omega0 is None or gamma is None):
        raise ValueError(
            f"cannot evaluate scan {fit.scan_number}/{fit.detector_id}: "
            "no stored amplitudes and no re-parseable counts + resolution context")
    if "resolution_curve" in res:   # the fallback rebuilds a Voigt: it would show a model that was not fitted
        raise ValueError(f"cannot evaluate scan {fit.scan_number}/{fit.detector_id}: "
                         "a tabulated-resolution fit needs its stored amplitudes")
    from isar.report.audit import reconstruct_curve  # lazy: pulls scipy only on the fallback
    rc = reconstruct_curve(energy, counts, omega0=omega0, gamma=gamma,
                           center=res["center"], gauss_fwhm=res["gauss_fwhm"],
                           lorentz_fwhm=res["lorentz_fwhm"], T=res["T"], x_eval=x_eval)
    return SimpleNamespace(x=rc.x, total=rc.total, components=rc.components, pure=False)
