"""The observe-only scene hook at the tail of a delivery (SCENE-3 PR 3c).

Two claims, and the first is the load-bearing one: SCENE-3 observes and steers nothing, so a
scene that cannot be built must cost a campaign nothing at all — no raised exception, no changed
status, no missing results.csv. The second is that on a real delivery the hook actually publishes
the scene the delivered ledger says it should.
"""
import shutil

import pytest

from isar.parsers import ill_in8, spec_s30
from isar.scene import build as scene_build
from isar.scene import store
from isar.session import driver
from isar.synth_format import run_builder

from .test_driver_smoke import IN8_DIR, _have_in8, _init_workspace


def _s30_batch(tmp_path):
    """The cheapest driver delivery in the suite: the held-for-calibration S30 bootstrap gate.

    Nothing is fitted, which is exactly what makes it the right isolation fixture — the hook has
    to survive the empty case as well as the rich one, and it runs in about a second.
    """
    ws = _init_workspace(tmp_path, spec_s30)
    run = run_builder.build_s30_run("SYN", n_scans=3, seed0=1000)
    data = tmp_path / "SYN.spec_supplementary.dat"
    data.write_bytes(spec_s30.emit_file(run, spec_s30.card_template()))
    return ws, data


def test_a_raising_scene_build_is_logged_and_skipped(tmp_path, monkeypatch):
    """The isolation contract. A scene build that blows up leaves the delivery byte-for-byte the
    delivery it would have been, and the only trace is a context line an operator can see."""
    ws, data = _s30_batch(tmp_path)

    def boom(*_a, **_kw):
        raise RuntimeError("scene scoring exploded")

    monkeypatch.setattr(scene_build, "build_scene", boom)
    summary = driver.run_batch(ws, [data], M=10, detectors="ana5")

    assert [line for line in summary.context_lines if line.startswith("scene revision")] == \
        ["scene revision skipped: scene scoring exploded"]
    # The delivery itself is untouched: the bootstrap gate still holds every scan, and the
    # persisted artifacts are all there.
    _card, session = ws.load()
    assert {e.status for e in session.scans.values()} == {"held_for_calibration"}
    assert (ws.report_dir / "results.csv").exists()
    # Nothing was published, not even an empty directory.
    assert not (ws.report_dir / "scene").exists()


def test_a_delivery_with_nothing_to_score_still_publishes_a_scene(tmp_path):
    """The same held delivery, hook intact. An arena-less workspace is a legal scene (there is
    nothing to score yet), and publishing it is what makes the unchanged-digest path free on
    every later delivery — the alternative is a workspace whose scene appears only once
    something happens to fit."""
    ws, data = _s30_batch(tmp_path)
    summary = driver.run_batch(ws, [data], M=10, detectors="ana5")

    line, = [line for line in summary.context_lines if line.startswith("scene revision")]
    assert not line.startswith("scene revision skipped")
    pointer, payload = store.read_current(ws.report_dir)
    assert line == f"scene revision {pointer['digest'][:12]}"
    assert payload["observations"] == []
    assert payload["catalogue_version"] == "isar.catalogue.default/1"


@pytest.mark.slow
@pytest.mark.skipif(not _have_in8, reason="IN8 test_data not present (gitignored)")
def test_a_real_delivery_publishes_the_scans_it_delivered(tmp_path):
    """The rich case, on the cheapest real-data driver fixture there is. The published revision
    verifies against its own digest and holds the arena the delivery actually produced — the hook
    reads the ledger the delivery just wrote, not a stale one.

    That arena is the POOLED MASTER, not the member scan: the master fitted the same neutrons
    (combine-before-divide), so the adapter's exactly-once rule supersedes the member row. Pinned
    here because it is the delivery-shaped half of that rule — the adapter tests build the two
    rows by hand."""
    data = tmp_path / "in8"
    data.mkdir()
    for numor in ("055182", "055183"):
        shutil.copy(IN8_DIR / numor, data / numor)
    ws = _init_workspace(tmp_path, ill_in8)

    driver.run_batch(ws, [data], M=30)

    # read_current re-digests the parsed payload, so this asserts the blob and the pointer agree.
    pointer, payload = store.read_current(ws.report_dir)
    observation, = payload["observations"]
    assert observation["scan_ref"].startswith("master:"), observation["scan_ref"]
    assert observation["candidates"], "the delivered arena carries no candidate table"
    assert (ws.report_dir / "scene" / f"{pointer['digest']}.json").is_file()

    # Re-running the same batch fits nothing new, so the scene rebuilds to the SAME digest and the
    # pointer does not move — the property the hook relies on to be free on every delivery.
    summary = driver.run_batch(ws, [data], M=30)
    assert any(line.startswith("scene revision") and line.endswith("(unchanged)")
               for line in summary.context_lines), summary.context_lines
    assert store.read_current(ws.report_dir)[0]["digest"] == pointer["digest"]
