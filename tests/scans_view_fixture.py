"""Synthetic scans-view workspace shared with the spectra-read and GUI checks."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from bl43_fixture import BA122_FILE, KEY_A, KEY_B, KEY_SINGLE, STEM_A, STEM_B
from bl43_intake_fixture import build_intake_workspace, write_run

N_ANALYZERS = 7
_A_DETS = ("a03", "a04", "a05", "a06", "a07", "a08", "a09")
_B_DETS = ("a03", "a04", "a05", "a06", "a08", "a09")
SPECTRA_A = tuple(f"{KEY_A}:{det}" for det in _A_DETS)
SPECTRA_B = tuple(f"{KEY_B}:{det}" for det in _B_DETS)
ONE_MODE = SPECTRA_A[0]
TWO_MODE = SPECTRA_A[1]
NO_Q = SPECTRA_A[2]
DEEP_REFUSED = SPECTRA_A[3]
DEEP_DELTA = SPECTRA_A[4]
DEEP_OUTDATED = DEEP_DELTA
DROPOUT = SPECTRA_A[5]
NO_ZERO = SPECTRA_A[6]
OUTDATED = SPECTRA_B[0]
DEEP_TRUSTED = SPECTRA_B[1]
STOKES_ONLY = SPECTRA_B[2]
FAST_ZERO_MODE = SPECTRA_B[3]
DEEP_NO_PARAMS = SPECTRA_B[4]
HELD = SPECTRA_B[5]
NO_MONITOR = f"{KEY_SINGLE}:a06"
FAST_ONLY = (NO_Q, DROPOUT, OUTDATED, NO_MONITOR)
FITTED = tuple(key for key in (*SPECTRA_A, *SPECTRA_B, NO_MONITOR)
               if key not in (NO_ZERO, HELD))
DEEP_LABELS = {
    ONE_MODE: "1 DHO",
    DEEP_REFUSED: None,
    DEEP_DELTA: "1 DHO",
    DEEP_TRUSTED: "1 DHO",
    STOKES_ONLY: "1 DHO",
    FAST_ZERO_MODE: "1 DHO",
    DEEP_NO_PARAMS: "1 DHO",
}

_ENERGY = np.round(np.arange(60, dtype=float) * 0.2 - 4.0, 4)
_BA122_ENERGY = np.round(np.arange(8, dtype=float) * 0.2, 4)
_CENTER = 0.12
_GAUSS = 0.8
_LORENTZ = 0.1


@dataclass(frozen=True)
class Truth:
    x: np.ndarray
    counts: np.ndarray
    monitor: np.ndarray | None
    time: np.ndarray | None
    dropout: tuple[int, ...]
    m_ref: float | None
    exposure_source: str | None
    exposure_scale: np.ndarray | None
    rate: np.ndarray
    center: float
    fast_manifest: dict | None
    deep: dict | None


def _manifest(modes=(), *, peak="stokes+astokes") -> dict:
    components = []
    parameters = {}
    for number, (energy, gamma, height) in enumerate(modes, 1):
        prefix = "phonon" if number == 1 else f"phonon{number}"
        components.append({"type": "phonon_dho", "prefix": prefix, "peak": peak})
        parameters.update({f"{prefix}.omega0": energy, f"{prefix}.Gamma": gamma,
                           f"{prefix}.h": height})
    components.extend(({"type": "elastic", "prefix": "elastic"},
                       {"type": "constant_background", "prefix": "bg"}))
    parameters.update({"elastic.h": 300.0, "bg.BG": 3.0})
    return {"schema": "isar.model_manifest/2", "components": components,
            "parameters": parameters}


def _manifest_rate(manifest: dict, x, temperature=97.0) -> np.ndarray:
    from isar.analyze.model_manifest import EvaluationContext, compose_manifest

    model, theta = compose_manifest(manifest)
    context = EvaluationContext(center=_CENTER, gauss_fwhm=_GAUSS,
                                lorentz_fwhm=_LORENTZ, T=temperature)
    return np.asarray(model.evaluate(np.asarray(x, dtype=float), theta, context), dtype=float)


def _fast_manifest(key: str) -> tuple[str, dict, list[tuple[str, dict]]]:
    if key == TWO_MODE:
        selected = _manifest(((5.5, 0.4, 32.0), (2.5, 0.4, 24.0)))
        return "2 DHO", selected, [("2 DHO", selected)]
    if key == FAST_ZERO_MODE:
        selected = _manifest()
        one_mode = _manifest(((4.0, 0.4, 30.0),))
        return "elastic+bg", selected, [("elastic+bg", selected),
                                                      ("1 DHO", one_mode)]
    if key == STOKES_ONLY:
        selected = _manifest(((4.2, 0.4, 30.0),), peak="stokes")
    elif key == DEEP_DELTA:
        selected = _manifest(((3.8, 0.4, 30.0),))
    elif key == NO_MONITOR:
        selected = _manifest(((0.8, 0.2, 12.0),))
    else:
        selected = _manifest(((4.0, 0.4, 30.0),))
    label = "1 DHO"
    models = [(label, selected)]
    if key in (DEEP_DELTA, DEEP_TRUSTED, STOKES_ONLY, DEEP_NO_PARAMS):
        models.append(("2 DHO", _manifest(((4.0, 0.4, 24.0), (5.5, 0.4, 20.0)))))
    return label, selected, models


def _deep_record(key: str) -> dict | None:
    if key == DEEP_REFUSED:
        return {"state": "refused", "trusted": False,
                "reason": "deep model selection was refused", "refuse": "no deep winner"}
    if key not in DEEP_LABELS or key == DEEP_NO_PARAMS:
        return None
    if key == DEEP_DELTA:
        chosen_manifest = _manifest(((3.8, 0.001, 30.0),))
    elif key == STOKES_ONLY:
        chosen_manifest = _manifest(((4.2, 0.4, 30.0),), peak="stokes")
    elif key == FAST_ZERO_MODE:
        chosen_manifest = _manifest(((4.0, 0.4, 30.0),))
    else:
        chosen_manifest = _manifest(((4.0, 0.4, 30.0),))
    params = dict(chosen_manifest["parameters"])
    minos = {}
    for prefix in ("phonon", "phonon2"):
        if f"{prefix}.omega0" in params:
            energy, gamma = params[f"{prefix}.omega0"], params[f"{prefix}.Gamma"]
            minos[f"{prefix}.omega0"] = [energy - 0.2, energy + 0.25, True]
            minos[f"{prefix}.Gamma"] = [max(0.0, gamma - 0.1), gamma + 0.15, True]
    trusted = key != ONE_MODE
    result = {"state": "resolved", "trusted": trusted,
            "chosen": {"label": DEEP_LABELS[key], "params": params,
                       "model_manifest": chosen_manifest,
                       "errors": {"minos": minos}}}
    if not trusted:
        result["reason"] = "the stored deep fit did not meet the trust criterion"
    return result


def _truth_rows() -> dict[str, Truth]:
    rows = {}
    for index, key in enumerate((*SPECTRA_A, *SPECTRA_B)):
        label, manifest, _models = _fast_manifest(key)
        rate = _manifest_rate(manifest, _ENERGY)
        analyzer = key.rsplit(":", 1)[1]
        phase = int(analyzer[1:]) * 0.37 + (0.9 if key in SPECTRA_B else 0.0)
        monitor = np.round(1e5 * (1.0 + 0.3 * np.sin(0.37 * np.arange(60) + phase)), 1)
        if key in SPECTRA_B:
            monitor *= 2.0
        dropout = (30,) if key == DROPOUT else ()
        if dropout:
            monitor[list(dropout)] = 0.0
        valid = monitor > 0.0
        m_ref = float(np.median(monitor[valid]))
        exposure_scale = monitor / m_ref
        counts = np.random.default_rng(100 + index).poisson(rate * exposure_scale).astype(float)
        if dropout:
            counts[list(dropout)] = 0.0
        rows[key] = Truth(_ENERGY.copy(), counts, monitor, None, dropout, m_ref,
                          "monitor", exposure_scale, rate, _CENTER, manifest,
                          _deep_record(key))

    no_monitor_manifest = _fast_manifest(NO_MONITOR)[1]
    time = 10.0 * (1.0 + 0.5 * (np.arange(8) % 3))
    exposure_scale = time / 15.0
    rate = _manifest_rate(no_monitor_manifest, _BA122_ENERGY, temperature=26.0)
    counts = np.random.default_rng(199).poisson(rate * exposure_scale).astype(float)
    rows[NO_MONITOR] = Truth(_BA122_ENERGY.copy(), counts, None, time, (), 15.0,
                             "time", exposure_scale, rate, _CENTER,
                             no_monitor_manifest, None)
    return rows


_TRUTH = _truth_rows()


def truth(key: str) -> Truth:
    return _TRUTH[key]


def _analyzer_text(number: int, values: Truth, *, with_q: bool, temperature: float) -> str:
    rows = ["#      meV      secs   npts          norm_i             y_i            ye_i"]
    for energy, counts, monitor in zip(values.x, values.counts, values.monitor):
        y = counts / monitor if monitor > 0.0 else 0.0
        ye = np.sqrt(counts) / monitor if monitor > 0.0 else 0.0
        rows.append(f"{energy:10.4f} {10.0:9.1f} {1:6d} {monitor:15.7E} {y:15.7E} {ye:15.7E}")
    if with_q:
        rows.append(f"#Q=   {20.0 + number / 10:.3f} /nm")
    rows.append(f"#T=  {temperature:.3f} K")
    return "\n".join(rows) + "\n"


def _synthetic_content() -> dict[str, str]:
    content = {}
    for key in SPECTRA_A:
        det = key.rsplit(":", 1)[1]
        number = int(det[1:])
        content[f"{STEM_A}_{det}"] = _analyzer_text(
            number, truth(key), with_q=key != NO_Q, temperature=97.0)
    for key in SPECTRA_B:
        det = key.rsplit(":", 1)[1]
        number = int(det[1:])
        content[f"{STEM_B}_{det}"] = _analyzer_text(
            number, truth(key), with_q=True, temperature=97.0)
    no_monitor = truth(NO_MONITOR)
    content[BA122_FILE] = "\n".join(
        f"{energy:.4f} {count:.6E} {np.sqrt(count):.6E}"
        for energy, count in zip(no_monitor.x, no_monitor.counts)) + "\n"
    return content


def _rewrite_special_unit(ws_root: Path, incoming: Path, key: str, *, time=None,
                          needs_zero: bool = False) -> None:
    from isar.session import ingest, unit_data
    from isar.session.workspace import Workspace

    ws = Workspace.open(ws_root)
    card, _session = ws.load()
    if key == NO_MONITOR:
        paths = [incoming / BA122_FILE]
    else:
        det = key.rsplit(":", 1)[1]
        stem = STEM_B if key in SPECTRA_B else STEM_A
        paths = [incoming / f"{stem}_{det}", incoming / f"{stem}.var"]
    records = ingest.parse_all(ingest.discover(paths, card), card)
    scan_key, det = key.rsplit(":", 1)
    exp, scan = scan_key.rsplit(":", 1)
    record = next(rec for rec in records if rec.experiment_id == exp and rec.scan_number == int(scan))
    if time is not None:
        record.spectra[det].time = np.asarray(time, dtype=float)
    record.energy_calibration.status = "needs_zero" if needs_zero else "absolute"
    record.energy_calibration.elastic_zero_offset = None if needs_zero else 0.0
    unit_data.write_unit_data(record, det, ws_root / "data")


def _outcome(key: str) -> dict:
    label, manifest, models = _fast_manifest(key)
    params = manifest["parameters"]
    modes = []
    for prefix in ("phonon", "phonon2"):
        energy = params.get(f"{prefix}.omega0")
        if energy is not None:
            modes.append({"omega0": energy, "omega0_err": 0.1,
                          "gamma": params[f"{prefix}.Gamma"], "amplitude": params[f"{prefix}.h"]})
    deep = _deep_record(key)
    if key == DEEP_NO_PARAMS:
        deep = {"state": "resolved", "trusted": False,
                "chosen": {"label": DEEP_LABELS[key], "params": None,
                           "model_manifest": {"schema": "isar.model_manifest/2",
                                              "components": manifest["components"],
                                              "parameters": {}}}}
    if deep is not None:
        deep = dict(deep)
    state = ("search_limited" if key == DEEP_REFUSED else
             "close_call" if key == TWO_MODE else
             "resolved")
    return {
        "detector_id": key.rsplit(":", 1)[1], "verdict": "resolved",
        "disposition": "resolved", "T": 26.0 if key == NO_MONITOR else 97.0, "q": None,
        "omega0": modes[0]["omega0"] if modes else None,
        "omega0_err": 0.1 if modes else None,
        "gamma": modes[0]["gamma"] if modes else None,
        "n_modes": len(modes), "best_label": label, "modes": modes,
        "model_manifest": manifest,
        "calib": {"center": _CENTER, "gauss_fwhm": _GAUSS, "lorentz_fwhm": _LORENTZ},
        "fast_pass": {"schema": "isar.fast-pass/1", "state": state,
                       "preferred_count": len(modes),
                       "count_probabilities": {str(len(modes)): 1.0},
                       "peaks": [], "flags": [],
                       "deep_pass_candidate": deep is not None,
                       "models": [{"label": model_label, "params": model["parameters"],
                                   "model_manifest": model}
                                  for model_label, model in models]},
        **({"deep_pass": deep} if deep is not None else {}),
    }


def build_scans_view_workspace(root: Path) -> Path:
    """Build the full deterministic C-spectra-read fixture and return ``root/ws``."""
    from bl43_fixture import _load, _save, stamp_status

    root = Path(root)
    ws_root = build_intake_workspace(root, N_ANALYZERS, curves=True, fits=False,
                                     content=_synthetic_content())
    incoming = root / "incoming"
    for key in (*SPECTRA_A, *SPECTRA_B):
        _rewrite_special_unit(ws_root, incoming, key, needs_zero=key == NO_ZERO)
    _rewrite_special_unit(ws_root, incoming, NO_MONITOR, time=truth(NO_MONITOR).time)

    ws, _card, session = _load(ws_root)
    for key in (*SPECTRA_A, *SPECTRA_B, NO_MONITOR):
        entry = session.scans[key]
        status = "needs_zero" if key == NO_ZERO else "absolute"
        entry.provenance = {**(entry.provenance or {}), "energy_status": status,
                            "raw_abscissa_kind": "energy"}
    for key in FITTED:
        entry = session.scans[key]
        entry.detectors = {key.rsplit(":", 1)[1]: _outcome(key)}
        entry.status, entry.reasons = "fitted", []
    _save(ws, _card, session)
    stamp_status(ws_root, HELD, "held_for_calibration", ["a09: energy zero is not available"])
    stamp_status(ws_root, NO_ZERO, "held_for_calibration", ["a09: energy zero is not available"])
    write_run(ws_root, FITTED, stale={OUTDATED: {"data": "earlier revision"}})
    write_run(ws_root, [DEEP_OUTDATED], passes=("deep",),
              stale={DEEP_OUTDATED: {"data": "earlier revision"}})
    return ws_root
