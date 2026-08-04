"""Capture bounded EIG-4 Slice 8 case evidence into a self-contained retained bundle.

The scientific cases are produced by existing TAVI/synthetic campaign paths.  This tool performs
only the frozen retention step: reauthenticate their eligibility and Slice 7 artifacts, copy the
exact content-addressed files, and write the compact case/run manifests consumed by
``eig_slice8_gate.py``.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import hashlib
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from isar.drive.acquisition.realized_comparison import load_scan_design_realized_comparison
from isar.drive.acquisition.scan_design import load_scan_design_inventory
from isar.schema.action_identity import canonical_json
from isar.schema.eig_realized import RealizedScanRequest
from isar.schema.statistics import semantics_fingerprint
from tools.stats import eig_slice8_gate as gate


log = logging.getLogger(__name__)

CAPTURE_SPEC_CONTRACT = "isar.eig4_slice8_capture_spec/o36.8"
RUN_MANIFEST_CONTRACT = "isar.eig4_slice8_run_manifest/o36.8"
REPAIR_CAPTURE_SPEC_CONTRACT = "isar.eig4_slice8_capture_spec/o36.8.1"
REPLACEMENT_CAPTURE_SPEC_CONTRACT = "isar.eig4_slice8_capture_spec/o36.8.2"


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        log.error("cannot read capture input %s: %s", path, error)
        raise gate.GateEvidenceError(f"capture input {path} is unreadable") from error
    if not isinstance(value, Mapping):
        raise gate.GateEvidenceError(f"capture input {path} must contain an object")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    encoded = canonical_json(value).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, encoding="utf-8")
    return completed.stdout.strip()


def _safe_source_root(value: Any) -> Path:
    if not isinstance(value, str) or not value:
        raise gate.GateEvidenceError("source workspace is invalid")
    root = Path(value).resolve()
    if not root.is_dir():
        raise gate.GateEvidenceError(f"source workspace does not exist: {root}")
    return root


def _copy_artifact(source_root: Path, destination_root: Path,
                   reference: Mapping[str, Any]) -> dict[str, Any]:
    relative = reference.get("path") if isinstance(reference, Mapping) else None
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise gate.GateEvidenceError("artifact reference path is invalid")
    source_root = source_root.resolve()
    destination_root = destination_root.resolve()
    source = (source_root / relative).resolve()
    try:
        source.relative_to(source_root)
    except ValueError as error:
        raise gate.GateEvidenceError("source artifact escapes its workspace") from error
    destination = (destination_root / relative).resolve()
    try:
        destination.relative_to(destination_root)
    except ValueError as error:
        raise gate.GateEvidenceError("destination artifact escapes its case root") from error
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    if _sha256(destination) != reference.get("sha256"):
        raise gate.GateEvidenceError("copied artifact does not retain its raw-byte address")
    return {"path": destination.relative_to(destination_root).as_posix(),
            "sha256": reference["sha256"]}


def _primary_blocks(comparison: Any, inventory: Any) -> list[float]:
    variant = next((item for item in comparison.variants
                    if item.policy_id == gate.PRIMARY_POLICY_ID
                    and item.variant_id == gate.PRIMARY_VARIANT_ID), None)
    if variant is None or variant.status != "complete":
        raise gate.GateEvidenceError("capture case lacks the complete frozen primary variant")
    score_set, legacy_id, target_id = gate._score_set_payload(comparison, inventory, variant)
    return list(gate._paired_blocks(score_set, legacy_id, target_id))


def _capture_case(raw: Mapping[str, Any], definition: Mapping[str, Any],
                  bundle: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    allowed = {
        "case_id", "case_generation_seed", "classification",
        "fixture_definition_fingerprint", "eligibility_source",
        "source_workspace", "realized_comparison_ref", "applicability",
        "executed_grid_status", "submission_provenance", "analysis_outcome",
        "analysis_evidence",
    }
    if not isinstance(raw, Mapping) or set(raw) - allowed:
        raise gate.GateEvidenceError("capture case fields are invalid")
    if any(raw.get(name) != definition[name] for name in (
            "case_id", "case_generation_seed", "classification")):
        raise gate.GateEvidenceError("capture case identity does not match the frozen manifest")
    expected_fixture = gate.fixture_definition_fingerprint(definition["fixture_definition"])
    if raw.get("fixture_definition_fingerprint") != expected_fixture:
        raise gate.GateEvidenceError("capture case does not match its frozen fixture definition")
    _source, summary = gate.deserialize_eligibility_input(raw.get("eligibility_source"))
    if summary["status"] != definition["expected_eligibility"]:
        raise gate.GateEvidenceError("capture case eligibility is not the frozen state")

    evidence: dict[str, Any] = {
        "contract": gate.CASE_EVIDENCE_CONTRACT,
        "case_id": definition["case_id"],
        "case_generation_seed": definition["case_generation_seed"],
        "classification": definition["classification"],
        "fixture_definition_fingerprint": expected_fixture,
        "eligibility_source": raw["eligibility_source"],
        "case_evidence_fingerprint": "",
    }
    retained: list[dict[str, Any]] = []
    if summary["status"] == "eligible":
        source_root = _safe_source_root(raw.get("source_workspace"))
        reference = raw.get("realized_comparison_ref")
        comparison = load_scan_design_realized_comparison(reference, source_root)
        inventory = load_scan_design_inventory(comparison.source_inventory_ref, source_root)
        case_root_relative = Path("artifacts") / definition["case_id"]
        case_root = bundle / case_root_relative
        retained.append(_copy_artifact(source_root, case_root, comparison.source_inventory_ref))
        retained.append(_copy_artifact(source_root, case_root, reference))
        # Prove the copied pair is sufficient for the existing strict loader before publishing it.
        copied = load_scan_design_realized_comparison(reference, case_root)
        if copied.to_dict() != comparison.to_dict():
            raise gate.GateEvidenceError("copied realized comparison does not reproduce")
        if not isinstance(comparison.realized_pair.target_core, RealizedScanRequest):
            raise gate.GateEvidenceError("eligible capture case has no realized target-core request")
        evidence.update({
            "artifact_root": case_root_relative.as_posix(),
            "realized_comparison_ref": dict(reference),
            "applicability": raw.get("applicability"),
            "executed_grid_status": raw.get("executed_grid_status"),
            "submission_provenance": raw.get("submission_provenance"),
            "paired_block_deltas_per_executed_point_nats": _primary_blocks(
                comparison, inventory),
            "analysis_outcome": raw.get("analysis_outcome"),
            "analysis_evidence": raw.get("analysis_evidence"),
        })
    evidence["case_evidence_fingerprint"] = gate._fingerprint(
        "isar.eig4_slice8_case_evidence_fingerprint/o36.8",
        evidence,
        "case_evidence_fingerprint",
    )
    return evidence, retained


def _capture_fault(raw: Mapping[str, Any], definition: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "fault_id", "source_case_id", "executed_grid_status", "reason_code",
        "ordinary_shadow_analysis_continued",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise gate.GateEvidenceError("capture fault fields are invalid")
    value = {"contract": gate.FAULT_EVIDENCE_CONTRACT, **dict(raw),
             "fault_evidence_fingerprint": ""}
    if (value["fault_id"] != definition["fault_id"]
            or value["source_case_id"] != definition["source_case_id"]):
        raise gate.GateEvidenceError("capture fault identity is not frozen")
    value["fault_evidence_fingerprint"] = gate._fingerprint(
        "isar.eig4_slice8_fault_evidence_fingerprint/o36.8",
        value,
        "fault_evidence_fingerprint",
    )
    return value


def capture_bundle(bundle: Path, capture_spec: Mapping[str, Any]) -> dict[str, Any]:
    if capture_spec.get("contract") in {
            REPAIR_CAPTURE_SPEC_CONTRACT, REPLACEMENT_CAPTURE_SPEC_CONTRACT}:
        return capture_repair_bundle(bundle, capture_spec)
    gate_manifest = gate.validate_gate_manifest(_read_json(bundle / "gate-manifest.json"))
    case_manifest = gate.validate_case_manifest(
        _read_json(bundle / "case-manifest.json"), gate_manifest)
    fields = {
        "contract", "implementation_anchor", "evidence_generation_anchor", "cases", "faults",
        "runtime_invariants", "verification", "commands", "durations_seconds",
    }
    if not isinstance(capture_spec, Mapping) or set(capture_spec) != fields:
        raise gate.GateEvidenceError("capture specification fields are invalid")
    if (capture_spec["contract"] != CAPTURE_SPEC_CONTRACT
            or capture_spec["implementation_anchor"] != gate_manifest["implementation_anchor"]
            or not gate._valid_commit(capture_spec["evidence_generation_anchor"])):
        raise gate.GateEvidenceError("capture specification anchors are invalid")
    head = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain")
    if head != capture_spec["evidence_generation_anchor"] or status:
        raise gate.GateEvidenceError(
            "evidence capture requires clean HEAD at the evidence-generation anchor")

    by_case = {item.get("case_id"): item for item in capture_spec["cases"]
               if isinstance(item, Mapping)}
    if set(by_case) != {item["case_id"] for item in gate.CASE_DEFINITIONS}:
        raise gate.GateEvidenceError("capture specification does not cover six frozen cases")
    by_fault = {item.get("fault_id"): item for item in capture_spec["faults"]
                if isinstance(item, Mapping)}
    if set(by_fault) != {item["fault_id"] for item in gate.FAULT_DEFINITIONS}:
        raise gate.GateEvidenceError("capture specification does not cover the frozen fault")

    cases = []
    artifacts = []
    for definition in case_manifest["cases"]:
        evidence, retained = _capture_case(by_case[definition["case_id"]], definition, bundle)
        cases.append(evidence)
        artifacts.extend({"case_id": definition["case_id"], **item} for item in retained)
    faults = [_capture_fault(by_fault[item["fault_id"]], item)
              for item in gate.FAULT_DEFINITIONS]
    results: dict[str, Any] = {
        "contract": gate.CASE_RESULTS_CONTRACT,
        "gate_manifest_fingerprint": gate_manifest["gate_manifest_fingerprint"],
        "case_manifest_fingerprint": case_manifest["case_manifest_fingerprint"],
        "implementation_anchor": capture_spec["implementation_anchor"],
        "evidence_generation_anchor": capture_spec["evidence_generation_anchor"],
        "cases": cases,
        "faults": faults,
        "runtime_invariants": capture_spec["runtime_invariants"],
        "verification": capture_spec["verification"],
        "case_results_fingerprint": "",
    }
    results["case_results_fingerprint"] = gate._fingerprint(
        "isar.eig4_slice8_case_results_fingerprint/o36.8",
        results,
        "case_results_fingerprint",
    )
    _write_json(bundle / "case-results.json", results)
    gate_result = gate.evaluate_bundle(bundle)
    _write_json(bundle / "gate-result.json", gate_result)

    run_manifest: dict[str, Any] = {
        "contract": RUN_MANIFEST_CONTRACT,
        "starting_anchor": "a9107ffaf74dfac2fdb554753b25b578e14f1585",
        "implementation_anchor": capture_spec["implementation_anchor"],
        "evidence_generation_anchor": capture_spec["evidence_generation_anchor"],
        "clean_head_verified": True,
        "gate_manifest_fingerprint": gate_manifest["gate_manifest_fingerprint"],
        "case_manifest_fingerprint": case_manifest["case_manifest_fingerprint"],
        "case_results_fingerprint": results["case_results_fingerprint"],
        "gate_result_fingerprint": gate_result["gate_result_fingerprint"],
        "commands": capture_spec["commands"],
        "durations_seconds": capture_spec["durations_seconds"],
        "environment": {
            "python": sys.version.split()[0],
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "packages": {
                "numpy": importlib.metadata.version("numpy"),
                "scipy": importlib.metadata.version("scipy"),
            },
        },
        "retained_artifacts": artifacts,
        "run_manifest_fingerprint": "",
    }
    run_manifest["run_manifest_fingerprint"] = semantics_fingerprint(
        "isar.eig4_slice8_run_manifest_fingerprint/o36.8",
        {key: value for key, value in run_manifest.items()
         if key != "run_manifest_fingerprint"},
    )
    _write_json(bundle / "run-manifest.json", run_manifest)
    return gate_result


def _repair_case_evidence(raw: Mapping[str, Any], definition: Mapping[str, Any],
                          bundle: Path) -> dict[str, Any]:
    status = definition["expected_eligibility"]
    source_workspace = raw.get("source_workspace")
    if status == "eligible":
        source = _safe_source_root(source_workspace)
        artifact_root = bundle / "artifacts" / definition["case_id"]
        if artifact_root.exists():
            raise gate.GateEvidenceError(f"repair artifact root already exists: {artifact_root}")
        shutil.copytree(source, artifact_root)
        relative_root = artifact_root.relative_to(bundle).as_posix()
        fields = {
            "artifact_root": relative_root,
            "runtime_evidence_status": "available", "runtime_unavailability": None,
            "scientific_action_fingerprint": raw["scientific_action_fingerprint"],
            "proposal_metadata_ref": raw["proposal_metadata_ref"],
            "scoring_source_ref": raw["scoring_source_ref"],
            "prior_source_record_ref": raw["prior_source_record_ref"],
            "prior_scan_design_inventory_ref": raw["prior_scan_design_inventory_ref"],
            "current_scan_design_inventory_ref": raw["current_scan_design_inventory_ref"],
            "realized_comparison_ref": raw["realized_comparison_ref"],
            "applicability": raw["applicability"],
            "executed_grid_status": raw["executed_grid_status"],
            "submission_provenance_ref": "report/current-submission.json",
            "acquisition_provenance_ref": "report/current-acquisition-provenance.json",
            "paired_block_deltas_per_executed_point_nats": list(
                raw["paired_block_deltas_per_executed_point_nats"]),
            "analysis_evidence_ref": raw.get("analysis_evidence_ref"),
            "fork_evidence_ref": raw.get("fork_evidence_ref"),
        }
    else:
        fields = {
            "artifact_root": None,
            "runtime_evidence_status": "available", "runtime_unavailability": None,
            "scientific_action_fingerprint": None,
            "proposal_metadata_ref": None, "scoring_source_ref": None,
            "prior_source_record_ref": None, "prior_scan_design_inventory_ref": None,
            "current_scan_design_inventory_ref": None, "realized_comparison_ref": None,
            "applicability": None, "executed_grid_status": None,
            "submission_provenance_ref": None, "acquisition_provenance_ref": None,
            "paired_block_deltas_per_executed_point_nats": None,
            "analysis_evidence_ref": None, "fork_evidence_ref": None,
        }
    value: dict[str, Any] = {
        "contract": gate.REPAIR_CASE_EVIDENCE_CONTRACT,
        "case_id": definition["case_id"],
        "case_generation_seed": definition["case_generation_seed"],
        "classification": definition["classification"],
        "fixture_definition_fingerprint": gate.fixture_definition_fingerprint(
            definition["fixture_definition"]),
        "eligibility_source": raw["eligibility_source"],
        **fields,
        "case_evidence_fingerprint": "",
    }
    value["case_evidence_fingerprint"] = gate._fingerprint(
        "isar.eig4_slice8_case_evidence_fingerprint/o36.8.1", value,
        "case_evidence_fingerprint")
    return value


def capture_repair_bundle(bundle: Path, capture_spec: Mapping[str, Any]) -> dict[str, Any]:
    """Capture an acyclic coherent bundle using the retained o36.8.1 evidence layer."""
    fields = {
        "contract", "implementation_anchor", "evidence_generation_anchor", "cases", "faults",
        "runtime_invariants", "verification_records", "commands", "durations_seconds",
    }
    if not isinstance(capture_spec, Mapping) or set(capture_spec) != fields:
        raise gate.GateEvidenceError("repair capture specification fields are invalid")
    replacement = capture_spec.get("contract") == REPLACEMENT_CAPTURE_SPEC_CONTRACT
    if capture_spec.get("contract") not in {
            REPAIR_CAPTURE_SPEC_CONTRACT, REPLACEMENT_CAPTURE_SPEC_CONTRACT}:
        raise gate.GateEvidenceError("coherent capture specification contract is unsupported")
    gate_document = _read_json(bundle / "gate-manifest.json")
    case_document = _read_json(bundle / "case-manifest.json")
    gate_manifest = (gate.validate_replacement_gate_manifest(gate_document) if replacement
                     else gate.validate_repair_gate_manifest(gate_document))
    case_manifest = (gate.validate_replacement_case_manifest(
        case_document, gate_manifest) if replacement else gate.validate_repair_case_manifest(
            case_document, gate_manifest))
    if (capture_spec["implementation_anchor"] != gate_manifest["implementation_anchor"]
            or not gate._valid_commit(capture_spec["evidence_generation_anchor"])):
        raise gate.GateEvidenceError("repair capture anchors are invalid")
    if (_git("rev-parse", "HEAD") != capture_spec["evidence_generation_anchor"]
            or _git("status", "--porcelain")):
        raise gate.GateEvidenceError(
            "repair evidence capture requires clean HEAD at its evidence-generation anchor")
    by_case = {item.get("case_id"): item for item in capture_spec["cases"]
               if isinstance(item, Mapping)}
    by_fault = {item.get("fault_id"): item for item in capture_spec["faults"]
                if isinstance(item, Mapping)}
    if (set(by_case) != {item["case_id"] for item in gate.CASE_DEFINITIONS}
            or set(by_fault) != {item["fault_id"] for item in gate.FAULT_DEFINITIONS}):
        raise gate.GateEvidenceError("repair capture does not cover the frozen case/fault set")

    case_evidence = [_repair_case_evidence(
        by_case[item["case_id"]], item, bundle) for item in case_manifest["cases"]]
    strong = next(item for item in case_evidence
                  if item["case_id"] == "s8-c01-strong-confirmed")
    raw_fault = by_fault["s8-f01-executed-grid-order"]
    fault: dict[str, Any] = {
        "contract": gate.REPAIR_FAULT_EVIDENCE_CONTRACT,
        "fault_id": "s8-f01-executed-grid-order",
        "source_case_id": "s8-c01-strong-confirmed",
        "artifact_root": strong["artifact_root"],
        "injected_grid_ref": raw_fault["injected_grid_ref"],
        "event_ref": raw_fault["event_ref"],
        "mismatch_provenance_ref": raw_fault["mismatch_provenance_ref"],
        "post_analysis_ref": raw_fault["post_analysis_ref"],
        "fault_evidence_fingerprint": "",
    }
    fault["fault_evidence_fingerprint"] = gate._fingerprint(
        "isar.eig4_slice8_fault_evidence_fingerprint/o36.8.1", fault,
        "fault_evidence_fingerprint")
    verification: dict[str, Any] = {
        "contract": gate.REPAIR_VERIFICATION_CONTRACT,
        "implementation_anchor": gate_manifest["implementation_anchor"],
        "records": capture_spec["verification_records"],
        "verification_evidence_fingerprint": "",
    }
    verification["verification_evidence_fingerprint"] = gate._fingerprint(
        "isar.eig4_slice8_verification_evidence_fingerprint/o36.8.1", verification,
        "verification_evidence_fingerprint")
    _write_json(bundle / "verification-evidence.json", verification)

    retained_paths = [
        path for path in bundle.rglob("*") if path.is_file()
        and path.name not in {"run-manifest.json", "case-results.json", "gate-result.json"}
    ]
    retained_files = [{
        "path": path.relative_to(bundle).as_posix(), "sha256": _sha256(path),
    } for path in sorted(retained_paths)]
    run: dict[str, Any] = {
        "contract": gate.REPAIR_RUN_MANIFEST_CONTRACT,
        "implementation_anchor": gate_manifest["implementation_anchor"],
        "evidence_generation_anchor": capture_spec["evidence_generation_anchor"],
        "gate_manifest_fingerprint": gate_manifest["gate_manifest_fingerprint"],
        "case_manifest_fingerprint": case_manifest["case_manifest_fingerprint"],
        "verification_evidence_fingerprint": verification[
            "verification_evidence_fingerprint"],
        "retained_files": retained_files,
        "commands": list(capture_spec["commands"]),
        "durations_seconds": dict(capture_spec["durations_seconds"]),
        "run_manifest_fingerprint": "",
    }
    run["run_manifest_fingerprint"] = gate._fingerprint(
        "isar.eig4_slice8_run_manifest_fingerprint/o36.8.1", run,
        "run_manifest_fingerprint")
    _write_json(bundle / "run-manifest.json", run)
    results: dict[str, Any] = {
        "contract": gate.REPAIR_CASE_RESULTS_CONTRACT,
        "gate_manifest_fingerprint": gate_manifest["gate_manifest_fingerprint"],
        "case_manifest_fingerprint": case_manifest["case_manifest_fingerprint"],
        "run_manifest_fingerprint": run["run_manifest_fingerprint"],
        "verification_evidence_fingerprint": verification[
            "verification_evidence_fingerprint"],
        "implementation_anchor": gate_manifest["implementation_anchor"],
        "evidence_generation_anchor": capture_spec["evidence_generation_anchor"],
        "cases": case_evidence, "faults": [fault],
        "runtime_invariants": capture_spec["runtime_invariants"],
        "case_results_fingerprint": "",
    }
    results["case_results_fingerprint"] = gate._fingerprint(
        "isar.eig4_slice8_case_results_fingerprint/o36.8.1", results,
        "case_results_fingerprint")
    _write_json(bundle / "case-results.json", results)
    disposition = gate.evaluate_repair_bundle(bundle, replacement=replacement)
    _write_json(bundle / "gate-result.json", disposition)
    return disposition


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--capture-spec", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    try:
        result = capture_bundle(args.bundle, _read_json(args.capture_spec))
        print(canonical_json({"overall_disposition": result["overall_disposition"],
                              "gate_result_fingerprint": result["gate_result_fingerprint"]}))
        return 0
    except (gate.GateEvidenceError, OSError, subprocess.CalledProcessError, ValueError) as error:
        log.error("Slice 8 capture failed: %s", error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
