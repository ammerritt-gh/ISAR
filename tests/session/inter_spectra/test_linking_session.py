"""The session side of mode linking: the fits in use, what a link leaves out and why, the series, and the link item.

Prevents: a spectrum on a stale recipe or with a fit that cannot be linked crashing the whole link, linking that
writes to the workspace, the temperature series left short, and an analyzer's recorded efficiency ignored.
The fixture is generated at test time (hard rule 10); its fast records are rewritten here with plain peaks.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from inter_fixtures import build_inter_workspace, scan_keys, spectrum_keys
from isar.analyze.prior import PRIOR_RECIPE_VERSION
from isar.session import writer
from isar.session.inter_spectra import linking
from isar.session.workspace import Workspace


@pytest.fixture
def ws(tmp_path):
    """Two temperatures of one q setting, three analyzers each; every analyzer has a fast solo fit."""
    root = build_inter_workspace(tmp_path, temperatures=(100, 200), setups=("0p5_0p5_3",), n_analyzers=3)
    for unit in spectrum_keys(root):
        _plain_fast(root, unit)
    return root


def _plain_fast(root: Path, unit: str, *, version: str = PRIOR_RECIPE_VERSION) -> None:
    """Rewrite the unit's stored fast record with two modes, the resolution and the recipe a link reads."""
    ws = Workspace.open(root)
    card, session = ws.load()
    entry = session.scans[unit]
    det = unit.rsplit(":", 1)[1]
    peaks = [{"index": i, "omega0": o, "Gamma": 0.2, "height": 5.0, "area_stokes": 10.0, "sigma_area": 1.0,
              "sigma_omega0": 0.05, "sigma_Gamma": 0.02, "regime": "underdamped",
              "mode_status": "determined_by_own_data", "at_width_cap": False, "confident": True}
             for i, o in enumerate((4.0, 8.0))]
    record = {**entry.detectors[det]["fast_pass"], "peaks": peaks, "flag_statistics": {"fwhm": 0.4},
              "prior_provenance": {"version": version, "energy_support_meV": [0.5, 12.0]}}
    entry.detectors = {det: {**entry.detectors[det], "fast_pass": record}}
    ws.save(card, session)


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in Path(root).rglob("*")
            if p.is_file() and p.name != ".isar-write.lock"}


def test_intensity_scale_is_one_over_the_recorded_efficiency():
    assert linking.intensity_scale("exp1:12:a03", efficiency=0.5) == 2.0
    assert linking.intensity_scale("exp1:12:a03") == 1.0
    assert linking.intensity_scale("exp1:12:a03", efficiency=0.0) == 1.0


def test_link_scans_links_the_fits_in_use_leaves_out_an_unfitted_analyzer_and_never_writes(ws):
    unfitted = spectrum_keys(ws)[-1]
    ws_ = Workspace.open(ws)
    card, session = ws_.load()
    session.scans[unfitted].detectors = {}
    session.scans[unfitted].status = "ingested"
    ws_.save(card, session)
    before = _snapshot(ws)
    result = linking.link_scans(ws, scan_keys(ws))
    assert _snapshot(ws) == before                  # linking reads only
    assert set(result.made_from) == set(spectrum_keys(ws)) - {unfitted}
    inputs, _ = linking.inputs_for(ws, [u for u in spectrum_keys(ws) if u != unfitted])
    assert inputs and all(li.areas == (10.0, 10.0) and li.sigma_areas == (1.0, 1.0) for li in inputs.values())
    assert result.excluded[unfitted][0] and result.excluded[unfitted][1] is None
    assert result.branches, "the same two modes in every analyzer should link"


def test_a_spectrum_on_an_older_recipe_is_left_out_with_its_reason(ws):
    unit = spectrum_keys(ws)[0]
    _plain_fast(ws, unit, version="isar.mode-prior-recipe/6")
    inputs, excluded = linking.inputs_for(ws, spectrum_keys(ws))
    assert unit not in inputs
    reason, token = excluded[unit]
    assert reason == linking.OLD_RECIPE and token is not None


def test_series_widening_gives_every_scan_of_the_temperature_series(ws):
    widened = linking.widen_to_series(ws, [scan_keys(ws)[0]])
    assert len(scan_keys(ws)) == 2
    assert set(widened) == set(scan_keys(ws))


def test_a_held_map_and_its_own_source_give_the_same_inputs_as_the_store_read(ws):
    units = spectrum_keys(ws)
    read, _ = linking.inputs_for(ws, units)
    held = {u: li.held for u, li in read.items()}
    assert linking.inputs_for(ws, units, held=held)[0] == read
    stored = {u: Workspace.open(ws).load()[1].scans[u].detectors[u.rsplit(":", 1)[1]]["fast_pass"] for u in units}
    sourced, _ = linking.inputs_for(ws, units, held=held, source=lambda u, h: (stored[u], 0.4))
    assert sourced == read


def test_a_recorded_efficiency_scales_its_spectrum_and_no_efficiency_is_one(ws):
    unit, other = spectrum_keys(ws)[0], spectrum_keys(ws)[1]
    ws_ = Workspace.open(ws)
    card, session = ws_.load()
    for key, facts in ((unit, {"efficiency": {"value": 0.5}}), (other, {})):
        entry = session.scans[key]
        entry.provenance = {**(entry.provenance or {}), "analyzer_facts": facts}
    ws_.save(card, session)
    inputs, _ = linking.inputs_for(ws, [unit, other])
    assert inputs[unit].scale == 2.0 and inputs[other].scale == 1.0


def test_the_link_item_emits_one_done_result_that_the_writer_saves(ws):
    item = linking.LinkItem("i1", "link:l1:link", "link", 1,
                            {"link_id": "l1", "scans": scan_keys(ws), "series": False, "reason": "user"})
    events = []
    linking.run_link(ws, item, progress=lambda d: None, emit=events.append)
    (done,) = events
    assert done.status == "done" and done.pass_kind == "link", done
    assert done.reason.startswith("2 scans, 6 spectra:")
    (saved,) = writer.save_link_payloads(ws, [done.payload])
    assert saved["saved"] and saved["new"]


def test_the_link_item_is_held_for_scans_with_no_fit_and_failed_for_a_bad_item(ws):
    events = []
    linking.run_link(ws, linking.LinkItem("i2", "link:l2:link", "link", 1, {"scans": ["nope:99"]}),
                     progress=lambda d: None, emit=events.append)
    linking.run_link(ws, linking.LinkItem("i3", "link:l3:link", "link", 1, {}),
                     progress=lambda d: None, emit=events.append)
    assert [e.status for e in events] == ["held", "failed"]
    assert all(e.payload is None for e in events)
