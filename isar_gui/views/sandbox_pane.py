"""SandboxPane — the G7 sandbox preview: a dashed fit over the data, unmistakably marked NOT a result.

Presentational only: it renders a :class:`~isar.plotspec.spec.PlotSpec` (data + a ``Role.SANDBOX``
dashed curve) built from the worker's :class:`~isar.sandbox_api.SandboxResult`. A persistent banner
and the dashed curve make the ephemeral, non-official nature obvious (G7). Writes nothing.

The energy zero is a **placed object**, not a threshold: a draggable ``E₀`` line marks it. Drag it
onto the visible elastic line and release → the sandbox re-fits from that zero. When no automatic
zero is available the panel still shows the raw spectrum + the line (never a blank), so the operator
sets the zero by eye — a threshold has no business overruling what they can see.
"""
from __future__ import annotations

import pyqtgraph as pg
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from isar_gui.plotspec_pg import clear_render, disable_view_drops, render_into

_BANNER = ("QLabel#sandboxBanner { background: #fff3b0; color: #6b5300;"
           " border: 1px solid #e0c95a; border-radius: 3px; padding: 3px 6px; font-weight: bold; }")

# The draggable E₀ marker: a dashed amber vertical line with a grab handle.
_ZERO_PEN = pg.mkPen("#c07000", width=2, style=pg.QtCore.Qt.DashLine)
_ZERO_HOVER = pg.mkPen("#ff8c00", width=3)


class SandboxPane(QWidget):
    """A live preview fit over a :class:`~isar_gui.viewmodels.sandbox.SandboxViewModel`."""

    def __init__(self, vm, parent=None):
        super().__init__(parent)
        self._vm = vm
        self._unit_key = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)

        banner = QLabel("SANDBOX — not a result (preview only; the official number comes from a run)")
        banner.setObjectName("sandboxBanner")
        banner.setWordWrap(True)
        banner.setStyleSheet(_BANNER)
        outer.addWidget(banner)

        self._status = QLabel("Right-click a scan → “Fit (sandbox preview)”.")
        self._status.setStyleSheet("color: #666;")
        self._status.setWordWrap(True)
        outer.addWidget(self._status)

        self._glw = pg.GraphicsLayoutWidget()
        disable_view_drops(self._glw)
        self._plot = self._glw.addPlot(row=0, col=0)
        outer.addWidget(self._glw)

        # The E₀ marker persists across re-renders (render_into only removes its own items).
        self._zero_line = pg.InfiniteLine(angle=90, movable=True, pen=_ZERO_PEN, hoverPen=_ZERO_HOVER,
                                          label="E₀", labelOpts={"position": 0.92, "color": "#c07000"})
        self._zero_line.setVisible(False)
        self._zero_line.sigPositionChangeFinished.connect(self._on_zero_moved)
        self._plot.addItem(self._zero_line)

        vm.busy_changed.connect(self._on_busy)
        vm.result_ready.connect(self._on_result)

    def _on_busy(self, busy: bool) -> None:
        if busy:
            self._status.setText("fitting… (sandbox, fast tier)")

    # ---- the placed energy zero ------------------------------------------
    def _on_zero_moved(self) -> None:
        """The operator dragged E₀ and released → re-fit from the placed zero (raw-frame energy)."""
        if self._unit_key is None:
            return
        self._vm.request_fit(self._unit_key, center_override=float(self._zero_line.value()))

    def _show_zero_line(self, pos) -> None:
        if pos is None:
            self._zero_line.setVisible(False)
            return
        # setValue re-emits no "finished" signal (only interactive drags do), so this won't loop.
        self._zero_line.setValue(float(pos))
        self._zero_line.setVisible(True)

    def _on_result(self, res) -> None:
        self._unit_key = getattr(res, "unit_key", None)
        if res is None:
            clear_render(self._plot)
            self._zero_line.setVisible(False)
            self._status.setText("error: no result")
            return

        if res.status == "fitted":
            render_into(self._plot, self._spec(res, fit=True))
            self._show_zero_line(res.zero_guess if res.zero_guess is not None else res.center)
            trust = ("HESSE trusted — preview only, not certified" if res.hesse_trusted
                     else "HESSE untrusted — preview only, not certified")
            iv = f" [{res.interval[0]:.3g}, {res.interval[1]:.3g}]" if res.interval else ""
            zero = ""
            if res.zero_source and "manual" in res.zero_source:
                zero = " · zero: placed by you"
            self._status.setText(
                f"ω₀ = {res.omega0:.3g}{iv} meV · Γ = {res.gamma:.3g} meV  "
                f"(fast tier — {trust}){zero}  ·  drag E₀ to re-fit")
            return

        # held: show the RAW spectrum + the draggable E₀ so the operator sets the zero by eye.
        if res.status == "held" and res.x is not None:
            render_into(self._plot, self._spec(res, fit=False))
            self._show_zero_line(res.zero_guess)
            if getattr(res, "needs_manual_zero", False):
                self._status.setText(
                    f"held: {res.reason}  —  drag the dashed E₀ line onto the elastic line and release.")
            else:
                self._status.setText(f"held: {res.reason}")
            return

        clear_render(self._plot)
        self._zero_line.setVisible(False)
        self._status.setText(f"{getattr(res, 'status', 'error')}: {getattr(res, 'reason', '')}")

    @staticmethod
    def _spec(res, *, fit: bool):
        from isar.plotspec import spec as ps
        series = [ps.Series(x=res.x, y=res.counts, yerr=res.yerr, role=ps.Role.DATA, label="data")]
        if fit and res.x_fit is not None and res.total is not None:
            series.append(ps.Series(x=res.x_fit, y=res.total, role=ps.Role.SANDBOX,
                                    label="sandbox fit"))
        if fit:
            title = "SANDBOX preview — not a result"
        elif getattr(res, "needs_manual_zero", False):
            title = "SANDBOX — place E₀ on the elastic line"
        else:
            title = "SANDBOX preview held — not a result"
        return ps.PlotSpec(
            title=title, xlabel="raw energy (meV) — E₀ marks the zero", ylabel="counts", series=series)
