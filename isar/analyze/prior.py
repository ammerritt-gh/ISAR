"""The dispersion-prior expectation injected into step-4 verdict/confidence.

`PhononPrior` is the seam the future *running* dispersion prior (built from already-fitted
neighbours, §11) will fill. Step 4 only ever consumes it — it is an input, never built here.
`ScanContext.prior_omega` is the scalar shortcut (= `PhononPrior.omega0`); the richer object
adds the expected peak strength the detection-power test needs and the spread the confidence
distance term uses.

The second half of the module is the **mode prior recipe** (v5): the one place a mode's parameter
bounds, and so its priors, come from. Amplitudes are *empirical Bayes*: the peak and elastic
heights are flat on ``[0, margin x the spectrum's highest count]`` and the background on
``[0, highest count]`` (operator ruling 2026-10-25, replacing v4's rule that no prior reads the
counts; for amplitudes only). Widths take the softened log-uniform density ``1/(Gamma + knee)``
and energies are ordered-uniform across the fit window, with their k! normalisation. Every fit
records the data-derived quantity (``SpectrumPrior.provenance``). Starting values may still come
from the data. The v4 recipe (declared exposure x a scale) survives as the ``legacy_v4``
sensitivity arm and for the quasi-elastic area. Derivation of the constants:
``docs/STATS_PRIMER.md``, "The mode prior recipe".
"""
from __future__ import annotations

import dataclasses
import math
from dataclasses import asdict, dataclass

import numpy as np

from isar.analyze.phonon import EPS_OMEGA0, GAMMA_FLOOR, GAMMA_MAX, GAMMA_MAX_V4


@dataclass(frozen=True)
class PhononPrior:
    """Expected phonon at a scan's Q.

    omega0: expected peak position (meV).
    amplitude: expected peak strength (counts above background) for the detection-power test;
        ``None`` = unknown, so detection power abstains (no underpowered verdict can be raised).
    omega0_sigma: prior spread on omega0 (meV) for the confidence distance term.
    """

    omega0: float
    amplitude: float | None = None
    omega0_sigma: float = 1.0


# ----------------------------------------------------------------------------
# The mode prior recipe
# ----------------------------------------------------------------------------
# Bump the version whenever a constant or a rule below changes: it is hashed into the guidelines
# signature (so the affected spectra refit) and recorded with every fit.
# /5: heights flat on [0, 1.2 x max count] (empirical Bayes), softened log-uniform widths on
# 0..20 meV, ordered energies with k!, posterior-mode Laplace with the box rule.
PRIOR_RECIPE_VERSION = "isar.mode-prior-recipe/5"

# --- The v4 reference arm (legacy_v4) and the quasi-elastic area rule ---------------------------
# Amplitude ceiling: a fitted height h (counts per point at the reference exposure) is bounded by
# scale x exposure, i.e. a resolution-limited peak may reach `scale x exposure` counts per point;
# a quasi-elastic area by scale x exposure x width. The scale depends on what the exposure counts: monitor counts (one count per
# monitor count) or seconds (a count rate per second). Agent-proposed from order-of-magnitude
# instrument figures, no measured value: docs/STATS_PRIMER.md. No card override; the S6 sensitivity
# check is how the values reach the operator.
AMPLITUDE_SCALE_PER_MONITOR = 1.0
# Counts per second: the top of the 1e4-1e5 counts/s an analyzer detector saturates at (the primer's
# figure), so a seconds-normalised ceiling never clips a real line.
AMPLITUDE_SCALE_PER_SECOND = 1.0e5

# Exposure assumed for a spectrum that declares none (the legacy uniform-exposure path): a declared
# nominal figure, never a function of the counts.
NOMINAL_EXPOSURE = 1.0e6

# A mode width may fall to this fraction of the resolution width before the absolute GAMMA_FLOOR
# (kept: the H8 boundary-pin tolerance reads it) is reached; it only bites for a resolution sharper
# than GAMMA_FLOOR / WIDTH_FLOOR_FRACTION = 0.1 meV.
WIDTH_FLOOR_FRACTION = 1.0e-2


def recipe_exposure(ctx) -> float:
    """The spectrum's declared exposure (``ctx.exposure_reference``: the median kept monitor, or
    counting time where there is no monitor), else :data:`NOMINAL_EXPOSURE`. Never the counts."""
    ref = getattr(ctx, "exposure_reference", None)
    if ref is not None and np.isfinite(float(ref)) and float(ref) > 0.0:
        return float(ref)
    return NOMINAL_EXPOSURE


def resolution_fwhm(ctx) -> float:
    """Total resolution FWHM (meV): the Voigt of ``gauss_fwhm`` and ``lorentz_fwhm`` by the
    Olivero-Longbothum approximation (exact in either limit); a tabulated curve has put its own
    FWHM in ``gauss_fwhm`` with ``lorentz_fwhm`` 0. A spectrum with no usable resolution is held
    upstream, so this refuses rather than defaults."""
    g = float(getattr(ctx, "gauss_fwhm", 0.0) or 0.0)
    lor = float(getattr(ctx, "lorentz_fwhm", 0.0) or 0.0)
    w = 0.5346 * lor + math.sqrt(0.2166 * lor * lor + g * g)
    if not (np.isfinite(w) and w > 0.0):
        raise ValueError(f"mode prior recipe needs a positive resolution width, got "
                         f"gauss_fwhm={g!r} lorentz_fwhm={lor!r}")
    return w


def amplitude_scale(ctx) -> float:
    """The amplitude ceiling's scale for this spectrum, by the source its exposure was counted in.
    A spectrum with no declared exposure keeps the monitor scale on the nominal exposure."""
    if getattr(ctx, "exposure_reference", None) is None:
        return AMPLITUDE_SCALE_PER_MONITOR
    source = getattr(ctx, "exposure_source", None)
    if source == "monitor":
        return AMPLITUDE_SCALE_PER_MONITOR
    if source == "time":
        return AMPLITUDE_SCALE_PER_SECOND
    raise ValueError(f"exposure reference {ctx.exposure_reference!r} has no declared source "
                     f"('monitor' or 'time'), got {source!r}: the amplitude scale cannot be chosen")


# --- Recipe v5: settings, the resolved spectrum prior, the Laplace evidence ----------------------
@dataclass(frozen=True)
class PriorSettings:
    """What the sensitivity check (design 2.4) varies. Defaults are the operator's 2026-10-25 recipe.

    amplitude_margin: peak and elastic heights flat on [0, margin x max count]  (1.2; check 1.05, 3).
    width_kind: "softlog" -> density 1/(Gamma + width_knee) on [0, width_cap]; "flat" -> uniform.
    width_knee: meV (0.12).  width_cap: meV (20; check 40).
    energy_cap: meV, E0 uniform-ordered on (energy_floor, min(energy_cap, max fitted E)].
    legacy_v4: True -> the v4 recipe in the height coordinate (S1U2's /4h), the sensitivity
        check's reference arm.
    """
    amplitude_margin: float = 1.2
    width_kind: str = "softlog"
    width_knee: float = 0.12
    width_cap: float = GAMMA_MAX
    energy_floor: float = EPS_OMEGA0
    energy_cap: float = 20.0
    legacy_v4: bool = False

    def __post_init__(self):
        if self.width_kind not in ("softlog", "flat"):
            raise ValueError(f"width_kind must be 'softlog' or 'flat', got {self.width_kind!r}")
        if not (self.amplitude_margin > 0 and self.width_cap > 0 and self.width_knee > 0
                and self.energy_cap > self.energy_floor >= 0):
            raise ValueError(f"invalid prior settings {self!r}")


SENSITIVITY_ARMS = {
    "recipe": PriorSettings(),
    "margin_1.05": PriorSettings(amplitude_margin=1.05),
    "margin_3": PriorSettings(amplitude_margin=3.0),
    "width_flat": PriorSettings(width_kind="flat"),
    "width_cap_40": PriorSettings(width_cap=40.0),   # moves the prior and the TRF bound, not
                                                     # phonon._gamma_at_bound (a diagnostic flag)
    "today_v4": PriorSettings(legacy_v4=True),
}


@dataclass(frozen=True)
class SpectrumPrior:
    """The resolved prior for one spectrum (the empirical-Bayes part is `max_count`).

    Every log density below is a normalised density in the stated coordinate; -inf outside support.
    """
    settings: PriorSettings
    max_count: float          # highest count at the reference exposure among kept points; >= 1
    e_lo: float               # meV, energy support lower end
    e_hi: float               # meV, energy support upper end
    v4_height_hi: float | None = None   # legacy_v4 arm only: amplitude_scale(ctx) x recipe_exposure(ctx)

    # ---- bounds (used as TRF bounds; the support of each density) ----
    @property
    def height_hi(self) -> float:
        if self.settings.legacy_v4:
            return self._v4_ceiling()
        return self.settings.amplitude_margin * self.max_count

    @property
    def background_hi(self) -> float:
        return self._v4_ceiling() if self.settings.legacy_v4 else self.max_count

    def _v4_ceiling(self) -> float:
        if self.v4_height_hi is None:
            raise ValueError("the legacy_v4 prior needs v4_height_hi: resolve it with resolve_prior_for")
        return self.v4_height_hi

    def _width_support(self) -> tuple[float, float]:
        if self.settings.legacy_v4:
            return (GAMMA_FLOOR, GAMMA_MAX_V4)       # v4: flat between the fit's own bounds
        return (0.0, self.settings.width_cap)

    def bounds(self, kind: str) -> tuple[float, float]:
        """kind in {"h", "omega0", "Gamma", "elastic", "bg"}."""
        if kind in ("h", "elastic"):
            return (0.0, self.height_hi)
        if kind == "bg":
            return (0.0, self.background_hi)
        if kind == "omega0":
            return (self.e_lo, self.e_hi)
        if kind == "Gamma":
            return self._width_support()
        raise ValueError(f"no prior for parameter kind {kind!r}")

    # ---- log densities ----
    def ln_height(self, h: float) -> float:
        return -math.log(self.height_hi) if 0.0 <= h <= self.height_hi else -math.inf

    def ln_background(self, b: float) -> float:
        return -math.log(self.background_hi) if 0.0 <= b <= self.background_hi else -math.inf

    def ln_width(self, g: float) -> float:
        s = self.settings
        lo, hi = self._width_support()
        if not lo <= g <= hi:
            return -math.inf
        if s.width_kind == "flat" or s.legacy_v4:
            return -math.log(hi - lo)
        return -math.log(g + s.width_knee) - math.log(math.log((hi + s.width_knee)
                                                              / s.width_knee))

    def ln_energies(self, energies) -> float:
        """ln(k!/W^k) on the ordered region; a function of the set, so label order is irrelevant.
        One implementation: :func:`ordered_energy_log_prior`."""
        return ordered_energy_log_prior(energies, self.e_lo, self.e_hi)

    def ln_prior(self, peaks, elastic: float | None, bg: float | None) -> float:
        """peaks: iterable of (omega0, Gamma, h); Gamma None is a delta mode and carries no width
        factor (its width is pinned, not fitted). elastic/bg None = that variant has no such term."""
        peaks = list(peaks)
        lp = self.ln_energies([p[0] for p in peaks])
        for _, g, h in peaks:
            if g is not None:
                lp += self.ln_width(g)
            lp += self.ln_height(h)
        if elastic is not None:
            lp += self.ln_height(elastic)
        if bg is not None:
            lp += self.ln_background(bg)
        return lp

    def provenance(self) -> dict:
        """Recorded with every fit: the recipe, its settings and the data-derived quantity."""
        empirical = not self.settings.legacy_v4      # the v4 arm's amplitudes come from exposure
        out = {"version": PRIOR_RECIPE_VERSION, "empirical_bayes": empirical,
               "empirical_quantity": "max_count" if empirical else None,
               "max_count": float(self.max_count),
               "height_hi": float(self.height_hi), "background_hi": float(self.background_hi),
               "energy_support_meV": [float(self.e_lo), float(self.e_hi)],
               "settings": asdict(self.settings)}
        return out


def resolve_prior(counts, energies_used, settings: PriorSettings | None = None, *,
                  exposure_scale=None) -> SpectrumPrior:
    """The one place the empirical-Bayes quantity is read: the highest count at the reference
    exposure, max(y_i / exposure_scale_i) over kept points (raw max when exposure_scale is None).
    Missing/NaN counts (and points with non-positive exposure) are ignored; a spectrum whose highest
    count is 0 gets max_count 1 (so the support is never empty; recorded)."""
    s = settings or PriorSettings()
    c = np.asarray(counts, float)
    if exposure_scale is not None:
        sc = np.asarray(exposure_scale, float)
        if sc.shape != c.shape:
            raise ValueError("resolve_prior: exposure_scale must match counts")
        with np.errstate(divide="ignore", invalid="ignore"):
            c = np.where(sc > 0, c / sc, np.nan)
    c = c[np.isfinite(c)]
    if c.size == 0:
        raise ValueError("resolve_prior: no finite counts")
    mx = max(1.0, float(c.max()))
    e_max = float(np.max(energies_used))
    e_hi = e_max if s.legacy_v4 else min(s.energy_cap, e_max)   # v4 never capped the window
    if not e_hi > s.energy_floor:
        raise ValueError(f"resolve_prior: no positive energy range (max E {e_hi!r})")
    return SpectrumPrior(settings=s, max_count=mx, e_lo=s.energy_floor, e_hi=e_hi)


def resolve_prior_for(ctx, settings: PriorSettings | None = None) -> SpectrumPrior:
    """The one ctx-level entry (``mode_bounds``, ``recipe_provenance`` and the fast pass call it):
    the kept points' counts, energies and exposure; under ``legacy_v4`` also the v4 ceiling."""
    s = settings or PriorSettings()
    sp = resolve_prior(ctx.counts_used, ctx.x_used, s, exposure_scale=ctx.exposure_used)
    if s.legacy_v4:
        sp = dataclasses.replace(sp, v4_height_hi=amplitude_scale(ctx) * recipe_exposure(ctx))
    return sp


def mode_bounds(names, ctx, settings: PriorSettings | None = None) -> list[tuple[float, float]]:
    """(lo, hi) for each global parameter name, in order: the recipe's bounds, and so the support
    of its priors. All of them come from :func:`resolve_prior_for`, so the TRF box and the Laplace
    box can never disagree.

    * ``.omega0`` (the undamped energy E0): ``(energy_floor, min(20, max fitted energy))``.
    * ``.Gamma`` (width): from ``WIDTH_FLOOR_FRACTION`` of the resolution width (never above the
      absolute ``GAMMA_FLOOR``) to the width cap (``GAMMA_MAX`` = 20 meV).
    * ``.h`` (phonon and elastic heights, counts per point): ``(0, margin x max count)``.
    * ``.BG`` (flat background, counts per point): ``(0, max count)``.
    * ``.QE`` (area): v4's rule, ``(0, amplitude_scale x exposure x resolution width)``; the
      quasi-elastic line is out of the recipe's scope and its coefficient is an area, not a height.

    ``PriorSettings(legacy_v4=True)`` gives the v4 bounds (amplitude ``scale x exposure``).
    """
    sp = resolve_prior_for(ctx, settings)
    width = resolution_fwhm(ctx)
    out = []
    for name in names:
        if name.endswith(".omega0"):
            out.append(sp.bounds("omega0"))
        elif name.endswith(".Gamma"):
            out.append((min(GAMMA_FLOOR, WIDTH_FLOOR_FRACTION * width), sp.bounds("Gamma")[1]))
        elif name.endswith(".h"):
            out.append(sp.bounds("h"))
        elif name.endswith(".QE"):
            out.append((0.0, amplitude_scale(ctx) * recipe_exposure(ctx) * width))
        elif name.endswith(".BG"):
            out.append(sp.bounds("bg"))
        else:
            raise ValueError(f"mode prior recipe has no bound for parameter {name!r}")
    return out


def recipe_provenance(ctx, settings: PriorSettings | None = None) -> dict:
    """What a fit records about the recipe it was bounded by: the version, the settings and the
    data-derived quantity (``max_count``) it resolved to for this spectrum (JSON-able)."""
    return resolve_prior_for(ctx, settings).provenance()


# ----------------------------------------------------------------------------
# Ordered mode energies
# ----------------------------------------------------------------------------
# N modes of one structure are exchangeable: swapping two labels gives the same curve, so the
# likelihood has N! identical copies of every solution. A flat prior per label makes the two evidence
# lanes disagree by ln N!: nested sampling finds all N! copies, a Laplace expansion at one fitted
# point sees one. The recipe orders the energy coordinate (`.omega0`, the sampled one, so the
# normalisation below is exact) and puts one joint density on the ordered region: N!/W^N. The Laplace
# evidence (ordered_energy_log_prior) and the nested-sampling prior transform
# (ordered_energy_transform) are the same prior, so each lane gets the N! exactly once and agrees.
def ordered_energy_log_prior(energies, lo: float, hi: float) -> float:
    """``ln(N!/W^N)`` for N mode energies inside the window ``[lo, hi]`` (W = hi - lo), else -inf.

    A function of the *set* of energies, so every label permutation gives the same value: it is the
    density on the ordered region ``e_1 < ... < e_N``, which integrates to 1 there."""
    e = np.asarray(energies, dtype=float).ravel()
    if not hi > lo:
        raise ValueError(f"energy window needs lo < hi, got ({lo!r}, {hi!r})")
    if e.size and (e.min() < lo or e.max() > hi):
        return -math.inf
    return math.lgamma(e.size + 1) - e.size * math.log(hi - lo)


def ordered_energy_transform(u, lo: float, hi: float) -> np.ndarray:
    """Nested-sampling prior transform for the ordered energies: unit-cube ``u`` (length N) to the
    N order statistics of uniforms on ``[lo, hi]``, ascending. Sampling this is sampling
    :func:`ordered_energy_log_prior`."""
    u = np.asarray(u, dtype=float).ravel()
    n = u.size
    t = np.empty(n)
    if n:
        t[-1] = u[-1] ** (1.0 / n)                  # the largest of n uniforms
        for i in range(n - 2, -1, -1):              # then the largest of the i + 1 left below it
            t[i] = t[i + 1] * u[i] ** (1.0 / (i + 1))
    return lo + (hi - lo) * t


# ----------------------------------------------------------------------------
# Posterior-mode Laplace evidence with the box rule
# ----------------------------------------------------------------------------
# ln Z = ln L + ln prior + d/2 ln 2pi - 1/2 ln det H, H the exact Hessian of -(ln L + ln prior) in
# the fit coordinates (central differences of the scalar objective, never J^T J). The prior is
# supported on a box, and the Gaussian must not be priced as if it did not spread beyond it: the
# integral of the quadratic over the box is the Gaussian's normaliser times the mass P_box inside.
# A coordinate railed at a bound adds the linear term of the one-sided gradient there (the quadratic's
# centre moves outside the box); a coordinate with a huge sd is cut by the box without being railed.
@dataclass
class LaplaceResult:
    ln_z: float               # nan unless status is "ok" or "ok_boundary"
    status: str               # "ok" | "ok_boundary" | "no_evidence"
    reason: str               # "" or why there is no evidence
    ln_det_h: float
    cov: np.ndarray | None    # H^-1 in theta's order (free coordinates only), None if not PD
    boundary_method: str      # "" | "box"
    ln_box_mass: float        # ln of the Gaussian's mass inside the support box (0.0 when none)


def hessian_fd(f, x, steps):
    """Central-difference Hessian of scalar f at x; steps per coordinate (absolute)."""
    x = np.asarray(x, float)
    n = x.size
    H = np.empty((n, n))
    f0 = f(x)
    for i in range(n):
        ei = np.zeros(n); ei[i] = steps[i]
        H[i, i] = (f(x + ei) - 2 * f0 + f(x - ei)) / steps[i] ** 2
        for j in range(i + 1, n):
            ej = np.zeros(n); ej[j] = steps[j]
            H[i, j] = H[j, i] = (f(x + ei + ej) - f(x + ei - ej) - f(x - ei + ej)
                                 + f(x - ei - ej)) / (4 * steps[i] * steps[j])
    return H


def _ln_interval_mass(a, b):
    """ln(Phi(b) - Phi(a)) for standardised limits, either of which may be +-inf."""
    from scipy.special import log_ndtr
    if np.isneginf(a):
        return float(log_ndtr(b))
    if np.isposinf(b):
        return float(log_ndtr(-a))
    lb = float(log_ndtr(b))
    return lb + math.log1p(-math.exp(float(log_ndtr(a)) - lb))


def _ln_box_mass(mu, cov, lo, hi, sd):
    """ln of the mass of N(mu, cov) inside [lo, hi], over the coordinates whose bound lies within
    6 sd of mu (the rest contribute 1)."""
    # shortcut: 6-sd cut leaves under 1e-9 of mass, far below Genz's own ~1e-4 relative error,
    # which is accepted; upgrade only if a model's evidence ever contends at that precision.
    a = (lo - mu) / sd
    b = (hi - mu) / sd
    a = np.where(a > -6.0, a, -np.inf)
    b = np.where(b < 6.0, b, np.inf)
    near = np.flatnonzero(np.isfinite(a) | np.isfinite(b))
    if near.size == 0:
        return 0.0
    if near.size == 1:
        return _ln_interval_mass(a[near[0]], b[near[0]])
    from scipy.stats import multivariate_normal
    # Standardised (correlation matrix, limits a and b): the mass is scale free and scipy's
    # singularity test is not, so counts-scale heights beside meV widths read as singular.
    s = sd[near]
    mvn = multivariate_normal(mean=np.zeros(near.size), cov=cov[np.ix_(near, near)] / np.outer(s, s),
                              seed=0, abseps=1e-12, releps=1e-6, maxpts=20000 * near.size)
    mass = float(mvn.cdf(b[near], lower_limit=a[near]))
    if np.isfinite(mass) and mass >= 1e-10:
        return math.log(mass)
    # Genz loses relative accuracy below about 1e-10; upgrade to a tilted estimator if such a
    # model ever contends. The product of the 1-D masses is exact for uncorrelated coordinates.
    return float(sum(_ln_interval_mass(a[i], b[i]) for i in near))


def laplace_log_evidence(neg_log_post, theta, lo, hi, *, rel_step=1e-4) -> LaplaceResult:
    """Posterior-mode Laplace evidence on the support box ``[lo, hi]`` (the box rule).

    neg_log_post(theta) = -(ln L + ln prior), finite inside the support. ``theta`` is the posterior
    mode found by a bounded optimiser, free coordinates only. A coordinate within 1e-6 x span of a
    bound is railed: its Hessian stencil is nudged two steps inward and the one-sided inward
    gradient g enters. With C = H^-1 and mu = theta - C g the quadratic's centre,
    ``ln Z = -f(theta) + g'Cg/2 + (d/2) ln 2pi - (1/2) ln det H + ln P_box``, P_box the mass of
    N(mu, C) inside the box. One railed coordinate reproduces the single-boundary correction
    ``(g s)^2/2 + ln Phi(-g s)``. A Hessian that is not positive definite is ``no_evidence``."""
    theta = np.asarray(theta, float).copy()
    lo = np.asarray(lo, float)
    hi = np.asarray(hi, float)
    d = theta.size
    span = np.maximum(hi - lo, 1e-9)
    steps = rel_step * np.maximum(np.abs(theta), 1e-2 * span)
    tol = 1e-6 * span
    at_lo = theta - lo <= tol
    at_hi = hi - theta <= tol
    x = theta.copy()
    x[at_lo] = lo[at_lo] + 2.0 * steps[at_lo]          # stencil inside the support
    x[at_hi] = hi[at_hi] - 2.0 * steps[at_hi]
    f0 = float(neg_log_post(theta))
    H = hessian_fd(neg_log_post, x, steps)
    if not np.all(np.isfinite(H)):
        return LaplaceResult(math.nan, "no_evidence", "Hessian not finite", math.nan, None, "", 0.0)
    try:
        L = np.linalg.cholesky(H)
    except np.linalg.LinAlgError:
        return LaplaceResult(math.nan, "no_evidence", "Hessian not positive definite",
                             math.nan, None, "", 0.0)
    ln_det = 2.0 * float(np.sum(np.log(np.diag(L))))
    cov = np.linalg.inv(H)
    g = np.zeros(d)
    for i in np.flatnonzero(at_lo):
        e = np.zeros(d); e[i] = steps[i]
        g[i] = max((float(neg_log_post(theta + e)) - f0) / steps[i], 0.0)
    for i in np.flatnonzero(at_hi):
        e = np.zeros(d); e[i] = steps[i]
        g[i] = -max((float(neg_log_post(theta - e)) - f0) / steps[i], 0.0)
    mu = theta - cov @ g
    try:
        ln_box = _ln_box_mass(mu, cov, lo, hi, np.sqrt(np.diag(cov)))
    except np.linalg.LinAlgError:
        return LaplaceResult(math.nan, "no_evidence", "box covariance numerically singular",
                             math.nan, None, "", 0.0)
    ln_z = (-f0 + 0.5 * float(g @ cov @ g) + 0.5 * d * math.log(2 * math.pi)
            - 0.5 * ln_det + ln_box)
    boxed = abs(ln_box) > 1e-6
    return LaplaceResult(ln_z, "ok_boundary" if boxed else "ok", "", ln_det, cov,
                         "box" if boxed else "", ln_box)
