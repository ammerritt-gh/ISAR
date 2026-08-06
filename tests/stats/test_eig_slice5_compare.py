"""Integration tests for the filesystem-only EIG Slice 5 runner."""
from __future__ import annotations

import importlib.util
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools" / "stats"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
RUNNER_PATH = TOOLS / "eig_slice5_compare.py"
SPEC = importlib.util.spec_from_file_location("eig_slice5_runner_test", RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
RUNNER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RUNNER
SPEC.loader.exec_module(RUNNER)


def _campaign(tmp_path: Path, *, stop: str | None = "met", root_text: str = "campaign") -> Path:
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    (campaign / "session.json").write_text(json.dumps({"schema_version": "isar.session/3", "scans": {}}), encoding="utf-8")
    (campaign / "card.state.json").write_text("{}", encoding="utf-8")
    (campaign / "card.policy.toml").write_text("[experiment]\nname = 'fixture'\n", encoding="utf-8")
    if stop is not None:
        log = campaign / "report" / "session_log.jsonl"
        log.parent.mkdir()
        log.write_text(json.dumps({"kind": "stop", "payload": {"state": stop, "reasons": ["fixture"]}}) + "\n", encoding="utf-8")
    manifest = tmp_path / "campaigns.toml"
    manifest.write_text(
        "[[campaign]]\nid = 'one'\ncohort = 'ordinary'\nevidence_tier = 'primary'\n"
        f"root = '{root_text}'\nlineage = 'one'\n", encoding="utf-8")
    return manifest


@pytest.mark.parametrize("state,terminal", [
    ("met", True), ("incomplete", True), ("infeasible", True), ("operator_stopped", True),
    ("continue", False), ("pending", False), ("unknown", False),
])
def test_final_stop_state_only_accepts_authoritative_terminals(tmp_path: Path, state: str, terminal: bool):
    manifest = _campaign(tmp_path, stop=state)
    observed = RUNNER._final_stop_state(tmp_path / "campaign")
    assert observed["state"] == state
    assert (observed["status"] == "terminal") is terminal
    assert manifest.exists()


def test_final_stop_state_missing_is_nonterminal(tmp_path: Path):
    _campaign(tmp_path, stop=None)
    assert RUNNER._final_stop_state(tmp_path / "campaign") == {
        "status": "nonterminal_or_unclassified", "state": "missing", "reasons": [],
    }


def test_campaign_roots_are_preserved_byte_for_byte(tmp_path: Path):
    manifest = _campaign(tmp_path, root_text="campaign/./")
    assert RUNNER._raw_campaign_roots(manifest) == {"one": "campaign/./"}


def test_candidate_view_uses_content_addressed_candidate_id_not_family():
    candidate = SimpleNamespace(candidate_id="candidate-content-hash", candidate_family="legacy_uniform/1",
                                energies=(1.0, 2.0), ncount=50)
    assert RUNNER._candidate_view(candidate) == {
        "candidate_id": "candidate-content-hash", "family": "legacy_uniform/1",
        "energy_meV": [1.0, 2.0], "exposure_per_point": 50.0,
    }


def test_shadow_alias_and_refusal_remain_distinct_pair_outcomes():
    legacy = SimpleNamespace(candidate_family="legacy_uniform/1", alias_families=("target_core/1",))
    with pytest.raises(RUNNER.OfflineRunnerError, match="candidate_pair_degenerate"):
        RUNNER._shadow_pair_candidates(SimpleNamespace(candidates=(legacy,), refusals=()))
    refused = SimpleNamespace(candidate_family="target_core/1", reason_code="target_core_truncated_after_domain_clip")
    with pytest.raises(RUNNER.OfflineRunnerError, match="target_core_truncated_after_domain_clip"):
        RUNNER._shadow_pair_candidates(SimpleNamespace(candidates=(
            SimpleNamespace(candidate_family="legacy_uniform/1", alias_families=()),), refusals=(refused,)))


def test_persisted_generator_rehydrates_nondefault_configuration_and_authenticates_it():
    from isar.drive.acquisition.scan_design import (
        PHYSICAL_WINDOW_GENERATOR_ID,
        PHYSICAL_WINDOW_GENERATOR_VERSION,
        PhysicalWindowGeneratorConfig,
    )

    config = PhysicalWindowGeneratorConfig(core_fwhm_multiple=2.5)
    record = {"generator": {
        "id": PHYSICAL_WINDOW_GENERATOR_ID,
        "version": PHYSICAL_WINDOW_GENERATOR_VERSION,
        "config": config.to_dict(),
        "config_fingerprint": config.fingerprint,
    }}
    restored, identity = RUNNER._persisted_generator(record)
    assert restored == config
    assert identity["config"] == config.to_dict()

    record["generator"]["config_fingerprint"] = "0" * 64
    with pytest.raises(RUNNER.OfflineRunnerError, match="generator_config_fingerprint_mismatch"):
        RUNNER._persisted_generator(record)


def test_authenticated_pair_construction_failure_is_comparison_unavailable(monkeypatch, tmp_path: Path):
    core = RUNNER._offline_core()
    row = {
        "campaign_id": "one",
        "master_session_key": "master",
        "detector_id": "detector",
        "cohort": "ordinary",
        "evidence_tier": "primary",
        "campaign_stop": {"status": "terminal", "state": "met", "reasons": []},
        "outcome": {},
        "qoi_evidence": {"status": "missing"},
        "eligibility": {"scientific_codes": [], "observability_codes": []},
    }

    def unavailable(*_args, **_kwargs):
        raise RUNNER.ComparisonUnavailableError("candidate_exposure_mismatch")

    monkeypatch.setattr(RUNNER, "_prepare_discovery_case", unavailable)
    cases, recipes = RUNNER._build_cases(
        core,
        (row,),
        {"one": ({"producer_revision": "tests.scope/1", "fingerprint": "1" * 64}, None)},
        {"one": tmp_path},
    )
    assert recipes == {}
    assert cases[0].status == "comparison_unavailable"
    assert cases[0].reason_codes == ("candidate_exposure_mismatch",)
    assert cases[0].qoi_id is None
    assert cases[0].qoi_unit is None


def test_sidecar_requires_content_addressed_filename(tmp_path: Path):
    directory = tmp_path / "data" / "master_predictive"
    directory.mkdir(parents=True)
    raw = b'{"schema":"fixture"}'
    digest = hashlib.sha256(raw).hexdigest()
    path = directory / f"{digest}.json"
    path.write_bytes(raw)
    assert RUNNER._load_sidecar(tmp_path, f"data/master_predictive/{digest}.json", "data/master_predictive") == {
        "schema": "fixture"}
    path.write_text('{"schema":"tampered"}', encoding="utf-8")
    with pytest.raises(RUNNER.OfflineRunnerError, match="sidecar_content_address_mismatch"):
        RUNNER._load_sidecar(tmp_path, f"data/master_predictive/{digest}.json", "data/master_predictive")


def test_atomic_output_refuses_overwrite_and_preserves_existing_bytes(tmp_path: Path):
    output = tmp_path / "index.json"
    output.write_bytes(b'{"old":true}\n')
    with pytest.raises(RUNNER.OfflineRunnerError, match="output already exists"):
        RUNNER._atomic_write_json(output, {"new": True}, overwrite=False)
    assert output.read_bytes() == b'{"old":true}\n'
    RUNNER._atomic_write_json(output, {"new": True}, overwrite=True)
    assert json.loads(output.read_text(encoding="utf-8")) == {"new": True}


def test_discover_refuses_output_inside_campaign_root(tmp_path: Path, monkeypatch):
    manifest = _campaign(tmp_path)
    session = tmp_path / "campaign" / "session.json"
    before = session.read_bytes()
    monkeypatch.setattr(RUNNER, "_REPO_ROOT", tmp_path)

    with pytest.raises(RUNNER.OfflineRunnerError, match="outside campaign roots"):
        RUNNER.discover(
            manifest.name,
            Path("campaign/session.json"),
            overwrite=True,
        )

    assert session.read_bytes() == before


def test_malformed_corpus_does_not_replace_report(tmp_path: Path, monkeypatch):
    manifest = _campaign(tmp_path)
    bad_index = tmp_path / "bad-index.json"
    bad_index.write_text("{not json", encoding="utf-8")
    report = tmp_path / "report.json"
    report.write_text('{"old":true}\n', encoding="utf-8")
    monkeypatch.setattr(RUNNER, "_REPO_ROOT", tmp_path)

    assert RUNNER.main([
        "score", "--campaign-set", manifest.name, "--corpus-index", bad_index.name,
        "--output", report.name, "--seed", "1", "--simulation-count", "8", "--block-count", "2",
    ]) == 2
    assert report.read_text(encoding="utf-8") == '{"old":true}\n'


def test_discover_manifest_is_ordered_and_root_independent(tmp_path: Path, monkeypatch):
    manifest = _campaign(tmp_path, root_text="campaign/./")
    monkeypatch.setattr(RUNNER, "_REPO_ROOT", tmp_path)
    first = RUNNER.discover(manifest.name, Path("first.json"))
    second = RUNNER.discover(manifest.name, Path("second.json"))

    assert [case["case_identity"] for case in first["cases"]] == sorted(
        case["case_identity"] for case in first["cases"])
    assert first["corpus_fingerprint"] == second["corpus_fingerprint"]
    assert first["diagnostics"]["campaign_roots"] == {"one": "campaign/./"}


def test_zero_ready_corpus_scores_without_a_policy_file(tmp_path: Path, monkeypatch):
    manifest = _campaign(tmp_path)
    monkeypatch.setattr(RUNNER, "_REPO_ROOT", tmp_path)
    index = tmp_path / "index.json"
    report = tmp_path / "report.json"

    assert RUNNER.main(["discover", "--campaign-set", manifest.name, "--output", index.name]) == 0
    assert RUNNER.main([
        "score", "--campaign-set", manifest.name, "--corpus-index", index.name,
        "--output", report.name, "--seed", "1", "--simulation-count", "8", "--block-count", "2",
    ]) == 0
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["status"] == "complete"
    assert payload["results"] == []


def test_hand_authored_policy_toml_needs_no_fingerprints(tmp_path: Path):
    policy = tmp_path / "policy.toml"
    policy.write_text(
        "contract = 'isar.qoi_distinction_policy_set/o36.5'\n"
        "[[policies]]\n"
        "contract = 'isar.qoi_distinction_policy/o36.5'\n"
        "policy_id = 'analysis'\n"
        "policy_kind = 'isar.scalar_absolute_partitions/o36.5'\n"
        "[policies.source_qoi]\n"
        "id = 'excitation.energy'\nunit = 'meV'\n"
        "[policies.scope]\nkind = 'case_identity'\n"
        f"fingerprint = '{'1' * 64}'\n"
        "[policies.scope.annotation]\n"
        "campaign_name = 'fixture'\nexperiment_description = 'literal TOML'\n"
        "date = '2026-08-03'\n"
        "[policies.provenance]\nkind = 'explicit_user_analysis/1'\n"
        "source_id = 'owner'\nrationale = 'hand authored sensitivity analysis'\n"
        "[[policies.variants]]\nvariant_id = 'fine'\n"
        "[policies.variants.parameters]\norigin = 0.0\nbin_width = 0.1\n"
        "offsets = [0.0]\n",
        encoding="utf-8",
    )
    loaded = RUNNER.load_qoi_distinction_policies(policy)
    assert loaded.policy_set_fingerprint
    assert loaded.policies[0].policy_fingerprint
    assert loaded.policies[0].variants[0].variant_fingerprint


def test_malformed_supported_policy_fails_before_replacing_output(tmp_path: Path, monkeypatch):
    manifest = _campaign(tmp_path)
    monkeypatch.setattr(RUNNER, "_REPO_ROOT", tmp_path)
    index = tmp_path / "index.json"
    report = tmp_path / "report.json"
    policy = tmp_path / "policy.toml"
    RUNNER.discover(Path(manifest.name), Path(index.name))
    report.write_text('{"old":true}\n', encoding="utf-8")
    policy.write_text(
        "contract = 'isar.qoi_distinction_policy_set/o36.5'\n"
        "[[policies]]\ncontract = 'isar.qoi_distinction_policy/o36.5'\n"
        "policy_id = 'bad'\npolicy_kind = 'isar.scalar_absolute_partitions/o36.5'\n"
        "[policies.source_qoi]\nid = 'x'\nunit = 'u'\n"
        f"[policies.scope]\nkind = 'case_identity'\nfingerprint = '{'2' * 64}'\n"
        "[policies.scope.annotation]\ncampaign_name = 'c'\nexperiment_description = 'd'\ndate = '2026-08-03'\n"
        "[policies.provenance]\nkind = 'explicit_user_analysis/1'\nsource_id = 'owner'\nrationale = 'bad width'\n"
        "[[policies.variants]]\nvariant_id = 'bad'\n"
        "[policies.variants.parameters]\norigin = 0.0\nbin_width = -1.0\noffsets = [0.0]\n",
        encoding="utf-8",
    )
    assert RUNNER.main([
        "score", "--campaign-set", manifest.name, "--corpus-index", index.name,
        "--policy-file", policy.name, "--output", report.name, "--overwrite",
        "--seed", "1", "--simulation-count", "8", "--block-count", "2",
    ]) == 2
    assert report.read_text(encoding="utf-8") == '{"old":true}\n'
