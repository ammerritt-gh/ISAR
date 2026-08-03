"""Focused contract tests for the pure Slice-5 offline comparison seam."""
from __future__ import annotations

import pytest

from isar.eig.offline_compare import (
    OfflineCaseSnapshot,
    OfflineCorpusIndex,
    OfflineDiscoveryConfig,
    OfflineScoringConfig,
    QoIDistinctionPolicy,
    QoIDistinctionPolicySet,
    QoIDistinctionVariant,
    matching_qoi_policies,
    score_offline_comparison,
    validate_offline_candidate_pair,
    validate_offline_comparison_report,
)
from isar.eig import offline_compare as compare
from isar.schema.statistics import semantics_fingerprint


def _fp(name: str) -> str:
    return semantics_fingerprint("tests.eig.offline_compare", {"name": name})


def _case(*, status: str = "unassessable") -> OfflineCaseSnapshot:
    return OfflineCaseSnapshot(
        case_identity=_fp("case"), campaign_configuration_fingerprint=_fp("campaign"),
        qoi_id="omega0", qoi_unit="meV", status=status,
        reason_codes=() if status == "ready" else ("held",),
        authority_scopes=(("acquisition_selection", "ineligible"), ("eig_authoritative", "ineligible"),
                          ("eig_shadow", "diagnostic") if status == "ready" else ("eig_shadow", "ineligible")),
    )


def test_empty_policy_set_and_zero_ready_corpus_produce_complete_empty_report():
    corpus = OfflineCorpusIndex(OfflineDiscoveryConfig(), ())
    policies = QoIDistinctionPolicySet(())
    config = OfflineScoringConfig(seed=4, simulation_count=4, block_count=2)
    report = score_offline_comparison(corpus, policies, config, {})
    assert report.status == "complete"
    assert report.results == ()
    assert OfflineCorpusIndex.from_dict(corpus.to_dict()).corpus_fingerprint == corpus.corpus_fingerprint


def test_policy_annotation_is_nonsemantic_and_case_identity_scope_matches_directly():
    case = _case()
    common = dict(policy_id="scalar", policy_kind="isar.scalar_absolute_partitions/o36.5",
                  source_qoi={"id": "omega0", "unit": "meV"},
                  provenance={"kind": "explicit_user_analysis/1", "source_id": "operator", "rationale": "test"},
                  variants=(QoIDistinctionVariant("base", {"origin": 0.0, "bin_width": 0.5, "offsets": [0.0]}),))
    first = QoIDistinctionPolicy(scope={"kind": "case_identity", "fingerprint": case.case_identity,
                                       "annotation": {"campaign_name": "campaign", "experiment_description": "desc", "date": "2026-08-03"}}, **common)
    second = QoIDistinctionPolicy(scope={"kind": "case_identity", "fingerprint": case.case_identity,
                                        "annotation": {"campaign_name": "other", "experiment_description": "other", "date": "2026-08-04"}}, **common)
    assert first.policy_fingerprint == second.policy_fingerprint
    assert matching_qoi_policies(case, QoIDistinctionPolicySet((first,))) == (first,)


def test_candidate_pair_distinguishes_regenerated_and_shadow_mismatch():
    legacy = {"candidate_id": "legacy", "family": "legacy_uniform/1", "energy_meV": [0.0, 1.0], "exposure_per_point": 2.0}
    target = {"candidate_id": "target", "family": "target_core/1", "energy_meV": [0.0, 0.5], "exposure_per_point": 2.0}
    with pytest.raises(ValueError, match="persisted_legacy_candidate_mismatch"):
        validate_offline_candidate_pair(legacy, legacy, target,
                                        regenerated_persisted_legacy={**legacy, "energy_meV": [0.0, 2.0]})
    with pytest.raises(ValueError, match="shadow_legacy_candidate_mismatch"):
        validate_offline_candidate_pair(legacy, {**legacy, "energy_meV": [0.0, 2.0]}, target)


def test_ready_case_and_report_read_gates_reject_nested_tampering():
    legacy = {"candidate_id": "legacy", "family": "legacy_uniform/1", "energy_meV": [0.0, 1.0], "exposure_per_point": 2.0}
    target = {"candidate_id": "target", "family": "target_core/1", "energy_meV": [0.0, 0.5], "exposure_per_point": 2.0}
    pair = validate_offline_candidate_pair(legacy, legacy, target, regenerated_persisted_legacy=legacy)
    persisted_context = {"scan_window_eig_mode": "off"}
    context = {"scan_window_eig_mode": "shadow"}
    transform = {"revision": "isar.offline_shadow_context_transform/o36.5",
                 "persisted_context": persisted_context, "offline_context": context,
                 "persisted_context_fingerprint": compare._fp("scan_design_context", persisted_context),
                 "offline_context_fingerprint": compare._fp("scan_design_context", context),
                 "changed_fields": ["scan_window_eig_mode"]}
    case = OfflineCaseSnapshot(_fp("ready-case"), _fp("campaign"), "omega0", "meV", "ready",
                               context_transform=transform, candidate_pair=pair)
    assert case.case_fingerprint
    broken = case.to_dict(); broken["candidate_pair"]["candidate_pair_fingerprint"] = _fp("wrong")
    with pytest.raises(ValueError, match="candidate pair"):
        OfflineCaseSnapshot.from_dict(broken)
    corpus = OfflineCorpusIndex(OfflineDiscoveryConfig(), ())
    policy = QoIDistinctionPolicySet(())
    config = OfflineScoringConfig(1, 4, 2)
    report = score_offline_comparison(corpus, policy, config, {})
    restored = validate_offline_comparison_report(report)
    assert restored.report_fingerprint == report.report_fingerprint
    tampered = report.to_dict(); tampered["counts"]["complete"] = 3
    with pytest.raises(ValueError, match="report_fingerprint"):
        type(report).from_dict(tampered)


def test_scoring_refuses_prepared_inputs_not_joined_to_ready_case():
    legacy = {"candidate_id": "legacy", "family": "legacy_uniform/1",
              "energy_meV": [0.0, 1.0], "exposure_per_point": 2.0}
    target = {"candidate_id": "target", "family": "target_core/1",
              "energy_meV": [0.0, 0.5], "exposure_per_point": 2.0}
    pair = validate_offline_candidate_pair(
        legacy, legacy, target, regenerated_persisted_legacy=legacy)
    persisted_context = {"scan_window_eig_mode": "off"}
    offline_context = {"scan_window_eig_mode": "shadow"}
    transform = {
        "revision": "isar.offline_shadow_context_transform/o36.5",
        "persisted_context": persisted_context,
        "offline_context": offline_context,
        "persisted_context_fingerprint": compare._fp(
            "scan_design_context", persisted_context),
        "offline_context_fingerprint": compare._fp(
            "scan_design_context", offline_context),
        "changed_fields": ["scan_window_eig_mode"],
    }
    case = OfflineCaseSnapshot(
        _fp("joined-ready-case"), _fp("campaign"), "omega0", "meV", "ready",
        source_fingerprints={
            "design_measure": _fp("expected-measure"),
            "source_draw_identity": _fp("expected-draws"),
            "predictive_basis": _fp("expected-predictive"),
        },
        context_transform=transform,
        candidate_pair=pair,
    )
    variant = QoIDistinctionVariant(
        "base", {"origin": 0.0, "bin_width": 0.5, "offsets": [0.0]})
    policy = QoIDistinctionPolicy(
        policy_id="scalar",
        policy_kind="isar.scalar_absolute_partitions/o36.5",
        source_qoi={"id": "omega0", "unit": "meV"},
        scope={"kind": "case_identity", "fingerprint": case.case_identity,
               "annotation": {"campaign_name": "fixture",
                              "experiment_description": "join test",
                              "date": "2026-08-03"}},
        provenance={"kind": "explicit_user_analysis/1", "source_id": "operator",
                    "rationale": "exercise the strict prepared-input join"},
        variants=(variant,),
    )
    report = score_offline_comparison(
        OfflineCorpusIndex(OfflineDiscoveryConfig(), (case,)),
        QoIDistinctionPolicySet((policy,)),
        OfflineScoringConfig(seed=4, simulation_count=4, block_count=2),
        {case.case_identity: (object(), ())},
    )

    assert report.results[0].reason_codes == ("offline_scientific_case_mismatch",)
