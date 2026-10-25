"""A-11 4D-honesty + truth-gating tests: the honesty laws that the spec layer promises are checked
here at the SPEC level (not the pixel level), across every read-side view built from one synthetic
workspace with deliberate out-of-plane scans, an unreliable ω̂, an entropy channel, and a pooled
master.

The invariants (ARCHITECTURE §12 / design/gui.md §7.3, G9):
* heatmap + dispersion always carry a ``ProjectionLegend`` (the plane is declared, G9) — asserted
  both on the emitted specs and at construction level (``dispersion_spec(projection=None)`` raises);
* the projection's exclusion-count annotation equals the constructed out-of-window count exactly;
* no strip spans a gap (the strip x-extents are pairwise disjoint — a gap stays a gap);
* a **property sweep** over ALL specs from every VM: an UNRELIABLE element never carries whiskers, an
  ``"(unreliable)"`` label never smuggles a numeric interval, and an "assignment entropy" legend note
  only ever appears when some series actually carries a populated ``color_values`` (truth-gating).

Offscreen (conftest forces QT_QPA_PLATFORM=offscreen), importorskip, xdist-safe.
"""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from isar.plotspec import builders
from isar.plotspec.spec import State
from isar.workspace_api import Workspace
from isar_gui.viewmodels.dispersion import DispersionViewModel
from isar_gui.viewmodels.heatmap import HeatmapViewModel
from isar_gui.viewmodels.inspector import InspectorViewModel
from isar_gui.viewmodels.waterfall import WaterfallViewModel

_X = np.linspace(-2.0, 8.0, 21)


def _counts(center: float) -> list:
    return list((100.0 + 500.0 * np.exp(-((_X - center) ** 2) / 2.0)).round())


def _outcome(*, h, k, l, omega0, reliable=True, T=200.0, entropy=0.0, verdict="resolved",
             amplitudes=None, pooled=False, n_members=None) -> dict:
    oc = {
        "detector_id": "det", "verdict": verdict, "disposition": verdict, "confidence": 0.9,
        "omega0": omega0, "gamma": 1.0, "q": [h, k, l], "T": T, "sample": "tavi",
        "assignment_entropy": entropy, "best_label": "phonon", "n_modes": 1,
        "calib": {"center": 0.0, "gauss_fwhm": 1.2, "lorentz_fwhm": 0.0},
        "interval": [omega0 - 0.4, omega0 + 0.4], "interval_reliable": bool(reliable),
    }
    if amplitudes is not None:
        oc["amplitudes"] = amplitudes
    if pooled:
        oc["pooled"] = True
        oc["n_members"] = n_members
    return oc


# Three in-window scans (K=L=0), scan 3 unreliable; scan 4 out-of-window (deviant K); a pooled
# master (scan 10, in-window) with stored amplitudes so its curve-only overlay builds.
_SPECS = [
    dict(scan=1, h=2.0, k=0.0, l=0.0, omega0=4.0, reliable=True, entropy=0.0, verdict="resolved"),
    dict(scan=2, h=2.1, k=0.0, l=0.0, omega0=5.0, reliable=True, entropy=0.3, verdict="resolved"),
    dict(scan=3, h=2.2, k=0.0, l=0.0, omega0=6.0, reliable=False, entropy=0.69, verdict="flagged"),
    dict(scan=4, h=2.05, k=0.5, l=0.0, omega0=5.5, reliable=True, entropy=0.1, verdict="resolved"),
]

_AMPL = {"phonon.h": 500.0, "bg.BG": 100.0}


def _make(make_ws) -> Workspace:
    scans, rows, data = {}, [], []
    for s in _SPECS:
        oc = _outcome(h=s["h"], k=s["k"], l=s["l"], omega0=s["omega0"], reliable=s["reliable"],
                      entropy=s["entropy"], verdict=s["verdict"], amplitudes=_AMPL)
        sid = f"tavi:{s['scan']}:det"
        scans[sid] = {"scan_id": sid, "scan_number": s["scan"], "scan_type": "measurement",
                      "status": "fitted", "detectors": {"det": oc}, "provenance": {"M": 200}}
        rows.append({"scan": s["scan"], "detector": "det", "sample": "tavi", "T": 200.0,
                     "h": s["h"], "k": s["k"], "l": s["l"], "status": "fitted"})
        data.append({"scan_number": s["scan"], "energy": list(_X), "counts": _counts(s["omega0"])})
    moc = _outcome(h=2.1, k=0.0, l=0.0, omega0=5.0, reliable=True, entropy=0.0, verdict="resolved",
                   amplitudes=_AMPL, pooled=True, n_members=3)
    scans["tavi:10:det"] = {"scan_id": "tavi:10:det", "scan_number": 10, "scan_type": "measurement",
                            "status": "fitted", "detectors": {"det": moc}, "provenance": {"M": 600}}
    rows.append({"scan": 10, "detector": "det", "sample": "tavi", "T": 200.0,
                 "h": 2.1, "k": 0.0, "l": 0.0, "status": "fitted", "pooled": True, "n_members": 3})
    root = make_ws(scans=scans, rows=rows, data_files=data)
    return Workspace.open(root)


# ---- ProjectionLegend always present (spec + construction level) ----------
def test_heatmap_and_dispersion_carry_projection(make_ws):
    ws = _make(make_ws)
    assert HeatmapViewModel(ws).spec.projection is not None
    assert DispersionViewModel(ws).spec.projection is not None


def test_dispersion_spec_rejects_missing_projection():
    # Construction-level enforcement: the plane declaration is mandatory (G9), never optional.
    with pytest.raises(ValueError):
        builders.dispersion_spec(points=[{"q": 2.0, "omega": 4.0, "yerr": 0.2}],
                                 projection=None, title="t", xlabel="x", ylabel="y")


# ---- exclusion count matches the constructed out-of-window count ----------
def test_exclusion_count_matches_constructed(make_ws):
    ws = _make(make_ws)
    # Both views agree the one deviant-K scan is out of the plane...
    hm, dv = HeatmapViewModel(ws), DispersionViewModel(ws)
    assert hm.excluded_count == 1 and dv.excluded_count == 1
    # ...and the heatmap renders that exact count as an on-screen annotation (the plane's honesty
    # note; the dispersion sibling carries the count in meta but draws no strip plane).
    notes = [a for a in hm.spec.annotations if "outside this projection" in a.text]
    assert len(notes) == 1
    assert notes[0].text.startswith("1 ")
    assert hm.spec.meta["excluded_count"] == 1 and dv.spec.meta["excluded_count"] == 1


# ---- no strip spans a gap -------------------------------------------------
def test_no_strip_spans_a_gap(make_ws):
    ws = _make(make_ws)
    strips = HeatmapViewModel(ws).spec.strips
    assert len(strips) >= 2
    extents = sorted((s.x_center - s.width / 2.0, s.x_center + s.width / 2.0) for s in strips)
    for (lo1, hi1), (lo2, hi2) in zip(extents, extents[1:]):
        assert hi1 <= lo2 + 1e-9, "a strip overlaps/bridges the gap to its neighbour"


# ---- property sweep over EVERY spec from EVERY view -----------------------
def _all_specs(ws):
    """Collect (name, PlotSpec) from every read-side VM: inspector overlay+residuals for a member
    and for the pooled master, the heatmap, the dispersion, and the waterfall in both modes."""
    out = []

    insp = InspectorViewModel(ws)
    insp.set_fit(ws.scans[0].detectors["det"])          # a reliable member: overlay + residuals
    if insp.state.main_spec is not None:
        out.append(("inspector.member.overlay", insp.state.main_spec))
    if insp.state.residuals_spec is not None:
        out.append(("inspector.member.residuals", insp.state.residuals_spec))

    pooled_fit = next(f for f in ws.results if f.pooled and f.omega0 is not None)
    insp2 = InspectorViewModel(ws)
    insp2.set_fit(pooled_fit)                            # the pooled master: curve-only
    if insp2.state.main_spec is not None:
        out.append(("inspector.pooled.overlay", insp2.state.main_spec))

    out.append(("heatmap", HeatmapViewModel(ws).spec))
    out.append(("dispersion", DispersionViewModel(ws).spec))

    wf = WaterfallViewModel(ws)
    wf.set_mode("waterfall")
    out.append(("waterfall.waterfall", wf.spec))
    wf.set_mode("comparison")
    out.append(("waterfall.comparison", wf.spec))
    return out


def test_honesty_property_sweep(make_ws):
    ws = _make(make_ws)
    specs = _all_specs(ws)
    assert specs, "the synthetic fixtures produced ZERO specs (vacuous sweep)"

    n_series = n_unreliable = n_entropy_notes = 0
    for name, spec in specs:
        d = spec.to_dict()
        series = d.get("series", [])
        n_series += len(series)

        # (1) an UNRELIABLE series never carries whiskers.
        for s in series:
            if s["state"] == State.UNRELIABLE.value:
                n_unreliable += 1
                assert s["yerr"] is None, f"{name}: unreliable series carries whiskers"

        # (2) an "(unreliable)" label (series / marker / annotation) never smuggles a number.
        texts = ([a["text"] for a in d.get("annotations", [])]
                 + [m["label"] for m in d.get("markers", []) if m.get("label")]
                 + [s["label"] for s in series if s.get("label")])
        for t in texts:
            if "(unreliable)" in t:
                n_unreliable += 1
                assert not any(ch.isdigit() for ch in t), (
                    f"{name}: an '(unreliable)' label carries a numeric interval: {t!r}")

        # (3) truth-gating: an entropy legend note implies a populated color_values on some series.
        entropy_notes = [a["text"] for a in d.get("annotations", [])
                         if "assignment entropy" in a["text"]]
        if entropy_notes:
            n_entropy_notes += len(entropy_notes)
            assert any(s["color_values"] is not None for s in series), (
                f"{name}: an entropy legend note with no color-coded series (empty claim)")

    # The sweep must have actually exercised the gates, not passed vacuously.
    assert n_series > 0, "no series across any view spec"
    assert n_unreliable > 0, "the fixture never produced an UNRELIABLE element to check"
    assert n_entropy_notes > 0, "the fixture never produced an entropy legend note to gate"
