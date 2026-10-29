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
    _row("q_tolerance_meV", "front", "float", 2.0, "meV", 0, 50,
         "Anchored batches: the energy change along q that costs nothing before any penalty applies."),
    _row("t_tolerance_meV", "front", "float", 1.0, "meV", 0, 50,
         "Temperature neighbours: the energy change that costs little at the reference temperature step.",
         strict=True, cal=True),
    _row("flag_threshold", "front", "float", 54.0, None, 0, 1000,
         "A spectrum is flagged for review when its flag score reaches this value.", strict=True, cal=True),
    _row("shape_family", "advanced", "choice", "welsch", None, None, None,
         "The shape of the penalty: welsch (bounded, the default) or quartic (for comparison).",
         choices=("welsch", "quartic")),
    _row("step_exponent", "advanced", "float", 0.25, None, 0, 2,
         "How much the tolerated temperature change widens with the size of the temperature step.", cal=True),
    _row("sweep_tol_meV", "advanced", "float", 0.01, "meV", 0, 1,
         "A sweep that moves no mode energy by more than this is quiet; two quiet sweeps settle a batch.",
         strict=True),
    _row("sweep_cap", "advanced", "int", 20, "sweeps", 2, 1000,
         "A simultaneous batch that has not settled after this many sweeps is kept and flagged as did not settle."),
    _row("q_change_scale_meV", "hidden", "float", 1.0, "meV", 0, 50,
         "Anchored q changes: the energy scale of the penalty beyond the zero zone.", strict=True),
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
    _row("collinearity_tol", "hidden", "float", 1e-6, None, 0, 1,
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
    q_tolerance_meV: float = 2.0
    t_tolerance_meV: float = 1.0
    flag_threshold: float = 54.0
    shape_family: str = "welsch"
    step_exponent: float = 0.25
    sweep_tol_meV: float = 0.01
    sweep_cap: int = 20
    q_change_scale_meV: float = 1.0
    q_curvature_scale_meV: float = 1.0
    reference_T_K: float = 100.0
    t_spacing_form: str = "widen"
    penalty_cap: float = 12.0
    birth_edge: float = 2.0
    birth_mid: float = 12.0
    cost_rel_tol: float = 1e-5
    collinearity_tol: float = 1e-6
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
