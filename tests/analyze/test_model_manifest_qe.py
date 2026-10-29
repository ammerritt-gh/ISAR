"""The BL43LXU central QE on the saved-manifest and verdict routes (S1U4): a fitted and a pinned
``central_qe`` round-trip through the manifest, a phonon-less BL43LXU winner takes the verdict's
phonon-less branch, and a non-BL43LXU winner with no mode does not. Synthetic spectra at test time
on the committed a06 curve; no real data."""
import types

import numpy as np
import pytest

from isar.analyze import loop, model_manifest as mm, select, verdict
from isar.analyze.components import CentralQE, CompositeModel, ConstantBackground, Elastic, PhononDHO
from isar.analyze.context import ScanContext
from isar.session.predictive_artifact import model_manifest_of
from isar.synth import bl43_curves

CURVE = bl43_curves.curve("a06")
X = np.linspace(-10.0, 10.0, 81)


def _ctx(counts, *, instrument="BL43LXU", x=X):
    counts = np.asarray(counts, dtype=float)
    return ScanContext(x=x, counts=counts, mask=np.ones(x.size, dtype=bool), T=97.0, center=0.0,
                       gauss_fwhm=1.13, lorentz_fwhm=0.0, instrument=instrument,
                       energy_status="absolute", resolution_curve=CURVE)


def _by_hyp(composite, label):
    return {label: types.SimpleNamespace(build=lambda: composite)}


def _result(label, params, *, n_modes=0):
    """A converged fit with ``n_modes`` modes (none here: the phonon-less winners)."""
    return loop.StructureFitResult(
        label=label, k=len(params), ndof=40, cost=40.0, cost_per_dof=1.0,
        optimizer_converged=True, covariance_ok=True, profile_ok=False, well_constrained=True,
        analysis_usable=True, params=dict(params), param_errors={n: None for n in params},
        gamma=float("nan"), gamma_err=float("nan"), omega0=float("nan"),
        omega0_err_hesse=float("nan"), message="ok", modes=[], n_modes=n_modes)


def _sel(best, scores=()):
    return select.Selection(scores=list(scores), best=best, survivors=[best], pooled=None,
                            modality="unpooled")


def _flat_plus_elastic(seed=3):
    """Seeded Poisson counts: a resolution-shaped central line on flat background, no phonon."""
    from isar.analyze.lineshapes import Voigt

    lam = 20.0 + Voigt(X, 300.0, 1.13, 0.0, 0.0)
    return np.random.default_rng(seed).poisson(lam).astype(float)


# ------------------------------------------------------------------------- manifest round-trip
@pytest.mark.parametrize("width", [None, 1.5])
def test_a_central_qe_manifest_rebuilds_the_composite_it_came_from(width):
    qe = CentralQE(width=width)
    composite = CompositeModel([PhononDHO(), Elastic(), qe, ConstantBackground()])
    names = composite.param_names()
    values = {"phonon.omega0": 4.0, "phonon.Gamma": 0.4, "phonon.h": 30.0, "elastic.h": 300.0,
              "bg.BG": 3.0, "qe.h": 12.0}
    if width is None:
        values["qe.width"] = 2.2
    best = types.SimpleNamespace(label="elastic+stokes+qe+bg",
                                 params={n: values[n] for n in names})
    manifest = model_manifest_of(best, _by_hyp(composite, "elastic+stokes+qe+bg"))
    item = next(c for c in manifest["components"] if c["type"] == "central_qe")
    assert item == ({"type": "central_qe", "prefix": "qe"} if width is None
                    else {"type": "central_qe", "prefix": "qe", "width": 1.5})
    rebuilt, theta = mm.compose_manifest(manifest)
    assert rebuilt.param_names() == names
    ctx = _ctx(np.ones(X.size))
    np.testing.assert_allclose(rebuilt.evaluate(X, theta, ctx),
                               composite.evaluate(X, [values[n] for n in names], ctx),
                               rtol=0, atol=1e-12)


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), "wide"])
def test_a_pinned_central_qe_width_must_be_positive_and_finite(bad):
    manifest = {"schema": mm.MANIFEST_SCHEMA, "parameters": {"qe.h": 1.0, "bg.BG": 1.0},
                "components": [{"type": "central_qe", "prefix": "qe", "width": bad},
                               {"type": "constant_background", "prefix": "bg"}]}
    with pytest.raises(mm.ModelManifestError) as err:
        mm.build_composite(manifest)
    assert err.value.reason_code == mm.MALFORMED_MANIFEST


# ---------------------------------------------------------------------- phonon-less verdicts
def test_a_phonon_less_bl43lxu_winner_takes_the_phonon_less_branch():
    """elastic + QE + background, no mode: not a GoF or excursion case, so the phonon-less branch
    decides. No LRT vs background exists for this label, so no elastic line is claimed."""
    counts = _flat_plus_elastic()
    params = {"elastic.h": 300.0, "qe.width": 3.0, "qe.h": 10.0, "bg.BG": 20.0}
    v = verdict.classify([_result("elastic+qe+bg", params)], _ctx(counts), _sel("elastic+qe+bg"))
    assert v.verdict == "no_signal" and v.confidence == 0.0
    assert any("without established significance" in r for r in v.reasons)
    assert "elastic+qe+bg won selection" in " ".join(v.reasons)


def test_a_qe_winner_without_an_elastic_line_is_not_background_alone():
    counts = _flat_plus_elastic()
    params = {"qe.width": 3.0, "qe.h": 10.0, "bg.BG": 20.0}
    v = verdict.classify([_result("qe+bg", params)], _ctx(counts), _sel("qe+bg"))
    assert v.verdict == "no_signal"
    assert not any("background alone" in r for r in v.reasons)


def test_a_mode_free_winner_off_bl43lxu_keeps_the_excursion_route():
    """A card-declared TAS quasi-elastic winner with no mode is not a BL43LXU phonon-less winner:
    with no excursion above background it still falls through to the step-6 'no excursion' null."""
    flat = np.full(X.size, 20.0)
    params = {"qe.QE": 5.0, "bg.BG": 20.0}
    v = verdict.classify([_result("qe+bg", params)], _ctx(flat, instrument="TAS"),
                         _sel("qe+bg"))
    assert v.verdict == "no_signal"
    assert any("no excursion above background" in r for r in v.reasons)
    assert not any("without established significance" in r for r in v.reasons)


# ------------------------------------------------------------------- rebuilding reduced models
def test_an_on_demand_reduced_bl43lxu_model_can_be_rebuilt_for_the_verdict():
    """The fast pass's on-demand reduced model (the QE removed) is not an enumerated structure, but
    the verdict rebuilds the curve it was fitted as, so the residual checks can see it."""
    params = {"phonon.omega0": 4.0, "phonon.Gamma": 0.4, "phonon.h": 30.0, "elastic.h": 300.0,
              "bg.BG": 3.0}
    curve = verdict._fitted_model("elastic+stokes+bg", params, _ctx(np.ones(X.size)))
    assert curve is not None and curve.shape == X.shape
