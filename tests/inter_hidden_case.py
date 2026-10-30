"""The three-spectrum hidden-mode case (C-hidden-fixture), built through the real Snapshot/Held/Term types.

A top-level helper (``tests/`` is on ``sys.path``): consumers import ``hidden_snapshot``, never copy it.
Every value is made up and generated at test time (hard rule 10). The middle spectrum y holds ONE summed DHO
exactly where its two T-neighbours carry two branches, b1 (strong) and b2 (weak) under it, so the counts fix
the total intensity and say almost nothing about how it divides between the two modes.

The spectrum builders below are a short duplicate of the ones in ``tests/analyze/test_coupled_refit.py``
(a test module, not importable as a helper).
"""
from __future__ import annotations

import numpy as np

from isar.analyze import catalogue
from isar.analyze.context import ScanContext
from isar.analyze.coupled.settings import InterSettings
from isar.analyze.coupled.types import Held, HeldMode, Snapshot, TermInput
from isar.analyze.fast_pass import FastPassSettings
from isar.session.inter_spectra.graph import Term

X = np.linspace(-6.0, 24.0, 241)
FWHM = 1.5
STRUCT = ("elastic+bg", "elastic+stokes+bg", "elastic+stokes2+bg", "elastic+stokes3+bg")
E_STRONG = 8.0                    # meV, where the strong mode sits and the weak one hides


class Prepared:                    # the prepared context shape the core reads (ctx, settings, structures)
    def __init__(self, ctx, ceiling):
        self.ctx, self.settings, self.structures = ctx, FastPassSettings(mode_ceiling=ceiling), STRUCT


def _ctx(counts):
    return ScanContext(x=X, counts=np.asarray(counts, float), mask=np.ones(X.size, bool), T=300.0, center=0.0,
                       gauss_fwhm=FWHM, lorentz_fwhm=0.0, instrument="S30", energy_status="absolute")


def _label(k):
    return "elastic+bg" if k == 0 else "elastic+stokes+bg" if k == 1 else f"elastic+stokes{k}+bg"


def _expected(modes, elastic=100.0, bg=2.0):
    ctx = _ctx(np.ones(X.size))
    hyp = next(h for h in catalogue.enumerate_count_structures(ctx, len(modes)) if h.label == _label(len(modes)))
    return hyp.build().evaluate(X, [v for m in modes for v in m] + [elastic, bg], ctx)


def _names(k):
    out = []
    for i in range(k):
        p = "phonon" if i == 0 else f"phonon{i + 1}"
        out += [f"{p}.omega0", f"{p}.Gamma", f"{p}.h"]
    return out + ["elastic.h", "bg.BG"]


def _record(modes, elastic=100.0, bg=2.0):
    k, names = len(modes), _names(len(modes))
    params = dict(zip(names, [v for m in modes for v in m] + [elastic, bg]))
    return {"selected_model_id": f"{_label(k)}#0", "count_probabilities": {str(k): 0.95},
            "models": [{"model_id": f"{_label(k)}#0", "label": _label(k), "n_peaks": k, "elastic": True,
                        "background": True, "delta_modes": [], "free": names, "map_params": params}],
            "peaks": [{"omega0": e, "Gamma": g, "height": h, "sigma_omega0": 0.1, "regime": "underdamped"}
                      for e, g, h in sorted(modes)]}


def _held(unit, modes, branches, *, guard=0.05, T=None):
    hm = tuple(HeldMode(b, e, g, h, guard, "solo", "determined_by_own_data", "underdamped", False)
               for (e, g, h), b in sorted(zip(modes, branches)))
    return Held(unit=unit, fit="fast", inputs=None, window=(0.75, 24.0), modes=hm, dormant=(), T=T)


def hidden_snapshot(w, *, strong=40.0, seed=3, asimov=False, dl=0.8, ceiling=2, b2_at=None) -> Snapshot:
    """Middle spectrum y (200 K): one DHO at 8.0 meV of height ``strong + w``; left x (100 K) and right z
    (300 K) hold b1 at 8.0 and b2 at 8.0 -+ ``dl`` of height ``w``; y's incoming fit is one mode matched to
    b1; an anchored batch. ``asimov``: the counts are the expected counts (noise free). ``b2_at``: both
    neighbours predict b2 at that energy instead (the opposite case, a branch whose intensity vanishes: the
    data are unchanged)."""
    own_true = [(E_STRONG, 0.4, strong + w)]
    counts = _expected(own_true) if asimov else np.random.default_rng(seed).poisson(_expected(own_true))
    prep = Prepared(_ctx(counts), ceiling)
    left = E_STRONG - dl if b2_at is None else b2_at
    right = E_STRONG + dl if b2_at is None else b2_at
    xm = [(E_STRONG, 0.4, strong), (left, 0.4, w)]
    zm = [(E_STRONG, 0.4, strong), (right, 0.4, w)]
    terms = (TermInput(Term("T-change", ("x", "y"), (100.0,), None, None),
                       (_held("x", xm, ["b1", "b2"], T=100.0),)),
             TermInput(Term("T-change", ("y", "z"), (100.0,), None, None),
                       (_held("z", zm, ["b1", "b2"], T=300.0),)))
    return Snapshot(snapshot_id="b1:round1", stage_id="b1:round1", unit="y", fit_id="f00000000000a", context=prep,
                    own=_held("y", own_true, ["b1"], T=200.0), own_record=_record(own_true), terms=terms,
                    settings=InterSettings(), batch_kind="anchored")
