"""Reading a STORED agenda revision back out of a workspace (SCENE-4 PR 4.2b C8 / B7).

Mirrors :mod:`isar.workspace_api.scene_view`'s structure and error posture exactly: absence is
graceful (no agenda store, or an empty pointer, reads as ``None``), corruption is loud (a damaged
pointer, a blob that does not hash to its own digest, or a payload this reader cannot interpret
raises ``ValueError`` rather than degrading into a warning — see ``scene_view``'s module docstring
for why burying that in ``ws.warnings`` would go unnoticed).

**Why the store objects are re-instantiated here rather than imported.** ``isar.workspace_api``
may import ``isar.scene`` and ``isar.schema`` but never ``isar.drive``
(``tests/test_import_contract.py``'s ``WORKSPACE_API_ALLOWED``) — so C7's
``isar.drive.steering.planner.PROGRAM_STORE`` is off limits from here. Symmetrically,
``isar.scene.agenda`` is NOT on the allow-list either (it is the derivation engine — predicates,
questions, the whole evaluator — a far heavier and more coupled import than a read-only facade
should ever need just to read a pointer). Both the agenda and program store BINDINGS are therefore
re-instantiated locally from the one shared core, :class:`isar.scene.store.RevisionStore` (already
on the allow-list) — never a second implementation of the store itself, only a second binding of
it, the same relationship ``AGENDA_STORE``/``SCENE_STORE``/``PROGRAM_STORE`` already have with each
other. Their schema strings are pinned equal to the drive/scene originals by test
(``tests/scene/test_workspace_api_agenda.py``), never assumed.

**Freshness is five comparands, tri-state** (resolution (g); PR #33 A-P1-f added ``derivation``):
``scene``, ``program``, ``policy``, ``registry``, ``derivation``. Any MISMATCH marks ``stale``
(every mismatched comparand named); with no mismatch, any comparand this reader cannot recover
(no program store yet, a 4.1-era agenda with a null registry fingerprint, a step the current
program no longer declares, a manifest predating the opener/matcher/evaluator version fields)
marks ``unknown``; only when every comparand recovers and matches is the verdict ``fresh``. A
scene match ALONE is never enough — matching scene digests do not imply the program, policy,
registry, or derivation code are current.

**``derivation`` closes a gap the other four leave open.** ``registry`` only compares the
generator registry's own fingerprint; it says nothing about the OPENER/matcher/predicate-
evaluator code that ran to produce a stored agenda's specs and assessments in the first place. A
stored agenda derived under an older ``OPENER_VERSION`` (or predicate evaluator version) can have
a current scene, program, policy, and registry while still reflecting retired derivation logic —
exactly the gap this comparand closes, comparing the persisted ``derivation_manifest``'s
``opener_version``/``matcher_version``/``evaluator_versions`` against this module's own pinned
current literals (never against ``isar.scene.agenda``/``isar.scene.predicates`` directly — see
below).

Policy identity check: read directly off ``isar.scene.agenda``'s own payload assembly (verified
against source, not memory) — an agenda revision persists BOTH the complete ``resolved_policy``
block AND a dedicated ``resolved_policy_fingerprint`` field (``fingerprint(policy)``, the SAME
``isar.scene.model.fingerprint`` this module already uses). The dedicated field is compared
directly against a fresh fingerprint of the recovered program step's ``resolved_policy`` — there
is no need to re-fingerprint the embedded block by hand, and doing so would be a second
computation of a value already sitting on the payload.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from isar.schema.generators import DISCOVERY_GENERATOR_REGISTRY_VERSION
from isar.scene.model import canonical_bytes, fingerprint
from isar.scene.store import RevisionStore

# Duplicated rather than imported (see the module docstring): pinned equal to
# isar.scene.agenda.AGENDA_SCHEMA and isar.scene.agenda.AGENDA_STORE / the C7 PROGRAM_STORE by
# tests/scene/test_workspace_api_agenda.py, never assumed.
AGENDA_SCHEMA = "isar.agenda-revision/1"
_AGENDA_STORE = RevisionStore("agenda", "isar.agenda-pointer/1", "agenda")
_PROGRAM_STORE = RevisionStore("program", "isar.program-pointer/1", "program")
_TRANSITIONS_NAME = "transitions.jsonl"

# PR #33 A-P1-f: pinned duplicates of isar.scene.agenda.OPENER_VERSION/MATCHER_VERSION and
# isar.scene.predicates.EVALUATORS' per-kind versions — NOT imported, for the same reason
# AGENDA_SCHEMA above is duplicated rather than imported (isar.scene.agenda/predicates are
# heavier, more coupled modules than a read-only facade should need). Pinned equal by test
# (tests/scene/test_workspace_api_agenda.py), never assumed.
_OPENER_VERSION = 4
_MATCHER_VERSION = 1
_EVALUATOR_VERSIONS = {
    "coverage_complete": 1,
    "structural_presence": 1,
    "identity_resolved": 1,
    "track_connected": 1,
    "parameter_within_tolerance": 2,
    "zc_linear_compatibility": 1,
    "mismatch_assessed": 1,
    "completeness_relative": 1,
}

# The top-level keys this reader projects; presence and type only (see scene_view.py's rationale
# for the same posture — a payload that cannot answer these cannot be read as an agenda at all).
_INTERPRETED = ("specs", "assessments")


# ----------------------------------------------------------------------------
# Freshness
# ----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class FreshnessReport:
    """``verdict`` in ``{"fresh", "stale", "unknown"}``. ``stale_reasons`` names every mismatched
    comparand (never just the first); ``unknown_reasons`` names every comparand this reader could
    not recover a fresh value for at all — a workspace with no program store yet reads as
    ``unknown`` on BOTH ``program`` and ``policy``, honestly, never silently as ``fresh``."""

    verdict: str
    stale_reasons: tuple[str, ...]
    unknown_reasons: tuple[str, ...]


def _current_scene_digest(report_dir: Path) -> str | None:
    from isar.scene.store import SCENE_STORE
    return SCENE_STORE.pointer_digest(report_dir)


def compute_freshness(agenda_payload: dict, report_dir: Path) -> FreshnessReport:
    """The five-comparand freshness verdict for one agenda payload against the CURRENT scene,
    program, registry, and derivation-code state under ``report_dir``. Pure given its inputs;
    re-reads the other stores each call (deliberately not cached — the whole point is "as of
    right now")."""
    stale: list[str] = []
    unknown: list[str] = []

    scene_now = _current_scene_digest(report_dir)
    if scene_now is None:
        unknown.append("scene")
    elif agenda_payload.get("scene_digest") != scene_now:
        stale.append("scene")

    program_current = _PROGRAM_STORE.read_current(report_dir)
    if program_current is None:
        # No program store at all: neither "program" nor "policy" (which is recovered FROM the
        # stored program's matching step) can be checked against anything.
        unknown.extend(("program", "policy"))
    else:
        program_pointer, program_payload = program_current
        if agenda_payload.get("program_fingerprint") != program_pointer["digest"]:
            stale.append("program")
        step = next((s for s in program_payload.get("steps") or ()
                    if isinstance(s, dict) and s.get("id") == agenda_payload.get("step_id")),
                   None)
        resolved_policy = (step or {}).get("payload", {}).get("resolved_policy") \
            if isinstance(step, dict) else None
        if not isinstance(resolved_policy, dict):
            # The current program no longer declares this step (or declares it without a frozen
            # policy block) -- a real, honest state, not store damage, so it reads as unknown
            # rather than raising.
            unknown.append("policy")
        elif agenda_payload.get("resolved_policy_fingerprint") != fingerprint(resolved_policy):
            stale.append("policy")

    manifest = agenda_payload.get("derivation_manifest") or {}
    registry_fp = manifest.get("generator_registry_fingerprint")
    if registry_fp is None:
        unknown.append("registry")
    elif registry_fp != DISCOVERY_GENERATOR_REGISTRY_VERSION:
        stale.append("registry")

    # PR #33 A-P1-f: the fifth comparand -- opener/matcher/predicate-evaluator versions, distinct
    # from `registry` above (which only speaks to the generator registry). A manifest predating
    # these fields (a hand-built old fixture) is honestly unknown, never assumed fresh.
    opener_version = manifest.get("opener_version")
    matcher_version = manifest.get("matcher_version")
    evaluator_versions = manifest.get("evaluator_versions")
    if opener_version is None or matcher_version is None or evaluator_versions is None:
        unknown.append("derivation")
    elif (opener_version, matcher_version, evaluator_versions) != (
            _OPENER_VERSION, _MATCHER_VERSION, _EVALUATOR_VERSIONS):
        stale.append("derivation")

    if stale:
        verdict = "stale"
    elif unknown:
        verdict = "unknown"
    else:
        verdict = "fresh"
    return FreshnessReport(verdict=verdict, stale_reasons=tuple(stale),
                           unknown_reasons=tuple(unknown))


# ----------------------------------------------------------------------------
# The stored revision
# ----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AgendaRevisionView:
    """One stored agenda revision: pointer metadata, the payload it names, and (lazily, since it
    reads OTHER stores) its freshness.

    ``payload`` is decoded fresh on every access, exactly mirroring ``SceneRevisionView`` — a
    shared mutable dict handed to several readers is how one renderer's edit becomes another's
    belief.
    """

    digest: str
    parent: str | None
    written_at: str | None
    canonical: bytes
    _report_dir: Path

    @property
    def payload(self) -> dict:
        return json.loads(self.canonical)

    @property
    def specs(self) -> list:
        return self.payload.get("specs") or []

    @property
    def assessments(self) -> list:
        return self.payload.get("assessments") or []

    @property
    def derivation_manifest(self) -> dict:
        return self.payload.get("derivation_manifest") or {}

    @property
    def resolved_policy(self) -> dict:
        return self.payload.get("resolved_policy") or {}

    def freshness(self) -> FreshnessReport:
        return compute_freshness(self.payload, self._report_dir)


def agenda_revision(ws) -> AgendaRevisionView | None:
    """The agenda revision ``ws`` currently points at, or ``None`` if it has none.

    Raises ``ValueError`` on a damaged store or an uninterpretable payload — never degraded into
    "no agenda", which means something different to an operator (see the module docstring).
    """
    current = _AGENDA_STORE.read_current(ws._report_dir)
    if current is None:
        return None
    pointer, payload = current
    digest = str(pointer["digest"])
    _refuse_uninterpretable(payload, ws._report_dir / "agenda" / f"{digest}.json")
    return AgendaRevisionView(digest=digest, parent=pointer.get("parent"),
                              written_at=pointer.get("written_at"),
                              canonical=canonical_bytes(payload), _report_dir=ws._report_dir)


def _refuse_uninterpretable(payload, blob_path) -> None:
    if not isinstance(payload, dict):
        raise ValueError(f"agenda revision {blob_path} is not an object: {payload!r}")
    schema = payload.get("schema")
    if schema != AGENDA_SCHEMA:
        raise ValueError(f"agenda revision {blob_path} declares schema {schema!r}, expected "
                         f"{AGENDA_SCHEMA!r} — this reader will not interpret it")
    missing = [k for k in _INTERPRETED if not isinstance(payload.get(k), list)]
    if missing:
        raise ValueError(f"agenda revision {blob_path} declares {AGENDA_SCHEMA} but carries no "
                         f"usable {', '.join(missing)}")


# ----------------------------------------------------------------------------
# Transitions
# ----------------------------------------------------------------------------
def read_transitions(ws) -> list[dict]:
    """Every recorded agenda transition line, in write order — one entry per flip
    (``report/agenda/transitions.jsonl``), never re-derived from the stored specs/assessments."""
    path = _AGENDA_STORE._root(ws._report_dir) / _TRANSITIONS_NAME
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
