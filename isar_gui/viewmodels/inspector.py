"""InspectorViewModel — view-ready state for the C2 fit inspector (Task 10, A-4).

The flagship read-side view: given the :class:`~isar.workspace_api.FitView` the browser selected, it
assembles everything the inspector widget draws — the data+fit overlay spec, the residuals spec, the
interval-ladder rows (labeled by tier, one certifying), the AIC candidate rows, and the
verdict/provenance dicts. ALL derivation lives here; the widget only renders these outputs.

Interval-ladder derivation follows ``FitOutcome`` (isar/session/driver.py) exactly:

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

import numpy as np
from PySide6.QtCore import Signal

from isar.plotspec import builders
from isar_gui.viewmodels.base import ViewModel

_FINE_N = 400


def _fmt_interval(iv) -> str:
    lo, hi = float(iv[0]), float(iv[-1])
    return f"[{lo:.3f}, {hi:.3f}]"


@dataclass
class LadderRow:
    """One interval-ladder tier as display-ready state. ``certifying`` is set on at most one row
    (bayes never). ``interval_text`` is the honest text — ``"(unreliable)"`` or ``"—"``, never
    numbers, when the tier does not carry a usable numeric range."""

    tier: str
    interval_text: str
    certifying: bool = False
    annotation: str = ""
    diagnostics_only: bool = False


@dataclass
class CandidateRow:
    label: str
    delta_aic: float | None
    weight: float | None
    n_modes: int | None
    winner: bool = False


@dataclass
class InspectorState:
    """The whole assembled view-state (empty when no fit is selected)."""

    empty: bool = True
    main_spec: object | None = None
    residuals_spec: object | None = None
    ladder_rows: list = field(default_factory=list)
    candidate_rows: list = field(default_factory=list)
    candidates_recorded: bool = False
    verdict: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)
    certifying_tier: str | None = None
    curve_error: str | None = None   # set when the specs could not be built (tables still shown)


class InspectorViewModel(ViewModel):
    """Assembles the C2 inspector view-state from a selected ``FitView``; emits ``spec_changed``."""

    #: Emitted after :meth:`set_fit` rebuilds the state (including the empty-state reset).
    spec_changed = Signal()

    def __init__(self, ws, parent=None):
        super().__init__(ws, parent)
        self._fit = None
        self._state = InspectorState()

    # ---- public state -----------------------------------------------------
    @property
    def state(self) -> InspectorState:
        return self._state

    @property
    def is_empty(self) -> bool:
        return self._state.empty

    # ---- selection slot ---------------------------------------------------
    def set_fit(self, fit_view) -> None:
        """Slot wired to ``BrowserViewModel.selection_changed``. ``None`` resets to the empty state."""
        self._fit = fit_view
        self._state = self._build(fit_view)
        self.spec_changed.emit()

    def on_workspace_changed(self, changed=None) -> None:
        # Re-assemble against the refreshed facade if a fit is selected, then re-emit.
        if self._fit is not None:
            self._state = self._build(self._fit)
        self.spec_changed.emit()
        super().on_workspace_changed(changed)

    # ---- assembly ---------------------------------------------------------
    def _build(self, fit) -> InspectorState:
        if fit is None:
            return InspectorState(empty=True)
        ladder = self._ladder_rows(fit)
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
        self._attach_specs(fit, st)
        return st

    # -- specs (data + fit + residuals) --
    def _attach_specs(self, fit, st: InspectorState) -> None:
        ws = self._ws
        try:
            energy, counts, yerr = ws.scan_data(fit.scan_number)
            if energy is None or energy.size == 0:
                st.curve_error = "no measured data file for this scan"
                return
            fine = np.linspace(float(np.min(energy)), float(np.max(energy)), _FINE_N)
            curve = ws.evaluate_model(fit.scan_number, fit.detector_id, x_eval=fine)
            total_on_data = np.interp(energy, np.asarray(curve.x, float),
                                      np.asarray(curve.total, float))
            residuals = counts - total_on_data
        except (KeyError, ValueError, OSError) as exc:
            st.curve_error = str(exc)
            return

        # The ω₀ whisker in the plot shows the CERTIFYING tier's interval (None => warning marker),
        # not bootstrap-regardless — so an uncertified ω₀ never draws a numeric whisker.
        cert_interval = self._certifying_interval(fit, st.certifying_tier)
        st.main_spec = builders.fit_overlay_spec(
            x=energy, counts=counts, yerr=yerr,
            x_fit=curve.x, total=curve.total, components=curve.components,
            omega0=fit.omega0, interval=cert_interval,
            interval_reliable=(cert_interval is not None),
            scan_number=fit.scan_number, det_id=fit.detector_id, q=fit.q,
            pooled=bool(fit.pooled), members=fit.members, n_members=fit.n_members,
        )
        st.residuals_spec = builders.residuals_spec(x=energy, residuals=residuals, yerr=yerr)

    @staticmethod
    def _certifying_interval(fit, certifying_tier):
        """The numeric interval of the certifying tier (bayes never certifies), or ``None``."""
        if certifying_tier == "bootstrap":
            return fit.interval
        if certifying_tier == "profile":
            return fit.omega0_profile
        if certifying_tier == "hesse":
            return fit.hesse_interval
        return None

    # -- interval ladder --
    def _ladder_rows(self, fit) -> list:
        boot = fit.interval
        reliable = fit.interval_reliable is not False
        prof = fit.omega0_profile
        hesse = fit.hesse_interval
        hesse_trusted = bool(fit.hesse_trusted)
        bayes = fit.bayes_interval

        boot_certifies = reliable and boot is not None
        prof_certifies = (not boot_certifies) and prof is not None
        hesse_certifies = (not boot_certifies and not prof_certifies
                           and hesse_trusted and hesse is not None)

        # bootstrap: never numbers when unreliable
        if boot is None:
            boot_text = "—"
        elif not reliable:
            boot_text = "(unreliable)"
        else:
            boot_text = _fmt_interval(boot)
        rows = [LadderRow("bootstrap", boot_text, certifying=boot_certifies,
                          annotation=("authoritative" if boot_certifies else ""))]

        rows.append(LadderRow("profile", _fmt_interval(prof) if prof is not None else "—",
                              certifying=prof_certifies))

        hesse_note = "trusted" if hesse_trusted else ("not trusted" if hesse is not None else "")
        rows.append(LadderRow("hesse", _fmt_interval(hesse) if hesse is not None else "—",
                              certifying=hesse_certifies, annotation=hesse_note))

        ess = fit.bayes_ess
        bayes_note = "diagnostics-only"
        if ess is not None:
            bayes_note += f"; ESS={ess:.0f}"
        rows.append(LadderRow("bayes", _fmt_interval(bayes) if bayes is not None else "—",
                              certifying=False, annotation=bayes_note, diagnostics_only=True))
        return rows

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
                d["pooled"] = "pooled — uncertifiable"
            else:
                d["pooled"] = f"pooled ({fit.n_members} members)" if fit.n_members else "pooled"
            if fit.members:
                d["members"] = [str(m) for m in fit.members]
        return d
