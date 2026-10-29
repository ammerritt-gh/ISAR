"""The BL43LXU central QE on the saved-manifest route (S1U4): a fitted and a pinned ``central_qe``
round-trip through the manifest, and a refused pinned width. Synthetic spectra at test time on the
committed a06 curve; no real data."""
import types

import numpy as np
import pytest

from isar.analyze import model_manifest as mm
from isar.analyze.components import CentralQE, CompositeModel, ConstantBackground, Elastic, PhononDHO
from isar.analyze.context import ScanContext
from isar.session.predictive_artifact import model_manifest_of
from isar.synth import bl43_curves

CURVE = bl43_curves.curve("a06")
X = np.linspace(-10.0, 10.0, 81)


def _ctx(counts):
    return ScanContext(x=X, counts=np.asarray(counts, dtype=float),
                       mask=np.ones(X.size, dtype=bool), T=97.0, center=0.0, gauss_fwhm=1.13,
                       lorentz_fwhm=0.0, instrument="BL43LXU", energy_status="absolute",
                       resolution_curve=CURVE)


def _by_hyp(composite, label):
    return {label: types.SimpleNamespace(build=lambda: composite)}


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
