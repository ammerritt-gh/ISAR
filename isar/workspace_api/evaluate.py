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
import re
from pathlib import Path
from types import SimpleNamespace

import numpy as np

# prefixes we can rebuild purely; anything else (e.g. a 2nd phonon "phonon2") -> fall back
_KNOWN_PREFIXES = {"phonon", "elastic", "qe", "bg"}
_COMPONENT_ORDER = ("phonon", "elastic", "qe", "bg")


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
    result = d.get("result") or {}
    energy, counts = result.get("scan_values_1"), result.get("counts")
    if energy is None or counts is None:
        return None, None
    e = np.asarray(energy, dtype=float)
    c = np.asarray(counts, dtype=float)
    if e.shape != c.shape:
        w = min(e.size, c.size)
        e, c = e[:w], c[:w]
    mask = result.get("valid_mask_1")
    if mask is not None:
        m = np.asarray(mask, dtype=bool)
        keep = (m[:e.size] if m.size >= e.size else np.ones(e.shape, bool)) & np.isfinite(e) & np.isfinite(c)
    else:
        keep = np.isfinite(e) & np.isfinite(c)
    return e[keep], c[keep]


def _compose(amplitudes: dict, quasielastic_fwhm):
    """Build the CompositeModel implied by the amplitude keys, or ``None`` if not purely rebuildable.

    Returns ``None`` when an unknown / multi-mode prefix appears, when there is no phonon (so the
    stored ω₀/Γ have nothing to drive), or when a quasi-elastic amplitude is present but no width is
    available to place it.
    """
    from isar.analyze.components import (CompositeModel, ConstantBackground, Elastic,
                                         PhononDHO, QuasiElastic)
    prefixes = {str(k).split(".")[0] for k in amplitudes.keys()}
    if not prefixes or not prefixes <= _KNOWN_PREFIXES:
        return None
    if "phonon" not in prefixes:
        return None
    if "qe" in prefixes and quasielastic_fwhm is None:
        return None
    comps = []
    for p in _COMPONENT_ORDER:
        if p not in prefixes:
            continue
        if p == "phonon":
            comps.append(PhononDHO())
        elif p == "elastic":
            comps.append(Elastic())
        elif p == "qe":
            comps.append(QuasiElastic())
        elif p == "bg":
            comps.append(ConstantBackground())
    return CompositeModel(comps)


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
    amps = fit.amplitudes
    if amps and omega0 is not None and gamma is not None and res is not None and x is not None:
        comp = _compose(amps, quasielastic_fwhm)
        if comp is not None:
            from isar.report.audit import assemble_curve
            res = {k: v for k, v in res.items() if k != "quasielastic_fwhm"}  # explicit arg wins if res ever carries this key
            ctx = SimpleNamespace(quasielastic_fwhm=quasielastic_fwhm, **res)
            rc = assemble_curve(comp, x, omega0=omega0, gamma=gamma, amp=amps, ctx=ctx)
            return SimpleNamespace(x=rc.x, total=rc.total, components=rc.components, pure=True)

    # --- fallback path -----------------------------------------------------
    if (energy is None or counts is None or energy.size == 0
            or res is None or omega0 is None or gamma is None):
        raise ValueError(
            f"cannot evaluate scan {fit.scan_number}/{fit.detector_id}: "
            "no stored amplitudes and no re-parseable counts + resolution context")
    from isar.report.audit import reconstruct_curve  # lazy: pulls scipy only on the fallback
    rc = reconstruct_curve(energy, counts, omega0=omega0, gamma=gamma,
                           center=res["center"], gauss_fwhm=res["gauss_fwhm"],
                           lorentz_fwhm=res["lorentz_fwhm"], T=res["T"], x_eval=x_eval)
    return SimpleNamespace(x=rc.x, total=rc.total, components=rc.components, pure=False)
