"""Qt-free spectrum DTOs shared by the Scan(s) page and fit inspector."""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

import hashlib
import json
import logging
import math
import re
from pathlib import Path
from types import MappingProxyType

import numpy as np

from .scan_list import PooledRow, SpectrumRow, Status
from .evaluate import UnsupportedFitView, _component_manifest, deep_manifest, evaluate_manifest

Scale = Literal["raw", "normalised"]
PassName = Literal["fast", "deep"]
Role = Literal["mode", "elastic", "quasielastic", "background"]

FINE_POINTS = 400
CACHE_SIZE = 64

NOT_PARSED = "not_parsed"
POOLED = "pooled"
NO_ENERGY_ZERO = "no_energy_zero"
NO_MONITOR = "no_monitor"
MONITOR_NOT_STORED = "monitor_not_stored"
NO_PASS = "no_pass"
CURVE_UNAVAILABLE = "curve_unavailable"
DEEP_DOES_NOT_REBUILD = "deep_does_not_rebuild"

OUTDATED_TEXT = "outdated: values changed since fit"
NOT_JUDGED_TEXT = "currentness not recorded"
ENERGY_HEADING = "Energy E₀ (meV, undamped DHO energy)"
LINEWIDTH_HEADING = "Linewidth Γ (meV, DHO damping, HWHM)"
AMPLITUDE_HEADING = "Amplitude h (counts per point at the spectrum's reference exposure)"


@dataclass(frozen=True)
class NotDrawn:
    code: str
    text: str


@dataclass(frozen=True)
class Measured:
    value: float | None
    lo: float | None
    hi: float | None
    kind: str | None


@dataclass(frozen=True)
class ModeRow:
    number: int
    prefix: str
    energy: Measured
    linewidth: Measured
    amplitude: Measured


@dataclass(frozen=True)
class PassSummary:
    pass_name: PassName
    present: bool
    state: str
    state_reason: str | None
    label: str | None
    trusted: bool | None
    trust_reason: str | None
    current: bool | None
    outdated_text: str | None
    unjudged_text: str | None
    changed: tuple[tuple[str, str, str], ...]
    modes: tuple[ModeRow, ...]


@dataclass(frozen=True)
class CurveComponent:
    role: Role
    mode_number: int | None
    name: str
    prefix: str
    values: np.ndarray


@dataclass(frozen=True)
class PassCurves:
    total: np.ndarray
    components: tuple[CurveComponent, ...]
    residuals: np.ndarray


@dataclass(frozen=True)
class ScaledSpectrum:
    key: str
    scale: Scale
    R: float | None
    y_unit: str
    not_drawn: NotDrawn | None
    x: np.ndarray
    y: np.ndarray
    yerr: np.ndarray
    kept: np.ndarray
    fine_x: np.ndarray
    passes: Mapping[PassName, PassCurves | NotDrawn]


@dataclass(frozen=True)
class SpectrumBundle:
    key: str
    revision: str
    not_drawn: NotDrawn | None
    summaries: Mapping[PassName, PassSummary]
    energy: np.ndarray
    counts: np.ndarray
    variance: np.ndarray
    kept: np.ndarray
    monitor: np.ndarray | None
    exposure_stored: bool
    exposure_source: str | None
    exposure_scale: np.ndarray | None
    m_ref: float | None
    fine_energy: np.ndarray
    _passes: Mapping[PassName, PassCurves | NotDrawn] = field(
        default_factory=dict, repr=False, compare=False)

    def scaled(self, scale: Scale, R: float | None = None) -> ScaledSpectrum:
        if scale not in ("raw", "normalised"):
            raise ValueError(f"unknown spectrum scale {scale!r}")
        ref = R if scale == "normalised" else None
        if self.not_drawn is not None:
            passes = {name: (value if isinstance(value, NotDrawn) else self.not_drawn)
                      for name, value in self._passes.items()}
            return _empty_view(self, scale, ref, self.not_drawn, passes)

        if scale == "raw":
            y = self.counts
            yerr = _readonly(np.sqrt(np.clip(self.variance, 0.0, None)))
            curves = {}
            for name, value in self._passes.items():
                if isinstance(value, NotDrawn):
                    curves[name] = value
                else:
                    curves[name] = PassCurves(
                        total=value.total, components=value.components,
                        residuals=value.residuals)
            return ScaledSpectrum(
                self.key, scale, None, "Counts", None, self.energy, y, yerr, self.kept,
                self.fine_energy, MappingProxyType(curves))

        monitor = self.monitor
        if not self.exposure_stored:
            reason = NotDrawn(
                MONITOR_NOT_STORED,
                "monitor not stored for this spectrum yet (the next analysis run stores it)")
            return _empty_view(self, scale, ref, reason, _blocked_passes(self, reason))
        if monitor is None or not np.isfinite(monitor).any():
            reason = NotDrawn(NO_MONITOR, "no monitor recorded")
            return _empty_view(self, scale, ref, reason, _blocked_passes(self, reason))
        if self.exposure_source != "monitor":
            reason = NotDrawn(
                NO_MONITOR, "no usable monitor (no kept point has a positive monitor)")
            return _empty_view(self, scale, ref, reason, _blocked_passes(self, reason))
        drawable = np.isfinite(monitor) & (monitor > 0)
        if (not np.any(drawable & self.kept) or self.m_ref is None
                or not math.isfinite(self.m_ref) or self.m_ref <= 0):
            reason = NotDrawn(
                NO_MONITOR, "no usable monitor (no kept point has a positive monitor)")
            return _empty_view(self, scale, ref, reason, _blocked_passes(self, reason))
        reference = _positive_reference(R)
        drawable, values, errors = normalised_points(
            self.counts, self.variance, monitor, reference)
        factor = reference / self.m_ref
        curves = {}
        for name, value in self._passes.items():
            if isinstance(value, NotDrawn):
                curves[name] = value
            else:
                components = tuple(CurveComponent(
                    role=component.role, mode_number=component.mode_number, name=component.name,
                    prefix=component.prefix, values=_readonly(component.values * factor))
                    for component in value.components)
                residuals = value.residuals[drawable] * (reference / monitor[drawable])
                curves[name] = PassCurves(
                    total=_readonly(value.total * factor), components=components,
                    residuals=_readonly(residuals))
        return ScaledSpectrum(
            self.key, scale, reference, _normalised_unit(reference), None,
            _readonly(self.energy[drawable]), _readonly(values[drawable]),
            _readonly(errors[drawable]), _readonly(self.kept[drawable]),
            self.fine_energy, MappingProxyType(curves))


class SpectraCache:
    """Bounded least-recently-used cache for immutable spectrum bundles."""

    def __init__(self, maxsize: int = CACHE_SIZE):
        self.maxsize = maxsize
        self._cache: OrderedDict[str, SpectrumBundle] = OrderedDict()

    def _get(self, revision):
        bundle = self._cache.get(revision)
        if bundle is not None:
            self._cache.move_to_end(revision)
        return bundle

    def _put(self, revision, bundle):
        if self.maxsize <= 0:
            return
        self._cache[revision] = bundle
        self._cache.move_to_end(revision)
        while len(self._cache) > self.maxsize:
            self._cache.popitem(last=False)


_log = logging.getLogger(__name__)


def _readonly(values, dtype=None):
    array = np.array(values, dtype=dtype, copy=True)
    array.setflags(write=False)
    return array


def _empty(dtype=float):
    return _readonly(np.empty(0, dtype=dtype))


def _positive_reference(value):
    try:
        reference = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("normalisation reference must be finite and positive") from exc
    if not math.isfinite(reference) or reference <= 0:
        raise ValueError("normalisation reference must be finite and positive")
    return reference


def _normalised_unit(reference):
    exponent = math.log10(reference)
    if exponent == round(exponent):
        digits = str(int(round(exponent)))
        superscript = str.maketrans(
            "0123456789-", "\u2070\u00b9\u00b2\u00b3\u2074\u2075\u2076\u2077\u2078\u2079\u207b")
        return f"Counts per 10{digits.translate(superscript)} monitor"
    return f"Counts per {reference:g} monitor"


def nearest_power_of_ten(value: float) -> float:
    """Nearest power of ten in log space; exact half-decade ties round upward."""
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("value must be finite and positive") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError("value must be finite and positive")
    return float(10.0 ** math.floor(math.log10(value) + 0.5))


def normalised_points(counts, variance, monitor, R: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return drawable mask and normalised values/errors for every measured point."""
    reference = _positive_reference(R)
    counts = np.asarray(counts, dtype=float)
    variance = np.asarray(variance, dtype=float)
    if counts.shape != variance.shape:
        raise ValueError("counts and variance must have the same shape")
    if monitor is None:
        return (_readonly(np.zeros(counts.shape, dtype=bool)),
                _readonly(np.full(counts.shape, np.nan)),
                _readonly(np.full(counts.shape, np.nan)))
    monitor = np.asarray(monitor, dtype=float)
    if monitor.shape != counts.shape:
        raise ValueError("monitor must have the same shape as counts")
    drawable = np.isfinite(monitor) & (monitor > 0)
    values = np.full(counts.shape, np.nan)
    errors = np.full(counts.shape, np.nan)
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        factor = reference / monitor[drawable]
        values[drawable] = counts[drawable] * factor
        errors[drawable] = factor * np.sqrt(np.clip(
            variance[drawable] + counts[drawable] ** 2 / monitor[drawable], 0.0, None))
    return _readonly(drawable), _readonly(values), _readonly(errors)


def _natural_prefix(prefix):
    match = re.fullmatch(r"phonon(\d*)", str(prefix))
    return (int(match.group(1) or 1) if match else 0, str(prefix))


def _numeric(value):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        _log.debug("spectrum value %r is not numeric (%s: %s)",
                   value, type(exc).__name__, exc)
        return None
    return number if math.isfinite(number) else None


def _measured(value, error=None, *, bounds=None, kind=None):
    number = _numeric(value)
    if number is None:
        return Measured(None, None, None, None)
    if bounds is not None and kind is not None:
        lo, hi = (_numeric(bounds[0]), _numeric(bounds[1]))
        if lo is not None and hi is not None and lo <= hi:
            return Measured(number, lo, hi, kind)
    uncertainty = _numeric(error)
    if uncertainty is not None and uncertainty >= 0:
        return Measured(number, number - uncertainty, number + uncertainty, "hesse")
    return Measured(number, None, None, None)


def _mode_rows(fit, pass_name):
    if fit is None:
        return ()
    if pass_name == "deep":
        chosen = (fit.deep_pass or {}).get("chosen")
        if not isinstance(chosen, Mapping) or not isinstance(chosen.get("params"), Mapping):
            return ()
        params = chosen["params"]
        errors = chosen.get("errors")
        minos = errors.get("minos") if isinstance(errors, Mapping) else None
        prefixes = {str(name).split(".", 1)[0] for name in params}
        prefixes = [prefix for prefix in prefixes if re.fullmatch(r"phonon\d*", prefix)]
        hesse_modes = ()
    else:
        try:
            manifest = _component_manifest(fit, None)
        except UnsupportedFitView as exc:
            _log.info("fast mode table unavailable for %s: %s", fit.unit_id, exc)
            manifest = None
        has_manifest = isinstance(manifest, Mapping)
        params = manifest.get("parameters", {}) if has_manifest else {}
        if not isinstance(params, Mapping):
            _log.warning("fast mode table unavailable for %s: stored parameters are not an object",
                         fit.unit_id)
            params = {}
            has_manifest = True
        components = manifest.get("components", ()) if has_manifest else ()
        if not isinstance(components, (list, tuple)):
            _log.warning("fast mode table unavailable for %s: stored components are not a list",
                         fit.unit_id)
            components = ()
            has_manifest = True
        prefixes = ([str(item.get("prefix")) for item in components
                     if isinstance(item, Mapping) and item.get("type") == "phonon_dho"
                     and isinstance(item.get("prefix"), str)
                     and re.fullmatch(r"phonon\d*", item["prefix"])]
                    if has_manifest else [])
        hesse_modes = tuple(fit.modes or ())

    if pass_name == "deep":
        mode_defs = [(prefix, _numeric(params.get(f"{prefix}.omega0"))) for prefix in prefixes]
    elif prefixes or has_manifest:
        mode_defs = [(prefix, _numeric(params.get(f"{prefix}.omega0"))) for prefix in prefixes]
    else:
        fallback = sorted((mode for mode in (fit.modes or ()) if isinstance(mode, Mapping)),
                          key=lambda mode: (_numeric(mode.get("omega0"))
                                           if _numeric(mode.get("omega0")) is not None else math.inf))
        mode_defs = []
        for index, mode in enumerate(fallback):
            prefix = "phonon" if index == 0 else f"phonon{index + 1}"
            params = dict(params)
            params[f"{prefix}.omega0"] = mode.get("omega0")
            params[f"{prefix}.Gamma"] = mode.get("gamma")
            mode_defs.append((prefix, _numeric(mode.get("omega0"))))
        hesse_modes = tuple(fallback)

    mode_defs.sort(key=lambda item: (
        item[1] if item[1] is not None else math.inf, _natural_prefix(item[0])))
    used_hesse = set()
    rows = []
    for number, (prefix, energy_value) in enumerate(mode_defs, 1):
        hesse = None
        if pass_name == "fast":
            for index, candidate in enumerate(hesse_modes):
                if index in used_hesse or not isinstance(candidate, Mapping):
                    continue
                if _numeric(candidate.get("omega0")) == energy_value:
                    used_hesse.add(index)
                    hesse = candidate
                    break
        if pass_name == "deep":
            energy_bounds = _minos_bounds(minos, f"{prefix}.omega0")
            linewidth_bounds = _minos_bounds(minos, f"{prefix}.Gamma")
            energy = _measured(params.get(f"{prefix}.omega0"), bounds=energy_bounds,
                               kind="minos" if energy_bounds else None)
            linewidth = _measured(params.get(f"{prefix}.Gamma"), bounds=linewidth_bounds,
                                   kind="minos" if linewidth_bounds else None)
        else:
            energy = _measured(params.get(f"{prefix}.omega0"),
                               hesse.get("omega0_err") if hesse else None)
            linewidth = _measured(params.get(f"{prefix}.Gamma"),
                                  hesse.get("gamma_err") if hesse else None)
        rows.append(ModeRow(
            number, prefix, energy, linewidth, _measured(params.get(f"{prefix}.h"))))
    return tuple(rows)


def _minos_bounds(minos, name):
    if not isinstance(minos, Mapping):
        return None
    bounds = minos.get(name)
    if not isinstance(bounds, (list, tuple)) or len(bounds) < 3 or bounds[2] is not True:
        return None
    lo, hi = _numeric(bounds[0]), _numeric(bounds[1])
    return (lo, hi) if lo is not None and hi is not None else None


def _summary(row, pass_name, fit):
    status = row.fast if pass_name == "fast" else row.deep
    record = ((fit.fast_pass or {}) if fit is not None and pass_name == "fast"
              else (fit.deep_pass or {}) if fit is not None else {})
    chosen = record.get("chosen") if isinstance(record, Mapping) else None
    label = (fit.best_label if pass_name == "fast" and fit is not None
             else chosen.get("label") if isinstance(chosen, Mapping) else None)
    trusted = record.get("trusted") if pass_name == "deep" and isinstance(record, Mapping) else None
    trusted = trusted if isinstance(trusted, bool) else None
    current = status.current
    changed = tuple(status.changed)
    if pass_name == "deep" and fit is not None:
        deep_arena = (fit.deep_pass or {}).get("arena_fingerprint")
        fast_arena = (fit.fast_pass or {}).get("arena_fingerprint")
        if deep_arena is not None and fast_arena is not None and deep_arena != fast_arena:
            current = False
            arena_change = ("data", f"deep pass arena {str(deep_arena)[:8]}",
                            f"fast pass arena {str(fast_arena)[:8]}")
            if arena_change not in changed:
                changed += (arena_change,)
    reason = status.reason
    trust_reason = None
    if (pass_name == "deep" and trusted is False and status.word != "refused" and reason):
        trust_reason = reason.rsplit("; ", 1)[-1].strip()
    return PassSummary(
        pass_name=pass_name, present=bool(status.stored), state=status.word,
        state_reason=reason, label=label, trusted=trusted, trust_reason=trust_reason,
        current=current, outdated_text=OUTDATED_TEXT if current is False else None,
        unjudged_text=(NOT_JUDGED_TEXT if pass_name == "deep" and status.stored
                       and current is None else None),
        changed=changed, modes=_mode_rows(fit, pass_name))


def _summaries(row, fit):
    return MappingProxyType({
        "fast": _summary(row, "fast", fit),
        "deep": _summary(row, "deep", fit),
    })


def _empty_view(bundle, scale, reference, reason, passes):
    reference_value = _numeric(reference)
    return ScaledSpectrum(
        key=bundle.key, scale=scale, R=reference,
        y_unit="Counts" if scale == "raw" else (
            _normalised_unit(reference_value) if reference_value is not None and reference_value > 0
            else "Counts per monitor"),
        not_drawn=reason, x=_empty(), y=_empty(), yerr=_empty(), kept=_empty(bool),
        fine_x=_empty(), passes=MappingProxyType(dict(passes)))


def _blocked_passes(bundle, reason):
    return {name: value if isinstance(value, NotDrawn) else reason
            for name, value in bundle._passes.items()}


def _resolution_token(fit):
    if fit is None:
        return "-"
    calib = fit.calib or {}
    ref = calib.get("tabulated")
    if isinstance(ref, Mapping):
        try:
            path = Path(ref["folder"]) / ref["file"]
            from isar.analyze.resolution import file_stamp
            stamp = file_stamp(path)
        except (KeyError, TypeError, OSError) as exc:
            _log.info("resolution curve stamp unavailable for %s (%s: %s)",
                      fit.unit_id, type(exc).__name__, exc)
            return repr((str(ref.get("folder", "")), str(ref.get("file", "")),
                         "unreadable", str(exc)))
        return repr((str(path), stamp))
    try:
        context = fit.resolution_context()
    except Exception as exc:
        _log.info("resolution context unavailable for %s (%s: %s)",
                  fit.unit_id, type(exc).__name__, exc)
        return repr(("unreadable", type(exc).__name__, str(exc)))
    if not isinstance(context, Mapping):
        return "-"
    return repr(tuple(context.get(key) for key in (
        "center", "gauss_fwhm", "lorentz_fwhm", "T")))


def _revision(row, unit):
    digest = hashlib.sha256()
    digest.update(str(row.key).encode("utf-8"))
    for name in ("x", "counts", "variance", "mask", "monitor"):
        value = getattr(unit, name, None)
        if value is None:
            digest.update(b"-")
            continue
        array = np.ascontiguousarray(value)
        digest.update(array.dtype.str.encode("ascii"))
        digest.update(repr(array.shape).encode("ascii"))
        digest.update(array.tobytes())
    exposure = getattr(unit, "exposure", None)
    exposure_payload = None if exposure is None else {
        "source": getattr(exposure, "source", None),
        "reference": getattr(exposure, "reference", None),
        "scale": (None if getattr(exposure, "scale", None) is None
                  else np.asarray(exposure.scale).tolist()),
    }
    digest.update(json.dumps(
        [getattr(unit, "axis", None), exposure_payload,
         getattr(unit, "exposure_stored", exposure is not None)],
        sort_keys=True, default=repr).encode("utf-8"))
    fit = row.fit
    digest.update((fit.stored_revision() if fit is not None else "-").encode("ascii"))
    digest.update(_resolution_token(fit).encode("utf-8"))
    digest.update(repr(row.fast).encode("utf-8"))
    digest.update(repr(row.deep).encode("utf-8"))
    return digest.hexdigest()[:16]


def _not_drawn_bundle(key, revision, reason, summaries, *, unit=None, passes=None):
    empty = _empty()
    if unit is None:
        energy = counts = variance = kept = empty
        monitor = exposure_scale = None
        exposure_stored = False
        exposure_source = m_ref = None
    else:
        counts = _readonly(unit.counts)
        variance = _readonly(unit.variance)
        kept = _readonly(unit.mask, bool)
        monitor = None if unit.monitor is None else _readonly(unit.monitor)
        exposure = unit.exposure
        exposure_stored = bool(unit.exposure_stored)
        exposure_source = getattr(exposure, "source", None) if exposure is not None else None
        exposure_scale = (None if exposure is None or exposure.scale is None
                          else _readonly(exposure.scale))
        m_ref = (getattr(exposure, "reference", None)
                 if exposure is not None and exposure_source == "monitor" else None)
        energy = empty
    return SpectrumBundle(
        key=key, revision=revision, not_drawn=reason, summaries=summaries,
        energy=energy, counts=counts, variance=variance, kept=kept, monitor=monitor,
        exposure_stored=exposure_stored, exposure_source=exposure_source,
        exposure_scale=exposure_scale, m_ref=m_ref, fine_energy=empty,
        _passes=MappingProxyType(dict(passes or {
            "fast": reason, "deep": reason})))


def _pass_not_drawn(row, pass_name, fit, resolution, resolution_error, fine_raw, raw_x,
                    counts, exposure_scale, qe):
    if fit is None:
        return NotDrawn(NO_PASS, "no fast pass" if pass_name == "fast" else "no deep pass")
    status = row.fast if pass_name == "fast" else row.deep
    if pass_name == "fast":
        try:
            manifest = _component_manifest(fit, qe)
        except UnsupportedFitView as exc:
            _log.info("fast curve unavailable for %s: %s", row.key, exc)
            return NotDrawn(CURVE_UNAVAILABLE, f"fast curve unavailable ({exc})")
        if manifest is None:
            return NotDrawn(CURVE_UNAVAILABLE, "fast curve unavailable (no stored model parameters)")
    else:
        record = fit.deep_pass or {}
        if not record:
            return NotDrawn(NO_PASS, "no deep pass")
        if status.word == "refused":
            reason = status.reason or record.get("refuse") or "refused"
            return NotDrawn(NO_PASS, f"deep pass refused: {reason}")
        chosen = record.get("chosen")
        if not isinstance(chosen, Mapping) or not isinstance(chosen.get("params"), Mapping):
            why = record.get("errors_failure") or "no chosen model"
            return NotDrawn(NO_PASS, f"deep pass stored no fit: {why}")
        try:
            manifest = deep_manifest(fit)
        except UnsupportedFitView as exc:
            _log.info("deep curve unavailable for %s: %s", row.key, exc)
            return NotDrawn(
                DEEP_DOES_NOT_REBUILD,
                f"deep curve not recoverable from the stored fit ({exc})")
    if resolution_error is not None:
        return NotDrawn(
            CURVE_UNAVAILABLE,
            f"{pass_name} curve unavailable ({type(resolution_error).__name__}: {resolution_error})")
    if resolution is None:
        return NotDrawn(CURVE_UNAVAILABLE, f"{pass_name} curve unavailable (no resolution context)")
    try:
        total_fine, parts_fine = evaluate_manifest(manifest, resolution, fine_raw, qe)
        total_points, _parts_points = evaluate_manifest(manifest, resolution, raw_x, qe)
        parameters = manifest.get("parameters") or {}
        if not isinstance(parameters, Mapping):
            raise UnsupportedFitView("stored model parameters are not an object")
        parts_by_prefix = {prefix: values for prefix, values in parts_fine}
        component_defs = []
        for item in manifest.get("components") or ():
            if not isinstance(item, Mapping):
                raise UnsupportedFitView("stored model component is not an object")
            prefix = item.get("prefix")
            kind = item.get("type")
            roles = {
                "phonon_dho": ("mode", None, f"Mode {prefix}"),
                "elastic": ("elastic", None, "Elastic"),
                "quasielastic": ("quasielastic", None, "Quasielastic"),
                "constant_background": ("background", None, "Background"),
            }
            if kind not in roles or prefix not in parts_by_prefix:
                raise UnsupportedFitView(f"stored component {prefix!r} cannot be drawn")
            role, _unused, name = roles[kind]
            energy = _numeric(parameters.get(f"{prefix}.omega0")) if role == "mode" else None
            component_defs.append((role, prefix, energy, name))
        modes = sorted((item for item in component_defs if item[0] == "mode"),
                       key=lambda item: (item[2] if item[2] is not None else math.inf,
                                         _natural_prefix(item[1])))
        ordered = modes + [item for role in ("elastic", "quasielastic", "background")
                           for item in component_defs if item[0] == role]
        components = []
        mode_number = 0
        for role, prefix, _energy, name in ordered:
            number = None
            if role == "mode":
                mode_number += 1
                number = mode_number
                name = f"Mode {number}"
            components.append(CurveComponent(
                role=role, mode_number=number, name=name, prefix=prefix,
                values=_readonly(parts_by_prefix[prefix])))
        scale_values = None if exposure_scale is None else exposure_scale
        mean = total_points if scale_values is None else total_points * scale_values
        residuals = counts - mean
        return PassCurves(
            total=_readonly(total_fine), components=tuple(components),
            residuals=_readonly(residuals))
    except UnsupportedFitView as exc:
        _log.info("%s curve unavailable for %s: %s", pass_name, row.key, exc)
        if pass_name == "deep":
            return NotDrawn(
                DEEP_DOES_NOT_REBUILD,
                f"deep curve not recoverable from the stored fit ({exc})")
        return NotDrawn(CURVE_UNAVAILABLE, f"fast curve unavailable ({exc})")
    except Exception as exc:
        _log.warning("%s curve unavailable for %s (%s: %s)",
                     pass_name, row.key, type(exc).__name__, exc)
        return NotDrawn(
            CURVE_UNAVAILABLE,
            f"{pass_name} curve unavailable ({type(exc).__name__}: {exc})")


def _bundle(ws, row, qe, cache):
    fit = row.fit
    summaries = _summaries(row, fit)
    try:
        unit = ws.unit_data(row.key)
    except Exception as exc:
        _log.warning("unit data read failed for %s (%s: %s)",
                     row.key, type(exc).__name__, exc)
        unit = None
    if unit is None:
        return _not_drawn_bundle(
            row.key, "-", NotDrawn(NOT_PARSED, "not parsed or failed"), summaries)
    revision = _revision(row, unit)
    if cache is not None and (hit := cache._get(revision)) is not None:
        return hit
    try:
        raw_x = np.asarray(unit.x, dtype=float)
        counts = np.asarray(unit.counts, dtype=float)
        variance = np.asarray(unit.variance, dtype=float)
        mask = np.asarray(unit.mask, dtype=bool)
        if (raw_x.ndim != 1 or counts.ndim != 1 or variance.shape != counts.shape
                or mask.shape != counts.shape or raw_x.shape != counts.shape):
            raise ValueError("unit arrays are not aligned one-dimensional vectors")
        finite_x = np.isfinite(raw_x)
        if not finite_x.any():
            raise ValueError("the spectrum has no finite abscissa")
    except Exception as exc:
        _log.warning("invalid unit data for %s (%s: %s)",
                     row.key, type(exc).__name__, exc)
        return _not_drawn_bundle(
            row.key, revision, NotDrawn(NOT_PARSED, "not parsed or failed"), summaries,
            unit=unit)

    axis = unit.axis if isinstance(unit.axis, Mapping) else {}
    status = axis.get("calibration_status")
    calib = fit.calib or {} if fit is not None else {}
    zero_source = calib.get("zero_source")
    zero = fit.energy_zero if fit is not None else None
    shift = None
    if status == "absolute":
        shift = zero if zero is not None and zero_source is not None else 0.0
    elif (status == "needs_zero" and zero is not None
          and zero_source in ("elastic_fit", "pipeline_zero")):
        shift = zero
    if shift is None:
        return _not_drawn_bundle(
            row.key, revision, NotDrawn(NO_ENERGY_ZERO, "no energy zero yet"), summaries,
            unit=unit)

    energy = _readonly(raw_x - shift)
    fine_raw = np.linspace(float(np.min(raw_x[finite_x])), float(np.max(raw_x[finite_x])),
                           FINE_POINTS)
    fine_energy = _readonly(fine_raw - shift)
    monitor = None if unit.monitor is None else _readonly(unit.monitor)
    exposure = unit.exposure
    exposure_scale = (None if exposure is None or exposure.scale is None
                      else _readonly(exposure.scale))
    if exposure_scale is not None and exposure_scale.shape != counts.shape:
        _log.warning("invalid exposure scale for %s; ignoring it", row.key)
        exposure_scale = None
    kept = mask if exposure_scale is None else mask & (exposure_scale > 0)
    stored = bool(unit.exposure_stored)
    source = getattr(exposure, "source", None) if exposure is not None else None
    m_ref = (getattr(exposure, "reference", None)
             if exposure is not None and source == "monitor" else None)
    resolution = None
    resolution_error = None
    if fit is not None:
        try:
            resolution = fit.resolution_context()
        except Exception as exc:
            _log.warning("resolution context unavailable for %s (%s: %s)",
                         row.key, type(exc).__name__, exc)
            resolution_error = exc
    passes = {}
    for pass_name in ("fast", "deep"):
        passes[pass_name] = _pass_not_drawn(
            row, pass_name, fit, resolution, resolution_error, fine_raw, raw_x,
            counts, exposure_scale, qe)
    bundle = SpectrumBundle(
        key=row.key, revision=revision, not_drawn=None, summaries=summaries,
        energy=energy, counts=_readonly(counts), variance=_readonly(variance),
        kept=_readonly(kept, bool), monitor=monitor, exposure_stored=stored,
        exposure_source=source, exposure_scale=exposure_scale, m_ref=m_ref,
        fine_energy=fine_energy, _passes=MappingProxyType(passes))
    if cache is not None:
        cache._put(revision, bundle)
    return bundle


def spectrum_bundles(ws, rows: Sequence[SpectrumRow | PooledRow], *,
                     cache: SpectraCache | None = None) -> tuple[SpectrumBundle, ...]:
    qe = getattr(getattr(ws.card, "policy", None), "quasielastic_fwhm", None)
    out = []
    for row in rows:
        if isinstance(row, PooledRow):
            summaries = _summaries(row, row.fit)
            out.append(_not_drawn_bundle(
                row.key, "-", NotDrawn(POOLED, "pooled master"), summaries))
        elif isinstance(row, SpectrumRow):
            out.append(_bundle(ws, row, qe, cache))
        else:
            raise ValueError("rows must contain only SpectrumRow or PooledRow")
    return tuple(out)


def normalisation_reference(ws, keys: Iterable[str]) -> float | None:
    monitors = []
    for key in keys:
        unit = ws.unit_data(key)
        if unit is None or unit.monitor is None:
            continue
        monitor = np.asarray(unit.monitor, dtype=float)
        mask = np.asarray(unit.mask, dtype=bool)
        if monitor.shape != mask.shape:
            _log.warning("monitor and mask are not aligned for %s", key)
            continue
        exposure = getattr(unit, "exposure", None)
        scale = getattr(exposure, "scale", None) if exposure is not None else None
        kept = mask if scale is None else mask & (np.asarray(scale, dtype=float) > 0)
        valid = kept & np.isfinite(monitor) & (monitor > 0)
        if valid.any():
            monitors.extend(monitor[valid].tolist())
    return nearest_power_of_ten(float(np.median(monitors))) if monitors else None
