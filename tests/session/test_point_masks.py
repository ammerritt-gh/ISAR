"""Manual masks enter the shared Fast/Deep/inter-spectra preparation, without changing counts."""
from dataclasses import replace

import numpy as np

import intake_fixtures as fx
from isar.intake import inputs, scans, store
from isar.intake.point_masks import point_ids, save_selection
from isar.session import file_fit, ingest
from isar.session.inter_spectra.authority import stale_of
from isar.workspace_api import Workspace as ReadWorkspace


def test_mask_freeze_context_and_qc_restore(tmp_path):
    ws, data = fx.bl43_workspace(tmp_path, stems=('A',))
    card, session = ws.load()
    ingest.ingest_batch(card, session, [data], workspace_root=ws.root, data_dir=ws.root / 'data')
    ws.save(card, session)
    key = next(k for k in session.scans if k.endswith(':a03'))
    reader = ReadWorkspace.open(ws.root)
    raw = reader.unit_data(key)
    last = raw.point_ids[-1]
    assert save_selection(ws.root, {key: [last]}).ok
    frozen = file_fit.freeze(ws.root)
    exp, scan, det = key.rsplit(':', 2)
    parsed = file_fit.prepare_scan(frozen, exp, int(scan), frozen.session.get(key))
    prep = file_fit.prepare_unit(frozen, parsed, key, ('fast', 'deep'))
    assert not prep.reason
    spectrum = prep.eff.spectra[det]
    assert not spectrum.mask[-1]
    np.testing.assert_array_equal(spectrum.intensity, raw.counts)
    context = file_fit.fit_context(prep)
    assert isinstance(context, file_fit.FitContext)
    assert not context.ctx.mask[-1]
    assert raw.x[-1] not in context.ctx.x_used
    used = prep.up.started[0]['inputs']
    assert used['point_masks'] == [last]
    # Withdrawing a manual exclusion cannot re-enable a QC-excluded point.
    si = inputs.scan_inputs(scans.read_scans(ws.root)[key.rsplit(':', 1)[0]],
                            inputs.index_entries([], lines=[]))
    qc = replace(parsed[0], spectra={det: replace(parsed[0].spectra[det], mask=spectrum.mask.copy())})
    assert not inputs.apply(qc, si).spectra[det].mask[-1]
    assert save_selection(ws.root, {key: [last]}, masked=False).ok
    read = store.read_all(ws.root, store.ENTRIES)
    now_si = inputs.scan_inputs(scans.read_scans(ws.root)[key.rsplit(':', 1)[0]],
                               inputs.index_entries(read.entries, lines=read.lines))
    current = inputs.unit_inputs(scans.read_scans(ws.root)[key.rsplit(':', 1)[0]], now_si, det, None)
    assert any(c.field == 'point_masks' for c in inputs.changes(used, current))
    assert inputs.inputs_hash(used) != inputs.inputs_hash(current)
    from types import SimpleNamespace
    line = SimpleNamespace(unit='other', inputs='unchanged', conditioned_on=(
        SimpleNamespace(unit=key, inputs=inputs.inputs_hash(used)),))
    stale = stale_of(line, {'other': 'unchanged', key: inputs.inputs_hash(current)})
    assert stale.code == 'neighbour_inputs_changed' and stale.unit == key


def test_source_identity_distinguishes_duplicates_and_ignores_display_changes():
    ids = point_ids([0, 1, 1], [4, 8, 8], [4, 8, 8])
    assert len(set(ids)) == 3
    assert point_ids([1, 0, 1, 2], [8, 4, 8, 9], [8, 4, 8, 9])[:3] == (ids[1], ids[0], ids[2])
    assert point_ids([0, 1, 1], [4, 99, 8], [4, 99, 8])[1] != ids[1]
