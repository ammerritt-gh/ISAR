"""Measurement-arena record (SCENE-4 PR 4.0, work package #26): counts are authoritative, fits
are disposable.

One line of ``isar.measurement-arena/1`` is written per measured arena. The durability contract is
precise, not one blanket claim (PR 4.0 review response, W4): the record is CONSTRUCTED pre-fit
(:mod:`isar.session.scan_delivery`); a ``count_trust="attempted"`` STUB is PERSISTED pre-dispatch,
before any worker starts (:mod:`isar.session.driver`, the single-writer parent) — this is what
survives a hard worker kill or an ``on_complete`` that never runs; the enriched record is persisted
at checkpoint (post-fit, last-line-wins on read) — this survives a caught ``fit_one`` exception and
any later-stage failure, but NOT a hard kill mid-fit, which leaves only the stub. It is what makes
the scene layer's honest states possible:

* ``count_trust="trusted"`` — the QC-masked measurement is usable; a fitted candidate table (if
  any) is a SEPARATE, disposable interpretation of it. A trusted arena with no candidates is not
  a failure — it is a measurement with no usable interpretation yet, and still fills the
  experiment map.
* ``count_trust="excluded"`` / ``"held_for_calibration"`` / ``"undersampled"`` — a terminal gate
  outcome. The scan was ATTEMPTED but never reached a usable measurement, so it earns no
  coverage credit downstream (:mod:`isar.scene.build`), but it is never silently dropped either —
  the record itself is the durable proof of the attempt.
* ``count_trust="attempted"`` — the pre-dispatch STUB (W4): a task was handed to a worker and
  nothing else is known yet. Non-trusted like the three states above (no coverage credit), and
  overwritten by whatever terminal state the worker eventually reaches — unless the worker is
  killed before it reaches one, in which case this stub is the only record that a scan was ever
  started at all.

Stdlib-only, like :mod:`isar.scene.model` (whose ``SceneObservation`` this record eventually
feeds via :func:`isar.workspace_api.scene.scene_observations`): the schema layer is imported from
everywhere, so it carries no dependency on :mod:`isar.session` or :mod:`isar.scene`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

MEASUREMENT_ARENA_SCHEMA = "isar.measurement-arena/1"

# States a scan can be recorded under. Only "trusted" may ever feed a scene footprint or coverage
# credit (isar.scene.build); the other four are recorded as ATTEMPTED, never as coverage —
# "attempted" (W4) is the pre-dispatch stub state, before a worker has reached any terminal outcome.
COUNT_TRUST_STATES = ("trusted", "excluded", "held_for_calibration", "undersampled", "attempted")

# The path-topology role a driven scan declares (SCENE-4 PR 4.3, D2). Owned here, beside the record
# field that carries it, and re-exported by ``isar.scene.model`` for the DTO that reads it — one
# vocabulary, so a producer and a consumer can never disagree about the legal strings.
# ``"canonical_path"`` = a scan measuring the program step's declared path; ``"context_probe"`` = a
# driver-initiated diagnostic, withheld from path derivation (isar.scene.build).
TOPOLOGY_ROLES = ("canonical_path", "context_probe")

# The campaign-provenance block a submitted request carries to the delivery pipeline (D2/D3): the
# driver owns the facts, the record owns their persistence. One tuple of names so the request
# artifact (``isar.drive.loop._persist_acquisition_provenance``) and the stamping site
# (``isar.session.scan_delivery._measurement_record``) key on the same strings.
CAMPAIGN_PROVENANCE_FIELDS = ("topology_role", "program_fingerprint", "program_step",
                              "path_fingerprint")


def _opt_float(v):
    return None if v is None else float(v)


def _opt_int(v):
    return None if v is None else int(v)


def _opt_str(v):
    return None if v is None else str(v)


def _opt_triple(v):
    if v is None:
        return None
    a, b, c = v
    return (float(a), float(b), float(c))


def _opt_pair(v):
    if v is None:
        return None
    a, b = v
    return (float(a), float(b))


def _opt_bands(v):
    if v is None:
        return None
    return tuple((float(b[0]), float(b[1])) for b in v)


def campaign_provenance(source) -> dict:
    """The four campaign-provenance fields off a request artifact, as ``MeasurementArena`` kwargs.

    ``source`` is the submitted-request payload on the driver side and the persisted acquisition
    record on the delivery side; a manual or legacy scan passes ``None`` and every field reads
    ``None`` — the absent-provenance lane the scene layer's existing geometric fallback governs.
    """
    src = source or {}
    return {name: _opt_str(src.get(name)) for name in CAMPAIGN_PROVENANCE_FIELDS}


def _conditions(v) -> tuple:
    """Name -> value pairs, sorted by name — the canonical form the fingerprint relies on."""
    return tuple(sorted(((str(k), float(val)) for k, val in (v or ())), key=lambda kv: kv[0]))


def current_lattice_epoch(epochs) -> int | None:
    """The highest-versioned lattice epoch declared as of NOW, or ``None`` when none is declared.

    Same ordering rule as :func:`isar.workspace_api.scene.sample_knowledge` (ordered as a NUMBER,
    inside the guard, so a mixed-width version series never orders lexically). Stamped onto a
    measurement-arena record at write time and never re-derived on read: a scene built later
    against a newer epoch must not silently reinterpret an older record's ``q`` under the new
    frame — the token travels with the record instead (SCENE_DESIGN H13).
    """
    versioned = [e for e in (epochs or ()) if isinstance(e, dict) and e.get("version") is not None]
    try:
        return max((int(e["version"]) for e in versioned), default=None)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unreadable lattice epoch series {epochs!r}: {exc}") from exc


@dataclass(frozen=True, slots=True)
class MeasurementArena:
    """One arena's counts-authoritative record: schema ``isar.measurement-arena/1``.

    ``count_trust`` decides everything downstream (SCENE_DESIGN §2b intake, PR 4.0 #26): only
    ``"trusted"`` may ever carry a footprint or coverage credit. The record is written once a
    :class:`isar.analyze.context.ScanContext` exists, or once a terminal gate fires — and NEVER
    depends on whether a fit was attempted, converged, or raised. That independence is precisely
    the honesty this schema exists to keep: counts are authoritative, fits are disposable.

    ``content_hash`` is the DELIVERY-GENERATION identity (the same value stamped into
    ``results.csv``/the ledger for this scan): the scene adapter joins a delivered fit row to this
    record on ``scan_ref`` AND ``content_hash``, so a record from delivery N+1 can never be read
    against a fit row still on delivery N. ``program_fingerprint`` / ``program_step`` /
    ``topology_role`` / ``path_fingerprint`` are the campaign-provenance block (PR 4.3, D2/D3):
    stamped verbatim from the submitted request, and ``None`` for every manual or pre-4.3 scan —
    an absent field reads ``None`` (H1: no compat shim), and nothing invents campaign identity for
    a scan that had none.
    """

    scan_ref: str                              # "exp:scan:det" — matches the scene adapter's key
    scan_id: str
    scan_number: int | None
    detector: str | None
    sample: str | None
    conditions: tuple                          # sorted (name, value) pairs; v1: temperature_K
    q: tuple | None                             # (h, k, l) or None
    lattice_epoch: int | None                   # current epoch off card.state.lattice_epochs
    # Signed calibrated meV, and the kept-cell runs behind it (see
    # isar.scene.model.SceneObservation for why bands and window are two different honesties).
    # None for a gated arena that never built a context, and for a built context whose
    # calibration zero was unusable.
    energy_window: tuple | None
    energy_bands: tuple | None
    effective_exposure: float | None
    exposure_provenance: dict | None
    count_trust: str                            # one of COUNT_TRUST_STATES
    reasons: tuple
    written_at: str                             # ISO-8601 UTC, stamped by the writer
    content_hash: str                           # the delivery-generation identity (required)
    program_fingerprint: str | None = None      # PR 4.3: campaign-program identity, None until then
    program_step: str | None = None             # PR 4.3: CampaignStep.id, None until then
    topology_role: str | None = None            # PR 4.3: one of TOPOLOGY_ROLES, None off-campaign
    path_fingerprint: str | None = None         # PR 4.3: isar.scene.model.path_geometry_fingerprint
    schema: str = MEASUREMENT_ARENA_SCHEMA

    def __post_init__(self) -> None:
        if self.count_trust not in COUNT_TRUST_STATES:
            raise ValueError(f"measurement-arena record {self.scan_ref!r}: count_trust "
                             f"{self.count_trust!r} is not one of {COUNT_TRUST_STATES}")
        # A present-but-unknown role refuses loudly (H1: no compat shim) — an absent field is the
        # legitimate off-campaign lane, an invented string is a producer defect.
        if self.topology_role is not None and self.topology_role not in TOPOLOGY_ROLES:
            raise ValueError(f"measurement-arena record {self.scan_ref!r}: topology_role "
                             f"{self.topology_role!r} is not one of {(*TOPOLOGY_ROLES, None)}")
        # A canonical-path arena with no path identity is unplaceable: isar.scene.model's
        # partition_key would silently drop it into the provenance-less lane, pooling it with
        # arenas from any other path at the same sample/conditions/epoch. H1: refuse loudly, no
        # invented fingerprint. A "context_probe" without one is legitimate — it declares no path.
        if self.topology_role == "canonical_path" and self.path_fingerprint is None:
            raise ValueError(f"measurement-arena record {self.scan_ref!r}: topology_role "
                             f"'canonical_path' carries no path_fingerprint")
        names = [name for name, _ in self.conditions]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"measurement-arena record {self.scan_ref!r}: duplicate condition "
                             f"name(s) {dupes} in {self.conditions!r}")
        for name, value in self.conditions:
            if not math.isfinite(value):
                raise ValueError(f"measurement-arena record {self.scan_ref!r}: condition "
                                 f"{name!r} carries non-finite value {value!r}")
        if self.q is not None and not all(math.isfinite(v) for v in self.q):
            raise ValueError(f"measurement-arena record {self.scan_ref!r}: q {self.q!r} carries "
                             f"a non-finite component")
        if self.effective_exposure is not None and not math.isfinite(self.effective_exposure):
            raise ValueError(f"measurement-arena record {self.scan_ref!r}: effective_exposure "
                             f"{self.effective_exposure!r} is not finite")
        if self.energy_window is not None:
            lo, hi = self.energy_window
            if not (math.isfinite(lo) and math.isfinite(hi)):
                raise ValueError(f"measurement-arena record {self.scan_ref!r}: energy_window "
                                 f"{self.energy_window!r} carries a non-finite bound")
            if lo > hi:
                raise ValueError(f"measurement-arena record {self.scan_ref!r}: energy_window "
                                 f"{self.energy_window!r} has lo > hi")
        if self.energy_bands is not None:
            for band in self.energy_bands:
                lo, hi = band
                if not (math.isfinite(lo) and math.isfinite(hi)):
                    raise ValueError(f"measurement-arena record {self.scan_ref!r}: energy_bands "
                                     f"entry {band!r} carries a non-finite bound")
                if lo > hi:
                    raise ValueError(f"measurement-arena record {self.scan_ref!r}: energy_bands "
                                     f"entry {band!r} has lo > hi")

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "scan_ref": self.scan_ref,
            "scan_id": self.scan_id,
            "scan_number": self.scan_number,
            "detector": self.detector,
            "sample": self.sample,
            "conditions": [[k, v] for k, v in self.conditions],
            "q": None if self.q is None else list(self.q),
            "lattice_epoch": self.lattice_epoch,
            "energy_window": None if self.energy_window is None else list(self.energy_window),
            "energy_bands": (None if self.energy_bands is None
                             else [list(b) for b in self.energy_bands]),
            "effective_exposure": self.effective_exposure,
            "exposure_provenance": (dict(self.exposure_provenance)
                                    if self.exposure_provenance is not None else None),
            "count_trust": self.count_trust,
            "reasons": list(self.reasons),
            "written_at": self.written_at,
            "content_hash": self.content_hash,
            "program_fingerprint": self.program_fingerprint,
            "program_step": self.program_step,
            "topology_role": self.topology_role,
            "path_fingerprint": self.path_fingerprint,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MeasurementArena":
        schema = str(d.get("schema", ""))
        if schema != MEASUREMENT_ARENA_SCHEMA:
            raise ValueError(f"measurement-arena record declares schema {schema!r}, expected "
                             f"{MEASUREMENT_ARENA_SCHEMA!r} — this reader will not interpret it")
        return cls(
            schema=schema,
            scan_ref=str(d["scan_ref"]),
            scan_id=str(d["scan_id"]),
            scan_number=_opt_int(d.get("scan_number")),
            detector=_opt_str(d.get("detector")),
            sample=_opt_str(d.get("sample")),
            conditions=_conditions(d.get("conditions")),
            q=_opt_triple(d.get("q")),
            lattice_epoch=_opt_int(d.get("lattice_epoch")),
            energy_window=_opt_pair(d.get("energy_window")),
            energy_bands=_opt_bands(d.get("energy_bands")),
            effective_exposure=_opt_float(d.get("effective_exposure")),
            exposure_provenance=(dict(d["exposure_provenance"])
                                 if d.get("exposure_provenance") is not None else None),
            count_trust=str(d["count_trust"]),
            reasons=tuple(str(r) for r in (d.get("reasons") or ())),
            written_at=str(d["written_at"]),
            content_hash=str(d["content_hash"]),
            program_fingerprint=_opt_str(d.get("program_fingerprint")),
            program_step=_opt_str(d.get("program_step")),
            topology_role=_opt_str(d.get("topology_role")),
            path_fingerprint=_opt_str(d.get("path_fingerprint")),
        )
