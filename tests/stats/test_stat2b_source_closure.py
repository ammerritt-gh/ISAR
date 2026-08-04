"""Focused contract tests for the STAT-2b.1 source-closure module."""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
ENGINE_PATH = ROOT / "tools" / "stats" / "_stat2b_source_closure.py"
SPEC = importlib.util.spec_from_file_location("stat2b_source_closure_test_engine", ENGINE_PATH)
assert SPEC is not None and SPEC.loader is not None
ENGINE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ENGINE
SPEC.loader.exec_module(ENGINE)


def _candidate() -> dict:
    return json.loads(
        (ROOT / ENGINE.DEFAULT_CANDIDATE_PATH).read_text(encoding="utf-8")
    )


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _closure_repo(tmp_path: Path) -> Path:
    """Copy exactly the portable content needed by the closure interface."""
    manifest = _candidate()
    relatives = {
        ENGINE.DEFAULT_CANDIDATE_PATH,
        manifest["review_record"]["path"],
        ENGINE.HISTORICAL_GATE_PATH,
        ENGINE.HELDOUT_SUMMARY_PATH,
        ENGINE.HISTORICAL_EVALUATOR_PATH,
        *[module["path"] for module in manifest["modules"]],
    }
    for relative in sorted(relatives):
        _copy_file(ROOT / relative, tmp_path / relative)
    return tmp_path


def _rewrite_json(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _initialize_git(repo: Path) -> str:
    _git(repo, "init")
    _git(repo, "config", "user.name", "Closure Test")
    _git(repo, "config", "user.email", "closure@example.invalid")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "production anchor")
    return _git(repo, "rev-parse", "HEAD")


def _promote_candidate(repo: Path, artifact_commit: str) -> dict:
    candidate_path = repo / ENGINE.DEFAULT_CANDIDATE_PATH
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    active = deepcopy(candidate)
    active.update(
        {
            "status": "active",
            "owner_approved": True,
            "approved_by": ENGINE.APPROVED_OWNER,
            "approved_on": "2026-08-04",
            "approved_production_anchor": candidate["proposed_production_anchor"],
            "candidate_artifact_commit": artifact_commit,
            "candidate_artifact_sha256": ENGINE.sha256_file(candidate_path),
            "approved_candidate_fingerprint": candidate[
                "candidate_closure_fingerprint"
            ],
        }
    )
    active_path = repo / ENGINE.DEFAULT_ACTIVE_PATH
    _rewrite_json(active_path, active)
    return active


def _approved_fixture(tmp_path: Path) -> tuple[Path, dict, str]:
    repo = _closure_repo(tmp_path)
    original = _candidate()
    (repo / ENGINE.DEFAULT_CANDIDATE_PATH).unlink()
    (repo / original["review_record"]["path"]).unlink()
    anchor = _initialize_git(repo)
    candidate, _ = ENGINE.build_candidate(
        repo,
        proposed_production_anchor=anchor,
        review_path="docs/campaigns/cycle-1/review.json",
        verification_evidence={"status": "passed", "commands": [], "parity_checks": []},
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "candidate artifact")
    artifact_commit = _git(repo, "rev-parse", "HEAD")
    active = _promote_candidate(repo, artifact_commit)
    assert candidate["candidate_closure_fingerprint"] == active[
        "approved_candidate_fingerprint"
    ]
    return repo, active, artifact_commit


def test_historical_gate_and_aggregate_replay_are_exact():
    gate = ENGINE.authenticate_historical_gate(ROOT)
    replay = ENGINE.replay_heldout_aggregate(ROOT, gate)

    assert ENGINE.sha256_file(ROOT / ENGINE.HISTORICAL_GATE_PATH) == (
        ENGINE.HISTORICAL_GATE_SHA256
    )
    assert replay["status"] == "authority_withheld"
    assert replay["aggregate_path"] == ENGINE.HELDOUT_SUMMARY_PATH
    assert replay["replay_fingerprint"] == ENGINE.EXPECTED_REPLAY_FINGERPRINT
    assert {record["rule_id"] for record in replay["failed_rules"]} == {
        "comparative_false_certification_noninferiority",
        "minimum_complete_comparison_rate",
        "missed_mode_noninferiority",
        "omega_rmse_noninferiority",
        "predictive_loss_superiority",
    }
    assert all(
        set(record)
        == {"rule_id", "metric", "scope", "observed_bound", "required_margin"}
        for record in replay["failed_rules"]
    )


def test_historical_replay_rejects_development_summary_substitution(tmp_path):
    repo = _closure_repo(tmp_path)
    development = (
        ROOT / "docs/assets/stat2b/2026-07-30-stat2b1/decision_summary.json"
    )
    heldout = repo / ENGINE.HELDOUT_SUMMARY_PATH
    heldout.write_bytes(development.read_bytes())

    gate = ENGINE.authenticate_historical_gate(repo)
    with pytest.raises(ENGINE.ClosureError, match="held-out aggregate bytes changed"):
        ENGINE.replay_heldout_aggregate(repo, gate)


def test_historical_replay_recomputes_the_complete_summary_fingerprint(
    tmp_path, monkeypatch
):
    repo = _closure_repo(tmp_path)
    heldout = repo / ENGINE.HELDOUT_SUMMARY_PATH
    summary = json.loads(heldout.read_text(encoding="utf-8"))
    summary["cell_summaries"][0]["n"] = 102
    _rewrite_json(heldout, summary)
    monkeypatch.setattr(ENGINE, "HELDOUT_SUMMARY_SHA256", ENGINE.sha256_file(heldout))

    gate = ENGINE.authenticate_historical_gate(repo)
    with pytest.raises(ENGINE.ClosureError, match="semantic fingerprint does not reproduce"):
        ENGINE.replay_heldout_aggregate(repo, gate)


def test_historical_drift_inventory_is_the_reviewed_three_three_zero():
    gate = ENGINE.authenticate_historical_gate(ROOT)
    inventory = ENGINE.drift_inventory(ROOT, gate)

    assert inventory["counts"] == {
        "added": 4,
        "modified": 3,
        "deleted": 0,
        "unchanged": 57,
    }
    assert {entry["path"] for entry in inventory["entries"]} == {
        "isar/drive/objective.py",
        "isar/schema/action_identity.py",
        "isar/schema/card.py",
        "isar/schema/eig_policy.py",
        "isar/schema/eig_realized.py",
        "isar/schema/eig_shadow.py",
        "isar/session/predictive_artifact.py",
    }
    dispositions = {
        entry["path"]: entry["current_change_disposition"]
        for entry in inventory["entries"]
    }
    assert dispositions["isar/session/predictive_artifact.py"] == (
        "evidence_contract_semantic"
    )
    assert set(dispositions.values()) == {
        "evidence_contract_semantic",
        "adjacent_nonsemantic",
        "unrelated_exempt",
    }
    assert dispositions["isar/schema/action_identity.py"] == "unrelated_exempt"
    assert dispositions["isar/schema/eig_shadow.py"] == "unrelated_exempt"
    assert dispositions["isar/schema/eig_realized.py"] == "unrelated_exempt"
    assert all("current_public_symbols" in entry for entry in inventory["entries"])
    assert all("changed_symbols" not in entry for entry in inventory["entries"])


def test_reusable_drift_engine_accepts_nonhistorical_counts(tmp_path):
    source = tmp_path / "isar/example.py"
    source.parent.mkdir(parents=True)
    source.write_text("def current():\n    return 1\n", encoding="utf-8")
    current_hash = ENGINE.source_content_sha256(source)
    inventory = ENGINE._drift_inventory(
        tmp_path,
        {},
        {"isar/example.py": current_hash},
        change_reviews={
            "isar/example.py": {
                "current_change_disposition": "adjacent_nonsemantic",
                "proof_tests": ["tests/example.py"],
            }
        },
    )
    assert inventory["counts"] == {
        "added": 1,
        "modified": 0,
        "deleted": 0,
        "unchanged": 0,
    }


def test_candidate_is_unapproved_and_reconstructs_portably():
    result = ENGINE.verify_manifest(
        ROOT, ENGINE.DEFAULT_CANDIDATE_PATH, candidate=True
    )
    assert result.ok, result.findings
    manifest = result.manifest
    assert manifest["status"] == "candidate"
    assert manifest["owner_approved"] is False
    assert manifest["approved_by"] is None
    assert manifest["approved_on"] is None
    assert manifest["candidate_artifact_commit"] is None
    assert manifest["candidate_artifact_sha256"] is None
    assert manifest["approved_production_anchor"] is None
    provenance = ENGINE.git_provenance(ROOT, manifest)
    assert provenance.ok, provenance.findings
    assert manifest["approved_candidate_fingerprint"] is None
    assert manifest["candidate_closure_fingerprint"] == ENGINE._candidate_fingerprint(
        manifest
    )


def test_candidate_cannot_be_read_as_an_active_approval():
    result = ENGINE.verify_manifest(
        ROOT, ENGINE.DEFAULT_CANDIDATE_PATH, candidate=False
    )
    assert not result.ok
    assert "not active" in result.findings[-1]["message"]


def test_dependency_graph_is_deterministic_and_contains_real_edges():
    gate = ENGINE.authenticate_historical_gate(ROOT)
    first = ENGINE.dependency_closure(ROOT, gate)
    second = ENGINE.dependency_closure(ROOT, gate)

    assert first == second
    modules, edges = first
    assert len(modules) == 88
    assert len(edges) == 192
    assert {
        (edge["importer"], edge["imported"])
        for edge in edges
    } >= {
        ("isar/session/predictive_artifact.py", "isar/predictive.py"),
        ("isar/analyze/shadow_selection.py", "isar/analyze/select.py"),
        ("isar/drive/objective.py", "isar/schema/eig_policy.py"),
    }
    policy = {module["path"]: module for module in modules}
    assert policy["isar/session/driver.py"]["dependency_disposition"] == (
        "protected_semantic"
    )
    assert policy["isar/drive/objective.py"]["dependency_disposition"] == (
        "protected_adjacent"
    )
    assert policy["isar/schema/action_identity.py"]["dependency_disposition"] == (
        "unrelated_exempt"
    )
    assert policy["isar/schema/eig_realized.py"]["dependency_disposition"] == (
        "unrelated_exempt"
    )
    assert policy["isar/predictive.py"]["module_role"] == "evidence_critical"


def test_unrelated_schema_addition_does_not_change_the_closure(tmp_path):
    repo = _closure_repo(tmp_path)
    addition = repo / "isar" / "schema" / "unrelated_example.py"
    addition.write_text("VALUE = 1\n", encoding="utf-8")

    result = ENGINE.verify_manifest(
        repo, ENGINE.DEFAULT_CANDIDATE_PATH, candidate=True
    )
    assert result.ok, result.findings


def test_new_import_from_protected_source_fails_unclassified(tmp_path):
    repo = _closure_repo(tmp_path)
    addition = repo / "isar" / "schema" / "unclassified_example.py"
    addition.write_text("VALUE = 1\n", encoding="utf-8")
    protected = repo / "isar" / "analyze" / "select.py"
    protected.write_text(
        protected.read_text(encoding="utf-8")
        + "\nimport isar.schema.unclassified_example\n",
        encoding="utf-8",
        newline="\n",
    )

    result = ENGINE.verify_manifest(
        repo, ENGINE.DEFAULT_CANDIDATE_PATH, candidate=True
    )
    assert not result.ok
    codes = {finding["code"] for finding in result.findings}
    assert {
        "protected_hash_mismatch",
        "dependency_graph_changed",
        "unclassified_dependencies",
    } <= codes


def test_every_protected_module_is_traversed_for_new_dependencies(tmp_path):
    repo = _closure_repo(tmp_path)
    manifest = _candidate()
    inbound = {edge["imported"] for edge in manifest["dependency_edges"]}
    entrypoints = set(manifest["dependency_entrypoints"])
    protected = next(
        module["path"]
        for module in manifest["modules"]
        if module["dependency_disposition"] != "unrelated_exempt"
        and module["path"] not in inbound
        and module["path"] not in entrypoints
    )
    addition = repo / "isar" / "schema" / "deep_unclassified_example.py"
    addition.write_text("VALUE = 1\n", encoding="utf-8")
    source = repo / protected
    source.write_text(
        source.read_text(encoding="utf-8")
        + "\nimport isar.schema.deep_unclassified_example\n",
        encoding="utf-8",
        newline="\n",
    )

    result = ENGINE.verify_manifest(
        repo, ENGINE.DEFAULT_CANDIDATE_PATH, candidate=True
    )
    assert not result.ok
    unclassified = next(
        item for item in result.findings if item.get("code") == "unclassified_dependencies"
    )
    assert "isar/schema/deep_unclassified_example.py" in unclassified["paths"]


@pytest.mark.parametrize(
    ("path", "expected_response"),
    [
        ("isar/analyze/select.py", "semantic review"),
        ("isar/drive/objective.py", "bounded source review"),
    ],
)
def test_hash_mismatch_reports_proportionate_response(
    tmp_path, path, expected_response
):
    repo = _closure_repo(tmp_path)
    source = repo / path
    source.write_text(
        source.read_text(encoding="utf-8") + "\n# reviewed test drift\n",
        encoding="utf-8",
        newline="\n",
    )

    result = ENGINE.verify_manifest(
        repo, ENGINE.DEFAULT_CANDIDATE_PATH, candidate=True
    )
    assert not result.ok
    finding = next(
        item
        for item in result.findings
        if item.get("code") == "protected_hash_mismatch" and item.get("path") == path
    )
    assert expected_response in finding["required_response"]


def test_missing_protected_path_refuses(tmp_path):
    repo = _closure_repo(tmp_path)
    (repo / "isar" / "analyze" / "select.py").unlink()

    result = ENGINE.verify_manifest(
        repo, ENGINE.DEFAULT_CANDIDATE_PATH, candidate=True
    )
    assert not result.ok
    assert any(
        finding.get("code") == "protected_source_missing"
        and finding.get("category") == "protected_semantic"
        and "proportionate evidence" in finding.get("required_response", "")
        for finding in result.findings
    )


def test_source_content_hash_is_portable_across_line_endings(tmp_path):
    source = tmp_path / "source.py"
    source.write_bytes(b"value = 1\r\nprint(value)\r\n")
    windows_hash = ENGINE.source_content_sha256(source)
    source.write_bytes(b"value = 1\nprint(value)\n")
    assert ENGINE.source_content_sha256(source) == windows_hash


def test_manifest_tampering_refuses(tmp_path):
    repo = _closure_repo(tmp_path)
    path = repo / ENGINE.DEFAULT_CANDIDATE_PATH
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["accepted_disposition"]["production_criterion"] = "AICc"
    _rewrite_json(path, manifest)

    result = ENGINE.verify_manifest(
        repo, ENGINE.DEFAULT_CANDIDATE_PATH, candidate=True
    )
    assert not result.ok
    assert any("S8" in finding.get("message", "") for finding in result.findings)


def test_manifest_and_review_schemas_reject_unknown_fields(tmp_path):
    repo = _closure_repo(tmp_path)
    candidate_path = repo / ENGINE.DEFAULT_CANDIDATE_PATH
    manifest = json.loads(candidate_path.read_text(encoding="utf-8"))
    manifest["unexpected"] = True
    _rewrite_json(candidate_path, manifest)
    result = ENGINE.verify_manifest(repo, ENGINE.DEFAULT_CANDIDATE_PATH, candidate=True)
    assert not result.ok
    assert any(
        "fields are invalid" in finding.get("message", "")
        for finding in result.findings
    )

    repo = _closure_repo(tmp_path / "review-case")
    candidate_path = repo / ENGINE.DEFAULT_CANDIDATE_PATH
    manifest = json.loads(candidate_path.read_text(encoding="utf-8"))
    review_path = repo / manifest["review_record"]["path"]
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["unexpected"] = True
    review["review_record_fingerprint"] = ENGINE._review_fingerprint(review)
    _rewrite_json(review_path, review)
    manifest["review_record"]["sha256"] = ENGINE.sha256_file(review_path)
    manifest["review_record"]["semantic_fingerprint"] = review[
        "review_record_fingerprint"
    ]
    manifest["candidate_closure_fingerprint"] = ENGINE._candidate_fingerprint(manifest)
    _rewrite_json(candidate_path, manifest)
    result = ENGINE.verify_manifest(repo, ENGINE.DEFAULT_CANDIDATE_PATH, candidate=True)
    assert not result.ok
    assert any(
        "fields are invalid" in finding.get("message", "")
        for finding in result.findings
    )


def test_review_tampering_refuses(tmp_path):
    repo = _closure_repo(tmp_path)
    manifest = _candidate()
    review_path = repo / manifest["review_record"]["path"]
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["aggregate_replay"]["status"] = "pass"
    _rewrite_json(review_path, review)

    result = ENGINE.verify_manifest(
        repo, ENGINE.DEFAULT_CANDIDATE_PATH, candidate=True
    )
    assert not result.ok
    assert any("review record byte hash" in finding.get("message", "") for finding in result.findings)


def test_active_contract_requires_explicit_independent_approval_fields(tmp_path):
    repo = _closure_repo(tmp_path)
    candidate_path = repo / ENGINE.DEFAULT_CANDIDATE_PATH
    manifest = json.loads(candidate_path.read_text(encoding="utf-8"))
    manifest["status"] = "active"
    active_path = repo / ENGINE.DEFAULT_ACTIVE_PATH
    active_path.parent.mkdir(parents=True, exist_ok=True)
    _rewrite_json(active_path, manifest)

    result = ENGINE.verify_manifest(repo, ENGINE.DEFAULT_ACTIVE_PATH)
    assert not result.ok
    assert any("not owner approved" in finding.get("message", "") for finding in result.findings)


def test_active_contract_validates_owner_date_commit_and_committed_artifacts(tmp_path):
    repo, active, _ = _approved_fixture(tmp_path)
    portable = ENGINE.verify_manifest(repo, ENGINE.DEFAULT_ACTIVE_PATH)
    assert portable.ok, portable.findings
    provenance = ENGINE.git_provenance(repo, portable.manifest)
    assert provenance.ok, provenance.findings

    active_path = repo / ENGINE.DEFAULT_ACTIVE_PATH
    invalid_owner = deepcopy(active)
    invalid_owner["approved_by"] = "someone-else"
    _rewrite_json(active_path, invalid_owner)
    result = ENGINE.verify_manifest(repo, ENGINE.DEFAULT_ACTIVE_PATH)
    assert not result.ok
    assert "repository owner" in result.findings[-1]["message"]

    invalid_date = deepcopy(active)
    invalid_date["approved_on"] = "not-a-date"
    _rewrite_json(active_path, invalid_date)
    result = ENGINE.verify_manifest(repo, ENGINE.DEFAULT_ACTIVE_PATH)
    assert not result.ok
    assert "approval date" in result.findings[-1]["message"]

    invalid_commit = deepcopy(active)
    invalid_commit["candidate_artifact_commit"] = "not-a-commit"
    _rewrite_json(active_path, invalid_commit)
    result = ENGINE.verify_manifest(repo, ENGINE.DEFAULT_ACTIVE_PATH)
    assert not result.ok
    assert "artifact commit" in result.findings[-1]["message"]

    missing_commit = deepcopy(active)
    missing_commit["candidate_artifact_commit"] = "0" * 40
    _rewrite_json(active_path, missing_commit)
    portable = ENGINE.verify_manifest(repo, ENGINE.DEFAULT_ACTIVE_PATH)
    assert portable.ok, portable.findings
    provenance = ENGINE.git_provenance(repo, portable.manifest)
    assert not provenance.ok
    assert any(
        finding["code"] == "candidate_artifact_unavailable"
        for finding in provenance.findings
    )

    wrong_hash = deepcopy(active)
    wrong_hash["candidate_artifact_sha256"] = "0" * 64
    _rewrite_json(active_path, wrong_hash)
    portable = ENGINE.verify_manifest(repo, ENGINE.DEFAULT_ACTIVE_PATH)
    assert portable.ok, portable.findings
    provenance = ENGINE.git_provenance(repo, portable.manifest)
    assert not provenance.ok
    assert any(
        finding["code"] == "candidate_artifact_hash_mismatch"
        for finding in provenance.findings
    )


def test_ast_edges_distinguish_runtime_type_and_conditional_imports(tmp_path):
    for relative in (
        "isar/__init__.py",
        "isar/example/__init__.py",
        "isar/example/runtime_dep.py",
        "isar/example/type_dep.py",
        "isar/example/conditional_dep.py",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    source = tmp_path / "isar" / "example" / "source.py"
    source.write_text(
        "from typing import TYPE_CHECKING\n"
        "import isar.example.runtime_dep\n"
        "if TYPE_CHECKING:\n"
        "    import isar.example.type_dep\n"
        "def load():\n"
        "    import isar.example.conditional_dep\n",
        encoding="utf-8",
    )

    edges = ENGINE._imports_from_source(tmp_path, "isar/example/source.py")
    kinds = {edge["imported"]: edge["edge_kind"] for edge in edges}
    assert kinds == {
        "isar/example/conditional_dep.py": "conditional",
        "isar/example/runtime_dep.py": "runtime",
        "isar/example/type_dep.py": "type_only",
    }


def test_package_reexports_and_manual_dynamic_edges_are_explicit(tmp_path, monkeypatch):
    for relative in (
        "isar/__init__.py",
        "isar/example/__init__.py",
        "isar/example/exported.py",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    package = tmp_path / "isar/example/__init__.py"
    package.write_text("from . import exported\n", encoding="utf-8")
    assert ENGINE._imports_from_source(tmp_path, "isar/example/__init__.py") == [
        {
            "importer": "isar/example/__init__.py",
            "imported": "isar/example/exported.py",
            "edge_kind": "package_reexport",
        }
    ]

    declaration = {
        "importer": "isar/analyze/select.py",
        "imported": "isar/predictive.py",
        "edge_kind": "manually_declared_dynamic",
    }
    monkeypatch.setattr(ENGINE, "MANUAL_DEPENDENCIES", (declaration,))
    gate = ENGINE.authenticate_historical_gate(ROOT)
    _, edges = ENGINE.dependency_closure(ROOT, gate)
    assert declaration in edges


def test_candidate_builder_refuses_an_unauthenticated_active_without_overwriting(tmp_path):
    repo = _closure_repo(tmp_path)
    active = repo / ENGINE.DEFAULT_ACTIVE_PATH
    active.parent.mkdir(parents=True, exist_ok=True)
    active.write_text("{}\n", encoding="utf-8")
    original = active.read_bytes()
    with pytest.raises(ENGINE.ClosureError, match="cannot be authenticated"):
        ENGINE.build_candidate(
            repo,
            proposed_production_anchor=ENGINE.BOOTSTRAP_PRODUCTION_ANCHOR,
            review_path="docs/campaigns/cycle-2/review.json",
        )
    assert active.read_bytes() == original


def test_renewal_preserves_active_and_compares_against_it(tmp_path):
    repo, active, _ = _approved_fixture(tmp_path)
    active_path = repo / ENGINE.DEFAULT_ACTIVE_PATH
    active_bytes = active_path.read_bytes()

    protected = repo / "isar/analyze/select.py"
    protected.write_text(
        protected.read_text(encoding="utf-8") + "\n# renewal review drift\n",
        encoding="utf-8",
        newline="\n",
    )
    _git(repo, "add", "isar/analyze/select.py")
    _git(repo, "commit", "-m", "reviewed production change")
    proposed_anchor = _git(repo, "rev-parse", "HEAD")
    candidate, review = ENGINE.build_candidate(
        repo,
        proposed_production_anchor=proposed_anchor,
        review_path="docs/campaigns/cycle-2/review.json",
        verification_evidence={"status": "passed", "commands": [], "parity_checks": []},
        change_reviews={
            "isar/analyze/select.py": {
                "current_change_disposition": "selection_semantic",
                "proof_tests": ["tests/analyze/test_select.py"],
            }
        },
    )

    assert active_path.read_bytes() == active_bytes
    assert candidate["cycle_mode"] == "renewal"
    assert candidate["comparison_basis"]["manifest_path"] == ENGINE.DEFAULT_ACTIVE_PATH
    assert candidate["comparison_basis"]["candidate_closure_fingerprint"] == active[
        "candidate_closure_fingerprint"
    ]
    assert review["drift_inventory"]["counts"]["modified"] == 1
    assert review["drift_inventory"]["entries"][0]["path"] == (
        "isar/analyze/select.py"
    )
    assert review["dependency_drift"] == {"added": [], "removed": []}
    result = ENGINE.verify_manifest(
        repo, ENGINE.DEFAULT_CANDIDATE_PATH, candidate=True
    )
    assert result.ok, result.findings
    provenance = ENGINE.git_provenance(
        repo, result.manifest, release_anchor=proposed_anchor
    )
    assert provenance.ok, provenance.findings


def test_review_record_does_not_reference_its_manifest():
    manifest = _candidate()
    review = json.loads(
        (ROOT / manifest["review_record"]["path"]).read_text(encoding="utf-8")
    )
    assert "candidate_closure_fingerprint" not in review
    assert "closure_manifest_fingerprint" not in review
    assert review["production_source_fingerprint"] == manifest[
        "production_source_fingerprint"
    ]


def test_cli_audits_a_named_candidate_and_exposes_no_approval_command():
    cli = ROOT / "tools/stats/stat2b_source_closure.py"
    help_result = subprocess.run(
        [sys.executable, str(cli), "--help"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "{candidate,audit,verify}" in help_result.stdout
    assert "\n    approve " not in help_result.stdout
    audit = subprocess.run(
        [
            sys.executable,
            str(cli),
            "audit",
            "--manifest",
            ENGINE.DEFAULT_CANDIDATE_PATH.replace("/", "\\"),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "PASSED (candidate_valid)" in audit.stdout
