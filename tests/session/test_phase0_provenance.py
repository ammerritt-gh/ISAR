"""Phase 0 (steering) — interfaces + provenance plumbing, additive/no-behavior-change.

Contract tests (docs/MEASUREMENT_STEERING.md §4.4): portable intensity round-trip, add_point
provenance storage, backward-compatible state.json, and the new optional-field defaults.
"""
import types
from dataclasses import fields

import numpy as np
import pytest

from isar.analyze.context import ScanContext
from isar.analyze.loop import StructureFitResult
from isar.schema.card import CardState
from isar.schema.steering import IntensityMeasure, ParamConstraints
from isar.session import session_prior as SP


# --------------------------------------------------------------------------- IntensityMeasure
def test_intensity_measure_expected_amplitude_is_exposure_portable():
    """value = A/exposure; predicting at a different exposure recovers A·(e_query/e_store)."""
    amp, e_store, e_query = 42.0, 1.0e6, 3.0e6
    im = IntensityMeasure(value=amp / e_store, exposure=e_store, counting_mode="monitor",
                          gauss_fwhm=1.5, lorentz_fwhm=0.0, instrument="S30")
    assert im.expected_amplitude(e_query) == pytest.approx(amp * e_query / e_store)
    assert im.expected_amplitude(e_store) == pytest.approx(amp)


def test_intensity_measure_dict_roundtrip():
    im = IntensityMeasure(value=2.0, exposure=1e6, counting_mode="monitor", gauss_fwhm=1.5,
                          lorentz_fwhm=0.2, instrument="S30", pooled=True, n_members=4)
    assert IntensityMeasure.from_dict(im.to_dict()) == im
    # tolerant of extra keys (a future field on an old point)
    d = im.to_dict(); d["future_field"] = 1
    assert IntensityMeasure.from_dict(d) == im


# --------------------------------------------------------------------------- driver helper
def test_intensity_measure_from_fit_normalizes_by_exposure():
    from isar.session import driver, scan_delivery
    rec = types.SimpleNamespace(normalization=types.SimpleNamespace(
        counting_mode="monitor", monitor=np.array([1e6, 1e6, 2e6, 1e6]), time=None))
    ctx = types.SimpleNamespace(mask=np.array([True, True, False, True]),
                                gauss_fwhm=1.5, lorentz_fwhm=0.0, instrument="S30")
    # Post-H4 a mode record carries both: `amplitude` is the MEASURED Stokes strength A·(n+1),
    # `amplitude_intrinsic` the fitted A. The measure is built from the intrinsic one.
    fit = {"omega0": 5.0, "modes": [{"omega0": 5.0, "amplitude": 96.0,
                                     "amplitude_intrinsic": 42.0}]}
    im = scan_delivery._intensity_measure(rec, ctx, fit)
    # exposure = median monitor over KEPT points [1e6, 1e6, 1e6] = 1e6
    assert im["exposure"] == pytest.approx(1e6)
    assert im["value"] == pytest.approx(42.0 / 1e6)               # intrinsic, not the 96.0
    assert im["counting_mode"] == "monitor"
    assert im["basis"] == "intrinsic_amplitude_per_exposure"
    assert IntensityMeasure.from_dict(im).expected_amplitude(2e6) == pytest.approx(84.0)


def test_intensity_measure_refuses_a_mode_record_without_the_intrinsic_amplitude():
    """H4/H1: `amplitude` alone is the measured strength. Reading it as intrinsic would seed the
    branch model with an occupation-inflated anchor, so the producer declines to interpret it."""
    from isar.session import scan_delivery
    rec = types.SimpleNamespace(normalization=types.SimpleNamespace(
        counting_mode="monitor", monitor=np.array([1e6]), time=None))
    ctx = types.SimpleNamespace(mask=np.array([True]), gauss_fwhm=1.5, lorentz_fwhm=0.0,
                                instrument="S30")
    fit = {"omega0": 5.0, "modes": [{"omega0": 5.0, "amplitude": 96.0}]}

    assert scan_delivery._intensity_measure(rec, ctx, fit) is None


def test_intensity_measure_none_when_no_amplitude_or_exposure():
    from isar.session import driver, scan_delivery
    ctx = types.SimpleNamespace(mask=np.array([True]), gauss_fwhm=1.5, lorentz_fwhm=0.0,
                                instrument="S30")
    rec = types.SimpleNamespace(normalization=types.SimpleNamespace(
        counting_mode="monitor", monitor=np.array([1e6]), time=None))
    assert scan_delivery._intensity_measure(rec, ctx, {"omega0": None, "modes": None}) is None
    # no usable exposure → None
    rec0 = types.SimpleNamespace(normalization=types.SimpleNamespace(
        counting_mode="monitor", monitor=np.array([0.0]), time=None))
    fit = {"omega0": 5.0, "modes": [{"omega0": 5.0, "amplitude": 42.0}]}
    assert scan_delivery._intensity_measure(rec0, ctx, fit) is None


# --------------------------------------------------------------------------- add_point provenance
def test_add_point_stores_gamma_and_intensity_when_given():
    st = CardState()
    im = IntensityMeasure(value=2.0, exposure=1e6, counting_mode="monitor", gauss_fwhm=1.5,
                          lorentz_fwhm=0.0, instrument="S30").to_dict()
    assert SP.add_point(st, scan_id="a", sample="S", T=100, q=[3, 0.1, 0], omega0=5.0,
                        modality="fold", verdict="resolved",
                        gamma=0.3, gamma_err=0.05, intensity=im) is True
    p = st.dispersion_prior[0]
    assert p["gamma"] == 0.3 and p["gamma_err"] == 0.05
    assert p["intensity"]["value"] == 2.0 and p["intensity"]["exposure"] == 1e6


def test_add_point_omits_new_fields_when_none():
    st = CardState()
    SP.add_point(st, scan_id="b", sample="S", T=100, q=[3, 0.1, 0], omega0=5.0,
                 modality="fold", verdict="resolved")
    p = st.dispersion_prior[0]
    assert "gamma" not in p and "gamma_err" not in p and "intensity" not in p


def test_add_point_gate_unchanged_with_new_kwargs():
    st = CardState()
    # fork still rejected even with provenance supplied
    assert SP.add_point(st, scan_id="c", sample="S", T=100, q=[3, 0.1, 0], omega0=5.0,
                        modality="fork", verdict="resolved", gamma=0.3) is False
    assert st.dispersion_prior == []


# --------------------------------------------------------------------------- CardState back-compat
def test_cardstate_branch_models_default_and_roundtrip():
    st = CardState()
    assert st.branch_models == []
    st.branch_models.append({"branch_id": "acoustic", "sample": "S"})
    d = st.to_dict()
    assert d["branch_models"] == [{"branch_id": "acoustic", "sample": "S"}]
    assert CardState.from_dict(d).branch_models == st.branch_models


def test_old_state_json_without_branch_models_loads():
    old = {"exclusions": [], "corrections": [], "dispersion_prior": []}
    st = CardState.from_dict(old)
    assert st.branch_models == []      # defaulted, not an error


# --------------------------------------------------------------------------- new field defaults
def test_structure_fit_result_new_fields_default_unconstrained():
    d = {f.name: f.default for f in fields(StructureFitResult)}
    assert d["fit_kind"] == "unconstrained"
    assert d["prior_penalty"] is None
    assert d["posterior_cost"] is None


def test_scancontext_constraints_defaults_none():
    d = {f.name: f.default for f in fields(ScanContext)}
    assert d["constraints"] is None


def test_param_constraints_empty_and_roundtrip():
    assert ParamConstraints().is_empty() is True
    pc = ParamConstraints(soft={"phonon.Gamma": (0.3, 0.1)}, hard={"phonon.A": 5.0})
    assert pc.is_empty() is False
    rt = ParamConstraints.from_dict(pc.to_dict())
    assert rt.soft == {"phonon.Gamma": (0.3, 0.1)} and rt.hard == {"phonon.A": 5.0}
