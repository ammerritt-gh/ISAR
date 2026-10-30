"""The fake core's search (S3U1): the C-search protocol, and the real ``drive`` over the fake, on generated snapshots."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))       # the fake core is imported by name, as the other session tests do
import _inter_fakes as fakes  # noqa: E402
from isar.analyze.coupled.search import drive  # noqa: E402
from isar.analyze.coupled.settings import InterSettings  # noqa: E402
from isar.analyze.coupled.types import Priced, Snapshot  # noqa: E402


def _snapshot(**cfg) -> Snapshot:
    record = {"peaks": [{"omega0": 5.0, "branch": "b1"}, {"omega0": 9.0, "branch": "b2"}], "fake": cfg}
    own = fakes.held_from_fit(record, unit="y", fit="f0", inputs=None, context=None, T=100.0)
    return Snapshot(snapshot_id="b:ring1", stage_id="b:ring1", unit="y", fit_id="f1", context=None, own=own,
                    own_record=record, terms=(), settings=InterSettings(), batch_kind="anchored")


def test_a_triggered_spectrum_driven_by_drive_is_priced_and_stamped_on_its_stage():
    snap = _snapshot(triggered=True)
    search = fakes.open_search(snap)
    (job,) = search.next_jobs()
    assert job.starts[0].modes == tuple((m.omega0, m.gamma, m.height) for m in snap.own.modes)
    result, used = drive(search, lambda j: fakes.price(snap, j))
    assert result.discrete == "priced" and result.snapshot_id == snap.stage_id
    assert len(used) == 1 and used[0].key == job.key


def test_the_search_protocol_holds_before_and_after_the_feed():
    snap = _snapshot(triggered=True)
    search = fakes.open_search(snap)
    jobs = search.next_jobs()
    assert search.next_jobs() == jobs and not search.done          # idempotent without a feed
    with pytest.raises(RuntimeError):
        search.result()
    with pytest.raises(ValueError):
        search.feed([])
    with pytest.raises(ValueError):
        search.feed([Priced("not-the-key", 2, ("b1", "b2"), 0.0, "ok", None, None, 1, 0.0)])
    search.feed([fakes.price(snap, jobs[0])])
    assert search.next_jobs() == () and search.done
    assert search.result().discrete == "priced"


def test_a_spectrum_the_fake_does_not_trigger_is_done_at_once_and_skipped():
    search = fakes.open_search(_snapshot())
    assert search.done and search.next_jobs() == ()
    assert search.result().discrete == "skipped"


class _Silent:
    done = False

    def next_jobs(self):
        return ()


def test_drive_refuses_a_search_that_asks_for_nothing_while_not_done():
    with pytest.raises(RuntimeError):
        drive(_Silent(), fakes.price)
