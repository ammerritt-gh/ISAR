"""The cold-start declared partition (SCENE-4 PR 4.3, D4).

A campaign that has measured nothing has no observed partition, so nothing in the workspace can
tell the question layer WHICH physical partition the first scan will land in — and without one
the agenda fans out to the program-scoped lane, every generator answers
``program_scoped_subject_unfanned``, and declared knowledge (zone geometry, lattice frame) goes
inert at exactly the moment it is the only knowledge there is.

This module mints that partition from the DECLARATIONS instead, and does it through the SAME
:func:`isar.scene.model.partition_key` the builder stamps on a measured arena — never a parallel
re-implementation (owner ruling, cold start Option A, ``823502e``). A resolved declaration's
first measurement therefore lands on the identical key BY CONSTRUCTION; a declaration that turns
out to be wrong about reality is an ordinary dissolve/reopen through the existing agenda
transitions, never a special case.

**Refusal, not fallback.** ``partition_key``'s unresolved-condition arm salts the key with the
arena's own ``scan_ref`` so it couples with nothing — and a DECLARED partition has no scan_ref
to salt with. A declared condition that cannot resolve through the declared ``t_round`` binning
is therefore a program/card defect surfaced at first plan, and this module refuses loudly
(:class:`ColdStartConditionUnresolved`) rather than minting an orphan partition nothing will ever
join.

Pure and deterministic: no clock, no randomness, no workspace read. The card is hand-edited
intent (hard rule 1) and this module only READS declared values its caller hands it; replay
determinism rests on the key components the planner embeds in the decision record, never on
re-reading the mutable card.
"""
from __future__ import annotations

from dataclasses import dataclass

from isar.scene.model import (ConditionSet, partition_id, partition_key,
                              path_geometry_fingerprint)

__all__ = ["CANONICAL_PATH", "COLD_START_CONDITION_UNRESOLVED", "ColdStartConditionUnresolved",
           "DeclaredPartition", "declare_partition"]

#: The ``topology_role`` a program step's own scans carry — the one role that joins the path
#: component of :func:`~isar.scene.model.partition_key` (D3).
CANONICAL_PATH = "canonical_path"

#: The named refusal (D4). A program/card defect, never a degraded lane.
COLD_START_CONDITION_UNRESOLVED = "cold_start_condition_unresolved"


class ColdStartConditionUnresolved(ValueError):
    """The declared ConditionSet does not resolve through the declared ``t_round`` binning."""

    name = COLD_START_CONDITION_UNRESOLVED


def _canonical_conditions(conditions) -> tuple:
    """``(name, value)`` pairs sorted by name, values as floats — the SAME canonical form
    :func:`isar.schema.measurement._conditions` gives a measured arena, so a declaration and the
    measurement that fulfils it present identical condition input to ``partition_key``."""
    return tuple(sorted(((str(name), float(value)) for name, value in (conditions or ())),
                        key=lambda pair: pair[0]))


@dataclass(frozen=True, slots=True)
class DeclaredPartition:
    """The partition a not-yet-measured program step declares it will measure in.

    Carries its FULL key components, not only the id: the planner embeds them in the decision
    record so a replay reproduces the key from the record itself (the D1 invariant — no
    decision-affecting input lives outside a persisted record, and a card edited between runs
    changes the record visibly rather than silently).
    """

    partition_id: str
    sample: str | None
    conditions: tuple
    lattice_epoch: int | None
    condition_t_round: float | None
    path_fingerprint: str
    step_id: str
    topology_role: str = CANONICAL_PATH

    def __post_init__(self):
        """The resolved-condition refusal runs on EVERY construction path, not only at first
        mint: a hand-edited or corrupted decision record replayed through :meth:`from_dict` would
        otherwise mint exactly the orphan the module docstring refuses — a ``scan_ref=""``-salted
        key that couples with nothing — and do it silently, on the replay path where nobody is
        watching."""
        if not ConditionSet.of(self.conditions, self.condition_t_round).resolved:
            raise ColdStartConditionUnresolved(
                f"{COLD_START_CONDITION_UNRESOLVED}: step {self.step_id!r} declares conditions "
                f"{self.conditions!r} but condition_t_round={self.condition_t_round!r} cannot bin "
                "them, so the declared partition key would be salted with a scan_ref a "
                "declaration does not have — it would couple with nothing, including the very "
                "measurement that fulfils it. Declare collation.t_round (>= 1e-6) on the card, "
                "or declare no condition at all.")

    @property
    def key(self) -> tuple:
        """The partition key, recomputed from the carried components through the one
        implementation — what makes the round-trip below a real check and not a copy."""
        return partition_key(self.sample, self.conditions, self.lattice_epoch, "",
                             self.condition_t_round, self.topology_role, self.path_fingerprint)

    def to_dict(self) -> dict:
        return {"partition_id": self.partition_id, "sample": self.sample,
                "conditions": [list(pair) for pair in self.conditions],
                "lattice_epoch": self.lattice_epoch,
                "condition_t_round": self.condition_t_round,
                "path_fingerprint": self.path_fingerprint, "step_id": self.step_id,
                "topology_role": self.topology_role}

    @classmethod
    def from_dict(cls, d: dict) -> "DeclaredPartition":
        return cls(partition_id=str(d["partition_id"]), sample=d["sample"],
                   conditions=_canonical_conditions(d.get("conditions")),
                   lattice_epoch=d["lattice_epoch"],
                   condition_t_round=d["condition_t_round"],
                   path_fingerprint=str(d["path_fingerprint"]), step_id=str(d["step_id"]),
                   topology_role=str(d["topology_role"]))


def declare_partition(step, *, sample, conditions=(), lattice_epoch=None,
                      condition_t_round=None) -> DeclaredPartition:
    """The declared partition for one ``discover_and_map`` step. Pure.

    ``step`` is the frozen :class:`isar.schema.campaign_program.CampaignStep` (its payload's
    declared endpoints ARE the physical path, D3); everything else is card declaration the caller
    read: ``sample`` identity, the declared setpoint ``conditions`` as ``(name, value)`` pairs,
    the declared ``lattice_epoch``, and the card's own ``collation.t_round`` binning policy.

    Raises :class:`ColdStartConditionUnresolved` when the declared conditions do not resolve
    through that binning — see the module docstring for why that may not degrade.
    """
    canonical = _canonical_conditions(conditions)
    path = path_geometry_fingerprint(step.payload.q_start, step.payload.q_end)
    key = partition_key(sample, canonical, lattice_epoch, "", condition_t_round,
                        CANONICAL_PATH, path)
    return DeclaredPartition(
        partition_id=partition_id(key), sample=sample, conditions=canonical,
        lattice_epoch=lattice_epoch, condition_t_round=condition_t_round,
        path_fingerprint=path, step_id=step.id)
