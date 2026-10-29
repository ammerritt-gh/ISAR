"""InterState (C-gui-state): reads run on one worker, an answer from before a reload is dropped, a writer saves one
delta and then reloads, and a refresh never writes. The facade reads and the writers are monkeypatched here (their
real bodies are S1U5's); the worker and the debounce are real."""
from __future__ import annotations

import logging
import threading

import pytest

from gui.inter_fakes import make_entry, make_history, make_overview, make_preview
from isar.intake import inter_values
from isar.intake.settings import SaveResult
from isar.workspace_api import inter_spectra as reads
from isar.workspace_api.inter_spectra import BatchRequest, SpectrumInter
from isar_gui.viewmodels.intake import NO_EXPERIMENT, IntakeState
from isar_gui.viewmodels.inter_state import InterState

KEY = "e:1:a03"


def _spectrum(key: str = KEY) -> SpectrumInter:
    return SpectrumInter(key=key, fit_id="fast", in_use=False, source="fast", label="Fast solo", flag=None,
                         locked=False, locked_fit_id=None, lock_stale=False, stale=False, stale_text=None)


def _saved(ok: bool = True, targets=()) -> SaveResult:
    return SaveResult(ok=ok, reason=None if ok else "refused", value=None, targets=tuple(targets), entry_id="x")


@pytest.fixture
def intake(qtbot):
    return IntakeState()


@pytest.fixture
def state(qtbot, intake):
    return InterState(intake)


@pytest.fixture
def overview(monkeypatch):
    """The overview read: ``value`` is what it returns, ``raise`` makes it fail, ``calls`` counts the reads."""
    box = {"value": make_overview(revision="1"), "calls": 0, "raise": None}

    def read(root):
        box["calls"] += 1
        if box["raise"]:
            raise box["raise"]
        return box["value"]

    monkeypatch.setattr(reads, "inter_overview", read)
    return box


def test_bind_reads_the_overview_and_emits_changed(qtbot, state, overview, tmp_path):
    overview["value"] = make_overview(revision="7", anchors=(KEY,), by_key={KEY: _spectrum()})
    seen = []
    state.changed.connect(lambda: seen.append(state.revision))
    state.bind(tmp_path)
    qtbot.waitUntil(lambda: state.revision == "7", timeout=5000)
    assert seen == ["7"]
    assert state.anchors == (KEY,)
    assert state.row(KEY).fit_id == "fast"
    assert state.row("e:9:a01") is None


def test_an_answer_from_before_a_reload_is_dropped(qtbot, state, monkeypatch, tmp_path):
    started, gate = threading.Event(), threading.Event()
    calls = []

    def read(root):
        calls.append(root)
        if len(calls) == 1:
            started.set()
            assert gate.wait(timeout=5)
            return make_overview(revision="old")
        return make_overview(revision="new")

    monkeypatch.setattr(reads, "inter_overview", read)
    seen = []
    state.changed.connect(lambda: seen.append(state.revision))
    state.bind(tmp_path)
    assert started.wait(timeout=5)      # the first read is in flight before the reload starts
    state.reload()                      # the newer read queues behind it
    gate.set()
    qtbot.waitUntil(lambda: state.revision == "new", timeout=5000)
    assert seen == ["new"]              # the old answer arrived and was dropped, never shown


def test_unbound_empties_every_read_and_refuses_a_write(qtbot, state, overview, tmp_path, monkeypatch):
    state.bind(tmp_path)
    qtbot.waitUntil(lambda: state.revision == "1", timeout=5000)
    monkeypatch.setattr(inter_values, "set_anchors", lambda *a, **k: pytest.fail("writer called while unbound"))
    state.bind(None)
    assert state.revision == "" and state.anchors == () and state.row(KEY) is None
    outcome = state.set_anchors([KEY], True)
    assert not outcome.ok and outcome.message == NO_EXPERIMENT


def test_a_writer_saves_one_delta_then_reloads(qtbot, state, overview, monkeypatch, tmp_path):
    writes = []

    def set_anchors(root, keys, *, anchored, reason):
        writes.append((root, tuple(keys), anchored, reason))
        return _saved(targets=keys)

    monkeypatch.setattr(inter_values, "set_anchors", set_anchors)
    state.bind(tmp_path)
    qtbot.waitUntil(lambda: state.revision == "1", timeout=5000)
    saved = []
    state.saved.connect(saved.append)
    reads_before = overview["calls"]
    outcome = state.set_anchors([KEY, "e:1:a04"], True)
    assert outcome.ok and outcome.message == "Saved: 2 spectra set as anchors"
    assert writes == [(tmp_path, (KEY, "e:1:a04"), True, None)]
    assert saved == [outcome.message]
    qtbot.waitUntil(lambda: overview["calls"] > reads_before, timeout=5000)


def test_each_writer_passes_its_arguments_through(qtbot, state, monkeypatch, tmp_path):
    calls = {}

    def spy(name):
        def write(root, *args, **kwargs):
            calls[name] = (args, kwargs)
            return _saved()
        return write

    monkeypatch.setattr(inter_values, "pick_fit", spy("pick_fit"))
    monkeypatch.setattr(inter_values, "set_lock", spy("set_lock"))
    monkeypatch.setattr(inter_values, "save_settings", spy("save_settings"))
    state.bind(tmp_path)
    assert state.pick_fit(KEY, "f1").ok
    assert state.set_lock(KEY, True, "f1").ok
    assert state.save_settings({"flag_threshold": 50.0, "sweep_cap": None}).ok
    assert calls == {
        "pick_fit": ((KEY, "f1"), {}),
        "set_lock": ((KEY,), {"locked": True, "fit_id": "f1"}),
        "save_settings": (({"flag_threshold": 50.0, "sweep_cap": None},), {}),
    }


def test_a_busy_export_refuses_the_write_without_reloading(qtbot, state, intake, overview, monkeypatch, tmp_path):
    writes = []
    monkeypatch.setattr(inter_values, "set_anchors", lambda *a, **k: writes.append(a))
    state.bind(tmp_path)
    qtbot.waitUntil(lambda: state.revision == "1", timeout=5000)
    intake.set_busy_reason("An export is running; try again when it finishes")
    saved = []
    state.saved.connect(saved.append)
    reads_before = overview["calls"]
    outcome = state.set_anchors([KEY], True)
    assert not outcome.ok and outcome.message == "An export is running; try again when it finishes"
    assert writes == [] and saved == [outcome.message]
    assert overview["calls"] == reads_before


def test_a_failed_read_is_logged_and_the_last_good_overview_stays(qtbot, state, overview, tmp_path, caplog):
    state.bind(tmp_path)
    qtbot.waitUntil(lambda: state.revision == "1", timeout=5000)
    overview["raise"] = RuntimeError("the fit log is unreadable")
    with caplog.at_level(logging.ERROR, logger="isar_gui.viewmodels.inter_state"):
        state.reload()
        qtbot.waitUntil(lambda: state.error is not None, timeout=5000)
    assert state.revision == "1"
    assert state.error == "overview: RuntimeError: the fit log is unreadable"
    assert any(r.levelno == logging.ERROR and "failed" in r.getMessage() for r in caplog.records)


def test_history_is_read_once_cached_and_dropped_by_a_reload(qtbot, state, overview, monkeypatch, tmp_path):
    asked = []

    def history(root, key):
        asked.append(key)
        return make_history(make_entry("f1", "Batch 1 · ring 1", batch_id="b1"))

    monkeypatch.setattr(reads, "inter_history", history)
    state.bind(tmp_path)
    qtbot.waitUntil(lambda: state.revision == "1", timeout=5000)
    ready = []
    state.history_ready.connect(ready.append)
    assert state.history(KEY) is None
    qtbot.waitUntil(lambda: ready == [KEY], timeout=5000)
    assert state.history(KEY)[1].fit_id == "f1"
    assert state.history(KEY) is not None and asked == [KEY]
    state.reload()
    assert state.history(KEY) is None        # a save may have changed the fits: the cache is gone
    qtbot.waitUntil(lambda: ready == [KEY, KEY], timeout=5000)
    assert asked == [KEY, KEY]


def test_a_preview_request_debounces_and_only_the_newest_is_computed(qtbot, state, overview, monkeypatch, tmp_path):
    asked = []

    def preview(root, req):
        asked.append(req)
        return make_preview(line=f"{len(req.anchors)} anchors")

    monkeypatch.setattr(reads, "batch_preview", preview)
    state.bind(tmp_path)
    qtbot.waitUntil(lambda: state.revision == "1", timeout=5000)
    ready = []
    state.preview_ready.connect(ready.append)
    a = BatchRequest(kind="anchored", direction="q", anchors=(KEY,), selection=())
    b = BatchRequest(kind="anchored", direction="q", anchors=(KEY, "e:1:a04"), selection=())
    c = BatchRequest(kind="anchored", direction="T", anchors=(KEY, "e:1:a04"), selection=())
    state.request_preview(a)
    state.request_preview(b)
    state.request_preview(c)
    qtbot.waitUntil(lambda: ready == [c], timeout=5000)
    assert asked == [c]
    assert state.preview_for(c).line == "2 anchors"
    assert state.preview_for(a) is None


def test_a_preview_that_finishes_after_a_newer_request_is_dropped(qtbot, state, overview, monkeypatch, tmp_path):
    held = threading.Event()
    asked = []

    def preview(root, req):
        asked.append(req)
        if req.direction == "q":            # the first request is held until the newer one is queued
            held.wait(5)
        return make_preview(line=f"{len(req.anchors)} anchors")

    monkeypatch.setattr(reads, "batch_preview", preview)
    state.bind(tmp_path)
    qtbot.waitUntil(lambda: state.revision == "1", timeout=5000)
    ready = []
    state.preview_ready.connect(ready.append)
    a = BatchRequest(kind="anchored", direction="q", anchors=(KEY,), selection=())
    b = BatchRequest(kind="anchored", direction="T", anchors=(KEY, "e:1:a04"), selection=())
    state.request_preview(a)
    qtbot.waitUntil(lambda: asked == [a], timeout=5000)
    state.request_preview(b)
    qtbot.waitUntil(lambda: ("preview", b) in state._loading, timeout=5000)
    held.set()
    qtbot.waitUntil(lambda: ready[-1:] == [b], timeout=5000)
    assert ready == [b]                      # a's late answer is never emitted
    assert state.preview_for(a) is None


def test_a_preview_that_raises_is_reported_and_a_later_request_retries(qtbot, state, overview, monkeypatch, tmp_path):
    failing = {"on": True}
    asked = []

    def preview(root, req):
        asked.append(req)
        if failing["on"]:
            raise ValueError("the fit log is unreadable")
        return make_preview(line="1 anchor")

    monkeypatch.setattr(reads, "batch_preview", preview)
    state.bind(tmp_path)
    qtbot.waitUntil(lambda: state.revision == "1", timeout=5000)
    failed = []
    state.preview_failed.connect(failed.append)
    req = BatchRequest(kind="anchored", direction="q", anchors=(KEY,), selection=())
    state.request_preview(req)
    qtbot.waitUntil(lambda: failed == [req], timeout=5000)
    assert state.preview_error_for(req) == "preview: ValueError: the fit log is unreadable"
    assert ("preview", req) not in state._loading and state.preview_for(req) is None
    failing["on"] = False
    state.request_preview(req)
    qtbot.waitUntil(lambda: state.preview_for(req) is not None, timeout=5000)
    assert asked == [req, req]


def test_a_refresh_calls_no_writer(qtbot, state, overview, monkeypatch, tmp_path):
    writes = []
    for name in ("set_anchors", "pick_fit", "set_lock", "save_settings"):
        monkeypatch.setattr(inter_values, name, lambda *a, _name=name, **k: writes.append(_name))
    seen = []
    state.changed.connect(lambda: seen.append(state.revision))
    state.bind(tmp_path)
    state.reload()
    qtbot.waitUntil(lambda: seen == ["1"], timeout=5000)
    qtbot.wait(20)
    assert writes == []
