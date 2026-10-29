"""Composable model components for the structure-hypothesis loop.

A :class:`Component` owns an ordered list of named :class:`Parameter` objects and knows
how to ``evaluate`` its contribution on an energy grid. A :class:`CompositeModel` is an
ordered list of components summed together, with a flat prefixed parameter namespace
(``phonon.omega0``, ``elastic.h``, ``bg.BG`` …).

The components wrap the lineshape library (:mod:`isar.analyze.lineshapes`), where the full
damped-harmonic-oscillator form of McGuinness et al. 2026 Eq. 2 lives — the physics is not
reimplemented here. The phonon component carries its own Bose-tied anti-Stokes
(``peak="stokes+astokes"``), so anti-Stokes is never a free parameter (a *functional*
tie, unlike PUFFIN's equality-only link groups).

Each peak's amplitude coordinate is its convolved **height** ``h``: the maximum, over E >= 0, of
its whole convolved profile (:func:`isar.analyze.lineshapes.unit_peak_of_nodes`), in model units at
the spectrum's reference exposure. The unit column of a peak is its profile divided by that
maximum, so the model mean is ``exposure × Σ h_i·column_i``; the exposure multiplies once, in
:class:`CompositeModel`, never in a column.

Parameter ``kind`` (``"linear"`` vs ``"nonlinear"``) is what the grid+NNLS IC
(:mod:`isar.analyze.phonon`) needs: linear heights are solved by non-negative least
squares at fixed nonlinear values; nonlinear values (E0 = ``omega0``, Γ) are grid-scanned / optimized.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from isar.analyze.lineshapes import (KB, Lorentzian, UnitPeak, Voigt, _dho_grid_values, bose_numerator,
                                     convolve_tabulated, dho_signal, resolution_convolver, unit_peak,
                                     unit_peak_of_nodes, voigt_area_for_height)


def _curve(ctx):
    """The context's tabulated resolution curve, or ``None`` (duck-typed contexts have none)."""
    return getattr(ctx, "resolution_curve", None)


def _convolver(x, ctx):
    """The context's cached resolution convolution for a model evaluated at ``x``."""
    return resolution_convolver(x, ctx.gauss_fwhm, ctx.lorentz_fwhm, _curve(ctx))


def phonon_unit_columns(x, ctx, modes, peaks, *, areas=True):
    """``(columns, unit_peaks)`` for DHO modes ``modes`` = ``[(E0, Gamma), …]`` with peak selectors
    ``peaks``, from ONE batched convolution: ``columns`` is ``(k, x.size)``, each row the mode's
    convolved profile at ``x - ctx.center`` divided by its height (the maximum over E >= 0, so the
    row's maximum is 1; no exposure), and ``unit_peaks`` each mode's :class:`UnitPeak`.
    :meth:`CompositeModel.evaluate`, :meth:`CompositeModel.basis` and the fast-pass adapter all
    go through here. Likelihood-only callers pass ``areas=False`` to skip unused area sums."""
    x = np.asarray(x, dtype=float)
    if len(modes) == 0:
        return np.empty((0, x.size)), ()
    conv = _convolver(x, ctx)
    prepared = _dho_grid_values(conv, ctx.T)
    signals = np.stack([dho_signal(conv.grid, e0, gamma, 1.0, ctx.T, peak, _prepared=prepared)
                        for (e0, gamma), peak in zip(modes, peaks)])
    rows = conv.nodes(signals)
    ups = tuple(unit_peak_of_nodes(conv, row, e0, gamma, areas=areas)
                for row, (e0, gamma) in zip(rows, modes))
    q = x - ctx.center
    cols = np.stack([np.interp(q, conv.x_out, row, left=0.0, right=0.0) / up.height
                     for row, up in zip(rows, ups)])
    return cols, ups


@dataclass(frozen=True)
class Parameter:
    """A single model parameter. ``kind`` is ``"linear"`` (NNLS height) or
    ``"nonlinear"`` (grid-scanned / optimized)."""

    name: str
    kind: str  # "linear" | "nonlinear"


class Component:
    """Base class for a model component.

    Subclasses set ``prefix`` and ``params`` and implement :meth:`evaluate` and
    :meth:`unit_columns`. ``ctx`` is any object exposing ``gauss_fwhm``,
    ``lorentz_fwhm``, ``center`` and ``T`` (e.g. :class:`isar.analyze.context.ScanContext`).
    A context whose ``resolution_curve`` is set convolves with that tabulated curve instead of
    the Voigt (DHO, elastic and quasi-elastic alike).
    """

    prefix: str = "component"
    params: tuple[Parameter, ...] = ()

    def __init__(self, prefix: str | None = None):
        """Optional instance-level ``prefix`` override (default: the class attribute).

        Components are stateless — ``params`` is a class attribute and ``evaluate`` reads
        only its arguments — so instantiating a subclass by class (``PhononDHO()``) keeps the
        class prefix, while ``PhononDHO(prefix="phonon2")`` gives an independent namespace
        (the N-mode case). Subclasses do not define ``__init__``; they inherit this one.
        """
        if prefix is not None:
            self.prefix = prefix

    def param_names(self) -> list[str]:
        return [p.name for p in self.params]

    def linear_params(self) -> list[str]:
        return [p.name for p in self.params if p.kind == "linear"]

    def nonlinear_params(self) -> list[str]:
        return [p.name for p in self.params if p.kind == "nonlinear"]

    def evaluate(self, x, local: dict, ctx) -> np.ndarray:  # pragma: no cover - abstract
        raise NotImplementedError

    def unit_columns(self, x, nonlinear: dict, ctx) -> dict:  # pragma: no cover - abstract
        """Each linear parameter's unit-response column (value = 1), given the
        component's nonlinear values. Used to build the NNLS basis."""
        raise NotImplementedError


class PhononDHO(Component):
    """The full DHO (paper Eq. 2) and its Bose-tied anti-Stokes, convolved with the resolution.

    Fit-space params: ``omega0`` (the undamped energy E0) and ``Gamma`` (nonlinear), and ``h``
    (linear), the convolved height: the maximum of the whole convolved profile over E >= 0, in
    model units at the reference exposure. E0 is fitted directly, so overdamped modes
    (Γ > E0) are representable. The intrinsic amplitude ``A`` of the lineshape (the narrow-limit
    area) is derived, never fit: ``A = h / UnitPeak.height`` (:meth:`intrinsic_amplitude`), and the
    anti-Stokes tie is exact pointwise (detailed balance) by construction.

    ``peak`` selects the lineshape sides. Default ``"stokes+astokes"`` keeps the Bose-tied pair.
    ``"stokes"`` drops the anti-Stokes half for the high-q Bose-dead rescue (steering Phase 3a):
    where ``exp(−ħω/kT)`` is negligible the anti-Stokes carries no counts and only adds a degenerate
    gain-side peak, so fitting the Stokes side alone removes that degeneracy.
    """

    prefix = "phonon"
    params = (Parameter("omega0", "nonlinear"),
              Parameter("Gamma", "nonlinear"),
              Parameter("h", "linear"))

    def __init__(self, prefix: str | None = None, peak: str = "stokes+astokes"):
        super().__init__(prefix)
        self.peak = peak

    def evaluate(self, x, local: dict, ctx) -> np.ndarray:
        """``h`` times the unit column; no exposure (:class:`CompositeModel` applies it once)."""
        return local["h"] * self.unit_columns(x, local, ctx)["h"]

    def unit_columns(self, x, nonlinear: dict, ctx) -> dict:
        cols, _ = phonon_unit_columns(x, ctx, [(nonlinear["omega0"], nonlinear["Gamma"])],
                                      [self.peak], areas=False)
        return {"h": cols[0]}

    def unit_peak(self, nonlinear: dict, ctx) -> UnitPeak:
        """The :class:`UnitPeak` (per unit intrinsic A) at ``{"omega0", "Gamma"}``, on the grid of
        the context's fitted window."""
        conv = _convolver(np.asarray(ctx.x_used, dtype=float), ctx)
        return unit_peak(conv, nonlinear["omega0"], nonlinear["Gamma"], ctx.T, self.peak)[1]

    def intrinsic_amplitude(self, local: dict, ctx) -> float:
        """The lineshape's intrinsic A behind the fitted height: ``h / UnitPeak.height``."""
        return local["h"] / self.unit_peak(local, ctx).height


class Elastic(Component):
    """Resolution-limited elastic line: the resolution profile at ``ctx.center``.

    Linear param ``h`` is the line's height (the resolution profile divided by its continuous
    maximum, so ``h`` is the peak value). Distinct from a broad quasi-elastic component
    (which would carry its own nonlinear width) — that is a later-step extension.
    """

    prefix = "elastic"
    params = (Parameter("h", "linear"),)

    def evaluate(self, x, local: dict, ctx) -> np.ndarray:
        return local["h"] * self.unit_columns(x, {}, ctx)["h"]

    def unit_columns(self, x, nonlinear: dict, ctx) -> dict:
        x = np.asarray(x, dtype=float)
        curve = _curve(ctx)
        if curve is not None:
            return {"h": curve.profile(x - ctx.center) / float(np.max(curve.values))}
        area = voigt_area_for_height(1.0, ctx.gauss_fwhm, ctx.lorentz_fwhm)
        return {"h": Voigt(x, area, ctx.gauss_fwhm, ctx.lorentz_fwhm, ctx.center)}


class QuasiElastic(Component):
    """Broad quasi-elastic line: a Lorentzian of fixed width ``ctx.quasielastic_fwhm``
    centred at ``ctx.center``, convolved with the Gaussian resolution — i.e. a Voigt whose
    *Lorentzian* part is the (broad) QE width, not the resolution's narrow Lorentzian.

    Linear param ``QE`` (the Voigt area); the width is a card default, not fitted, so this
    slots into the NNLS basis as one extra linear column and model selection decides if it
    earns its parameter (§2/§4 "added only on evidence"). It models real diffuse / magnetic
    quasi-elastic scattering (e.g. Fe₂Mo₃O₈, §8) — distinct from the resolution-limited
    :class:`Elastic`, and, being broad, it has in-window leverage at EN≥1 where the sharp
    elastic has none (the off-window-elastic degeneracy the elastic gate excludes).
    """

    prefix = "qe"
    params = (Parameter("QE", "linear"),)

    def evaluate(self, x, local: dict, ctx) -> np.ndarray:
        curve = _curve(ctx)
        if curve is not None:
            width = ctx.quasielastic_fwhm
            return convolve_tabulated(x, ctx.center, curve,
                                      lambda e: Lorentzian(e, local["QE"], width, 0.0))
        return Voigt(np.asarray(x, dtype=float), local["QE"],
                     ctx.gauss_fwhm, ctx.quasielastic_fwhm, ctx.center)

    def unit_columns(self, x, nonlinear: dict, ctx) -> dict:
        return {"QE": self.evaluate(x, {"QE": 1.0}, ctx)}


QE_WEIGHT_CONVENTION = ("intrinsic Lorentzian x relaxational factor, unconvolved, integrated over "
                        "the fitted energy window")


def _relaxational_factor(E, T):
    """``r(E) = (E/kT) / (1 - exp(-E/kT))``: the relaxational detailed-balance factor, ``r(0) = 1`` and
    ``r(-E)/r(E) = exp(-E/kT)``. 1 when T is missing, non-finite or <= 0: the high-temperature limit
    (the T -> 0 limit diverges); BL43LXU records always carry a temperature, so this is a guard."""
    E = np.asarray(E, dtype=float)
    if T is None or not np.isfinite(T) or T <= 0:
        return np.ones_like(E)
    return bose_numerator(E, T) / (KB * float(T))


def central_qe_columns(x, ctx, widths, *, weights=False):
    """``(columns, weight_per_h)``: ``columns`` is ``(len(widths), x.size)``, row ``i`` the QE of
    intrinsic FWHM ``widths[i]`` (a unit-area Lorentzian at the energy zero times the relaxational
    factor, applied before the convolution and never renormalised over the grid) convolved with the
    resolution, at ``x - ctx.center`` and divided by its maximum over the convolver's output nodes
    (so a row's maximum is <= 1, = 1 when the peak lies inside ``x``: ``h`` is the convolved peak
    height, the phonons' convention). ``weight_per_h`` (``len(widths)``) is the intrinsic shape's
    integral over ``[min(ctx.x_used), max(ctx.x_used)] - ctx.center`` divided by the same maximum
    (``None`` unless ``weights``). One batched convolution.
    shortcut: the grid ends 20 meV past the data, so a width of tens of meV loses its far tail;
    widen ``lineshapes.CONVOLUTION_PAD`` if the fitted width ever rails at the prior's far end."""
    x = np.asarray(x, dtype=float)
    widths = np.atleast_1d(np.asarray(widths, dtype=float))
    conv = _convolver(x, ctx)

    def intrinsic(E):
        return (widths[:, None] / (2.0 * np.pi)) / (E ** 2 + (0.5 * widths[:, None]) ** 2) \
            * _relaxational_factor(E, ctx.T)

    rows = conv.nodes(intrinsic(conv.grid))
    peak = rows.max(axis=1)
    q = x - ctx.center
    cols = np.stack([np.interp(q, conv.x_out, row, left=0.0, right=0.0) / p
                     for row, p in zip(rows, peak)])
    if not weights:
        return cols, None
    used = np.asarray(ctx.x_used, dtype=float) - ctx.center
    lo, hi = float(used.min()), float(used.max())
    inside = conv.grid[(conv.grid > lo) & (conv.grid < hi)]
    E = np.concatenate(([lo], inside, [hi]))
    return cols, np.trapezoid(intrinsic(E), E, axis=1) / peak


class CentralQE(Component):
    """The central quasi-elastic component of a BL43LXU spectrum: phenomenological, a "central
    excess" with no mechanism claimed (reported as a spectral weight and a width, nothing more).

    A Lorentzian of fitted intrinsic FWHM ``width`` centred on the energy zero (never fitted
    separately), times the relaxational detailed-balance factor (:func:`central_qe_columns`), always
    convolved with the measured resolution. ``h`` (linear) is the convolved peak height, so the
    phonons' height prior applies unchanged; ``width`` (nonlinear) has its own prior
    (:class:`isar.analyze.prior.QEWidthPrior`). ``CentralQE(width=w)`` pins the width (a card's
    ``quasielastic_fwhm``): no width parameter, no width prior. Not :class:`QuasiElastic`, which
    stays the IN8 fixed-width variant, and not a scene component."""

    prefix = "qe"
    params = (Parameter("width", "nonlinear"), Parameter("h", "linear"))
    width: float | None = None        # the pinned width, None when fitted

    def __init__(self, prefix: str | None = None, width: float | None = None):
        super().__init__(prefix)
        if width is not None:
            if not (np.isfinite(width) and width > 0):
                raise ValueError(f"a pinned QE width must be positive and finite, got {width!r}")
            self.width = float(width)
            self.params = (Parameter("h", "linear"),)

    def width_of(self, local: dict) -> float:
        return self.width if self.width is not None else float(local["width"])

    def unit_columns(self, x, nonlinear: dict, ctx) -> dict:
        cols, _ = central_qe_columns(x, ctx, [self.width_of(nonlinear)])
        return {"h": cols[0]}

    def evaluate(self, x, local: dict, ctx) -> np.ndarray:
        """``h`` times the unit column; no exposure (:class:`CompositeModel` applies it once)."""
        return local["h"] * self.unit_columns(x, local, ctx)["h"]

    def spectral_weight(self, local: dict, ctx) -> float:
        """The intrinsic shape's integral over the fitted window, at height ``local["h"]``
        (:data:`QE_WEIGHT_CONVENTION`): counts x meV at the reference exposure."""
        _, per_h = central_qe_columns(ctx.x_used, ctx, [self.width_of(local)], weights=True)
        return float(local["h"] * per_h[0])


class ConstantBackground(Component):
    """Flat background. Linear param ``BG``."""

    prefix = "bg"
    params = (Parameter("BG", "linear"),)

    def evaluate(self, x, local: dict, ctx) -> np.ndarray:
        return np.full(np.shape(x), float(local["BG"]), dtype=float)

    def unit_columns(self, x, nonlinear: dict, ctx) -> dict:
        return {"BG": np.ones(np.shape(x), dtype=float)}


class CompositeModel:
    """An ordered list of components summed together over a flat parameter vector.

    The global parameter order is the concatenation of each component's params, named
    ``f"{component.prefix}.{param}"``. For ``[PhononDHO, Elastic, ConstantBackground]``
    this is ``(omega0, Gamma, h, elastic.h, BG)``.
    """

    def __init__(self, components: list[Component]):
        self.components = list(components)

    # -- parameter namespace -------------------------------------------------
    def _global(self, comp: Component, name: str) -> str:
        return f"{comp.prefix}.{name}"

    def param_names(self) -> list[str]:
        return [self._global(c, n) for c in self.components for n in c.param_names()]

    def linear_names(self) -> list[str]:
        return [self._global(c, n) for c in self.components for n in c.linear_params()]

    def nonlinear_names(self) -> list[str]:
        return [self._global(c, n) for c in self.components for n in c.nonlinear_params()]

    @property
    def n_params(self) -> int:
        return len(self.param_names())

    # -- evaluation ----------------------------------------------------------
    def _split(self, theta) -> list[dict]:
        """Map a flat theta vector to one local {name: value} dict per component."""
        theta = np.asarray(theta, dtype=float)
        out, i = [], 0
        for c in self.components:
            names = c.param_names()
            out.append({n: float(theta[i + k]) for k, n in enumerate(names)})
            i += len(names)
        return out

    def _phonon_columns(self, x, ctx, locals_):
        """The unit columns of every phonon component, from one batched convolution:
        ``{component index: column}``."""
        at = [i for i, c in enumerate(self.components) if isinstance(c, PhononDHO)]
        cols, _ = phonon_unit_columns(x, ctx, [(locals_[i]["omega0"], locals_[i]["Gamma"])
                                               for i in at],
                                      [self.components[i].peak for i in at], areas=False)
        return dict(zip(at, cols))

    def evaluate(self, x, theta, ctx) -> np.ndarray:
        """The model mean at ``x``: ``exposure × (Σ h_i·column_i + the other components)``. The
        phonon modes' columns come from one batched convolution."""
        x = np.asarray(x, dtype=float)
        locals_ = self._split(theta)
        phonon_cols = self._phonon_columns(x, ctx, locals_)
        total = np.zeros_like(x)
        for i, (comp, local) in enumerate(zip(self.components, locals_)):
            total = total + (local["h"] * phonon_cols[i] if i in phonon_cols
                             else comp.evaluate(x, local, ctx))
        exposure = getattr(ctx, "exposure_for", lambda _x: None)(x)
        if exposure is not None:
            total = total * exposure
        return total

    def evaluate_components(self, x, theta, ctx) -> list[tuple[str, np.ndarray]]:
        """Per-component contributions ``[(prefix, values), …]`` (they sum to evaluate).

        Useful for plotting the model decomposition and for residual diagnosis."""
        x = np.asarray(x, dtype=float)
        exposure = getattr(ctx, "exposure_for", lambda _x: None)(x)
        parts = [(comp.prefix, comp.evaluate(x, local, ctx))
                 for comp, local in zip(self.components, self._split(theta))]
        if exposure is not None:
            parts = [(name, values * exposure) for name, values in parts]
        return parts

    def nnls_basis(self, x, nonlinear_global: dict, ctx):
        """Stack each linear parameter's unit column → (phi, linear_global_names).

        ``nonlinear_global`` maps global nonlinear names (e.g. ``"phonon.omega0"``) to
        the values at which the basis is evaluated (the IC grid candidate)."""
        nonlinear = set(self.nonlinear_names())
        theta = [float(nonlinear_global[n]) if n in nonlinear else 0.0 for n in self.param_names()]
        return self.basis(x, theta, ctx), self.linear_names()

    def static_columns(self, x, ctx) -> dict:
        """The unit columns that depend on no parameter (components with no nonlinear parameter:
        elastic, quasi-elastic, background), keyed by component index, for :meth:`basis` to reuse
        across the evaluations of one fit."""
        return {i: comp.unit_columns(x, {}, ctx) for i, comp in enumerate(self.components)
                if not comp.nonlinear_params()}

    def basis(self, x, theta, ctx, *, static=None) -> np.ndarray:
        """One column per :meth:`linear_names` entry: that parameter's unit response at
        ``theta``'s nonlinear values, exposure applied, so the model mean is ``basis @
        heights`` (``theta``'s linear entries are not read). The phonon columns share one
        batched convolution; ``static`` (from :meth:`static_columns`) supplies the columns that
        depend on no parameter."""
        x = np.asarray(x, dtype=float)
        locals_ = self._split(theta)
        by_component = self._phonon_columns(x, ctx, locals_)
        cols = []
        for i, (comp, local) in enumerate(zip(self.components, locals_)):
            if i in by_component:
                cols.append(by_component[i])
                continue
            unit = static[i] if static is not None and i in static else comp.unit_columns(x, local, ctx)
            cols.extend(np.asarray(unit[n], dtype=float) for n in comp.linear_params())
        phi = np.column_stack(cols) if cols else np.empty((x.size, 0))
        exposure = getattr(ctx, "exposure_for", lambda _x: None)(x)
        if exposure is not None:
            phi = phi * exposure[:, None]
        return phi

    # -- derived quantities --------------------------------------------------
    def _phonon_slots(self):
        """``[(global_index_of_omega0, component), …]`` for each PhononDHO, in component
        order. Within a PhononDHO the params are ``(omega0, Gamma, h)`` so ``i`` is E0,
        ``i+1`` is Gamma, ``i+2`` is the linear height h."""
        out, i = [], 0
        for c in self.components:
            if isinstance(c, PhononDHO):
                out.append((i, c))
            i += len(c.param_names())
        return out

    def _phonon_index(self):
        """Global index of the FIRST phonon's omega0 (back-compat helper), or None."""
        slots = self._phonon_slots()
        return slots[0][0] if slots else None

    def phonon_indices(self) -> list[int]:
        """Global omega0 index of every PhononDHO, in component order."""
        return [i for i, _ in self._phonon_slots()]

    def mode_order(self, theta) -> np.ndarray:
        """Index permutation ``perm`` of the flat parameter vector that puts the phonon blocks
        ``(omega0, Gamma, h)`` in ascending ``omega0``: ``theta[perm]`` is the canonical labelling
        (and ``cov[np.ix_(perm, perm)]`` its covariance). Swapped labels are one solution; the
        ordered-energy prior (:mod:`isar.analyze.prior`) is defined on the ordered one, so a fit
        that converged with swapped labels is canonicalised through this before its prior is read.
        Ordered on ``omega0``, the sampled coordinate, where the prior's normalisation is exact."""
        theta = np.asarray(theta, dtype=float)
        starts = self.phonon_indices()
        width = len(PhononDHO.params)
        perm = np.arange(self.n_params)
        for dst, src in zip(starts, sorted(starts, key=lambda i: theta[i])):
            perm[dst:dst + width] = np.arange(src, src + width)
        return perm

    def is_stokes_only(self) -> bool:
        """True iff the composite has ≥1 phonon and *every* phonon is Stokes-only
        (``peak`` has no anti-Stokes side) — the Bose-dead high-q rescue (Phase 3a).
        Drives the ``stokes_only`` fit-kind tag so a one-sided fit never reads as an
        ordinary two-sided ``resolved`` phonon."""
        slots = self._phonon_slots()
        return bool(slots) and all("astokes" not in getattr(c, "peak", "stokes+astokes")
                                   for _, c in slots)

    def _omega0_at(self, theta, cov, i):
        """E0 + HESSE error for the phonon whose omega0 is at global index ``i`` (the error is
        NaN without a usable variance)."""
        if cov is None:
            return float(theta[i]), float("nan")
        var = float(np.asarray(cov, dtype=float)[i, i])
        return float(theta[i]), float(np.sqrt(var)) if var > 0 else float("nan")

    def omega0_all(self, theta, cov):
        """All phonons' (E0, error), sorted ascending by E0 (canonicalization)."""
        theta = np.asarray(theta, dtype=float)
        out = [self._omega0_at(theta, cov, i) for i in self.phonon_indices()]
        out.sort(key=lambda t: t[0])
        return out

    def primary_phonon_index(self, theta, cov=None, prior_omega=None, acoustic_omega=None,
                             T=None, ctx=None):
        """Global omega0 index of the *primary* phonon. Precedence (Phase A): the mode nearest the
        branch-assignment ``acoustic_omega`` hint (which mode IS the acoustic QoI) if given; else the
        mode nearest the dispersion ``prior_omega``; else the strongest mode, the largest intrinsic
        amplitude ``A`` (:meth:`PhononDHO.intrinsic_amplitude`, which needs ``ctx``). Not the largest
        height ``h``: a broad near-elastic mode that mimics the elastic wings can out-top a real
        phonon in height while carrying far less intrinsic amplitude. Without ``ctx`` there is no
        ``A``, and the largest ``h`` stands in. ``T`` is kept for the callers' signature and not
        read. ``None`` if there is no phonon."""
        theta = np.asarray(theta, dtype=float)
        slots = self.phonon_indices()
        if not slots:
            return None
        target = acoustic_omega if acoustic_omega is not None else prior_omega
        if target is not None:
            po = float(target)
            return min(slots, key=lambda i: abs(theta[i] - po))
        if ctx is None:
            return max(slots, key=lambda i: theta[i + 2])
        slot = dict(self._phonon_slots())
        return max(slots, key=lambda i: slot[i].intrinsic_amplitude(
            {"omega0": theta[i], "Gamma": theta[i + 1], "h": theta[i + 2]}, ctx))

    def omega0(self, theta, cov, prior_omega=None, acoustic_omega=None, ctx=None):
        """E0 and its HESSE error for the *primary* phonon (§ primary =
        acoustic-hint / nearest-to-prior / largest-intrinsic-amplitude). Single-phonon composites
        are unchanged."""
        theta = np.asarray(theta, dtype=float)
        i = self.primary_phonon_index(theta, cov, prior_omega, acoustic_omega, ctx=ctx)
        if i is None:
            raise ValueError("composite has no PhononDHO component")
        return self._omega0_at(theta, cov, i)
