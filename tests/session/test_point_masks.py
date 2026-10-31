"""Manual masks enter the shared Fast/Deep/inter-spectra preparation, without changing counts."""
from dataclasses import replace

import numpy as np

import intake_fixtures as fx
from isar.intake import fit_inputs, inputs, scans, store, values
from isar.intake.point_masks import point_ids, save_selection
from isar.session import calibrations, file_fit, guidelines as guide_mod, ingest, start_point
from isar.session.inter_spectra.authority import stale_of
from isar.session.unit_data import read_unit_data_for_unit, unit_data_path
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
                            inputs.index_entries([], lines=[]), data_dir=ws.root / "data")
    qc = replace(parsed[0], spectra={det: replace(parsed[0].spectra[det], mask=spectrum.mask.copy())})
    assert not inputs.apply(qc, si).spectra[det].mask[-1]
    assert save_selection(ws.root, {key: [last]}, masked=False).ok
    read = store.read_all(ws.root, store.ENTRIES)
    now_si = inputs.scan_inputs(scans.read_scans(ws.root)[key.rsplit(':', 1)[0]],
                               inputs.index_entries(read.entries, lines=read.lines), data_dir=ws.root / "data")
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


def test_an_analyzer_whose_masks_cannot_be_checked_is_not_prepared_or_started(tmp_path):
    ws, data = fx.bl43_workspace(tmp_path, stems=('A',))
    card, session = ws.load()
    ingest.ingest_batch(card, session, [data], workspace_root=ws.root, data_dir=ws.root / 'data')
    ws.save(card, session)
    key = next(k for k in session.scans if k.endswith(':a03'))
    exp, scan_no, _det = key.rsplit(':', 2)
    stored = read_unit_data_for_unit(ws.root / 'data', exp, int(scan_no), 'a03')
    assert save_selection(ws.root, {key: [point_ids(stored['x'], stored['counts'], stored['variance'])[-1]]}).ok
    unit_data_path(ws.root / 'data', exp, int(scan_no), 'a03').write_text('{not json', encoding='utf-8')
    frozen = file_fit.freeze(ws.root)
    scan_prep = file_fit.prepare_scan(frozen, exp, int(scan_no), frozen.session.get(key))
    prep = file_fit.prepare_unit(frozen, scan_prep, key, ('fast',))
    assert prep.held and prep.reason == f'a03: {inputs.UNCHECKED_MASKS}'
    # the scan's start point, as a run builds it: the held unit is handed no task and writes no started line
    rec, _sources = scan_prep
    scan = f'{exp}:{scan_no}'
    rows = scans.read_scans(ws.root)
    sp = start_point.StartPoint(frozen.root, frozen.card, guide_mod.resolve(frozen.card),
                                calibrations.calibration_view(frozen.card), parsed={scan: rec},
                                rows={scan: rows[scan]}, data_revision={scan: rows[scan]['data_revision']},
                                passes=('fast',), run_id='r1', session=frozen.session,
                                is_fresh=lambda *a, **k: True, write_stub=lambda t: None, loaded=None)
    group = [(rec, 'a03', None, None, None, key, rows[scan]['data_revision'], 'g', 1)]
    assert sp(group).items == [None]
    assert not any(line.get('unit') == key for line in fit_inputs.read_log(ws.root))


def test_the_queue_reports_a_held_analyzer_as_held_with_its_reason(tmp_path):
    ws, data = fx.bl43_workspace(tmp_path, stems=('A',))
    card, session = ws.load()
    ingest.ingest_batch(card, session, [data], workspace_root=ws.root, data_dir=ws.root / 'data')
    ws.save(card, session)
    key = next(k for k in session.scans if k.endswith(':a03'))
    exp, scan_no, _det = key.rsplit(':', 2)
    stored = read_unit_data_for_unit(ws.root / 'data', exp, int(scan_no), 'a03')
    assert save_selection(ws.root, {key: [point_ids(stored['x'], stored['counts'], stored['variance'])[-1]]}).ok
    unit_data_path(ws.root / 'data', exp, int(scan_no), 'a03').write_text('{not json', encoding='utf-8')
    out = []
    file_fit.fit_file(ws.root, file_fit.FileItem('i1', key, 'fast', 1), progress=None, emit=out.append)
    (res,) = out
    assert (res.status, res.reason) == ('held', f'a03: {inputs.UNCHECKED_MASKS}')


def test_a_dead_analyzer_keeps_its_declaration_when_its_masks_cannot_be_checked(tmp_path):
    ws, data = fx.bl43_workspace(tmp_path, stems=('A',))
    card, session = ws.load()
    ingest.ingest_batch(card, session, [data], workspace_root=ws.root, data_dir=ws.root / 'data')
    ws.save(card, session)
    key = next(k for k in session.scans if k.endswith(':a03'))
    exp, scan_no, _det = key.rsplit(':', 2)
    stored = read_unit_data_for_unit(ws.root / 'data', exp, int(scan_no), 'a03')
    assert save_selection(ws.root, {key: [point_ids(stored['x'], stored['counts'], stored['variance'])[-1]]}).ok
    unit_data_path(ws.root / 'data', exp, int(scan_no), 'a03').write_text('{not json', encoding='utf-8')
    assert values.save_condition(ws.root, 'dead', [key], reason='no counts').ok
    frozen = file_fit.freeze(ws.root)
    rec, _sources = file_fit.prepare_scan(frozen, exp, int(scan_no), frozen.session.get(key))
    scan = f'{exp}:{scan_no}'
    rows = scans.read_scans(ws.root)
    sp = start_point.StartPoint(frozen.root, frozen.card, guide_mod.resolve(frozen.card),
                                calibrations.calibration_view(frozen.card), parsed={scan: rec},
                                rows={scan: rows[scan]}, data_revision={scan: rows[scan]['data_revision']},
                                passes=('fast',), run_id='r2', session=frozen.session,
                                is_fresh=lambda *a, **k: True, write_stub=lambda t: None, loaded=None)
    group = [(rec, 'a03', None, None, None, key, rows[scan]['data_revision'], 'g', 1)]
    (entry,) = sp(group).items           # the declaration is written as the unit's entry, never held
    assert entry is not None and entry.status == 'excluded'
