"""The analyzer path's plot spec (C-link-plotvocab, design 2.5): one series per branch, lines only between path-adjacent
stops that both carry the branch, analyzer ticks, link markers at E* with their words, and the unavailable path.

Prevents: a branch line joining two stops the branch does not share (an invented connection), a link marker placed at
a broad mode's omega0 rather than E*, a mode with no E* given a marker at zero, and a split candidate's two names
lost from the plot.
"""
import json

import numpy as np

from isar.plotspec import spec as ps
from isar.plotspec.path_builders import UNLINKED_LABEL, UNRESOLVED_LABEL, analyzer_path_spec


def _mode(energy, *, name=None, peak_E=None, lo=None, hi=None, unresolved=(), markers=()):
    return {"energy": energy, "lo": lo, "hi": hi, "peak_E": peak_E, "name": name,
            "unresolved": tuple(unresolved), "markers": list(markers)}


def _stop(x, beamline, *modes, note=None, covered=True):
    return {"x": x, "beamline": beamline, "note": note, "covered": covered, "modes": list(modes)}


def _spec(stops, **kw):
    return analyzer_path_spec(stops=stops, title="t", xlabel="x", ylabel="y", **kw)


def _named(spec, label):
    return [s for s in spec.series if s.label == label]


def test_one_series_per_branch_at_omega0_with_its_interval_and_the_unlinked_modes_apart():
    spec = _spec([_stop(0.0, "a03", _mode(4.0, name="B1", lo=3.9, hi=4.1), _mode(8.0)),
                  _stop(0.1, "a04", _mode(4.2, name="B1")),
                  _stop(0.3, "a05", _mode(4.4, name="B1", lo=4.3, hi=4.5))])
    (b1,) = _named(spec, "B1")
    assert list(b1.x) == [0.0, 0.1, 0.3] and list(b1.y) == [4.0, 4.2, 4.4]
    assert np.allclose(b1.yerr, [0.1, 0.0, 0.1])                       # no interval: no whisker, not a guess
    (muted,) = _named(spec, UNLINKED_LABEL)
    assert list(muted.x) == [0.0] and list(muted.y) == [8.0]
    assert json.dumps(spec.to_dict(), allow_nan=False)                  # the spec is a JSON value


def test_a_line_joins_only_path_adjacent_stops_that_both_carry_the_branch():
    spec = _spec([_stop(0.0, "a03", _mode(4.0, name="B1")), _stop(0.1, "a04", _mode(4.2)),
                  _stop(0.3, "a05", _mode(4.4, name="B1")), _stop(0.4, "a06", _mode(4.5, name="B1"))])
    lines = [s for s in spec.series if s.role is ps.Role.BRANCH]
    assert [list(s.x) for s in lines] == [[0.3, 0.4]]                   # a03 and a05 are not joined across a04


def test_ticks_are_the_analyzers_in_path_order():
    spec = _spec([_stop(0.0, "a03", _mode(4.0)), _stop(0.1, "a04", _mode(4.1))])
    assert spec.x_ticks == [(0.0, "a03"), (0.1, "a04")]
    assert spec.to_dict()["x_ticks"] == [[0.0, "a03"], [0.1, "a04"]]


def test_link_markers_sit_at_e_star_with_their_words_and_a_mode_without_e_star_gets_none():
    unlinked = {"group": 3, "label": "no link to a neighbour", "words": "Mode 1 follows no neighbour"}
    change = {"group": 2, "label": "energy differs from neighbours", "words": "energy 4.6 meV, neighbours 4.1"}
    spec = _spec([_stop(0.0, "a06", _mode(9.7, peak_E=0.945, markers=[unlinked])),
                  _stop(0.1, "a07", _mode(4.1, name="B1", markers=[change]))])   # no E*: no marker
    (mark,) = spec.markers
    assert mark.kind is ps.MarkerKind.LINK_UNLINKED
    assert (mark.x, mark.y) == (0.0, 0.945)                             # E*, not the omega0 of 9.7
    assert mark.tooltip == "Mode 1 follows no neighbour" and mark.label == "no link to a neighbour"


def test_an_unresolved_split_candidate_is_its_own_series_with_both_names_asked():
    spec = _spec([_stop(0.0, "a03", _mode(5.0, name="B2", unresolved=("B2", "B3"), peak_E=4.9))])
    assert _named(spec, "B2") == []                                     # not a settled branch series
    (series,) = _named(spec, UNRESOLVED_LABEL)
    assert list(series.y) == [5.0]
    (note,) = [a for a in spec.annotations if a.xy is not None]
    assert (note.text, note.xy) == ("B2 / B3?", (0.0, 5.0))


def test_a_mode_no_current_saved_link_covers_is_not_called_unlinked():
    spec = _spec([_stop(0.0, "a03", _mode(8.0), covered=False), _stop(0.1, "a04", _mode(4.1, name="B1"))])
    assert _named(spec, UNLINKED_LABEL) == []                           # Link modes never said it follows no neighbour
    (series,) = _named(spec, "modes (no saved links)")
    assert list(series.x) == [0.0] and list(series.y) == [8.0]
    assert series.tooltip == "Link modes has not covered this analyzer"


def test_a_stop_with_no_fit_is_noted_and_an_unavailable_path_is_an_empty_spec_with_its_reason():
    spec = _spec([_stop(0.0, "a03", note="no fit to draw"), _stop(0.1, "a04", _mode(4.1))])
    assert [a.text for a in spec.annotations] == ["a03: no fit to draw"]
    empty = analyzer_path_spec(stops=(), title="t", xlabel="x", ylabel="y", reason="pick two analyzers first")
    assert (empty.series, empty.markers, empty.x_ticks) == ([], [], [])
    assert [a.text for a in empty.annotations] == ["pick two analyzers first"]
