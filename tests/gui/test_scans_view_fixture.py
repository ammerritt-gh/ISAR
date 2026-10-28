"""The shared scans-view fixture exercises the persisted workspace read path."""
import numpy as np

from isar.workspace_api.scan_list import scan_list
from isar.workspace_api.workspace import Workspace
from scans_view_fixture import (DEEP_DELTA, DEEP_NO_PARAMS, DEEP_REFUSED, DEEP_TRUSTED,
                                DROPOUT, FAST_ZERO_MODE, FITTED, HELD, NO_MONITOR, NO_Q,
                                NO_ZERO, OUTDATED, SPECTRA_A, SPECTRA_B, STOKES_ONLY,
                                TWO_MODE, ONE_MODE, truth)


def test_fixture_builds_the_intended_scan_and_fit_rows(scans_view_ws_readonly):
    ws = Workspace.open(scans_view_ws_readonly)
    snapshot = scan_list(ws)
    spectra = {row.key: row for scan_set in snapshot.sets for row in scan_set.spectra}

    assert set(spectra) == set(SPECTRA_A) | set(SPECTRA_B) | {NO_MONITOR}
    assert snapshot.pooled == ()
    assert len(snapshot.sets) == 3
    assert spectra[NO_Q].q.value is None
    assert spectra[OUTDATED].fast.current is False
    assert spectra[HELD].fast.stored is False
    assert spectra[NO_ZERO].fast.stored is False
    assert spectra[DEEP_DELTA].deep.current is False
    assert spectra[DEEP_REFUSED].deep.word == "refused"
    fitted_keys = {row.key for scan_set in snapshot.sets for row in scan_set.spectra
                   if row.fit is not None}
    assert fitted_keys == set(FITTED)


def test_fixture_keeps_counts_exposure_and_models_tied_to_truth(scans_view_ws_readonly):
    ws = Workspace.open(scans_view_ws_readonly)
    fits = {fit.unit_id: fit for fit in ws.results}

    for key in (ONE_MODE, TWO_MODE):
        expected = truth(key)
        curve = ws.evaluate_fit(fits[key], x_eval=expected.x)
        np.testing.assert_allclose(curve.total, expected.rate, rtol=1e-9, atol=1e-9)
        assert fits[key].model_manifest == expected.fast_manifest
        unit = ws.unit_data(key)
        np.testing.assert_array_equal(unit.x, expected.x)
        np.testing.assert_array_equal(unit.counts, expected.counts)
        np.testing.assert_array_equal(unit.yerr, np.sqrt(expected.counts))

    one_mode = ws.unit_data(ONE_MODE)
    assert one_mode.axis["calibration_status"] == "absolute"
    dropped = ws.unit_data(DROPOUT)
    assert not dropped.mask[30] and dropped.counts[30] == 0

    for key in (ONE_MODE, DEEP_DELTA, DEEP_TRUSTED, STOKES_ONLY, FAST_ZERO_MODE):
        chosen = fits[key].deep_pass["chosen"]
        assert chosen["label"] == truth(key).deep["chosen"]["label"]
        assert chosen["model_manifest"] == truth(key).deep["chosen"]["model_manifest"]
    assert fits[ONE_MODE].deep_pass["trusted"] is False
    assert fits[ONE_MODE].deep_pass["reason"]
    assert fits[DEEP_TRUSTED].deep_pass["trusted"] is True
    assert truth(TWO_MODE).fast_manifest["parameters"]["phonon.omega0"] == 5.5
    assert truth(TWO_MODE).fast_manifest["parameters"]["phonon2.omega0"] == 2.5
    assert truth(STOKES_ONLY).fast_manifest["components"][0]["peak"] == "stokes"
    assert not any(component["type"] == "phonon_dho"
                   for component in truth(FAST_ZERO_MODE).fast_manifest["components"])
    no_params = fits[DEEP_NO_PARAMS].deep_pass["chosen"]
    assert no_params["params"] is None and no_params["model_manifest"]["parameters"] == {}

    no_zero = ws.unit_data(NO_ZERO)
    assert no_zero.axis["calibration_status"] == "needs_zero"
    no_monitor = ws.unit_data(NO_MONITOR)
    assert no_monitor.counts.size == 8
