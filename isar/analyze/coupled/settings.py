"""The inter-spectra settings: one table of names, bounds and starting values (C-settings).

Five rows are calibrated on synthetic tracks, not on real data (``calibrated=True``); the rest are
uncalibrated starting values. Stdlib only; Qt-free.
``InterSettings.from_values`` is the checked reader: an unknown name, a wrong type or an out-of-bounds
value raises ValueError naming it. The writer (deltas, ``None`` = back to default) is
``isar.intake.inter_values``; the reader that folds those deltas is ``isar.intake.inter_spectra``.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

SETTINGS_VERSION = 1


@dataclass(frozen=True)
class SettingDef:
    name: str
    group: Literal["front", "advanced", "hidden"]     # dialog placement; "hidden" = not shown
    kind: Literal["float", "int", "choice", "bool"]
    default: object
    calibrated: bool                                  # False = "uncalibrated"; True = calibrated on synthetic tracks
    unit: str | None
    minimum: float | None
    maximum: float | None
    choices: tuple[str, ...] | None
    help: str                                         # one plain sentence: what it does
    strict_minimum: bool = False                      # the table's "> 0": the minimum itself is refused


def _row(name, group, kind, default, unit, minimum, maximum, help_, *, choices=None, strict=False, cal=False):
    return SettingDef(name, group, kind, default, cal, unit, minimum, maximum, choices, help_, strict)


SETTING_DEFS: tuple[SettingDef, ...] = (
    _row("q_slope_meV_per_rlu", "front", "float", 10.6, "meV/r.l.u.", 0, 1000,
         "Anchored batches, scans with no lattice loaded: the energy change per reciprocal lattice unit of distance "
         "between neighbours at which the q-change penalty rises most steeply.", strict=True),
    _row("q_slope_meV_per_invA", "front", "float", 8.0, "meV/Å⁻¹", 0, 1000,
         "Anchored batches, scans with a lattice loaded: the energy change per inverse angstrom of distance between "
         "neighbours at which the q-change penalty rises most steeply.", strict=True),
    _row("t_tolerance_meV", "front", "float", 1.0, "meV", 0, 50,
         "Temperature neighbours: the energy change that costs little at the reference temperature step.",
         strict=True, cal=True),
    _row("flag_threshold", "front", "float", 54.0, None, 0, 1000,
         "A spectrum is flagged for review when its flag score reaches this value.", strict=True, cal=True),
    _row("shape_family", "advanced", "choice", "welsch", None, None, None,
         "The shape of the temperature and curvature penalties: welsch (bounded, the default) or quartic (for comparison); "
         "the q-change penalty is always welsch.",
         choices=("welsch", "quartic")),
    _row("step_exponent", "advanced", "float", 0.25, None, 0, 2,
         "How much the tolerated temperature change widens with the size of the temperature step.", cal=True),
    _row("sweep_tol_meV", "advanced", "float", 0.01, "meV", 0, 1,
         "A sweep that moves no mode energy by more than this is quiet; two quiet sweeps settle a batch.",
         strict=True),
    _row("sweep_cap", "advanced", "int", 20, "sweeps", 2, 1000,
         "A simultaneous batch that has not settled after this many sweeps is kept and flagged as did not settle."),
    _row("q_curvature_scale_meV", "hidden", "float", 1.0, "meV", 0, 50,
         "Simultaneous q links: the energy scale of the curvature residual along a line of three spectra.",
         strict=True),
    _row("reference_T_K", "hidden", "float", 100.0, "K", 0, 1000,
         "The temperature step, in kelvin, at which the temperature tolerance applies unchanged.", strict=True, cal=True),
    _row("t_spacing_form", "hidden", "choice", "widen", None, None, None,
         "How the temperature step enters the penalty: widen (the default) or divide (the literal form, for comparison).",
         choices=("widen", "divide")),
    _row("penalty_cap", "hidden", "float", 12.0, "deviance", 0, 1000,
         "The largest price one penalty term can add to a spectrum's fit.", strict=True, cal=True),
    _row("birth_edge", "hidden", "float", 2.0, "deviance", 0, 1000,
         "The entry price of a branch that no neighbour predicts, at any energy in the window."),
    _row("birth_mid", "hidden", "float", 12.0, "deviance", 0, 1000,
         "The extra entry price of such a branch when it appears in the middle of the window."),
    _row("cost_rel_tol", "hidden", "float", 1e-5, None, 0, 1,
         "A sweep whose total cost improves by less than this relative amount is not improving.", strict=True),
    _row("collinearity_tol", "hidden", "float", 0.01, None, 0, 1,
         "How far, relative to its length, the middle of three q points may sit off the straight line through the outer two."),
    _row("composition_limit", "hidden", "int", 4, None, 0, 100,
         "How many ranked alternative compositions are priced per mode count, beyond the incoming and all-unmatched ones."),
    _row("confidence", "hidden", "float", 0.80, None, 0.5, 1,
         "A mode count or assignment whose probability is below this is reported as ambiguous."),
    _row("close_call_nats", "hidden", "float", 2.0, "nats", 0, 20,
         "Alternatives within this much log evidence of the winner are kept for review."),
    _row("evidence_hysteresis_nats", "hidden", "float", 0.1, "nats", 0, 5,
         "A count or assignment change must win the evidence by this margin, or the incoming one stays."),
    _row("linewidth_ratio", "hidden", "float", 2.0, None, 1, 100,
         "A linewidth change is reported when a mode's damping ratio to its fast solo fit passes this.", strict=True),
    _row("full_search", "hidden", "bool", False, None, None, None,
         "Price every allowed count and composition, not only the triggered ones (slow; for calibration and proofs)."),
)

_BY_NAME = {d.name: d for d in SETTING_DEFS}
RETIRED_SETTINGS = frozenset({"q_tolerance_meV", "q_change_scale_meV"})   # saved values are ignored (intake fold)


def _expect(d: SettingDef) -> str:
    if d.kind == "choice":
        return f"one of {', '.join(d.choices)}"
    return {"bool": "true or false", "int": "a whole number", "float": "a finite number"}[d.kind]


def _checked(d: SettingDef, value: object) -> object:
    if d.kind == "bool":
        ok = isinstance(value, bool)
    elif d.kind == "int":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif d.kind == "float":
        ok = (isinstance(value, (int, float)) and not isinstance(value, bool)
              and (isinstance(value, int) or math.isfinite(value)))
    else:
        ok = isinstance(value, str) and value in d.choices
    if not ok:
        raise ValueError(f"inter-spectra setting {d.name!r} must be {_expect(d)} (got {value!r})")
    if d.kind in ("float", "int"):
        low = d.minimum is not None and (value < d.minimum or (d.strict_minimum and value == d.minimum))
        if low or (d.maximum is not None and value > d.maximum):
            raise ValueError(f"inter-spectra setting {d.name!r} is out of bounds (got {value!r})")
        return float(value) if d.kind == "float" else value
    return value


@dataclass(frozen=True)
class InterSettings:
    version: int = SETTINGS_VERSION
    q_slope_meV_per_rlu: float = 10.6
    q_slope_meV_per_invA: float = 8.0
    t_tolerance_meV: float = 1.0
    flag_threshold: float = 54.0
    shape_family: str = "welsch"
    step_exponent: float = 0.25
    sweep_tol_meV: float = 0.01
    sweep_cap: int = 20
    q_curvature_scale_meV: float = 1.0
    reference_T_K: float = 100.0
    t_spacing_form: str = "widen"
    penalty_cap: float = 12.0
    birth_edge: float = 2.0
    birth_mid: float = 12.0
    cost_rel_tol: float = 1e-5
    collinearity_tol: float = 0.01
    composition_limit: int = 4
    confidence: float = 0.80
    close_call_nats: float = 2.0
    evidence_hysteresis_nats: float = 0.1
    linewidth_ratio: float = 2.0
    full_search: bool = False

    @staticmethod
    def from_values(values: Mapping[str, object]) -> "InterSettings":
        unknown = sorted((k for k in values if k not in _BY_NAME), key=str)
        if unknown:
            raise ValueError(f"unknown inter-spectra setting {unknown[0]!r}")
        return InterSettings(version=SETTINGS_VERSION, **{
            d.name: _checked(d, values[d.name]) if d.name in values else d.default for d in SETTING_DEFS})

    def to_values(self) -> dict[str, object]:
        return {d.name: getattr(self, d.name) for d in SETTING_DEFS}

    # -- the q-change penalty and the width every penalty takes from its neighbours' uncertainty
    def q_slope(self, metric: str | None, where: str) -> float:
        """Tolerated slope of a q link, meV per unit of its own metric: r.l.u. ("hkl") or inverse angstrom ("lattice")."""
        if metric == "hkl":
            return self.q_slope_meV_per_rlu
        if metric == "lattice":
            return self.q_slope_meV_per_invA
        raise ValueError(f"{where}: a q link has no metric ({metric!r}), so no tolerated slope applies")

    def q_tolerance(self, metric: str | None, spacing: float, where: str) -> float:
        """s_dispersion of a q-change, meV: slope(metric) times the link's spacing in that metric."""
        return self.q_slope(metric, where) * abs(float(spacing))

    def t_base(self, dT: float) -> tuple[float, float]:
        """(base meV, d) of a temperature change over |dT| kelvin: widen form (base, 0.0); divide form (t_tolerance, step)."""
        step = abs(float(dT)) / self.reference_T_K
        if self.t_spacing_form == "divide":
            return self.t_tolerance_meV, step
        return self.t_tolerance_meV * step ** self.step_exponent, 0.0

    def widened(self, base: float, spread_sq: float, d: float = 0.0) -> float:
        """s_eff = sqrt(base^2 + (C_eff/2) * spread_sq): spread_sq is the sum of squared neighbour guards.
        C_eff = C/d on the divide arm (its curvature at the centre is C/d), else C."""
        cap = self.penalty_cap / d if d > 0 else self.penalty_cap
        return math.sqrt(base * base + 0.5 * cap * spread_sq)

    def link_tolerance(self, link) -> float:
        """Tolerated energy change across one graph link (duck-typed: kind, step, metric, a, b)."""
        if link.kind == "q":
            return self.q_tolerance(link.metric, link.step, f"{link.a}|{link.b}")
        return self.t_base(link.step)[0]
