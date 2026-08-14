"""Question vocabulary: subjects, specs, assessments, blockers, exhaustion (SCENE-4 PR 4.1).

Three objects, not one, because identity, assessment, and agenda change at different rates
(H17): a :class:`QuestionSpec` is immutable and names WHAT is asked; a
:class:`QuestionAssessment` is one scene revision's answer state; the agenda revision that
carries both lives in ``isar.scene.agenda``. This module is the vocabulary layer — stdlib-only
like ``model.py`` (enforced by ``tests/scene/test_import_ban.py``), no evaluator logic (that is
``predicates.py``), no derivation or persistence (that is ``agenda.py``).

**Identity is semantic, never structural.** A question's stable ID is a fingerprint of
``{family, claim kind, subject}`` and nothing else: no thresholds, no policy provenance, no
program fingerprint, no scene content — so a consultation-driven threshold re-freeze and an
added neighbouring scan both leave every ID unchanged. Subjects are built only from frozen
program declarations, the frozen catalogue vocabulary, declared sample knowledge, and partition
identity; never from cluster indices, alternative pointers, component IDs, or scan refs, all of
which churn when the scene changes. A bare ``question_id`` is unique only within its
agenda/program-step scope — external references (4.2/4.3) persist ``(agenda_digest,
question_id)``.

**Blockers are typed** because "harvested reasons" are not all the same thing: only a durable
experimental blocker may combine with an authenticated exhaustion proof to close a question
``unresolvable``. A quarantined scan means "retry or act differently", not "physics forbids the
measurement", and a missing scene capability (no identity hypotheses, no persisted parameter
uncertainty) is an authority limitation that closes nothing.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .model import fingerprint

# ----------------------------------------------------------------------------
# Enumerated vocabularies
# ----------------------------------------------------------------------------
FAMILIES = ("DiscoverRegion", "ConfirmPresence", "ClassifyIdentity", "ConnectTrack",
            "EstimateParameter", "TestModelMismatch", "VerifyCompleteness")

SUBJECT_KINDS = ("region", "phenomenon", "track", "parameter", "model", "campaign")

# The snapshot statuses — each a pure function of the derivation inputs. ``overtaken``,
# ``dissolved`` and ``reopened`` are deliberately NOT here: they are relations between two
# agendas, recorded as typed history transitions by the store layer, which a pure snapshot
# cannot contain (the epoch-3 review ruling).
SNAPSHOT_STATUSES = ("open", "answered", "unresolvable")

# Typed history-transition relations (agenda flip time, outside the digest).
TRANSITION_RELATIONS = ("retained", "split_into", "merged_from", "superseded", "dissolved",
                        "reopened")

# Authority lanes, shared vocabulary with the predicate evaluators.
AUTHORITY_LANES = ("exact", "bounded", "unresolved")

# What a predicate may ANSWER from — a requirement over lanes, not a lane itself ("unresolved"
# never answers, so it cannot be required).
AUTHORITY_REQUIREMENTS = ("exact", "exact_or_bounded")

# Blocker classes. Only ``durable`` can feed unresolvable closure.
BLOCKER_DURABLE = "durable"
BLOCKER_TRANSIENT = "transient"
BLOCKER_AUTHORITY = "model_authority"
BLOCKER_CLASSES = (BLOCKER_DURABLE, BLOCKER_TRANSIENT, BLOCKER_AUTHORITY)

# The named blockers this slice knows, each pinned to its class so no caller re-decides what is
# durable. The mapping is the contract; an unlisted name refuses (a blocker nobody classified
# must not silently count toward — or be excluded from — unresolvable closure).
BLOCKER_CLASS_BY_NAME = {
    # durable: physics or hard declarations forbid the discriminating measurement
    "resolution_floor": BLOCKER_DURABLE,
    "bose_dead": BLOCKER_DURABLE,
    "instrument_geometry_infeasible": BLOCKER_DURABLE,
    "program_boundary": BLOCKER_DURABLE,
    # transient: retry or another action can clear it
    "quarantined_scan": BLOCKER_TRANSIENT,
    "instrument_failure_transient": BLOCKER_TRANSIENT,
    "fit_failed": BLOCKER_TRANSIENT,
    "analysis_unavailable": BLOCKER_TRANSIENT,
    # model/authority: the scene cannot currently represent the answer
    "identity_hypotheses_unavailable": BLOCKER_AUTHORITY,
    "dispersion_support_undeclared": BLOCKER_AUTHORITY,
    "posterior_truncation_unresolved": BLOCKER_AUTHORITY,
    "within_candidate_uncertainty_unavailable": BLOCKER_AUTHORITY,
    "tolerance_below_representable_precision": BLOCKER_AUTHORITY,
    "bound_vacuous_for_threshold": BLOCKER_AUTHORITY,
    "program_partition_ambiguous": BLOCKER_AUTHORITY,
    "detection_authority_unavailable": BLOCKER_AUTHORITY,
}


def _sorted_pairs(value, what: str) -> tuple:
    """Canonicalize a mapping-ish input to sorted ``(name, value)`` pairs, hashably."""
    if value is None:
        return ()
    items = value.items() if isinstance(value, dict) else value
    out = []
    for item in items:
        name, v = item
        out.append((str(name), v if not isinstance(v, list) else tuple(v)))
    out.sort(key=lambda p: p[0])
    return tuple(out)


def _interval(value, what: str) -> tuple[float, float] | None:
    """A closed interval, canonicalized so reversed endpoints are the same identity.

    A region retyped as ``(hi, lo)`` is the same physics as ``(lo, hi)``; letting the order
    through would mint a second scientific question for one region.
    """
    if value is None:
        return None
    lo, hi = float(value[0]), float(value[1])
    if not (math.isfinite(lo) and math.isfinite(hi)):
        raise ValueError(f"{what} must be finite, got {value!r}")
    return (lo, hi) if lo <= hi else (hi, lo)


# ----------------------------------------------------------------------------
# Subjects
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class QuestionSubject:
    """The canonical semantic identity a question is ABOUT.

    ``partition_id`` is the PR 4.0 partition fingerprint this subject lives in;
    ``scope_selector`` (mutually exclusive with it) marks a program-scoped subject that the
    agenda derivation fans out into one partition-specific child per compatible scene partition —
    no predicate ever pools probability across physical partitions (epoch-3 partition rule).

    ``region_rlu`` and ``omega_band_meV`` are intervals on frozen grids: the q quantum is the
    program's ``maximum_initial_spacing_rlu``, the ω quantum the policy profile's
    ``emergent_omega_tile_meV`` — both compile-frozen, so no identity constant hides in code and
    an emergent subject's tile is reproducible across recompiles of the same program.

    ``anchor`` is a DECLARED fold/zone-centre coordinate (zone geometry, H13) — deliberately not
    "a Bragg reflection": FCC (3,0,0) is a fold with an extinct reflection, and the two
    declarations are independent (`SampleKnowledge`).

    Track subjects are (partition, endpoint tile A, endpoint tile B[, class, ω anchors]) —
    NEVER "the component containing component-ID X": component IDs embed scan refs and candidate
    labels and churn with the scene, which would turn dependency and stop semantics into
    label-matching.
    """

    kind: str
    partition_id: str | None = None
    scope_selector: tuple = ()                   # sorted pairs; program-scoped subjects only
    region_rlu: tuple[float, float] | None = None
    omega_band_meV: tuple[float, float] | None = None
    catalogue_class: str | None = None
    parameter: str | None = None
    anchor: float | None = None                  # declared fold/ZC coordinate, axis rlu
    endpoint_a_rlu: tuple[float, float] | None = None   # track subjects: canonical tile A
    endpoint_b_rlu: tuple[float, float] | None = None   # track subjects: canonical tile B
    frame: tuple = ()                            # sorted pairs: what this subject is relative to

    def __post_init__(self):
        if self.kind not in SUBJECT_KINDS:
            raise ValueError(f"unknown subject kind {self.kind!r}; allowed: {SUBJECT_KINDS}")
        if self.partition_id is not None and self.scope_selector:
            raise ValueError("a subject is partition-specific OR program-scoped, never both: "
                             f"partition_id={self.partition_id!r} with a scope_selector")
        object.__setattr__(self, "scope_selector",
                           _sorted_pairs(self.scope_selector, "scope_selector"))
        object.__setattr__(self, "frame", _sorted_pairs(self.frame, "frame"))
        object.__setattr__(self, "region_rlu", _interval(self.region_rlu, "region_rlu"))
        object.__setattr__(self, "omega_band_meV",
                           _interval(self.omega_band_meV, "omega_band_meV"))
        # Track endpoints are canonicalized as a PAIR: the track A→B is the track B→A unless
        # direction is ever made scientifically meaningful, so the lower tile sorts first.
        a = _interval(self.endpoint_a_rlu, "endpoint_a_rlu")
        b = _interval(self.endpoint_b_rlu, "endpoint_b_rlu")
        if (a is None) != (b is None):
            raise ValueError("a track subject declares both endpoint tiles or neither")
        if a is not None and b is not None and b < a:
            a, b = b, a
        object.__setattr__(self, "endpoint_a_rlu", a)
        object.__setattr__(self, "endpoint_b_rlu", b)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "partition_id": self.partition_id,
            "scope_selector": [[k, v] for k, v in self.scope_selector],
            "region_rlu": None if self.region_rlu is None else list(self.region_rlu),
            "omega_band_meV": (None if self.omega_band_meV is None
                               else list(self.omega_band_meV)),
            "catalogue_class": self.catalogue_class,
            "parameter": self.parameter,
            "anchor": self.anchor,
            "endpoint_a_rlu": (None if self.endpoint_a_rlu is None
                               else list(self.endpoint_a_rlu)),
            "endpoint_b_rlu": (None if self.endpoint_b_rlu is None
                               else list(self.endpoint_b_rlu)),
            "frame": [[k, v] for k, v in self.frame],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "QuestionSubject":
        return cls(
            kind=str(d["kind"]),
            partition_id=d.get("partition_id"),
            scope_selector=tuple((p[0], p[1]) for p in d.get("scope_selector") or ()),
            region_rlu=d.get("region_rlu"),
            omega_band_meV=d.get("omega_band_meV"),
            catalogue_class=d.get("catalogue_class"),
            parameter=d.get("parameter"),
            anchor=d.get("anchor"),
            endpoint_a_rlu=d.get("endpoint_a_rlu"),
            endpoint_b_rlu=d.get("endpoint_b_rlu"),
            frame=tuple((p[0], p[1]) for p in d.get("frame") or ()),
        )


def snap_tile(value: float, quantum: float) -> tuple[float, float]:
    """The frozen-grid tile containing ``value``: ``[n·quantum, (n+1)·quantum)`` as a closed
    identity interval.

    Emergent subjects are identified by TILE, never by the posterior-continuous coordinate that
    triggered them — snapping is what keeps the ID stable when a second scan lands in the same
    tile a little to the left.
    """
    if not (math.isfinite(quantum) and quantum > 0.0):
        raise ValueError(f"tile quantum must be finite and positive, got {quantum!r}")
    n = math.floor(value / quantum)
    # Rounded to a fixed decimal grid: tile edges enter question identity through the
    # fingerprint, and ``41 * 0.05 == 2.0500000000000003`` would make two spellings of one
    # edge two identities.
    return (round(n * quantum, 12), round((n + 1) * quantum, 12))


def question_id(family: str, claim_kind: str, subject: QuestionSubject) -> str:
    """The stable semantic ID: ``fingerprint({family, claim_kind, subject})`` and NOTHING else.

    ``claim_kind`` (the predicate kind, without its version) joins identity because two
    scientifically different claims over one subject are two questions — EstimateParameter's
    ``parameter_within_tolerance`` and ``zc_linear_compatibility`` must not collide. Evaluator
    VERSIONS stay out: a versioned evaluator re-assesses the same question, it does not mint a
    new one.
    """
    if family not in FAMILIES:
        raise ValueError(f"unknown question family {family!r}; allowed: {FAMILIES}")
    return fingerprint({"family": family, "claim_kind": str(claim_kind),
                        "subject": subject.to_dict()})


# ----------------------------------------------------------------------------
# Predicate declaration (declarative; evaluators live in predicates.py)
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class PredicateDeclaration:
    """WHAT closes this question, declared on the spec — never evaluated here.

    ``parameters`` carry the EFFECTIVE values frozen from the resolved policy block at
    derivation time, with the profile id in ``policy_provenance`` on the spec — predicates read
    the program's frozen numbers only, never module constants at evaluation time (Topic 4).
    ``authority_requirement`` names the weakest cluster authority the predicate may answer from.
    """

    kind: str
    version: int
    parameters: tuple = ()                       # sorted pairs of effective values
    required_scene_variables: tuple[str, ...] = ()
    authority_requirement: str = "exact_or_bounded"
    evaluator_version: int = 1

    def __post_init__(self):
        if self.authority_requirement not in AUTHORITY_REQUIREMENTS:
            raise ValueError(f"unknown authority requirement {self.authority_requirement!r}; "
                             f"allowed: {AUTHORITY_REQUIREMENTS}")
        object.__setattr__(self, "parameters", _sorted_pairs(self.parameters, "parameters"))
        object.__setattr__(self, "required_scene_variables",
                           tuple(sorted(str(v) for v in self.required_scene_variables)))

    def to_dict(self) -> dict:
        return {"kind": self.kind, "version": self.version,
                "parameters": [[k, v] for k, v in self.parameters],
                "required_scene_variables": list(self.required_scene_variables),
                "authority_requirement": self.authority_requirement,
                "evaluator_version": self.evaluator_version}

    @classmethod
    def from_dict(cls, d: dict) -> "PredicateDeclaration":
        return cls(kind=str(d["kind"]), version=int(d["version"]),
                   parameters=tuple((p[0], p[1]) for p in d.get("parameters") or ()),
                   required_scene_variables=tuple(d.get("required_scene_variables") or ()),
                   authority_requirement=str(d.get("authority_requirement",
                                                   "exact_or_bounded")),
                   evaluator_version=int(d.get("evaluator_version", 1)))


# ----------------------------------------------------------------------------
# Spec and assessment
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class QuestionSpec:
    """Immutable: identity, predicate declaration, priority, dependencies, generator bindings.

    ``generator_ids`` are DECLARED REFERENCE strings into the acquisition generator registry —
    real bindings and exhaustion accounting land in 4.2; a spec referencing a generator that
    does not exist yet is a declaration, not a wiring. ``dependencies`` are question IDs this
    question waits on (the scheduler's lane 2 in 4.2); the derivation validates the whole set
    acyclic (:func:`validate_dependency_dag`).
    """

    family: str
    claim_kind: str
    subject: QuestionSubject
    predicate: PredicateDeclaration
    priority: int = 0
    dependencies: tuple[str, ...] = ()
    generator_ids: tuple[str, ...] = ()
    policy_provenance: tuple = ()                # sorted pairs: profile id/version, field sources
    question_id: str = field(init=False)

    def __post_init__(self):
        if self.family not in FAMILIES:
            raise ValueError(f"unknown question family {self.family!r}; allowed: {FAMILIES}")
        object.__setattr__(self, "dependencies",
                           tuple(sorted(str(d) for d in self.dependencies)))
        object.__setattr__(self, "generator_ids",
                           tuple(sorted(str(g) for g in self.generator_ids)))
        object.__setattr__(self, "policy_provenance",
                           _sorted_pairs(self.policy_provenance, "policy_provenance"))
        object.__setattr__(self, "question_id",
                           question_id(self.family, self.claim_kind, self.subject))

    def to_dict(self) -> dict:
        return {"question_id": self.question_id, "family": self.family,
                "claim_kind": self.claim_kind, "subject": self.subject.to_dict(),
                "predicate": self.predicate.to_dict(), "priority": self.priority,
                "dependencies": list(self.dependencies),
                "generator_ids": list(self.generator_ids),
                "policy_provenance": [[k, v] for k, v in self.policy_provenance]}

    @classmethod
    def from_dict(cls, d: dict) -> "QuestionSpec":
        spec = cls(family=str(d["family"]), claim_kind=str(d["claim_kind"]),
                   subject=QuestionSubject.from_dict(d["subject"]),
                   predicate=PredicateDeclaration.from_dict(d["predicate"]),
                   priority=int(d.get("priority", 0)),
                   dependencies=tuple(d.get("dependencies") or ()),
                   generator_ids=tuple(d.get("generator_ids") or ()),
                   policy_provenance=tuple((p[0], p[1])
                                           for p in d.get("policy_provenance") or ()))
        recorded = d.get("question_id")
        if recorded is not None and recorded != spec.question_id:
            # A stored spec whose ID does not re-derive from its own subject is corrupt or
            # hand-edited; matching on it would silently re-key dependency and stop semantics.
            raise ValueError(f"stored question_id {recorded!r} does not re-derive "
                             f"({spec.question_id!r}) — refusing the spec")
        return spec


@dataclass(frozen=True)
class Blocker:
    """One recorded obstacle, typed by class. ``name`` must be classified in
    :data:`BLOCKER_CLASS_BY_NAME` — an unclassified blocker refuses rather than silently
    counting toward (or being excluded from) unresolvable closure."""

    name: str
    detail: str = ""

    def __post_init__(self):
        if self.name not in BLOCKER_CLASS_BY_NAME:
            raise ValueError(f"unclassified blocker {self.name!r}; known: "
                             f"{sorted(BLOCKER_CLASS_BY_NAME)}")

    @property
    def blocker_class(self) -> str:
        return BLOCKER_CLASS_BY_NAME[self.name]

    def to_dict(self) -> dict:
        return {"name": self.name, "class": self.blocker_class, "detail": self.detail}

    @classmethod
    def from_dict(cls, d: dict) -> "Blocker":
        return cls(name=str(d["name"]), detail=str(d.get("detail", "")))


@dataclass(frozen=True)
class QuestionAssessment:
    """One scene revision's answer state for one question — a pure function of the derivation
    inputs, which is why only the snapshot statuses are representable here.

    ``answer`` is the structured claim (predicate-specific), present only when status is
    ``answered``. ``mass_interval`` is the [lower, upper] posterior interval the bound
    arithmetic produced (equal endpoints under ``exact`` authority). ``authority`` is the
    weakest lane among contributing clusters. ``blockers`` are typed; ``notes`` carry
    machine-readable diagnostics (``generator_defect``, vacuous-bound records) that are not
    blockers.
    """

    question_id: str
    status: str
    authority: str | None = None
    answer: tuple = ()                           # sorted pairs; () = no answer
    mass_interval: tuple[float, float] | None = None
    blockers: tuple[Blocker, ...] = ()
    reasons: tuple[str, ...] = ()
    notes: tuple = ()                            # sorted pairs

    def __post_init__(self):
        if self.status not in SNAPSHOT_STATUSES:
            raise ValueError(f"status {self.status!r} is not a snapshot status "
                             f"{SNAPSHOT_STATUSES} — overtaken/dissolved/reopened are history "
                             "transitions, recorded outside the digested snapshot")
        if self.authority is not None and self.authority not in AUTHORITY_LANES:
            raise ValueError(f"unknown authority {self.authority!r}; allowed: "
                             f"{AUTHORITY_LANES}")
        # Canonicalize BEFORE the answered check, so the check judges the canonical form and a
        # malformed answer shape fails in _sorted_pairs with its own error, not a misleading
        # "must carry its answer".
        object.__setattr__(self, "answer", _sorted_pairs(self.answer, "answer"))
        object.__setattr__(self, "notes", _sorted_pairs(self.notes, "notes"))
        if self.status == "answered" and not self.answer:
            raise ValueError("an answered assessment must carry its answer")
        object.__setattr__(self, "reasons", tuple(str(r) for r in self.reasons))
        if self.mass_interval is not None:
            lo, hi = float(self.mass_interval[0]), float(self.mass_interval[1])
            if not (0.0 <= lo <= hi <= 1.0):
                raise ValueError(f"mass_interval must be within [0, 1] and ordered, got "
                                 f"{self.mass_interval!r}")
            object.__setattr__(self, "mass_interval", (lo, hi))

    def to_dict(self) -> dict:
        return {"question_id": self.question_id, "status": self.status,
                "authority": self.authority,
                "answer": [[k, v] for k, v in self.answer],
                "mass_interval": (None if self.mass_interval is None
                                  else list(self.mass_interval)),
                "blockers": [b.to_dict() for b in self.blockers],
                "reasons": list(self.reasons),
                "notes": [[k, v] for k, v in self.notes]}

    @classmethod
    def from_dict(cls, d: dict) -> "QuestionAssessment":
        return cls(question_id=str(d["question_id"]), status=str(d["status"]),
                   authority=d.get("authority"),
                   answer=tuple((p[0], p[1]) for p in d.get("answer") or ()),
                   mass_interval=(None if d.get("mass_interval") is None
                                  else tuple(d["mass_interval"])),
                   blockers=tuple(Blocker.from_dict(b) for b in d.get("blockers") or ()),
                   reasons=tuple(d.get("reasons") or ()),
                   notes=tuple((p[0], p[1]) for p in d.get("notes") or ()))

    def durable_blockers(self) -> tuple[Blocker, ...]:
        return tuple(b for b in self.blockers if b.blocker_class == BLOCKER_DURABLE)


# ----------------------------------------------------------------------------
# Exhaustion evidence (produced by 4.2; the DTO and its freshness contract land now)
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class QuestionExhaustion:
    """Evidence that every required generator has nothing left to propose for one question.

    NOT a boolean: the proof binds to the exact assessment basis it was computed against, so a
    stale proof — an older agenda, a different scene, a changed generator registry — is
    rejected rather than closing a question it no longer describes. ``basis_agenda_digest`` is
    compared against the SAME-PASS exhaustion-free derivation digest (``basis_digest`` on the
    agenda payload), ``scene_digest`` against the input scene, and
    ``generator_registry_fingerprint`` against the derivation manifest.
    """

    question_id: str
    basis_agenda_digest: str
    scene_digest: str
    generator_registry_fingerprint: str
    required_generator_ids: tuple[str, ...] = ()
    generator_status: tuple = ()                 # sorted pairs: generator_id -> status string

    def __post_init__(self):
        object.__setattr__(self, "required_generator_ids",
                           tuple(sorted(str(g) for g in self.required_generator_ids)))
        object.__setattr__(self, "generator_status",
                           _sorted_pairs(self.generator_status, "generator_status"))

    def to_dict(self) -> dict:
        return {"question_id": self.question_id,
                "basis_agenda_digest": self.basis_agenda_digest,
                "scene_digest": self.scene_digest,
                "generator_registry_fingerprint": self.generator_registry_fingerprint,
                "required_generator_ids": list(self.required_generator_ids),
                "generator_status": [[k, v] for k, v in self.generator_status]}

    @classmethod
    def from_dict(cls, d: dict) -> "QuestionExhaustion":
        return cls(question_id=str(d["question_id"]),
                   basis_agenda_digest=str(d["basis_agenda_digest"]),
                   scene_digest=str(d["scene_digest"]),
                   generator_registry_fingerprint=str(d["generator_registry_fingerprint"]),
                   required_generator_ids=tuple(d.get("required_generator_ids") or ()),
                   generator_status=tuple((p[0], p[1])
                                          for p in d.get("generator_status") or ()))

    def exhausted(self) -> bool:
        """Every required generator reports itself done. An empty requirement set is NOT
        exhaustion — a question with no implemented generator cannot honestly exhaust (4.2)."""
        if not self.required_generator_ids:
            return False
        status = dict(self.generator_status)
        return all(status.get(g) == "exhausted" for g in self.required_generator_ids)


# ----------------------------------------------------------------------------
# Subject matching into a scene revision payload
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class SubjectMatch:
    """What one subject selects out of one scene revision payload: footprint scan_refs, the
    matched footprint dicts, and the clusters (by payload index) any of them belong to.
    Everything a predicate reads flows through this — never a stored cluster pointer."""

    scan_refs: tuple[str, ...]
    footprints: tuple[dict, ...]
    cluster_indices: tuple[int, ...]


def scene_partitions(scene_payload: dict) -> tuple[str, ...]:
    """Every partition id the revision's footprints carry, sorted; ``None`` partitions
    (topology underivable) are excluded — nothing can be scoped to them."""
    ids = {fp.get("partition_id") for fp in scene_payload.get("footprints") or ()}
    return tuple(sorted(i for i in ids if i is not None))


def _band_overlaps(fp: dict, band: tuple[float, float]) -> bool:
    return any(b["hi"] > band[0] and b["lo"] < band[1] for b in fp.get("bands") or ())


def match_subject(subject: QuestionSubject, scene_payload: dict) -> SubjectMatch:
    """Select the observations/footprints/clusters a partition-specific subject matches.

    Matching is geometric and partition-exact: same ``partition_id``, ``axis_coordinate``
    inside ``region_rlu`` (when the subject declares one), intrinsic bands overlapping
    ``omega_band_meV`` (when declared). A program-scoped subject (``partition_id is None``)
    matches nothing here by design — the agenda derivation fans it out into per-partition
    children first, so no evaluator can silently pool across physical partitions.
    """
    if subject.partition_id is None:
        return SubjectMatch((), (), ())
    matched = []
    for fp in scene_payload.get("footprints") or ():
        if fp.get("partition_id") != subject.partition_id:
            continue
        if subject.region_rlu is not None:
            coord = fp.get("axis_coordinate")
            if coord is None:
                continue
            lo, hi = subject.region_rlu
            if not (lo <= coord <= hi):
                continue
        if subject.omega_band_meV is not None and not _band_overlaps(
                fp, subject.omega_band_meV):
            continue
        matched.append(fp)
    refs = tuple(sorted(fp["scan_ref"] for fp in matched))
    ref_set = set(refs)
    clusters = tuple(i for i, cluster in enumerate(scene_payload.get("clusters") or ())
                     if ref_set.intersection(cluster.get("scan_refs") or ()))
    return SubjectMatch(scan_refs=refs, footprints=tuple(matched), cluster_indices=clusters)


def resolve_scope(subject: QuestionSubject, scene_payload: dict) -> tuple[str, ...]:
    """The partitions a program-scoped subject is compatible with, from its scope selector.

    v1 selector dimensions: ``sample`` and ``lattice_epoch`` (matched against the embedded
    observations of each partition); an omitted dimension matches any value. Fan-out over the
    result is the derivation's job; an empty result on a scene WITH partitions means the
    program's declarations identify none of them, which the derivation records as
    ``program_partition_ambiguous`` (fail closed) rather than guessing.
    """
    selector = dict(subject.scope_selector)
    by_partition: dict[str, list] = {}
    partition_of = {fp["scan_ref"]: fp.get("partition_id")
                    for fp in scene_payload.get("footprints") or ()}
    for obs in scene_payload.get("observations") or ():
        pid = partition_of.get(obs.get("scan_ref"))
        if pid is not None:
            by_partition.setdefault(pid, []).append(obs)
    out = []
    for pid, members in by_partition.items():
        if "sample" in selector and any(o.get("sample") != selector["sample"]
                                        for o in members):
            continue
        if "lattice_epoch" in selector and any(o.get("lattice_epoch")
                                               != selector["lattice_epoch"] for o in members):
            continue
        out.append(pid)
    return tuple(sorted(out))


# ----------------------------------------------------------------------------
# Dependency DAG
# ----------------------------------------------------------------------------
def validate_dependency_dag(specs) -> None:
    """Refuse a cyclic dependency graph — assessment order must never depend on iteration.

    Dependencies naming ABSENT questions are legal (a dependency on a question another
    partition or a later derivation owns is a fact, not an error); only cycles among the
    present specs refuse.
    """
    graph = {s.question_id: [d for d in s.dependencies] for s in specs}
    state: dict[str, int] = {}                   # 0 visiting, 1 done

    def visit(node: str, trail: tuple) -> None:
        mark = state.get(node)
        if mark == 1 or node not in graph:
            return
        if mark == 0:
            cycle = trail[trail.index(node):] + (node,)
            raise ValueError(f"question dependency cycle: {' -> '.join(cycle)}")
        state[node] = 0
        for dep in graph[node]:
            visit(dep, trail + (node,))
        state[node] = 1

    for qid in graph:
        visit(qid, ())
