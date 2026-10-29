"""The inter-spectra user values (C-settings-store): the checked writers and the folded read (S1U5, packet 1).

Anchors, picks, locks and settings are the user's; ISAR never writes them. Every writer refuses whole or writes one
line, and a refused save leaves ``intake/inter_spectra.jsonl`` byte-identical.
"""
import json

import pytest

from isar.analyze.coupled.settings import InterSettings
from isar.intake import inter_spectra, scans, store
from isar.intake.inter_spectra import LockState, Pick, read_inter_settings, read_user
from isar.intake.inter_values import pick_fit, save_settings, set_anchors, set_lock
from isar.session.state import ScanEntry, SessionState

A, B = "spec1:42:a03", "spec1:42:a04"
F1, F2 = "f0123456789ab", "fabcdef012345"


def _fit(root, *units):
    """A stored fast solo fit for each unit, the shape ``file_fit`` leaves in the ledger."""
    state = SessionState()
    for unit in units:
        scan, det = unit.rsplit(":", 1)
        state.put(ScanEntry(scan_id=unit, scan_number=int(scan.split(":")[1]), scan_type="dhobox",
                            status="fitted", detectors={det: {"fast_pass": {"state": "resolved"}}}))
    state.save(root / "session.json")


@pytest.fixture
def root(tmp_path):
    """Two scans on record (file T 97 K); spec1:42:a03 and a04 hold fast fits, spec1:43:a04 has none."""
    scans.record_import(tmp_path, [{"scan": k, "facts": {"T_K": {"value": 97.0, "source": "trailer"}}}
                                   for k in ("spec1:42", "spec1:43")])
    _fit(tmp_path, A, B, "spec1:43:a03")
    return tmp_path


def _bytes(root):
    path = root / inter_spectra.FILE
    return path.read_bytes() if path.exists() else None


def test_an_empty_workspace_reads_as_nothing(tmp_path):
    assert read_user(tmp_path) == inter_spectra.InterUser((), {}, {}, {}, 0, ())
    assert read_inter_settings(tmp_path) == InterSettings()


def test_anchors_follow_the_latest_declaring_entry_in_seq_order(root):
    assert set_anchors(root, [A], anchored=True).ok        # seq 1
    assert set_anchors(root, [B], anchored=True).ok        # seq 2
    assert set_anchors(root, [A], anchored=False).ok       # seq 3
    assert set_anchors(root, [A], anchored=True).ok        # seq 4
    assert read_user(root).anchors == (B, A)               # A's latest declaring entry is the newest


def test_a_repeated_anchor_writes_nothing(root):
    assert set_anchors(root, [A], anchored=True).ok
    before = _bytes(root)
    r = set_anchors(root, [A], anchored=True)
    assert (r.ok, r.reason, r.value) == (True, None, True)
    assert _bytes(root) == before


def test_setting_many_anchors_reads_the_ledger_once(root, monkeypatch):
    """One save reads the session ledger once, not once per analyzer: the GUI thread writes it (C-gui-state)."""
    loads = []
    load = SessionState.load.__func__
    monkeypatch.setattr(SessionState, "load", classmethod(lambda cls, path: loads.append(path) or load(cls, path)))
    assert set_anchors(root, [A, B, "spec1:43:a03"], anchored=True).ok
    assert len(loads) == 1


def test_picks_and_locks_fold_latest_wins(root):
    assert pick_fit(root, A, "fast").ok                    # seq 1
    assert pick_fit(root, A, F1).ok                        # seq 2
    assert set_lock(root, A, locked=True, fit_id=F2).ok    # seq 3
    assert set_lock(root, A, locked=False).ok              # seq 4
    assert pick_fit(root, A, F2).ok                        # seq 5: picks work again once unlocked
    assert set_lock(root, A, locked=True, fit_id=F1).ok    # seq 6: locking again re-pins
    u = read_user(root)
    assert u.picks[A] == Pick(F2, 5)
    assert u.locks[A] == LockState(True, F1, 6)


def test_settings_deltas_fold_and_none_goes_back_to_default(root):
    assert save_settings(root, {"sweep_cap": 30, "flag_threshold": 60}).ok
    assert save_settings(root, {"sweep_cap": None}).ok
    assert read_user(root).settings == {"flag_threshold": 60.0}
    s = read_inter_settings(root)
    assert (s.flag_threshold, s.sweep_cap) == (60.0, 20)


@pytest.mark.parametrize("call, reason", [
    (lambda r: set_anchors(r, ["spec1:99:a03"], anchored=True), "unknown scan spec1:99 (in spec1:99:a03)"),
    (lambda r: set_anchors(r, ["spec1:42:a10"], anchored=True), "a10 is not a BL43 analyzer (spec1:42:a10)"),
    (lambda r: set_anchors(r, ["spec1:43:a04"], anchored=True), "spec1:43:a04 has no stored fast fit"),
    (lambda r: set_anchors(r, [], anchored=True), "no targets given"),
    (lambda r: set_anchors(r, A, anchored=True), "targets must be a list of unit keys, not a string"),
    (lambda r: set_anchors(r, [A], anchored=True, reason=5), "reason must be text (got int)"),
    (lambda r: set_anchors(r, [A], anchored="yes"), "anchored must be true or false"),
    (lambda r: pick_fit(r, A, "f12"), "fit id 'f12' is not 'fast' or 'f' followed by 12 hex digits"),
    (lambda r: pick_fit(r, "spec1:42", "fast"), "spec1:42 names a scan, not an analyzer (a target is <scan key>:aNN)"),
    (lambda r: set_lock(r, A, locked=True), "a lock needs the fit it pins (fast, or f followed by 12 hex digits)"),
    (lambda r: set_lock(r, A, locked=False), "spec1:42:a03 is not locked"),
    (lambda r: set_lock(r, A, locked=False, fit_id=F1), "fit_id is only given to lock a spectrum"),
    (lambda r: save_settings(r, {"sweep_cap": 0}), "inter-spectra setting 'sweep_cap' is out of bounds (got 0)"),
    (lambda r: save_settings(r, {"bogus": 1}), "unknown inter-spectra setting 'bogus'"),
    (lambda r: save_settings(r, {"bogus": None}), "unknown inter-spectra setting 'bogus'"),
    (lambda r: save_settings(r, {"flag_threshold": True}),
     "inter-spectra setting 'flag_threshold' must be a finite number (got True)"),
    (lambda r: save_settings(r, {}), "no settings given"),
])
def test_a_refused_save_writes_nothing_and_says_why(root, call, reason):
    assert set_anchors(root, [B], anchored=True).ok        # something already on record
    before = _bytes(root)
    r = call(root)
    assert (r.ok, r.reason, r.value, r.entry_id) == (False, reason, None, None)
    assert _bytes(root) == before


def test_a_pick_is_refused_while_locked_and_the_file_stays_byte_identical(root):
    assert set_lock(root, A, locked=True, fit_id=F1).ok
    before = _bytes(root)
    r = pick_fit(root, A, "fast")
    assert (r.ok, r.reason, r.value, r.entry_id) == (False, f"locked to {F1}; unlock first", None, None)
    assert _bytes(root) == before


def test_an_unlock_of_an_unlocked_spectrum_is_refused(root):
    r = set_lock(root, A, locked=False)
    assert (r.ok, r.reason) == (False, "spec1:42:a03 is not locked")
    assert _bytes(root) is None                              # nothing was written, not even an empty line


def test_damaged_lines_are_skipped_and_listed_and_the_rest_still_counts(root):
    assert pick_fit(root, A, F1).ok                          # line 1
    lines = [
        {"kind": "weather", "id": "e1", "seq": 2, "at": "x", "targets": [A]},                 # line 2: unknown kind
        {"kind": "pick", "id": "e2", "seq": 3, "at": "x", "targets": [A]},                    # line 3: no fit
        {"kind": "settings", "changes": {"sweep_cap": 0}, "id": "e3", "seq": 4, "at": "x",
         "targets": ["experiment"]},                                                          # line 4: out of bounds
    ]
    with open(root / inter_spectra.FILE, "ab") as f:
        for entry in lines:
            f.write(json.dumps(entry).encode("utf-8") + b"\n")
        f.write(b'{"kind":"pick","fit"')                     # line 5: a torn last line
    u = read_user(root)
    assert u.picks == {A: Pick(F1, 1)}
    assert u.settings == {}
    assert u.max_seq == 4                                    # the torn line has no seq and does not count
    assert [d.line for d in u.damaged] == [2, 3, 4, 5]
    assert all(d.file == inter_spectra.FILE for d in u.damaged)
