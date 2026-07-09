"""Fake-seam determinism / identity harness — the Stage-0 refactor guard.

This module does NOT test fit numerics (the fakes write fixed rows). Its job is to lock the
*behavioral contract* of a fake-seam campaign so the pure decompositions of ``isar/drive/loop.py``
and ``isar/session/driver.py`` (plan §E) cannot silently change what the driver *decides*, how it
*folds rows*, or the *flow of events* it emits.

It reuses the fakes already built for ``test_loop.py`` (the MockTavi client + the mock ``isar run``
runner that writes a deterministic ``report/results.csv``) rather than re-inventing them.

Two guards:

* **Test A (determinism):** the same seeded campaign, run twice into two fresh workspaces, must
  produce a byte-identical ``report/results.csv`` and an identical sequence of decide/stop/propose
  events (ignoring the ``ts`` stamp, wall-clock ``seconds``/``eta`` fields, and the profiling
  ``timing`` events). A refactor that reorders decisions or folds rows differently breaks this.
* **Test B (event-flow contract):** the set of ``timing`` span names must still cover the required
  stages (subset — tolerant of ADDITIVE new spans), and within every iteration that proposes a scan
  the decision events must appear in the exact order propose → validate → run → analyze. A refactor
  that drops/renames a span or reorders the decision events fails loudly.
"""
import json
from collections import defaultdict

from isar.drive.loop import run_campaign
from isar.drive.session_log import read_log

# Reuse the fakes + fixtures already built for the loop tests (sibling module in this package).
from drive.test_loop import MockTavi, make_mock_runner, _objective, _UNITY_CAL

# ω(q) that keeps every peak inside the first survey window: the campaign reaches "met" and runs its
# certification pass, so decide events include the certify_start / certify_done pair.
_OMEGA = lambda h: (h - 2.0) * 2.0

# Fields that legitimately vary run-to-run (wall clock, per-workspace absolute paths) or are pure
# profiling noise. They are excluded from the identity comparison so the guard tracks *decisions*,
# not timing or the tmp-dir a run happened to land in. ``probe`` is the absolute path of the init
# data file (embeds the workspace dir); it is workspace-dependent, not a decision.
_VOLATILE_KEYS = frozenset({"ts", "seconds", "eta", "timings", "probe"})
_DECISION_KINDS = ("propose", "validate", "run", "analyze")


def _run_campaign_in(ws, *, seed=7):
    """Run one full fake-seam campaign into workspace ``ws`` and return its summary.

    Fresh MockTavi + fresh mock runner per call (so two invocations share NO mutable state), same
    seed/objective → deterministic. Mirrors ``test_loop._run`` but takes an explicit workspace so
    Test A can drive two independent trees.
    """
    (ws / "report").mkdir(parents=True, exist_ok=True)
    cfg = {"workspace": ws, "client": MockTavi(_OMEGA), "run_isar": make_mock_runner(_OMEGA),
           "calibration": _UNITY_CAL, "resolution": {"fwhm_dE": 0.8}, "seed": seed}
    return run_campaign(_objective(), cfg)


def _decision_signature(events):
    """A replayable fingerprint of the decide/stop/propose stream.

    Each decision event → ``(kind, canonical-json of its payload minus volatile fields)``. Comparing
    the full projected payload (keys AND stable values) — not just the key set — means a refactor that
    proposes a different q, decides differently, or stops for a different reason changes the signature.
    ``timing`` events and wall-clock fields are excluded.
    """
    sig = []
    for e in events:
        kind = e.get("kind")
        if kind not in ("decide", "stop", "propose"):
            continue
        payload = {k: v for k, v in (e.get("payload") or {}).items() if k not in _VOLATILE_KEYS}
        sig.append((kind, json.dumps(payload, sort_keys=True, ensure_ascii=False)))
    return sig


# --------------------------------------------------------------------------- Test A: determinism
def test_seeded_campaign_is_bit_identical_across_two_fresh_workspaces(tmp_path):
    s1 = _run_campaign_in(tmp_path / "run1")
    s2 = _run_campaign_in(tmp_path / "run2")

    # (a) The analysis-boundary artifact is byte-identical.
    csv1 = (tmp_path / "run1" / "report" / "results.csv").read_bytes()
    csv2 = (tmp_path / "run2" / "report" / "results.csv").read_bytes()
    assert csv1 == csv2, "results.csv differs between two identically-seeded fake-seam runs"

    # (b) The decide/stop/propose event stream is identical (modulo ts / timing / wall-clock fields).
    sig1 = _decision_signature(read_log(s1["session_log"]))
    sig2 = _decision_signature(read_log(s2["session_log"]))
    assert sig1, "campaign produced no decide/stop/propose events to fingerprint"
    assert sig1 == sig2, "decision-event sequence diverged between two identically-seeded runs"

    # Both runs reach the same terminal decision (sanity anchor on the fixture).
    assert s1["stop"]["state"] == s2["stop"]["state"] == "met"


# --------------------------------------------------------------------------- Test B: flow contract
# Spans that MUST exist (subset check — new spans may be added without breaking the guard).
_REQUIRED_SPANS = {"stop_eval", "model_fit", "propose", "validate", "submit", "wait_scan",
                   "isar_run", "fold_rows", "iteration"}


def test_campaign_event_flow_matches_refactor_contract(tmp_path):
    summary = _run_campaign_in(tmp_path / "run")
    events = read_log(summary["session_log"])

    # Timing spans: the required set must be present; ADDITIVE new spans are fine (subset assertion).
    span_names = {e["payload"]["stage"] for e in events if e.get("kind") == "timing"}
    missing = _REQUIRED_SPANS - span_names
    assert not missing, f"refactor dropped/renamed required timing span(s): {sorted(missing)}"

    # Per-iteration decision ordering: within each iteration that proposed a scan, the decision events
    # must appear in exactly propose → validate → run → analyze. Reordering (or dropping one) fails.
    by_iter = defaultdict(list)
    for e in events:
        if e.get("kind") in _DECISION_KINDS:
            it = (e.get("payload") or {}).get("iter")
            if isinstance(it, int):
                by_iter[it].append(e["kind"])

    proposing_iters = [it for it, kinds in by_iter.items() if "propose" in kinds]
    assert proposing_iters, "no iteration emitted a full propose→...→analyze decision chain"
    for it in proposing_iters:
        assert by_iter[it] == list(_DECISION_KINDS), (
            f"iteration {it} decision-event order changed: {by_iter[it]} "
            f"(expected {list(_DECISION_KINDS)})")
