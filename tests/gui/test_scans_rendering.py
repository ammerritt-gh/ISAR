import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore

from isar.plotspec import spec as ps
from isar_gui.plotspec_pg import render_into


def test_grouped_legend_style_parity_and_replacement(qtbot):
    import matplotlib.pyplot as plt
    from isar.plotspec.backends.mpl import _render_onto

    x = np.arange(3.)
    long_name = 'scan <12> ' + 'a' * 60
    spec = ps.PlotSpec('', 'Energy', 'Counts', series=[
        ps.Series(x, x, yerr=np.ones(3), label=long_name, color='#123456', symbol='star', legend_group='Spectra', legend_bold=True),
        ps.Series(x, x + 1, label='Total', role=ps.Role.FIT, style='heavy dashed', legend_group='Curves · Deep'),
        ps.Series(x, x + 2, label='Total', role=ps.Role.FIT, style='heavy dashed', legend_group='Curves · Deep'),
        ps.Series(x, x + 3, label='Background', style='dashed dotted', legend_group='Curves · Deep'),
    ], annotations=[ps.Annotation('active <scan>', (2, 2), anchor=(1, 1), tooltip=long_name, bold=True)])
    widget = pg.GraphicsLayoutWidget()
    qtbot.addWidget(widget)
    widget.resize(640, 480)
    plot = widget.addPlot()
    widget.show()
    qtbot.waitExposed(widget)
    rendered = render_into(plot, spec)
    QtCore.QCoreApplication.processEvents()
    legend_rect = rendered.legend.mapRectToParent(rendered.legend.boundingRect())
    assert plot.getViewBox().boundingRect().contains(legend_rect)
    assert rendered.legend.width() < 500
    assert rendered.legend_names == {long_name, 'Total', 'Background'}
    assert len(rendered.legend.items) == 3
    swatch, label = rendered.legend.items[0]
    assert label.toolTip() == long_name
    assert '…' in label.item.toPlainText()
    assert len(label.item.toPlainText()) == 48
    assert label.opts['bold'] is True
    assert swatch.acceptedMouseButtons() == QtCore.Qt.MouseButton.NoButton
    headers = [rendered.legend.layout.itemAt(i, 0) for i in range(rendered.legend.layout.rowCount())]
    assert [h.item.toPlainText() for h in headers if isinstance(h, pg.LabelItem)] == ['Spectra', 'Curves · Deep']
    lines = [i for i in rendered.items if isinstance(i, pg.PlotDataItem)]
    assert lines[0].opts['symbol'] == 'star'
    assert lines[0].opts['symbolPen'].color().name() == '#123456'
    assert lines[1].opts['pen'].style() == QtCore.Qt.PenStyle.DashLine
    assert lines[1].opts['pen'].widthF() == 2
    assert lines[-1].opts['pen'].style() == QtCore.Qt.PenStyle.DotLine
    text = next(i for i in rendered.items if isinstance(i, pg.TextItem))
    assert tuple(text.anchor) == (1, 1)
    assert text.toolTip() == long_name
    assert text.toPlainText() == 'active <scan>'
    fig, ax = plt.subplots()
    try:
        _render_onto(ax, spec)
        assert set(ax.get_legend_handles_labels()[1]) == {long_name, 'Total', 'Background'}
        assert len(ax.get_legend_handles_labels()[1]) == 3
        assert any(line.get_linestyle() == ':' for line in ax.lines)
        assert any(line.get_linewidth() == 2 for line in ax.lines)
        assert ax.containers[0].lines[0].get_marker() == '*'
    finally:
        plt.close(fig)
    old_legend = rendered.legend
    render_into(plot, spec)
    assert old_legend.scene() is None
