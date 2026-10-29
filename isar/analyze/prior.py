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
# /6: each ordered energy's flat density times the near-zero penalty (EnergyPrior), renormalised
# over the window; the ramp's numbers are PriorSettings.energy_ramp_*.
PRIOR_RECIPE_VERSION = "isar.mode-prior-recipe/6"

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
    width_kind: "softlog" -> density 1/(Gamma + width_knee) on [0, width_cap]; "flat" -> uniform.
    width_knee: meV (0.12).  width_cap: meV (20; check 40).
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
    energy: EnergyPrior       # each mode energy's density on [e_lo, e_hi] (the near-zero penalty)
    fwhm: float               # meV, the resolution FWHM the penalty is measured in
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
        """ln(k! prod g(E_i)) on the ordered region, g the penalised energy density
        (:attr:`energy`); a function of the set, so label order is irrelevant.
        One implementation: :func:`ordered_energy_log_prior`."""
        return ordered_energy_log_prior(energies, self.energy)

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
               "energy_penalty": self.energy.spec() | {"fwhm_meV": float(self.fwhm)},
               "settings": asdict(self.settings)}
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
    if not e_hi > s.energy_floor:
        raise ValueError(f"resolve_prior: no positive energy range (max E {e_hi!r})")
    return SpectrumPrior(settings=s, max_count=mx, e_lo=s.energy_floor, e_hi=e_hi,
                         energy=energy_prior(s.energy_floor, e_hi, fwhm, s), fwhm=float(fwhm))


def resolve_prior_for(ctx, settings: PriorSettings | None = None) -> SpectrumPrior:
    """The one ctx-level entry (``mode_bounds``, ``recipe_provenance`` and the fast pass call it):
    the kept points' counts, energies, exposure and resolution FWHM; under ``legacy_v4`` also the
    v4 ceiling."""
    s = settings or PriorSettings()
    sp = resolve_prior(ctx.counts_used, ctx.x_used, s, fwhm=resolution_fwhm(ctx),
                       exposure_scale=ctx.exposure_used)
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

    def log_pdf(self, e: float) -> float:
        """The normalised log density at ``e``; -inf outside ``[lo, hi]``. Scalar and cheap: it
        runs inside the fast pass's objective."""
        e = float(e)
        if not self.lo <= e <= self.hi:
            return -math.inf
        return -self.penalty(e) - self._ln_z

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


def laplace_log_evidence(neg_log_post, theta, lo, hi, *, rel_step=1e-4) -> LaplaceResult:
    """Posterior-mode Laplace evidence on the support box ``[lo, hi]`` (the box rule).

    neg_log_post(theta) = -(ln L + ln prior), finite inside the support. ``theta`` is the posterior
    mode found by a bounded optimiser, free coordinates only. Every Hessian stencil stays inside
    the support: its centre is nudged inward when needed, and its step is capped by the span.
    A coordinate within 1e-6 x span of a bound is railed: the one-sided inward gradient g enters.
    With C = H^-1 and mu = theta - C g the quadratic's centre,
    ``ln Z = -f(theta) + g'Cg/2 + (d/2) ln 2pi - (1/2) ln det H + ln P_box``, P_box the mass of
    N(mu, C) inside the box. One railed coordinate reproduces the single-boundary correction
    ``(g s)^2/2 + ln Phi(-g s)``. A Hessian that is not positive definite is ``no_evidence``."""
    theta = np.asarray(theta, float).copy()
    lo = np.asarray(lo, float)
    hi = np.asarray(hi, float)
    d = theta.size
    span = np.maximum(hi - lo, 1e-9)
    steps = np.minimum(rel_step * np.maximum(np.abs(theta), 1e-2 * span), (hi - lo) / 4.0)
    tol = 1e-6 * span
    at_lo = theta - lo <= tol
    at_hi = hi - theta <= tol
    # The rail tolerance determines the gradient correction, not whether a stencil crosses
    # the support. Keep central and mixed differences inside even just beyond that tolerance.
    x = np.clip(theta, lo + 2.0 * steps, hi - 2.0 * steps)
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
