"""Reconstruct a model curve from reported parameters, for the visual audit.

The audit figure overlays the human expert's (provisional) fit on the data, next to ISAR's. The
expert's by-hand fit gives us its ω₀, Γ, energy zero and resolution but not the raw lineshape, so we
rebuild the curve here: fix those nonlinear values and solve only the linear amplitudes by NNLS — the
same component model (`isar.analyze.components`) and the same amplitude solve ISAR's IC uses
(`isar.analyze.ic`). This is presentation only; it changes no fitting logic.
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
from scipy.optimize import nnls

from isar.analyze.components import CompositeModel, ConstantBackground, Elastic, PhononDHO

_OMEGA_D_FLOOR = 1e-6  # keep Ω_d real when a reported ω₀ < Γ (an overdamped/blown-up expert fit)


def assemble_curve(comp, x_eval, *, omega0, gamma, amp, ctx):
    """Assemble ``theta`` from fixed nonlinear (ω₀, Γ) + a linear-amplitude dict, then evaluate.

    The nonlinear phonon values are fixed to the reported ω₀/Γ (Ω_d = √max(ω₀²−Γ², floor)); every
    linear amplitude is taken from ``amp`` (``{global_param_name: value}``, default 0.0 when absent).
    Returns the same ``SimpleNamespace`` (``x``, ``theta``, ``total``, ``components``, ``omega0``,
    ``gamma``) that :func:`reconstruct_curve` returns. Shared by :func:`reconstruct_curve` (``amp``
    solved by NNLS against counts) and ``isar.workspace_api`` (``amp`` read from a stored
    ``FitOutcome.amplitudes``) so a params-only evaluator and the counts-based reconstruction use one
    theta-assembly path.
    """
    omega_d = float(np.sqrt(max(float(omega0) ** 2 - float(gamma) ** 2, _OMEGA_D_FLOOR)))
    theta = []
    for pn in comp.param_names():
        if pn.endswith(".Omega_d"):
            theta.append(omega_d)
        elif pn.endswith(".Gamma"):
            theta.append(float(gamma))
        else:
            theta.append(float(amp.get(pn, 0.0)))
    xe = np.asarray(x_eval, dtype=float)
    return SimpleNamespace(
        x=xe, theta=theta,
        total=comp.evaluate(xe, theta, ctx),
        components=comp.evaluate_components(xe, theta, ctx),
        omega0=float(omega0), gamma=float(gamma))


def reconstruct_curve(x, counts, *, omega0, gamma, center, gauss_fwhm, lorentz_fwhm, T,
                      x_eval=None):
    """Rebuild an elastic+phonon+background curve at fixed (ω₀, Γ, center, resolution).

    The nonlinear values are fixed to the reported ones; the linear amplitudes (phonon area, elastic
    area, background) are solved by NNLS against `counts`. Returns a namespace with `total`,
    per-component `components` ([(name, values), …]), and the assembled `theta`. `x_eval` (optional)
    evaluates the recovered curve on a finer grid for plotting.
    """
    x = np.asarray(x, dtype=float)
    counts = np.asarray(counts, dtype=float)
    ctx = SimpleNamespace(center=float(center), gauss_fwhm=float(gauss_fwhm),
                          lorentz_fwhm=float(lorentz_fwhm), T=float(T))
    comp = CompositeModel([PhononDHO(), Elastic(), ConstantBackground()])

    omega_d = float(np.sqrt(max(float(omega0) ** 2 - float(gamma) ** 2, _OMEGA_D_FLOOR)))
    nl = {"phonon.Omega_d": omega_d, "phonon.Gamma": float(gamma)}
    phi, names = comp.nnls_basis(x, nl, ctx)
    amps, _ = nnls(phi, counts)
    amp = dict(zip(names, amps))

    xe = x if x_eval is None else np.asarray(x_eval, dtype=float)
    return assemble_curve(comp, xe, omega0=omega0, gamma=gamma, amp=amp, ctx=ctx)
