"""Private implementation of the EIG-4 Slice 1 read-only census.

This module deliberately lives under ``tools``.  It reads persisted evidence and
reports whether the current contract can be observed; it is not a runtime
eligibility evaluator and must not be imported by the fitting or steering path.
"""
from __future__ import annotations

import csv
import copy
import hashlib
import json
import logging
import math
import os
import shutil
import tempfile
import time
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from isar.predictive import (
    PREDICTIVE_ARTIFACT_SCHEMA,
    PREDICTIVE_MODEL_WEIGHT_FLOOR,
    PredictiveArtifactError,
    TARGET_PROJECTION_SCHEMA,
    json_fingerprint,
    validate_master_predictive_artifact,
)
from isar.schema.statistics import (
    SemanticsConstructionError,
    validate_fit_selection_semantics,
    validate_predictive_semantics,
    validate_shadow_model_selection,
    semantics_fingerprint,
)
from isar.schema.card import Card, CardState, load_state
from isar.session.state import SessionState


RULESET = "isar.eig4_slice1_census_rules/1"
_LOG = logging.getLogger(__name__)
_VALID_COHORTS = {"ordinary", "difficult"}
_VALID_TIERS = {"primary", "supplementary"}
_LEGACY_PREDICTIVE_BASES = {
    "nested_sampling", "parametric_bootstrap", "laplace_hesse",
}


class ConfigError(ValueError):
    """The reviewed population or invocation is unsafe or malformed."""


class NoReadableCampaignError(RuntimeError):
    """No campaign survived the mandatory session preflight."""


class OutputError(RuntimeError):
    """The complete output bundle could not be published."""


class InvariantError(RuntimeError):
    """A tool-internal invariant was violated."""


@dataclass(frozen=True)
class CampaignSpec:
    campaign_id: str
    cohort: str
    evidence_tier: str
    root: Path
    root_relative: str
    lineage: str
    summary: str | None
    mapping_kind: str | None
    note: str | None


@dataclass(frozen=True)
class CampaignSet:
    campaigns: tuple[CampaignSpec, ...]
    source: Path


@dataclass
class CensusResult:
    campaign_set: CampaignSet
    rows: list[dict[str, Any]]
    warnings: list[dict[str, str]]
    input_hashes: dict[str, str]
    elapsed_seconds: float


def _canonical_json(value: Any) -> str:
    return json.dumps(_sanitize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def _sanitize(value: Any) -> Any:
    """Make output JSON finite and deliberately exclude accidental absolute paths."""
    if isinstance(value, Mapping):
        return {str(key): _sanitize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Path):
        return value.name
    if isinstance(value, str) and (Path(value).is_absolute() or value.startswith("/")
                                   or ":\\" in value or "/Users/" in value):
        return "<redacted-absolute-path>"
    return value


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _is_finite(value: Any) -> bool:
    try:
        return not isinstance(value, bool) and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _error_code(error: BaseException) -> str:
    return str(getattr(error, "reason_code", None) or type(error).__name__)


def load_campaign_set(path: Path | str, repo_root: Path | str) -> CampaignSet:
    """Load the explicit, repository-relative campaign population."""
    root = Path(repo_root).resolve()
    source = Path(path)
    if not source.is_absolute():
        source = root / source
    try:
        raw = tomllib.loads(source.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"cannot read campaign set {source.name}: {_error_code(error)}") from error
    entries = raw.get("campaign")
    if not isinstance(entries, list) or not entries:
        raise ConfigError("campaign set must contain one or more [[campaign]] entries")
    campaigns: list[CampaignSpec] = []
    seen: set[str] = set()
    for item in entries:
        if not isinstance(item, Mapping):
            raise ConfigError("campaign entry must be a TOML table")
        campaign_id = item.get("id")
        cohort = item.get("cohort")
        tier = item.get("evidence_tier")
        rel = item.get("root")
        if (not isinstance(campaign_id, str) or not campaign_id
                or campaign_id in seen or "/" in campaign_id or "\\" in campaign_id):
            raise ConfigError("campaign id must be a unique, path-free string")
        if cohort not in _VALID_COHORTS or tier not in _VALID_TIERS:
            raise ConfigError(f"campaign {campaign_id!r} has invalid cohort or evidence tier")
        if not isinstance(rel, str) or not rel or Path(rel).is_absolute():
            raise ConfigError(f"campaign {campaign_id!r} root must be a relative path")
        candidate = (root / rel).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise ConfigError(f"campaign {campaign_id!r} root escapes repository") from error
        seen.add(campaign_id)
        campaigns.append(CampaignSpec(
            campaign_id=campaign_id, cohort=cohort, evidence_tier=tier, root=candidate,
            root_relative=Path(rel).as_posix(), lineage=str(item.get("lineage") or campaign_id),
            summary=(str(item["summary"]) if item.get("summary") else None),
            mapping_kind=(str(item["mapping_kind"]) if item.get("mapping_kind") else None),
            note=(str(item["note"]) if item.get("note") else None),
        ))
    return CampaignSet(tuple(campaigns), source.resolve())


def _preflight(spec: CampaignSpec) -> tuple[Path | None, list[str]]:
    findings: list[str] = []
    session_path = spec.root / "session.json"
    if not session_path.is_file():
        return None, ["session_missing"]
    try:
        _read_json(session_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        _LOG.warning("campaign %s has unreadable session: %s", spec.campaign_id, error)
        return None, ["session_unreadable"]
    for filename, missing, unreadable in (
        ("card.state.json", "card_state_missing", "card_state_unreadable"),
        ("card.policy.toml", "card_policy_missing", "card_policy_unreadable"),
    ):
        path = spec.root / filename
        if not path.is_file():
            findings.append(missing)
            continue
        try:
            path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            _LOG.warning("campaign %s has unreadable %s: %s", spec.campaign_id, filename, error)
            findings.append(unreadable)
    return session_path, findings


def _resolve_reference(root: Path, ref: Any, directory: str) -> tuple[Path | None, str | None]:
    if not isinstance(ref, str) or not ref:
        return None, "reference_missing"
    expected = (root / directory).resolve()
    path = (root / ref).resolve()
    try:
        path.relative_to(expected)
    except ValueError:
        return None, "reference_escaped"
    if not path.is_file():
        return None, "reference_unreadable"
    return path, None


def _strict_block(record: Any, validator, *, expected_schema: str | None = None) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        return {"status": "missing", "reason_code": "record_missing"}
    if expected_schema and record.get("schema") != expected_schema:
        return {"status": "unsupported", "reason_code": "schema_unsupported"}
    try:
        validator(record)
    except (PredictiveArtifactError, SemanticsConstructionError, TypeError, ValueError,
            KeyError, OverflowError) as error:
        return {"status": "failed", "reason_code": _error_code(error)}
    return {"status": "passed", "reason_code": None}


def _predictive_details(root: Path, ref: Any, cache: dict[tuple[str, str], dict]) -> dict[str, Any]:
    path, problem = _resolve_reference(root, ref, "data/master_predictive")
    if problem:
        return {"strict_validation": {"status": "missing", "reason_code": problem},
                "findings": [f"predictive_{problem}"], "masses": _empty_masses()}
    key = (str(root), str(path))
    if key in cache:
        # The cache is artifact-derived truth only.  Callers receive a private
        # view because projection/QoI evidence is specific to one master row.
        return copy.deepcopy(cache[key])
    try:
        record = _read_json(path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        result = {"strict_validation": {"status": "failed", "reason_code": _error_code(error)},
                  "findings": ["predictive_unreadable"], "masses": _empty_masses()}
        cache[key] = copy.deepcopy(result)
        return copy.deepcopy(result)
    schema = record.get("schema") if isinstance(record, Mapping) else None
    strict = _strict_block(record, validate_master_predictive_artifact,
                           expected_schema=PREDICTIVE_ARTIFACT_SCHEMA)
    findings: list[str] = []
    if schema == "isar.master_predictive/1":
        strict = {"status": "unsupported", "reason_code": "legacy_unauthenticated"}
        findings.append("predictive_legacy_unauthenticated")
    elif strict["status"] != "passed":
        findings.append(f"predictive_{strict['reason_code']}")
    models = record.get("models") if isinstance(record, Mapping) else []
    result = {
        "reference": _safe_relative(path, root), "sha256": _sha256_file(path),
        "strict_validation": strict, "findings": findings,
        "predictive_basis_fingerprint": record.get("predictive_basis_fingerprint") if isinstance(record, Mapping) else None,
        "completeness": record.get("completeness") if isinstance(record, Mapping) else None,
        "exposure_basis": record.get("exposure_basis") if isinstance(record, Mapping) else None,
        "models": _model_diagnostics(models),
        "dropped_models": _dropped_model_diagnostics(record),
        "masses": _masses(models, record),
        "legacy_unauthenticated": (
            _legacy_predictive_diagnostics(models)
            if schema == "isar.master_predictive/1" else None),
        "_record": record,
    }
    cache[key] = copy.deepcopy(result)
    return copy.deepcopy(result)


def _empty_masses() -> dict[str, float]:
    return {name: 0.0 for name in (
        "observed_bootstrap_source_mass", "observed_nested_source_mass",
        "observed_laplace_source_mass", "basis_unknown_or_pruned_source_mass",
        "dropped_draw_mass", "draw_mass_unknown_source_mass",
        "qoi_unavailable_source_mass", "target_absent_source_mass",
    )}


def _model_diagnostics(models: Any) -> list[dict[str, Any]]:
    if not isinstance(models, list):
        return []
    result = []
    for model in models:
        if not isinstance(model, Mapping):
            continue
        result.append({key: model.get(key) for key in (
            "label", "identity_label", "source_weight", "weight", "draw_basis",
            "draw_weighting", "source_draw_count", "retained_draw_count", "dropped_draws",
            "draw_mass_retained",
        )})
    return result


def _dropped_model_diagnostics(record: Any) -> list[dict[str, Any]]:
    completeness = record.get("completeness") if isinstance(record, Mapping) else None
    dropped = completeness.get("dropped_models") if isinstance(completeness, Mapping) else None
    if not isinstance(dropped, list):
        return []
    return [
        {"label": item.get("label"), "original_weight": item.get("original_weight"),
         "reason": item.get("reason")}
        for item in dropped if isinstance(item, Mapping)
    ]


def _legacy_predictive_diagnostics(models: Any) -> dict[str, Any]:
    """Describe `/1` model weights without authenticating or upconverting them."""
    distribution = {basis: 0.0 for basis in sorted(_LEGACY_PREDICTIVE_BASES)}
    distribution["legacy_basis_unknown"] = 0.0
    diagnostics: list[dict[str, Any]] = []
    raw_total = 0.0
    unreadable = 0
    if not isinstance(models, list):
        models = []
    for model in models:
        if not isinstance(model, Mapping):
            unreadable += 1
            continue
        basis = model.get("basis")
        weight = model.get("weight")
        raw_weight = float(weight) if _is_finite(weight) and float(weight) >= 0.0 else None
        diagnostics.append({
            "label": model.get("label"),
            "basis": basis if isinstance(basis, str) else None,
            "raw_legacy_weight": raw_weight,
        })
        if raw_weight is None:
            unreadable += 1
            continue
        raw_total += raw_weight
        bucket = basis if basis in _LEGACY_PREDICTIVE_BASES else "legacy_basis_unknown"
        distribution[bucket] += raw_weight
    return {
        "authority": "legacy_unauthenticated",
        "models": diagnostics,
        "basis_weight_distribution": {
            key: round(value, 12) for key, value in distribution.items()
        },
        "raw_legacy_weight_total": round(raw_total, 12),
        "unreadable_weight_model_count": unreadable,
    }


def _masses(models: Any, record: Any) -> dict[str, float]:
    masses = _empty_masses()
    if not isinstance(models, list):
        return masses
    for model in models:
        if not isinstance(model, Mapping):
            continue
        weight = model.get("source_weight")
        if not _is_finite(weight) or float(weight) < 0:
            continue
        weight = float(weight)
        basis = model.get("draw_basis")
        key = {"parametric_bootstrap": "observed_bootstrap_source_mass",
               "nested": "observed_nested_source_mass", "laplace": "observed_laplace_source_mass"}.get(basis)
        if key:
            masses[key] += weight
        else:
            masses["basis_unknown_or_pruned_source_mass"] += weight
        retained_mass = model.get("draw_mass_retained")
        if _is_finite(retained_mass) and 0.0 <= float(retained_mass) <= 1.0:
            masses["dropped_draw_mass"] += weight * (1.0 - float(retained_mass))
        else:
            # Counts cannot recover lost probability mass: deterministic cap
            # thinning can reduce draw count while retaining the full measure.
            masses["draw_mass_unknown_source_mass"] += weight
    completeness = record.get("completeness") if isinstance(record, Mapping) else None
    if isinstance(completeness, Mapping):
        for dropped in completeness.get("dropped_models") or []:
            if isinstance(dropped, Mapping) and _is_finite(dropped.get("original_weight")):
                masses["basis_unknown_or_pruned_source_mass"] += float(dropped["original_weight"])
    return {key: round(value, 12) for key, value in masses.items()}


def _projection_raw(root: Path, ref: Any, cache: dict[tuple[str, str], dict]) -> dict[str, Any]:
    """Cache only payload-level checks; joins and authority are master-specific."""
    path, problem = _resolve_reference(root, ref, "data/target_projection")
    if problem:
        return {"strict_validation": {"status": "missing", "reason_code": problem},
                "findings": [f"projection_{problem}"], "record": None}
    key = (str(root), str(path))
    if key in cache:
        return copy.deepcopy(cache[key])
    try:
        record = _read_json(path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return {"strict_validation": {"status": "failed", "reason_code": _error_code(error)},
                "findings": ["projection_unreadable"], "record": None}
    strict = {"status": "passed", "reason_code": None}
    findings: list[str] = []
    if not isinstance(record, Mapping) or record.get("schema") != TARGET_PROJECTION_SCHEMA:
        strict = {"status": "unsupported", "reason_code": "schema_unsupported"}
        findings.append("projection_schema_unsupported")
    elif not isinstance(record.get("authority"), Mapping):
        strict = {"status": "failed", "reason_code": "authority_missing_or_malformed"}
        findings.append("projection_authority_missing_or_malformed")
    elif not isinstance(record.get("models"), list):
        strict = {"status": "failed", "reason_code": "models_not_list"}
        findings.append("projection_models_not_list")
    else:
        # Match Workspace.target_projection exactly: semantics are additive and
        # therefore optional; when present, authenticate against the validated
        # normalized record rather than the untrusted input mapping.
        semantics = record.get("statistical_semantics")
        if semantics is not None:
            try:
                validated = validate_predictive_semantics(semantics)
            except (SemanticsConstructionError, TypeError, ValueError, OverflowError) as error:
                strict = {"status": "failed", "reason_code": _error_code(error)}
                findings.append(f"projection_{strict['reason_code']}")
            else:
                if any(record.get(name) != validated.get(name) for name in (
                        "statistical_semantics_fingerprint",
                        "source_predictive_semantics_fingerprint",
                        "target_projection_set_fingerprint")):
                    strict = {"status": "failed",
                              "reason_code": "projection_semantics_payload_mismatch"}
                    findings.append("projection_semantics_payload_mismatch")
    result = {"reference": _safe_relative(path, root), "sha256": _sha256_file(path),
              "strict_validation": strict, "findings": sorted(set(findings)),
              "complete": record.get("complete") if isinstance(record, Mapping) else None,
              "reason_code": record.get("reason") if isinstance(record, Mapping) else None,
              "record": record}
    cache[key] = copy.deepcopy(result)
    return copy.deepcopy(result)


def _projection_details(raw: Mapping[str, Any], predictive: Mapping[str, Any], outcome: Mapping[str, Any],
                        entry: Any, policy_fingerprint: str | None) -> dict[str, Any]:
    record = raw.get("record")
    result = {key: value for key, value in raw.items() if key != "record"}
    result["joins"] = _projection_joins(record, predictive)
    result["authority_fields"] = _projection_currentness(record, outcome, entry, policy_fingerprint)
    statuses = list(result["authority_fields"].values())
    result["currentness"] = ("stale" if "fail" in statuses else
                             "current" if statuses and all(item == "pass" for item in statuses) else "unknown")
    result["completeness_status"] = (
        "complete" if result.get("complete") is True
        else "projection_incomplete" if result.get("complete") is False
        else "unknown")
    result["particle_masses"] = _projection_particle_masses(record, predictive)
    result["findings"] = sorted(set(result.get("findings", []) +
                                     [f"projection_{key}_mismatch" for key, value in result["joins"].items() if value == "fail"] +
                                     (["projection_incomplete"] if result.get("complete") is False else []) +
                                     (["projection_stale"] if result["currentness"] == "stale" else [])))
    return result


def _projection_joins(record: Any, predictive: Mapping[str, Any]) -> dict[str, str]:
    joins = {key: "unknown" for key in ("predictive_basis", "retained_set", "semantics", "projection_set", "models", "draw_alignment")}
    if not isinstance(record, Mapping):
        return joins
    precord = predictive.get("_record") if isinstance(predictive, Mapping) else None
    for key, pkey, rkey in (("predictive_basis", "predictive_basis_fingerprint", "predictive_basis_fingerprint"),
                            ("retained_set", "predictive_retained_set_fingerprint", "source_predictive_retained_set_fingerprint"),
                            ("semantics", "statistical_semantics_fingerprint", "source_predictive_semantics_fingerprint")):
        expected = precord.get(pkey) if isinstance(precord, Mapping) else None
        actual = record.get(rkey)
        if isinstance(expected, str) and isinstance(actual, str):
            joins[key] = "pass" if expected == actual else "fail"
    try:
        expected_set = semantics_fingerprint("isar.target_projection_model_set/1", sorted([
            {"label": item.get("label"), "weight": item.get("weight"),
             "draw_count": len(item.get("target_present_by_draw") or ())}
            for item in record.get("models") or []], key=lambda item: str(item["label"])))
        joins["projection_set"] = "pass" if record.get("target_projection_set_fingerprint") == expected_set else "fail"
    except (SemanticsConstructionError, TypeError, ValueError):
        joins["projection_set"] = "fail"
    pmodels = precord.get("models") if isinstance(precord, Mapping) else []
    rmodels = record.get("models") or []
    if isinstance(pmodels, list) and isinstance(rmodels, list):
        if len(pmodels) != len(rmodels):
            joins["models"] = "fail"
        else:
            joins["models"] = "pass"
            aligned = "pass"
            for left, right in zip(pmodels, rmodels):
                if (not isinstance(right, Mapping) or left.get("label") != right.get("label")
                        or left.get("identity_label") != right.get("identity_label")
                        or left.get("weight") != right.get("weight")):
                    joins["models"] = "fail"
                qoi = right.get("target_qoi_draws")
                target = right.get("target_present_by_draw")
                count = left.get("retained_draw_count")
                valid_pairs = (isinstance(qoi, list) and isinstance(target, list)
                               and all(isinstance(value, bool) for value in target)
                               and all((not present and value is None) or (present and (_is_finite(value) or value is None))
                                       for present, value in zip(target, qoi)))
                if isinstance(count, int) and (not valid_pairs or len(qoi) != count or len(target) != count):
                    aligned = "fail"
            joins["draw_alignment"] = aligned
    return joins


def _projection_currentness(record: Any, outcome: Mapping[str, Any], entry: Any,
                            policy_fingerprint: str | None) -> dict[str, str]:
    if not isinstance(record, Mapping) or not isinstance(record.get("authority"), Mapping):
        return {name: "unknown" for name in ("action_role", "master_revision", "track_revision", "track_state", "candidate_id", "candidate_state", "selected_candidate_index", "calibration_fingerprint", "policy_fingerprint")}
    authority = record["authority"]
    source = entry.provenance.get("target_projection_authority", {}) if entry is not None else {}
    if not isinstance(source, Mapping):
        source = {}
    expected = {
        "action_role": source.get("action_role"),
        "master_revision": source.get("master_revision", outcome.get("master_revision")),
        "track_revision": outcome.get("track_revision"),
        "track_state": outcome.get("track_state"),
        "candidate_id": outcome.get("candidate_id"),
        "candidate_state": outcome.get("candidate_state"),
        "selected_candidate_index": outcome.get("selected_candidate_index"),
        "calibration_fingerprint": source.get("calibration_fingerprint"),
        "policy_fingerprint": policy_fingerprint,
    }
    return {key: ("unknown" if value is None else "pass" if authority.get(key) == value else "fail")
            for key, value in expected.items()}


def _projection_particle_masses(record: Any, predictive: Mapping[str, Any]) -> dict[str, float]:
    masses = {"qoi_unavailable_source_mass": 0.0, "target_absent_source_mass": 0.0}
    precord = predictive.get("_record") if isinstance(predictive, Mapping) else None
    if not isinstance(record, Mapping) or not isinstance(precord, Mapping):
        return masses
    for model, projection in zip(precord.get("models") or [], record.get("models") or []):
        if not isinstance(model, Mapping) or not isinstance(projection, Mapping) or not _is_finite(model.get("source_weight")):
            continue
        weight = float(model["source_weight"])
        target, qoi = projection.get("target_present_by_draw"), projection.get("target_qoi_draws")
        if not isinstance(target, list) or not isinstance(qoi, list) or not target:
            masses["qoi_unavailable_source_mass"] += weight
            continue
        for present, value in zip(target, qoi):
            if not present:
                masses["target_absent_source_mass"] += weight / len(target)
            elif not _is_finite(value):
                masses["qoi_unavailable_source_mass"] += weight / len(target)
    return {key: round(value, 12) for key, value in masses.items()}


def _semantics_details(record: Any, validator) -> dict[str, Any]:
    return {"strict_validation": _strict_block(record, validator)}


def _shadow_category(record: Any, strict: Mapping[str, Any]) -> str:
    if strict["status"] == "missing":
        return "missing_legacy"
    if strict["status"] == "unsupported":
        return "unsupported_schema"
    if strict["status"] == "failed":
        return "malformed"
    status = record.get("selection_status")
    if status == "complete":
        return "complete_disagrees" if record.get("selection_changed") else "complete_agrees"
    if status == "degenerate_single_candidate":
        return "complete_meaning_differs"
    reason = str(record.get("unavailable_reason_code") or "other")
    if "deadline" in reason:
        return "soft_deadline_skip"
    stage = str(record.get("unavailable_stage") or "other")
    return f"failure:{stage if stage in {'construction', 'candidate_fit', 'identity_projection', 'comparison'} else 'other'}"


def _member_triple(value: Any) -> tuple[str, str, str] | None:
    if isinstance(value, (list, tuple)) and len(value) == 3:
        return tuple(str(item) for item in value)
    if isinstance(value, str) and value.count(":") >= 2:
        source, scan, detector = value.rsplit(":", 2)
        if source and scan and detector:
            return source, scan, detector
    return None


def _physical_member_index(session: SessionState) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    index: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for key, candidate in session.scans.items():
        if key.startswith("master:"):
            continue
        for detector, detector_outcome in candidate.detectors.items():
            identity = _member_triple(candidate.scan_id)
            if identity is None or identity[2] != str(detector):
                identity = _member_triple(key)
            if identity is None or identity[2] != str(detector):
                # A scan-level ledger ID is older but still resolvable when it
                # carries a stable source prefix and one detector payload.
                marker = f":{candidate.scan_number}"
                scan_id = str(candidate.scan_id)
                source = scan_id[:-len(marker)] if scan_id.endswith(marker) else None
                if source:
                    identity = source, str(candidate.scan_number), str(detector)
            if identity is None:
                continue
            acquisition_id = (detector_outcome.get("acquisition_id")
                              if isinstance(detector_outcome, Mapping) else None)
            index.setdefault(identity, []).append({
                "session_key": key,
                "detector_id": str(detector),
                "acquisition_id": str(acquisition_id) if acquisition_id is not None else None,
            })
    return index


def _resolve_outcome_member(value: Any, declared: list[tuple[str, str, str]],
                            physical: Mapping[tuple[str, str, str], list[dict[str, Any]]]
                            ) -> tuple[str, str, str] | None:
    direct = _member_triple(value)
    if direct is not None:
        return direct
    token = str(value)
    candidates = {item for item in declared if item[1] == token}
    if len(candidates) == 1:
        return next(iter(candidates))
    candidates = {item for item in physical if item[1] == token}
    return next(iter(candidates)) if len(candidates) == 1 else None


def _member_join(session: SessionState, entry: Any, outcome: Mapping[str, Any],
                 card_state: CardState | None) -> dict[str, Any]:
    declared_raw = entry.provenance.get("members") if isinstance(entry.provenance, Mapping) else None
    outcome_raw = outcome.get("members")
    result: dict[str, Any] = {
        "status": "unknown", "reason_code": "member_provenance_missing",
        "declared_members": [], "outcome_members": [], "acquisition_ids": [],
        "acquisition_roles": [], "acquisition_provenance_status": "unknown",
        "scan_design_inventory_refs": [],
    }
    if not isinstance(declared_raw, list) or not isinstance(outcome_raw, list):
        return result
    declared = [_member_triple(item) for item in declared_raw]
    if any(item is None for item in declared):
        result["reason_code"] = "declared_member_identity_unrecognized"
        return result
    normalized_declared = [item for item in declared if item is not None]
    physical = _physical_member_index(session)
    normalized_outcome = [
        _resolve_outcome_member(item, normalized_declared, physical) for item in outcome_raw
    ]
    result["declared_members"] = [list(item) for item in normalized_declared]
    result["outcome_members"] = [list(item) if item is not None else None
                                 for item in normalized_outcome]
    if any(item is None for item in normalized_outcome):
        result["reason_code"] = "outcome_member_identity_ambiguous"
        return result
    if Counter(normalized_declared) != Counter(normalized_outcome):
        result.update(status="fail", reason_code="logical_member_mismatch")
        return result
    missing = [item for item in normalized_declared if item not in physical]
    ambiguous = [item for item in normalized_declared if len(physical.get(item, ())) != 1]
    if missing:
        result.update(status="fail", reason_code="physical_member_missing")
        return result
    if ambiguous:
        result["reason_code"] = "physical_member_ambiguous"
        return result
    result.update(status="pass", reason_code=None)
    acquisition_ids = [physical[item][0].get("acquisition_id") for item in normalized_declared]
    acquisition_ids = [item for item in acquisition_ids if isinstance(item, str)]
    result["acquisition_ids"] = sorted(set(acquisition_ids))
    if card_state is None:
        result["acquisition_provenance_status"] = "card_state_unavailable"
        return result
    provenance = getattr(card_state, "acquisition_provenance", None)
    if not isinstance(provenance, list):
        result["acquisition_provenance_status"] = "card_state_provenance_malformed"
        return result
    by_id: dict[str, list[Mapping[str, Any]]] = {}
    for item in provenance:
        if isinstance(item, Mapping) and item.get("acquisition_id") is not None:
            by_id.setdefault(str(item["acquisition_id"]), []).append(item)
    if not acquisition_ids:
        result["acquisition_provenance_status"] = "physical_acquisition_ids_missing"
        return result
    matched: list[Mapping[str, Any]] = []
    for acquisition_id in acquisition_ids:
        records = by_id.get(acquisition_id, [])
        if len(records) != 1:
            result["acquisition_provenance_status"] = (
                "acquisition_mapping_missing" if not records else "acquisition_mapping_ambiguous")
            return result
        matched.append(records[0])
    result["acquisition_provenance_status"] = "pass"
    result["acquisition_roles"] = sorted({
        str(item["acquisition_action_role"]) for item in matched
        if isinstance(item.get("acquisition_action_role"), str)
    })
    refs = [item.get("scan_design_inventory_ref") for item in matched
            if isinstance(item.get("scan_design_inventory_ref"), Mapping)]
    result["scan_design_inventory_refs"] = refs
    return result


def _acquisition_intent(member_join: Mapping[str, Any], entry: Any,
                        outcome: Mapping[str, Any]) -> str:
    if member_join.get("status") != "pass" or member_join.get("acquisition_provenance_status") != "pass":
        return "unknown"
    roles = set(member_join.get("acquisition_roles") or [])
    identities = set(member_join.get("acquisition_ids") or [])
    if not roles:
        return "unknown"
    if roles == {"production"}:
        if outcome.get("evidence_sink") != "canonical_master":
            return "noncanonical"
        transition = entry.provenance.get("confirmation_transition") if isinstance(entry.provenance, Mapping) else None
        if (isinstance(transition, Mapping)
                and str(transition.get("acquisition_id")) in identities
                and transition.get("confirmed") is True):
            return "confirmation"
        # A production target must carry an explicit branch/candidate identity, not merely a role.
        if outcome.get("candidate_id") is not None or outcome.get("identity_branch_id") is not None:
            return "production"
        return "unknown"
    if "classification" in roles or "discovery" in roles:
        return "noncanonical"
    return "mixed"


def _selection_science_findings(record: Any, strict: Mapping[str, Any]) -> list[str]:
    if strict["status"] != "passed":
        return []
    findings = []
    if not isinstance(record, Mapping):
        return findings
    if record.get("model_weight_kind") != "akaike_relative_weight":
        findings.append("selection_not_aggregate_akaike")
    if record.get("selection_eligibility") != "eligible":
        findings.append("selection_ineligible")
    if record.get("estimator_kind") not in (None, "data_likelihood_mle"):
        findings.append("selection_estimator_not_data_likelihood_mle")
    route = record.get("candidate_generation_route")
    if isinstance(route, Mapping) and route.get("route") not in (None, "full_two_stage"):
        findings.append("selection_not_full_two_stage")
    if record.get("effective_constraints"):
        findings.append("selection_effective_constraints_or_rescue")
    reasons = record.get("selection_ineligibility_reasons") or []
    if any("rescue" in str(reason) or "post_selection" in str(reason) for reason in reasons):
        findings.append("selection_rescued_or_postselection")
    if isinstance(route, Mapping) and route.get("multimode_route") == "guideline_forced":
        findings.append("selection_guideline_altered_model_set")
    if isinstance(route, Mapping):
        offered, scored = route.get("offered_labels"), route.get("scored_labels")
        if isinstance(offered, list) and isinstance(scored, list) and set(offered) != set(scored):
            findings.append("selection_model_set_incomplete")
    return findings


def _scan_design_details(root: Path, ref: Any, *, acquisition_ids: tuple[str, ...] = ()) -> dict[str, Any]:
    """Validate an inventory envelope without constructing or ranking any candidates."""
    if ref is None:
        return {"status": "missing", "reason_code": "reference_missing"}
    if not isinstance(ref, Mapping):
        return {"status": "failed", "reason_code": "reference_malformed"}
    if ref.get("kind") != "scan_design_inventory":
        return {"status": "failed", "reason_code": "reference_kind_invalid"}
    digest = ref.get("sha256")
    rel = ref.get("path")
    if (not isinstance(digest, str) or len(digest) != 64 or digest.lower() != digest
            or not isinstance(rel, str)):
        return {"status": "failed", "reason_code": "reference_malformed"}
    path, problem = _resolve_reference(root, rel, "report/scan-design")
    if problem:
        return {"status": "failed", "reason_code": problem}
    try:
        record = _read_json(path)
        from isar.drive.acquisition.inventory import canonical_json
        actual = _sha256_bytes(canonical_json(record).encode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        return {"status": "failed", "reason_code": _error_code(error)}
    if actual != digest or path.name != f"{digest}.json":
        return {"status": "failed", "reason_code": "content_address_mismatch"}
    if not isinstance(record, Mapping) or record.get("schema") != "isar.scan_design_inventory/2":
        return {"status": "failed", "reason_code": "schema_unsupported"}
    selected = record.get("selected_candidate_id")
    candidates = record.get("candidates") or []
    candidate = next((item for item in candidates if isinstance(item, Mapping)
                      and item.get("candidate_id") == selected), None)
    if candidate is None or candidate.get("schema") != "isar.scan_design_candidate/2":
        return {"status": "failed", "reason_code": "selected_candidate_invalid"}
    events: list[str] = []
    log_path = root / "report" / "session_log.jsonl"
    if log_path.is_file():
        try:
            records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
                       if line.strip()]
            related_acquisitions = set(acquisition_ids)
            for event in records:
                payload = event.get("payload") if isinstance(event, Mapping) else None
                if (isinstance(event, Mapping) and event.get("kind") == "physical_submission"
                        and isinstance(payload, Mapping)
                        and payload.get("scan_design_inventory_ref") == ref
                        and payload.get("acquisition_id") is not None):
                    related_acquisitions.add(str(payload["acquisition_id"]))
            for event in records:
                if not isinstance(event, Mapping):
                    continue
                kind = event.get("kind")
                payload = event.get("payload")
                if not isinstance(payload, Mapping):
                    continue
                if (kind == "scan_design_realization"
                        and payload.get("schema") == "isar.scan_design_realization/1"
                        and payload.get("inventory_ref") == ref
                        and payload.get("candidate_id") == selected):
                    adjustment = payload.get("adjustment")
                    if isinstance(adjustment, str):
                        events.append(f"scan_design_realization:{adjustment}")
                if (kind in {"recovery_pending", "recovery_completed"}
                        and payload.get("acquisition_id") is not None
                        and str(payload["acquisition_id"]) in related_acquisitions):
                    events.append(str(kind))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            _LOG.warning("scan-design event diagnostics unavailable: %s", error)
    return {"status": "passed", "reason_code": None, "reference": _safe_relative(path, root),
            "sha256": digest, "selected_candidate_id": selected,
            "window_family": candidate.get("window_family"), "realization_events": sorted(set(events))}


def _classify(row: dict[str, Any]) -> None:
    findings = row["findings"]
    science: list[str] = []
    gaps: list[str] = []
    outcome = row["outcome"]
    if not _is_finite(outcome.get("omega0")):
        science.append("target_not_finite")
    track = outcome.get("track_state")
    if track is None:
        gaps.append("track_state_unknown")
    elif track != "confirmed":
        science.append("track_not_confirmed")
    if outcome.get("pool_consistent") is False:
        science.append("pool_inconsistent")
    member_join = row["member_join"]
    if member_join["status"] == "fail":
        science.append("member_provenance_inconsistent")
    elif member_join["status"] == "unknown":
        gaps.append(f"member_{member_join['reason_code']}")
    if member_join.get("acquisition_provenance_status") not in {"pass", None}:
        gaps.append(f"acquisition_{member_join['acquisition_provenance_status']}")
    intent = row["acquisition_intent"]
    if intent == "unknown":
        gaps.append("action_intent_unknown")
    elif intent not in {"production", "confirmation"}:
        science.append("action_not_canonical_production")
    for name, detail in (("selection", row["selection"]), ("predictive", row["predictive"]),
                         ("projection", row["projection"]), ("shadow", row["shadow"])):
        strict = detail["strict_validation"]
        if strict["status"] != "passed":
            gaps.append(f"{name}_{strict['reason_code']}")
    scan_design = row["scan_design"]
    if scan_design["status"] != "passed":
        gaps.append(f"scan_design_{scan_design['reason_code']}")
    if any("recovery" in event or "missed" in event for event in scan_design.get("realization_events", [])):
        science.append("scan_design_recovery_or_missed_window")
    if row["projection"].get("currentness") == "unknown":
        gaps.append("projection_currentness_unknown")
    elif row["projection"].get("currentness") == "stale":
        science.append("projection_stale")
    for join, state in row["projection"].get("joins", {}).items():
        if state == "fail":
            science.append(f"projection_{join}_mismatch")
        elif state == "unknown":
            gaps.append(f"projection_{join}_unknown")
    if row["projection"]["strict_validation"]["status"] == "passed":
        if row["projection"].get("complete") is False:
            science.append("projection_incomplete")
        elif row["projection"].get("complete") is not True:
            gaps.append("projection_completeness_unknown")
    # This is the row-local empirical-measure view.  Predictive.masses stays
    # artifact-only and is safe to cache/share across masters.
    masses = dict(row["predictive"].get("masses", {}))
    for name, value in row["projection"].get("particle_masses", {}).items():
        masses[name] = round(float(masses.get(name, 0.0)) + float(value), 12)
    row["particle_masses"] = masses
    nonbootstrap = float(masses.get("observed_nested_source_mass", 0.0)) + float(masses.get("observed_laplace_source_mass", 0.0))
    if nonbootstrap >= PREDICTIVE_MODEL_WEIGHT_FLOOR:
        science.append("material_nonbootstrap_mass")
    if float(masses.get("basis_unknown_or_pruned_source_mass", 0.0)) >= PREDICTIVE_MODEL_WEIGHT_FLOOR:
        gaps.append("material_particle_basis_unknown_or_pruned")
    if float(masses.get("draw_mass_unknown_source_mass", 0.0)) >= PREDICTIVE_MODEL_WEIGHT_FLOOR:
        gaps.append("material_draw_mass_unknown")
    if float(masses.get("qoi_unavailable_source_mass", 0.0)) >= PREDICTIVE_MODEL_WEIGHT_FLOOR:
        science.append("material_qoi_unavailable_mass")
    if row["shadow"]["category"] in {"complete_disagrees", "complete_meaning_differs"} or row["shadow"]["category"].startswith("failure:"):
        science.append(f"shadow_{row['shadow']['category']}")
    science.extend(_selection_science_findings(row["selection_record"], row["selection"]["strict_validation"]))
    findings.extend(science + gaps)
    row["findings"] = sorted(set(findings))
    row["primary_scientific_exclusion"] = science[0] if science else None
    row["primary_observability_gap"] = gaps[0] if gaps else None
    row["current_contract_observability"] = "complete" if not gaps else "incomplete"
    row["provisional_eligibility"] = ("ineligible" if science else
                                        "eligible" if not gaps else "unassessable")


def _outcome_view(outcome: Mapping[str, Any]) -> dict[str, Any]:
    """The audit-facing subset; raw outcomes can carry unrelated local provenance."""
    return {name: outcome.get(name) for name in (
        "omega0", "pooled", "n_members", "members", "target_delivery_eligible",
        "track_state", "track_revision", "candidate_id", "candidate_state",
        "selected_candidate_index", "master_revision", "pool_consistent", "fit_kind",
        "evidence_sink", "predictive_ref", "target_projection_ref", "acquisition_id",
        "identity_branch_id", "scan_design_inventory_ref",
    )}


def _master_rows(spec: CampaignSpec, session: SessionState, preflight_findings: list[str],
                 predictive_cache: dict, projection_cache: dict, policy_fingerprint: str | None,
                 card_state: CardState | None, only_key: str | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, entry in sorted(session.scans.items()):
        if not key.startswith("master:"):
            continue
        if only_key is not None and key != only_key:
            continue
        pooled = [(str(detector), value) for detector, value in sorted(entry.detectors.items())
                  if isinstance(value, Mapping) and value.get("pooled") is True]
        if not pooled:
            rows.append({"row_kind": "master_entry_without_pooled_outcome", "campaign_id": spec.campaign_id,
                         "cohort": spec.cohort, "evidence_tier": spec.evidence_tier, "lineage": spec.lineage,
                         "master_session_key": key, "detector_id": None,
                         "findings": sorted(set(preflight_findings + ["master_outcome_missing"]))})
            continue
        detector_ids = [detector for detector, _ in pooled]
        if len(detector_ids) != len(set(detector_ids)):
            rows.append({"row_kind": "ambiguous_master_join", "campaign_id": spec.campaign_id,
                         "cohort": spec.cohort, "evidence_tier": spec.evidence_tier, "lineage": spec.lineage,
                         "master_session_key": key, "detector_id": None,
                         "findings": sorted(set(preflight_findings + ["duplicate_pooled_detector"]))})
            continue
        for detector, outcome in pooled:
            member_join = _member_join(session, entry, outcome, card_state)
            predictive = _predictive_details(spec.root, outcome.get("predictive_ref"), predictive_cache)
            projection = _projection_details(_projection_raw(spec.root, outcome.get("target_projection_ref"), projection_cache),
                                             predictive, outcome, entry, policy_fingerprint)
            selection_record = outcome.get("fit_selection_semantics")
            selection = _semantics_details(selection_record, validate_fit_selection_semantics)
            shadow_record = outcome.get("shadow_model_selection")
            shadow_strict = _strict_block(shadow_record, validate_shadow_model_selection)
            scan_design_ref = (outcome.get("scan_design_inventory_ref")
                               or entry.provenance.get("scan_design_inventory_ref"))
            if scan_design_ref is None:
                refs = member_join.get("scan_design_inventory_refs") or []
                unique_refs = {_canonical_json(item): item for item in refs if isinstance(item, Mapping)}
                if len(unique_refs) == 1:
                    scan_design_ref = next(iter(unique_refs.values()))
            row = {"row_kind": "pooled_outcome", "campaign_id": spec.campaign_id,
                   "cohort": spec.cohort, "evidence_tier": spec.evidence_tier, "lineage": spec.lineage,
                   "master_session_key": key, "detector_id": detector, "outcome": _outcome_view(outcome),
                   "member_join": member_join,
                   "acquisition_intent": _acquisition_intent(member_join, entry, outcome),
                   "predictive": predictive, "projection": projection,
                   "selection": selection, "selection_record": selection_record,
                   "scan_design": _scan_design_details(
                       spec.root, scan_design_ref,
                       acquisition_ids=tuple(member_join.get("acquisition_ids") or ())),
                   "shadow": {"strict_validation": shadow_strict,
                              "category": _shadow_category(shadow_record, shadow_strict)},
                   "findings": list(preflight_findings)}
            _classify(row)
            rows.append(row)
    return rows


def analyze_campaign_set(campaign_set: CampaignSet, repo_root: Path | str) -> CensusResult:
    """Read all listed campaigns, retaining per-campaign failure as evidence."""
    del repo_root
    started = time.perf_counter()
    rows: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []
    input_hashes: dict[str, str] = {}
    readable = 0
    predictive_cache: dict = {}
    projection_cache: dict = {}
    for spec in campaign_set.campaigns:
        session_path, preflight_findings = _preflight(spec)
        if session_path is None:
            warnings.append({"campaign_id": spec.campaign_id, "reason": preflight_findings[0]})
            continue
        try:
            session = SessionState.load(session_path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            _LOG.warning("campaign %s session failed after preflight: %s", spec.campaign_id, error)
            warnings.append({"campaign_id": spec.campaign_id, "reason": "session_load_failed"})
            continue
        readable += 1
        input_hashes[f"{spec.campaign_id}:session.json"] = _sha256_file(session_path)
        for filename in ("card.state.json", "card.policy.toml"):
            path = spec.root / filename
            if path.is_file():
                input_hashes[f"{spec.campaign_id}:{filename}"] = _sha256_file(path)
        card_state: CardState | None = None
        if not any(item in preflight_findings for item in
                   ("card_state_missing", "card_state_unreadable")):
            try:
                card_state = load_state(spec.root / "card.state.json")
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError,
                    TypeError, ValueError) as error:
                _LOG.warning("campaign %s card state unavailable: %s", spec.campaign_id, error)
                preflight_findings.append("card_state_unreadable")
        policy_fingerprint = None
        try:
            policy_fingerprint = json_fingerprint(Card.load(spec.root / "card.policy.toml").policy)
        except (OSError, UnicodeDecodeError, ValueError, TypeError, KeyError, tomllib.TOMLDecodeError) as error:
            _LOG.warning("campaign %s policy fingerprint unavailable: %s", spec.campaign_id, error)
            preflight_findings.append("policy_fingerprint_unknown")
        for key, entry in sorted(session.scans.items()):
            if not key.startswith("master:"):
                continue
            try:
                rows.extend(_master_rows(spec, session, preflight_findings, predictive_cache,
                                         projection_cache, policy_fingerprint, card_state,
                                         only_key=key))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                _LOG.exception("master %s/%s census failed: %s", spec.campaign_id, key, error)
                rows.append({"row_kind": "ambiguous_master_join", "campaign_id": spec.campaign_id,
                             "cohort": spec.cohort, "evidence_tier": spec.evidence_tier,
                             "lineage": spec.lineage, "master_session_key": key, "detector_id": None,
                             "findings": sorted(set(preflight_findings + ["master_analysis_failed"]))})
    if not readable:
        raise NoReadableCampaignError("all configured roots failed session preflight")
    rows.sort(key=lambda row: (row["cohort"], row["campaign_id"], row["master_session_key"],
                               row.get("detector_id") or ""))
    return CensusResult(campaign_set=campaign_set, rows=rows, warnings=warnings,
                        input_hashes=dict(sorted(input_hashes.items())),
                        elapsed_seconds=time.perf_counter() - started)


def _summary(result: CensusResult) -> dict[str, Any]:
    pooled = [row for row in result.rows if row["row_kind"] == "pooled_outcome"]
    groups: dict[str, list[dict[str, Any]]] = {
        "primary_combined": [row for row in pooled if row["evidence_tier"] == "primary"],
        "supplementary": [row for row in pooled if row["evidence_tier"] == "supplementary"],
    }
    for cohort in _VALID_COHORTS:
        groups[f"cohort:{cohort}"] = [row for row in pooled if row["cohort"] == cohort]
        for tier in _VALID_TIERS:
            groups[f"{tier}_cohort:{cohort}"] = [
                row for row in pooled
                if row["cohort"] == cohort and row["evidence_tier"] == tier
            ]
    for spec in result.campaign_set.campaigns:
        groups[f"campaign:{spec.campaign_id}"] = [row for row in pooled if row["campaign_id"] == spec.campaign_id]
    for lineage in sorted({spec.lineage for spec in result.campaign_set.campaigns}):
        groups[f"lineage:{lineage}"] = [row for row in pooled if row["lineage"] == lineage]

    def distribution(values) -> dict[str, int]:
        return dict(sorted(Counter(str(value) for value in values if value is not None).items()))

    def legacy_particle_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
        buckets = {basis: 0.0 for basis in sorted(_LEGACY_PREDICTIVE_BASES)}
        buckets["legacy_basis_unknown"] = 0.0
        artifact_rows = 0
        raw_total = 0.0
        unreadable = 0
        for row in rows:
            legacy = row.get("predictive", {}).get("legacy_unauthenticated")
            if not isinstance(legacy, Mapping):
                continue
            artifact_rows += 1
            if _is_finite(legacy.get("raw_legacy_weight_total")):
                raw_total += float(legacy["raw_legacy_weight_total"])
            unreadable += int(legacy.get("unreadable_weight_model_count") or 0)
            values = legacy.get("basis_weight_distribution")
            if isinstance(values, Mapping):
                for basis in buckets:
                    if _is_finite(values.get(basis)):
                        buckets[basis] += float(values[basis])
        return {
            "authority": "legacy_unauthenticated",
            "artifact_rows": artifact_rows,
            "raw_legacy_weight_total": round(raw_total, 12),
            "unreadable_weight_model_count": unreadable,
            "basis_weight_distribution": {
                key: round(value, 12) for key, value in buckets.items()
            },
        }

    def counts(rows: list[dict[str, Any]]) -> dict[str, Any]:
        observable = sum(row["current_contract_observability"] == "complete" for row in rows)
        eligible = sum(row["provisional_eligibility"] == "eligible" for row in rows)
        canonical = sum(row.get("acquisition_intent") in {"production", "confirmation"}
                        and row["outcome"].get("track_state") == "confirmed" for row in rows)
        return {
            "pooled_outcomes": len(rows),
            "finite_target": sum(_is_finite(row["outcome"].get("omega0")) for row in rows),
            "tracker_confirmed": sum(row["outcome"].get("track_state") == "confirmed" for row in rows),
            "canonical_target_production_or_confirmation": canonical,
            "fully_observable": observable,
            "eligible": eligible,
            "ineligible": sum(row["provisional_eligibility"] == "ineligible" for row in rows),
            "unassessable": sum(row["provisional_eligibility"] == "unassessable" for row in rows),
            "headline_eligibility_rate": (eligible / observable if observable else None),
            "all_reason_counts": distribution(
                finding for row in rows for finding in row.get("findings", [])),
            "primary_scientific_exclusion_counts": distribution(
                row.get("primary_scientific_exclusion") for row in rows),
            "primary_observability_gap_counts": distribution(
                row.get("primary_observability_gap") for row in rows),
            "particle_source_masses": {
                name: round(sum(float(row.get("particle_masses", {}).get(name, 0.0))
                                for row in rows), 12)
                for name in _empty_masses()
            },
            "legacy_particle_description": legacy_particle_summary(rows),
            "shadow_categories": distribution(
                row.get("shadow", {}).get("category") for row in rows),
            "projection_strict_status": distribution(
                row.get("projection", {}).get("strict_validation", {}).get("status") for row in rows),
            "projection_reason_codes": distribution(
                row.get("projection", {}).get("strict_validation", {}).get("reason_code") for row in rows),
            "projection_currentness": distribution(
                row.get("projection", {}).get("currentness") for row in rows),
            "projection_completeness": distribution(
                "complete" if row.get("projection", {}).get("complete") is True
                else "incomplete" if row.get("projection", {}).get("complete") is False
                else "unknown" for row in rows),
            "scan_design_events": distribution(
                event for row in rows
                for event in row.get("scan_design", {}).get("realization_events", [])),
        }
    all_findings = Counter(finding for row in pooled for finding in row.get("findings", []))
    primary_exclusions = Counter(row.get("primary_scientific_exclusion") for row in pooled if row.get("primary_scientific_exclusion"))
    primary_gaps = Counter(row.get("primary_observability_gap") for row in pooled if row.get("primary_observability_gap"))
    shadow = Counter(row.get("shadow", {}).get("category") for row in pooled if row.get("shadow"))
    projections = Counter(row.get("projection", {}).get("currentness") for row in pooled if row.get("projection"))
    projection_completeness = Counter(
        "complete" if row.get("projection", {}).get("complete") is True
        else "incomplete" if row.get("projection", {}).get("complete") is False
        else "unknown" for row in pooled)
    particle = {name: round(sum(float(row.get("particle_masses", {}).get(name, 0.0))
                               for row in pooled), 12) for name in _empty_masses()}
    return {"schema": "isar.eig4_slice1_census_summary/1", "ruleset": RULESET,
            "groups": {name: counts(rows) for name, rows in sorted(groups.items())},
            "row_kinds": {kind: sum(row["row_kind"] == kind for row in result.rows)
                          for kind in ("pooled_outcome", "master_entry_without_pooled_outcome", "ambiguous_master_join")},
            "all_reason_counts": dict(sorted(all_findings.items())),
            "primary_scientific_exclusion_counts": dict(sorted(primary_exclusions.items())),
            "primary_observability_gap_counts": dict(sorted(primary_gaps.items())),
            "shadow_categories": dict(sorted(shadow.items())),
            "projection_currentness": dict(sorted(projections.items())),
            "projection_completeness": dict(sorted(projection_completeness.items())),
            "particle_source_masses": particle,
            "legacy_particle_description": legacy_particle_summary(pooled),
            "warnings": result.warnings}


def _findings_markdown(summary: Mapping[str, Any]) -> str:
    primary = summary["groups"]["primary_combined"]
    rate = ("Current-contract eligibility rate not measurable from the retained campaign population."
            if primary["fully_observable"] == 0 else
            f"Current-contract eligibility rate: {primary['eligible']}/{primary['fully_observable']} "
            f"({primary['headline_eligibility_rate']:.1%}).")
    def bullets(values: Mapping[str, Any], *, empty: str) -> list[str]:
        return ([f"- `{name}`: {value}" for name, value in values.items()]
                if values else [f"- {empty}"])

    group_rows = []
    for name, values in summary["groups"].items():
        group_rows.append(
            f"| {name} | {values['pooled_outcomes']} | {values['fully_observable']} | "
            f"{values['eligible']} | {values['ineligible']} | {values['unassessable']} |")
    particle = primary["particle_source_masses"]
    legacy_particle = primary["legacy_particle_description"]
    lines = [
        "# EIG-4 Slice 1 census findings", "", rate, "",
        "This is a read-only, provisional contract-observability census; it does not establish runtime EIG authority.",
        "", "## Contract observability", "",
        "| Population | Pooled | Observable | Eligible | Ineligible | Unassessable |",
        "|---|---:|---:|---:|---:|---:|", *group_rows, "",
        f"The primary population contains {primary['finite_target']} finite-target outcomes, "
        f"{primary['tracker_confirmed']} tracker-confirmed outcomes, and "
        f"{primary['canonical_target_production_or_confirmation']} outcomes with authenticated "
        "canonical target production or confirmation intent.", "",
        "## What explicitly excludes otherwise retained outcomes?", "",
        *bullets(primary["primary_scientific_exclusion_counts"],
                 empty="No explicit scientific exclusion was observed."), "",
        "## What evidence prevents a current-contract decision?", "",
        *bullets(primary["primary_observability_gap_counts"],
                 empty="No observability gap was observed."), "",
        "## All observed reason codes", "",
        *bullets(primary["all_reason_counts"], empty="No reason codes were observed."), "",
        "## Authenticated `/2` particle mass", "",
        f"- Observed bootstrap source mass: {particle['observed_bootstrap_source_mass']}",
        f"- Observed nested source mass: {particle['observed_nested_source_mass']}",
        f"- Observed Laplace source mass: {particle['observed_laplace_source_mass']}",
        f"- Unknown or pruned source mass: {particle['basis_unknown_or_pruned_source_mass']}",
        f"- Source mass with unknown draw retention: {particle['draw_mass_unknown_source_mass']}", "",
        "These masses come only from authenticated current-contract fields. A homogeneous observed subset is not a complete homogeneous source measure when unknown or pruned mass is nonzero.", "",
        "## Legacy `/1` descriptive particle weights", "",
        f"- Legacy artifact rows: {legacy_particle['artifact_rows']}",
        f"- Raw legacy weight total: {legacy_particle['raw_legacy_weight_total']}",
        f"- Models with unreadable legacy weight: {legacy_particle['unreadable_weight_model_count']}",
        *bullets(legacy_particle["basis_weight_distribution"],
                 empty="No legacy particle weights were present."), "",
        "Legacy basis strings and weights are unauthenticated description only; they do not establish current-contract particle authority or homogeneity.", "",
        "## Projection and STAT-2a evidence", "",
        "Projection completeness:", "",
        *bullets(primary["projection_completeness"], empty="No projection evidence was present."), "",
        "Projection strict status:", "",
        *bullets(primary["projection_strict_status"], empty="No projection evidence was present."), "",
        "Projection reason codes:", "",
        *bullets(primary["projection_reason_codes"], empty="No projection reason was recorded."), "",
        "Projection currentness:", "",
        *bullets(primary["projection_currentness"], empty="No projection evidence was present."), "",
        "Shadow categories:", "",
        *bullets(primary["shadow_categories"], empty="No shadow evidence was present."), "",
        "## Historical limitations", "",
        "Historical `/1` predictive artifacts and absent projection, selection, shadow, scan-design, or acquisition authority remain unassessable rather than being treated as failed current evidence.",
        "Legacy delivery/action defaults cannot establish current canonical production or confirmation authority, and related reruns are descriptive lineages rather than independent replications.", "",
    ]
    return "\n".join(lines)


def _csv_rows(rows: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    fields = ["row_kind", "campaign_id", "cohort", "evidence_tier", "lineage", "master_session_key",
              "detector_id", "current_contract_observability", "provisional_eligibility",
              "primary_scientific_exclusion", "primary_observability_gap", "findings"]
    output = []
    for row in rows:
        output.append({field: (";".join(row.get("findings", [])) if field == "findings" else row.get(field))
                       for field in fields})
    return fields, output


def _public_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Strip implementation-only raw records before serializing the audit row."""
    def strip(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {key: strip(item) for key, item in value.items()
                    if key not in {"selection_record", "_record", "record"}}
        if isinstance(value, list):
            return [strip(item) for item in value]
        return value
    return strip({key: value for key, value in row.items() if key != "selection_record"})


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def write_census_bundle(result: CensusResult, output_dir: Path | str, *, repo_root: Path | str) -> dict[str, Path]:
    """Publish a complete bundle atomically enough that no file is authoritative until all exist."""
    output = Path(output_dir)
    root = Path(repo_root).resolve()
    if not output.is_absolute():
        output = root / output
    output = output.resolve()
    for spec in result.campaign_set.campaigns:
        try:
            output.relative_to(spec.root)
        except ValueError:
            continue
        raise ConfigError(f"output directory is inside campaign {spec.campaign_id!r}")
    parent = output.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=parent))
        summary = _summary(result)
        rows_jsonl = "".join(_canonical_json(_public_row(row)) + "\n" for row in result.rows)
        _write_text(stage / "masters.jsonl", rows_jsonl)
        fields, csv_rows = _csv_rows(result.rows)
        with (stage / "masters.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows(csv_rows)
        _write_text(stage / "summary.json", json.dumps(_sanitize(summary), indent=2, sort_keys=True,
                                                          ensure_ascii=False, allow_nan=False) + "\n")
        _write_text(stage / "findings.md", _findings_markdown(summary))
        deterministic = ["masters.jsonl", "masters.csv", "summary.json", "findings.md"]
        manifest = {"schema": "isar.eig4_slice1_census_run_manifest/1", "ruleset": RULESET,
                    "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "elapsed_seconds": round(result.elapsed_seconds, 6), "campaign_set": result.campaign_set.source.name,
                    "campaigns": [{"id": spec.campaign_id, "cohort": spec.cohort,
                                   "evidence_tier": spec.evidence_tier, "lineage": spec.lineage,
                                   "root": spec.root_relative, "summary": spec.summary,
                                   "mapping_kind": spec.mapping_kind, "note": spec.note}
                                  for spec in result.campaign_set.campaigns],
                    "input_hashes": result.input_hashes,
                    "referenced_sidecars": sorted({
                        (row.get(section, {}).get("reference"), row.get(section, {}).get("sha256"))
                        for row in result.rows for section in ("predictive", "projection")
                        if isinstance(row.get(section), Mapping) and row.get(section, {}).get("reference")
                    }),
                    "output_hashes": {name: _sha256_file(stage / name) for name in deterministic}}
        _write_text(stage / "run_manifest.json", json.dumps(_sanitize(manifest), indent=2, sort_keys=True,
                                                               ensure_ascii=False, allow_nan=False) + "\n")
        # Publish the directory as a unit.  Replacing individual files would allow a
        # crash or full disk to leave a superficially complete mixed-version bundle.
        backup = parent / f".{output.name}.previous"
        if backup.exists():
            shutil.rmtree(backup)
        if output.exists():
            os.replace(output, backup)
        try:
            os.replace(stage, output)
        except OSError:
            if backup.exists() and not output.exists():
                os.replace(backup, output)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    except ConfigError:
        raise
    except (OSError, UnicodeError, csv.Error, TypeError, ValueError) as error:
        _LOG.exception("census output failed: %s", error)
        try:
            if 'stage' in locals() and stage.exists():
                shutil.rmtree(stage)
        except OSError as cleanup_error:
            _LOG.error("could not clean failed census stage: %s", cleanup_error)
        raise OutputError(_error_code(error)) from error
    return {name: output / name for name in ("masters.jsonl", "masters.csv", "summary.json", "findings.md", "run_manifest.json")}
