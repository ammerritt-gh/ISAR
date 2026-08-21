"""The REAL scene build over a mixed canonical-path/context-probe measurement set (SCENE-4 PR
4.4, D5) -- the counterpart ``tests/drive/test_off_path_world.py``'s seam test does not cover: an
actual ``isar.session.scene_hook.build_and_store`` build (the SAME operation ``isar scene-build``,
``isar run``, and ``isar report --scene`` all share, per ``isar/cli/scene_build_cmd.py``'s own
docstring) over arenas whose ``measurements.jsonl`` records declare ``topology_role`` -- not a
hand-built ``Observation`` list. Contrast ``tests/scene/test_partition.py``, which drives
``build_scene`` directly over synthetic ``Observation`` objects; this file drives the SAME claim
through the real workspace-adapter + store path, using ``tests/scene/test_workspace_api_scene.py``'s
own row/measurement-record fixtures (``_row``/``_candidate`` for the delivered-fit ledger,
``_measurement``/``_write_measurement`` for the counts-authoritative record that carries
provenance) rather than reinventing them.

Claim: a context-probe arena at an off-path coordinate, delivered beside canonical-path arenas,
(a) builds into its OWN partition with the None path component (never assigned a ``path_index``),
(b) leaves the canonical partition's membership untouched -- identical to a control build made
without the probe arena at all -- and (c) the build never refuses.
"""
from __future__ import annotations

from isar.scene import store
from isar.scene.model import path_geometry_fingerprint
from isar.session import scene_hook
from synthetic_ws import stamp_ws

from .test_workspace_api_scene import COLUMNS, _candidate, _measurement, _row, _write_measurement

PATH_START = (2.0, 0.0, 0.0)
PATH_END = (2.2, 0.0, 0.0)
PATH_FP = path_geometry_fingerprint(PATH_START, PATH_END)
PROBE_Q = (2.1, 0.4, 0.0)   # off the (2.0,0,0)->(2.2,0,0) segment
#: ``_row``'s default ``scan_id`` (``tavi:<scan>:det``) already ends in the detector segment
#: ``_scan_ref`` requires -- an ``scan_id`` that does not would get the detector appended a
#: second time, and the measurement record would key under the wrong ref (isar/workspace_api
#: /scene.py's ``_scan_ref``).
CANONICAL_REFS = ["tavi:1:det", "tavi:2:det", "tavi:3:det"]
PROBE_REF = "tavi:9:det"


def _canonical_rows_and_measurements(root):
    """Three canonical-path arenas, coupling into one path component -- the same declared-path
    fixture shape ``tests/scene/test_partition.py`` pins, driven here through the real ledger +
    measurement-record pair instead of a hand-built ``Observation``."""
    rows = []
    for i, scan_ref in enumerate(CANONICAL_REFS, start=1):
        h = PATH_START[0] + 0.1 * (i - 1)
        rows.append(_row(i, candidates=[_candidate("stokes+bg", omegas=[5.0 + 0.1 * i])],
                         h=h, k=0.0, l=0.0))
        _write_measurement(root, _measurement(
            scan_ref=scan_ref, scan_id=scan_ref, scan_number=i, q=(h, 0.0, 0.0),
            topology_role="canonical_path", path_fingerprint=PATH_FP))
    return rows


def _probe_row_and_measurement(root):
    row = _row(9, candidates=[_candidate("stokes+bg", omegas=[5.0])],
              h=PROBE_Q[0], k=PROBE_Q[1], l=PROBE_Q[2])
    _write_measurement(root, _measurement(
        scan_ref=PROBE_REF, scan_id=PROBE_REF, scan_number=9, q=PROBE_Q,
        topology_role="context_probe", path_fingerprint=None))
    return row


def _build(root, rows) -> dict:
    """Drives the REAL builder ``isar scene-build``/``isar run`` share -- never a hand-assembled
    scene payload."""
    root.mkdir(parents=True, exist_ok=True)
    stamp_ws(root, rows=rows, columns=COLUMNS)
    scene_hook.build_and_store(root, source="test")
    _pointer, payload = store.read_current(root / "report")
    return payload


def test_a_context_probe_among_canonical_arenas_partitions_separately_and_nothing_refuses(tmp_path):
    # The CONTROL builds FIRST: if build_and_store ever grew process-global state, contamination
    # could then only flow control -> probed build, never into the reference the comparison
    # trusts (review-hardened ordering; the roots are separate either way).
    control_root = tmp_path / "control"
    control_rows = _canonical_rows_and_measurements(control_root)
    control = _build(control_root, control_rows)                        # (c) must not raise

    with_probe_root = tmp_path / "with_probe"
    canonical_rows = _canonical_rows_and_measurements(with_probe_root)
    probe_row = _probe_row_and_measurement(with_probe_root)
    with_probe = _build(with_probe_root, canonical_rows + [probe_row])   # (c) must not raise

    clusters_with = {c["cluster_id"]: c["scan_refs"] for c in with_probe["clusters"]}
    clusters_control = {c["cluster_id"]: c["scan_refs"] for c in control["clusters"]}

    # (c) the probe's candidates were actually scored into the scene, not dropped.
    assert PROBE_REF in {ref for refs in clusters_with.values() for ref in refs}

    # (a) the context arena occupies its OWN partition -- a singleton cluster of its own.
    assert clusters_with[PROBE_REF] == [PROBE_REF]

    # (b) the canonical partition's membership is IDENTICAL to the control build made without the
    # probe arena at all: same three refs, coupled into the same one component either way.
    assert clusters_with[CANONICAL_REFS[0]] == CANONICAL_REFS
    assert clusters_control[CANONICAL_REFS[0]] == CANONICAL_REFS

    footprints_with = {f["scan_ref"]: f for f in with_probe["footprints"]}
    probe_footprint = footprints_with[PROBE_REF]
    canonical_footprint = footprints_with[CANONICAL_REFS[0]]

    # (a) ...with the None path component: never assigned a path_index, unlike the canonical arenas.
    assert probe_footprint["path_index"] is None
    assert canonical_footprint["path_index"] is not None

    # (b), continued: the probe never joins the canonical partition, nor mints a second one.
    assert probe_footprint["partition_id"] != canonical_footprint["partition_id"]
