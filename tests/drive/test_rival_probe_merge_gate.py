"""The merge gate for the rival probe (PR2.7a): can the pipeline actually consume what it produces?

A generator whose output nothing downstream reads spends beam time for nothing, so the gate for
registering one is not "it emits a plausible intent" — it is that the whole chain carries the action's
meaning end to end. This module walks that chain on one campaign in AP6's failing state:

    plan_actions → the emitted ScanProposal → acquisition provenance in card.state.json →
    collation (excluded from the pool) → _tracked_authorities (merged as a tagged candidate)

The regression that motivates the first two tests was live in this repo and invisible to unit tests of
the generator. ``_build_proposal`` re-derived every window from the *target* search band, so the probe's
declared window on the predicted rival was discarded and the emitted scan covered (-2, 7) meV — the
exact band AP6 was already scanning, and the exact reason it never saw 7.115 meV. The intent was right
and the scan was wrong, which is why the gate has to assert on the **emitted proposal** rather than on
what the generator declared.
"""
from __future__ import annotations

import pytest

from isar.drive.acquisition import acquire as acq
from isar.drive.acquisition import model as acq_model
from isar.drive.objective import ObjectiveSpec, shipped_hypothesis_snapshot
from isar.drive.supervisor.stopping import model_points, summarize_grid
from isar.schema.steering import AUXILIARY_TRACK_NODE, CANONICAL_MASTER

RIVAL_MEV = 7.115
PATH = [(2.0, 0.0, 0.0), (2.05, 0.0, 0.0), (2.9, 0.0, 0.0)]
BLOCKED_HKL = [2.9, 0.0, 0.0]


def _objective(**kw):
    base = dict(path=PATH, tolerance_meV=0.5, budget={"kind": "neutrons", "amount": 1e12},
                target_branch="acoustic", target_search_source="compiler")
    base.update(kw)
    return ObjectiveSpec(**base, **shipped_hypothesis_snapshot("acoustic", (-2.0, 7.0)))


def _observations():
    """AP6's index-18 state: identity ambiguous, and three comparable revisions at 4x the exposure
    that moved neither the target probability nor the identity entropy."""
    obs = [{"kind": "pooled_master_update", "hkl": BLOCKED_HKL, "effective_exposure": exposure,
            "target_probability": 0.7069, "identity_entropy": 0.5972, "omega0": 5.754,
            "resolved": True, "disposition": "resolved"}
           for exposure in (1.0e7, 2.0e7, 4.0e7)]
    obs += [{"hkl": list(point), "kind": "production", "calibrated_halfwidth": 0.3,
             "resolved": True, "disposition": "resolved", "omega0": 5.7} for point in PATH]
    return obs


def _proposals(objective=None):
    objective = objective or _objective()
    observations = _observations()
    model = acq_model.fit(model_points(summarize_grid(objective, observations)))
    plan = acq.plan_actions(objective, model, observations, resolution_info={"fwhm_dE": 0.8},
                            budget_state={}, rival_band=lambda g: (RIVAL_MEV, 0.2))
    out = []
    while (proposal := plan.next_proposal()) is not None:
        out.append(proposal)
    return out


# -- the emitted scan, not the declared intent -------------------------------------------------------

def test_the_blocked_campaign_now_offers_an_identity_probe_beside_ordinary_work():
    proposals = _proposals()
    kinds = [p.kind for p in proposals]

    assert "identity_rival_probe" in kinds
    # Ranked last: the probe is what to try when the cheap path runs out, never a competitor for beam
    # while ordinary work remains.
    assert kinds[-1] == "identity_rival_probe"


def test_the_emitted_probe_window_actually_reaches_the_rival():
    """The regression guard. A probe whose emitted window is the ordinary target band is not a
    discriminator — it is the scan AP6 already took, relabelled."""
    probe = next(p for p in _proposals() if p.kind == "identity_rival_probe")

    assert probe.e_lo < RIVAL_MEV < probe.e_hi
    assert probe.e_hi > 7.0, "the whole point is reaching above the target search ceiling"
    # Pin the actual bounds, not just overlap. A future window bug that still happened to straddle
    # 7.115 would pass a mere-overlap check while emitting the wrong scan; the declared half-width is
    # max(2*sigma, 1.5*fwhm) = max(0.4, 0.75) = 0.75 around the rival.
    assert probe.e_lo == pytest.approx(RIVAL_MEV - 0.75, abs=1e-6)
    assert probe.e_hi == pytest.approx(RIVAL_MEV + 0.75, abs=1e-6)
    # The energy the scan is *about*, which becomes the persisted acquisition_target_energy and the
    # centre the effective-exposure weighting uses. Left at the target model's prediction it was 0.0,
    # which would score the probe's counts over a region it deliberately did not scan.
    assert probe.e_pred == pytest.approx(RIVAL_MEV, abs=1e-6)


def test_the_probe_is_cheaper_than_the_target_scan_it_supplements():
    """It answers a yes/no question about one predicted mode, so it must not cost a survey."""
    proposals = _proposals()
    probe = next(p for p in proposals if p.kind == "identity_rival_probe")
    ordinary = next(p for p in proposals if p.kind != "identity_rival_probe")

    assert (probe.e_hi - probe.e_lo) < (ordinary.e_hi - ordinary.e_lo)
    assert probe.n_points < ordinary.n_points


def test_the_probe_carries_its_sink_and_is_not_certification_eligible():
    probe = next(p for p in _proposals() if p.kind == "identity_rival_probe")
    meta = probe.meta or {}

    assert meta.get("evidence_sink") == AUXILIARY_TRACK_NODE
    assert meta.get("certification_eligible") is False
    ordinary = next(p for p in _proposals() if p.kind != "identity_rival_probe")
    assert (ordinary.meta or {}).get("evidence_sink") == CANONICAL_MASTER


def test_an_operator_declared_band_yields_ordinary_work_and_no_probe():
    """The refusal path, end to end: the campaign keeps working, and simply never emits the scan it
    was not authorised to take."""
    kinds = [p.kind for p in _proposals(_objective(target_search_source="operator"))]
    assert "identity_rival_probe" not in kinds


# -- the chain past acquisition ----------------------------------------------------------------------

def test_the_sink_survives_persistence_into_card_state(tmp_path):
    """The one channel by which analysis learns this scan was not ordinary evidence. If it is dropped
    here, reduction pools the probe into the master and every guarantee above is void."""
    import shutil
    from pathlib import Path

    from isar.drive.loop import _persist_acquisition_provenance
    from isar.schema.card import Card

    probe = next(p for p in _proposals() if p.kind == "identity_rival_probe")
    # A shipped template rather than a hand-rolled stub: the persistence path loads the real policy
    # schema, so a minimal fake would test the test rather than the code.
    policy = tmp_path / "card.policy.toml"
    shutil.copy(Path(__file__).resolve().parents[2] / "isar" / "parsers" / "cards"
                / "tavi.policy.toml", policy)

    # The loop assigns acquisition_id at submission time; the proposal builder does not.
    _persist_acquisition_provenance(tmp_path, {
        "job_id": "j1", "acquisition_id": "acq-1",
        "acquisition_target_energy": float(probe.e_pred), "submitted_ncount": int(probe.ncount),
        "kind": probe.kind, "evidence_sink": (probe.meta or {}).get("evidence_sink")})

    card = Card.load(policy, tmp_path / "card.state.json")
    entry = card.state.acquisition_provenance[-1]
    assert entry["evidence_sink"] == AUXILIARY_TRACK_NODE


def test_a_persisted_probe_resolves_back_to_its_sink_for_reduction_and_the_session():
    """Both consumers read the sink through one resolver, so this is the hop that decides whether the
    probe is pooled and whether the tracker may merge it."""
    from isar.schema.steering import evidence_sink_for

    provenance = [{"job_id": "j1", "evidence_sink": AUXILIARY_TRACK_NODE}]
    assert evidence_sink_for({"tavi": {"job_id": "j1"}}, provenance) == AUXILIARY_TRACK_NODE


def test_the_probe_changes_the_next_inventory():
    """The final link: evidence that cannot change what is proposed next is evidence nothing reads.
    The probe's scientific fingerprint is tied to the evidence basis, so once its result lands the
    campaign is no longer asking the identical question."""
    objective = _objective()
    observations = _observations()
    model = acq_model.fit(model_points(summarize_grid(objective, observations)))
    before, _ = acq.enumerate_actions(objective, model, observations,
                                      resolution_info={"fwhm_dE": 0.8},
                                      rival_band=lambda g: (RIVAL_MEV, 0.2))
    probe_intent = next(e.intent for e in before.evaluations
                        if e.intent.action_class == "identity_rival_probe")

    # The probe landed and revised the authoritative evidence at that q.
    revised = [dict(o) for o in observations]
    revised.append({"kind": "pooled_master_update", "hkl": BLOCKED_HKL,
                    "effective_exposure": 8.0e7, "target_probability": 0.94,
                    "identity_entropy": 0.11, "omega0": 5.754, "resolved": True,
                    "disposition": "resolved", "content_hash": "after-probe"})
    after, _ = acq.enumerate_actions(objective, acq_model.fit(model_points(
        summarize_grid(objective, revised))), revised, resolution_info={"fwhm_dE": 0.8},
        rival_band=lambda g: (RIVAL_MEV, 0.2))

    repeated = [e for e in after.evaluations
                if e.intent.scientific_action_fingerprint
                == probe_intent.scientific_action_fingerprint]
    assert repeated == [], "the identical probe must not be re-proposed against changed evidence"


# -- anti-brute-force --------------------------------------------------------------------------------

def test_the_probe_fingerprint_is_stable_so_a_suppression_can_target_it():
    """Durable suppression is only possible if the same question keeps the same name. If the probe's
    fingerprint drifted between enumerations, a recorded refusal would never match it again and the
    driver could re-fire it indefinitely at the same q for the same reason."""
    objective, observations = _objective(), _observations()
    model = acq_model.fit(model_points(summarize_grid(objective, observations)))
    fingerprints = set()
    for _ in range(3):
        inventory, _c = acq.enumerate_actions(objective, model, observations,
                                              resolution_info={"fwhm_dE": 0.8},
                                              rival_band=lambda g: (RIVAL_MEV, 0.2))
        fingerprints.add(next(e.intent.scientific_action_fingerprint for e in inventory.evaluations
                              if e.intent.action_class == "identity_rival_probe"))
    assert len(fingerprints) == 1


def test_a_suppressed_probe_is_not_re_proposed():
    """The bound itself: once an attempt is durably refused, the same action stops being offered while
    the evidence that defined it is unchanged."""
    objective, observations = _objective(), _observations()
    model = acq_model.fit(model_points(summarize_grid(objective, observations)))
    inventory, _c = acq.enumerate_actions(objective, model, observations,
                                          resolution_info={"fwhm_dE": 0.8},
                                          rival_band=lambda g: (RIVAL_MEV, 0.2))
    fingerprint = next(e.intent.scientific_action_fingerprint for e in inventory.evaluations
                       if e.intent.action_class == "identity_rival_probe")

    _inv, candidates = acq.enumerate_actions(
        objective, model, observations, resolution_info={"fwhm_dE": 0.8},
        rival_band=lambda g: (RIVAL_MEV, 0.2),
        suppressed={fingerprint: {"class": "structural", "reason": "already refused"}})

    assert all(c.evaluation.intent.action_class != "identity_rival_probe" for c in candidates)
