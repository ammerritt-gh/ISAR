"""AP6 migrated replay: the repaired campaign, measured against the frozen failure (PR2.8).

:mod:`tests.drive.test_ap6_forensic_replay` locks *what AP6 did* — mapped the acoustic phonon
(2,0,0)→(3,0,0) and stopped ``incomplete — branch_ambiguity`` at path indices 18–20 with beam time
left, because the rival mode that would settle the identity at H=2.9 sits at ~7.115 meV, above the
declared 7.0 meV ceiling, and no scan the driver was willing to emit ever covered it. That module is
the baseline and must never change.

This module measures the repaired driver against that baseline. It reconstructs AP6's blocked frontier
at the ``enumerate_actions`` level and runs the grown registry (which now holds ``narrow_rival_probe``)
over it. The result turns entirely on **who chose the 7.0 meV band**, which is the distinction PR2.5
built and PR2.7a consumes:

* **Strict historical replay** — AP6's real frozen objective carried no search-band provenance. An
  objective whose band might merely equal the shipped default is treated as a hard constraint (equality
  is not authorship), so the rival is *not* pursued and the terminal is unchanged. The one thing that
  changed is honesty: the campaign now names the action it was forbidden to take, where before it
  stopped silent.
* **Migrated replay** — the same campaign with the band's provenance recorded as compiler-generated.
  Now the identity domain may widen to admit the rival, the probe enumerates as an eligible action, and
  the frontier that used to be a dead end has a justified next measurement.

**Acceptance is not "AP6 reaches ``met``".** H=2.9–3.0 is a zone boundary (this sample has zone centres
at every even H): the acoustic branch is flat at its maximum and closest to the optic branch there, so
the point may be genuinely underdetermined. The bar is that the driver stops failing for the *wrong*
reason — a missing action class — not that it manufactures certainty. The four checks below are the
plan's acceptance criteria, none of them a posterior tuned through the 0.85 gate.
"""
from __future__ import annotations

import pytest

from isar.drive.acquisition import acquire as acq
from isar.drive.acquisition import model as acq_model
from isar.drive.acquisition.inventory import GENERATOR_REGISTRY
from isar.drive.objective import ObjectiveSpec, shipped_hypothesis_snapshot
from isar.drive.supervisor.stopping import model_points, summarize_grid
from isar.schema.steering import AUXILIARY_TRACK_NODE

# AP6's real path: (2,0,0) → (3,0,0) in 21 steps of 0.05 rlu. Index 18 = H=2.90, the blocked frontier.
PATH = [(2.0 + 0.05 * i, 0.0, 0.0) for i in range(21)]
BLOCKED_INDEX = 18
BLOCKED_HKL = [2.9, 0.0, 0.0]
RIVAL_MEV = 7.115           # the acoustic-vs-optic discriminator, above the 7.0 meV ceiling
TARGET_MEV = 5.754          # the ambiguous target mode at the frontier


def _objective(**kw):
    base = dict(path=PATH, tolerance_meV=0.5, budget={"kind": "neutrons", "amount": 1e12},
                target_branch="acoustic")
    base.update(kw)
    return ObjectiveSpec(**base, **shipped_hypothesis_snapshot("acoustic", (-2.0, 7.0)))


def _frontier_observations():
    """An observation stream that reproduces index 18's terminal state: identity ambiguous, and three
    comparable revisions at 4x the exposure that moved neither the target probability nor the identity
    entropy — the ``identity_saturated`` brake AP6 recorded, which is what makes the rival probe (a
    different *kind* of measurement) the only remaining action rather than more counting."""
    obs = [{"kind": "pooled_master_update", "hkl": BLOCKED_HKL, "effective_exposure": exposure,
            "target_probability": 0.7069, "identity_entropy": 0.5972, "omega0": TARGET_MEV,
            "resolved": True, "disposition": "resolved"}
           for exposure in (1.0e7, 2.0e7, 4.0e7)]
    obs += [{"hkl": list(point), "kind": "production", "calibrated_halfwidth": 0.3,
             "resolved": True, "disposition": "resolved", "omega0": 5.7} for point in PATH]
    return obs


def _inventory(objective):
    observations = _frontier_observations()
    model = acq_model.fit(model_points(summarize_grid(objective, observations)))
    return acq.enumerate_actions(objective, model, observations, resolution_info={"fwhm_dE": 0.8},
                                 rival_band=lambda g: (RIVAL_MEV, 0.2))


def _rival_evaluations(inventory):
    return [e for e in inventory.evaluations if e.intent.action_class == "identity_rival_probe"]


# -- the reconstruction reproduces the frozen frontier -----------------------------------------------

def test_the_reconstruction_reproduces_ap6s_saturated_frontier():
    """Before measuring the repair, prove the reconstruction is AP6's failure and not a softer one: the
    frontier point must be identity-ambiguous and saturated, or the rival probe would fire for a reason
    the real campaign never had."""
    grid = summarize_grid(_objective(target_search_source="compiler"), _frontier_observations())
    point = grid[BLOCKED_INDEX]

    assert tuple(round(v, 4) for v in point.hkl) == (2.9, 0.0, 0.0)
    assert point.identity_saturated is True
    assert point.local_identity_clear is not True


def test_the_saturation_is_exposure_driven_not_an_accidental_default():
    """A negative control, so the replay cannot pass for a reason AP6 never had. Saturation must come
    from the exposure-growth rule — exposure rose while the identity metrics stalled — not from a
    default flag. Collapse the exposure growth (same metrics, flat exposure) and the same reconstruction
    must report the frontier as *not* saturated, which is what would restore ordinary same-q work."""
    flat = _frontier_observations()
    for update in flat:
        if update.get("kind") == "pooled_master_update":
            update["effective_exposure"] = 1.0e7        # same rows, no growth
    grid = summarize_grid(_objective(target_search_source="compiler"), flat)

    assert grid[BLOCKED_INDEX].identity_saturated is False


# -- strict historical replay: the terminal is unchanged, but now it explains itself -----------------

def test_strict_historical_replay_still_withholds_the_rival():
    """AP6's real objective carried no provenance. Equality with the shipped 7.0 meV default is not
    evidence anyone chose it as a hard bound *and* not evidence anyone chose it as a soft default, so
    the safe reading is to treat it as a constraint and not scan past it silently."""
    inventory, _cands = _inventory(_objective())  # no *_source: the legacy, un-migrated objective

    assert _rival_evaluations(inventory) == [], "a legacy band must not be widened on equality alone"


def test_strict_historical_replay_now_names_the_action_it_was_forbidden_to_take():
    """The one thing the repair changes for the un-migrated campaign. AP6 stopped ambiguous and silent;
    now the inventory records that an identity probe would have settled the frontier and the declared
    band forbade it — the fact an operator needs to decide whether to widen it."""
    _inv, _cands = _inventory(_objective())
    inventory = _inv
    report = next(r for r in inventory.reports if r.generator == "narrow_rival_probe")

    assert report.reason_code.startswith("policy_forbidden:identity_domain")
    assert f"{RIVAL_MEV}" in report.reason_code, "name the rival that was not pursued"


def test_an_operator_declared_band_is_also_a_hard_constraint():
    """Migration is not a licence to scan anywhere: a band the operator actually declared stays a hard
    limit, and the rival is withheld and reported exactly as in the legacy case."""
    inventory, _cands = _inventory(_objective(target_search_source="operator"))
    assert _rival_evaluations(inventory) == []


# -- migrated replay: the frontier is no longer a dead end -------------------------------------------

def test_migrated_replay_enumerates_the_rival_probe():
    """The repair, on the migrated objective: the band is recorded as compiler-generated, so the
    identity domain may widen to admit the rival and the probe becomes a real, eligible action."""
    inventory, candidates = _inventory(_objective(target_search_source="compiler"))
    rivals = _rival_evaluations(inventory)

    assert len(rivals) == 1
    assert rivals[0].is_eligible
    assert any(c.evaluation.intent.action_class == "identity_rival_probe" for c in candidates)


def test_migrated_replay_the_probe_reaches_the_rival_and_is_non_canonical():
    inventory, _cands = _inventory(_objective(target_search_source="compiler"))
    intent = _rival_evaluations(inventory)[0].intent

    assert intent.energy_lo < RIVAL_MEV < intent.energy_hi
    assert intent.energy_hi > 7.0, "the emitted window must cross the ceiling AP6 was clipped at"
    assert intent.evidence_sink == AUXILIARY_TRACK_NODE


# -- the four acceptance criteria (none of them "reach met") -----------------------------------------

def test_a_it_does_not_stop_merely_for_a_missing_on_grid_neighbour():
    """(a) The endpoint has no unvisited neighbour to broaden to — the original dead end. The action
    space at the frontier is now non-empty for a reason unrelated to grid geometry."""
    inventory, candidates = _inventory(_objective(target_search_source="compiler"))
    assert candidates, "the grown registry offers an action where the endpoint geometry offered none"
    assert inventory.has_eligible_action


def test_b_it_tries_the_justified_rival_action():
    """(b) The justified action is the predicted-rival probe, and it is eligible for selection rather
    than merely enumerated-and-rejected."""
    inventory, _cands = _inventory(_objective(target_search_source="compiler"))
    assert any(e.is_eligible for e in _rival_evaluations(inventory))


def test_c_a_refused_action_becomes_a_durable_classed_suppression():
    """(c) A rejected action must change the inventory through a durable, correctly classed suppression
    keyed on its scientific fingerprint — not vanish, and not be silently retried. Fed back as a
    suppression, the same probe is no longer offered."""
    objective = _objective(target_search_source="compiler")
    inventory, _cands = _inventory(objective)
    fingerprint = _rival_evaluations(inventory)[0].intent.scientific_action_fingerprint

    observations = _frontier_observations()
    model = acq_model.fit(model_points(summarize_grid(objective, observations)))
    _inv, candidates = acq.enumerate_actions(
        objective, model, observations, resolution_info={"fwhm_dE": 0.8},
        rival_band=lambda g: (RIVAL_MEV, 0.2),
        suppressed={fingerprint: {"class": "structural", "reason": "validated infeasible"}})

    assert all(c.evaluation.intent.scientific_action_fingerprint != fingerprint for c in candidates)


def test_d_the_completeness_proof_covers_the_grown_registry():
    """(d) The proof a terminal rests on must account for *every* generator now registered. A proof
    naming only the legacy adapter would claim a completeness it no longer has — an exhaustion terminal
    that never consulted the rival probe would be exactly the AP6 failure wearing an honest label. The
    inventory is complete and its reports cover the grown registry whether or not the rival is eligible,
    so suppress it and confirm the proof still names it."""
    objective = _objective(target_search_source="compiler")
    inventory, _cands = _inventory(objective)
    fingerprint = _rival_evaluations(inventory)[0].intent.scientific_action_fingerprint

    observations = _frontier_observations()
    model = acq_model.fit(model_points(summarize_grid(objective, observations)))
    exhausted, candidates = acq.enumerate_actions(
        objective, model, observations, resolution_info={"fwhm_dE": 0.8},
        rival_band=lambda g: (RIVAL_MEV, 0.2),
        suppressed={fingerprint: {"class": "structural", "reason": "validated infeasible"}})

    assert exhausted.is_complete, "a completeness proof requires every generator to have run cleanly"
    reported = {r.generator for r in exhausted.reports}
    assert reported == {spec.generator_id for spec in GENERATOR_REGISTRY}
    assert "narrow_rival_probe" in reported, "the proof must cover the grown registry, not the old one"
    # The suppressed rival is gone; any remaining candidate is ordinary work elsewhere on the path, not
    # the refused probe re-offered.
    assert all(c.evaluation.intent.scientific_action_fingerprint != fingerprint for c in candidates)
