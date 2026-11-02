from dataclasses import replace

import numpy as np
import pytest

from isar.plotspec.builders import ScansCurve, ScansMark, ScansTrace, scans_spec, scans_residual_spec, stack_step
from isar.plotspec import spec as ps


def traces():
    return tuple(ScansTrace(name, None, np.arange(3.), np.array(y, dtype=float),
                           np.ones(3), np.ones(3, dtype=bool), None, (), False, i == 1)
                 for i, (name, y) in enumerate((('A', [1, 5, 2]), ('B', [0, 2, 1]),
                                                ('C', [3, 3, 3]))))


def deep_traces():
    a, b, _ = traces()
    total = ScansCurve('Total', 'total', None, np.array([2., 3., 2.]))
    mode = ScansCurve('Mode 1', 'mode', 1, np.array([1., 2., 1.]))
    bg = ScansCurve('Background', 'background', None, np.ones(3))
    return (replace(a, curve_x=a.x, curves=(total, mode, bg), deep=True),
            replace(b, curve_x=b.x, curves=(total, mode, replace(mode, name='Mode 2', number=2), bg), deep=True))


def build(ts, view='offset', step=4.8):
    return scans_spec(traces=ts, view=view, step=step, ylabel='Counts, traces offset',
                      curves_header='Curves · Deep (untrusted)')


def test_stack_data_geometry_and_masked_spike():
    ts = traces()
    assert stack_step(ts) == pytest.approx(4.4)
    spike = replace(ts[1], y=np.array([0., 2., 100.]), kept=np.array([True, True, False]))
    assert stack_step((ts[0], spike, ts[2])) == pytest.approx(4.4)
    assert stack_step(ts, ratio=1.0) == 4
    assert stack_step(ts, ratio=1.15) == pytest.approx(4.6)
    plotted = build(ts)
    np.testing.assert_allclose([s.y for s in plotted.series if s.role is ps.Role.DATA], [[10.6, 14.6, 11.6], [4.8, 6.8, 5.8], [3, 3, 3]])
    assert [a.xy for a in plotted.annotations] == [(2., 9.6), (2., 4.8), (2., 0.)]
    assert all(a.anchor == (1, -0.15) for a in plotted.annotations)
    assert all(np.array_equal(s.yerr, np.ones(3)) for s in plotted.series if s.role is ps.Role.DATA)
    np.testing.assert_array_equal(build(ts, step=1).series[0].y, [3, 7, 4])
    masked = build((spike,))
    assert masked.markers[0].y == 100
    assert len(masked.series[0].y) == 2
    assert stack_step(()) == 1
    assert stack_step((ts[2],)) == 0


def test_overlay_is_data_only_and_preserves_identity_symbols():
    ts = traces()
    plotted = build(ts, view='overlay')
    assert not plotted.annotations
    assert [s.color for s in plotted.series] == list(ps.TRACE_COLORS[:3])
    assert [s.symbol for s in plotted.series] == ['o', 's', 't']
    for original, series in zip(ts, plotted.series):
        np.testing.assert_array_equal(original.y, series.y)
    with pytest.raises(ValueError, match='data only'):
        build(deep_traces(), view='overlay')
    with pytest.raises(ValueError, match='exactly one'):
        build(ts, view='single')


def test_curve_roles_stay_in_order_and_background_is_dotted():
    plotted = build(deep_traces())
    backgrounds = [s for s in plotted.series if s.style == 'dotted' and s.legend_group == 'Curves · Deep (untrusted)']
    assert len(backgrounds) == 2
    assert all(s.label is None for s in backgrounds)
    plotted.series = [s for s in plotted.series if s.label is not None]
    assert [s.label for s in plotted.series] == ['A', 'B', 'Total', 'Total', 'Mode 1', 'Mode 1', 'Mode 2']
    assert plotted.series[1].legend_bold
    assert all(s.legend_group == 'Spectra' for s in plotted.series[:2])
    assert all(s.legend_group == 'Curves · Deep (untrusted)' for s in plotted.series[2:])
    assert [s.style for s in plotted.series[2:]] == ['heavy dashed', 'heavy dashed', 'heavy dashed', 'heavy dashed', 'heavy dashed']
    assert plotted.series[4].color == plotted.series[5].color


def test_missing_pass_full_identity_and_residual_mask_are_preserved():
    trace = replace(traces()[0], identity='A' * 60, note='no deep pass', kept=np.array([True, False, True]))
    plotted = build((trace,))
    assert plotted.series[0].label == trace.identity + ' · no deep pass'
    assert len(plotted.annotations[0].text) == 28
    assert plotted.annotations[0].tooltip == trace.identity
    residual = scans_residual_spec(trace=trace, residuals=np.array([1., -2., 3.]), ylabel='data − fit (Counts)')
    np.testing.assert_array_equal(residual.series[0].y, [1, 3])
    assert residual.markers[0].y == -2
    assert residual.title == ''


@pytest.mark.parametrize('view', ['single', 'offset', 'overlay'])
def test_zero_guides_are_viewport_lines_and_leave_the_data_unchanged(view):
    ts = traces()[:1] if view == 'single' else traces()
    args = dict(traces=ts, view=view, step=5, ylabel='Counts', curves_header=None)
    plain = scans_spec(**args, zero_lines=False)
    guided = scans_spec(**args, energy_zero_line=True)
    assert [(g.axis, g.value) for g in guided.guides] == (
        [('y', 10), ('y', 5), ('y', 0), ('x', 0)] if view == 'offset' else [('x', 0)])
    for before, after in zip(plain.series, guided.series):
        np.testing.assert_array_equal(before.x, after.x)
        np.testing.assert_array_equal(before.y, after.y)
    assert guided.to_dict()['guides'][-1] == {
        'axis': 'x', 'value': 0.0, 'color': ps.GUIDE_COLOR, 'style': 'dotted'}
    assert 'guides' not in plain.to_dict()


def test_mode_labels_sit_at_the_peak_and_follow_the_peak_lines_switch():
    trace = replace(deep_traces()[0], deep=False, marks=(
        ScansMark(1, 1.5, name='P3'),
        ScansMark(2, 1.0, name='P2'),        # mode 2 has no drawn curve here: no label
        ScansMark(1, 9.0, name='P3'),        # outside the grid: no label
    ))
    spec = scans_spec(traces=(trace,), view='single', step=0, ylabel='Counts', curves_header=None)
    assert not spec.markers
    [name] = spec.annotations
    assert (name.text, name.xy) == ('P3', (1.5, 1.5)) and name.tooltip
    quiet = scans_spec(traces=(trace,), view='single', step=0, ylabel='Counts', curves_header=None, peak_lines=False)
    assert not quiet.annotations


def test_a_phonon_is_one_colour_in_every_trace_in_ascending_number_and_energy_order_modes_keep_theirs():
    a, b = deep_traces()
    p3 = replace(b.curves[1], name='P3', phonon='B3')           # the same phonon in both spectra
    named_b = (b.curves[0], p3, replace(b.curves[2], name='P2', phonon='B2'), b.curves[3])
    named_a = (a.curves[0], replace(a.curves[1], name='P3', phonon='B3'), a.curves[2])
    spec = scans_spec(traces=(replace(a, deep=False, curves=named_a), replace(b, deep=False, curves=named_b)),
                      view='offset', step=1, ylabel='Counts', curves_header=None)
    assert {s.label: s.color for s in spec.series if s.label in ('P2', 'P3')} == {
        'P2': ps.TRACE_COLORS[0], 'P3': ps.TRACE_COLORS[1]}
    assert {s.color for s in spec.series if s.label == 'P3'} == {ps.TRACE_COLORS[1]}
    plain = scans_spec(traces=(replace(a, deep=False), replace(b, deep=False)),
                       view='offset', step=1, ylabel='Counts', curves_header=None)
    assert ps.CURVE_COLORS['mode1'] in {s.color for s in plain.series if s.label == 'Mode 1'}


def test_phonons_get_nine_colours_none_of_them_the_elastic_grey_and_repeat_past_nine():
    ones = np.ones(3)
    curves = tuple(ScansCurve(f'P{k}', 'mode', k, ones, phonon=f'B{k}') for k in range(1, 11))
    trace = ScansTrace('a03', None, np.arange(3.), ones, ones, np.ones(3, dtype=bool), np.arange(3.), curves,
                       False, True)
    spec = scans_spec(traces=(trace,), view='single', step=0, ylabel='Counts', curves_header=None)
    colour = {s.label: s.color for s in spec.series if s.label in {f'P{k}' for k in range(1, 11)}}
    assert ps.CURVE_COLORS['elastic'] not in colour.values()
    assert len({colour[f'P{k}'] for k in range(1, 10)}) == 9 and colour['P10'] == colour['P1']


def test_new_style_fields_are_optional_in_serialization():
    plain = ps.Series(np.array([1]), np.array([2]))
    assert not {'color', 'symbol', 'legend_group', 'legend_bold'} & plain.to_dict().keys()
    styled = replace(plain, color='#123456', symbol='star', legend_group='Spectra', legend_bold=True)
    assert styled.to_dict()['symbol'] == 'star'
    assert styled.to_dict()['legend_bold'] is True
    with pytest.raises(ValueError, match='symbol'):
        replace(plain, symbol='missing')
    assert ps.Annotation('note').to_dict() == {'text': 'note', 'xy': None, 'role': None}
    assert ps.Annotation('note', anchor=(1, 1), tooltip='full', bold=True).to_dict()['anchor'] == [1, 1]
