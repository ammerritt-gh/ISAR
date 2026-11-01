"""The dispersion-prior expectation injected into step-4 verdict/confidence.

`PhononPrior` is the seam the future *running* dispersion prior (built from already-fitted
neighbours, §11) will fill. Step 4 only ever consumes it — it is an input, never built here.
`ScanContext.prior_omega` is the scalar shortcut (= `PhononPrior.omega0`); the richer object
adds the expected peak strength the detection-power test needs and the spread the confidence
distance term uses.

The second half of the module is the **mode prior recipe** (/6): the one place a mode's parameter
bounds, and so its priors, come from. Amplitudes are *empirical Bayes*: the peak and elastic
heights are flat on ``[0, margin x the spectrum's highest count]`` and the background on
``[0, highest count]`` (operator ruling 2026-10-25, replacing v4's rule that no prior reads the
counts; for amplitudes only). Widths take the softened log-uniform density ``1/(Gamma + knee)``.
Energies are ordered across the fit window, with their k! normalisation, each under one density
``g(E)``: flat, times a near-zero penalty (:class:`EnergyPrior`; 0 at and above 1 resolution
FWHM, rising 10 nats per FWHM to a 7.5-nat ceiling at 1/4 FWHM, operator 2026-10-29), so a mode
must earn its place against the elastic line. Every fit
records the data-derived quantity (``SpectrumPrior.provenance``). Starting values may still come
from the data. The v4 recipe (declared exposure x a scale) survives as the ``legacy_v4``
sensitivity arm and for the quasi-elastic area. Derivation of the constants:
``docs/STATS_PRIMER.md``, "The mode prior recipe".
"""
from __future__ import annotations

import bisect
import dataclasses
import math
from dataclasses import asdict, dataclass

import numpy as np

from scipy import special

from isar.analyze.phonon import (EPS_OMEGA0, GAMMA_FLOOR, GAMMA_MAX, GAMMA_MAX_V4,
                                 central_qe_admissible)


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
# /6: each ordered energy's flat density times the near-zero penalty (EnergyPrior), renormalised
# over the window; the ramp's numbers are PriorSettings.energy_ramp_*.
# /7: each mode's width is a proper density on [g_lo, c(omega0)], c = min(WIDTH_CAP_LONE_MEV,
# omega0): a lone spectrum fits no mode wider than 4 meV, nor wider than its own energy. The old
# 20 meV ceiling survives as the lifted one (width_cap), for a mode a neighbour-carried branch
# vouches for. Every energy is bounded below by 2 g_lo so the width range is never empty.
PRIOR_RECIPE_VERSION = "isar.mode-prior-recipe/7"

# The widest a mode of a lone spectrum may be (meV): a fixed number of the recipe, not a setting.
WIDTH_CAP_LONE_MEV = 4.0

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


# --- Recipe /6: settings, the resolved spectrum prior, the Laplace evidence ----------------------
def _check_ramp(start, end, ceiling, corner, unit: str) -> None:
    """The near-zero ramp's validity, shared by PriorSettings (FWHM units) and EnergyPrior (meV)."""
    if not all(math.isfinite(float(v)) for v in (start, end, ceiling, corner)) or ceiling < 0:
        raise ValueError(f"energy ramp needs finite numbers and ceiling >= 0, got start={start!r} "
                         f"end={end!r} ceiling={ceiling!r} corner={corner!r} ({unit})")
    if ceiling > 0 and not (corner > 0 and end - corner > 0 and start - end >= 2 * corner):
        raise ValueError(f"energy ramp needs corner > 0, end - corner > 0 and start - end >= "
                         f"2 corner, got start={start!r} end={end!r} corner={corner!r} ({unit})")


@dataclass(frozen=True)
class PriorSettings:
    """What the sensitivity check (design 2.4) varies. Defaults are the operator's 2026-10-25 recipe.

    amplitude_margin: peak and elastic heights flat on [0, margin x max count]  (1.2; check 1.05, 3).
    width_kind: "softlog" -> density 1/(Gamma + width_knee) on [g_lo, ceiling]; "flat" -> uniform.
    width_knee: meV (0.12).  width_cap: meV (20; check 40): the LIFTED ceiling, the widest a mode
        a neighbour-carried branch vouches for may be; a lone spectrum's mode stops at
        min(WIDTH_CAP_LONE_MEV, width_cap, its own omega0) (:func:`width_ceiling`).
    energy_cap: meV, E0 ordered on (energy_floor, min(energy_cap, max fitted E)].
    legacy_v4: True -> the v4 recipe in the height coordinate (S1U2's /4h), the sensitivity
        check's reference arm.
    energy_ramp_*: the near-zero energy penalty (:class:`EnergyPrior`), in resolution FWHM: 0 at
        and above ``start``, rising linearly to ``ceiling`` nats at ``end`` and flat below; each
        corner rounded over +-``corner``. ``energy_ramp_ceiling = 0.0`` is no penalty. Operator
        ruling 2026-10-29 (start, end, ceiling); the corner is an algorithm setting. Not a card
        setting (``isar.session.guidelines.fast_pass_settings`` drops them).
    """
    amplitude_margin: float = 1.2
    width_kind: str = "softlog"
    width_knee: float = 0.12
    width_cap: float = GAMMA_MAX
    energy_floor: float = EPS_OMEGA0
    energy_cap: float = 20.0
    legacy_v4: bool = False
    energy_ramp_start_fwhm: float = 1.0      # penalty 0 at and above (ruling)
    energy_ramp_end_fwhm: float = 0.25       # ceiling reached here (ruling)
    energy_ramp_ceiling: float = 7.5         # nats (ruling); 0.0 = no penalty
    energy_ramp_corner_fwhm: float = 0.05    # half-width of each rounded corner (algorithm setting)

    def __post_init__(self):
        if self.width_kind not in ("softlog", "flat"):
            raise ValueError(f"width_kind must be 'softlog' or 'flat', got {self.width_kind!r}")
        if not (self.amplitude_margin > 0 and self.width_cap > 0 and self.width_knee > 0
                and self.energy_cap > self.energy_floor >= 0):
            raise ValueError(f"invalid prior settings {self!r}")
        _check_ramp(self.energy_ramp_start_fwhm, self.energy_ramp_end_fwhm,
                    self.energy_ramp_ceiling, self.energy_ramp_corner_fwhm, "FWHM")


SENSITIVITY_ARMS = {
    "recipe": PriorSettings(),
    "margin_1.05": PriorSettings(amplitude_margin=1.05),
    "margin_3": PriorSettings(amplitude_margin=3.0),
    "width_flat": PriorSettings(width_kind="flat"),
    "width_cap_40": PriorSettings(width_cap=40.0),   # under /7 moves only the lifted ceiling
    "today_v4": PriorSettings(legacy_v4=True),
}


# --- The width ceiling (recipe /7) -----------------------------------------------------------------
def width_ceiling(omega0: float, *, lifted: bool = False,
                  settings: "PriorSettings | None" = None) -> float:
    """The widest this mode may be (meV). ``lifted``: ``settings.width_cap``. Else exactly
    ``min(WIDTH_CAP_LONE_MEV, settings.width_cap, omega0)`` (no floor: the recipe keeps omega0 above
    ``2 g_lo``). ``legacy_v4``: ``GAMMA_MAX_V4`` whatever omega0 and ``lifted``."""
    s = settings or PriorSettings()
    if s.legacy_v4:
        return GAMMA_MAX_V4
    if lifted:
        return s.width_cap
    return min(WIDTH_CAP_LONE_MEV, s.width_cap, float(omega0))


def d_width_ceiling(omega0: float, *, lifted: bool = False,
                    settings: "PriorSettings | None" = None) -> float:
    """d :func:`width_ceiling` / d omega0: 1 where the lone ceiling equals omega0
    (``0 < omega0 < min(WIDTH_CAP_LONE_MEV, width_cap)``), else 0. At the kink the right derivative."""
    s = settings or PriorSettings()
    if s.legacy_v4 or lifted:
        return 0.0
    return 1.0 if 0.0 < omega0 < min(WIDTH_CAP_LONE_MEV, s.width_cap) else 0.0


def gamma_from_t(t: float, g_lo: float, ceiling: float) -> float:
    """The width at optimiser coordinate ``t`` in [0, 1] on ``[g_lo, ceiling]``."""
    if not ceiling > g_lo:
        raise ValueError(f"width range [{g_lo!r}, {ceiling!r}] is empty")
    return g_lo + (ceiling - g_lo) * t


def t_from_gamma(gamma: float, g_lo: float, ceiling: float) -> float:
    """The optimiser coordinate of a width (not clipped)."""
    if not ceiling > g_lo:
        raise ValueError(f"width range [{g_lo!r}, {ceiling!r}] is empty")
    return (gamma - g_lo) / (ceiling - g_lo)


def ln_width_given(gamma: float, ceiling: float, g_lo: float, settings: "PriorSettings") -> float:
    """The width's log density given its mode's ceiling: proper on ``[g_lo, ceiling]`` (the optimiser's
    own interval), -inf outside. Softlog ``1/(Gamma + knee)``, flat (or ``legacy_v4``) uniform."""
    if not g_lo <= gamma <= ceiling:
        return -math.inf
    if settings.width_kind == "flat" or settings.legacy_v4:
        return -math.log(ceiling - g_lo)
    k = settings.width_knee
    return -math.log(gamma + k) - math.log(math.log((ceiling + k) / (g_lo + k)))


def d_ln_width_given(gamma: float, ceiling: float, g_lo: float,
                     settings: "PriorSettings") -> tuple[float, float]:
    """``(d/d gamma, d/d ceiling)`` of :func:`ln_width_given` inside the support."""
    if settings.width_kind == "flat" or settings.legacy_v4:
        return 0.0, -1.0 / (ceiling - g_lo)
    k = settings.width_knee
    return (-1.0 / (gamma + k),
            -1.0 / ((ceiling + k) * math.log((ceiling + k) / (g_lo + k))))


class GammaMap:
    """The optimiser coordinate of the widths over a full parameter vector: each mapped mode's
    Gamma is fitted as ``t`` in [0, 1] on ``[g_lo, ceiling(omega0)]``; everything else is untouched.
    ``pairs``: ``(pos_omega0, pos_Gamma, g_lo, lifted)``. Pure numpy; stored and shown values stay
    Gamma in meV (``t`` lives only at the optimiser seam). The z-space log density is
    ``ln p(theta) + ln_jac(theta)``."""

    def __init__(self, pairs, settings: "PriorSettings | None" = None):
        self.pairs = tuple((int(a), int(g), float(lo), bool(lf)) for a, g, lo, lf in pairs)
        self.settings = settings or PriorSettings()

    def ceiling(self, theta, k: int) -> float:
        a, _g, _lo, lifted = self.pairs[k]
        return width_ceiling(float(theta[a]), lifted=lifted, settings=self.settings)

    def _d_ceiling(self, theta, k: int) -> float:
        a, _g, _lo, lifted = self.pairs[k]
        return d_width_ceiling(float(theta[a]), lifted=lifted, settings=self.settings)

    def box(self, bounds):
        """``bounds`` (a ``[(lo, hi)]`` list over the full vector) with each mapped Gamma's -> (0, 1)."""
        out = [tuple(b) for b in bounds]
        for _a, g, _lo, _lf in self.pairs:
            out[g] = (0.0, 1.0)
        return out

    def to_z(self, theta) -> np.ndarray:
        z = np.array(theta, dtype=float)
        for k, (_a, g, g_lo, _lf) in enumerate(self.pairs):
            z[g] = min(max(t_from_gamma(z[g], g_lo, self.ceiling(theta, k)), 0.0), 1.0)
        return z

    def to_theta(self, z) -> np.ndarray:
        """The full vector at ``z``; omega0 is read AFTER the assignment (it is its own coordinate)."""
        t = np.array(z, dtype=float)
        for k, (_a, g, g_lo, _lf) in enumerate(self.pairs):
            c = self.ceiling(t, k)
            # t = 1 is exactly the ceiling (a capped mode below 4 meV then has Gamma == omega0, which
            # the regime rule reads as overdamped); elsewhere clip a ulp past an edge
            t[g] = c if z[g] >= 1.0 else min(max(gamma_from_t(float(z[g]), g_lo, c), g_lo), c)
        return t

    def ln_jac(self, theta) -> float:
        return float(sum(math.log(self.ceiling(theta, k) - g_lo)
                         for k, (_a, _g, g_lo, _lf) in enumerate(self.pairs)))

    def d_ln_jac(self, theta) -> np.ndarray:
        """d ln_jac / d theta (the omega0 entries: ``c' / (c - g_lo)``)."""
        out = np.zeros(len(theta))
        for k, (a, _g, g_lo, _lf) in enumerate(self.pairs):
            out[a] += self._d_ceiling(theta, k) / (self.ceiling(theta, k) - g_lo)
        return out

    def jac(self, theta) -> np.ndarray:
        """``d theta / d z`` (full, at the theta whose Gamma are real widths)."""
        n = len(theta)
        J = np.eye(n)
        for k, (a, g, g_lo, _lf) in enumerate(self.pairs):
            c = self.ceiling(theta, k)
            s = c - g_lo
            J[g, g] = s
            J[g, a] += (float(theta[g]) - g_lo) / s * self._d_ceiling(theta, k)
        return J

    def cov_to_theta(self, theta, cov_z) -> np.ndarray:
        J = self.jac(theta)
        return J @ np.asarray(cov_z, dtype=float) @ J.T


@dataclass(frozen=True)
class SpectrumPrior:
    """The resolved prior for one spectrum (the empirical-Bayes part is `max_count`).

    Every log density below is a normalised density in the stated coordinate; -inf outside support.
    """
    settings: PriorSettings
    max_count: float          # highest count at the reference exposure among kept points; >= 1
    e_lo: float               # meV, energy support lower end
    e_hi: float               # meV, energy support upper end
    energy: EnergyPrior       # each mode energy's density on [e_lo, e_hi] (the near-zero penalty)
    fwhm: float               # meV, the resolution FWHM the penalty is measured in
    v4_height_hi: float | None = None   # legacy_v4 arm only: amplitude_scale(ctx) x recipe_exposure(ctx)
    qe_width: QEWidthPrior | None = None   # the central QE's width density (BL43LXU only)

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

    @property
    def g_lo(self) -> float:
        """The widest-allowed lower edge of every width (meV): the one definition ``mode_bounds`` reads."""
        return min(GAMMA_FLOOR, WIDTH_FLOOR_FRACTION * self.fwhm)

    def _width_support(self) -> tuple[float, float]:
        """The LIFTED width support (the density ``ln_width`` is normalised on)."""
        if self.settings.legacy_v4:
            return (GAMMA_FLOOR, GAMMA_MAX_V4)       # v4: flat between the fit's own bounds
        return (self.g_lo, self.settings.width_cap)

    def ceiling_at(self, omega0: float, *, lifted: bool = False) -> float:
        return width_ceiling(omega0, lifted=lifted, settings=self.settings)

    def ln_width_at(self, g: float, omega0: float, *, lifted: bool = False) -> float:
        """The width's log density given its mode's energy (proper on ``[g_lo, ceiling]``)."""
        return ln_width_given(g, self.ceiling_at(omega0, lifted=lifted), self.g_lo, self.settings)

    def d_ln_width_at(self, g: float, omega0: float, *, lifted: bool = False) -> tuple[float, float]:
        """``(d/d g, d/d omega0)`` of :meth:`ln_width_at` inside the support."""
        dg, dc = d_ln_width_given(g, self.ceiling_at(omega0, lifted=lifted), self.g_lo, self.settings)
        return dg, dc * d_width_ceiling(omega0, lifted=lifted, settings=self.settings)

    def bounds(self, kind: str) -> tuple[float, float]:
        """kind in {"h", "omega0", "Gamma", "elastic", "bg", "qe_width"}."""
        if kind in ("h", "elastic"):
            return (0.0, self.height_hi)
        if kind == "bg":
            return (0.0, self.background_hi)
        if kind == "omega0":
            return (self.e_lo, self.e_hi)
        if kind == "Gamma":      # the static outer box; each mode's own ceiling is width_ceiling
            lo, hi = self._width_support()
            return (lo, hi if self.settings.legacy_v4 else min(hi, WIDTH_CAP_LONE_MEV))
        if kind == "qe_width":
            if self.qe_width is None:
                raise ValueError("this spectrum has no central QE width prior (BL43LXU only)")
            return (self.qe_width.lo, self.qe_width.hi)
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
                                                              / (lo + s.width_knee)))

    def d_ln_width(self, g: float) -> float:
        """d ln_width / d Gamma inside the support (the heights' and background's densities are flat: no
        derivative)."""
        s = self.settings
        return 0.0 if s.width_kind == "flat" or s.legacy_v4 else -1.0 / (g + s.width_knee)

    def ln_qe_width(self, w: float) -> float:
        """The central QE's intrinsic-width log density (:class:`QEWidthPrior`)."""
        if self.qe_width is None:
            raise ValueError("this spectrum has no central QE width prior (BL43LXU only)")
        return self.qe_width.log_pdf(w)

    def d_ln_qe_width(self, w: float) -> float:
        """d ln_qe_width / d w inside the support."""
        if self.qe_width is None:
            raise ValueError("this spectrum has no central QE width prior (BL43LXU only)")
        return self.qe_width.d_log_pdf(w)

    def ln_energies(self, energies) -> float:
        """ln(k! prod g(E_i)) on the ordered region, g the penalised energy density
        (:attr:`energy`); a function of the set, so label order is irrelevant.
        One implementation: :func:`ordered_energy_log_prior`."""
        return ordered_energy_log_prior(energies, self.energy)

    def ln_prior(self, peaks, elastic: float | None, bg: float | None) -> float:
        """peaks: iterable of (omega0, Gamma, h); Gamma None is a delta mode and carries no width
        factor (its width is pinned, not fitted). elastic/bg None = that variant has no such term."""
        peaks = list(peaks)
        lp = self.ln_energies([p[0] for p in peaks])
        for e, g, h in peaks:
            if g is not None:
                lp += self.ln_width_at(g, e)
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
               "energy_penalty": self.energy.spec() | {"fwhm_meV": float(self.fwhm)},
               "settings": asdict(self.settings)}
        if self.qe_width is not None:
            out["qe_width_prior"] = self.qe_width.spec() | {"fwhm_meV": float(self.fwhm),
                                                            "recipe": CENTRAL_QE_RECIPE_VERSION}
        return out


def resolve_prior(counts, energies_used, settings: PriorSettings | None = None, *, fwhm: float,
                  exposure_scale=None) -> SpectrumPrior:
    """The one place the empirical-Bayes quantity is read: the highest count at the reference
    exposure, max(y_i / exposure_scale_i) over kept points (raw max when exposure_scale is None).
    Missing/NaN counts (and points with non-positive exposure) are ignored; a spectrum whose highest
    count is 0 gets max_count 1 (so the support is never empty; recorded). ``fwhm`` (meV, the total
    resolution FWHM) places the near-zero energy penalty."""
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
    # every admissible omega0 has a width ceiling of at least 2 g_lo, so [g_lo, ceiling] is never empty
    e_lo = s.energy_floor if s.legacy_v4 else max(
        s.energy_floor, 2.0 * min(GAMMA_FLOOR, WIDTH_FLOOR_FRACTION * float(fwhm)))
    if not e_hi > e_lo:
        raise ValueError(f"resolve_prior: no positive energy range (max E {e_hi!r})")
    return SpectrumPrior(settings=s, max_count=mx, e_lo=e_lo, e_hi=e_hi,
                         energy=energy_prior(e_lo, e_hi, fwhm, s), fwhm=float(fwhm))


def resolve_prior_for(ctx, settings: PriorSettings | None = None) -> SpectrumPrior:
    """The one ctx-level entry (``mode_bounds``, ``recipe_provenance`` and the fast pass call it):
    the kept points' counts, energies, exposure and resolution FWHM; under ``legacy_v4`` also the
    v4 ceiling."""
    s = settings or PriorSettings()
    sp = resolve_prior(ctx.counts_used, ctx.x_used, s, fwhm=resolution_fwhm(ctx),
                       exposure_scale=ctx.exposure_used)
    if s.legacy_v4:
        sp = dataclasses.replace(sp, v4_height_hi=amplitude_scale(ctx) * recipe_exposure(ctx))
    if central_qe_admissible(ctx):
        sp = dataclasses.replace(sp, qe_width=qe_width_prior(sp.fwhm))
    return sp


def mode_bounds(names, ctx, settings: PriorSettings | None = None) -> list[tuple[float, float]]:
    """(lo, hi) for each global parameter name, in order: the recipe's bounds, and so the support
    of its priors. All of them come from :func:`resolve_prior_for`, so the TRF box and the Laplace
    box can never disagree.

    * ``.omega0`` (the undamped energy E0): ``(max(energy_floor, 2 g_lo), min(20, max fitted energy))``.
    * ``.Gamma`` (width): from ``WIDTH_FLOOR_FRACTION`` of the resolution width (never above the
      absolute ``GAMMA_FLOOR``) to the lone cap (``WIDTH_CAP_LONE_MEV`` = 4 meV): the static outer
      box; each mode's own ceiling, ``min(4, omega0)``, is :func:`width_ceiling`.
    * ``.h`` (phonon and elastic heights, counts per point): ``(0, margin x max count)``.
    * ``.BG`` (flat background, counts per point): ``(0, max count)``.
    * ``.width`` (the central QE's intrinsic FWHM, BL43LXU): the :class:`QEWidthPrior` support.
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
            out.append((sp.g_lo, sp.bounds("Gamma")[1]))
        elif name.endswith(".h"):
            out.append(sp.bounds("h"))
        elif name.endswith(".width"):
            out.append(sp.bounds("qe_width"))
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
# likelihood has N! identical copies of every solution. A labelled prior makes the two evidence
# lanes disagree by ln N!: nested sampling finds all N! copies, a Laplace expansion at one fitted
# point sees one. The recipe orders the energy coordinate (`.omega0`, the sampled one, so the
# normalisation below is exact) and puts one joint density on the ordered region: N! prod g(E_i),
# g the one-energy density of EnergyPrior (flat times the near-zero penalty, recipe /6). The
# Laplace evidence (ordered_energy_log_prior) and the nested-sampling prior transform
# (ordered_energy_transform: the order statistics of uniforms mapped through g's inverse CDF) are
# the same prior, so each lane gets the N! exactly once and agrees.
_FLAT, _LINEAR, _CORNER = 0, 1, 2
# A corner's exponent varies by at most c x corner (0.5 nats): 16 nodes reach machine precision.
_GL_NODES = tuple(zip(*(a.tolist() for a in np.polynomial.legendre.leggauss(16))))


@dataclass(frozen=True)
class EnergyPrior:
    """One mode energy's recipe density on [lo, hi] (meV): flat, times exp(-penalty(E)),
    renormalised over the window.

    With ``c = ceiling / (start - end)`` (nats per meV) and the smooth hinge ``h(x, k, d)`` = 0 for
    ``x >= k + d``, ``k - x`` for ``x <= k - d`` and ``(k + d - x)**2 / (4 d)`` between, the penalty
    is ``c (h(E, start, corner) - h(E, end, corner))``: 0 above the ramp, ``c (start - E)`` on it,
    ``ceiling`` below it, C1 everywhere (the corners are rounded so the fast pass's finite-difference
    optimiser and Hessian never sit on a kink). ``ceiling == 0`` is the flat prior. Energies are
    absolute meV from zero energy transfer. On each piece between the breakpoints the penalty is a
    polynomial of degree <= 2, so the CDF is closed form on flat and linear pieces and a 16-node
    Gauss-Legendre rule on a corner; ``Z`` and the piece masses are computed once, at construction.
    """
    lo: float          # meV, window lower end (= SpectrumPrior.e_lo)
    hi: float          # meV, window upper end (= SpectrumPrior.e_hi)
    start: float       # meV, ramp start (energy_ramp_start_fwhm x FWHM)
    end: float         # meV, ramp end (energy_ramp_end_fwhm x FWHM)
    ceiling: float     # nats
    corner: float      # meV, corner half-width (energy_ramp_corner_fwhm x FWHM)

    def __post_init__(self):
        if not (math.isfinite(self.lo) and math.isfinite(self.hi) and self.lo < self.hi):
            raise ValueError(f"energy window needs finite lo < hi, got ({self.lo!r}, {self.hi!r})")
        _check_ramp(self.start, self.end, self.ceiling, self.corner, "meV")
        lo, hi, d = float(self.lo), float(self.hi), float(self.corner)
        c = self.ceiling / (self.start - self.end) if self.ceiling > 0 else 0.0
        if c > 0:
            edges = (-math.inf, self.end - d, self.end + d, self.start - d, self.start + d, math.inf)
            kinds = (_FLAT, _CORNER, _LINEAR, _CORNER, _FLAT)
        else:
            edges, kinds = (-math.inf, math.inf), (_FLAT,)
        pieces = [(max(left, lo), min(right, hi), kind)
                  for left, right, kind in zip(edges[:-1], edges[1:], kinds)
                  if max(left, lo) < min(right, hi)]
        # Scalar core in plain floats: the sampler maps a handful of energies per call, where
        # per-element Python beats numpy's per-call overhead.
        object.__setattr__(self, "_c", c)
        object.__setattr__(self, "_a", [p[0] for p in pieces])
        object.__setattr__(self, "_b", [p[1] for p in pieces])
        object.__setattr__(self, "_kind", [p[2] for p in pieces])
        # A flat piece's penalty is a constant (0 or the ceiling); a linear piece's density at its
        # left end anchors its closed-form CDF.
        object.__setattr__(self, "_left", [math.exp(-self.penalty(p[0])) for p in pieces])
        cum = [0.0]
        for i, p in enumerate(pieces):
            cum.append(cum[-1] + self._mass_to(i, p[1]))
        object.__setattr__(self, "_cum", cum[:-1])
        object.__setattr__(self, "_cum_end", cum[1:])
        object.__setattr__(self, "_z", cum[-1])
        object.__setattr__(self, "_ln_z", math.log(cum[-1]))

    @classmethod
    def flat(cls, lo: float, hi: float) -> "EnergyPrior":
        """The unpenalised density ``1/(hi - lo)`` (a declared flat energy prior, recipe /5)."""
        return cls(float(lo), float(hi), 0.0, 0.0, 0.0, 0.0)

    @property
    def ln_z(self) -> float:
        """ln of the normaliser, the integral of exp(-penalty) over [lo, hi] (meV)."""
        return self._ln_z

    def penalty(self, e: float) -> float:
        """The log-prior penalty at energy ``e`` (nats, >= 0), any real ``e``."""
        if self.ceiling <= 0:
            return 0.0
        return self._c * (_hinge(float(e), self.start, self.corner)
                          - _hinge(float(e), self.end, self.corner))

    def d_penalty(self, e: float) -> float:
        """d penalty / d e (nats per meV), any real ``e``."""
        if self.ceiling <= 0:
            return 0.0
        return self._c * (_d_hinge(float(e), self.start, self.corner)
                          - _d_hinge(float(e), self.end, self.corner))

    def log_pdf(self, e: float) -> float:
        """The normalised log density at ``e``; -inf outside ``[lo, hi]``. Scalar and cheap: it
        runs inside the fast pass's objective."""
        e = float(e)
        if not self.lo <= e <= self.hi:
            return -math.inf
        return -self.penalty(e) - self._ln_z

    def d_log_pdf(self, e: float) -> float:
        """d log_pdf / d e inside ``[lo, hi]``."""
        return -self.d_penalty(e)

    def _mass_to(self, i: int, x: float) -> float:
        """Unnormalised mass of piece ``i`` from its left end to ``x`` (inside the piece)."""
        a, kind = self._a[i], self._kind[i]
        if kind == _FLAT:
            return self._left[i] * (x - a)
        if kind == _LINEAR:                     # exp(-c (start - E)) is its own antiderivative / c
            return (math.exp(-self.penalty(x)) - self._left[i]) / self._c
        half = 0.5 * (x - a)
        mid = a + half
        return half * math.fsum(w * math.exp(-self.penalty(mid + half * t))
                                for t, w in _GL_NODES)

    def _cdf1(self, e: float) -> float:
        e = min(max(float(e), self.lo), self.hi)
        i = max(bisect.bisect_right(self._a, e) - 1, 0)
        return min(max((self._cum[i] + self._mass_to(i, e)) / self._z, 0.0), 1.0)

    def _ppf1(self, u: float) -> float:
        target = min(max(float(u), 0.0), 1.0) * self._z
        i = min(bisect.bisect_left(self._cum_end, target), len(self._a) - 1)
        r = max(target - self._cum[i], 0.0)
        a, b, kind = self._a[i], self._b[i], self._kind[i]
        if kind == _FLAT:
            x = a + r / self._left[i]
        elif kind == _LINEAR:                   # exp(-c (start - x)) = exp(-c (start - a)) + c r
            x = self.start + math.log(self._left[i] + self._c * r) / self._c
        else:
            x = self._corner_ppf(i, r)
        return min(max(x, a), b)

    def _corner_ppf(self, i: int, r: float) -> float:
        """Safeguarded Newton (bisection when a step leaves the bracket) on a corner's CDF."""
        a, b = self._a[i], self._b[i]
        lo_b, hi_b = a, b
        x = a + (b - a) * min(max(r / (self._cum_end[i] - self._cum[i]), 0.0), 1.0)
        for _ in range(60):
            f = self._mass_to(i, x) - r
            if f < 0:
                lo_b = x
            elif f > 0:
                hi_b = x
            else:
                return x
            step = x - f / math.exp(-self.penalty(x))
            if not lo_b < step < hi_b:
                step = 0.5 * (lo_b + hi_b)
            if abs(step - x) <= 1e-15 * max(1.0, abs(x)):
                return step
            x = step
        return x

    def cdf(self, e) -> np.ndarray:
        """The CDF at ``e`` (vectorised): 0 at and below ``lo``, 1 at and above ``hi``."""
        e = np.asarray(e, dtype=float)
        return np.array([self._cdf1(v) for v in e.ravel().tolist()]).reshape(e.shape)

    def ppf(self, u) -> np.ndarray:
        """The inverse CDF at ``u`` in [0, 1] (vectorised): closed form on flat and linear pieces,
        safeguarded Newton on a corner."""
        u = np.asarray(u, dtype=float)
        return np.array([self._ppf1(v) for v in u.ravel().tolist()]).reshape(u.shape)

    def spec(self) -> dict:
        """The six fields as floats (JSON-able)."""
        return {"lo": float(self.lo), "hi": float(self.hi), "start": float(self.start),
                "end": float(self.end), "ceiling": float(self.ceiling),
                "corner": float(self.corner)}


def _hinge(x: float, k: float, d: float) -> float:
    if x >= k + d:
        return 0.0
    if x <= k - d:
        return k - x
    return (k + d - x) ** 2 / (4.0 * d)


def _d_hinge(x: float, k: float, d: float) -> float:
    """d _hinge / d x."""
    if x >= k + d:
        return 0.0
    if x <= k - d:
        return -1.0
    return -(k + d - x) / (2.0 * d)


def energy_prior(lo: float, hi: float, fwhm: float, settings: PriorSettings) -> EnergyPrior:
    """The recipe's mode-energy density on ``[lo, hi]`` (meV) for a spectrum of total resolution
    FWHM ``fwhm`` (meV): the settings' FWHM-unit ramp placed in meV."""
    w = float(fwhm)
    if not (math.isfinite(w) and w > 0.0):
        raise ValueError(f"energy prior needs a positive finite resolution FWHM, got {fwhm!r}")
    s = settings
    return EnergyPrior(float(lo), float(hi), s.energy_ramp_start_fwhm * w,
                       s.energy_ramp_end_fwhm * w, float(s.energy_ramp_ceiling),
                       s.energy_ramp_corner_fwhm * w)


def ordered_energy_log_prior(energies, ep: EnergyPrior) -> float:
    """``ln(N! prod g(E_i))`` for N mode energies, g = ``ep``'s density; -inf when any lies outside
    ``[ep.lo, ep.hi]``; 0.0 for N = 0.

    A function of the *set* of energies, so every label permutation gives the same value: it is the
    density on the ordered region ``e_1 < ... < e_N``, which integrates to 1 there."""
    e = np.asarray(energies, dtype=float).ravel()
    total = math.lgamma(e.size + 1)
    for v in e.tolist():
        total += ep.log_pdf(v)
    return total


def ordered_energy_transform(u, ep: EnergyPrior) -> np.ndarray:
    """Nested-sampling prior transform for the ordered energies: unit-cube ``u`` (length N) to the
    N order statistics of ``ep``'s density, ascending: the order statistics ``t`` of N uniforms on
    [0, 1], mapped through ``ep.ppf`` (increasing, so the order holds). Sampling this is sampling
    :func:`ordered_energy_log_prior`."""
    u = np.asarray(u, dtype=float).ravel()
    n = u.size
    t = np.empty(n)
    if n:
        t[-1] = u[-1] ** (1.0 / n)                  # the largest of n uniforms
        for i in range(n - 2, -1, -1):              # then the largest of the i + 1 left below it
            t[i] = t[i + 1] * u[i] ** (1.0 / (i + 1))
    return ep.ppf(t)


# ----------------------------------------------------------------------------
# The central quasi-elastic width prior (BL43LXU only)
# ----------------------------------------------------------------------------
# The QE's intrinsic FWHM W has a proper density on [floor, knee + tail/slope], in units of the
# spectrum's resolution FWHM: flat from the floor to the knee, then exp(-slope (W - knee)) (1 nat per
# FWHM above 2 FWHM), the knee rounded over +-corner so the fast pass's finite-difference optimiser
# and Hessian never sit on a slope discontinuity. On each of the three pieces the CDF and its
# inverse are closed form (Gaussian piece: erf / erfinv), so the Laplace density (QEWidthPrior.log_pdf)
# and the nested-sampling transform (QEWidthPrior.ppf) are one implementation.
# Bump CENTRAL_QE_RECIPE_VERSION when the QE's model or the BL43LXU fast pass changes BL43LXU numbers.
CENTRAL_QE_RECIPE_VERSION = "isar.central-qe-recipe/2"


@dataclass(frozen=True)
class QEWidthSettings:
    """The QE width prior's numbers, in resolution FWHM (slope in nats per FWHM). Not part of
    :class:`PriorSettings` (whose ``asdict`` every signature hashes) and not a card setting.
    ``floor_fwhm``, ``knee_fwhm`` and ``slope`` are operator rulings (2026-10-29); ``corner_fwhm`` and
    ``tail_nats`` are algorithm settings: the corner is the half-width the near-zero ramp uses, and
    the support ends where the penalty reaches ``tail_nats`` (e^-40 of the mass is dropped)."""
    floor_fwhm: float = 1.0
    knee_fwhm: float = 2.0
    slope: float = 1.0
    corner_fwhm: float = 0.05
    tail_nats: float = 40.0

    def __post_init__(self):
        vals = (self.floor_fwhm, self.knee_fwhm, self.slope, self.corner_fwhm, self.tail_nats)
        if not all(math.isfinite(float(v)) for v in vals):
            raise ValueError(f"QE width settings must be finite, got {self!r}")
        if not (self.slope > 0 and self.tail_nats > 0 and self.corner_fwhm > 0
                and 0 <= self.floor_fwhm < self.knee_fwhm - self.corner_fwhm):
            raise ValueError(f"invalid QE width settings {self!r}")


@dataclass(frozen=True)
class QEWidthPrior:
    """The QE intrinsic-width density on ``[lo, hi]`` (meV): ``exp(-slope u(W))`` renormalised, with
    ``u`` = 0 for ``W <= knee - corner``, ``W - knee`` for ``W >= knee + corner`` and
    ``(W - knee + corner)**2 / (4 corner)`` between (C1). ``slope`` is in nats per meV. Zero outside
    ``[lo, hi]``; ``hi = knee + tail_nats / slope`` (:func:`qe_width_prior`)."""
    lo: float
    knee: float
    hi: float
    slope: float
    corner: float

    def __post_init__(self):
        vals = (self.lo, self.knee, self.hi, self.slope, self.corner)
        if not all(math.isfinite(float(v)) for v in vals):
            raise ValueError(f"QE width prior must be finite, got {self!r}")
        if not (self.slope > 0 and self.corner > 0 and 0 <= self.lo < self.knee - self.corner
                and self.hi > self.knee + self.corner):
            raise ValueError(f"invalid QE width prior {self!r}")
        s, c = float(self.slope), float(self.corner)
        a = s / (4.0 * c)                                  # the Gaussian piece exp(-a t^2), t in [0, 2c]
        flat = (self.knee - c) - self.lo
        corner = 0.5 * math.sqrt(math.pi / a) * math.erf(math.sqrt(a) * 2.0 * c)
        tail = (math.exp(-s * c) - math.exp(-s * (self.hi - self.knee))) / s
        z = flat + corner + tail
        for name, value in (("_a", a), ("_m_flat", flat), ("_m_corner", corner), ("_z", z),
                            ("_ln_z", math.log(z))):
            object.__setattr__(self, name, value)

    def penalty(self, w: float) -> float:
        """The log-prior penalty ``slope u(W)`` (nats, >= 0), any real ``w``."""
        t = float(w) - self.knee
        if t <= -self.corner:
            return 0.0
        if t >= self.corner:
            return self.slope * t
        return self.slope * (t + self.corner) ** 2 / (4.0 * self.corner)

    def d_penalty(self, w: float) -> float:
        """d penalty / d w (nats per meV), any real ``w``."""
        t = float(w) - self.knee
        if t <= -self.corner:
            return 0.0
        if t >= self.corner:
            return self.slope
        return self.slope * (t + self.corner) / (2.0 * self.corner)

    def log_pdf(self, w: float) -> float:
        """The normalised log density at ``w``; -inf outside ``[lo, hi]``."""
        w = float(w)
        if not self.lo <= w <= self.hi:
            return -math.inf
        return -self.penalty(w) - self._ln_z

    def d_log_pdf(self, w: float) -> float:
        """d log_pdf / d w inside ``[lo, hi]``."""
        return -self.d_penalty(w)

    def _cdf1(self, w: float) -> float:
        w = min(max(float(w), self.lo), self.hi)
        s, c = self.slope, self.corner
        edge = self.knee - c
        if w <= edge:
            mass = w - self.lo
        elif w <= self.knee + c:
            mass = self._m_flat + 0.5 * math.sqrt(math.pi / self._a) * math.erf(
                math.sqrt(self._a) * (w - edge))
        else:
            mass = (self._m_flat + self._m_corner
                    + (math.exp(-s * c) - math.exp(-s * (w - self.knee))) / s)
        return min(max(mass / self._z, 0.0), 1.0)

    def _ppf1(self, u: float) -> float:
        target = min(max(float(u), 0.0), 1.0) * self._z
        s, c = self.slope, self.corner
        if target <= self._m_flat:
            w = self.lo + target
        elif target <= self._m_flat + self._m_corner:
            r = target - self._m_flat
            arg = min(r * 2.0 * math.sqrt(self._a / math.pi), 1.0 - 1e-16)
            w = self.knee - c + float(special.erfinv(arg)) / math.sqrt(self._a)
        else:
            r = target - self._m_flat - self._m_corner
            w = self.knee - math.log(max(math.exp(-s * c) - s * r, 1e-300)) / s
        return min(max(w, self.lo), self.hi)

    def cdf(self, w) -> np.ndarray:
        """The CDF at ``w`` (vectorised): 0 at and below ``lo``, 1 at and above ``hi``."""
        w = np.asarray(w, dtype=float)
        return np.array([self._cdf1(v) for v in w.ravel().tolist()]).reshape(w.shape)

    def ppf(self, u) -> np.ndarray:
        """The inverse CDF at ``u`` in [0, 1] (vectorised), closed form on every piece."""
        u = np.asarray(u, dtype=float)
        return np.array([self._ppf1(v) for v in u.ravel().tolist()]).reshape(u.shape)

    def spec(self) -> dict:
        """The five fields as floats (JSON-able)."""
        return {"lo": float(self.lo), "knee": float(self.knee), "hi": float(self.hi),
                "slope": float(self.slope), "corner": float(self.corner)}


def qe_width_prior(fwhm: float, settings: QEWidthSettings | None = None) -> QEWidthPrior:
    """The QE width density for a spectrum of total resolution FWHM ``fwhm`` (meV): the one
    constructor every lane (fast pass, nested sampler, provenance) calls."""
    s = settings or QEWidthSettings()
    w = float(fwhm)
    if not (math.isfinite(w) and w > 0.0):
        raise ValueError(f"QE width prior needs a positive finite resolution FWHM, got {fwhm!r}")
    slope = s.slope / w
    knee = s.knee_fwhm * w
    return QEWidthPrior(s.floor_fwhm * w, knee, knee + s.tail_nats / slope, slope, s.corner_fwhm * w)


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


def hessian_from_grad(grad, x, steps):
    """Central difference of the gradient ``grad`` at x, symmetrised: column i is
    ``(grad(x + s_i e_i) - grad(x - s_i e_i)) / (2 s_i)``; 2n gradient calls. The full observed Hessian
    of whatever ``grad`` differentiates (never a Gauss-Newton form)."""
    x = np.asarray(x, float)
    n = x.size
    H = np.empty((n, n))
    for i in range(n):
        ei = np.zeros(n); ei[i] = steps[i]
        H[:, i] = (np.asarray(grad(x + ei), float) - np.asarray(grad(x - ei), float)) / (2 * steps[i])
    return 0.5 * (H + H.T)


def _ln_interval_mass(a, b):
    """ln(Phi(b) - Phi(a)) for standardised limits, either of which may be +-inf."""
    from scipy.special import log_ndtr
    if np.isneginf(a):
        return float(log_ndtr(b))
    if np.isposinf(b):
        return float(log_ndtr(-a))
    # In the upper tail both CDFs round to one. Mirror before subtracting, and use
    # expm1 to retain the difference for a narrow interval in either tail.
    if a >= 0.0:
        a, b = -b, -a
    lb = float(log_ndtr(b))
    return lb + math.log(-math.expm1(float(log_ndtr(a)) - lb))


def _ln_box_mass(mu, cov, lo, hi, sd):
    """ln of the mass of N(mu, cov) inside [lo, hi], keeping both limits of same-tail intervals.
    Limits beyond 6 sd may be omitted only when the interval crosses mu."""
    # The 6-sd cut drops under 1e-9 of absolute mass. It must not enlarge a same-tail
    # interval: there the omitted tail can be most of the interval's mass.
    a = (lo - mu) / sd
    b = (hi - mu) / sd
    a = np.where((a <= -6.0) & (b > 0.0), -np.inf, a)
    b = np.where((b >= 6.0) & (a < 0.0), np.inf, b)
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


def laplace_log_evidence(neg_log_post, theta, lo, hi, *, rel_step=1e-4, grad=None,
                         step_floor=None) -> LaplaceResult:
    """Posterior-mode Laplace evidence on the support box ``[lo, hi]`` (the box rule).

    neg_log_post(theta) = -(ln L + ln prior), finite inside the support. ``theta`` is the posterior
    mode found by a bounded optimiser, free coordinates only. Every Hessian stencil stays inside
    the support: its centre is nudged inward when needed, and its step is capped by the span.
    A coordinate within 1e-6 x span of a bound is railed: the one-sided inward gradient g enters.
    With C = H^-1 and mu = theta - C g the quadratic's centre,
    ``ln Z = -f(theta) + g'Cg/2 + (d/2) ln 2pi - (1/2) ln det H + ln P_box``, P_box the mass of
    N(mu, C) inside the box. One railed coordinate reproduces the single-boundary correction
    ``(g s)^2/2 + ln Phi(-g s)``. A Hessian that is not positive definite is ``no_evidence``.
    ``grad`` (the gradient of neg_log_post, or None): the Hessian is the central difference of the gradient
    (:func:`hessian_from_grad`) at the same nudged centre and steps, and a railed coordinate's inward
    gradient is ``grad(theta)`` clipped the same way; the value still comes from neg_log_post.
    A step is ``rel_step x max(|theta|, floor)``, the floor ``1e-2 x span`` unless ``step_floor`` (an
    array over the coordinates, NaN: that default) gives one: a width in its z coordinate asks for
    the floor its meV scale had before the coordinate changed, or a narrow pole's round-off wins."""
    theta = np.asarray(theta, float).copy()
    lo = np.asarray(lo, float)
    hi = np.asarray(hi, float)
    d = theta.size
    span = np.maximum(hi - lo, 1e-9)
    floor = 1e-2 * span
    if step_floor is not None:
        given = np.asarray(step_floor, float)
        floor = np.where(np.isfinite(given), given, floor)
    steps = np.minimum(rel_step * np.maximum(np.abs(theta), floor), (hi - lo) / 4.0)
    tol = 1e-6 * span
    at_lo = theta - lo <= tol
    at_hi = hi - theta <= tol
    # The rail tolerance determines the gradient correction, not whether a stencil crosses
    # the support. Keep central and mixed differences inside even just beyond that tolerance.
    x = np.clip(theta, lo + 2.0 * steps, hi - 2.0 * steps)
    f0 = float(neg_log_post(theta))
    H = hessian_fd(neg_log_post, x, steps) if grad is None else hessian_from_grad(grad, x, steps)
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
    if grad is not None:
        if at_lo.any() or at_hi.any():
            g0 = np.asarray(grad(theta), float)
            g[at_lo] = np.maximum(g0[at_lo], 0.0)
            g[at_hi] = np.minimum(g0[at_hi], 0.0)
    else:
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
