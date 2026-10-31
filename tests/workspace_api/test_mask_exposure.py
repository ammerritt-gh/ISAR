"""A new mask can change a fit's reference monitor: curves retain their recorded scale."""
import copy
from dataclasses import replace

import numpy as np

from scans_view_fixture import ONE_MODE
from isar.workspace_api import FitView, Workspace
from isar.workspace_api.scan_list import scan_list
from isar.workspace_api.spectra import spectrum_bundles


def test_each_pass_uses_its_own_recorded_exposure(scans_view_ws_readonly):
    ws = Workspace.open(scans_view_ws_readonly)
    row = scan_list(ws).spectrum(ONE_MODE)
    original = spectrum_bundles(ws, [row])[0]
    outcome = copy.deepcopy(row.fit._outcome)
    for name, factor in (('fast_pass', 2), ('deep_pass', 3)):
        outcome[name]['exposure_basis'] = {'source': 'monitor', 'reference': original.m_ref * factor}
    fit = FitView.from_outcome(outcome, scan_number=row.fit.scan_number, unit_id=row.key)
    changed = spectrum_bundles(ws, [replace(row, fit=fit)])[0]
    for scale in ('raw', 'normalised'):
        before = original.scaled(scale, 1000000)
        after = changed.scaled(scale, 1000000)
        np.testing.assert_array_equal(before.y, after.y)
        for name, factor in (('fast', 2), ('deep', 3)):
            np.testing.assert_allclose(after.passes[name].total, before.passes[name].total / factor)
            # Residuals are observed counts minus the model at each point's actual exposure.
            np.testing.assert_allclose(after.passes[name].residuals,
                                       before.y - (before.y - before.passes[name].residuals) / factor)


def test_changed_exposure_source_does_not_draw_a_wrongly_scaled_curve(scans_view_ws_readonly):
    ws = Workspace.open(scans_view_ws_readonly)
    row = scan_list(ws).spectrum(ONE_MODE)
    outcome = copy.deepcopy(row.fit._outcome)
    outcome['fast_pass']['exposure_basis'] = {'source': 'time', 'reference': 3.0}
    fit = FitView.from_outcome(outcome, scan_number=row.fit.scan_number, unit_id=row.key)
    bundle = spectrum_bundles(ws, [replace(row, fit=fit)])[0].scaled('raw')
    assert 'different exposure sources' in bundle.passes['fast'].text
