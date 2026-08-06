"""Composable model components for the structure-hypothesis loop.

A :class:`Component` owns an ordered list of named :class:`Parameter` objects and knows
how to ``evaluate`` its contribution on an energy grid. A :class:`CompositeModel` is an
ordered list of components summed together, with a flat prefixed parameter namespace
(``phonon.Omega_d``, ``elastic.E``, ``bg.BG`` …).

The components wrap the *frozen, validated* lineshape library
(``Fitting_Functions.convolute_voigt_dho`` via :mod:`isar.analyze.lineshapes`) — the physics is
not reimplemented here. The phonon component carries its own Bose-tied anti-Stokes
(``peak="stokes+astokes"``), so anti-Stokes is never a free parameter (a *functional*
tie, unlike PUFFIN's equality-only link groups).

Parameter ``kind`` (``"linear"`` vs ``"nonlinear"``) is what the grid+NNLS IC
(:mod:`isar.analyze.ic`) needs: linear amplitudes are solved by non-negative least
squares at fixed nonlinear values; nonlinear values (Ω_d, Γ) are grid-scanned / optimized.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from isar.analyze.lineshapes import Voigt, convolute_voigt_dho
from isar.analyze.phonon import omega0_from, omega0_value_error


@dataclass(frozen=True)
class Parameter:
    """A single model parameter. ``kind`` is ``"linear"`` (NNLS amplitude) or
    ``"nonlinear"`` (grid-scanned / optimized)."""

    name: str
    kind: str  # "linear" | "nonlinear"


class Component:
    """Base class for a model component.

    Subclasses set ``prefix`` and ``params`` and implement :meth:`evaluate` and
    :meth:`unit_columns`. ``ctx`` is any object exposing ``gauss_fwhm``,
    ``lorentz_fwhm``, ``center`` and ``T`` (e.g. :class:`isar.analyze.context.ScanContext`).
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
        """Each linear amplitude's unit-response column (amplitude = 1), given the
        component's nonlinear values. Used to build the NNLS basis."""
        raise NotImplementedError


class PhononDHO(Component):
    """Stokes DHO + its Bose-tied anti-Stokes, convolved with the Voigt resolution.

    Fit-space params: ``Omega_d``, ``Gamma`` (nonlinear) and ``A`` (linear). The reported
    ω₀ = √(Ω_d²+Γ²) is derived, never fit directly (keeps Ω_d ≥ 0, ω₀ ≥ Γ).

    ``peak`` selects the lineshape sides passed to ``convolute_voigt_dho``. Default
    ``"stokes+astokes"`` keeps the Bose-tied pair (unchanged Step-1 behaviour). ``"stokes"``
    drops the anti-Stokes mirror for the high-q Bose-dead rescue (steering Phase 3a): where
    ``exp(−ħω/kT)`` is negligible the anti-Stokes carries no counts and only adds a degenerate
    gain-side peak, so fitting the Stokes side alone removes that degeneracy.
    """

    prefix = "phonon"
    params = (Parameter("Omega_d", "nonlinear"),
              Parameter("Gamma", "nonlinear"),
              Parameter("A", "linear"))

    def __init__(self, prefix: str | None = None, peak: str = "stokes+astokes"):
        super().__init__(prefix)
        self.peak = peak

    def evaluate(self, x, local: dict, ctx) -> np.ndarray:
        omega0 = omega0_from(local["Omega_d"], local["Gamma"])
        return convolute_voigt_dho(x, omega0, ctx.center, ctx.gauss_fwhm, ctx.lorentz_fwhm,
                                   local["Gamma"], local["A"], 0.0, 0.0, ctx.T,
                                   peak=self.peak)

    def unit_columns(self, x, nonlinear: dict, ctx) -> dict:
        unit = {"Omega_d": nonlinear["Omega_d"], "Gamma": nonlinear["Gamma"], "A": 1.0}
        return {"A": self.evaluate(x, unit, ctx)}


class Elastic(Component):
    """Resolution-limited elastic line: a Voigt at ``ctx.center`` (width = resolution).

    Linear param ``E`` (the Voigt area). Distinct from a broad quasi-elastic component
    (which would carry its own nonlinear width) — that is a later-step extension.
    """

    prefix = "elastic"
    params = (Parameter("E", "linear"),)

    def evaluate(self, x, local: dict, ctx) -> np.ndarray:
        return Voigt(np.asarray(x, dtype=float), local["E"],
                     ctx.gauss_fwhm, ctx.lorentz_fwhm, ctx.center)

    def unit_columns(self, x, nonlinear: dict, ctx) -> dict:
        return {"E": self.evaluate(x, {"E": 1.0}, ctx)}


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
        return Voigt(np.asarray(x, dtype=float), local["QE"],
                     ctx.gauss_fwhm, ctx.quasielastic_fwhm, ctx.center)

    def unit_columns(self, x, nonlinear: dict, ctx) -> dict:
        return {"QE": self.evaluate(x, {"QE": 1.0}, ctx)}


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
    this is exactly ``(Omega_d, Gamma, A, E, BG)`` — matching the frozen Phase-1 fitter.
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

    def evaluate(self, x, theta, ctx) -> np.ndarray:
        x = np.asarray(x, dtype=float)
        total = np.zeros_like(x)
        for comp, local in zip(self.components, self._split(theta)):
            total = total + comp.evaluate(x, local, ctx)
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
        """Stack each linear amplitude's unit column → (phi, linear_global_names).

        ``nonlinear_global`` maps global nonlinear names (e.g. ``"phonon.Omega_d"``) to
        the values at which the basis is evaluated (the IC grid candidate)."""
        cols, names = [], []
        for comp in self.components:
            local_nl = {n: nonlinear_global[self._global(comp, n)]
                        for n in comp.nonlinear_params()}
            unit = comp.unit_columns(x, local_nl, ctx)
            for n in comp.linear_params():
                names.append(self._global(comp, n))
                cols.append(np.asarray(unit[n], dtype=float))
        phi = np.column_stack(cols) if cols else np.empty((np.size(x), 0))
        exposure = getattr(ctx, "exposure_for", lambda _x: None)(np.asarray(x, dtype=float))
        if exposure is not None:
            phi = phi * exposure[:, None]
        return phi, names

    # -- derived quantities --------------------------------------------------
    def _phonon_slots(self):
        """``[(global_index_of_Omega_d, component), …]`` for each PhononDHO, in component
        order. Within a PhononDHO the params are ``(Omega_d, Gamma, A)`` so ``i`` is Omega_d,
        ``i+1`` is Gamma, ``i+2`` is the linear amplitude A."""
        out, i = [], 0
        for c in self.components:
            if isinstance(c, PhononDHO):
                out.append((i, c))
            i += len(c.param_names())
        return out

    def _phonon_index(self):
        """Global index of the FIRST phonon's Omega_d (back-compat helper), or None."""
        slots = self._phonon_slots()
        return slots[0][0] if slots else None

    def phonon_indices(self) -> list[int]:
        """Global Omega_d index of every PhononDHO, in component order."""
        return [i for i, _ in self._phonon_slots()]

    def is_stokes_only(self) -> bool:
        """True iff the composite has ≥1 phonon and *every* phonon is Stokes-only
        (``peak`` has no anti-Stokes side) — the Bose-dead high-q rescue (Phase 3a).
        Drives the ``stokes_only`` fit-kind tag so a one-sided fit never reads as an
        ordinary two-sided ``resolved`` phonon."""
        slots = self._phonon_slots()
        return bool(slots) and all("astokes" not in getattr(c, "peak", "stokes+astokes")
                                   for _, c in slots)

    def _omega0_at(self, theta, cov, i):
        """ω₀ + HESSE error for the phonon whose Omega_d is at global index ``i``."""
        sub = None
        if cov is not None:
            cov = np.asarray(cov, dtype=float)
            sub = np.array([[cov[i, i], cov[i, i + 1]],
                            [cov[i + 1, i], cov[i + 1, i + 1]]])
        return omega0_value_error(theta[i], theta[i + 1], sub)

    def omega0_all(self, theta, cov):
        """All phonons' derived (ω₀, error), sorted ascending by ω₀ (canonicalization)."""
        theta = np.asarray(theta, dtype=float)
        out = [self._omega0_at(theta, cov, i) for i in self.phonon_indices()]
        out.sort(key=lambda t: t[0])
        return out

    def primary_phonon_index(self, theta, cov=None, prior_omega=None, acoustic_omega=None):
        """Global Omega_d index of the *primary* phonon. Precedence (Phase A): the mode nearest the
        branch-assignment ``acoustic_omega`` hint (which mode IS the acoustic QoI) if given; else the
        mode nearest the dispersion ``prior_omega``; else the largest-amplitude mode (A at ``i+2``).
        ``None`` if there is no phonon. Bit-identical to the pre-Phase-A behaviour when both hints are
        None."""
        theta = np.asarray(theta, dtype=float)
        slots = self.phonon_indices()
        if not slots:
            return None
        target = acoustic_omega if acoustic_omega is not None else prior_omega
        if target is not None:
            po = float(target)
            return min(slots, key=lambda i: abs(omega0_from(theta[i], theta[i + 1]) - po))
        return max(slots, key=lambda i: theta[i + 2])

    def omega0(self, theta, cov, prior_omega=None, acoustic_omega=None):
        """Derived ω₀ and its (HESSE-propagated) error for the *primary* phonon (§ primary =
        acoustic-hint / nearest-to-prior / largest-amplitude). Single-phonon composites are unchanged."""
        theta = np.asarray(theta, dtype=float)
        i = self.primary_phonon_index(theta, cov, prior_omega, acoustic_omega)
        if i is None:
            raise ValueError("composite has no PhononDHO component")
        return self._omega0_at(theta, cov, i)
