"""STAT-2b.1 held-out evidence verifier.

Authenticates the frozen held-out gate and replays its aggregate so the preserved S8
decision cannot be silently substituted or tampered with. This is the surviving half of
the retired source-closure engine (S10, DECISIONS.md): the candidate/active closure
cycle, dependency-graph discovery, and promotion verification were deleted with the
closure regime — git history keeps them. It never grants owner approval and does not
import or execute any inspected ISAR source module.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence


REPLAY_SCHEMA = "isar.stat2b1_aggregate_replay/1"
CAMPAIGN_SOURCE_SCHEMA = "isar.stat2b_campaign_sources/1"
GATE_SCHEMA = "isar.stat2b_heldout_gate/1"
HELDOUT_SUMMARY_SCHEMA = "isar.stat2b_heldout_summary/1"

HISTORICAL_GATE_PATH = "tools/stats/stat2b1-heldout-gate.json"
HELDOUT_SUMMARY_PATH = (
    "docs/campaigns/2026-07-31_STAT2B1_HELDOUT/decision_summary.json"
)

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


class ClosureError(RuntimeError):
    """The held-out evidence cannot be authenticated."""


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
    # Same required content, hashed the way this module hashes every other module: the
    # evaluator carries no ``-text`` attribute, so a CRLF checkout changes its raw bytes
    # without changing a byte of source.  HISTORICAL_EVALUATOR_SHA256 is unchanged and still
    # the value frozen in the immutable held-out gate and aggregate, which record it over
    # canonical LF content.
    if source_content_sha256(evaluator) != HISTORICAL_EVALUATOR_SHA256:
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
