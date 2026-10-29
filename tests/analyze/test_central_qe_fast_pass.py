"""The BL43LXU fast pass with the central QE (S1U2): every starting model carries elastic + QE, a
zero-height QE collapses through the twin rule to an on-demand reduced model, and each group prices
one label. A BL43LXU context on the committed a06 curve, a short window, mode ceiling 1; no real data.
"""
import json
import math
from types import SimpleNamespace

import numpy as np
import pytest
from scipy.special import logsumexp

from isar.analyze import fast_pass, prior
from isar.analyze.components import CentralQE, CompositeModel, ConstantBackground, Elastic, PhononDHO
from isar.analyze.context import ScanContext
from isar.session import scan_delivery
from isar.synth import bl43_curves

CURVE = bl43_curves.curve("a06")
X = np.arange(-5.0, 10.0, 0.25)
SETTINGS = fast_pass.FastPassSettings(mode_ceiling=1)


def _ctx(counts):
    return ScanContext(x=X, counts=counts, mask=np.ones(X.size, bool), T=97.0, center=0.0,
                       gauss_fwhm=0.0, lorentz_fwhm=0.0, instrument="BL43LXU",
                       energy_status="absolute", resolution_curve=CURVE)


def _mean(components, theta):
    return CompositeModel(components).evaluate(X, theta, _ctx(np.ones(X.size)))


@pytest.fixture(scope="module")
def no_qe():
    """Noise-free elastic + one phonon + background, no QE in the truth."""
    ctx = _ctx(_mean([PhononDHO(), Elastic(), ConstantBackground()], [5.0, 0.6, 40.0, 200.0, 3.0]))
    return ctx, fast_pass.run_fast_pass(ctx, settings=SETTINGS)


def test_a_zero_qe_collapses_to_the_model_without_it(no_qe):
    _ctx_, res = no_qe
    by_id = {m.model_id: m for m in res.models}
    starting = [m for m in res.models if not m.on_demand]
    assert starting and all(m.qe and m.elastic for m in starting)          # V(k) = 2: bg or not
    assert all(m.ln_model_prior == pytest.approx(-math.log(2) - m.n_peaks * math.log(2))
               for m in res.models)
    twins = [m for m in starting if m.evidence_status == "degenerate_twin"
             and m.reason.startswith("zero qe")]
    assert twins
    for m in twins:
        target = by_id[m.twin_of]
        assert target.on_demand and target.removed == ("qe",) and not target.qe
        assert target.parent == m.parent == m.label
    sel = res.selected
    assert sel.label == "elastic+stokes+bg" and not sel.qe and sel.on_demand
    assert not any(n.startswith("qe.") for n in sel.free)
    assert res.qe is None and res.preferred_count == 1


def test_a_central_qe_is_fitted_and_recorded():
    fwhm = prior.resolution_fwhm(_ctx(np.ones(X.size)))
    truth = 2.0 * fwhm
    ctx = _ctx(_mean([Elastic(), CentralQE(), ConstantBackground()], [150.0, truth, 80.0, 20.0]))
    res = fast_pass.run_fast_pass(ctx, settings=SETTINGS)
    sel = res.selected
    assert res.preferred_count == 0 and sel.qe and not sel.on_demand
    q = res.qe
    assert q is not None and not q.pinned
    assert q.width == pytest.approx(truth, rel=0.10)
    point = sel.map_params
    assert q.height == point["qe.h"] and q.width == point["qe.width"]
    assert q.weight == CentralQE().spectral_weight({"h": point["qe.h"], "width": point["qe.width"]}, ctx)
    assert q.window == (-5.0, 9.75)
    rec = json.loads(json.dumps(scan_delivery._fast_pass_record(res)))
    assert rec["qe"]["width"] == q.width and rec["qe"]["window"] == [-5.0, 9.75]
    chosen = next(m for m in rec["models"] if m["model_id"] == rec["selected_model_id"])
    assert chosen["qe"] is True and chosen["on_demand"] is False and chosen["removed"] == []
    assert chosen["superseded"] is False and chosen["parent"] == chosen["label"]


def test_the_qe_starting_widths_lie_inside_its_support():
    eng = fast_pass._Engine(_ctx(np.full(X.size, 20.0)), SETTINGS)
    eng._choose_qe_start()
    widths = eng.qe_start_widths
    assert widths and min(widths) >= eng.fwhm and len(set(widths)) == len(widths)
    assert eng.qe_start in widths


def test_a_near_zero_elastic_line_is_never_removed_on_bl43lxu():
    # A phonon and background, no elastic line and no QE in the truth: the elastic height and the QE
    # height both fit near zero. The elastic line stays in every model; the zero QE collapses to the
    # model without the QE through the zero-QE twin (3b), never through a zero-elastic twin.
    ctx = _ctx(_mean([PhononDHO(), ConstantBackground()], [5.0, 0.6, 40.0, 3.0]))
    res = fast_pass.run_fast_pass(ctx, settings=SETTINGS)
    assert all(m.elastic and "elastic" not in m.removed for m in res.models)
    assert not any(m.reason.startswith("zero elastic") for m in res.models)
    assert any(m.evidence_status == "degenerate_twin" and m.reason.startswith("zero qe")
               for m in res.models)


# ------------------------------------------------------------------------- the group rule, pure
def _e(label, parent, ln_z, status="ok", count=1):
    return SimpleNamespace(label=label, parent=parent, ln_z=ln_z, status=status, count=count,
                           ln_model_prior=-math.log(2) - count * math.log(2), ln_like=ln_z,
                           k_free=4)


def test_one_label_per_group_is_priced():
    a0, a1 = _e("A", "A", -10.0), _e("A", "A", -11.0)        # a starting model, two basins
    r = _e("R", "A", -9.0)                                    # its reduced model, higher ln Z
    out = fast_pass._superseded([a0, a1, r])
    assert set(out) == {id(a0), id(a1)} and out[id(a0)] == "R"
    r_low = _e("R", "A", -12.0)                               # lower than the summed starting model
    assert set(fast_pass._superseded([a0, a1, r_low])) == {id(r_low)}
    # an unpriced starting model with two priced reduced models keeps the larger only
    u = _e("A", "A", None, status="degenerate_twin")
    r1, r2 = _e("R1", "A", -9.0), _e("R2", "A", -8.0)
    assert set(fast_pass._superseded([u, r1, r2])) == {id(u), id(r1)}
    # two starting labels sharing count, background and mask but not parent (off BL43LXU)
    s1, s2 = _e("stokes+bg", "stokes+bg", -5.0), _e("elastic+stokes+bg", "elastic+stokes+bg", -6.0)
    assert fast_pass._superseded([s1, s2]) == {}
    # a count's ln Z sums one term per group
    b = _e("B", "B", -7.0)
    entries = [a0, a1, r, b]
    tally = fast_pass._tally(entries, 10.0, fast_pass._superseded(entries))
    expected = logsumexp([r.ln_model_prior + r.ln_z, b.ln_model_prior + b.ln_z])
    assert tally.count_ln_z[1] == pytest.approx(expected)
    assert id(a0) not in tally.terms and id(a1) not in tally.terms


def test_the_delivery_never_scores_a_superseded_model(no_qe):
    _ctx_, res = no_qe
    gone = {m.label for m in res.models if m.superseded}
    assert gone and all(m.probability == 0.0 for m in res.models if m.superseded)
    rows = scan_delivery._candidate_models(res)
    assert not gone & set(rows) and not any(m.superseded for m in rows.values())
    sel = scan_delivery._selection_from_fast_pass(res, [m.fit for m in rows.values()], rows)
    assert not gone & set(sel.evidence.by_label())
    assert not gone & {s.label for s in sel.scores}
    assert sel.best == res.selected.label
