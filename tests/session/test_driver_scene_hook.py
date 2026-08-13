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
    # Nothing was PUBLISHED — no pointer, no blob, no lineage line.
    assert list((ws.report_dir / "scene").glob("*.json")) == \
        [ws.report_dir / "scene" / "last_attempt.json"]
    assert not (ws.report_dir / "scene" / "history.jsonl").exists()
    # ...but the failure is visible: a hook that cannot fail a delivery would otherwise leave a
    # scene that STOPPED building looking exactly like one with nothing to rebuild.
    attempt = store.read_last_attempt(ws.report_dir)
    assert attempt["status"] == "failed" and "scene scoring exploded" in attempt["reason"]


def test_a_delivery_with_nothing_to_score_still_publishes_a_scene(tmp_path):
    """The same held delivery, hook intact. A workspace with nothing SCORED is still a legal
    scene, and publishing it is what makes the unchanged-digest path free on every later delivery
    — the alternative is a workspace whose scene appears only once something happens to fit.

    SCENE-4 PR 4.0 (#26) re-baseline: the three held-for-calibration scans are still ATTEMPTED,
    so each now carries its own measurement-arena record and a synthetic observation with empty
    candidates — non-scoring (``count_untrusted``, no footprint), but no longer silently absent
    from the revision the way a pre-4.0 held scan was."""
    ws, data = _s30_batch(tmp_path)
    summary = driver.run_batch(ws, [data], M=10, detectors="ana5")

    line, = [line for line in summary.context_lines if line.startswith("scene revision")]
    assert not line.startswith("scene revision skipped")
    pointer, payload = store.read_current(ws.report_dir)
    assert line == f"scene revision {pointer['digest'][:12]}"
    assert len(payload["observations"]) == 3
    assert all(o["candidates"] == [] for o in payload["observations"])
    assert {n["reason"] for n in payload["non_scoring"]} == {"count_untrusted"}
    assert payload["footprints"] == []
    assert payload["clusters"] == []
    assert payload["catalogue_version"] == "isar.catalogue.default/1"
    assert store.read_last_attempt(ws.report_dir) == {
        "status": "ok", "digest": pointer["digest"],
        "written_at": store.read_last_attempt(ws.report_dir)["written_at"]}
    # The driver is the writer of record, and the flip is in the durable lineage.
    history = (ws.report_dir / "scene" / "history.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(history) == 1 and '"source":"driver"' in history[0].replace(" ", "")


@pytest.mark.slow
@pytest.mark.skipif(not _have_in8, reason="IN8 test_data not present (gitignored)")
def test_a_real_delivery_publishes_the_scans_it_delivered(tmp_path):
    """The rich case, on the cheapest real-data driver fixture there is. The published revision
    verifies against its own digest and holds the arena the delivery actually produced — the hook
    reads the ledger the delivery just wrote, not a stale one.

    The SCORING arena is the POOLED MASTER, not the member scan: the master fitted the same
    neutrons (combine-before-divide), so the adapter's exactly-once rule supersedes the member
    row (numor 55182 — no synthetic observation for it either). Pinned here because it is the
    delivery-shaped half of that rule — the adapter tests build the two rows by hand.

    SCENE-4 PR 4.0 (#26) re-baseline: numor 55183 is UNDERSAMPLED, and used to vanish from the
    scene entirely — the exact silent-omission failure #26 removes. Its measurement-arena record
    now survives the gate, so the delivery honestly publishes it as a count-untrusted,
    candidate-less observation that is non-scoring and earns no coverage credit."""
    data = tmp_path / "in8"
    data.mkdir()
    for numor in ("055182", "055183"):
        shutil.copy(IN8_DIR / numor, data / numor)
    ws = _init_workspace(tmp_path, ill_in8)

    driver.run_batch(ws, [data], M=30)

    # read_current re-digests the parsed payload, so this asserts the blob and the pointer agree.
    pointer, payload = store.read_current(ws.report_dir)
    by_ref = {o["scan_ref"]: o for o in payload["observations"]}
    assert len(by_ref) == 2, sorted(by_ref)
    master_ref, = [r for r in by_ref if r.startswith("master:")]
    assert by_ref[master_ref]["candidates"], "the delivered arena carries no candidate table"
    attempted_ref, = [r for r in by_ref if not r.startswith("master:")]
    assert attempted_ref.endswith(":55183:det0") or ":55183:" in attempted_ref, attempted_ref
    assert not by_ref[attempted_ref]["candidates"]
    assert [(a["scan_ref"], a["reason"]) for a in payload["non_scoring"]] == [
        (attempted_ref, "count_untrusted")]
    assert (ws.report_dir / "scene" / f"{pointer['digest']}.json").is_file()

    # Re-running the same batch fits nothing new, so the scene rebuilds to the SAME digest and the
    # pointer does not move — the property the hook relies on to be free on every delivery.
    summary = driver.run_batch(ws, [data], M=30)
    assert any(line.startswith("scene revision") and line.endswith("(unchanged)")
               for line in summary.context_lines), summary.context_lines
    assert store.read_current(ws.report_dir)[0]["digest"] == pointer["digest"]
