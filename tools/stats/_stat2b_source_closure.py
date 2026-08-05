"""STAT-2b.1 production source-closure engine.

This module owns the complete read-only audit interface: historical evidence
authentication, aggregate replay, AST dependency discovery, manifest
fingerprints, and candidate verification.  It never grants owner approval and
does not import or execute any inspected ISAR source module.
"""
from __future__ import annotations

import ast
import datetime as dt
import hashlib
import json
import math
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


CLOSURE_SCHEMA = "isar.stat2b_production_source_closure/2"
REVIEW_SCHEMA = "isar.stat2b_source_closure_review/2"
BOUNDARY_REVISION = "isar.stat2b_production_dependency_boundary/2"
REPLAY_SCHEMA = "isar.stat2b1_aggregate_replay/1"
CAMPAIGN_SOURCE_SCHEMA = "isar.stat2b_campaign_sources/1"
GATE_SCHEMA = "isar.stat2b_heldout_gate/1"
HELDOUT_SUMMARY_SCHEMA = "isar.stat2b_heldout_summary/1"

BOOTSTRAP_PRODUCTION_ANCHOR = "ce490fec5badbc76e8799b7fb3f84ef5c201b855"
APPROVED_OWNER = "ammerritt-gh"
HISTORICAL_GATE_PATH = "tools/stats/stat2b1-heldout-gate.json"
HELDOUT_SUMMARY_PATH = (
    "docs/campaigns/2026-07-31_STAT2B1_HELDOUT/decision_summary.json"
)
DEFAULT_CANDIDATE_PATH = (
    "tools/stats/stat2b1-production-source-closure.candidate.json"
)
DEFAULT_ACTIVE_PATH = "tools/stats/stat2b1-production-source-closure.json"

HISTORICAL_GATE_SHA256 = (
    "c4d7ef5cba4ee42c71c8aa1e112a4e77b300c2bac5fc29954ce797d71024e6e0"
)
HISTORICAL_SOURCE_FINGERPRINT = (
    "e655e3bbf92f2f57b1fbab4074120f8194d15930f46f55fb56590909f0662525"
)
HISTORICAL_GATE_FINGERPRINT = (
    "ceaecf278329297729ae77d29eccfd940a5eaeefde703b86815225be5fc2a645"
)
HISTORICAL_EVALUATOR_PATH = "isar/synth/stat2b/campaign.py"
HISTORICAL_EVALUATOR_SHA256 = (
    "6800a240ed4b42280d762f4d260e9a6630c4bf8caf3a31483e6d59ef28b65a64"
)
HELDOUT_SPEC_FINGERPRINT = (
    "43b9fcbc7f6c2cbb57510c4801ac8084e96caecc7b511e6707240e257e974a54"
)
DEVELOPMENT_SUMMARY_FINGERPRINT = (
    "cae8a48fd687a2cb725b2a36bc9f82ae2a4cb4874ef490b8b7bb61b8dfcb0da7"
)
HELDOUT_SUMMARY_FINGERPRINT = (
    "e85c05edb6114f0f014e66a30074147a5f086622af9db5d6df296b7666ef438c"
)
HELDOUT_SUMMARY_SHA256 = (
    "e4d626d813d1f827cc5c077963674c123073134589281cab8b7e2c361b64fb8b"
)
HELDOUT_RUN_FINGERPRINT = (
    "262e6c93932650f4576deb8edb906abb60a180906e80db14226cfb7fd328912d"
)
EXPECTED_REPLAY_FINGERPRINT = (
    "48b17e372543eca0e7d848d4eda595f7fa3b9a5381a5acbd15ed8a348e3b8f56"
)
EXPECTED_ARMS = (
    "legacy_aic",
    "oracle_aic",
    "oracle_aicc",
    "oracle_bic",
    "unconstrained_aic",
    "unconstrained_aic_null",
    "unconstrained_aicc",
    "unconstrained_aicc_null",
    "unconstrained_bic",
)
STANDARD_ARMS = tuple(arm for arm in EXPECTED_ARMS if not arm.startswith("oracle_"))
ORACLE_ARMS = tuple(arm for arm in EXPECTED_ARMS if arm.startswith("oracle_"))
ORACLE_SCENARIOS = {
    "absent-background",
    "absent-elastic",
    "absent-elastic-qe",
    "absent-qe",
    "continuum-absent",
    "misspecified-sloped-background",
    "partial-two-lower",
    "partial-two-upper",
    "separated-two",
}

HISTORICAL_SOURCE_ROOTS = (
    "isar/analyze",
    "isar/schema",
    "isar/session",
    "isar/synth/stat2b",
)

MODULE_ROLES = {
    "selection_critical",
    "evidence_critical",
    "campaign_runtime_dependency",
    "unrelated",
}
DEPENDENCY_DISPOSITIONS = {
    "protected_semantic",
    "protected_adjacent",
    "unrelated_exempt",
}
CHANGE_DISPOSITIONS = {
    "selection_semantic",
    "evidence_contract_semantic",
    "adjacent_nonsemantic",
    "unrelated_exempt",
}
EDGE_KINDS = {
    "runtime",
    "conditional",
    "type_only",
    "package_reexport",
    "manually_declared_dynamic",
}

SELECTION_CRITICAL = {
    "isar/analyze/components.py",
    "isar/analyze/context.py",
    "isar/analyze/errors/bootstrap.py",
    "isar/analyze/ic.py",
    "isar/analyze/loop.py",
    "isar/analyze/select.py",
    "isar/analyze/shadow_selection.py",
    "isar/analyze/structures.py",
    "isar/analyze/verdict.py",
    "isar/schema/statistics.py",
    "isar/session/driver.py",
    "isar/synth/_ff.py",
    "isar/synth/backends.py",
    "isar/synth/estimators.py",
    "isar/synth/fit.py",
}
EVIDENCE_CRITICAL = {
    "isar/analyze/errors/__init__.py",
    "isar/analyze/errors/agree.py",
    "isar/analyze/errors/bayes.py",
    "isar/analyze/errors/profile.py",
    "isar/analyze/model_manifest.py",
    "isar/predictive.py",
    "isar/session/predictive_artifact.py",
}
EXPLICIT_ADJACENT = {
    "isar/drive/objective.py",
    "isar/drive/supervisor/stopping.py",
    "isar/schema/card.py",
    "isar/schema/eig_policy.py",
}
EXPLICIT_EXEMPT = {
    "isar/eig/o41_authority.py": (
        "The restricted scan-window selector is reached only when the separately frozen EIG mode "
        "is authoritative; the frozen STAT-2b campaign uses the off/default path."
    ),
    "isar/schema/action_identity.py": (
        "EIG action identity is absent from the STAT-2b selection and accepted predictive "
        "evidence path."
    ),
    "isar/schema/eig_shadow.py": (
        "EIG shadow-score serialization is diagnostic-only and is not imported by the "
        "STAT-2b selection or predictive evidence path."
    ),
    "isar/schema/eig_realized.py": (
        "EIG realized-request comparison serialization is diagnostic-only and is not imported "
        "by the STAT-2b selection or predictive evidence path."
    ),
    "isar/schema/eig_authority.py": (
        "The O41 authority-decision sidecar is outside the frozen STAT-2b selection and accepted "
        "predictive evidence path."
    ),
    "isar/eig/policy.py": (
        "Only conditionally reached for a non-empty EIG policy set; the frozen STAT-2b "
        "campaign constructs the empty policy default."
    ),
}

DEPENDENCY_ENTRYPOINTS = (
    "isar/analyze/loop.py",
    "isar/analyze/select.py",
    "isar/analyze/shadow_selection.py",
    "isar/drive/objective.py",
    "isar/predictive.py",
    "isar/schema/card.py",
    "isar/session/driver.py",
    "isar/session/predictive_artifact.py",
)

# Reviewed AST search found no meaningful dynamic imports on the protected path.
# Add any future declaration here with the same strict edge schema; the graph
# fingerprint and candidate review will then bind it explicitly.
MANUAL_DEPENDENCIES: tuple[dict[str, str], ...] = ()

CURRENT_CHANGE_DISPOSITIONS = {
    "isar/drive/objective.py": "adjacent_nonsemantic",
    "isar/drive/session_log.py": "adjacent_nonsemantic",
    "isar/drive/supervisor/stopping.py": "adjacent_nonsemantic",
    "isar/schema/action_identity.py": "unrelated_exempt",
    "isar/schema/card.py": "adjacent_nonsemantic",
    "isar/schema/eig_policy.py": "adjacent_nonsemantic",
    "isar/schema/eig_shadow.py": "unrelated_exempt",
    "isar/schema/eig_realized.py": "unrelated_exempt",
    "isar/schema/eig_authority.py": "unrelated_exempt",
    "isar/schema/hypotheses.py": "adjacent_nonsemantic",
    "isar/session/driver.py": "selection_semantic",
    "isar/session/predictive_artifact.py": "evidence_contract_semantic",
    "isar/session/worker.py": "adjacent_nonsemantic",
}
PROOF_TESTS = {
    "isar/drive/objective.py": [
        "isar/synth/tests/test_stat2b_campaign.py",
        "tests/session/test_target_projection.py",
    ],
    "isar/drive/session_log.py": [
        "tests/drive/test_session_log.py",
        "tests/session/test_driver_smoke.py",
    ],
    "isar/drive/supervisor/stopping.py": [
        "tests/drive/test_stopping.py",
        "tests/drive/test_loop.py",
        "isar/synth/tests/test_stat2b_campaign.py",
    ],
    "isar/schema/action_identity.py": ["tests/eig/test_eligibility.py"],
    "isar/schema/card.py": [
        "isar/synth/tests/test_stat2b_campaign.py",
        "tests/schema/test_eig_policy.py",
    ],
    "isar/schema/eig_policy.py": ["tests/schema/test_eig_policy.py"],
    "isar/schema/eig_shadow.py": ["tests/schema/test_eig_shadow.py"],
    "isar/schema/eig_realized.py": [
        "tests/drive/test_realized_comparison.py",
        "tests/eig/test_realized_score.py",
    ],
    "isar/schema/eig_authority.py": ["tests/schema/test_eig_authority.py"],
    "isar/schema/hypotheses.py": [
        "isar/synth/tests/test_stat2b_campaign.py",
        "tests/schema/test_hypotheses.py",
    ],
    "isar/session/driver.py": [
        "tests/session/test_master_fit.py",
        "tests/session/test_driver_smoke.py",
        "tests/drive/test_predictive_promotion.py",
        "isar/synth/tests/test_stat2b_campaign.py",
    ],
    "isar/session/predictive_artifact.py": [
        "tests/session/test_master_predictive.py",
        "tests/session/test_target_projection.py",
        "tests/session/test_statistical_semantics.py",
    ],
    "isar/session/worker.py": [
        "tests/drive/test_worker_client.py",
        "tests/session/test_driver_smoke.py",
    ],
}


class ClosureError(RuntimeError):
    """The candidate or active closure cannot be authenticated."""


@dataclass(frozen=True)
class VerificationResult:
    """Complete observable result returned through the engine interface."""

    ok: bool
    status: str
    findings: tuple[dict[str, Any], ...]
    manifest: Mapping[str, Any] | None = None


def _canonical_value(value: Any, path: str = "$") -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ClosureError(f"non-finite fingerprint value at {path}")
        return float(value)
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ClosureError(f"non-string fingerprint key at {path}")
            result[key] = _canonical_value(item, f"{path}.{key}")
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _canonical_value(item, f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise ClosureError(f"non-canonical fingerprint value at {path}: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def semantics_fingerprint(namespace: str, payload: Any) -> str:
    if not isinstance(namespace, str) or not namespace.strip():
        raise ClosureError("fingerprint namespace is missing")
    encoded = canonical_json({"namespace": namespace, "payload": payload}).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    if not path.is_file():
        raise ClosureError(f"required file is missing: {path}")
    return sha256_bytes(path.read_bytes())


def _is_commit_sha(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def source_content_sha256(path: Path) -> str:
    """Hash canonical UTF-8 source content independently of checkout line endings."""
    if not path.is_file():
        raise ClosureError(f"required source file is missing: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ClosureError(f"cannot read UTF-8 source {path}: {exc}") from exc
    canonical = text.replace("\r\n", "\n").replace("\r", "\n")
    return sha256_bytes(canonical.encode("utf-8"))


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ClosureError(f"cannot read strict JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ClosureError(f"expected a JSON object: {path}")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ) + "\n"
    path.write_text(payload, encoding="utf-8", newline="\n")


def _repo_path(repo_root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ClosureError(f"invalid repository-relative path: {relative!r}")
    candidate = (repo_root / relative).resolve()
    try:
        candidate.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise ClosureError(f"path escapes repository: {relative}") from exc
    return candidate


def authenticate_historical_gate(repo_root: Path) -> dict[str, Any]:
    gate_path = _repo_path(repo_root, HISTORICAL_GATE_PATH)
    if sha256_file(gate_path) != HISTORICAL_GATE_SHA256:
        raise ClosureError("historical held-out gate bytes changed")
    gate = _read_json(gate_path)
    if gate.get("schema") != GATE_SCHEMA or gate.get("owner_approved") is not True:
        raise ClosureError("historical held-out gate is not the frozen owner-approved contract")
    source_hashes = gate.get("source_hashes")
    if not isinstance(source_hashes, dict):
        raise ClosureError("historical held-out gate lacks source hashes")
    source_fingerprint = semantics_fingerprint(
        CAMPAIGN_SOURCE_SCHEMA, dict(sorted(source_hashes.items()))
    )
    if source_fingerprint != HISTORICAL_SOURCE_FINGERPRINT:
        raise ClosureError("historical campaign-source fingerprint changed")
    if semantics_fingerprint(GATE_SCHEMA, gate) != HISTORICAL_GATE_FINGERPRINT:
        raise ClosureError("historical held-out gate semantic fingerprint changed")
    if source_hashes.get(HISTORICAL_EVALUATOR_PATH) != HISTORICAL_EVALUATOR_SHA256:
        raise ClosureError("historical gate does not bind the expected evaluator")
    return gate


def _evaluate_gate(
    gate: Mapping[str, Any],
    overall: Mapping[str, Any],
    strata: Mapping[str, Mapping[str, Any]],
    *,
    runtime_seconds: float,
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    all_pass = True
    for gate_name, raw_rule in gate["margins"].items():
        rule = dict(raw_rule)
        metric = str(rule["metric"])
        checks = {}
        applicable = set(rule.get("applicable_strata", strata))
        scoped = [("overall", overall.get(metric))] + [
            (name, values.get(metric))
            for name, values in sorted(strata.items())
            if name in applicable
        ]
        for scope, value in scoped:
            if value is None:
                checks[scope] = {"status": "unavailable", "value": None}
                all_pass = False
                continue
            passed = (
                float(value) <= float(rule["maximum"])
                if "maximum" in rule
                else float(value) >= float(rule["minimum"])
            )
            checks[scope] = {
                "status": "pass" if passed else "fail",
                "value": float(value),
            }
            all_pass = all_pass and passed
        results[gate_name] = checks
    runtime_ceiling = float(gate["runtime_ceiling_seconds"])
    runtime_pass = float(runtime_seconds) <= runtime_ceiling
    all_pass = all_pass and runtime_pass
    return {
        "status": "pass" if all_pass else "authority_withheld",
        "all_required_gates_pass": all_pass,
        "checks": results,
        "runtime_ceiling": {
            "status": "pass" if runtime_pass else "fail",
            "value": float(runtime_seconds),
            "maximum": runtime_ceiling,
        },
    }


def replay_heldout_aggregate(repo_root: Path, gate: Mapping[str, Any]) -> dict[str, Any]:
    evaluator = _repo_path(repo_root, HISTORICAL_EVALUATOR_PATH)
    if sha256_file(evaluator) != HISTORICAL_EVALUATOR_SHA256:
        raise ClosureError("historical evaluator source is unavailable at its frozen identity")
    summary_path = _repo_path(repo_root, HELDOUT_SUMMARY_PATH)
    if sha256_file(summary_path) != HELDOUT_SUMMARY_SHA256:
        raise ClosureError("held-out aggregate bytes changed")
    summary = _read_json(summary_path)
    stored_summary_fingerprint = summary.get("heldout_summary_fingerprint")
    summary_payload = dict(summary)
    summary_payload.pop("heldout_summary_fingerprint", None)
    recomputed_summary_fingerprint = semantics_fingerprint(
        HELDOUT_SUMMARY_SCHEMA, summary_payload
    )
    if not (
        stored_summary_fingerprint
        == recomputed_summary_fingerprint
        == HELDOUT_SUMMARY_FINGERPRINT
    ):
        raise ClosureError("held-out aggregate semantic fingerprint does not reproduce")
    exact = {
        "campaign_spec_fingerprint": HELDOUT_SPEC_FINGERPRINT,
        "heldout_summary_fingerprint": HELDOUT_SUMMARY_FINGERPRINT,
        "run_fingerprint": HELDOUT_RUN_FINGERPRINT,
        "frozen_owner_gate_fingerprint": HISTORICAL_GATE_FINGERPRINT,
        "cell_count": 21,
        "dataset_count": 2163,
    }
    for field, expected in exact.items():
        if summary.get(field) != expected:
            raise ClosureError(f"held-out aggregate has unexpected {field}")
    rows = summary.get("cell_summaries")
    expected_row_count = 21 * len(STANDARD_ARMS) + len(ORACLE_SCENARIOS) * len(ORACLE_ARMS)
    if not isinstance(rows, list) or len(rows) != expected_row_count:
        raise ClosureError("held-out aggregate has an invalid cell/arm matrix")
    observed = {(row.get("scenario_id"), row.get("arm_id")) for row in rows}
    scenarios = {row.get("scenario_id") for row in rows}
    expected = {(scenario, arm) for scenario in scenarios for arm in STANDARD_ARMS}
    expected.update(
        (scenario, arm) for scenario in ORACLE_SCENARIOS for arm in ORACLE_ARMS
    )
    if len(scenarios) != 21 or observed != expected:
        raise ClosureError("held-out aggregate does not contain the expected standard/oracle arms")
    if any(int(row.get("n", -1)) != 103 for row in rows):
        raise ClosureError("held-out aggregate does not contain n=103 for every arm/cell")
    if summary.get("source_hashes", {}).get(HISTORICAL_EVALUATOR_PATH) != (
        HISTORICAL_EVALUATOR_SHA256
    ):
        raise ClosureError("held-out aggregate is not bound to the historical evaluator")
    evaluation = _evaluate_gate(
        gate,
        summary["observed_gate_metrics"],
        summary["observed_critical_stratum_metrics"],
        runtime_seconds=float(summary["runtime"]["phase_wall_seconds"]),
    )
    if evaluation != summary.get("heldout_gate_evaluation"):
        raise ClosureError("aggregate replay differs from the committed evaluation")
    if evaluation["status"] != "authority_withheld":
        raise ClosureError("aggregate replay no longer withholds authority")
    replay_fingerprint = semantics_fingerprint(REPLAY_SCHEMA, evaluation)
    if replay_fingerprint != EXPECTED_REPLAY_FINGERPRINT:
        raise ClosureError("aggregate replay fingerprint differs from the planning probe")
    failed_rules = []
    for rule_id, checks in evaluation["checks"].items():
        rule = gate["margins"][rule_id]
        required = (
            {"kind": "maximum", "value": float(rule["maximum"])}
            if "maximum" in rule
            else {"kind": "minimum", "value": float(rule["minimum"])}
        )
        for scope, check in checks.items():
            if check["status"] != "pass":
                failed_rules.append(
                    {
                        "rule_id": rule_id,
                        "metric": rule["metric"],
                        "scope": scope,
                        "observed_bound": check["value"],
                        "required_margin": required,
                    }
                )
    return {
        "schema": REPLAY_SCHEMA,
        "aggregate_path": HELDOUT_SUMMARY_PATH,
        "campaign_spec_fingerprint": HELDOUT_SPEC_FINGERPRINT,
        "heldout_summary_fingerprint": HELDOUT_SUMMARY_FINGERPRINT,
        "heldout_summary_sha256": HELDOUT_SUMMARY_SHA256,
        "run_fingerprint": HELDOUT_RUN_FINGERPRINT,
        "gate_fingerprint": HISTORICAL_GATE_FINGERPRINT,
        "historical_evaluator_path": HISTORICAL_EVALUATOR_PATH,
        "historical_evaluator_sha256": HISTORICAL_EVALUATOR_SHA256,
        "status": evaluation["status"],
        "failed_rules": failed_rules,
        "evaluation": evaluation,
        "replay_fingerprint": replay_fingerprint,
    }


def historical_source_hashes(repo_root: Path, gate: Mapping[str, Any]) -> dict[str, str]:
    historical = dict(gate["source_hashes"])
    relatives = set(historical)
    for root in HISTORICAL_SOURCE_ROOTS:
        directory = _repo_path(repo_root, root)
        relatives.update(
            path.relative_to(repo_root).as_posix()
            for path in directory.rglob("*.py")
        )
    result = {}
    for relative in sorted(relatives):
        path = _repo_path(repo_root, relative)
        if path.is_file():
            result[relative] = sha256_file(path)
    return result


def _current_public_symbols(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise ClosureError(f"cannot parse changed source {path}: {exc}") from exc
    return sorted(
        node.name
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    )


def _drift_inventory(
    repo_root: Path,
    basis: Mapping[str, str | None],
    current: Mapping[str, str | None],
    *,
    change_reviews: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    overrides = dict(change_reviews or {})
    entries = []
    counts = {"added": 0, "modified": 0, "deleted": 0, "unchanged": 0}
    for path in sorted(set(basis) | set(current)):
        status = (
            "added"
            if path not in basis
            else "deleted"
            if path not in current
            else "modified"
            if basis[path] != current[path]
            else "unchanged"
        )
        counts[status] += 1
        if status == "unchanged":
            continue
        source = _repo_path(repo_root, path)
        override = dict(overrides.get(path, {}))
        disposition = override.get(
            "current_change_disposition", CURRENT_CHANGE_DISPOSITIONS.get(path)
        )
        proof_tests = override.get("proof_tests", PROOF_TESTS.get(path, []))
        entries.append(
            {
                "path": path,
                "basis_hash": basis.get(path),
                "current_hash": current.get(path),
                "status": status,
                "current_public_symbols": (
                    _current_public_symbols(source) if source.is_file() else []
                ),
                "current_change_disposition": disposition,
                "proof_tests": list(proof_tests),
            }
        )
    if any(entry["current_change_disposition"] not in CHANGE_DISPOSITIONS for entry in entries):
        raise ClosureError("one or more current source changes lack a reviewed disposition")
    return {"counts": counts, "entries": entries}


def drift_inventory(repo_root: Path, gate: Mapping[str, Any]) -> dict[str, Any]:
    """Bootstrap drift against the immutable historical campaign source map."""
    return _drift_inventory(
        repo_root,
        dict(gate["source_hashes"]),
        historical_source_hashes(repo_root, gate),
    )


def renewal_drift_inventory(
    repo_root: Path,
    basis_modules: Sequence[Mapping[str, Any]],
    current_modules: Sequence[Mapping[str, Any]],
    *,
    change_reviews: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    basis = {
        item["path"]: item.get("sha256")
        for item in basis_modules
        if item.get("dependency_disposition") != "unrelated_exempt"
    }
    current = {
        item["path"]: item.get("sha256")
        for item in current_modules
        if item.get("dependency_disposition") != "unrelated_exempt"
    }
    return _drift_inventory(
        repo_root,
        basis,
        current,
        change_reviews=change_reviews,
    )


def dependency_drift(
    basis_edges: Sequence[Mapping[str, Any]],
    current_edges: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    def key(edge: Mapping[str, Any]) -> tuple[str, str, str]:
        return (edge["importer"], edge["imported"], edge["edge_kind"])

    basis = {key(edge) for edge in basis_edges}
    current = {key(edge) for edge in current_edges}
    return {
        "added": [
            {"importer": item[0], "imported": item[1], "edge_kind": item[2]}
            for item in sorted(current - basis)
        ],
        "removed": [
            {"importer": item[0], "imported": item[1], "edge_kind": item[2]}
            for item in sorted(basis - current)
        ],
    }


def _path_to_module(relative: str) -> str:
    parts = Path(relative).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _module_to_path(repo_root: Path, module: str) -> str | None:
    if not module.startswith("isar"):
        return None
    stem = Path(*module.split("."))
    candidates = (stem.with_suffix(".py"), stem / "__init__.py")
    for candidate in candidates:
        if (repo_root / candidate).is_file():
            return candidate.as_posix()
    return None


def _relative_module(current_path: str, module: str | None, level: int) -> str:
    if level == 0:
        return module or ""
    current_module = _path_to_module(current_path)
    package = current_module if current_path.endswith("/__init__.py") else current_module.rpartition(".")[0]
    parts = package.split(".") if package else []
    if level > 0:
        if level - 1 > len(parts):
            raise ClosureError(f"unresolved relative import in {current_path}")
        parts = parts[: len(parts) - (level - 1)]
    if module:
        parts.extend(module.split("."))
    return ".".join(parts)


def _imports_from_source(repo_root: Path, relative: str) -> list[dict[str, str]]:
    path = _repo_path(repo_root, relative)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise ClosureError(f"cannot parse dependency source {relative}: {exc}") from exc
    edges: dict[tuple[str, str], str] = {}
    priority = {
        "runtime": 0,
        "conditional": 1,
        "type_only": 2,
        "package_reexport": 3,
    }

    def record(imported: str, kind: str) -> None:
        imported_path = _module_to_path(repo_root, imported)
        if imported_path is None:
            return
        actual_kind = "package_reexport" if relative.endswith("/__init__.py") else kind
        key = (relative, imported_path)
        prior = edges.get(key)
        if prior is None or priority[actual_kind] < priority[prior]:
            edges[key] = actual_kind

    def visit(statements: Iterable[ast.stmt], kind: str = "runtime") -> None:
        for node in statements:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    record(alias.name, kind)
            elif isinstance(node, ast.ImportFrom):
                base = _relative_module(relative, node.module, node.level)
                base_path = _module_to_path(repo_root, base)
                for alias in node.names:
                    candidate = f"{base}.{alias.name}" if base else alias.name
                    if _module_to_path(repo_root, candidate) is not None:
                        record(candidate, kind)
                    elif base_path is not None:
                        record(base, kind)
                    elif node.level > 0:
                        raise ClosureError(
                            f"unresolved repository-relative import {candidate} in {relative}"
                        )
            elif isinstance(node, ast.If):
                type_only = (
                    isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING"
                ) or (
                    isinstance(node.test, ast.Attribute)
                    and node.test.attr == "TYPE_CHECKING"
                )
                child_kind = "type_only" if type_only else "conditional"
                visit(node.body, child_kind)
                visit(node.orelse, child_kind)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(node.body, "conditional")
            elif isinstance(node, (ast.Try, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)):
                visit(getattr(node, "body", []), "conditional")
                visit(getattr(node, "orelse", []), "conditional")
                visit(getattr(node, "finalbody", []), "conditional")
                for handler in getattr(node, "handlers", []):
                    visit(handler.body, "conditional")
    visit(tree.body)
    return [
        {"importer": importer, "imported": imported, "edge_kind": kind}
        for (importer, imported), kind in sorted(edges.items())
    ]


def _module_policy(path: str, historical_paths: set[str]) -> dict[str, Any]:
    if path in SELECTION_CRITICAL:
        return {
            "module_role": "selection_critical",
            "dependency_disposition": "protected_semantic",
            "rationale": "Owns production candidate, fit, criterion, weighting, or selection/delivery wiring semantics.",
        }
    if path in EVIDENCE_CRITICAL:
        return {
            "module_role": "evidence_critical",
            "dependency_disposition": "protected_semantic",
            "rationale": "Owns predictive/model evidence identity, authentication, or retained-evidence construction.",
        }
    if path in EXPLICIT_EXEMPT:
        return {
            "module_role": "unrelated",
            "dependency_disposition": "unrelated_exempt",
            "rationale": EXPLICIT_EXEMPT[path],
        }
    if path in EXPLICIT_ADJACENT or path in historical_paths:
        return {
            "module_role": "campaign_runtime_dependency",
            "dependency_disposition": "protected_adjacent",
            "rationale": "Reached by the frozen campaign/runtime path but does not own accepted selection or evidence semantics.",
        }
    return {
        "module_role": "campaign_runtime_dependency",
        "dependency_disposition": "protected_adjacent",
        "rationale": "Transitively discovered local runtime dependency; conservatively hashed pending a separately reviewed narrower proof.",
    }


def dependency_closure(
    repo_root: Path,
    gate: Mapping[str, Any],
    *,
    basis_modules: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    historical_paths = set(gate["source_hashes"])
    if basis_modules is None:
        initial_paths = (
            historical_paths
            | set(EXPLICIT_EXEMPT)
            | EXPLICIT_ADJACENT
            | EVIDENCE_CRITICAL
            | SELECTION_CRITICAL
        )
        policies: dict[str, dict[str, Any]] = {
            path: _module_policy(path, historical_paths)
            for path in sorted(initial_paths)
            if (repo_root / path).is_file()
        }
    else:
        policies = {
            item["path"]: {
                "module_role": item["module_role"],
                "dependency_disposition": item["dependency_disposition"],
                "rationale": item["rationale"],
            }
            for item in basis_modules
            if (repo_root / item["path"]).is_file()
        }
    for path in DEPENDENCY_ENTRYPOINTS:
        if (repo_root / path).is_file():
            policies.setdefault(path, _module_policy(path, historical_paths))
    # Conservative closure: every protected module is parsed, not merely the
    # modules reached from the public entrypoints.  New local imports from any
    # hashed source therefore enter the review graph.
    queue = sorted(
        path
        for path, policy in policies.items()
        if policy["dependency_disposition"] != "unrelated_exempt"
    )
    visited: set[str] = set()
    edges: dict[tuple[str, str], str] = {}
    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        if not (repo_root / current).is_file():
            raise ClosureError(f"dependency entrypoint is missing: {current}")
        visited.add(current)
        policy = policies.setdefault(current, _module_policy(current, historical_paths))
        if policy["dependency_disposition"] == "unrelated_exempt":
            continue
        for edge in _imports_from_source(repo_root, current):
            key = (edge["importer"], edge["imported"])
            edges[key] = edge["edge_kind"]
            imported = edge["imported"]
            imported_policy = policies.setdefault(
                imported, _module_policy(imported, historical_paths)
            )
            if imported_policy["dependency_disposition"] != "unrelated_exempt":
                queue.append(imported)
    for edge in MANUAL_DEPENDENCIES:
        _exact_keys(edge, {"importer", "imported", "edge_kind"}, "manual dependency")
        if edge["edge_kind"] != "manually_declared_dynamic":
            raise ClosureError("manual dependency must use manually_declared_dynamic")
        importer = edge["importer"]
        imported = edge["imported"]
        if not (repo_root / importer).is_file() or not (repo_root / imported).is_file():
            raise ClosureError(f"manual dependency path is missing: {importer} -> {imported}")
        edges[(importer, imported)] = edge["edge_kind"]
        policies.setdefault(importer, _module_policy(importer, historical_paths))
        policies.setdefault(imported, _module_policy(imported, historical_paths))
    modules = []
    for path, policy in sorted(policies.items()):
        disposition = policy["dependency_disposition"]
        modules.append(
            {
                "path": path,
                **policy,
                "sha256": (
                    None
                    if disposition == "unrelated_exempt"
                    else source_content_sha256(repo_root / path)
                ),
            }
        )
    edge_records = [
        {"importer": importer, "imported": imported, "edge_kind": kind}
        for (importer, imported), kind in sorted(edges.items())
    ]
    return modules, edge_records


def _source_fingerprint(modules: Sequence[Mapping[str, Any]]) -> str:
    payload = [
        {
            "path": module["path"],
            "module_role": module["module_role"],
            "dependency_disposition": module["dependency_disposition"],
            "sha256": module["sha256"],
        }
        for module in modules
        if module["dependency_disposition"] != "unrelated_exempt"
    ]
    return semantics_fingerprint("isar.stat2b_production_sources/1", payload)


def _graph_fingerprint(
    modules: Sequence[Mapping[str, Any]], edges: Sequence[Mapping[str, Any]]
) -> str:
    payload = {
        "entrypoints": list(DEPENDENCY_ENTRYPOINTS),
        "modules": [
            {
                "path": module["path"],
                "dependency_disposition": module["dependency_disposition"],
            }
            for module in modules
        ],
        "edges": list(edges),
        "manual_dependencies": list(MANUAL_DEPENDENCIES),
    }
    return semantics_fingerprint("isar.stat2b_dependency_graph/2", payload)


def _review_fingerprint(review: Mapping[str, Any]) -> str:
    payload = dict(review)
    payload.pop("review_record_fingerprint", None)
    return semantics_fingerprint(REVIEW_SCHEMA, payload)


def _candidate_fingerprint(manifest: Mapping[str, Any]) -> str:
    excluded = {
        "status",
        "owner_approved",
        "approved_by",
        "approved_on",
        "candidate_artifact_commit",
        "candidate_artifact_sha256",
        "approved_production_anchor",
        "approved_candidate_fingerprint",
        "candidate_closure_fingerprint",
    }
    payload = {key: value for key, value in manifest.items() if key not in excluded}
    return semantics_fingerprint("isar.stat2b_production_source_closure_candidate/2", payload)


def build_candidate(
    repo_root: Path,
    *,
    proposed_production_anchor: str,
    review_path: str,
    candidate_path: str = DEFAULT_CANDIDATE_PATH,
    verification_evidence: Mapping[str, Any] | None = None,
    change_reviews: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not _is_commit_sha(proposed_production_anchor):
        raise ClosureError("proposed production anchor must be a 40-character Git SHA")
    if _repo_path(repo_root, candidate_path) == _repo_path(repo_root, DEFAULT_ACTIVE_PATH):
        raise ClosureError("candidate output cannot replace the active closure")
    gate = authenticate_historical_gate(repo_root)
    replay = replay_heldout_aggregate(repo_root, gate)
    active_path = _repo_path(repo_root, DEFAULT_ACTIVE_PATH)
    if active_path.exists():
        active_result = verify_manifest(
            repo_root,
            DEFAULT_ACTIVE_PATH,
            candidate=False,
            compare_current=False,
        )
        if not active_result.ok or active_result.manifest is None:
            raise ClosureError(
                f"active closure cannot be authenticated for renewal: {active_result.findings}"
            )
        basis_manifest = active_result.manifest
        active_git = git_provenance(repo_root, basis_manifest)
        if not active_git.ok:
            raise ClosureError(
                f"active closure Git provenance failed for renewal: {active_git.findings}"
            )
        if _repo_path(repo_root, review_path) == _repo_path(
            repo_root, basis_manifest["review_record"]["path"]
        ):
            raise ClosureError("renewal review path must be new and versioned")
        cycle_mode = "renewal"
        modules, edges = dependency_closure(
            repo_root,
            gate,
            basis_modules=basis_manifest["modules"],
        )
        drift = renewal_drift_inventory(
            repo_root,
            basis_manifest["modules"],
            modules,
            change_reviews=change_reviews,
        )
        graph_drift = dependency_drift(basis_manifest["dependency_edges"], edges)
        comparison_basis = {
            "mode": cycle_mode,
            "manifest_path": DEFAULT_ACTIVE_PATH,
            "candidate_closure_fingerprint": basis_manifest[
                "candidate_closure_fingerprint"
            ],
            "production_source_fingerprint": basis_manifest[
                "production_source_fingerprint"
            ],
            "dependency_graph_fingerprint": basis_manifest[
                "dependency_graph_fingerprint"
            ],
        }
    else:
        basis_manifest = None
        cycle_mode = "bootstrap"
        modules, edges = dependency_closure(repo_root, gate)
        drift = drift_inventory(repo_root, gate)
        graph_drift = {"added": [], "removed": []}
        comparison_basis = {
            "mode": cycle_mode,
            "manifest_path": HISTORICAL_GATE_PATH,
            "candidate_closure_fingerprint": None,
            "production_source_fingerprint": HISTORICAL_SOURCE_FINGERPRINT,
            "dependency_graph_fingerprint": None,
        }
    source_fingerprint = _source_fingerprint(modules)
    graph_fingerprint = _graph_fingerprint(modules, edges)
    verification = dict(verification_evidence or {})
    review = {
        "schema": REVIEW_SCHEMA,
        "cycle_mode": cycle_mode,
        "proposed_production_anchor": proposed_production_anchor,
        "comparison_basis": comparison_basis,
        "historical_gate": {
            "path": HISTORICAL_GATE_PATH,
            "file_sha256": HISTORICAL_GATE_SHA256,
            "source_fingerprint": HISTORICAL_SOURCE_FINGERPRINT,
            "gate_fingerprint": HISTORICAL_GATE_FINGERPRINT,
        },
        "drift_inventory": drift,
        "dependency_drift": graph_drift,
        "module_summary": {
            disposition: sum(
                module["dependency_disposition"] == disposition for module in modules
            )
            for disposition in sorted(DEPENDENCY_DISPOSITIONS)
        },
        "modules": modules,
        "dependency_entrypoints": list(DEPENDENCY_ENTRYPOINTS),
        "dependency_edges": edges,
        "manual_dependencies": list(MANUAL_DEPENDENCIES),
        "dependency_graph_fingerprint": graph_fingerprint,
        "production_source_fingerprint": source_fingerprint,
        "aggregate_replay": replay,
        "accepted_disposition": {
            "decision_id": "S8",
            "production_criterion": "AIC",
            "aicc_switch_authorized": False,
            "stat2c_authorized": False,
            "difficult_regimes": "open",
        },
        "verification_evidence": verification,
    }
    review["review_record_fingerprint"] = _review_fingerprint(review)
    review_target = _repo_path(repo_root, review_path)
    _write_json(review_target, review)
    manifest = {
        "schema": CLOSURE_SCHEMA,
        "status": "candidate",
        "owner_approved": False,
        "approved_by": None,
        "approved_on": None,
        "cycle_mode": cycle_mode,
        "comparison_basis": comparison_basis,
        "proposed_production_anchor": proposed_production_anchor,
        "approved_production_anchor": None,
        "candidate_artifact_commit": None,
        "candidate_artifact_path": candidate_path,
        "candidate_artifact_sha256": None,
        "approved_candidate_fingerprint": None,
        "accepted_disposition": review["accepted_disposition"],
        "historical_evidence": {
            "gate_path": HISTORICAL_GATE_PATH,
            "gate_file_sha256": HISTORICAL_GATE_SHA256,
            "heldout_specification_fingerprint": HELDOUT_SPEC_FINGERPRINT,
            "development_summary_fingerprint": DEVELOPMENT_SUMMARY_FINGERPRINT,
            "heldout_summary_fingerprint": HELDOUT_SUMMARY_FINGERPRINT,
            "heldout_summary_sha256": HELDOUT_SUMMARY_SHA256,
            "heldout_run_fingerprint": HELDOUT_RUN_FINGERPRINT,
            "historical_campaign_source_fingerprint": HISTORICAL_SOURCE_FINGERPRINT,
            "historical_gate_fingerprint": HISTORICAL_GATE_FINGERPRINT,
            "historical_evaluator_path": HISTORICAL_EVALUATOR_PATH,
            "historical_evaluator_sha256": HISTORICAL_EVALUATOR_SHA256,
            "aggregate_replay_fingerprint": replay["replay_fingerprint"],
        },
        "boundary_revision": BOUNDARY_REVISION,
        "modules": modules,
        "dependency_entrypoints": list(DEPENDENCY_ENTRYPOINTS),
        "dependency_edges": edges,
        "manual_dependencies": list(MANUAL_DEPENDENCIES),
        "dependency_graph_fingerprint": graph_fingerprint,
        "dependency_drift": graph_drift,
        "production_source_fingerprint": source_fingerprint,
        "current_change_reviews": drift["entries"],
        "review_record": {
            "path": review_path,
            "sha256": sha256_file(review_target),
            "semantic_fingerprint": review["review_record_fingerprint"],
        },
    }
    manifest["candidate_closure_fingerprint"] = _candidate_fingerprint(manifest)
    _write_json(_repo_path(repo_root, candidate_path), manifest)
    return manifest, review


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        raise ClosureError(f"{label} fields are invalid; missing={missing}, extra={extra}")


def _validate_manifest_shape(manifest: Mapping[str, Any], *, candidate: bool) -> None:
    _exact_keys(
        manifest,
        {
            "schema", "status", "owner_approved", "approved_by", "approved_on",
            "cycle_mode", "comparison_basis", "proposed_production_anchor",
            "approved_production_anchor", "candidate_artifact_commit",
            "candidate_artifact_path", "candidate_artifact_sha256",
            "approved_candidate_fingerprint", "accepted_disposition",
            "historical_evidence", "boundary_revision", "modules",
            "dependency_entrypoints", "dependency_edges", "manual_dependencies",
            "dependency_graph_fingerprint", "dependency_drift",
            "production_source_fingerprint",
            "current_change_reviews", "review_record", "candidate_closure_fingerprint",
        },
        "closure manifest",
    )
    if not isinstance(manifest.get("historical_evidence"), Mapping):
        raise ClosureError("closure manifest historical evidence is invalid")
    _exact_keys(
        manifest["historical_evidence"],
        {
            "gate_path", "gate_file_sha256", "heldout_specification_fingerprint",
            "development_summary_fingerprint", "heldout_summary_fingerprint",
            "heldout_summary_sha256", "heldout_run_fingerprint",
            "historical_campaign_source_fingerprint",
            "historical_gate_fingerprint", "historical_evaluator_path",
            "historical_evaluator_sha256", "aggregate_replay_fingerprint",
        },
        "historical evidence",
    )
    if not isinstance(manifest.get("comparison_basis"), Mapping):
        raise ClosureError("closure comparison basis is invalid")
    _exact_keys(
        manifest["comparison_basis"],
        {
            "mode", "manifest_path", "candidate_closure_fingerprint",
            "production_source_fingerprint", "dependency_graph_fingerprint",
        },
        "comparison basis",
    )
    if not isinstance(manifest.get("review_record"), Mapping):
        raise ClosureError("closure manifest review reference is invalid")
    _exact_keys(
        manifest["review_record"],
        {"path", "sha256", "semantic_fingerprint"},
        "review reference",
    )
    if not isinstance(manifest.get("modules"), list):
        raise ClosureError("closure manifest modules are invalid")
    for module in manifest["modules"]:
        if not isinstance(module, Mapping):
            raise ClosureError("closure module record is invalid")
        _exact_keys(
            module,
            {"path", "module_role", "dependency_disposition", "sha256", "rationale"},
            "closure module",
        )
    _validate_edge_records(manifest.get("dependency_edges"), "closure dependency edges")
    if not isinstance(manifest.get("manual_dependencies"), list):
        raise ClosureError("closure manual dependencies are invalid")
    for edge in manifest["manual_dependencies"]:
        if not isinstance(edge, Mapping):
            raise ClosureError("manual dependency record is invalid")
        _exact_keys(edge, {"importer", "imported", "edge_kind"}, "manual dependency")
        if edge["edge_kind"] != "manually_declared_dynamic":
            raise ClosureError("manual dependency edge kind is invalid")
    if not isinstance(manifest.get("current_change_reviews"), list):
        raise ClosureError("current change reviews are invalid")
    for change in manifest["current_change_reviews"]:
        if not isinstance(change, Mapping):
            raise ClosureError("current change review record is invalid")
        _exact_keys(
            change,
            {
                "path", "basis_hash", "current_hash", "status",
                "current_public_symbols", "current_change_disposition", "proof_tests",
            },
            "current change review",
        )
    _validate_dependency_drift(manifest.get("dependency_drift"))
    if manifest["schema"] != CLOSURE_SCHEMA:
        raise ClosureError("closure manifest schema is unsupported")
    if manifest["cycle_mode"] not in {"bootstrap", "renewal"}:
        raise ClosureError("closure cycle mode is invalid")
    if manifest["comparison_basis"].get("mode") != manifest["cycle_mode"]:
        raise ClosureError("comparison basis mode differs from closure cycle")
    basis = manifest["comparison_basis"]
    if manifest["cycle_mode"] == "bootstrap":
        if basis != {
            "mode": "bootstrap",
            "manifest_path": HISTORICAL_GATE_PATH,
            "candidate_closure_fingerprint": None,
            "production_source_fingerprint": HISTORICAL_SOURCE_FINGERPRINT,
            "dependency_graph_fingerprint": None,
        }:
            raise ClosureError("bootstrap comparison basis changed")
    else:
        if basis.get("manifest_path") != DEFAULT_ACTIVE_PATH or any(
            not isinstance(basis.get(field), str)
            or re.fullmatch(r"[0-9a-f]{64}", basis[field]) is None
            for field in (
                "candidate_closure_fingerprint",
                "production_source_fingerprint",
                "dependency_graph_fingerprint",
            )
        ):
            raise ClosureError("renewal comparison basis is invalid")
    if not _is_commit_sha(manifest["proposed_production_anchor"]):
        raise ClosureError("proposed production anchor is invalid")
    artifact_path = manifest.get("candidate_artifact_path")
    if (
        not isinstance(artifact_path, str)
        or not artifact_path.endswith(".candidate.json")
        or artifact_path == DEFAULT_ACTIVE_PATH
    ):
        raise ClosureError("candidate artifact path is invalid")
    expected_status = "candidate" if candidate else "active"
    if manifest["status"] != expected_status:
        raise ClosureError(f"closure manifest is not {expected_status}")
    if candidate:
        if manifest["owner_approved"] is not False or any(
            manifest[field] is not None
            for field in (
                "approved_by", "approved_on", "approved_production_anchor",
                "candidate_artifact_commit", "candidate_artifact_sha256",
                "approved_candidate_fingerprint",
            )
        ):
            raise ClosureError("candidate manifest contains owner approval")
    else:
        if manifest["owner_approved"] is not True:
            raise ClosureError("active closure is not owner approved")
        if manifest["approved_by"] != APPROVED_OWNER:
            raise ClosureError("active closure approver is not the repository owner")
        try:
            dt.date.fromisoformat(manifest["approved_on"])
        except (TypeError, ValueError) as exc:
            raise ClosureError("active closure approval date is invalid") from exc
        if manifest["approved_production_anchor"] != manifest[
            "proposed_production_anchor"
        ]:
            raise ClosureError("active approval changed the proposed production anchor")
        if not _is_commit_sha(manifest["candidate_artifact_commit"]):
            raise ClosureError("active closure candidate artifact commit is invalid")
        if not isinstance(manifest["candidate_artifact_sha256"], str) or not re.fullmatch(
            r"[0-9a-f]{64}", manifest["candidate_artifact_sha256"]
        ):
            raise ClosureError("active closure candidate artifact hash is invalid")
        if manifest["approved_candidate_fingerprint"] != manifest["candidate_closure_fingerprint"]:
            raise ClosureError("active approval does not bind the reviewed candidate")
    if manifest["boundary_revision"] != BOUNDARY_REVISION:
        raise ClosureError("dependency boundary revision changed")
    if manifest["accepted_disposition"] != {
        "decision_id": "S8",
        "production_criterion": "AIC",
        "aicc_switch_authorized": False,
        "stat2c_authorized": False,
        "difficult_regimes": "open",
    }:
        raise ClosureError("S8 accepted disposition changed")


def _validate_edge_records(value: Any, label: str) -> None:
    if not isinstance(value, list):
        raise ClosureError(f"{label} are invalid")
    for edge in value:
        if not isinstance(edge, Mapping):
            raise ClosureError(f"{label} contain an invalid record")
        _exact_keys(edge, {"importer", "imported", "edge_kind"}, label)
        if edge["edge_kind"] not in EDGE_KINDS:
            raise ClosureError(f"{label} contain an invalid edge kind")


def _validate_dependency_drift(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ClosureError("dependency drift is invalid")
    _exact_keys(value, {"added", "removed"}, "dependency drift")
    _validate_edge_records(value["added"], "added dependency edges")
    _validate_edge_records(value["removed"], "removed dependency edges")


def _expected_historical_evidence(replay: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "gate_path": HISTORICAL_GATE_PATH,
        "gate_file_sha256": HISTORICAL_GATE_SHA256,
        "heldout_specification_fingerprint": HELDOUT_SPEC_FINGERPRINT,
        "development_summary_fingerprint": DEVELOPMENT_SUMMARY_FINGERPRINT,
        "heldout_summary_fingerprint": HELDOUT_SUMMARY_FINGERPRINT,
        "heldout_summary_sha256": HELDOUT_SUMMARY_SHA256,
        "heldout_run_fingerprint": HELDOUT_RUN_FINGERPRINT,
        "historical_campaign_source_fingerprint": HISTORICAL_SOURCE_FINGERPRINT,
        "historical_gate_fingerprint": HISTORICAL_GATE_FINGERPRINT,
        "historical_evaluator_path": HISTORICAL_EVALUATOR_PATH,
        "historical_evaluator_sha256": HISTORICAL_EVALUATOR_SHA256,
        "aggregate_replay_fingerprint": replay["replay_fingerprint"],
    }


def _validate_review_shape(review: Mapping[str, Any]) -> None:
    _exact_keys(
        review,
        {
            "schema", "cycle_mode", "proposed_production_anchor", "comparison_basis",
            "historical_gate", "drift_inventory", "dependency_drift", "module_summary",
            "modules", "dependency_entrypoints", "dependency_edges",
            "manual_dependencies", "dependency_graph_fingerprint",
            "production_source_fingerprint", "aggregate_replay", "accepted_disposition",
            "verification_evidence", "review_record_fingerprint",
        },
        "review record",
    )
    if not isinstance(review.get("historical_gate"), Mapping):
        raise ClosureError("review historical gate record is invalid")
    _exact_keys(
        review["historical_gate"],
        {"path", "file_sha256", "source_fingerprint", "gate_fingerprint"},
        "review historical gate",
    )
    if not isinstance(review.get("drift_inventory"), Mapping):
        raise ClosureError("review drift inventory is invalid")
    _exact_keys(review["drift_inventory"], {"counts", "entries"}, "review drift inventory")
    _validate_dependency_drift(review.get("dependency_drift"))


def verify_manifest(
    repo_root: Path,
    manifest_path: str,
    *,
    candidate: bool = False,
    compare_current: bool = True,
) -> VerificationResult:
    findings: list[dict[str, Any]] = []
    try:
        gate = authenticate_historical_gate(repo_root)
        replay = replay_heldout_aggregate(repo_root, gate)
        manifest = _read_json(_repo_path(repo_root, manifest_path))
        _validate_manifest_shape(manifest, candidate=candidate)
        if candidate and manifest["candidate_artifact_path"] != manifest_path:
            raise ClosureError("candidate manifest path differs from its artifact path")
        if candidate and manifest["cycle_mode"] == "renewal":
            active_result = verify_manifest(
                repo_root,
                DEFAULT_ACTIVE_PATH,
                candidate=False,
                compare_current=False,
            )
            if not active_result.ok or active_result.manifest is None:
                raise ClosureError("renewal candidate has no authenticated active basis")
            active = active_result.manifest
            expected_basis = {
                "mode": "renewal",
                "manifest_path": DEFAULT_ACTIVE_PATH,
                "candidate_closure_fingerprint": active[
                    "candidate_closure_fingerprint"
                ],
                "production_source_fingerprint": active[
                    "production_source_fingerprint"
                ],
                "dependency_graph_fingerprint": active[
                    "dependency_graph_fingerprint"
                ],
            }
            if manifest["comparison_basis"] != expected_basis:
                raise ClosureError("renewal candidate comparison basis changed")
        if manifest["historical_evidence"] != _expected_historical_evidence(replay):
            raise ClosureError("closure manifest historical evidence changed")
        if manifest["dependency_entrypoints"] != list(DEPENDENCY_ENTRYPOINTS):
            raise ClosureError("closure dependency entrypoints changed")
        if manifest["manual_dependencies"] != list(MANUAL_DEPENDENCIES):
            raise ClosureError("closure manual dependency declarations changed")
        if manifest["candidate_closure_fingerprint"] != _candidate_fingerprint(manifest):
            raise ClosureError("candidate closure fingerprint does not reproduce")
        modules = manifest["modules"]
        if not isinstance(modules, list) or not modules:
            raise ClosureError("closure manifest has no modules")
        if [item.get("path") for item in modules] != sorted(item.get("path") for item in modules):
            raise ClosureError("closure modules are not deterministically ordered")
        for module in modules:
            role = module.get("module_role")
            disposition = module.get("dependency_disposition")
            if role not in MODULE_ROLES or disposition not in DEPENDENCY_DISPOSITIONS:
                raise ClosureError(f"invalid module classification: {module.get('path')}")
            path = _repo_path(repo_root, module["path"])
            if disposition == "unrelated_exempt":
                if module.get("sha256") is not None:
                    raise ClosureError(f"unrelated exemption is unexpectedly hashed: {module['path']}")
            elif compare_current and not path.is_file():
                findings.append(
                    {
                        "code": "protected_source_missing",
                        "path": module["path"],
                        "category": disposition,
                        "message": f"protected source is missing: {module['path']}",
                        "required_response": (
                            "restore or explicitly re-review the semantic boundary with proportionate evidence"
                            if disposition == "protected_semantic"
                            else "restore or explicitly re-review the adjacent boundary with focused parity"
                        ),
                    }
                )
            elif compare_current and source_content_sha256(path) != module.get("sha256"):
                findings.append(
                    {
                        "code": "protected_hash_mismatch",
                        "path": module["path"],
                        "category": disposition,
                        "required_response": (
                            "semantic review plus proportionate STAT-2b evidence"
                            if disposition == "protected_semantic"
                            else "bounded source review plus focused parity"
                        ),
                    }
                )
        if compare_current:
            current_modules, current_edges = dependency_closure(
                repo_root,
                gate,
                basis_modules=modules,
            )
            if current_edges != manifest["dependency_edges"]:
                findings.append(
                    {
                        "code": "dependency_graph_changed",
                        "required_response": "classify every new or removed local dependency edge",
                    }
                )
            manifest_paths = {item["path"] for item in modules}
            unclassified = sorted(
                item["path"] for item in current_modules if item["path"] not in manifest_paths
            )
            if unclassified:
                findings.append(
                    {
                        "code": "unclassified_dependencies",
                        "paths": unclassified,
                        "required_response": "classify and review before candidate promotion",
                    }
                )
        if _source_fingerprint(modules) != manifest["production_source_fingerprint"]:
            raise ClosureError("production source fingerprint does not reproduce")
        if _graph_fingerprint(modules, manifest["dependency_edges"]) != manifest[
            "dependency_graph_fingerprint"
        ]:
            raise ClosureError("dependency graph fingerprint does not reproduce")
        review_ref = manifest["review_record"]
        review_path = _repo_path(repo_root, review_ref["path"])
        if sha256_file(review_path) != review_ref["sha256"]:
            raise ClosureError("review record byte hash changed")
        review = _read_json(review_path)
        _validate_review_shape(review)
        if review.get("schema") != REVIEW_SCHEMA:
            raise ClosureError("review record schema is unsupported")
        if _review_fingerprint(review) != review.get("review_record_fingerprint"):
            raise ClosureError("review record semantic fingerprint does not reproduce")
        if review["review_record_fingerprint"] != review_ref["semantic_fingerprint"]:
            raise ClosureError("manifest does not authenticate the review record")
        if review.get("production_source_fingerprint") != manifest[
            "production_source_fingerprint"
        ]:
            raise ClosureError("review and manifest source fingerprints differ")
        if review.get("dependency_graph_fingerprint") != manifest[
            "dependency_graph_fingerprint"
        ]:
            raise ClosureError("review and manifest dependency fingerprints differ")
        if review.get("proposed_production_anchor") != manifest[
            "proposed_production_anchor"
        ]:
            raise ClosureError("review and manifest production anchors differ")
        if review.get("cycle_mode") != manifest["cycle_mode"]:
            raise ClosureError("review and manifest cycle modes differ")
        if review.get("comparison_basis") != manifest["comparison_basis"]:
            raise ClosureError("review and manifest comparison bases differ")
        if review.get("modules") != modules:
            raise ClosureError("review and manifest module records differ")
        if review.get("dependency_entrypoints") != manifest["dependency_entrypoints"]:
            raise ClosureError("review and manifest dependency entrypoints differ")
        if review.get("dependency_edges") != manifest["dependency_edges"]:
            raise ClosureError("review and manifest dependency edges differ")
        if review.get("manual_dependencies") != manifest["manual_dependencies"]:
            raise ClosureError("review and manifest manual dependencies differ")
        if review.get("drift_inventory", {}).get("entries") != manifest[
            "current_change_reviews"
        ]:
            raise ClosureError("review and manifest current-change records differ")
        if review.get("dependency_drift") != manifest["dependency_drift"]:
            raise ClosureError("review and manifest dependency drift differs")
        if review.get("aggregate_replay") != replay:
            raise ClosureError("review aggregate replay differs from authenticated evidence")
        if review.get("accepted_disposition") != manifest["accepted_disposition"]:
            raise ClosureError("review and manifest S8 disposition differ")
        if not candidate:
            verification = review.get("verification_evidence")
            if not isinstance(verification, dict) or verification.get("status") != "passed":
                raise ClosureError("active closure lacks completed verification evidence")
        status = "candidate_valid" if candidate else "active_valid"
    except ClosureError as exc:
        findings.append({"code": "authentication_failed", "message": str(exc)})
        return VerificationResult(False, "failed", tuple(findings), None)
    if findings:
        return VerificationResult(False, "failed", tuple(findings), manifest)
    return VerificationResult(True, status, (), manifest)


def git_provenance(
    repo_root: Path,
    manifest: Mapping[str, Any],
    *,
    release_anchor: str | None = None,
) -> VerificationResult:
    findings = []
    anchor = (
        manifest["approved_production_anchor"]
        if manifest["owner_approved"]
        else manifest["proposed_production_anchor"]
    )
    if release_anchor is not None and release_anchor != anchor:
        return VerificationResult(
            False,
            "failed",
            ({"code": "release_anchor_mismatch", "expected": anchor, "actual": release_anchor},),
            manifest,
        )
    if not manifest["owner_approved"] and manifest["cycle_mode"] == "renewal":
        active_result = verify_manifest(
            repo_root,
            DEFAULT_ACTIVE_PATH,
            candidate=False,
            compare_current=False,
        )
        if not active_result.ok or active_result.manifest is None:
            return VerificationResult(
                False,
                "failed",
                ({"code": "active_basis_authentication_failed"},),
                manifest,
            )
        active_provenance = git_provenance(repo_root, active_result.manifest)
        if not active_provenance.ok:
            return VerificationResult(
                False,
                "failed",
                tuple(
                    {
                        "code": "active_basis_provenance_failed",
                        "finding": finding,
                    }
                    for finding in active_provenance.findings
                    if finding.get("code") != "ancestry_report"
                ),
                manifest,
            )
    try:
        subprocess.run(
            ["git", "cat-file", "-e", f"{anchor}^{{commit}}"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        return VerificationResult(
            False,
            "failed",
            ({"code": "git_provenance_unavailable", "message": str(exc)},),
            manifest,
        )
    for module in manifest["modules"]:
        if module["dependency_disposition"] == "unrelated_exempt":
            continue
        try:
            blob = subprocess.run(
                ["git", "show", f"{anchor}:{module['path']}"],
                cwd=repo_root,
                check=True,
                capture_output=True,
            ).stdout
        except (OSError, subprocess.CalledProcessError) as exc:
            findings.append(
                {"code": "anchor_path_unavailable", "path": module["path"], "message": str(exc)}
            )
            continue
        if sha256_bytes(blob) != module["sha256"]:
            findings.append({"code": "anchor_content_mismatch", "path": module["path"]})
    if manifest["owner_approved"]:
        artifact_commit = manifest["candidate_artifact_commit"]
        artifact_path = manifest["candidate_artifact_path"]
        try:
            subprocess.run(
                ["git", "cat-file", "-e", f"{artifact_commit}^{{commit}}"],
                cwd=repo_root,
                check=True,
                capture_output=True,
                text=True,
            )
            candidate_blob = subprocess.run(
                ["git", "show", f"{artifact_commit}:{artifact_path}"],
                cwd=repo_root,
                check=True,
                capture_output=True,
            ).stdout
        except (OSError, subprocess.CalledProcessError) as exc:
            findings.append(
                {"code": "candidate_artifact_unavailable", "message": str(exc)}
            )
        else:
            if sha256_bytes(candidate_blob) != manifest["candidate_artifact_sha256"]:
                findings.append({"code": "candidate_artifact_hash_mismatch"})
            try:
                candidate_record = json.loads(candidate_blob.decode("utf-8"))
                if not isinstance(candidate_record, dict):
                    raise ValueError("candidate artifact is not an object")
                _validate_manifest_shape(candidate_record, candidate=True)
            except (UnicodeError, json.JSONDecodeError, ValueError, ClosureError) as exc:
                findings.append(
                    {"code": "candidate_artifact_invalid", "message": str(exc)}
                )
            else:
                if candidate_record["candidate_closure_fingerprint"] != _candidate_fingerprint(
                    candidate_record
                ):
                    findings.append({"code": "candidate_artifact_fingerprint_invalid"})
                if candidate_record["candidate_closure_fingerprint"] != manifest[
                    "approved_candidate_fingerprint"
                ]:
                    findings.append({"code": "candidate_artifact_fingerprint_mismatch"})
                if candidate_record["review_record"] != manifest["review_record"]:
                    findings.append({"code": "candidate_artifact_review_mismatch"})
                review_ref = candidate_record["review_record"]
                try:
                    review_blob = subprocess.run(
                        [
                            "git",
                            "show",
                            f"{artifact_commit}:{review_ref['path']}",
                        ],
                        cwd=repo_root,
                        check=True,
                        capture_output=True,
                    ).stdout
                except (OSError, subprocess.CalledProcessError) as exc:
                    findings.append(
                        {"code": "candidate_review_unavailable", "message": str(exc)}
                    )
                else:
                    if sha256_bytes(review_blob) != review_ref["sha256"]:
                        findings.append({"code": "candidate_review_hash_mismatch"})
                    try:
                        committed_review = json.loads(review_blob.decode("utf-8"))
                    except (UnicodeError, json.JSONDecodeError) as exc:
                        findings.append(
                            {"code": "candidate_review_invalid", "message": str(exc)}
                        )
                    else:
                        try:
                            review_fingerprint = _review_fingerprint(committed_review)
                        except ClosureError as exc:
                            findings.append(
                                {"code": "candidate_review_invalid", "message": str(exc)}
                            )
                        else:
                            if review_fingerprint != review_ref["semantic_fingerprint"]:
                                findings.append(
                                    {"code": "candidate_review_fingerprint_mismatch"}
                                )
    try:
        ancestry = subprocess.run(
            ["git", "merge-base", "--is-ancestor", anchor, "HEAD"],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
        ).returncode == 0
    except OSError as exc:
        findings.append({"code": "ancestry_unavailable", "message": str(exc)})
        ancestry = None
    findings.append({"code": "ancestry_report", "approved_anchor_is_ancestor": ancestry})
    hard = [finding for finding in findings if finding["code"] != "ancestry_report"]
    return VerificationResult(not hard, "git_provenance_checked", tuple(findings), manifest)
