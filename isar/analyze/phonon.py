"""Phonon-owned analysis machinery: everything that encodes the damped-harmonic-oscillator
(DHO) assumption, kept in one module so the fit, selection and manifest layers stay
excitation-agnostic.

Owns the (Ω_d,Γ) ↔ ω₀ conversions and their error propagation, fit-success assessment,
the parameter bounds and Γ-bound diagnostics, the grid+NNLS initial conditions (§3), and
the physics-gated enumeration of admissible peak structures (§2).

Import direction: :mod:`isar.analyze.components` imports this module at module level, so
this module must never import ``components`` at module level. The three places that need
component classes import them inside the function, each marked with the cycle it avoids.
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import nnls

from isar.analyze import estimators
from isar.analyze.lineshapes import KB
from isar.analyze.structures import Hypothesis

# ----------------------------------------------------------------------------
# Reparameterization helpers
# ----------------------------------------------------------------------------
def omega0_from(omega_d: float, gamma: float) -> float:
    """ω₀ = √(Ω_d² + Γ²)."""
    return float(np.sqrt(omega_d**2 + gamma**2))


def omega_d_from(omega0: float, gamma: float) -> float:
    """Ω_d = √(ω₀² − Γ²) (0 if overdamped)."""
    disc = omega0**2 - gamma**2
    return float(np.sqrt(disc)) if disc > 0 else 0.0


def bose_occupation(omega0: float, T: float) -> float:
    """The thermal occupation factor (n(ω₀,T)+1) = 1/(1−e^(−ω₀/k_BT)) on the Stokes side.

    The universal factor of every phonon amplitude (H4 owner ruling 2026-08-08): the *fitted*
    amplitude is the intrinsic strength and the model multiplies by (n+1) explicitly, so the
    Stokes/anti-Stokes detailed-balance tie stays exact ((n+1)·e^(−ω/kT) = n) and the intrinsic
    amplitude is temperature/occupation-decoupled — the quantity constraint transport carries.
    T ≤ 0 (no thermal occupation defined; the lineshape's own anti-Stokes guard) and ω₀ ≤ 0
    (defensive; bounds keep Ω_d ≥ ε) both return 1.0 — the factor never silently blows up.
    """
    if T <= 0.0 or omega0 <= 0.0:
        return 1.0
    return float(1.0 / (1.0 - np.exp(-float(omega0) / (KB * float(T)))))


def omega0_value_error(omega_d: float, gamma: float, cov) -> tuple[float, float]:
    """ω₀ and its symmetric error, propagating the full (Ω_d,Γ) covariance.

    σ_ω₀² = (Ω_d/ω₀)²σ_Ωd² + (Γ/ω₀)²σ_Γ² + 2(Ω_dΓ/ω₀²)cov(Ω_d,Γ). The cross term is
    mandatory (Ω_d, Γ are anticorrelated). Returns NaN error if covariance is missing.
    """
    omega0 = omega0_from(omega_d, gamma)
    if cov is None:
        return omega0, float("nan")
    if omega0 == 0.0:
        # Ω_d = Γ = 0: both derivatives are ∂ω₀/∂· = 0/0, so the covariance cannot be
        # propagated at all. Same contract as a missing covariance — an unusable error.
        return omega0, float("nan")
    var_od, var_g, cov_og = cov[0, 0], cov[1, 1], cov[0, 1]
    d_od, d_g = omega_d / omega0, gamma / omega0
    var = d_od**2 * var_od + d_g**2 * var_g + 2.0 * d_od * d_g * cov_og
    return omega0, float(np.sqrt(var)) if var > 0 else float("nan")


# ----------------------------------------------------------------------------
# Success assessment
# ----------------------------------------------------------------------------
def assess_success(x, errs, at_limit, backend_success, covariance_ok, eps_omega_d,
                   rtol=1e-2) -> bool:
    """Fit succeeds iff: backend converged, covariance usable, all σ finite & >0,
    Ω_d not pinned at ε (ω₀/Γ unseparable) and its error doesn't swamp it, and Ω_d not
    at the upper bound. Γ at its floor is NOT a failure (handled via a separate flag)."""
    if not backend_success or not covariance_ok or errs is None:
        return False
    errs = np.asarray(errs, dtype=float)
    if not np.all(np.isfinite(errs)) or np.any(errs <= 0):
        return False
    omega_d = float(x[0])
    if omega_d <= eps_omega_d * (1.0 + rtol):
        return False
    if errs[0] >= omega_d:
        return False
    if at_limit.get("omega_d", False):  # low already excluded → this is the upper bound
        return False
    return True

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------
EPS_OMEGA_D = 1e-3
GAMMA0 = 0.3
STEP = 0.25
PRIOR_HALF_WIDTH = 1.5  # meV grid half-window when a dispersion prior is present

GAMMA_FLOOR = 1e-3
GAMMA_MAX = 5.0

# Anti-Stokes is dropped only when its *predicted counts* — Stokes excursion × the
# detailed-balance weight exp(−ħω/kT) — cannot be told from the background Poisson noise.
# Detectability, not temperature alone: a weight-suppressed anti-Stokes on a huge Stokes peak
# is still resolvable (keep it); a moderately-suppressed one under a high background at short
# counting time is not (drop it). Governs the Stokes-only high-q rescue (steering Phase 3a).
AS_DETECT_Z = 3.0        # predicted anti-Stokes must clear this × √background to be resolvable
AS_MIN_COUNTS = 2.0      # …and this absolute floor (a fraction of a count is never resolvable)

# A phonon Γ this close to the floor is a resolution-limited δ-spike, not a fitted intrinsic
# width: ω₀ = √(Ω_d²+Γ²) ≈ Ω_d with Γ unmovable, so the parametric bootstrap collapses to a
# degenerate ω₀ interval (halfwidth ~1e-3 meV) that is spuriously tight, not trustworthy. Fits
# with any Γ at or below this tolerance (or at GAMMA_MAX, a runaway-broad DHO) get
# their ω₀ interval flagged unreliable so the σ-calibration / downstream can filter them out
# (an honestly-flagged interval beats a silently repaired one — ISAR philosophy). 5×floor
# separates resolution-limited spikes (≤0.005 meV) from genuinely-fit widths (≳0.05 meV) with
# wide margin, and is a flag, never a value edit.
GAMMA_BOUND_TOL = 5.0 * GAMMA_FLOOR

# cost/dof of the best converged 1-mode fit above which the two-stage loop offers the 2-mode
# hypothesis set. A documented default (Baker–Cousins -2lnL/dof; a good fit sits near 1),
# deliberately loose — the runs-test structured-residual clause catches the wide-window trap
# where cost/dof looks acceptable — and NOT tuned to any single toy spectrum.
GATE_COST = 2.0

# ----------------------------------------------------------------------------
# Bounds and Γ-bound diagnostics
# ----------------------------------------------------------------------------
def _gamma_at_bound(gammas) -> bool:
    """True if any phonon Γ has railed out — pinned near GAMMA_FLOOR (resolution-limited spike)
    or GAMMA_MAX (runaway-broad) — so its derived ω₀ interval is not trustworthy."""
    for g in gammas:
        g = float(g)
        if g <= GAMMA_BOUND_TOL or g >= GAMMA_MAX * (1.0 - 1e-3):
            return True
    return False


def _bound(name: str, x_max: float, cmax: float) -> tuple[float, float]:
    """Per-parameter bounds, mirroring ``synth.fit._build_problem`` exactly."""
    if name.endswith(".Omega_d"):
        return (EPS_OMEGA_D, x_max)
    if name.endswith(".Gamma"):
        return (GAMMA_FLOOR, GAMMA_MAX)
    if name.endswith(".A") or name.endswith(".E"):
        return (0.0, 1e4 * cmax)
    if name.endswith(".BG"):
        return (0.0, 5.0 * cmax + 10.0)
    return (-np.inf, np.inf)

# ----------------------------------------------------------------------------
# Grid+NNLS initial conditions (§3)
# ----------------------------------------------------------------------------
def omega_d_grid(x, x_max, prior_omega=None, *, eps=EPS_OMEGA_D, step=STEP,
                 half_width=PRIOR_HALF_WIDTH) -> np.ndarray:
    """Ω_d candidate grid. Without a prior: ``arange(max(eps, dx), x_max, step)`` (the
    frozen ``initial_guess`` grid). With a prior ω(Q): a narrow window around it."""
    x = np.asarray(x, dtype=float)
    dx = float(x[1] - x[0]) if x.size > 1 else step
    if prior_omega is None:
        return np.arange(max(eps, dx), x_max, step)
    lo = max(eps, float(prior_omega) - half_width)
    hi = min(float(x_max), float(prior_omega) + half_width)
    return np.arange(lo, hi + step / 2.0, step)


def phonon_structure_ic(composite, ctx, *, gamma0=GAMMA0, estimator="baker_cousins") -> np.ndarray:
    """Initial parameter vector (in ``composite.param_names()`` order) for this context.

    Scans Ω_d (Γ fixed at ``gamma0``); at each candidate solves the active components'
    linear amplitudes by NNLS and scores with the estimator; returns the lowest-cost seed.
    Fits the masked-in points only (``ctx.x_used``).
    """
    x = ctx.x_used
    counts = ctx.counts_used
    x_max = float(np.max(x))
    est = estimators.get_estimator(estimator)
    linear_set = set(composite.linear_names())

    if not composite.nonlinear_names():
        # Phonon-less composite (elastic+bg / bg): every param is linear, so one NNLS solve IS
        # the IC — no Ω_d to grid-scan, and the grid (empty on a Stokes-free window) is
        # irrelevant to this structure.
        phi, lin_names = composite.nnls_basis(x, {}, ctx)
        coef, _ = nnls(phi, counts)
        lin = dict(zip(lin_names, coef))
        return np.array([max(float(lin[n]), 0.0) for n in composite.param_names()], dtype=float)

    best_cost, best = np.inf, None
    for od in omega_d_grid(x, x_max, ctx.prior_omega, eps=EPS_OMEGA_D):
        nl = {name: (od if name.endswith(".Omega_d") else gamma0)
              for name in composite.nonlinear_names()}
        phi, lin_names = composite.nnls_basis(x, nl, ctx)
        coef, _ = nnls(phi, counts)
        cost = est.cost(counts, phi @ coef)
        if cost < best_cost:
            best_cost, best = cost, (float(od), dict(zip(lin_names, coef)))

    if best is None:
        raise ValueError(
            f"phonon_structure_ic: empty Ω_d candidate grid (x_used spans {float(np.min(x)):g}.."
            f"{x_max:g} meV) — the fit window has no Stokes-side (positive deltaE) coverage")
    od, lin = best
    out = []
    for name in composite.param_names():
        if name in linear_set:
            out.append(max(float(lin[name]), 0.0))
        elif name.endswith(".Omega_d"):
            out.append(od)
        else:  # a nonlinear non-Ω_d param (Γ) — fixed at gamma0 during IC
            out.append(gamma0)
    return np.array(out, dtype=float)


def _peak_omega0(peak):
    """ω₀ hint out of a peak declaration (attribute ``omega0`` or mapping key)."""
    if peak is None:
        return None
    if hasattr(peak, "omega0"):
        return float(peak.omega0)
    try:
        return float(peak["omega0"])
    except (TypeError, KeyError):
        return None


def greedy_multimode_ic(composite, ctx, one_mode_result, peaks=None, *, gamma0=GAMMA0,
                        estimator="baker_cousins") -> dict:
    """Greedy seed for a multi-mode composite: freeze mode-1 nonlinears at the 1-mode best fit
    (§ mode-1 fixed), then grid-scan mode-2's Ω_d over the residual, solving ALL linear
    amplitudes jointly by NNLS at each candidate and keeping the lowest Baker–Cousins cost.

    Mode-1 nonlinears come from ``one_mode_result`` (its fitted ``phonon.Omega_d/Gamma``); when
    it is ``None`` they are seeded internally by :func:`phonon_structure_ic` on a single-phonon+bg
    composite. ``peaks`` (declared expectations, ordered — ``peaks[0]`` is mode 1) narrows the
    mode-2 scan to ±``PRIOR_HALF_WIDTH`` meV around ``peaks[1]``'s ω₀ hint. Candidates within
    one resolution FWHM of mode-1 are skipped (identifiability). Returns a full seed dict keyed
    by ``composite.param_names()`` (Γ's at ``gamma0``)."""
    x = ctx.x_used
    counts = ctx.counts_used
    x_max = float(np.max(x))
    est = estimators.get_estimator(estimator)
    linear_set = set(composite.linear_names())

    od_names = [n for n in composite.nonlinear_names() if n.endswith(".Omega_d")]
    prefixes = [n.rsplit(".", 1)[0] for n in od_names]
    mode1, mode2 = prefixes[0], prefixes[1]

    # -- mode-1 nonlinears: from the 1-mode fit, or seeded internally when absent -----------
    if one_mode_result is not None and getattr(one_mode_result, "params", None):
        params = one_mode_result.params
        od1 = float(next(v for k, v in params.items() if k.endswith(".Omega_d")))
        g1 = float(next(v for k, v in params.items() if k.endswith(".Gamma")))
    else:
        from isar.analyze.components import CompositeModel, ConstantBackground, PhononDHO
        tmp = CompositeModel([PhononDHO(prefix=mode1), ConstantBackground()])
        seed = phonon_structure_ic(tmp, ctx, gamma0=gamma0, estimator=estimator)
        tnames = tmp.param_names()
        od1 = float(seed[tnames.index(f"{mode1}.Omega_d")])
        g1 = float(seed[tnames.index(f"{mode1}.Gamma")])
    w1 = omega0_from(od1, g1)

    # -- mode-2 grid: full model-free scan, narrowed around a declared peak hint ------------
    prior2 = _peak_omega0(peaks[1]) if peaks is not None and len(peaks) >= 2 else None
    grid = omega_d_grid(x, x_max, prior2, eps=EPS_OMEGA_D)
    res_fwhm = float(getattr(ctx, "gauss_fwhm", 0.0) or 0.0)

    def _scan(skip_identifiable):
        best_cost, best_od2, best_lin = np.inf, None, None
        for od2 in grid:
            if skip_identifiable and abs(omega0_from(od2, gamma0) - w1) < res_fwhm:
                continue
            nl = {}
            for name in composite.nonlinear_names():
                if name == f"{mode1}.Omega_d":
                    nl[name] = od1
                elif name == f"{mode2}.Omega_d":
                    nl[name] = float(od2)
                else:  # every Γ (mode-1 and mode-2) fixed at gamma0 during IC
                    nl[name] = gamma0
            nl[f"{mode1}.Gamma"] = g1
            phi, lin_names = composite.nnls_basis(x, nl, ctx)
            coef, _ = nnls(phi, counts)
            cost = est.cost(counts, phi @ coef)
            if cost < best_cost:
                best_cost, best_od2 = cost, float(od2)
                best_lin = dict(zip(lin_names, coef))
        return best_od2, best_lin

    best_od2, best_lin = _scan(skip_identifiable=True)
    if best_od2 is None:                    # whole grid sat under mode-1: relax the guard
        best_od2, best_lin = _scan(skip_identifiable=False)
    if best_od2 is None:                    # empty grid (degenerate) — nominal offset
        best_od2, best_lin = min(float(x_max), w1 + max(res_fwhm, STEP)), {}

    out = {}
    for name in composite.param_names():
        if name in linear_set:
            out[name] = max(float(best_lin.get(name, 0.0)), 0.0)
        elif name == f"{mode1}.Omega_d":
            out[name] = od1
        elif name == f"{mode1}.Gamma":
            out[name] = g1
        elif name == f"{mode2}.Omega_d":
            out[name] = best_od2
        else:  # mode-2 Γ (and any other nonlinear) at gamma0
            out[name] = gamma0
    return out

# ----------------------------------------------------------------------------
# Admissibility gates and structure enumeration (§2)
# ----------------------------------------------------------------------------
def elastic_admissible(ctx) -> bool:
    """Elastic line admissible iff its position is within the fitted window (± one
    resolution FWHM) and the energy axis is calibratable.

    The window test is the §2/§8 off-window-elastic guard: on IN8 FX=2 (EN≥1) the elastic
    at 0 sits below the window with no in-window leverage, so an elastic NNLS column would
    take an arbitrary degenerate amplitude — exclude it instead.
    """
    if ctx.energy_status == "needs_conversion_and_zero":
        return False
    lo, hi = ctx.window
    tol = ctx.gauss_fwhm
    return (lo - tol) <= ctx.center <= (hi + tol)


def quasielastic_admissible(ctx) -> bool:
    """A broad quasi-elastic component is admissible iff the experiment card declares its
    width (``ctx.quasielastic_fwhm`` is not None — a TAS card default, §8).

    Unlike the sharp :class:`~isar.analyze.components.Elastic`, a *broad* quasi-elastic has
    leverage across the window even when the elastic centre is off-window (the IN8 FX=2,
    EN≥1 case), so it carries no in-window gate. Whether it earns its extra parameter on a
    given scan is left to model selection (§4) — it is enumerated, not assumed.
    """
    return getattr(ctx, "quasielastic_fwhm", None) is not None


def _background_and_stokes_amp(ctx):
    """Robust (background, Stokes-peak excursion) in counts from the fitted points. Background =
    median (robust to the peak); excursion = max − background (conservative: an over-estimated
    Stokes amp over-estimates the predicted anti-Stokes, biasing toward *keeping* the pair — the
    safe direction). Returns (nan, 0.0) when there are no usable counts."""
    c = np.asarray(getattr(ctx, "counts_used", []), dtype=float)
    c = c[np.isfinite(c)]
    if c.size == 0:
        return float("nan"), 0.0
    bg = float(np.median(c))
    return bg, max(float(c.max()) - bg, 0.0)


def antistokes_mode(ctx) -> str:
    """How anti-Stokes is handled for this context.

    Default ``"tied"`` — the Bose tie inside :class:`PhononDHO` (negligible as T→0, growing
    with T), so a low-T gain-side feature is fit as the elastic line, not a phantom
    anti-Stokes peak. ``"tied"`` is also the *safe default* whenever the drop cannot be
    justified (no prior, no measurable Stokes peak): dropping a side removes model freedom, so
    do it only on positive evidence.

    ``"stokes_only"`` (steering Phase 3a, high-q rescue) when the anti-Stokes is **undetectable
    against the background**, not merely temperature-suppressed. The predicted anti-Stokes
    excursion is the measured Stokes excursion × the detailed-balance weight
    ``exp(−ħω/kT)`` (ħω from the dispersion prior ``ctx.prior_omega``); it is resolvable only if
    it clears ``AS_DETECT_Z × √background`` and the absolute ``AS_MIN_COUNTS`` floor. So a
    weight-suppressed anti-Stokes riding a strong Stokes peak stays ``tied`` (still resolvable),
    while a suppressed one under a high background / short count is dropped (buried in noise —
    the ``no_signal`` failure it otherwise causes). Combines temperature, background and
    amplitude, per the owner's note that usefulness depends on the noise floor, not just kT.
    A free/separate anti-Stokes component (high-T disambiguation, §4) remains a later extension.
    """
    e_pred = getattr(ctx, "prior_omega", None)
    T = getattr(ctx, "T", None)
    if e_pred is None or T is None:
        return "tied"
    e_pred, T = float(e_pred), float(T)
    if not (np.isfinite(e_pred) and e_pred > 0 and T > 0):
        return "tied"
    bg, stokes_amp = _background_and_stokes_amp(ctx)
    if not np.isfinite(bg) or stokes_amp <= 0:
        return "tied"                                      # no measurable Stokes peak → can't judge
    as_counts = stokes_amp * np.exp(-e_pred / (KB * T))    # predicted anti-Stokes excursion
    noise = np.sqrt(max(bg, 1.0))                          # Poisson background noise
    resolvable = as_counts >= AS_DETECT_Z * noise and as_counts >= AS_MIN_COUNTS
    return "tied" if resolvable else "stokes_only"


def enumerate_phonon_structures(ctx) -> list[Hypothesis]:
    """The admissible structure hypotheses for this scan context (§2).

    Always emits the single-phonon ``stokes+bg``; adds ``elastic+stokes+bg`` when the
    elastic line is admissible, ``stokes+qe+bg`` when the card declares a broad
    quasi-elastic width (§8 TAS), and ``elastic+stokes+qe+bg`` when both gates fire. The
    quasi-elastic is opt-in per card, not enumerated by default; model selection (§4)
    decides whether it earns its parameter. Second-phonon / sloped-BG remain later-step.

    The structure set is now assembled from the phenomenon catalogue
    (:mod:`isar.analyze.catalogue`, SCENE-1); the gates and the phonon recipe below still live
    here. Output is unchanged — labels, order and components are pinned by
    ``tests/analyze/test_catalogue.py``.
    """
    # local import: catalogue imports components, which imports this module, so it cannot come
    # in at module level (components.py -> phonon.py cycle).
    from isar.analyze.catalogue import enumerate_structures

    return enumerate_structures(ctx)


def _phonon_for(ctx, prefix: str | None = None):
    """The phonon *recipe* for this context: the bare :class:`PhononDHO` class (Bose-tied,
    default) or a Stokes-only pre-built instance when :func:`antistokes_mode` reports the
    anti-Stokes is Bose-dead (Phase 3a). Passing the instance rides the existing
    ``Hypothesis`` class-or-instance path (same mechanism as the ``phonon2`` override)."""
    # local import: components imports this module, so the classes cannot come in at
    # module level (components.py -> phonon.py cycle).
    from isar.analyze.components import PhononDHO

    if antistokes_mode(ctx) == "stokes_only":
        return PhononDHO(prefix=prefix, peak="stokes") if prefix else PhononDHO(peak="stokes")
    return PhononDHO(prefix=prefix) if prefix else PhononDHO


def enumerate_phonon_multimode(ctx, n_max: int = 2) -> list[Hypothesis]:
    """The two-mode (2-phonon) structure hypotheses for this scan context — the escalation set
    the two-stage loop (:mod:`isar.analyze.loop`) offers when the 1-mode winner's fit is poor
    or its residual is structured (§ multiplicity is data-gated, never a free knob).

    Mirrors :func:`enumerate_phonon_structures` under the identical elastic/quasi-elastic gates, but
    with a second phonon. Mode 1 keeps the default ``phonon`` prefix so each 1-mode hypothesis
    is *strictly nested* in its 2-mode counterpart (the LRT/AIC comparison in §4 is then exact);
    mode 2 gets the ``phonon2`` namespace. Capped at N=2 this round (``n_max`` is accepted for
    the later N=3 extension; values >2 are treated as 2).

    Catalogue-driven since SCENE-1 PR-b (multiplicity is the catalogue's concept: a second
    instance of the same phenomenon in the ``phonon2`` namespace); output pinned bit-identical
    to the legacy hand enumeration by ``tests/analyze/test_catalogue.py``."""
    # local import: catalogue imports components, which imports this module, so it cannot come
    # in at module level (components.py -> phonon.py cycle).
    from isar.analyze.catalogue import enumerate_multimode_structures

    return enumerate_multimode_structures(ctx, n_max)
