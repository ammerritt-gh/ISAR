"""The campaign program — the only input contract ``isar drive`` accepts (ruling H1).

A program is an ordered list of **steps**, each a declared measurement intent:

- ``targeted`` — the classical ISAR campaign: a reciprocal-space path, a target branch, and a
  per-point tolerance on ω₀. Its payload is the :class:`~isar.drive.objective.ObjectiveSpec`
  content, either as compact authoring input or as a frozen ``isar.campaign-step.targeted/1``
  record. This module deliberately does **not** re-validate that content — the objective compiler
  (:mod:`isar.drive.supervisor.goal_to_spec`) owns it, and a second copy of those rules here is how
  the two drift apart. What is checked at this layer is the payload's *identity*: a payload
  carrying a retired schema string is refused by name.
- ``discover_and_map`` — the SCENE discovery intent: a region, an intrinsic excitation-energy
  domain, a catalogue profile, and the deliverables asked of it. Parsed, validated and frozen
  here; **executing** one lands in SCENE-4.

The retired ``isar.objective/2`` payload and its ``isar.objective-envelope/2`` wrapper are
unreadable by design (H1): compatibility shims and dual-path readers are defects under that ruling,
so a retired document is refused with an error naming this contract rather than adapted.

The energy field is the one place where a schema decision carries physics. ``discover_and_map``
declares an **intrinsic** excitation-energy domain — the |ℏω| of the excitations sought, always
non-negative. It is NOT a signed measurement window: whether a scan measures the Stokes side, the
anti-Stokes side, or both is a runtime acquisition decision made against detailed balance and the
temperature, and conflating the two in the frozen program would silently decide it at compile time.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from . import question_policy as _policy

PROGRAM_SCHEMA = "isar.campaign-program/1"
TARGETED_STEP_SCHEMA = "isar.campaign-step.targeted/1"
DISCOVER_STEP_SCHEMA = "isar.campaign-step.discover_and_map/2"
STEP_KINDS = ("targeted", "discover_and_map")

#: Every schema string a current contract replaced, mapped to its replacement. Read as a
#: *refusal* table, never a migration table — the mapping exists so the refusal names the
#: correct successor (a retired step schema was not retired "by" the program schema).
RETIRED_SCHEMA_REPLACEMENTS = {
    "isar.objective/1": PROGRAM_SCHEMA,
    "isar.objective/2": PROGRAM_SCHEMA,
    "isar.objective-envelope/1": PROGRAM_SCHEMA,
    "isar.objective-envelope/2": PROGRAM_SCHEMA,
    # SCENE-4 PR 4.1: the /1 discover step froze no question policy, so reading one today
    # would acquire read-time defaults — exactly what H1 forbids.
    "isar.campaign-step.discover_and_map/1": DISCOVER_STEP_SCHEMA,
}
RETIRED_SCHEMAS = tuple(RETIRED_SCHEMA_REPLACEMENTS)


class ProgramError(ValueError):
    """A document is not a valid ``isar.campaign-program/1`` (or names a retired contract)."""


def retired_schema_error(schema: str, where: str) -> ProgramError:
    """The refusal for a document naming a contract this program replaced (H1)."""
    replacement = RETIRED_SCHEMA_REPLACEMENTS.get(schema, PROGRAM_SCHEMA)
    return ProgramError(
        f"{where}: {schema!r} was retired by {replacement!r} (DECISIONS H1) and is not readable; "
        f"recompile the campaign with `isar drive --goal ...  --compile-only <program.json>`")


def _q_triple(value: Any, where: str) -> tuple[float, float, float]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 3:
        raise ProgramError(f"{where} must be three reciprocal-lattice coordinates [h, k, l]")
    out = []
    for coordinate in value:
        if isinstance(coordinate, bool) or not isinstance(coordinate, (int, float)):
            raise ProgramError(f"{where} must contain numbers, got {coordinate!r}")
        if not math.isfinite(coordinate):
            raise ProgramError(f"{where} must be finite")
        out.append(round(float(coordinate), 9))
    return (out[0], out[1], out[2])


def _only(mapping: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise ProgramError(f"{where} has unknown fields: {unknown}")


# --------------------------------------------------------------------------- discovery intent
@dataclass(frozen=True)
class DiscoverAndMapSpec:
    """"Map everything interesting in here" — a region, an energy domain, and what to deliver.

    Attributes:
        q_start, q_end: the region's endpoints in r.l.u. Only ``region_kind="path"`` exists today;
            the field is explicit so a later volume/plane region is an added kind rather than a
            reinterpretation of these two points.
        maximum_initial_spacing_rlu: the coarsest acceptable spacing of the *initial* survey. It
            bounds the opening sampling, not the campaign — discovery refines where it finds
            structure, so this is a ceiling on ignorance, not a grid.
        intrinsic_omega_domain_meV: ``[lo, hi]`` on the INTRINSIC excitation energy |ℏω|, hence
            ``lo >= 0``. Deliberately not a signed measurement window (see the module docstring).
        catalogue_profile: the phenomenon catalogue this campaign judges against, by reference. The
            catalogue itself is a later slice; the reference is frozen now so the program that ran
            records which catalogue it meant.
        deliverables: what the campaign is asked to produce, as ``{"kind": ..., ...}`` records,
            frozen verbatim. Only the presence of a non-empty ``kind`` is checked here; the
            semantics belong to the stage that fulfils them.
        include_unknown: whether phenomena matching nothing in the catalogue are reported rather
            than discarded. Defaults true — a target-free campaign that silently drops what it
            cannot name is not doing discovery.
        resolved_policy: the REQUIRED frozen question-policy block
            (``isar.question-policy/2``, SCENE-4 PR 4.1/4.2a): effective thresholds, family
            priorities, emergent-question policy, survey order, staleness policy, prompt
            defaults, per-field provenance. The compiler resolves it (user ask > card guidelines
            > named defaults)
            and freezes it here; a program without one is refused with a recompile message,
            because a frozen program must never acquire read-time defaults (H1).
    """

    q_start: tuple[float, float, float]
    q_end: tuple[float, float, float]
    maximum_initial_spacing_rlu: float
    intrinsic_omega_domain_meV: tuple[float, float]
    catalogue_profile: str
    resolved_policy: Mapping[str, Any]
    deliverables: tuple[Mapping[str, Any], ...] = ()
    include_unknown: bool = True
    region_kind: str = "path"

    def __post_init__(self) -> None:
        s = object.__setattr__
        s(self, "q_start", _q_triple(self.q_start, "region.q_start"))
        s(self, "q_end", _q_triple(self.q_end, "region.q_end"))
        if self.region_kind != "path":
            raise ProgramError(
                f"region.kind {self.region_kind!r} is not supported; the only region kind in "
                f"{DISCOVER_STEP_SCHEMA!r} is 'path'")
        if self.q_start == self.q_end:
            raise ProgramError("region.q_start and region.q_end are identical; a path region "
                               "with no extent declares no region to discover in")
        spacing = self.maximum_initial_spacing_rlu
        if isinstance(spacing, bool) or not isinstance(spacing, (int, float)):
            raise ProgramError("region.maximum_initial_spacing_rlu must be a number in r.l.u.")
        if not math.isfinite(spacing) or spacing <= 0.0:
            raise ProgramError("region.maximum_initial_spacing_rlu must be positive and finite")
        s(self, "maximum_initial_spacing_rlu", float(spacing))

        domain = self.intrinsic_omega_domain_meV
        if (not isinstance(domain, Sequence) or isinstance(domain, (str, bytes))
                or len(domain) != 2):
            raise ProgramError("energy.intrinsic_omega_domain_meV must be [low, high] in meV")
        lo, hi = domain
        for value in (lo, hi):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ProgramError("energy.intrinsic_omega_domain_meV must contain numbers")
            if not math.isfinite(value):
                raise ProgramError("energy.intrinsic_omega_domain_meV must be finite")
        if float(lo) < 0.0:
            raise ProgramError(
                "energy.intrinsic_omega_domain_meV is an INTRINSIC excitation-energy domain "
                "(|hbar omega| >= 0), not a signed measurement window; a negative bound would "
                "freeze an anti-Stokes coverage decision that belongs to acquisition")
        if not float(lo) < float(hi):
            raise ProgramError("energy.intrinsic_omega_domain_meV must be [low, high] with "
                               "low < high")
        s(self, "intrinsic_omega_domain_meV", (float(lo), float(hi)))

        if not isinstance(self.catalogue_profile, str) or not self.catalogue_profile.strip():
            raise ProgramError("catalogue_profile must be a non-empty string reference")

        deliverables = self.deliverables
        if isinstance(deliverables, Mapping) or not isinstance(deliverables, Sequence) \
                or isinstance(deliverables, (str, bytes)):
            raise ProgramError("deliverables must be a list of {'kind': ...} records")
        frozen = []
        for index, deliverable in enumerate(deliverables):
            if not isinstance(deliverable, Mapping):
                raise ProgramError(f"deliverables[{index}] must be an object with a 'kind'")
            kind = deliverable.get("kind")
            if not isinstance(kind, str) or not kind.strip():
                raise ProgramError(f"deliverables[{index}] needs a non-empty string 'kind'")
            frozen.append(copy.deepcopy(dict(deliverable)))
        s(self, "deliverables", tuple(frozen))

        if not isinstance(self.include_unknown, bool):
            raise ProgramError("include_unknown must be a boolean")

        try:
            _policy.validate_resolved_block(self.resolved_policy, "resolved_policy")
        except ValueError as error:
            # Includes the missing-block case (None is not an object). ProgramError IS a
            # ValueError, so existing callers' handling is unchanged.
            raise ProgramError(
                f"{DISCOVER_STEP_SCHEMA} requires a frozen question-policy block: {error}. "
                "Recompile the campaign with `isar drive --goal ... --compile-only`") from error
        s(self, "resolved_policy", copy.deepcopy(dict(self.resolved_policy)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": DISCOVER_STEP_SCHEMA,
            "region": {"kind": self.region_kind,
                       "q_start": list(self.q_start),
                       "q_end": list(self.q_end),
                       "maximum_initial_spacing_rlu": self.maximum_initial_spacing_rlu},
            "energy": {"intrinsic_omega_domain_meV": list(self.intrinsic_omega_domain_meV)},
            "catalogue_profile": self.catalogue_profile,
            "deliverables": [copy.deepcopy(dict(d)) for d in self.deliverables],
            "include_unknown": self.include_unknown,
            "resolved_policy": copy.deepcopy(dict(self.resolved_policy)),
        }

    @classmethod
    def from_dict(cls, d: Any, *, where: str = "discover_and_map") -> "DiscoverAndMapSpec":
        """Read a FROZEN ``/2`` payload. Every effective field is required explicitly.

        The compiler fills omitted authoring values BEFORE writing a ``/2`` document
        (:func:`isar.drive.supervisor.goal_to_spec.goal_to_program`); the reader defaults
        nothing, because a stored program that acquires a future software default at read
        time is frozen in name only (H1). An untagged compact payload is authoring input,
        not a frozen step — it is refused toward the compiler rather than interpreted.
        """
        if not isinstance(d, Mapping):
            raise ProgramError(f"{where} payload must be an object")
        schema = d.get("schema")
        if schema in RETIRED_SCHEMAS:
            raise retired_schema_error(str(schema), where)
        if schema != DISCOVER_STEP_SCHEMA:
            raise ProgramError(
                f"{where} payload schema must be {DISCOVER_STEP_SCHEMA!r}, got {schema!r}; "
                "compact discover authoring is compiled, never read — recompile with "
                "`isar drive --goal ... --compile-only`")
        _only(d, {"schema", "region", "energy", "catalogue_profile", "deliverables",
                  "include_unknown", "resolved_policy"}, where)
        region = d.get("region")
        if not isinstance(region, Mapping):
            raise ProgramError(f"{where} needs a 'region' object")
        _only(region, {"kind", "q_start", "q_end", "maximum_initial_spacing_rlu"},
              f"{where}.region")
        for required in ("kind", "q_start", "q_end", "maximum_initial_spacing_rlu"):
            if required not in region:
                raise ProgramError(f"{where}.region needs {required!r}")
        energy = d.get("energy")
        if not isinstance(energy, Mapping):
            raise ProgramError(f"{where} needs an 'energy' object with "
                               "intrinsic_omega_domain_meV")
        _only(energy, {"intrinsic_omega_domain_meV"}, f"{where}.energy")
        if "intrinsic_omega_domain_meV" not in energy:
            raise ProgramError(f"{where}.energy needs 'intrinsic_omega_domain_meV'")
        # The full effective field set, no read-time defaulting: a /2 document missing any of
        # these is refused, not completed.
        for required in ("catalogue_profile", "deliverables", "include_unknown",
                         "resolved_policy"):
            if required not in d:
                raise ProgramError(
                    f"{where} ({DISCOVER_STEP_SCHEMA}) requires {required!r} explicitly; a "
                    "frozen program never acquires read-time defaults — recompile with "
                    "`isar drive --goal ... --compile-only`")
        return cls(
            q_start=region["q_start"],
            q_end=region["q_end"],
            maximum_initial_spacing_rlu=region["maximum_initial_spacing_rlu"],
            intrinsic_omega_domain_meV=energy["intrinsic_omega_domain_meV"],
            catalogue_profile=d["catalogue_profile"],
            resolved_policy=d["resolved_policy"],
            # Present-but-invalid must reach __post_init__: ``or ()`` turned a null or an object
            # into a valid empty tuple, so a malformed program round-tripped as a *different*
            # valid program instead of being refused.
            deliverables=d["deliverables"],
            include_unknown=d["include_unknown"],
            region_kind=region["kind"],
        )


# --------------------------------------------------------------------------- steps
@dataclass(frozen=True)
class CampaignStep:
    """One declared intent in a program. ``payload``'s type is decided by ``kind``.

    ``targeted`` carries a plain dict (compact objective authoring, or a frozen
    ``isar.campaign-step.targeted/1`` record); ``discover_and_map`` carries a
    :class:`DiscoverAndMapSpec`.
    """

    id: str
    kind: str
    payload: Any

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ProgramError("every step needs a non-empty string id")
        if self.kind not in STEP_KINDS:
            raise ProgramError(
                f"step {self.id!r}: unknown step kind {self.kind!r}; {PROGRAM_SCHEMA} defines "
                f"{list(STEP_KINDS)}")
        if self.kind == "discover_and_map":
            if not isinstance(self.payload, DiscoverAndMapSpec):
                raise ProgramError(
                    f"step {self.id!r}: a discover_and_map payload must be a DiscoverAndMapSpec")
            return
        if not isinstance(self.payload, Mapping):
            raise ProgramError(f"step {self.id!r}: a targeted payload must be an object")
        schema = self.payload.get("schema")
        if schema in RETIRED_SCHEMAS:
            raise retired_schema_error(str(schema), f"step {self.id!r} payload")
        if schema is not None and schema != TARGETED_STEP_SCHEMA:
            raise ProgramError(
                f"step {self.id!r}: a targeted payload schema must be {TARGETED_STEP_SCHEMA!r}, "
                f"got {schema!r}")
        object.__setattr__(self, "payload", copy.deepcopy(dict(self.payload)))

    def to_dict(self) -> dict[str, Any]:
        payload = (self.payload.to_dict() if isinstance(self.payload, DiscoverAndMapSpec)
                   else copy.deepcopy(dict(self.payload)))
        return {"id": self.id, "kind": self.kind, "payload": payload}

    @classmethod
    def from_dict(cls, d: Any, *, where: str = "step") -> "CampaignStep":
        if not isinstance(d, Mapping):
            raise ProgramError(f"{where} must be an object with id, kind and payload")
        _only(d, {"id", "kind", "payload"}, where)
        for required in ("id", "kind", "payload"):
            if required not in d:
                raise ProgramError(f"{where} needs {required!r}")
        kind = d["kind"]
        payload = d["payload"]
        if kind == "discover_and_map":
            payload = DiscoverAndMapSpec.from_dict(payload, where=f"{where} {d['id']!r}")
        return cls(id=d["id"], kind=kind, payload=payload)


# --------------------------------------------------------------------------- program
@dataclass(frozen=True)
class CampaignProgram:
    """An ordered, frozen list of campaign steps — what the driver is asked to do.

    ``source_text`` is the operator's command verbatim (the natural-language goal, when there was
    one). ``source`` is the compile-time provenance fingerprint the CLI stamps into
    ``campaign_start``; a program written by ``--compile-only`` carries it so a confirmed start
    stays comparable with the authoring input it came from.
    """

    steps: tuple[CampaignStep, ...]
    source_text: str | None = None
    source: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        steps = tuple(self.steps)
        if not steps:
            raise ProgramError(f"{PROGRAM_SCHEMA} needs at least one step")
        if any(not isinstance(step, CampaignStep) for step in steps):
            raise ProgramError("every program step must be a CampaignStep")
        seen: set[str] = set()
        for step in steps:
            if step.id in seen:
                raise ProgramError(f"duplicate step id {step.id!r}; step ids identify a step "
                                   "across the session log and must be unique")
            seen.add(step.id)
        object.__setattr__(self, "steps", steps)
        if self.source_text is not None and not isinstance(self.source_text, str):
            raise ProgramError("source_text must be the operator's command as a string")
        if not isinstance(self.source, Mapping):
            raise ProgramError("source must be an object")
        object.__setattr__(self, "source", copy.deepcopy(dict(self.source)))

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"schema": PROGRAM_SCHEMA}
        if self.source_text is not None:
            out["source_text"] = self.source_text
        if self.source:
            out["source"] = copy.deepcopy(dict(self.source))
        out["steps"] = [step.to_dict() for step in self.steps]
        return out

    @classmethod
    def from_dict(cls, d: Any, *, where: str = "program") -> "CampaignProgram":
        if not isinstance(d, Mapping):
            raise ProgramError(f"{where}: a campaign program must be a JSON object")
        schema = d.get("schema")
        if schema in RETIRED_SCHEMAS:
            raise retired_schema_error(str(schema), where)
        if schema != PROGRAM_SCHEMA:
            raise ProgramError(
                f"{where}: campaign program schema must be {PROGRAM_SCHEMA!r}, got {schema!r}")
        _only(d, {"schema", "source_text", "source", "steps"}, where)
        steps = d.get("steps")
        if not isinstance(steps, Sequence) or isinstance(steps, (str, bytes)):
            raise ProgramError(f"{where}: 'steps' must be an ordered list of at least one step")
        return cls(
            steps=tuple(CampaignStep.from_dict(step, where=f"{where}.steps[{i}]")
                        for i, step in enumerate(steps)),
            source_text=d.get("source_text"),
            # Absent means "no provenance"; a present value must be an object. ``or {}`` accepted a
            # null or a string and silently dropped it, discarding the fingerprint comparison the
            # dock relies on without saying so.
            source=d["source"] if "source" in d else {},
        )


def _store_canonical_bytes(payload: Any) -> bytes:
    """Byte-identical to ``isar.scene.model.canonical_bytes`` (model.py:138-151): sorted keys, no
    whitespace, UTF-8, strict JSON, and — the field that matters here — ``ensure_ascii=False``.

    A local 3-line copy, not an import: ``isar.schema`` may not import ``isar.scene`` (schema
    sits below scene). ``action_identity.canonical_json`` is NOT this function — it defaults
    ``ensure_ascii=True``, which escapes any non-ASCII character to ``\\uXXXX``. That is not
    "invisible for ASCII payloads" (the claim this docstring used to make); it is a SILENT BYTE
    DIVERGENCE on the one payload shape — a Unicode ``source_text`` or deliverable value — where
    it matters, and PR #33 review A-P0-5/B-1 demonstrated it concretely: the program store writes
    ``ensure_ascii=False`` bytes, a fingerprint computed with ``ensure_ascii=True`` names a
    DIFFERENT digest for the same content, and the next read reports the stored blob corrupt.
    Equality with the store's own encoding is therefore not assumed here either — it is PINNED BY
    TEST (``tests/schema/test_campaign_program.py``) on a Unicode payload, so a future divergence
    is caught immediately rather than silently forking what "the same program" digests to.
    """
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def program_fingerprint(program: CampaignProgram) -> str:
    """THE one implementation of a program's content address (PR 4.2b C7, fixed PR #33
    A-P0-5/B-1): sha256 hex over the EXACT bytes ``report/program/`` writes for this program —
    see :func:`_store_canonical_bytes`. Never ``action_identity.canonical_json``: every existing
    fingerprint in the codebase depends on that encoder's ``ensure_ascii=True`` behaviour, and
    changing it to fix this bug would move every one of them.
    """
    return hashlib.sha256(_store_canonical_bytes(program.to_dict())).hexdigest()


def targeted_program(payload: Mapping[str, Any], *, step_id: str = "targeted-1",
                     source_text: str | None = None,
                     source: Mapping[str, Any] | None = None) -> CampaignProgram:
    """The one-step targeted program a classical ISAR campaign compiles to."""
    return CampaignProgram(steps=(CampaignStep(id=step_id, kind="targeted", payload=payload),),
                           source_text=source_text, source=dict(source or {}))
