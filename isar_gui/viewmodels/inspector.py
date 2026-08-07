"""InspectorViewModel — view-ready state for the C2 fit inspector (Task 10, A-4).

The flagship read-side view: given the :class:`~isar.workspace_api.FitView` the browser selected, it
assembles everything the inspector widget draws — the data+fit overlay spec, the residuals spec, the
interval-ladder rows (labeled by tier, one certifying), the AIC candidate rows, and the
verdict/provenance dicts. ALL derivation lives here; the widget only renders these outputs.

Interval-ladder derivation follows ``FitOutcome`` (isar/session/outcome.py) exactly:

* bootstrap ``interval`` is authoritative and certifies **when** ``interval_reliable`` and present;
* else the profile-likelihood ``omega0_profile`` certifies when present;
* else the HESSE ``hesse_interval`` certifies **only** when ``hesse_trusted``;
* the bayes posterior is **diagnostics-only** — it appears on the ladder but NEVER certifies.

``interval_reliable=False`` renders the bootstrap row as ``"(unreliable)"`` — never numbers.

Qt import surface: PySide6 only; the specs come from ``isar.plotspec.builders`` (plain-data builders,
no matplotlib). Curve/data come through the read-only ``Workspace`` facade (``evaluate_model`` +
``scan_data``); this VM never re-fits or writes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PySide6.QtCore import Signal

from isar.plotspec import builders
from isar.plotspec import spec as ps
from isar.workspace_api import resolve_interval
from isar_gui.scan_keys import scan_prefix, split_key
from isar_gui.viewmodels import certify
from isar_gui.viewmodels.base import ViewModel
# Re-exported for backwards compatibility: the ladder derivation now lives in certify.py (shared with
# the heatmap/dispersion views), but LadderRow's public location historically was this module.
from isar_gui.viewmodels.certify import LadderRow  # noqa: F401

_FINE_N = 400


def _fmt_q(hkl) -> str:
    """A ``[h, k, l]`` display string, or ``""`` for a missing/non-numeric q."""
    if not hkl:
        return ""
    try:
        return "[" + ", ".join(f"{float(v):.3g}" for v in hkl) + "]"
    except (TypeError, ValueError):
        return ""


@dataclass
class CandidateRow:
    label: str
    delta_aic: float | None
    weight: float | None
    n_modes: int | None
    winner: bool = False


@dataclass
class InspectorState:
    """The whole assembled view-state (empty when no fit/unit is selected).

    ``kind`` is ``"fit"`` for a fitted-detector view (interval ladder / candidates / verdict) and
    ``"unit"`` for a raw parsed *scan × analyzer* unit (metadata panel + raw counts plot, no fit
    tables). ``metadata`` carries the unit-mode provenance panel (empty in fit mode)."""

    empty: bool = True
    kind: str = "fit"                # "fit" | "unit"
    main_spec: object | None = None
    residuals_spec: object | None = None
    ladder_rows: list = field(default_factory=list)
    candidate_rows: list = field(default_factory=list)
    candidates_recorded: bool = False
    verdict: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)   # unit-mode scan metadata (label -> display string)
    certifying_tier: str | None = None
    curve_error: str | None = None   # set when the specs could not be built (tables still shown)


class InspectorViewModel(ViewModel):
    """Assembles the C2 inspector view-state from a selected ``FitView``; emits ``spec_changed``."""

    #: Emitted after :meth:`set_fit` rebuilds the state (including the empty-state reset).
    spec_changed = Signal()

    def __init__(self, ws, parent=None):
        super().__init__(ws, parent)
        self._fit = None
        self._unit_key = None
        self._state = InspectorState()

    # ---- public state -----------------------------------------------------
    @property
    def state(self) -> InspectorState:
        return self._state

    @property
    def is_empty(self) -> bool:
        return self._state.empty

    @property
    def current_fit(self):
        """The currently-inspected ``FitView`` (``None`` in the empty/unit state). Read by the window's
        watch slot to re-look-up the fit by identity after a workspace change."""
        return self._fit

    @property
    def current_unit(self):
        """The currently-inspected unit key (``None`` unless a raw unit is shown). Read by the window's
        watch slot to re-assemble the unit view after a workspace change (a unit key IS stable)."""
        return self._unit_key

    # ---- selection slot ---------------------------------------------------
    def set_fit(self, fit_view) -> None:
        """Slot wired to ``BrowserViewModel.selection_changed``. ``None`` resets to the empty state."""
        self._fit = fit_view
        self._unit_key = None
        self._state = self._build(fit_view)
        self.spec_changed.emit()

    def set_unit(self, unit_key) -> None:
        """Show a raw parsed *scan × analyzer* unit: metadata panel + a data-only counts plot with an
        honest 3-state axis label (hard rule 4 — never labels uncalibrated x as energy transfer, never
        shifts x). ``None`` resets to the empty state. Missing unit file -> a curve_error, no crash."""
        self._fit = None
        self._unit_key = unit_key
        self._state = self._build_unit(unit_key)
        self.spec_changed.emit()

    def on_workspace_changed(self, changed=None) -> None:
        # Re-assemble against the refreshed facade if a fit or unit is selected, then re-emit.
        if self._unit_key is not None:
            self._state = self._build_unit(self._unit_key)
        elif self._fit is not None:
            self._state = self._build(self._fit)
        self.spec_changed.emit()
        super().on_workspace_changed(changed)

    # ---- assembly ---------------------------------------------------------
    def _build(self, fit) -> InspectorState:
        if fit is None:
            return InspectorState(empty=True)
        ladder = certify.ladder_rows(fit)
        certifying = next((r.tier for r in ladder if r.certifying), None)
        cand_rows, recorded = self._candidate_rows(fit)
        st = InspectorState(
            empty=False,
            ladder_rows=ladder,
            candidate_rows=cand_rows,
            candidates_recorded=recorded,
            verdict=self._verdict(fit),
            provenance=self._provenance(fit),
            certifying_tier=certifying,
        )
        interval, _assessment = resolve_interval(
            fit, getattr(self._ws, "master_assessments", ()))
        if interval is not None:
            st.provenance["interval_source"] = interval.source
            st.provenance["interval_method"] = interval.method or "unknown"
            st.provenance["interval_endpoints"] = f"[{interval.lo:.6g}, {interval.hi:.6g}] meV"
        self._attach_specs(fit, st)
        return st

    # -- unit (raw parsed scan x analyzer) assembly ------------------------
    def _build_unit(self, unit_key) -> InspectorState:
        if unit_key is None:
            return InspectorState(empty=True, kind="unit")
        _exp, scan_number, det = split_key(unit_key)
        sv = self._find_scan_view(unit_key)
        # A ScanView's scan_id key carries no detector when a single-detector legacy entry split it
        # off; recover the analyzer from the view's sole detector so the title/metadata still name it.
        if det is None and sv is not None:
            det = next(iter(getattr(sv, "detectors", None) or {}), None)
        st = InspectorState(empty=False, kind="unit",
                            metadata=self._unit_metadata(sv, unit_key, scan_number, det))
        try:
            unit = self._ws.unit_data(unit_key)
        except (KeyError, ValueError, OSError) as exc:  # a read surface should not raise; guard anyway
            st.curve_error = str(exc)
            return st
        if unit is None:
            st.curve_error = "no parsed counts for this unit (re-ingest to backfill)"
            return st
        axis = dict(getattr(unit, "axis", None) or {})
        try:
            st.main_spec = builders.fit_overlay_spec(
                x=unit.x, counts=unit.counts, yerr=unit.yerr, mask=unit.mask,
                x_fit=None, total=None, components=None,
                scan_number=scan_number, det_id=det,
                title=self._unit_title(scan_number, det),
                xlabel=self._axis_label(axis))
        except ValueError as exc:
            st.curve_error = f"invalid parsed unit data: {exc}"
        return st

    def _find_scan_view(self, unit_key):
        pref = scan_prefix(unit_key)
        try:
            scans = list(getattr(self._ws, "scans", None) or [])
        except Exception:  # noqa: BLE001 — the facade degrades; metadata is best-effort
            return None
        for sv in scans:
            if getattr(sv, "scan_id", None) == unit_key:
                return sv
        for sv in scans:  # legacy: a scan-level key covers the sole unit of that scan
            if scan_prefix(getattr(sv, "scan_id", "") or "") == pref:
                return sv
        return None

    @staticmethod
    def _unit_title(scan_number, det) -> str:
        num = "?" if scan_number is None else scan_number
        base = f"scan {num}" if det is None else f"scan {num}  {det}"
        return f"{base}  (parsed, not fitted)"

    @staticmethod
    def _axis_label(axis) -> str:
        """Rule-4-honest x-axis label for a raw unit. The x values are the RAW scanned abscissa (never
        energy-zero-shifted), so a non-``absolute`` calibration is NEVER labeled "energy transfer" and
        NEVER implies E0=0 — the label says out loud that the zero is pending / conversion is needed."""
        axis = axis or {}
        status = axis.get("calibration_status")
        units = axis.get("units") or "meV"
        kind = axis.get("raw_abscissa_kind") or axis.get("kind") or "scan axis"
        if status == "absolute":
            return f"energy transfer ({units})"
        if status == "needs_zero":
            return f"raw energy ({units}) — uncalibrated: elastic zero pending"
        if status == "needs_conversion_and_zero":
            return f"raw {kind} ({units}) — uncalibrated"
        return "scan axis (uncalibrated?)"

    @staticmethod
    def _unit_metadata(sv, unit_key, scan_number, det) -> dict:
        """Ordered display metadata for the unit panel, tolerant of pre-Phase-2 workspaces (every key
        is best-effort; an absent provenance field is simply omitted, never a placeholder)."""
        md: dict = {"unit": str(unit_key)}
        if sv is None:
            return md
        prov = getattr(sv, "provenance", None) or {}
        scan_type = getattr(sv, "scan_type", None)
        if scan_type:
            md["type"] = str(scan_type)
        status = getattr(sv, "status", None)
        if status:
            md["status"] = str(status)
        if det:
            md["analyzer"] = str(det)
        hkl = getattr(sv, "hkl", None)
        q = _fmt_q(hkl)
        if q:
            md["q"] = q
        qt = _fmt_q(prov.get("q_target"))
        if qt:
            md["target #Q"] = qt
        t = getattr(sv, "T", None)
        if t is not None:
            md["T"] = f"{float(t):g} K"
        if prov.get("command"):
            md["command"] = str(prov["command"])
        if prov.get("counting_mode"):
            md["counting mode"] = str(prov["counting_mode"])
        if prov.get("preset") is not None:
            try:
                md["preset"] = f"{float(prov['preset']):g}"
            except (TypeError, ValueError):
                pass
        if prov.get("n_points") is not None:
            md["points"] = str(prov["n_points"])
        if prov.get("energy_status"):
            md["energy status"] = str(prov["energy_status"])
        b = prov.get("B")
        if b is not None:
            try:
                md["B"] = f"{float(b):g} T"
            except (TypeError, ValueError):
                pass
        src = getattr(sv, "source_file", None)
        if src:
            md["file"] = Path(src).name
        return md

    # -- specs (data + fit + residuals) --
    def _attach_specs(self, fit, st: InspectorState) -> None:
        ws = self._ws
        pooled = bool(fit.pooled)
        try:
            if pooled:
                pooled_data = ws.pooled_spectrum(fit)
                if pooled_data is None:  # legacy workspace: honest curve-only fallback below
                    energy = counts = yerr = mask = axis = None
                else:
                    energy, counts, yerr = pooled_data.x, pooled_data.counts, pooled_data.yerr
                    mask, axis = pooled_data.mask, None
            else:
                unit = ws.unit_data(fit.unit_id) if fit.unit_id else None
                if unit is not None:
                    energy, counts, yerr = unit.x, unit.counts, unit.yerr
                    mask, axis = unit.mask, dict(getattr(unit, "axis", None) or {})
                else:
                    # Legacy workspaces have no per-unit ingest data. Their drive data are absolute
                    # TAVI energies, so retain the existing scan-number fallback only in this case.
                    energy, counts, yerr = ws.scan_data(fit.scan_number)
                    mask, axis = None, None
            # The delivered model is in calibrated energy-transfer coordinates. A raw scan axis
            # (including the explicit 3-state "needs zero" forms) must never be used as its input:
            # it would produce a plausible-looking but physically false curve. Keep the measured
            # points visible, including QC-masked ones, and state why the overlay is withheld.
            if axis is not None and axis.get("calibration_status") != "absolute":
                try:
                    st.main_spec = builders.fit_overlay_spec(
                        x=energy, counts=counts, yerr=yerr, mask=mask,
                        x_fit=None, total=None, components=None,
                        scan_number=fit.scan_number, det_id=fit.detector_id, q=fit.q,
                        xlabel=self._axis_label(axis))
                    st.main_spec.annotations.append(ps.Annotation(
                        text="fit curve withheld — calibration pending; raw counts shown", xy=None,
                        role=ps.State.UNRELIABLE))
                    st.curve_error = "fit overlay withheld until the scan axis is calibrated"
                except ValueError as exc:
                    st.curve_error = f"invalid fitted unit data: {exc}"
                return
            fine = self._fine_grid(fit, energy)
            if fine is None:
                st.curve_error = "no energy grid available for this scan"
                return
            try:
                curve = ws.evaluate_fit(fit, x_eval=fine)
            except (KeyError, ValueError, OSError) as exc:
                if energy is not None and energy.size:
                    st.main_spec = builders.fit_overlay_spec(
                        x=energy, counts=counts, yerr=yerr, mask=mask,
                        x_fit=None, total=None, components=None,
                        scan_number=fit.scan_number, det_id=fit.detector_id, q=fit.q,
                        xlabel=self._axis_label(axis) if axis else "energy transfer (meV)")
                    st.main_spec.annotations.append(ps.Annotation(
                        text="model curve unavailable — raw counts shown", xy=None,
                        role=ps.State.UNRELIABLE))
                    st.curve_error = f"model curve unavailable; raw counts shown ({exc})"
                else:
                    st.curve_error = f"model curve unavailable ({exc})"
                return
        except (KeyError, ValueError, OSError) as exc:
            st.curve_error = str(exc)
            return

        # The ω₀ whisker in the plot shows the CERTIFYING tier's interval (None => warning marker),
        # not bootstrap-regardless — so an uncertified ω₀ never draws a numeric whisker.
        display_interval, assessment = resolve_interval(
            fit, getattr(ws, "master_assessments", ()))
        cert_interval = (None if display_interval is None else
                         (display_interval.lo, display_interval.hi))
        display_omega0 = fit.omega0
        if assessment is not None and getattr(assessment, "omega0", None) is not None:
            display_omega0 = assessment.omega0

        # Legacy pooled fits remain curve-only; new fits use the exact persisted merged spectrum.
        data_x, data_counts, data_yerr, data_mask = energy, counts, yerr, mask
        xlabel = self._axis_label(axis) if axis else "energy transfer (meV)"
        try:
            st.main_spec = builders.fit_overlay_spec(
                x=data_x, counts=data_counts, yerr=data_yerr, mask=data_mask,
                x_fit=curve.x, total=curve.total, components=curve.components,
                omega0=display_omega0, interval=cert_interval,
                interval_reliable=(cert_interval is not None),
                scan_number=fit.scan_number, det_id=fit.detector_id, q=fit.q,
                pooled=pooled, members=fit.members, n_members=fit.n_members,
                pooled_data=bool(pooled and fit.spectrum_ref and energy is not None),
                xlabel=xlabel,
            )
        except ValueError as exc:
            st.curve_error = f"invalid fitted unit data: {exc}"
            return
        if energy is not None and energy.size:
            total_on_data = np.interp(energy, np.asarray(curve.x, float),
                                      np.asarray(curve.total, float))
            st.residuals_spec = builders.residuals_spec(
                x=energy, residuals=counts - total_on_data, yerr=yerr, mask=mask,
                xlabel=xlabel)

    @staticmethod
    def _fine_grid(fit, energy):
        """A fine x-grid for the delivered curve: the measured energy span when a data file exists,
        else a symmetric window around ω₀ (so a pooled master with no member file still plots a
        curve). ``None`` when neither is available."""
        if energy is not None and energy.size:
            return np.linspace(float(np.min(energy)), float(np.max(energy)), _FINE_N)
        if fit.omega0 is not None:
            c = float(fit.omega0)
            return np.linspace(c - 10.0, c + 10.0, _FINE_N)
        return None

    # -- AIC candidates --
    @staticmethod
    def _candidate_rows(fit):
        cands = fit.candidates
        if cands is None:
            return [], False
        rows = []
        for c in cands:
            if not isinstance(c, dict):
                continue
            da = c.get("delta_aic")
            rows.append(CandidateRow(
                label=str(c.get("label", "?")),
                delta_aic=None if da is None else float(da),
                weight=None if c.get("akaike_weight") is None else float(c["akaike_weight"]),
                n_modes=c.get("n_modes"),
                winner=(da is not None and float(da) == 0.0),
            ))
        return rows, True

    # -- verdict / provenance dicts --
    @staticmethod
    def _verdict(fit) -> dict:
        d = {
            "verdict": fit.verdict,
            "disposition": fit.disposition,
            "confidence": fit.confidence,
            "best_label": fit.best_label,
            "modality": fit.modality,
            "fit_kind": fit.fit_kind,
        }
        if fit.branch_id is not None:
            d["branch_id"] = fit.branch_id
        if fit.assignment_entropy is not None:
            d["assignment_entropy"] = fit.assignment_entropy
        return d

    @staticmethod
    def _provenance(fit) -> dict:
        d: dict = {}
        prior = fit.prior_used
        if prior:
            d["prior_used"] = ", ".join(f"{k}={prior[k]}" for k in sorted(prior))
        calib = fit.calib
        if calib:
            parts = []
            for k in ("center", "gauss_fwhm", "lorentz_fwhm"):
                if calib.get(k) is not None:
                    parts.append(f"{k}={float(calib[k]):.3g}")
            if parts:
                d["calib"] = ", ".join(parts)
        if fit.pooled:
            if fit.disposition == "rejected":
                d["pooled"] = "pooled master — uncertifiable"
            else:
                d["pooled"] = (f"pooled master ({fit.n_members} members)"
                               if fit.n_members else "pooled master")
            if fit.members:
                d["members"] = [str(m) for m in fit.members]
            if fit.master_revision:
                d["master_revision"] = fit.master_revision
            d["linewidth_state"] = fit.linewidth_state or "unknown"
            d["pool_consistency"] = (
                "pass" if fit.pool_consistent is True else
                "fail" if fit.pool_consistent is False else "not available")
            if fit.consistency_pvalue is not None:
                d["pool_consistency_p"] = f"{fit.consistency_pvalue:.4g}"
            d["predictive_ensemble"] = (fit.predictive_ref or "not available")
        return d
