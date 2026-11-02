"""The analyzer path's plot spec (design 2.5): one series per phonon in ascending number, lines only between path-adjacent
stops that both carry the phonon, analyzer ticks, a named mode at its E*, and the unavailable path.

Prevents: a phonon line joining two stops that do not share it (an invented connection), a named mode placed at its
omega0 rather than E*, a lone phonon dropped from the plot, a phonon drawn in two colours, and the removed link markers
or split and unlinked series coming back.
"""
import json

import numpy as np

from isar.plotspec import spec as ps
from isar.plotspec.path_builders import UNSAVED_LABEL, analyzer_path_spec


def _mode(energy, *, name=None, peak_E=None, lo=None, hi=None):
    return {"energy": energy, "lo": lo, "hi": hi, "peak_E": peak_E, "name": name}


def _stop(x, beamline, *modes, note=None):
    return {"x": x, "beamline": beamline, "note": note, "modes": list(modes)}


def _spec(stops, **kw):
    return analyzer_path_spec(stops=stops, title="t", xlabel="x", ylabel="y", **kw)


def _named(spec, label):
    return [s for s in spec.series if s.label == label]


def test_one_series_per_phonon_at_omega0_with_its_interval_and_the_unsaved_modes_apart():
    spec = _spec([_stop(0.0, "a03", _mode(4.0, name="P1", lo=3.9, hi=4.1), _mode(8.0)),
                  _stop(0.1, "a04", _mode(4.2, name="P1")),
                  _stop(0.3, "a05", _mode(4.4, name="P1", lo=4.3, hi=4.5))])
    (p1,) = _named(spec, "P1")
    assert list(p1.x) == [0.0, 0.1, 0.3] and list(p1.y) == [4.0, 4.2, 4.4]
    assert np.allclose(p1.errors.bottom, [0.1, 0.0, 0.1]) and np.allclose(p1.errors.top, [0.1, 0.0, 0.1])
    (unsaved,) = _named(spec, UNSAVED_LABEL)
    assert list(unsaved.x) == [0.0] and list(unsaved.y) == [8.0]
    assert json.dumps(spec.to_dict(), allow_nan=False)                  # the spec is a JSON value


def test_a_mode_at_omega0_keeps_its_stored_interval_asymmetric_and_one_sided():
    spec = _spec([_stop(0.0, "a03", _mode(5.0, lo=4.9, hi=5.5)), _stop(0.1, "a04", _mode(4.0, hi=4.2)),
                  _stop(0.2, "a05", _mode(3.0, lo=2.8))])
    (unsaved,) = _named(spec, UNSAVED_LABEL)
    assert np.allclose(unsaved.errors.bottom, [0.1, 0.0, 0.2]) and np.allclose(unsaved.errors.top, [0.5, 0.2, 0.0])


def test_a_line_joins_only_path_adjacent_stops_that_both_carry_the_phonon():
    spec = _spec([_stop(0.0, "a03", _mode(4.0, name="P1")), _stop(0.1, "a04", _mode(4.2)),
                  _stop(0.3, "a05", _mode(4.4, name="P1")), _stop(0.4, "a06", _mode(4.5, name="P1"))])
    lines = [s for s in spec.series if s.role is ps.Role.BRANCH]
    assert [list(s.x) for s in lines] == [[0.3, 0.4]]                   # a03 and a05 are not joined across a04


def test_a_lone_phonon_is_one_series_and_a_single_stop_is_a_point_with_no_line():
    spec = _spec([_stop(0.0, "a03", _mode(4.0, name="P9", peak_E=3.8)), _stop(0.1, "a04", _mode(4.1))])
    (p9,) = _named(spec, "P9")
    assert list(p9.x) == [0.0] and list(p9.y) == [3.8]
    assert not [s for s in spec.series if s.role is ps.Role.BRANCH]


def test_phonons_take_the_trace_colours_in_ascending_number_and_keep_them_across_the_path():
    spec = _spec([_stop(0.0, "a03", _mode(4.0, name="P7"), _mode(8.0, name="P2")),
                  _stop(0.1, "a04", _mode(4.1, name="P7"), _mode(8.1, name="P2"))])
    colour = {s.label: s.color for s in spec.series if s.label in ("P2", "P7")}
    assert colour == {"P2": ps.TRACE_COLORS[0], "P7": ps.TRACE_COLORS[1]}


def test_ticks_are_the_analyzers_in_path_order():
    spec = _spec([_stop(0.0, "a03", _mode(4.0)), _stop(0.1, "a04", _mode(4.1))])
    assert spec.x_ticks == [(0.0, "a03"), (0.1, "a04")]
    assert spec.to_dict()["x_ticks"] == [[0.0, "a03"], [0.1, "a04"]]


def test_a_named_mode_and_its_line_sit_at_e_star_with_no_whisker_and_no_link_marker_is_drawn():
    spec = _spec([_stop(0.0, "a06", _mode(9.7, name="P1", peak_E=0.945, lo=9.5, hi=9.9)),
                  _stop(0.1, "a07", _mode(1.0, name="P1", peak_E=1.0, lo=0.9, hi=1.1))])
    (p1,) = _named(spec, "P1")
    assert list(p1.y) == [0.945, 1.0] and not np.any(p1.errors.bottom) and not np.any(p1.errors.top)   # not at 9.7
    (line,) = [s for s in spec.series if s.role is ps.Role.BRANCH]
    assert list(line.y) == [0.945, 1.0]
    assert spec.markers == [] and {s.label for s in spec.series} - {None} <= {"P1", UNSAVED_LABEL}


def test_a_mode_no_current_saved_link_covers_is_drawn_as_a_saved_link_gap_with_its_tooltip():
    spec = _spec([_stop(0.0, "a03", _mode(8.0)), _stop(0.1, "a04", _mode(4.1, name="P1"))])
    (series,) = _named(spec, UNSAVED_LABEL)
    assert list(series.x) == [0.0] and list(series.y) == [8.0]
    assert series.tooltip == "Link modes has not covered this analyzer"


def test_a_stop_with_no_fit_is_noted_and_an_unavailable_path_is_an_empty_spec_with_its_reason():
    spec = _spec([_stop(0.0, "a03", note="no fit to draw"), _stop(0.1, "a04", _mode(4.1))])
    assert [a.text for a in spec.annotations] == ["a03: no fit to draw"]
    empty = analyzer_path_spec(stops=(), title="t", xlabel="x", ylabel="y", reason="make a path first")
    assert (empty.series, empty.markers, empty.x_ticks) == ([], [], [])
    assert [a.text for a in empty.annotations] == ["make a path first"]
