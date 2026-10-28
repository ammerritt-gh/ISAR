"""The scan inspector formats only the values stored in spectrum summaries."""
from __future__ import annotations

import math

import pytest
from PySide6.QtWidgets import QLabel

from isar.workspace_api.spectra import Measured, ModeRow, PassSummary, OUTDATED_TEXT
from isar_gui.views.scan_inspector import FitSection, measured_text


def _pass(name, *, label, modes=(), trusted=None, current=True, changed=(), present=True):
    return PassSummary(
        pass_name=name,
        present=present,
        state="resolved" if present else "not run",
        state_reason=None,
        label=label,
        trusted=trusted,
        trust_reason="provisional until calibration" if trusted is False else None,
        current=current,
        outdated_text=OUTDATED_TEXT if current is False else None,
        unjudged_text=None,
        changed=changed,
        modes=tuple(modes),
    )


def _mode(number, energy, linewidth, amplitude):
    return ModeRow(number, f"phonon{number}", energy, linewidth, amplitude)


def _label(widget, name):
    label = widget.findChild(QLabel, name)
    assert label is not None, name
    return label


@pytest.mark.parametrize(
    ("measured", "expected"),
    [
        (Measured(4.0, 3.95, 4.05, "hesse"), "4.000 ± 0.050"),
        (Measured(12.0, 11.8, 12.2, "hesse"), "12.00 ± 0.20"),
        (Measured(4.1, 4.0, 4.25, "minos"), "4.10 +0.15 −0.10"),
        (Measured(1.0, None, None, None), "1 ± —"),
        (Measured(None, None, None, None), "—"),
        (Measured(math.nan, 0.0, 1.0, "hesse"), "—"),
    ],
)
def test_measured_text_preserves_missing_uncertainty(measured, expected):
    assert measured_text(measured)[0] == expected


def test_fit_section_shows_stored_modes_state_trust_and_currentness(qtbot):
    fast = _pass("fast", label="phonon+background", modes=(
        _mode(1, Measured(4.0, None, None, None),
              Measured(0.5, 0.4, 0.6, "hesse"), Measured(30.0, None, None, None)),))
    deep = _pass("deep", label="stokes+background", trusted=False, current=False,
                 changed=(("data", "old", "new"),), modes=(
                     _mode(1, Measured(4.1, 4.0, 4.25, "minos"),
                           Measured(None, None, None, None), Measured(29.0, None, None, None)),))
    widget = FitSection()
    qtbot.addWidget(widget)
    widget.render({"fast": fast, "deep": deep}, exposure_source="monitor")

    assert _label(widget, "fit_fast_modes").text() == "1"
    assert _label(widget, "fit_fast_state").text() == "resolved"
    assert _label(widget, "fit_deep_trust").text() == "untrusted"
    assert _label(widget, "fit_deep_trust").toolTip() == "provisional until calibration"
    assert _label(widget, "fit_deep_inputs").text() == OUTDATED_TEXT
    assert _label(widget, "fit_deep_inputs").toolTip() == "data: old → new"
    assert _label(widget, "fit_fast_m1_amplitude").text() == "30 ± —"
    assert _label(widget, "fit_mode_1_amplitude_label").toolTip().endswith("(monitor)")
    assert _label(widget, "fit_deep_m1_energy").toolTip().endswith("MINOS interval 4.0 to 4.25)")
    assert _label(widget, "fit_deep_m1_linewidth").text() == "—"


def test_refused_deep_pass_is_absent_not_a_zero_mode_fit(qtbot):
    fast = _pass("fast", label="background", modes=())
    deep = _pass("deep", label=None, present=True, current=None, modes=())
    widget = FitSection()
    qtbot.addWidget(widget)
    widget.render({"fast": fast, "deep": deep})
    assert _label(widget, "fit_fast_modes").text() == "0"
    assert _label(widget, "fit_deep_modes").text() == "—"
