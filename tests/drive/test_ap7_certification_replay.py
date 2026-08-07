"""AP7 replay: the final-certification worklist-index fix, measured against the frozen failure.

AP7 is the real failed campaign this fix exists to repair. It mapped the acoustic phonon
(2,0,0)→(3,0,0) in 21 steps, converged all 20 measurable q in-loop (path index 0 is the reduced-q
origin — an inferred, structurally-unresolvable endpoint with no pooled master by construction), and
then **hard-failed at end-of-campaign final certification** with beam budget still unspent:

    isar/session/driver.py  _run_master_certification
    ValueError: certification worklist canonical indices must be contiguous and unique

The manifest builder correctly excludes the origin, leaving worklist canonical indices ``[1..20]``
and ``excluded_points = [0]``. The consumer then demanded the worklist *alone* equal
``range(len(worklist))`` = ``[0..19]`` — so the legitimate exclusion made ``[1..20] != [0..19]`` and
it raised. This deterministically broke final certification for every acoustic campaign mapping from
a zone-center origin (the default acoustic path).

The compact fixture ``ap7_certification_worklist.json`` is the worklist/excluded canonical-index
layout extracted from the gitignored ``test_workspaces/AP7_Failed/`` manifest; the fast tests below
need only the committed fixture. A companion re-derives the same layout from the full frozen
workspace when it is present.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from isar.session.master_delivery import _assert_canonical_indices_cover_path

_FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "ap7_certification_worklist.json"
_MANIFEST_PATH = (Path(__file__).resolve().parents[2] / "test_workspaces" / "AP7_Failed" / "report"
                  / "certification"
                  / "4845f89773d2d1fc7bdf3770dc99910a84e8e979cbdc1eedd19d4f115da803ea"
                  / "manifest.json")


@pytest.fixture(scope="module")
def ap7() -> dict:
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _units(indices):
    return [{"canonical_index": i} for i in indices]


# -- the recorded failure: the excluded origin shifted the worklist off zero --------------------------

def test_fixture_is_ap7s_excluded_origin_layout(ap7):
    """Lock what AP7's certification manifest actually held, so the repair is measured against the real
    failure and not a softer one: 20 measured masters at canonical indices 1..20, the reduced-q origin
    excluded at index 0, over a 21-point path."""
    assert ap7["worklist_canonical_indices"] == list(range(1, 21))
    assert ap7["excluded_canonical_indices"] == [0]
    assert ap7["objective_path_len"] == 21


def test_the_old_worklist_only_invariant_would_still_reject_ap7(ap7):
    """A negative control that pins *why* AP7 failed. The old rule compared the worklist's canonical
    indices to ``range(len(worklist))``; AP7's ``[1..20]`` is not ``[0..19]``, so it raised. If this
    control ever stops holding, the fixture no longer reproduces the original defect."""
    worklist_indices = ap7["worklist_canonical_indices"]
    assert worklist_indices != list(range(len(worklist_indices)))


# -- the repair: worklist ∪ excluded covers the path, so certification proceeds -----------------------

def test_ap7_worklist_is_accepted_after_the_fix(ap7):
    """The repaired gate checks the UNION of worklist + excluded against ``range(total)``. AP7's layout
    accounts for every one of the 21 path points exactly once, so it must be accepted — final
    certification would no longer hard-fail on the index invariant."""
    _assert_canonical_indices_cover_path(
        _units(ap7["worklist_canonical_indices"]),
        _units(ap7["excluded_canonical_indices"]))  # no raise


def test_a_genuinely_dropped_point_is_still_rejected(ap7):
    """The exclusion tolerance must not weaken the real guarantee — every path point accounted for
    exactly once. Drop the origin from *both* the worklist and the excluded set and the union no longer
    covers the path, which is a silently missing point and must still raise."""
    with pytest.raises(ValueError, match="contiguous, unique"):
        _assert_canonical_indices_cover_path(
            _units(ap7["worklist_canonical_indices"]), _units([]))


# -- companion: tie the committed fixture to the real frozen artifact when it is present --------------

@pytest.mark.skipif(not _MANIFEST_PATH.exists(),
                    reason="gitignored test_workspaces/AP7_Failed/ frozen manifest not present")
def test_frozen_manifest_matches_the_fixture_and_passes_the_fixed_gate(ap7):
    manifest = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    worklist = manifest["worklist"]
    excluded = manifest["excluded_points"]
    assert [u["canonical_index"] for u in worklist] == ap7["worklist_canonical_indices"]
    assert [e["canonical_index"] for e in excluded] == ap7["excluded_canonical_indices"]
    # The real artifact that hard-failed AP7's final certification now passes the gate unchanged.
    _assert_canonical_indices_cover_path(worklist, excluded)
