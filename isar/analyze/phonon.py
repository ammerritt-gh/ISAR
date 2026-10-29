"""Phonon-owned analysis machinery: everything that encodes the damped-harmonic-oscillator
(DHO) assumption, kept in one module so the fit, selection and manifest layers stay
excitation-agnostic.

Owns fit-success assessment, the Γ-bound diagnostics (the bounds themselves are the prior
recipe, :func:`isar.analyze.prior.mode_bounds`), the grid+NNLS initial conditions (§3), and
the physics-gated enumeration of admissible peak structures (§2). The fitted energy is the
undamped E0 itself (``.omega0``), so no (Ω_d, Γ) conversion lives here; the phase-1 synthetic
core keeps its own in :mod:`isar.synth.fit`.

Import direction: the places that need component or catalogue classes import them inside the
function (the catalogue imports this module at module level).
"""
from __future__ import annotations

import os
import warnings

import numpy as np
from scipy.optimize import nnls

from isar.analyze import estimators
from isar.analyze.lineshapes import KB
from isar.analyze.structures import Hypothesis

# ----------------------------------------------------------------------------
# Success assessment
# ----------------------------------------------------------------------------
def assess_success(x, errs, at_limit, backend_success, covariance_ok, eps_omega_d,
                   rtol=1e-2) -> bool:
    """Fit succeeds iff: backend converged, covariance usable, all σ finite & >0, the energy
    (``x[0]``: E0 in the fitting path, Ω_d in the phase-1 synthetic core) not pinned at ε and
    its error doesn't swamp it, and the energy not at the upper bound (``at_limit["omega_d"]``,
    the key both callers adapt). Γ at its floor is NOT a failure (handled via a separate flag)."""
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
EPS_OMEGA0 = 1e-3
GAMMA0 = 0.3
STEP = 0.25
PRIOR_HALF_WIDTH = 1.5  # meV grid half-window when a dispersion prior is present

GAMMA_FLOOR = 1e-3
GAMMA_MAX = 20.0      # the v5 width cap (prior recipe /5); every reader inherits it
GAMMA_MAX_V4 = 5.0    # today's cap: the legacy_v4 sensitivity arm only

# Anti-Stokes is dropped only when its *predicted counts* — Stokes excursion × the
# detailed-balance weight exp(−ħω/kT) — cannot be told from the background Poisson noise.
# Detectability, not temperature alone: a weight-suppressed anti-Stokes on a huge Stokes peak
# is still resolvable (keep it); a moderately-suppressed one under a high background at short
# counting time is not (drop it). Governs the Stokes-only high-q rescue (steering Phase 3a).
AS_DETECT_Z = 3.0        # predicted anti-Stokes must clear this × √background to be resolvable
AS_MIN_COUNTS = 2.0      # …and this absolute floor (a fraction of a count is never resolvable)

# A phonon Γ this close to the floor is a resolution-limited δ-spike, not a fitted intrinsic
# width: with Γ unmovable the parametric bootstrap collapses to a
# degenerate ω₀ interval (halfwidth ~1e-3 meV) that is spuriously tight, not trustworthy. Fits
# with any Γ at or below this tolerance (or at GAMMA_MAX, a runaway-broad DHO) get
# their ω₀ interval flagged unreliable so the σ-calibration / downstream can filter them out
# (an honestly-flagged interval beats a silently repaired one — ISAR philosophy). 5×floor
# separates resolution-limited spikes (≤0.005 meV) from genuinely-fit widths (≳0.05 meV) with
# wide margin, and is a flag, never a value edit.
GAMMA_BOUND_TOL = 5.0 * GAMMA_FLOOR

# Split seeds of the count ladder: every fitted mode of the (n-1)-mode fit is tried as two modes
# this many resolution FWHMs apart, "a small set spanning below and around one resolution width"
# (design 2026-10-22), with unequal heights (SPLIT_HEIGHT_RATIO : 1), the heavier member
# alternating side from one separation to the next.
SPLIT_SEPARATIONS = (0.5, 1.0)
SPLIT_HEIGHT_RATIO = 2.0

# ----------------------------------------------------------------------------
# Γ-bound diagnostics
# ----------------------------------------------------------------------------
def _gamma_at_bound(gammas) -> bool:
    """True if any phonon Γ has railed out — pinned near GAMMA_FLOOR (resolution-limited spike)
    or GAMMA_MAX (runaway-broad) — so its derived ω₀ interval is not trustworthy."""
    for g in gammas:
        g = float(g)
        if g <= GAMMA_BOUND_TOL or g >= GAMMA_MAX * (1.0 - 1e-3):
            return True
    return False

# ----------------------------------------------------------------------------
# Grid+NNLS initial conditions (§3)
# ----------------------------------------------------------------------------
def energy_grid(x, x_max, prior_omega=None, *, eps=EPS_OMEGA0, step=STEP,
                half_width=PRIOR_HALF_WIDTH) -> np.ndarray:
    """E0 (``.omega0``) candidate grid. Without a prior: ``arange(max(eps, dx), x_max, step)`` (the
    frozen ``initial_guess`` grid). With a prior ω(Q): a narrow window around it."""
    x = np.asarray(x, dtype=float)
    dx = float(x[1] - x[0]) if x.size > 1 else step
    if prior_omega is None:
        return np.arange(max(eps, dx), x_max, step)
    lo = max(eps, float(prior_omega) - half_width)
    hi = min(float(x_max), float(prior_omega) + half_width)
    return np.arange(lo, hi + step / 2.0, step)


def _width_starts(composite, ctx) -> dict:
    """``{name: resolution FWHM}`` for every nonlinear ``.width`` parameter of ``composite`` (the
    central QE's intrinsic width): not a phonon coordinate, so no IC scans it; every NNLS basis and
    seed holds it at the resolution width (:func:`isar.analyze.prior.resolution_fwhm`)."""
    from isar.analyze.prior import resolution_fwhm  # prior imports this module

    names = [n for n in composite.nonlinear_names() if n.endswith(".width")]
    if not names:
        return {}
    width = resolution_fwhm(ctx)
    return {n: width for n in names}


def phonon_structure_ic(composite, ctx, *, gamma0=GAMMA0, estimator="baker_cousins") -> np.ndarray:
    """Initial parameter vector (in ``composite.param_names()`` order) for this context.

    Scans E0 (``.omega0``, Γ fixed at ``gamma0``); at each candidate solves the active components'
    linear amplitudes by NNLS and scores with the estimator; returns the lowest-cost seed.
    Fits the masked-in points only (``ctx.x_used``).
    """
    x = ctx.x_used
    counts = ctx.counts_used
    x_max = float(np.max(x))
    est = estimators.get_estimator(estimator)
    linear_set = set(composite.linear_names())
    widths = _width_starts(composite, ctx)

    if not set(composite.nonlinear_names()) - set(widths):
        # Phonon-less composite (elastic+bg / bg, with or without the central QE): every param is
        # linear or a width held at its start, so one NNLS solve IS the IC — no energy to grid-scan,
        # and the grid (empty on a Stokes-free window) is irrelevant to this structure.
        phi, lin_names = composite.nnls_basis(x, widths, ctx)
        coef, _ = nnls(phi, counts)
        lin = dict(zip(lin_names, coef))
        return np.array([widths[n] if n in widths else max(float(lin[n]), 0.0)
                         for n in composite.param_names()], dtype=float)

    best_cost, best = np.inf, None
    for od in energy_grid(x, x_max, ctx.prior_omega, eps=EPS_OMEGA0):
        nl = {name: (od if name.endswith(".omega0") else widths.get(name, gamma0))
              for name in composite.nonlinear_names()}
        phi, lin_names = composite.nnls_basis(x, nl, ctx)
        coef, _ = nnls(phi, counts)
        cost = est.cost(counts, phi @ coef)
        if cost < best_cost:
            best_cost, best = cost, (float(od), dict(zip(lin_names, coef)))

    if best is None:
        raise ValueError(
            f"phonon_structure_ic: empty E0 candidate grid (x_used spans {float(np.min(x)):g}.."
            f"{x_max:g} meV) — the fit window has no Stokes-side (positive deltaE) coverage")
    od, lin = best
    out = []
    for name in composite.param_names():
        if name in linear_set:
            out.append(max(float(lin[name]), 0.0))
        elif name.endswith(".omega0"):
            out.append(od)
        else:  # a nonlinear non-energy param (Γ, or a QE width) — fixed at its start during IC
            out.append(widths.get(name, gamma0))
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


def _mode_prefixes(composite) -> list[str]:
    """The prefix of every phonon mode of ``composite``, in component order."""
    return [n.rsplit(".", 1)[0] for n in composite.nonlinear_names() if n.endswith(".omega0")]


def _fitted_modes(result, prefixes) -> dict:
    """``{prefix: (E0, Γ)}`` for each of ``prefixes`` the fitted ``result`` carries. A null
    (background-only) result carries none."""
    params = getattr(result, "params", None) or {}
    return {p: (float(params[f"{p}.omega0"]), float(params[f"{p}.Gamma"]))
            for p in prefixes if f"{p}.omega0" in params and f"{p}.Gamma" in params}


def _sub_composite(composite, keep):
    """``composite`` with only the phonon modes whose prefix is in ``keep``; every other
    component (elastic, quasi-elastic, background) stays."""
    from isar.analyze.components import CompositeModel, PhononDHO  # components.py -> phonon.py cycle

    return CompositeModel([c for c in composite.components
                           if not isinstance(c, PhononDHO) or c.prefix in keep])


def _seed_dict(composite, modes: dict, linear: dict, widths: dict | None = None) -> dict:
    """A full seed keyed by ``composite.param_names()``: each mode's ``(E0, Γ)`` from ``modes``,
    each linear height from ``linear`` (absent or negative → 0), each width from ``widths``."""
    linear_set = set(composite.linear_names())
    widths = widths or {}
    out = {}
    for name in composite.param_names():
        if name in linear_set:
            out[name] = max(float(linear.get(name, 0.0)), 0.0)
        elif name in widths:
            out[name] = float(widths[name])
        else:
            prefix, par = name.rsplit(".", 1)
            od, g = modes[prefix]
            out[name] = od if par == "omega0" else g
    return out


def _nnls_linear(composite, ctx, modes: dict) -> dict:
    """Every linear height of ``composite`` by NNLS with the modes' nonlinears held."""
    nl = dict(_width_starts(composite, ctx))
    for p, (od, g) in modes.items():
        nl[f"{p}.omega0"], nl[f"{p}.Gamma"] = od, g
    phi, lin_names = composite.nnls_basis(ctx.x_used, nl, ctx)
    coef, _ = nnls(phi, ctx.counts_used)
    return dict(zip(lin_names, coef))


def _scan_new_mode(composite, ctx, fixed: dict, new: str, hint, *, gamma0, estimator):
    """Grid-scan mode ``new``'s E0 over the residual of the held ``fixed`` modes
    (``{prefix: (E0, Γ)}``), solving every linear height jointly by NNLS at each candidate and
    keeping the lowest Baker–Cousins cost. ``hint`` (a declared ω₀) narrows the grid to
    ±``PRIOR_HALF_WIDTH``. Candidates within one resolution FWHM of a held mode are skipped
    (identifiability) unless that leaves none. ``composite`` must hold exactly the ``fixed`` modes
    and ``new``. Returns ``(E0, {linear name: height})``."""
    x = ctx.x_used
    counts = ctx.counts_used
    x_max = float(np.max(x))
    est = estimators.get_estimator(estimator)
    grid = energy_grid(x, x_max, hint, eps=EPS_OMEGA0)
    res_fwhm = float(getattr(ctx, "gauss_fwhm", 0.0) or 0.0)
    held_w = [od for od, _ in fixed.values()]
    base = dict(_width_starts(composite, ctx))
    for p, (od, g) in fixed.items():
        base[f"{p}.omega0"], base[f"{p}.Gamma"] = od, g

    def _scan(skip_identifiable):
        best_cost, best_od, best_lin = np.inf, None, None
        for od in grid:
            if skip_identifiable and any(abs(od - w) < res_fwhm for w in held_w):
                continue
            nl = {**base, f"{new}.omega0": float(od), f"{new}.Gamma": gamma0}
            phi, lin_names = composite.nnls_basis(x, nl, ctx)
            coef, _ = nnls(phi, counts)
            cost = est.cost(counts, phi @ coef)
            if cost < best_cost:
                best_cost, best_od, best_lin = cost, float(od), dict(zip(lin_names, coef))
        return best_od, best_lin

    od, lin = _scan(skip_identifiable=True)
    if od is None:                          # the whole grid sat under held modes: relax the guard
        od, lin = _scan(skip_identifiable=False)
    if od is None:                          # empty grid (degenerate) — nominal offset
        od, lin = min(x_max, max(held_w, default=0.0) + max(res_fwhm, STEP)), {}
    return od, lin


def _peak_hint(peaks, i):
    return _peak_omega0(peaks[i]) if peaks is not None and len(peaks) > i else None


def greedy_multimode_ic(composite, ctx, previous_result, peaks=None, *, gamma0=GAMMA0,
                        estimator="baker_cousins") -> dict:
    """The residual seed of an n-mode composite (n ≥ 2): hold the modes of ``previous_result``
    (the (n-1)-mode fit, matched by prefix; ``phonon``, ``phonon2``, ...), then grid-scan the last
    mode's E0 over their residual (:func:`_scan_new_mode`), solving ALL linear heights jointly
    by NNLS at each candidate.

    Leading modes ``previous_result`` does not carry (``None``, or a null fit) are seeded first,
    one at a time: the first by :func:`phonon_structure_ic` on a single-phonon+bg composite, each
    later one by the same residual scan. ``peaks`` (declared expectations, ordered — ``peaks[i]``
    is mode ``i+1``) narrows each scanned mode's grid around its declared ω₀. Returns a full seed
    dict keyed by ``composite.param_names()`` (scanned modes' Γ at ``gamma0``)."""
    from isar.analyze.components import CompositeModel, ConstantBackground, PhononDHO  # cycle

    prefixes = _mode_prefixes(composite)
    fixed = _fitted_modes(previous_result, prefixes[:-1])
    for i, p in enumerate(prefixes[:-1]):
        if p in fixed:
            continue
        if not fixed:
            tmp = CompositeModel([PhononDHO(prefix=p), ConstantBackground()])
            seed = phonon_structure_ic(tmp, ctx, gamma0=gamma0, estimator=estimator)
            names = tmp.param_names()
            fixed[p] = (float(seed[names.index(f"{p}.omega0")]),
                        float(seed[names.index(f"{p}.Gamma")]))
            continue
        od, _ = _scan_new_mode(_sub_composite(composite, {*fixed, p}), ctx, fixed, p,
                               _peak_hint(peaks, i), gamma0=gamma0, estimator=estimator)
        fixed[p] = (od, gamma0)
    last = prefixes[-1]
    od, lin = _scan_new_mode(composite, ctx, fixed, last, _peak_hint(peaks, len(prefixes) - 1),
                             gamma0=gamma0, estimator=estimator)
    return _seed_dict(composite, {**fixed, last: (od, gamma0)}, lin, _width_starts(composite, ctx))


def placed_modes_ic(composite, ctx, omega0s, *, gamma0=GAMMA0,
                    estimator="baker_cousins") -> dict:
    """A seed with the modes placed at ``omega0s`` (ω₀ in meV, ascending, one per mode in prefix
    order, Γ at ``gamma0``): the model-free descriptor's features or the card's declared peaks.
    Modes beyond ``len(omega0s)`` are then scanned one at a time over the residual; every linear
    height by NNLS with all modes held."""
    prefixes = _mode_prefixes(composite)
    x_max = float(np.max(ctx.x_used))
    fixed = {}
    for p, w in zip(prefixes, sorted(float(w) for w in omega0s)):
        fixed[p] = (min(max(w, 2.0 * EPS_OMEGA0), x_max), gamma0)
    for p in prefixes[len(fixed):]:
        od, _ = _scan_new_mode(_sub_composite(composite, {*fixed, p}), ctx, fixed, p, None,
                               gamma0=gamma0, estimator=estimator)
        fixed[p] = (od, gamma0)
    return _seed_dict(composite, fixed, _nnls_linear(composite, ctx, fixed),
                      _width_starts(composite, ctx))


def split_mode_ic(composite, ctx, previous_result, parent: str, separation: float, *,
                  heavier: str, ratio: float = SPLIT_HEIGHT_RATIO) -> dict | None:
    """A split seed of an n-mode composite: the (n-1)-mode fit ``previous_result`` with its mode
    ``parent`` replaced by two modes ``separation`` meV apart in ω₀, centred on the parent's ω₀,
    at half the parent's Γ, sharing the parent's height ``ratio`` : 1 (``heavier`` is
    ``"low"`` or ``"high"``). The lower member keeps the parent's prefix and the upper takes the
    composite's last one; every other parameter is the previous fit's. ``None`` when the previous
    fit does not carry the composite's first n-1 modes."""
    prefixes = _mode_prefixes(composite)
    held = _fitted_modes(previous_result, prefixes[:-1])
    if parent not in held or len(held) != len(prefixes) - 1:
        return None
    x_max = float(np.max(ctx.x_used))
    w, g_p = held[parent]
    g = max(0.5 * g_p, GAMMA_FLOOR)

    def _od(w_child):
        return min(max(w_child, 2.0 * EPS_OMEGA0), x_max)

    amp = float(previous_result.params.get(f"{parent}.h", 0.0))
    heavy, light = amp * ratio / (1.0 + ratio), amp / (1.0 + ratio)
    a_lo, a_hi = (heavy, light) if heavier == "low" else (light, heavy)
    new = prefixes[-1]
    modes = {**held, parent: (_od(w - 0.5 * separation), g), new: (_od(w + 0.5 * separation), g)}
    linear = {n: v for n, v in previous_result.params.items() if n in set(composite.linear_names())}
    linear[f"{parent}.h"], linear[f"{new}.h"] = a_lo, a_hi
    return _seed_dict(composite, modes, linear, _width_starts(composite, ctx))

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


# The central quasi-elastic component (CentralQE) is switched on by instrument, not by a card
# setting: phenomenological, no mechanism claimed; every BL43LXU model carries one.
CENTRAL_QE_INSTRUMENT = "BL43LXU"


def central_qe_admissible(ctx) -> bool:
    """The central QE belongs to every spectrum of the :data:`CENTRAL_QE_INSTRUMENT`."""
    return getattr(ctx, "instrument", None) == CENTRAL_QE_INSTRUMENT


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


MODE_CEILING_ENV = "ISAR_MODE_CEILING"
DEFAULT_MODE_CEILING = 6


def default_mode_ceiling() -> int:
    """The one mode-ceiling rule when the card names none: ``$ISAR_MODE_CEILING``, else 6. Read at
    call time, not at import: the suite caps it (repo-root ``conftest.py``) and production leaves
    it unset. A value that is not a whole number >= 1 warns and is ignored (it never silently
    wins). The card's ``[fitting] max_modes`` beats it
    (:func:`isar.session.guidelines.mode_ceiling`)."""
    raw = os.environ.get(MODE_CEILING_ENV)
    if raw is None or raw == "":
        return DEFAULT_MODE_CEILING
    try:
        n = int(raw)
        if float(raw) == n and n >= 1:
            return n
    except (TypeError, ValueError):
        pass
    warnings.warn(f"{MODE_CEILING_ENV}: {raw!r} is not a whole number >= 1 — ignoring")
    return DEFAULT_MODE_CEILING


def enumerate_phonon_multimode(ctx, n_max: int | None = None) -> list[Hypothesis]:
    """Every structure hypothesis holding 2..``n_max`` modes for this scan context (``None``: the
    :func:`default_mode_ceiling`), count by count, under the same elastic/quasi-elastic gates as
    :func:`enumerate_phonon_structures`. Mode ``i`` takes the ``phonon<i>`` namespace (mode 1 keeps
    ``phonon``), so each structure is *strictly nested* in the next count's.

    Catalogue-driven (:func:`isar.analyze.catalogue.enumerate_count_structures`); the two-mode
    output is pinned to the legacy hand enumeration by ``tests/analyze/test_catalogue.py``."""
    # local import: catalogue imports components, which imports this module, so it cannot come
    # in at module level (components.py -> phonon.py cycle).
    from isar.analyze.catalogue import enumerate_multimode_structures

    return enumerate_multimode_structures(ctx, default_mode_ceiling() if n_max is None else n_max)
