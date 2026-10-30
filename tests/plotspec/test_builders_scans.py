from dataclasses import replace

import numpy as np
import pytest

from isar.plotspec.builders import ScansCurve, ScansTrace, scans_spec, scans_residual_spec, stack_step
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
    assert stack_step(ts) == pytest.approx(4.8)
    spike = replace(ts[1], y=np.array([0., 2., 100.]), kept=np.array([True, True, False]))
    assert stack_step((ts[0], spike, ts[2])) == pytest.approx(4.8)
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
