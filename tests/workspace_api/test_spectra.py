"""Stored spectrum scaling and curve reads for the Scan(s) view."""
import copy
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from isar.analyze.model_manifest import MANIFEST_SCHEMA
from isar.workspace_api.scan_list import Fact, PooledRow, SpectrumRow, Status
from isar.workspace_api import (
    DEEP_DOES_NOT_REBUILD,
    NOT_JUDGED_TEXT,
    NO_ENERGY_ZERO,
    OUTDATED_TEXT,
    POOLED,
    FitView,
    NotDrawn,
    SpectraCache,
    nearest_power_of_ten,
    normalisation_reference,
    normalised_points,
    spectrum_bundles,
)
from isar.workspace_api.scan_list import scan_list
from isar.workspace_api.workspace import Workspace
from scans_view_fixture import (DEEP_DELTA, DEEP_NO_PARAMS, DEEP_REFUSED, DEEP_TRUSTED,
                                DROPOUT, FAST_ZERO_MODE, NO_MONITOR, ONE_MODE, STOKES_ONLY,
                                TWO_MODE, truth)
from isar.workspace_api.evaluate import evaluate_manifest


def _fit(*, zero_source="elastic_fit"):
    return FitView.from_outcome({
        "detector_id": "a01",
        "best_label": "background",
        "T": 100.0,
        "calib": {"center": 10.5, "zero_source": zero_source,
                  "gauss_fwhm": 1.0, "lorentz_fwhm": 0.0},
        "model_manifest": {
            "schema": MANIFEST_SCHEMA,
            "components": [{"type": "constant_background", "prefix": "bg"}],
            "parameters": {"bg.BG": 10.0},
        },
        "fast_pass": {"state": "finished", "models": []},
    }, scan_number=1, unit_id="exp:1:a01")


def _row(key="exp:1:a01", fit=None, *, deep=None):
    return SpectrumRow(
        key=key, set_key="exp:1", analyzer="a01", q=Fact(None, "rlu", "missing", "missing"),
        parsed=Status("ok", True), fast=Status("finished", True),
        deep=deep or Status("not run", False), file_name=None, file_path=None,
        n_points=4, fit=_fit() if fit is None else fit)


def _workspace(unit):
    return SimpleNamespace(
        card=SimpleNamespace(policy=None), warnings=[],
        unit_data=lambda key: unit[key] if isinstance(unit, dict) else unit,
    )


def _unit(*, axis="needs_zero"):
    counts = np.asarray([100.0, 0.0, 50.0, 30.0])
    monitor = np.asarray([200_000.0, 100_000.0, 0.0, 400_000.0])
    return SimpleNamespace(
        x=np.asarray([10.0, 11.0, 12.0, 13.0]), counts=counts, variance=counts.copy(),
        mask=np.asarray([True, True, False, False]),
        axis={"calibration_status": axis}, monitor=monitor,
        exposure=SimpleNamespace(source="monitor", reference=150_000.0,
                                 scale=np.asarray([4 / 3, 2 / 3, 0.0, 8 / 3])),
        exposure_stored=True,
    )


def test_normalised_points_and_nearest_power_of_ten_keep_poisson_zero():
    counts = np.asarray([100.0, 0.0, 50.0, 30.0])
    monitor = np.asarray([200_000.0, 100_000.0, 0.0, 400_000.0])
    drawable, values, errors = normalised_points(counts, counts, monitor, 100_000.0)

    np.testing.assert_array_equal(drawable, [True, True, False, True])
    np.testing.assert_allclose(values[[0, 1, 3]], [50.0, 0.0, 7.5])
    np.testing.assert_allclose(errors[[0, 1, 3]], [5.00124984378905, 0.0, 1.3693577417899239])
    assert np.isnan(values[2]) and np.isnan(errors[2])
    assert nearest_power_of_ten(290_000) == 100_000
    assert nearest_power_of_ten(320_000) == 1_000_000
    assert nearest_power_of_ten(10 ** 5.5) == 1_000_000
    with pytest.raises(ValueError):
        nearest_power_of_ten(0)


def test_bundle_scales_data_curves_and_exposure_aware_residuals():
    unit = _unit()
    ws = _workspace(unit)
    row = _row()
    bundle = spectrum_bundles(ws, [row])[0]

    raw = bundle.scaled("raw")
    assert raw.y_unit == "Counts"
    np.testing.assert_allclose(raw.x, [-0.5, 0.5, 1.5, 2.5])
    np.testing.assert_allclose(raw.yerr, [10.0, 0.0, np.sqrt(50.0), np.sqrt(30.0)])
    np.testing.assert_array_equal(raw.kept, [True, True, False, False])
    curves = raw.passes["fast"]
    assert not isinstance(curves, NotDrawn)
    np.testing.assert_allclose(curves.total, 10.0)
    np.testing.assert_allclose(curves.residuals, unit.counts - 10.0 * unit.exposure.scale)
    assert isinstance(raw.passes["deep"], NotDrawn)

    normalized = bundle.scaled("normalised", 100_000.0)
    assert normalized.y_unit == "Counts per 10⁵ monitor"
    np.testing.assert_allclose(normalized.x, [-0.5, 0.5, 2.5])
    np.testing.assert_allclose(normalized.y, [50.0, 0.0, 7.5])
    np.testing.assert_array_equal(normalized.kept, [True, True, False])
    np.testing.assert_allclose(normalized.passes["fast"].total, 10.0 * 100_000 / 150_000)
    np.testing.assert_allclose(
        normalized.passes["fast"].residuals,
        (unit.counts[[0, 1, 3]] - 10 * unit.exposure.scale[[0, 1, 3]])
        * 100_000 / unit.monitor[[0, 1, 3]],
    )


@pytest.mark.parametrize("source", ["elastic_fit", "pipeline_zero", "interpolated", None])
def test_needs_zero_uses_recorded_calibration_sources(source):
    bundle = spectrum_bundles(_workspace(_unit()), [_row(fit=_fit(zero_source=source))])[0]
    if source is None:
        assert bundle.not_drawn == NotDrawn(NO_ENERGY_ZERO, "no energy zero yet")
    else:
        assert bundle.not_drawn is None
        np.testing.assert_allclose(bundle.energy, [-0.5, 0.5, 1.5, 2.5])


def test_missing_monitor_cache_retries_a_changed_unit(scans_view_ws, monkeypatch):
    import json
    from isar.session.unit_data import unit_data_path

    ws = Workspace.open(scans_view_ws)
    cache = SpectraCache()
    original = ws.unit_data
    reads = []

    def read(key):
        reads.append(key)
        return original(key)

    monkeypatch.setattr(ws, "unit_data", read)
    assert normalisation_reference(ws, [NO_MONITOR], cache=cache) is None
    ws.refresh()
    assert normalisation_reference(ws, [NO_MONITOR], cache=cache) is None
    assert reads == [NO_MONITOR]

    path = unit_data_path(ws._data_dir, *ws._split_unit_id(NO_MONITOR))
    with path.open("r", encoding="utf-8", newline="") as source:
        payload = json.load(source)
    payload["monitor"] = [100_000.0] * len(payload["counts"])
    with path.open("w", encoding="utf-8", newline="\n") as target:
        json.dump(payload, target)
    assert normalisation_reference(ws, [NO_MONITOR], cache=cache) == 100_000.0
    assert reads == [NO_MONITOR, NO_MONITOR]


def test_revision_is_keyed_and_cache_reuses_only_the_same_bundle():
    first, same_data_different_key = _row(), _row(key="exp:1:a02")
    units = {first.key: _unit(), same_data_different_key.key: _unit()}
    ws = _workspace(units)
    cache = SpectraCache()
    one = spectrum_bundles(ws, [first], cache=cache)[0]
    assert spectrum_bundles(ws, [first], cache=cache)[0] is one
    two = spectrum_bundles(ws, [same_data_different_key], cache=cache)[0]
    assert two.revision != one.revision

    no_zero = spectrum_bundles(_workspace(_unit(axis="needs_conversion_and_zero")), [_row()])[0]
    assert no_zero.not_drawn == NotDrawn(NO_ENERGY_ZERO, "no energy zero yet")
    assert no_zero.summaries["fast"].state == "finished"

    pooled = PooledRow(
        key="exp:1:master", label="pooled master", temperature=Fact(None, "K", "missing", "missing"),
        q=Fact(None, "rlu", "missing", "missing"), n_members=2,
        fast=Status("finished", True), deep=Status("not run", False), fit=None)
    pooled_bundle = spectrum_bundles(ws, [pooled])[0]
    assert pooled_bundle.not_drawn == NotDrawn(POOLED, "pooled master")
    assert pooled_bundle.summaries["fast"].state == "finished"


def test_normalisation_reference_uses_kept_positive_monitors_only():
    a = _unit()
    b = SimpleNamespace(
        x=np.arange(3), counts=np.ones(3), variance=np.ones(3), mask=np.ones(3, bool),
        monitor=np.asarray([310_000.0, 330_000.0, 290_000.0]),
        exposure=SimpleNamespace(scale=np.ones(3)),
    )
    assert normalisation_reference(_workspace({"a": a, "b": b}), ["a", "b"]) == 100_000


def _fixture_rows(workspace):
    snapshot = scan_list(workspace)
    return {row.key: row for scan_set in snapshot.sets for row in scan_set.spectra}


def test_shared_workspace_exposure_scaling_tables_and_curve_residuals(scans_view_ws_readonly):
    ws = Workspace.open(scans_view_ws_readonly)
    rows = _fixture_rows(ws)
    selected = [rows[key] for key in (ONE_MODE, TWO_MODE, DROPOUT, NO_MONITOR)]
    bundles = {bundle.key: bundle for bundle in spectrum_bundles(ws, selected)}

    one_truth = truth(ONE_MODE)
    one = bundles[ONE_MODE]
    raw = one.scaled("raw")
    np.testing.assert_array_equal(one.counts, one_truth.counts)
    np.testing.assert_array_equal(one.monitor, one_truth.monitor)
    np.testing.assert_array_equal(raw.yerr, np.sqrt(one_truth.counts))
    expected_raw_residuals = one_truth.counts - one_truth.rate * one_truth.exposure_scale
    np.testing.assert_allclose(raw.passes["fast"].residuals, expected_raw_residuals,
                               rtol=1e-8, atol=1e-8)

    reference = normalisation_reference(ws, [ONE_MODE, TWO_MODE])
    normalized = one.scaled("normalised", reference)
    drawable = one_truth.monitor > 0
    expected_y = one_truth.counts[drawable] * reference / one_truth.monitor[drawable]
    expected_error = (reference / one_truth.monitor[drawable]) * np.sqrt(
        one_truth.counts[drawable] + one_truth.counts[drawable] ** 2 /
        one_truth.monitor[drawable])
    np.testing.assert_allclose(normalized.y, expected_y)
    np.testing.assert_allclose(normalized.yerr, expected_error)
    expected_normalised_residuals = (
        expected_raw_residuals[drawable] * reference / one_truth.monitor[drawable])
    np.testing.assert_allclose(normalized.passes["fast"].residuals,
                               expected_normalised_residuals, rtol=1e-8, atol=1e-8)

    two_modes = bundles[TWO_MODE].summaries["fast"].modes
    assert [mode.number for mode in two_modes] == [1, 2]
    np.testing.assert_allclose([mode.energy.value for mode in two_modes], [2.5, 5.5])
    two_deep = bundles[TWO_MODE].summaries["deep"]
    assert two_deep.modes == ()

    dropout = bundles[DROPOUT]
    assert dropout.counts[30] == 0 and not dropout.kept[30]
    assert truth(DROPOUT).x[30] not in dropout.scaled("normalised", reference).x
    no_monitor = bundles[NO_MONITOR]
    assert no_monitor.exposure_source == "time"
    assert no_monitor.scaled("normalised", reference).not_drawn.code == "no_monitor"


def test_deep_curve_uses_chosen_manifest_and_missing_manifest_keeps_table(scans_view_ws_readonly):
    ws = Workspace.open(scans_view_ws_readonly)
    rows = _fixture_rows(ws)

    delta = rows[DEEP_DELTA]
    delta_bundle = spectrum_bundles(ws, [delta])[0]
    delta_curves = delta_bundle.scaled("raw").passes
    assert not isinstance(delta_curves["deep"], NotDrawn)
    assert not np.allclose(delta_curves["fast"].total, delta_curves["deep"].total)
    delta_fit = delta.fit
    delta_manifest = truth(DEEP_DELTA).deep["chosen"]["model_manifest"]
    expected, _parts = evaluate_manifest(
        delta_manifest, delta_fit.resolution_context(), delta_bundle.fine_energy)
    np.testing.assert_allclose(delta_curves["deep"].total, expected, rtol=1e-9, atol=1e-9)

    # Clone the stored outcome, then remove only the deep model manifest as legacy records do.
    stokes = rows[STOKES_ONLY]
    outcome = copy.deepcopy(stokes.fit._outcome)
    outcome["fast_pass"]["arena_fingerprint"] = "shared-arena"
    outcome["deep_pass"]["arena_fingerprint"] = "shared-arena"
    tied = copy.deepcopy(outcome["deep_pass"]["chosen"]["model_manifest"])
    tied["components"][0]["peak"] = "stokes+astokes"
    outcome["deep_pass"]["chosen"]["model_manifest"] = tied
    fit_with_distinct_deep_peak = FitView.from_outcome(
        outcome, scan_number=stokes.fit.scan_number, unit_id=stokes.key)
    changed_row = replace(stokes, fit=fit_with_distinct_deep_peak)
    changed_bundle = spectrum_bundles(ws, [changed_row])[0]
    changed_curves = changed_bundle.scaled("raw").passes
    assert not np.allclose(changed_curves["fast"].total, changed_curves["deep"].total)
    assert (fit_with_distinct_deep_peak.fast_pass["arena_fingerprint"]
            == fit_with_distinct_deep_peak.deep_pass["arena_fingerprint"])
    assert changed_bundle.summaries["deep"].unjudged_text == NOT_JUDGED_TEXT
    expected, _parts = evaluate_manifest(
        tied, fit_with_distinct_deep_peak.resolution_context(),
        changed_bundle.fine_energy)
    np.testing.assert_allclose(changed_curves["deep"].total, expected, rtol=1e-9, atol=1e-9)

    legacy = copy.deepcopy(outcome)
    legacy["deep_pass"]["chosen"].pop("model_manifest")
    legacy_fit = FitView.from_outcome(
        legacy, scan_number=stokes.fit.scan_number, unit_id=stokes.key)
    legacy_bundle = spectrum_bundles(ws, [replace(stokes, fit=legacy_fit)])[0]
    assert legacy_bundle.summaries["deep"].modes
    unavailable = legacy_bundle.scaled("raw").passes["deep"]
    assert isinstance(unavailable, NotDrawn)
    assert unavailable.code == DEEP_DOES_NOT_REBUILD
    assert "peak choice" in unavailable.text


def test_fixture_deep_labels_trust_and_refusal_are_preserved(scans_view_ws_readonly):
    ws = Workspace.open(scans_view_ws_readonly)
    rows = _fixture_rows(ws)
    bundles = {bundle.key: bundle for bundle in spectrum_bundles(
        ws, [rows[key] for key in (DEEP_DELTA, DEEP_TRUSTED, DEEP_REFUSED,
                                   DEEP_NO_PARAMS, FAST_ZERO_MODE)])}

    outdated = bundles[DEEP_DELTA].summaries["deep"]
    assert outdated.current is False and outdated.outdated_text == OUTDATED_TEXT

    trusted = bundles[DEEP_TRUSTED].summaries["deep"]
    assert trusted.trusted is True and trusted.label == "1 DHO"
    assert trusted.modes[0].energy.kind == "minos"
    assert trusted.modes[0].energy.value == 4.0
    assert trusted.modes[0].energy.lo == 3.8
    assert trusted.modes[0].energy.hi == 4.25
    assert trusted.modes[0].linewidth.lo == pytest.approx(0.3)
    assert trusted.modes[0].linewidth.hi == pytest.approx(0.55)
    refused = bundles[DEEP_REFUSED]
    assert refused.summaries["deep"].state == "refused"
    assert refused.summaries["deep"].present
    assert refused.scaled("raw").passes["deep"].code == "no_pass"
    no_params = bundles[DEEP_NO_PARAMS]
    assert no_params.summaries["deep"].modes == ()
    assert isinstance(no_params.scaled("raw").passes["deep"], NotDrawn)
    zero_mode = bundles[FAST_ZERO_MODE].summaries["fast"]
    assert zero_mode.modes == ()
