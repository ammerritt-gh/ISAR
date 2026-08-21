"""The off-path context-scan SEAM, end to end (SCENE-4 PR 4.4, D5).

This is a SEAM-level test, not a world-level end-to-end claim: it pins that the per-delivery
provenance seam (``program_runner.proposal_from_intent``) accepts a ``context_probe`` role for one
delivery whose submitted coordinate is genuinely off the canonical path, that the partition keys
computed from each submission's OWN logged provenance separate cleanly (the off-path arena never
collapses into the canonical path's partition, and never mints a second canonical-path partition of
its own), and that the campaign continues to an honest verdict rather than treating the off-path
scan as a stop reason. It does NOT claim anything about a real off-path *producer*, a scene build
over mixed-topology arenas, or a certified verdict — those are ``tests/scene/``'s job (see
``test_context_probe_partition.py`` for the real-scene-build half of this split) and SCENE-5's
(an honest region-boundary predicate that decides FOR ITSELF when to take a context probe).

The 4.3/4.4 driver has no organic producer for ``"context_probe"`` yet (owner ruling, this PR's
D5: "program_boundary producer deferred by name"). Every discovery request in a step today carries
the same static ``"canonical_path"`` provenance block, computed once per step
(``program_runner._campaign_provenance_block``) and fed, delivery by delivery, through
``proposal_from_intent``. This test injects the off-path delivery by wrapping that exact
per-delivery seam for one call only — overriding both the provenance block AND the submitted
coordinate itself (the canonical path is (2,0,0)->(3,0,0); the injected delivery moves K to 0.1,
off that segment) — so the rest of the campaign — planning, scheduling, submission, logging — runs
completely unmodified through ``run_program``: the seam a real producer will plug into, once one
exists.
"""
from __future__ import annotations

import dataclasses

import isar.drive.program_runner as runner_mod
from isar.drive.program_runner import (
    VERDICT_COMPLETE, VERDICT_IN_PROGRESS, VERDICT_MACHINE_FAILURE, VERDICT_OVER_BUDGET,
    run_program)
from isar.schema.campaign_program import CampaignProgram
from isar.scene.model import partition_key
from isar.synth import generate, worlds

from drive.test_broken_scan_lane import config_for, moving_scenes
from drive.test_program_runner import FakeBuild, MockTavi, discover_step, log_payloads, workspace

DELIVERIES = 3
OFF_PATH_DELIVERY = 2   # 1-indexed: the second of three deliveries takes the context probe
OFF_PATH_K = 0.1        # the canonical path holds K=L=0 fixed; this is off that segment


# ------------------------------------------------------------------ the world (physics half)
def test_context_probe_world_is_registered_and_generates():
    """Seeded, embeds its own config, generates like the other twelve (see also
    ``tests/scene/test_worlds_fast.py``, which parametrizes the generic suite invariants — build,
    round-trip, replay — over every registered world including this one)."""
    cfg, truth = worlds.build("context_probe", 1013)
    assert cfg.seed == 1013 and cfg.name == "context_probe"
    assert truth.config is cfg
    data = generate.generate_world(truth)
    assert data.counts.min() >= 0


# ------------------------------------------------------------------ the campaign (drive half)
def _inject_one_context_probe(monkeypatch):
    """Stamp exactly ONE delivery's request as an off-path context probe, through the seam a real
    producer will feed (see module docstring): both its provenance role AND its submitted
    coordinate move off the canonical path, so the injected delivery is genuinely off-path rather
    than only labeled as such. Every other delivery is untouched."""
    real = runner_mod.proposal_from_intent
    calls = {"n": 0}

    def patched(intent, *, provenance, **kwargs):
        calls["n"] += 1
        if calls["n"] == OFF_PATH_DELIVERY:
            provenance = {**provenance, "topology_role": "context_probe",
                          "path_fingerprint": None}
            intent = dataclasses.replace(
                intent, hkl=(intent.hkl[0], OFF_PATH_K, intent.hkl[2]))
        return real(intent, provenance=provenance, **kwargs)

    monkeypatch.setattr(runner_mod, "proposal_from_intent", patched)
    return calls


def test_the_context_probe_seam_accepts_an_off_path_delivery_partitions_it_separately_and_the_campaign_continues(
        tmp_path, monkeypatch):
    root = workspace(tmp_path)
    client = MockTavi()
    step = discover_step()
    monkeypatch.setattr(runner_mod, "build_scene_revision",
                        FakeBuild(root, moving_scenes(DELIVERIES + 2)))
    calls = _inject_one_context_probe(monkeypatch)
    program = CampaignProgram(steps=(step,))
    outcome = run_program(program, step.id, config_for(
        root, trust="trusted", client=client, max_deliveries=DELIVERIES))

    # the campaign continues to an honest verdict — the off-path scan is never a stop reason.
    assert outcome.verdict in (VERDICT_COMPLETE, VERDICT_IN_PROGRESS, VERDICT_OVER_BUDGET)
    assert outcome.verdict != VERDICT_MACHINE_FAILURE
    assert outcome.result.deliveries == DELIVERIES
    assert calls["n"] == DELIVERIES

    submissions = [p for p in log_payloads(root, "run") if p.get("job_id")]
    assert len(submissions) == DELIVERIES
    roles = [p["topology_role"] for p in submissions]

    # the off-path arena is recorded, under the real vocabulary, and only the one delivery.
    assert roles[OFF_PATH_DELIVERY - 1] == "context_probe"
    assert roles.count("context_probe") == 1
    other_roles = [r for i, r in enumerate(roles) if i != OFF_PATH_DELIVERY - 1]
    assert other_roles == ["canonical_path"] * (DELIVERIES - 1)

    off_path = submissions[OFF_PATH_DELIVERY - 1]
    assert off_path.get("path_fingerprint") is None                # a context probe names no path

    on_path = [p for p in submissions if p["topology_role"] == "canonical_path"]
    path_fps = {p["path_fingerprint"] for p in on_path}
    assert len(path_fps) == 1 and next(iter(path_fps))            # the declared path never moved

    # the probe's submitted coordinate is genuinely off the canonical (2,0,0)->(3,0,0) segment
    # (K=L=0 there), while every canonical submission stays on it.
    assert off_path["hkl"][1] == OFF_PATH_K
    assert all(p["hkl"][1] == 0.0 for p in on_path)

    # the partition floor: the off-path arena's partition never collapses into the canonical
    # path's, and never mints a second canonical-path partition of its own. The keys are computed
    # from each submission's OWN logged provenance, holding every non-path argument identical, so
    # a partition_key regression that ignored topology_role/path_fingerprint fails here.
    def key_for(payload):
        return partition_key("s", (), 1, str(payload["job_id"]), None,
                             payload["topology_role"], payload.get("path_fingerprint"))

    on_path_keys = {key_for(p) for p in on_path}
    probe_key = key_for(off_path)
    assert len(on_path_keys) == 1                                  # one canonical partition, no fork
    canonical_key = next(iter(on_path_keys))
    assert probe_key != canonical_key                              # no collapse into the path
    assert canonical_key[-1] == next(iter(path_fps))
    assert probe_key[-1] is None
