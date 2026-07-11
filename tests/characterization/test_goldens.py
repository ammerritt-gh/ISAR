"""A-10 characterization goldens: pin the plot-spec dicts the read-side view models emit, so a
refactor that silently changes a rendering *semantic* (a role, a state token, a certifying tier, an
honesty annotation) is caught as a golden diff rather than shipping unnoticed.

Coverage
--------
* **C2 inspector** — ``fit_overlay_spec`` (``InspectorState.main_spec``) + ``residuals_spec`` for
  (a) the lowest-scan reliable fitted scan whose overlay builds, (b) the lowest-scan
  ``interval_reliable=False`` scan whose overlay builds, both from the frozen MVP-16 baseline; and
  (c) a pooled master rendered curve-only + "member data not shown" annotation. The baseline's own
  pooled masters are all descriptor-only (ω₀ absent) or rejected with no persisted spectrum, so none
  yields a buildable curve-only overlay — (c) is therefore pinned from a small deterministic
  synthetic pooled fixture (the honesty contract it exercises — a pooled title never draws member
  counts — is identical).
* **C3 heatmap** — the spec ``to_dict`` with each strip REDUCED to a manifest
  (``{x_center, width, meta, n_points, y_min, y_max, values_sum}``) so the golden stays reviewable;
  overlays / annotations / markers / projection are kept in full.
* **C5 dispersion** — the full spec ``to_dict`` (points / prior / branch / annotations).

Selection is DETERMINISTIC: the lowest ``scan_number`` matching each criterion whose overlay
actually builds (encoded in :func:`_first_buildable`), recorded here so a regeneration re-picks the
same scans.

Baseline ``C:\\Users\\AMM\\Documents\\ISAR_baselines\\mvp_zone_test16`` is READ-ONLY and
skip-if-missing; the pooled + determinism-of-the-synthetic checks run without it.

Regeneration
------------
Set ``UPDATE_GOLDENS=1`` and run this module (on a machine that HAS the baseline for the
baseline-derived goldens); each golden under ``tests/characterization/goldens/`` is rewritten from
the live canonicalised spec. Then re-run WITHOUT the flag to confirm the committed goldens match.
Canonicalisation rounds floats to 9 significant figures and sorts keys, so incidental float noise
never churns a golden.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from isar.workspace_api import Workspace
from isar_gui.viewmodels.dispersion import DispersionViewModel
from isar_gui.viewmodels.heatmap import HeatmapViewModel
from isar_gui.viewmodels.inspector import InspectorViewModel

_MVP16 = Path(r"C:\Users\AMM\Documents\ISAR_baselines\mvp_zone_test16")
_GOLDENS = Path(__file__).resolve().parent / "goldens"
_UPDATE = bool(os.environ.get("UPDATE_GOLDENS"))

_X = [-2.0 + 0.5 * i for i in range(21)]  # a 21-point energy grid for the synthetic pooled fixture


# ---------------------------------------------------------------------------
# Canonicalisation + golden IO
# ---------------------------------------------------------------------------
def _round_sig(x: float, sig: int = 9) -> float:
    if x == 0.0 or not math.isfinite(x):
        return x
    return round(x, -int(math.floor(math.log10(abs(x)))) + (sig - 1))


def _canon(obj):
    """Recursively sort dict keys and round floats to 9 sig figs (bools/ints pass through)."""
    if isinstance(obj, dict):
        return {k: _canon(obj[k]) for k in sorted(obj)}
    if isinstance(obj, (list, tuple)):
        return [_canon(v) for v in obj]
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        return _round_sig(obj)
    return obj


def _reduce_strips(spec_dict: dict) -> dict:
    """Replace each strip's y/values arrays with a compact manifest so the C3 golden is reviewable;
    everything else (overlays / annotations / markers / projection / legend) is kept in full."""
    d = dict(spec_dict)
    manifest = []
    for st in d.get("strips", []):
        y = st.get("y") or []
        vals = st.get("values") or []
        manifest.append({
            "x_center": st["x_center"], "width": st["width"], "meta": st["meta"],
            "n_points": len(y),
            "y_min": (min(y) if y else None), "y_max": (max(y) if y else None),
            "values_sum": (float(sum(vals)) if vals else 0.0),
        })
    d["strips"] = manifest
    return d


def _check_golden(name: str, spec_dict: dict) -> None:
    canon = _canon(spec_dict)
    path = _GOLDENS / f"{name}.json"
    if _UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(canon, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert path.exists(), f"golden {name!r} missing — regenerate with UPDATE_GOLDENS=1"
    expected = _canon(json.loads(path.read_text(encoding="utf-8")))
    assert canon == expected, f"golden {name!r} drifted from the committed snapshot"


# ---------------------------------------------------------------------------
# Spec builders (through the view models, headlessly)
# ---------------------------------------------------------------------------
def _inspector_state(ws, fit):
    vm = InspectorViewModel(ws)
    vm.set_fit(fit)
    return vm.state


def _first_buildable(ws, predicate):
    """Lowest-``scan_number`` fit matching ``predicate`` whose inspector overlay actually builds
    (has a non-None ``main_spec``); ``None`` when none qualifies. Deterministic selection rule."""
    for fit in sorted((f for f in ws.results if predicate(f)), key=lambda f: f.scan_number):
        st = _inspector_state(ws, fit)
        if st.main_spec is not None:
            return fit, st
    return None, None


def _reliable_pred(f):
    return (f.status == "fitted" and f.omega0 is not None
            and f.interval_reliable and not f.pooled)


def _unreliable_pred(f):
    return f.status == "fitted" and f.interval_reliable is False and not f.pooled


def _synthetic_pooled_ws(tmp_path) -> Workspace:
    """A minimal workspace with ONE pooled master carrying stored amplitudes (so ``evaluate_model``'s
    pure path yields a curve without member counts) — the C2(c) curve-only honesty case the frozen
    baseline cannot supply. Mirrors the inspector suite's pooled fixture."""
    from synthetic_ws import stamp_ws

    outcome = {
        "detector_id": "det", "verdict": "resolved", "disposition": "resolved",
        "confidence": 0.9, "omega0": 5.0, "gamma": 1.0, "q": [2.0, 0.0, 0.0], "T": 200.0,
        "sample": "tavi", "best_label": "phonon", "modality": "single", "fit_kind": "unconstrained",
        "n_modes": 1, "calib": {"center": 0.0, "gauss_fwhm": 1.2, "lorentz_fwhm": 0.0},
        "amplitudes": {"phonon.A": 500.0, "bg.BG": 100.0},
        "interval": [4.6, 5.4], "interval_reliable": True,
        "pooled": True, "n_members": 2, "members": ["1", "2"],
    }
    scans = {"tavi:1:det": {"scan_id": "tavi:1:det", "scan_number": 1, "scan_type": "measurement",
                            "status": "fitted", "detectors": {"det": outcome},
                            "provenance": {"M": 600}}}
    counts = [100.0 + 500.0 * math.exp(-((e - 5.0) ** 2) / 2.0) for e in _X]
    root = tmp_path / "pooled_ws"
    root.mkdir()
    stamp_ws(root, scans=scans,
             data_files=[{"scan_number": 1, "energy": list(_X), "counts": counts}])
    return Workspace.open(root)


def _baseline_specs():
    """Every baseline-derived spec dict, keyed by golden name. Skips (returns None) if the frozen
    baseline is absent. Raises loudly if the baseline is present but a required scan is missing."""
    if not _MVP16.exists():
        return None
    ws = Workspace.open(_MVP16)

    rel_fit, rel_st = _first_buildable(ws, _reliable_pred)
    unrel_fit, unrel_st = _first_buildable(ws, _unreliable_pred)
    assert rel_st is not None, "no reliable fitted scan with a buildable overlay in the baseline"
    assert unrel_st is not None, "no unreliable scan with a buildable overlay in the baseline"

    specs = {
        "c2_inspector_reliable_overlay": rel_st.main_spec.to_dict(),
        "c2_inspector_unreliable_overlay": unrel_st.main_spec.to_dict(),
        "c3_heatmap": _reduce_strips(HeatmapViewModel(ws).spec.to_dict()),
        "c5_dispersion": DispersionViewModel(ws).spec.to_dict(),
    }
    if rel_st.residuals_spec is not None:
        specs["c2_inspector_reliable_residuals"] = rel_st.residuals_spec.to_dict()
    if unrel_st.residuals_spec is not None:
        specs["c2_inspector_unreliable_residuals"] = unrel_st.residuals_spec.to_dict()
    return specs


# ---------------------------------------------------------------------------
# Determinism (no baseline needed for the synthetic half)
# ---------------------------------------------------------------------------
def test_pooled_curve_only_spec_is_deterministic(tmp_path):
    ws = _synthetic_pooled_ws(tmp_path)
    fit = ws.scans[0].detectors["det"]
    a = _inspector_state(ws, fit).main_spec.to_dict()
    b = _inspector_state(ws, fit).main_spec.to_dict()
    assert _canon(a) == _canon(b)
    assert a["series"], "pooled overlay produced zero series (vacuous)"


@pytest.mark.skipif(not _MVP16.exists(), reason="frozen MVP-16 baseline not present")
def test_baseline_specs_are_deterministic():
    first = _baseline_specs()
    second = _baseline_specs()
    assert first and second
    for name in first:
        assert _canon(first[name]) == _canon(second[name]), f"{name} not reproducible in-process"


# ---------------------------------------------------------------------------
# C2(c): pooled master curve-only + annotation (synthetic, always runs)
# ---------------------------------------------------------------------------
def test_c2_pooled_master_curve_only_golden(tmp_path):
    from isar.plotspec.spec import Role, State

    ws = _synthetic_pooled_ws(tmp_path)
    st = _inspector_state(ws, ws.scans[0].detectors["det"])
    assert st.main_spec is not None
    d = st.main_spec.to_dict()
    # Honesty law: a pooled title draws NO member data, and says so.
    assert all(s["role"] != Role.DATA.value for s in d["series"])
    assert any(a["role"] == State.POOLED.value and "member data not shown" in a["text"]
               for a in d["annotations"])
    assert st.residuals_spec is None  # no residuals without the (unpersisted) pooled spectrum
    _check_golden("c2_inspector_pooled_overlay", d)


# ---------------------------------------------------------------------------
# Baseline goldens (skip-if-missing)
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not _MVP16.exists(), reason="frozen MVP-16 baseline not present")
def test_baseline_goldens_match():
    specs = _baseline_specs()
    assert specs, "baseline present but no specs produced"
    for name, spec_dict in specs.items():
        _check_golden(name, spec_dict)
