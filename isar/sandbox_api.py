"""Qt-free G7 sandbox API: an ephemeral calibrate-and-fit preview of one scan.

The GUI is a viewer/proposer and never computes a number of record (P1/G2). G7 sanctions this one
core entry point for a fast, clearly marked preview. The result is never written or official.

Two layers:
- :func:`fit_context` — the pure core: ``build_context`` + ``fit_one(errors_tier="fast")`` +
  exact evaluation of the winning hypothesis. Testable with a reduced record + ``CalibResolution``,
  exactly as ``tests/session/test_driver_multimode.py`` exercises the engine.
- :func:`preview_fit` — the full front-end for a workspace unit: re-parse the source data (the same
  ``isar run`` does), reduce, run the card's calibration on an **unsaved** card copy (the elastic
  zero comes from the card's calibration scans — NOT the measurement scan — via
  ``router.apply_context_updates``), resolve, locate the unit's record, and call :func:`fit_context`.

**Hard Rule 4 holds:** a `needs_zero` scan with no resolvable zero stays *held* — never E0 = 0.
Nothing here writes the workspace: the card copy is mutated in memory and discarded.
"""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field

import numpy as np

log = logging.getLogger(__name__)

_FINE_N = 400
_SEED = 6000


@dataclass
class SandboxResult:
    """An ephemeral fit-preview result. ``status`` is ``"fitted" | "held" | "error"``."""
    unit_key: str
    status: str
    reason: str = ""
    # data in the raw energy frame; ``center`` marks the elastic zero
    x: object = None
    counts: object = None
    yerr: object = None
    mask: object = None
    # model curve on a fine grid in the same raw frame
    x_fit: object = None
    total: object = None
    components: list = field(default_factory=list)
    # parameters (preview — fast tier, not certified)
    omega0: float | None = None
    gamma: float | None = None
    interval: tuple | None = None
    hesse_trusted: bool | None = None
    verdict: str | None = None
    center: float | None = None
    gauss_fwhm: float | None = None
    zero_source: str | None = None   # how the energy zero was obtained ("interpolated" | "manual (preview)" | ...)
    zero_guess: float | None = None  # RAW-frame position for the draggable E₀ line (center when fitted)
    needs_manual_zero: bool = False  # true only when calibration is why this preview is held

    @property
    def ok(self) -> bool:
        return self.status == "fitted"


def _held(unit_key: str, reason: str) -> SandboxResult:
    return SandboxResult(unit_key=unit_key, status="held", reason=reason)


def _error(unit_key: str, reason: str) -> SandboxResult:
    return SandboxResult(unit_key=unit_key, status="error", reason=reason)


def _raw_arrays(record, detector_id):
    """The RAW-frame (x, counts, σ, mask) for a detector — the frame the sandbox plots and the user
    places the E₀ line in. σ = √N with no floor (convention D3)."""
    x = _energy_of(record, detector_id)
    sp = record.spectra[detector_id]
    counts = np.asarray(sp.intensity, dtype=float)
    yerr = np.sqrt(np.clip(counts, 0.0, None))
    mask = np.asarray(getattr(sp, "mask", np.ones_like(x, dtype=bool)), dtype=bool)
    return x, counts, yerr, mask


def _guess_zero(x, counts) -> float:
    """A STARTING position for the draggable E₀ line: the tallest point near the nominal zero
    (|x| < 2.5), else the global max. Only a starting guess — the user drags it onto the elastic."""
    x = np.asarray(x, dtype=float)
    counts = np.asarray(counts, dtype=float)
    if x.size == 0:
        return 0.0
    near = np.abs(x) < 2.5
    if near.any():
        return float(x[near][int(np.argmax(counts[near]))])
    return float(x[int(np.argmax(counts))])


def _held_zero(unit_key, reason, record, detector_id) -> SandboxResult:
    """Held for lack of a zero — but carry the RAW spectrum + a guessed E₀ so the dock can offer
    MANUAL placement (drag the E₀ line onto the visible elastic), never a blank panel."""
    x, counts, yerr, mask = _raw_arrays(record, detector_id)
    return SandboxResult(unit_key=unit_key, status="held", reason=reason,
                         x=x, counts=counts, yerr=yerr, mask=mask,
                         zero_guess=_guess_zero(x, counts), needs_manual_zero=True)


def _held_data(unit_key, reason, record, detector_id, *, center=None) -> SandboxResult:
    """A non-calibration held state carrying its raw data and calibrated marker when available."""
    x, counts, yerr, mask = _raw_arrays(record, detector_id)
    return SandboxResult(unit_key=unit_key, status="held", reason=reason,
                         x=x, counts=counts, yerr=yerr, mask=mask,
                         center=center, zero_guess=center, needs_manual_zero=False)


# --------------------------------------------------------------------------- the fit core
def fit_context(record, detector_id, card, calres, guidelines, *, unit_key, scan_number, q, T,
                prior=None, labels=None, peaks=None, m=200, seed=_SEED) -> SandboxResult:
    """Build the context, run the FAST-tier fitter, reconstruct a preview curve. Pure + Qt-free.

    ``calres`` is a :class:`~isar.session.calibrations.CalibResolution` (or a stub with
    ``center/gauss_fwhm/lorentz_fwhm``). A held calibration returns a ``held`` result that still
    carries the RAW spectrum + a guessed zero, so the dock can offer manual E₀ placement (never a
    fabricated curve). Everything is displayed in the RAW frame; ``center`` marks the energy zero."""
    from isar.session import scan_delivery

    try:
        temperature_ok = np.isfinite(float(T))
    except (TypeError, ValueError) as exc:
        log.warning("sandbox held %s: invalid temperature (%s)", unit_key, exc)
        temperature_ok = False
    if not temperature_ok:
        log.warning("sandbox held %s: temperature is missing or non-finite", unit_key)
        return _held_data(unit_key, "temperature is missing or non-finite", record, detector_id,
                          center=getattr(calres, "center", None))

    if getattr(calres, "held", False) or calres.center is None:
        reason = getattr(calres, "held_reason", None) or "no energy zero — drag E₀ onto the elastic line"
        return _held_zero(unit_key, reason, record, detector_id)

    x, counts, yerr, mask = _raw_arrays(record, detector_id)
    center = float(calres.center)

    ctx, n_used, _reasons = scan_delivery.build_context(
        record, detector_id, card, calres, prior, guidelines, scan_number=scan_number, q=q, T=T)
    if ctx is None:
        return _held_data(unit_key, f"too few points to fit ({n_used} kept)", record, detector_id,
                          center=center)

    try:
        fit = scan_delivery.fit_one(ctx, card, prior=prior, M=m, seed=seed,
                                    structure_labels=labels, peaks=peaks, errors_tier="fast")
    except Exception as exc:  # noqa: BLE001 — a preview fit failing is a shown state, not a crash
        log.warning("sandbox fit_one failed for %s: %s", unit_key, exc)
        return _error(unit_key, f"fit error: {type(exc).__name__}: {exc}")

    omega0 = fit.get("omega0")
    gamma = fit.get("gamma")
    if omega0 is None or gamma is None:
        return _held_data(unit_key, "fit produced no ω₀ (no admissible phonon)", record,
                          detector_id, center=center)

    fine = np.linspace(float(np.min(x)), float(np.max(x)), _FINE_N) if x.size else None
    try:
        curve = _winning_curve(fit, ctx, fine)
    except Exception as exc:  # noqa: BLE001
        log.warning("sandbox curve reconstruction failed for %s: %s", unit_key, exc)
        curve = None

    # RAW frame: data + model share the raw energy axis; `center` marks the (movable) E₀.
    return SandboxResult(
        unit_key=unit_key, status="fitted",
        x=x, counts=counts, yerr=yerr, mask=mask,
        x_fit=(None if curve is None else np.asarray(curve.x)),
        total=(None if curve is None else curve.total),
        components=([] if curve is None else list(getattr(curve, "components", []) or [])),
        omega0=float(omega0), gamma=float(gamma),
        interval=_interval_of(fit), hesse_trusted=fit.get("hesse_trusted"),
        verdict=fit.get("verdict"), center=center, gauss_fwhm=float(calres.gauss_fwhm),
        zero_source=getattr(calres, "zero_source", None), zero_guess=center)


def _winning_curve(fit, ctx, x_eval):
    """Evaluate the exact winning structure from the fit's own model manifest (its components and
    every fitted parameter, pinned ones included), so any winner the fit can return rebuilds,
    degenerate twins and background-free models among them, never a re-enumerated stand-in.
    """
    from types import SimpleNamespace

    from isar.analyze.model_manifest import compose_manifest

    manifest = fit.get("model_manifest")
    if not manifest:
        raise ValueError(f"winning hypothesis {fit.get('best_label')!r} has no model manifest")
    comp, theta = compose_manifest(manifest, getattr(ctx, "quasielastic_fwhm", None))
    xe = np.asarray(x_eval, dtype=float)
    components = comp.evaluate_components(xe, theta, ctx)
    return SimpleNamespace(x=xe, total=sum((values for _name, values in components),
                                           np.zeros_like(xe)), components=components)


def _energy_of(record, detector_id):
    """The scanned energy axis for a record (the abscissa `build_context`/`reconstruct_curve` use)."""
    from isar.schema import axes
    return np.asarray(axes.scanned_axes(record)[0].values, dtype=float)


def _interval_of(fit) -> tuple | None:
    iv = fit.get("interval") or fit.get("hesse_interval")
    if iv is not None and len(iv) >= 2:
        return (float(iv[0]), float(iv[-1]))
    return None


# --------------------------------------------------------------------------- the full front-end
def preview_fit(workspace_root, unit_key: str, *, center_override: float | None = None,
                m: int = 200, seed: int = _SEED) -> SandboxResult:
    """Re-parse → reduce → calibrate (ephemeral card copy) → resolve → fit ``unit_key``.

    An explicit ``center_override`` (the user placed the E₀ line) wins; otherwise only the card's
    calibration series is accepted. An unresolved scan remains held. Nothing is written. Returns a
    :class:`SandboxResult` (``fitted`` / ``held`` / ``error``)."""
    try:
        exp, scan_number, det = _split_unit(unit_key)
    except ValueError as exc:
        return _error(unit_key, str(exc))

    try:
        from isar.reduce import combine, qc
        from isar.intake import scans as intake_scans
        from isar.session import calibrations, driver, file_fit, ingest, router
        from isar.session import guidelines as guide_mod
        from isar.session.workspace import Workspace
    except Exception as exc:  # noqa: BLE001 — import failure is a shown state
        log.warning("sandbox engine import failed for %s: %s", unit_key, exc)
        return _error(unit_key, f"engine import failed: {exc}")

    try:
        ws = Workspace.open(workspace_root)
        card_real, session = ws.load()
        card = copy.deepcopy(card_real)   # EPHEMERAL: mutate calibration state in memory, never save
        try:
            rows = intake_scans.read_scans(workspace_root)
        except (ValueError, OSError):
            log.exception("sandbox preview_fit: the import record of %s is unreadable", workspace_root)
            rows = None
        files = file_fit.given_files(session, rows, unit_key)   # the files the user gave, never the folders
        if isinstance(files, str):
            return _held(unit_key, files)
        plan = ingest.discover(files, card)
        records = ingest.parse_all(plan, card)
        for rec in records:
            combine.reduce_record(rec, card)
            qc.qc_record(rec, card)
        routed = router.route(records)
        router.apply_context_updates(routed, card, session, companion_files=plan.companion_files)
        runcal = calibrations.calibration_view(card)
        g = guide_mod.resolve(card)
    except Exception as exc:  # noqa: BLE001
        log.warning("sandbox preview_fit front-end failed for %s: %s", unit_key, exc)
        return _error(unit_key, f"preview setup failed: {type(exc).__name__}: {exc}")

    rec = _find_record(routed.measurements, exp, scan_number)
    if rec is None or det not in getattr(rec, "spectra", {}):
        return _held(unit_key, "scan/detector not found in the parsed data")

    sp = rec.spectra[det]
    q = [float(v) for v in (sp.q if getattr(sp, "q", None) is not None else [0.0, 0.0, 0.0])]
    try:
        T = float(np.median(np.asarray(rec.temperature.setpoint, dtype=float)))
    except Exception as exc:  # noqa: BLE001
        log.warning("sandbox held %s: could not read temperature (%s)", unit_key, exc)
        T = float("nan")

    if not np.isfinite(T):
        log.warning("sandbox held %s: temperature is missing or non-finite", unit_key)
        return _held_data(unit_key, "temperature is missing or non-finite", rec, det)

    try:
        calres = calibrations.resolve(rec, det, card, runcal)
    except Exception as exc:  # noqa: BLE001
        log.warning("sandbox calibration failed for %s: %s", unit_key, exc)
        return _error(unit_key, f"calibration failed: {exc}")

    # Preview precedence: explicit operator placement, then the normal run-level calibration only.
    # Never infer E0 from this measurement: its tallest feature may be a phonon (Hard Rule 4).
    if center_override is not None:
        if not np.isfinite(float(center_override)):
            return _error(unit_key, "manual energy zero must be finite")
        calres = _manual_calres(rec, det, calres, float(center_override))

    prior, labels, peaks = _fit_inputs(g, rec, card, scan_number, q, T)
    return fit_context(rec, det, card, calres, g, unit_key=unit_key, scan_number=scan_number,
                       q=q, T=T, prior=prior, labels=labels, peaks=peaks, m=m, seed=seed)


def _manual_calres(record, detector_id, base, center):
    """User-placed energy zero (preview): the user asserts the elastic sits at ``center`` (raw frame).

    Refine the exact position + measure the Voigt resolution widths with a LOCAL elastic fit around
    that mark, then trust the user's placement — this deliberately BYPASSES the significance gate
    (they can see the line; a threshold has no business overruling that). Returns a
    :class:`~isar.session.calibrations.CalibResolution` (``zero_source="manual (preview)"``)."""
    from isar.calibrate.elastic import fit_elastic
    from isar.session.calibrations import CalibResolution
    x, counts, _y, _m = _raw_arrays(record, detector_id)
    gfw = float(getattr(base, "gauss_fwhm", 0.0) or 0.96)
    lfw = float(getattr(base, "lorentz_fwhm", 0.0) or 0.0)
    cen_r = 0.0
    try:
        cen_r, g_fit, l_fit, _ok = fit_elastic(x - float(center), counts)  # elastic now near 0
        gfw, lfw = float(g_fit), float(l_fit)
    except Exception as exc:  # noqa: BLE001 — a failed local fit still honours the user's zero + default widths
        log.debug("manual-zero local fit failed: %s", exc)
    # Trust the user's mark; only adopt the local refinement when it stayed close to where they put it.
    center_final = float(center) + float(cen_r) if abs(cen_r) < 1.5 else float(center)
    return CalibResolution(
        center=center_final, gauss_fwhm=gfw, lorentz_fwhm=lfw,
        zero_source="manual (preview)", resolution_source="manual_elastic",
        resolution_version=getattr(base, "resolution_version", "v0"))


def _fit_inputs(g, rec, card, scan_number, q, T):
    """(prior, structure_labels, peaks) from the card guidelines / session prior — best-effort."""
    prior = labels = peaks = None
    try:
        prior = g.expectation_for(scan_number, q=q, T=T)
    except Exception as exc:  # noqa: BLE001
        log.debug("sandbox: no guideline expectation (%s)", exc)
    if prior is None:
        try:
            from isar.session import session_prior
            sample = _sample_of(rec, card)
            period = (card.policy.dispersion or {}).get("zone_period") if getattr(
                card.policy, "dispersion", None) else None
            t_round = float((card.policy.collation or {}).get("t_round", 5.0))
            if period is not None:
                prior = session_prior.prior_for(card.state, sample=sample, T=T, q=q,
                                                period=period, t_round=t_round)
        except Exception as exc:  # noqa: BLE001
            log.debug("sandbox: no session prior (%s)", exc)
    for name, dst in (("structure_labels_for", "labels"), ("peaks_for", "peaks")):
        fn = getattr(g, name, None)
        if callable(fn):
            try:
                val = fn(scan_number, q, T)
                if dst == "labels":
                    labels = val
                else:
                    peaks = val
            except Exception as exc:  # noqa: BLE001
                log.debug("sandbox: %s failed (%s)", name, exc)
    return prior, labels, peaks


def _sample_of(rec, card) -> str:
    try:
        from isar.session import driver
        return str(driver._sample_of(rec, card))
    except Exception as exc:  # noqa: BLE001
        log.debug("sandbox: could not resolve sample identity (%s)", exc)
        return ""


def _split_unit(unit_key: str) -> tuple[str, int, str]:
    from isar.workspace_api.scan_keys import split_key

    exp, scan, det = split_key(unit_key)
    if exp is None or det is None or not isinstance(scan, int) or not exp or not det:
        raise ValueError(f"not a unit key (exp:scan:det): {unit_key!r}")
    return exp, scan, det


def _find_record(measurements, exp, scan_number):
    for rec in measurements:
        if (str(getattr(rec, "experiment_id", "")) == str(exp)
                and int(getattr(rec, "scan_number", -1)) == int(scan_number)):
            return rec
    return None
