"""Contract for the generation/evaluation split on the live acquisition path (PR2.1).

PR1 declared a two-phase pipeline — generators produce :class:`MeasurementIntent`, evaluators score
them, selection ranks the survivors — but the live path stayed fused: one legacy function chose one
pick and built one pre-scored evaluation, so ``dedupe_intents``, ``policy_rank`` and the per-generator
accounting were structure without exercise. These tests pin the split itself, *before* PR2.7 registers
generators that make it load-bearing:

* every generator sees one shared :class:`AcquisitionContext` — a new generator cannot silently read
  a different grid, policy or hypothesis than the legacy adapter did;
* two generators asking the same scientific question collapse to one action, and the accounting says
  so rather than quietly reporting a count that no evaluation backs;
* one generator raising isolates to a ``FAILED`` report while its peers still contribute — the
  inventory becomes incomplete (``controller_fault``), never a false exhaustion proof;
* an evaluator that raises is the same kind of fault as a generator that raises: an action whose
  utility is unknown cannot be ranked, so it must not sit in the inventory looking eligible.
"""
from __future__ import annotations

import dataclasses
import math

import pytest

from isar.drive.acquisition import acquire as acq
from isar.drive.acquisition import model as acq_model
from isar.drive.acquisition.inventory import (
    ActionEligibility,
    GeneratorSpec,
    GeneratorStatus,
    LEGACY_SELECTOR_GENERATOR,
)
from isar.drive.objective import ObjectiveSpec


def _obj():
    return ObjectiveSpec.along_h(h_start=2.1, h_end=2.4, step=0.1, tolerance_meV=0.5,
                                 budget={"kind": "neutrons", "amount": 1e12})


def _obs(hkl, kind, hw, resolved=True, disp="resolved", omega0=3.0):
    return {"hkl": hkl, "kind": kind, "calibrated_halfwidth": hw, "resolved": resolved,
            "disposition": disp, "omega0": omega0 if resolved else None}


def _master(hkl, hw, disp="resolved", reliable=True, n=2):
    return {"hkl": hkl, "omega0": 3.0, "halfwidth": hw, "reliable": reliable,
            "usable": disp in ("resolved", "flagged"), "disposition": disp, "n_members": n}


def _register(monkeypatch, generators: dict, evaluators: dict | None = None):
    """Register a synthetic generator set for one test, registry and dispatch kept in step.

    The registry (declarative) and the dispatch table (callables) are validated against each other at
    import, so a test that patches one must patch both — otherwise it proves nothing about the
    production path.
    """
    specs = tuple(GeneratorSpec(name, version=1) for name in generators)
    monkeypatch.setattr(acq, "GENERATOR_REGISTRY", specs)
    monkeypatch.setattr(acq, "ACTION_GENERATORS", dict(generators))
    monkeypatch.setattr(acq, "ACTION_EVALUATORS",
                        dict(evaluators if evaluators is not None
                             else {name: acq._legacy_selector_evaluate for name in generators}))


# -- one shared context ----------------------------------------------------------------------------

def test_every_generator_receives_the_same_context_object(monkeypatch):
    seen = []

    def probe(context):
        seen.append(context)
        return []

    _register(monkeypatch, {LEGACY_SELECTOR_GENERATOR: acq._legacy_selector_generate,
                            "probe_a": probe, "probe_b": probe})
    inventory, _candidates = acq.enumerate_actions(
        _obj(), acq_model.fit([]), [], resolution_info={"fwhm_dE": 0.8})

    assert len(seen) == 2 and seen[0] is seen[1]          # one construction site, not two worlds
    context = seen[0]
    assert context.objective is not None and context.grid
    assert context.policy is not None and context.runtime_limits is not None
    assert inventory.is_complete is True


def test_the_shared_context_sequences_cannot_be_mutated_by_one_generator():
    """``frozen=True`` only stops field *reassignment*. If the grid were a list, one generator could
    append to or reorder it and every generator after it would be reasoning about a different world —
    exactly the divergence the shared context exists to prevent, and invisible in the inventory."""
    context = acq.build_acquisition_context(
        _obj(), acq_model.fit([]), [], resolution_info={"fwhm_dE": 0.8})
    assert isinstance(context.grid, tuple)
    assert isinstance(context.results_so_far, tuple)
    with pytest.raises(AttributeError):
        context.grid.append(context.grid[0])
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.grid = ()


def test_the_context_carries_the_normalized_grid_the_legacy_adapter_selects_from(monkeypatch):
    """The driver-inserted candidate is appended (and its synthetic index normalized) while the context
    is built, so a PR2.7 generator sees the same working grid the legacy selector does — including the
    inserted probe at its real list position."""
    from isar.drive.supervisor.stopping import model_points, summarize_grid
    from isar.drive.objective import shipped_hypothesis_snapshot

    path = [(2.0, 0.0, 0.0), (2.1, 0.0, 0.0), (2.2, 0.0, 0.0)]
    obj = ObjectiveSpec(path=path, tolerance_meV=0.5, budget={"kind": "neutrons", "amount": 1e12},
                        target_branch="acoustic",
                        **shipped_hypothesis_snapshot("acoustic", (-2.0, 7.0)))
    obs = [_obs(p, "quicklook", 0.9) for p in path]
    model = acq_model.fit(model_points(summarize_grid(obj, obs)))
    classify = lambda g: 9.0 if math.dist(tuple(g.hkl), (2.15, 0.0, 0.0)) <= 1e-6 else 0.0

    seen = []
    _register(monkeypatch, {LEGACY_SELECTOR_GENERATOR: acq._legacy_selector_generate,
                            "probe": lambda context: seen.append(context) or []})
    acq.enumerate_actions(obj, model, obs, resolution_info={"fwhm_dE": 0.8}, classify=classify)

    grid = seen[0].grid
    assert len(grid) == len(path) + 1                      # the inserted probe is present
    assert grid[-1].index == len(grid) - 1                 # at its real position, not a synthetic one
    assert [point.index for point in grid] == list(range(len(grid)))


# -- dedupe ----------------------------------------------------------------------------------------

def test_two_generators_asking_the_same_question_collapse_to_one_action(monkeypatch):
    """A duplicate is not a second option — it is one scientific question that would be paid for twice.
    First-seen wins, and the accounting records the collapse instead of claiming an action no
    evaluation backs (which ``structural_problems`` would reject as an inconsistent proof)."""
    obj, model = _obj(), acq_model.fit([])

    def echo(context):
        return acq._legacy_selector_generate(context)

    _register(monkeypatch, {LEGACY_SELECTOR_GENERATOR: acq._legacy_selector_generate,
                            "echo": echo},
              evaluators={LEGACY_SELECTOR_GENERATOR: acq._legacy_selector_evaluate,
                          "echo": acq._legacy_selector_evaluate})
    inventory, candidates = acq.enumerate_actions(obj, model, [], resolution_info={"fwhm_dE": 0.8})

    assert len(candidates) == 1
    assert candidates[0].evaluation.generator == LEGACY_SELECTOR_GENERATOR   # first-seen wins
    reports = {r.generator: r for r in inventory.reports}
    assert reports[LEGACY_SELECTOR_GENERATOR].generated == 1
    assert reports["echo"].generated == 0 and reports["echo"].deduped == 1
    # The collapse must leave the inventory a *valid* proof, not a miscounted one.
    assert inventory.structural_problems() == []
    assert inventory.is_complete is True


def test_dedupe_keeps_genuinely_different_scientific_questions(monkeypatch):
    """Two actions at the same q are still two actions when they resolve different uncertainties."""
    obj, model = _obj(), acq_model.fit([])

    def rival(context):
        base = acq._legacy_selector_generate(context).actions
        if not base:
            return acq.GeneratorResult()
        original = base[0]
        intent = acq.MeasurementIntent(
            action_class="narrow_rival", hkl=original.intent.hkl, energy_lo=6.5, energy_hi=7.6,
            window_family="identity", target_branch_role=original.intent.target_branch_role,
            rival_branch_role=original.intent.rival_branch_role,
            evidence_basis_fingerprint=original.intent.evidence_basis_fingerprint,
            hypotheses_fingerprint=original.intent.hypotheses_fingerprint)
        return acq.GeneratorResult([acq.GeneratedAction(
            intent=intent, generator="rival", pick=original.pick, kind=original.kind,
            reason="narrow rival probe", score=original.score)])

    _register(monkeypatch, {LEGACY_SELECTOR_GENERATOR: acq._legacy_selector_generate,
                            "rival": rival})
    inventory, candidates = acq.enumerate_actions(obj, model, [], resolution_info={"fwhm_dE": 0.8})

    assert len(candidates) == 2
    assert {c.evaluation.intent.action_class for c in candidates} >= {"narrow_rival"}
    assert inventory.structural_problems() == []
    assert inventory.has_eligible_action is True


# -- failure isolation -----------------------------------------------------------------------------

def test_one_generator_raising_does_not_discard_its_peers_actions(monkeypatch):
    """Isolation must be per generator. A peer's actions stay enumerated *and* the inventory is still
    incomplete — a fault means the action space is partly unknown, so exhaustion is unprovable even
    though eligible actions exist."""
    def boom(_context):
        raise RuntimeError("rival generator exploded")

    _register(monkeypatch, {LEGACY_SELECTOR_GENERATOR: acq._legacy_selector_generate,
                            "boom": boom})
    inventory, candidates = acq.enumerate_actions(
        _obj(), acq_model.fit([]), [], resolution_info={"fwhm_dE": 0.8})

    assert len(candidates) == 1                       # the healthy generator still contributed
    failed = inventory.failed_reports()
    assert [r.generator for r in failed] == ["boom"]
    assert "RuntimeError" in failed[0].error_code and "exploded" in failed[0].error_code
    assert inventory.is_complete is False
    assert inventory.terminal_cause() == "controller_fault"


def test_an_evaluator_raising_is_a_fault_not_a_silently_eligible_action(monkeypatch):
    """An action whose utility could not be computed cannot be ranked against its rivals. Leaving it in
    the inventory would let selection pick an unscored action; dropping it silently would let the
    remaining set masquerade as the complete space. It must be a FAILED generator."""
    def broken(_action, _context):
        raise ValueError("scoring provider unavailable")

    _register(monkeypatch, {LEGACY_SELECTOR_GENERATOR: acq._legacy_selector_generate},
              evaluators={LEGACY_SELECTOR_GENERATOR: broken})
    inventory, candidates = acq.enumerate_actions(
        _obj(), acq_model.fit([]), [], resolution_info={"fwhm_dE": 0.8})

    assert candidates == []
    assert inventory.evaluations == []
    failed = inventory.failed_reports()
    assert [r.generator for r in failed] == [LEGACY_SELECTOR_GENERATOR]
    assert "ValueError" in failed[0].error_code
    assert inventory.is_complete is False
    assert inventory.terminal_cause() == "controller_fault"


# -- the split preserves the legacy selection ------------------------------------------------------

def test_the_split_phases_reproduce_the_fused_selection():
    """PR2.1 is structural: with only the legacy adapter registered, generation → dedupe → evaluation →
    selection must choose the identical action the fused path chose."""
    obj, model = _obj(), acq_model.fit([])
    context = acq.build_acquisition_context(obj, model, [], resolution_info={"fwhm_dE": 0.8})
    generated, reports = acq.generate_action_intents(context)
    evaluations = acq.evaluate_action_intents(generated, context)

    # Every declared generator reports; the legacy adapter is the one that contributes here, because
    # this fixture has no identity-saturated point for the probe to act on. That is the property worth
    # pinning — an ordinary campaign must keep choosing exactly what it chose before PR2.7a grew the
    # registry — rather than the registry's arity, which this increment deliberately changed.
    legacy = next(r for r in reports if r.generator == LEGACY_SELECTOR_GENERATOR)
    assert legacy.status is GeneratorStatus.COMPLETED and legacy.generated == 1
    assert len(generated) == len(evaluations) == 1
    assert evaluations[0].eligibility is ActionEligibility.ELIGIBLE

    inventory, candidates = acq.enumerate_actions(obj, model, [], resolution_info={"fwhm_dE": 0.8})
    assert candidates[0].evaluation.intent.scientific_action_fingerprint == \
        generated[0].intent.scientific_action_fingerprint
    proposal = acq.propose_scan(obj, model, [], {"fwhm_dE": 0.8}, {})
    assert proposal is not None
    assert candidates[0].pick.hkl[0] == pytest.approx(proposal.h)
    assert inventory.validate() == []


def test_an_empty_action_space_still_reports_a_valid_exhaustion_proof():
    """The converged single-q case: nothing generated, but the accounting must still be complete."""
    obj = ObjectiveSpec.along_h(h_start=2.1, h_end=2.1, step=0.1, tolerance_meV=0.5,
                                budget={"kind": "neutrons", "amount": 1e12})
    obs = [_obs((2.1, 0.0, 0.0), "production", 0.30)]
    masters = [_master((2.1, 0.0, 0.0), 0.30)]
    inventory, candidates = acq.enumerate_actions(
        obj, acq_model.fit([]), obs, resolution_info={"fwhm_dE": 0.8}, master_rows=masters)

    assert candidates == []
    assert inventory.reports[0].reason_code == "no_selectable_action"
    assert inventory.reports[0].generated == 0 and inventory.reports[0].deduped == 0
    assert inventory.is_complete is True
    assert inventory.terminal_cause() == "action_inventory_exhausted"


# -- registry / dispatch coherence -----------------------------------------------------------------

def test_the_dispatch_tables_cover_exactly_the_declared_registry():
    """A registered generator with no callable would silently report nothing and make an exhaustion
    proof over an action class that never ran; a callable with no registry entry would never run at
    all. Both are import-time errors, not runtime surprises."""
    declared = {spec.generator_id for spec in acq.GENERATOR_REGISTRY}
    assert set(acq.ACTION_GENERATORS) == declared
    assert set(acq.ACTION_EVALUATORS) == declared
