"""Who builds the scene, and how many times (SCENE-4.3 D1 — owner ruling 2026-08-18).

Rewritten from ``test_driver_scene_hook.py``: there is no driver scene hook any more. The ONE
canonical build operation (``isar.session.scene_hook.build_and_store``) moved out of the analysis
child, and ``isar run`` became an explicit COMPOSITION of analysis + that build. Three claims, and
the first is the load-bearing one:

1. ``driver.run_batch`` — the analysis child, reached directly by the persistent session worker —
   builds no scene at all. That is what makes "exactly one scene build per delivery,
   parent-invoked" an invariant rather than a hope: on the campaign path the child cannot build a
   second one even if the parent's build fails.
2. The composed ``isar run`` still publishes the scene every one of its frontends already got (the
   GUI shells this command and has no scene call site of its own), and ``--no-scene`` — what the
   campaign's own runners pass — skips it.
3. A scene build that blows up still costs the delivery nothing. Fail-soft moved with the code;
   it did not evaporate.

This supersedes DECISIONS H14(d) ("the driver calls it, observe-only"); the docs record that in
SCENE-4.3 C9.
"""
import shutil

import pytest

from isar.cli.main import main as cli_main
from isar.drive.isar_io import runner as runner_mod
from isar.parsers import ill_in8, spec_s30
from isar.scene import build as scene_build
from isar.scene import store
from isar.session import driver
from isar.synth_format import run_builder

from .test_driver_smoke import IN8_DIR, _have_in8, _init_workspace


def _s30_batch(tmp_path):
    """The cheapest driver delivery in the suite: the held-for-calibration S30 bootstrap gate.

    Nothing is fitted, which is exactly what makes it the right isolation fixture — the build has
    to survive the empty case as well as the rich one, and it runs in about a second.
    """
    ws = _init_workspace(tmp_path, spec_s30)
    run = run_builder.build_s30_run("SYN", n_scans=3, seed0=1000)
    data = tmp_path / "SYN.spec_supplementary.dat"
    data.write_bytes(spec_s30.emit_file(run, spec_s30.card_template()))
    return ws, data


def _scene_artifacts(ws):
    """Every file the scene store would leave behind, attempt sidecar included."""
    scene_dir = ws.report_dir / "scene"
    return sorted(p.name for p in scene_dir.iterdir()) if scene_dir.is_dir() else []


def _run_cli(ws, data, *extra):
    return cli_main(["run", str(data), "--workspace", str(ws.root),
                     "--M", "10", "--detectors", "ana5", "--no-render", *extra])


# ----------------------------------------------------------------------------
# 1. The analysis child builds nothing
# ----------------------------------------------------------------------------
def test_run_batch_alone_never_builds_a_scene(tmp_path):
    """The ownership move, asserted at its own seam. ``run_batch`` parses, QCs, fits and
    persists — and stops. The persistent session worker (``python -m isar.session.worker``) calls
    exactly this, so the worker path is analysis-only BY this deletion, not by a second opt-out
    it would have to remember to pass."""
    ws, data = _s30_batch(tmp_path)

    summary = driver.run_batch(ws, [data], M=10, detectors="ana5")

    assert not hasattr(driver, "_observe_scene"), "the deleted hook is back"
    assert _scene_artifacts(ws) == [], "the analysis child published a scene"
    assert [line for line in summary.context_lines if "scene" in line] == []
    # The delivery itself is exactly the delivery it always was.
    _card, session = ws.load()
    assert {e.status for e in session.scans.values()} == {"held_for_calibration"}
    assert (ws.report_dir / "results.csv").exists()


# ----------------------------------------------------------------------------
# 2. The composed CLI, and its opt-out
# ----------------------------------------------------------------------------
def test_the_run_cli_composes_analysis_and_one_scene_build(tmp_path, monkeypatch):
    """The GUI's behaviour, preserved through the composition rather than through a hook. A
    workspace with nothing SCORED is still a legal scene, and publishing it is what makes the
    unchanged-digest path free on every later delivery."""
    ws, data = _s30_batch(tmp_path)

    # Count real invocations: history.jsonl alone cannot catch a duplicate build, because the
    # unchanged-digest path appends no history line (review finding — the counter is the pin).
    from isar.session import scene_hook
    calls = []
    real = scene_hook.build_and_store
    monkeypatch.setattr(scene_hook, "build_and_store",
                        lambda root, **kw: calls.append(root) or real(root, **kw))

    assert _run_cli(ws, data) == 0
    assert len(calls) == 1, f"expected exactly one scene build, saw {len(calls)}"

    pointer, payload = store.read_current(ws.report_dir)
    assert len(payload["observations"]) == 3
    assert all(o["candidates"] == [] for o in payload["observations"])
    assert {n["reason"] for n in payload["non_scoring"]} == {"count_untrusted"}
    assert payload["catalogue_version"] == "isar.catalogue.default/1"
    assert store.read_last_attempt(ws.report_dir)["digest"] == pointer["digest"]
    # ONE flip, and the lineage still names the writer.
    history = (ws.report_dir / "scene" / "history.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(history) == 1 and '"source":"driver"' in history[0].replace(" ", "")


def test_no_scene_runs_the_analysis_and_publishes_nothing(tmp_path):
    """The campaign runners' invocation. Analysis is unaffected — same ledger, same results.csv —
    and the scene directory is not merely unmoved but absent: nothing was attempted, so nothing
    can be mistaken for a stale-but-current scene."""
    ws, data = _s30_batch(tmp_path)

    assert _run_cli(ws, data, "--no-scene") == 0

    assert _scene_artifacts(ws) == []
    assert store.read_current(ws.report_dir) is None
    assert (ws.report_dir / "results.csv").exists()
    _card, session = ws.load()
    assert {e.status for e in session.scans.values()} == {"held_for_calibration"}


# ----------------------------------------------------------------------------
# 3. Exactly one build per delivery on the campaign path
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("invoke", [
    lambda ws, data: runner_mod.SubprocessRunner().run(ws.root, [data], errors_tier="cheap"),
    lambda ws, data: runner_mod.run_isar(ws.root, [data]),
])
def test_every_campaign_side_isar_run_invocation_is_analysis_only(tmp_path, monkeypatch, invoke):
    """Both campaign-side shell-outs pass ``--no-scene``. The campaign PARENT owns the one build
    per delivery and pins the digest it gets back; a child building its own would be a second
    build from the same ledger, off the parent's critical path and outside its pin."""
    ws, data = _s30_batch(tmp_path)
    seen = []
    monkeypatch.setattr(runner_mod, "_exec_run",
                        lambda cmd, timeout: seen.append(list(cmd)) or runner_mod.RunResult(0, "", ""))

    invoke(ws, data)

    cmd, = seen
    assert "--no-scene" in cmd
    assert cmd[cmd.index("run") + 1] == str(data), "the flag displaced a positional"


def test_a_delivery_on_the_campaign_path_publishes_no_child_built_revision(tmp_path):
    """The invariant end to end, through the real subprocess the campaign actually spawns: after a
    campaign-shaped delivery the store is empty, so any revision a later assertion finds can only
    have come from the parent."""
    ws, data = _s30_batch(tmp_path)

    result = runner_mod.SubprocessRunner().run(ws.root, [data], errors_tier="cheap", render=False)

    assert result.returncode == 0, result.stderr
    assert (ws.report_dir / "results.csv").exists(), "the analysis did not run"
    assert _scene_artifacts(ws) == []


# ----------------------------------------------------------------------------
# 4. Fail-soft, at its new home
# ----------------------------------------------------------------------------
def test_a_raising_scene_build_is_logged_and_skipped(tmp_path, monkeypatch):
    """The isolation contract, moved with the code. A scene build that blows up leaves the
    delivery byte-for-byte the delivery it would have been, and the only trace is a context line
    an operator can see — plus the attempt sidecar, without which a scene that STOPPED building
    would look exactly like one with nothing to rebuild."""
    ws, data = _s30_batch(tmp_path)

    def boom(*_a, **_kw):
        raise RuntimeError("scene scoring exploded")

    monkeypatch.setattr(scene_build, "build_scene", boom)
    assert _run_cli(ws, data) == 0                     # the run still SUCCEEDS

    _card, session = ws.load()
    assert {e.status for e in session.scans.values()} == {"held_for_calibration"}
    assert (ws.report_dir / "results.csv").exists()
    assert _scene_artifacts(ws) == ["last_attempt.json"]   # nothing published
    attempt = store.read_last_attempt(ws.report_dir)
    assert attempt["status"] == "failed" and "scene scoring exploded" in attempt["reason"]


@pytest.mark.slow
@pytest.mark.skipif(not _have_in8, reason="IN8 test_data not present (gitignored)")
def test_a_real_delivery_publishes_the_scans_it_delivered(tmp_path):
    """The rich case, on the cheapest real-data fixture there is. The published revision verifies
    against its own digest and holds the arena the delivery actually produced — the composition
    reads the ledger the delivery just wrote, not a stale one.

    The SCORING arena is the POOLED MASTER, not the member scan: the master fitted the same
    neutrons (combine-before-divide), so the adapter's exactly-once rule supersedes the member row
    (numor 55182 — no synthetic observation for it either).

    SCENE-4 PR 4.0 (#26) re-baseline: numor 55183 is UNDERSAMPLED and used to vanish from the
    scene entirely. Its measurement-arena record now survives the gate, so the delivery honestly
    publishes it as a count-untrusted, candidate-less observation earning no coverage credit."""
    data = tmp_path / "in8"
    data.mkdir()
    for numor in ("055182", "055183"):
        shutil.copy(IN8_DIR / numor, data / numor)
    ws = _init_workspace(tmp_path, ill_in8)

    assert cli_main(["run", str(data), "--workspace", str(ws.root), "--M", "30",
                     "--no-render"]) == 0

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
    # pointer does not move — the property that makes the per-delivery build free when idle.
    assert cli_main(["run", str(data), "--workspace", str(ws.root), "--M", "30",
                     "--no-render"]) == 0
    assert store.read_current(ws.report_dir)[0]["digest"] == pointer["digest"]
