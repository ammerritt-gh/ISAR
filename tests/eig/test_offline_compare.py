"""Focused contract tests for the pure Slice-5 offline comparison seam."""
from __future__ import annotations

from pathlib import Path
import runpy

import pytest

from isar.drive.acquisition.scan_design import (
    DEFAULT_WINDOW_GENERATOR_CONFIG,
    PHYSICAL_WINDOW_GENERATOR_ID,
    PHYSICAL_WINDOW_GENERATOR_VERSION,
)

from isar.eig.offline_compare import (
    OfflineCaseSnapshot,
    OfflineComparisonReport,
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
    provenance = compare.make_source_provenance(
        reviewed_campaign_id="campaign", master_session_key="master",
        detector_id="detector", cohort="ordinary", evidence_tier="primary")
    return OfflineCaseSnapshot(
        case_identity=provenance["source_provenance_fingerprint"],
        decision_snapshot_identity=_fp("decision"), source_provenance=provenance,
        campaign_configuration_scope={"producer_revision": "tests.scope/1",
                                      "fingerprint": _fp("campaign")},
        qoi_id="omega0", qoi_unit="meV", status=status,
        reason_codes=() if status == "ready" else ("held",),
        authority_scopes=(("acquisition_selection", "ineligible"), ("eig_authoritative", "ineligible"),
                          ("eig_shadow", "diagnostic") if status == "ready" else ("eig_shadow", "ineligible")),
    )


def test_empty_policy_set_and_zero_ready_corpus_produce_complete_empty_report():
    corpus = OfflineCorpusIndex(OfflineDiscoveryConfig(), ())
    policies = QoIDistinctionPolicySet(())
    config = OfflineScoringConfig(seed=4, simulation_count=4, block_count=2)
    report = score_offline_comparison(corpus, policies, config, lambda _case: None)
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
                                        regenerated_persisted_legacy={**legacy, "energy_meV": [0.0, 2.0]},
                                        generator_identity=_generator())
    with pytest.raises(ValueError, match="shadow_legacy_candidate_mismatch"):
        validate_offline_candidate_pair(
            legacy, {**legacy, "energy_meV": [0.0, 2.0]}, target,
            regenerated_persisted_legacy=legacy, generator_identity=_generator())


def test_ready_case_and_report_read_gates_reject_nested_tampering():
    legacy = {"candidate_id": "legacy", "family": "legacy_uniform/1", "energy_meV": [0.0, 1.0], "exposure_per_point": 2.0}
    target = {"candidate_id": "target", "family": "target_core/1", "energy_meV": [0.0, 0.5], "exposure_per_point": 2.0}
    pair = validate_offline_candidate_pair(
        legacy, legacy, target, regenerated_persisted_legacy=legacy,
        generator_identity=_generator())
    persisted_context = {"scan_window_eig_mode": "off"}
    context = {"scan_window_eig_mode": "authoritative"}
    transform = {"revision": "isar.offline_shadow_context_transform/o36.5.1",
                 "persisted_context": persisted_context, "offline_context": context,
                 "persisted_context_fingerprint": compare._fp("scan_design_context", persisted_context),
                 "offline_context_fingerprint": compare._fp("scan_design_context", context),
                 "generator_config_fingerprint": DEFAULT_WINDOW_GENERATOR_CONFIG.fingerprint,
                 "changed_fields": ["scan_window_eig_mode"]}
    provenance = compare.make_source_provenance(
        reviewed_campaign_id="campaign", master_session_key="ready", detector_id="detector",
        cohort="ordinary", evidence_tier="primary")
    case = OfflineCaseSnapshot(
        provenance["source_provenance_fingerprint"], _fp("ready-case"), provenance,
        {"producer_revision": "tests.scope/1", "fingerprint": _fp("campaign")},
        "omega0", "meV", "ready", context_transform=transform, candidate_pair=pair)
    assert case.case_fingerprint
    broken = case.to_dict(); broken["candidate_pair"]["candidate_pair_fingerprint"] = _fp("wrong")
    with pytest.raises(ValueError, match="candidate pair"):
        OfflineCaseSnapshot.from_dict(broken)
    corpus = OfflineCorpusIndex(OfflineDiscoveryConfig(), ())
    policy = QoIDistinctionPolicySet(())
    config = OfflineScoringConfig(1, 4, 2)
    report = score_offline_comparison(corpus, policy, config, lambda _case: None)
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
        legacy, legacy, target, regenerated_persisted_legacy=legacy,
        generator_identity=_generator())
    persisted_context = {"scan_window_eig_mode": "off"}
    offline_context = {"scan_window_eig_mode": "authoritative"}
    transform = {
        "revision": "isar.offline_shadow_context_transform/o36.5.1",
        "persisted_context": persisted_context,
        "offline_context": offline_context,
        "persisted_context_fingerprint": compare._fp(
            "scan_design_context", persisted_context),
        "offline_context_fingerprint": compare._fp(
            "scan_design_context", offline_context),
        "generator_config_fingerprint": DEFAULT_WINDOW_GENERATOR_CONFIG.fingerprint,
        "changed_fields": ["scan_window_eig_mode"],
    }
    provenance = compare.make_source_provenance(
        reviewed_campaign_id="campaign", master_session_key="joined", detector_id="detector",
        cohort="ordinary", evidence_tier="primary")
    case = OfflineCaseSnapshot(
        provenance["source_provenance_fingerprint"], _fp("joined-ready-case"), provenance,
        {"producer_revision": "tests.scope/1", "fingerprint": _fp("campaign")},
        "omega0", "meV", "ready",
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
        lambda _case: (object(), ()),
    )

    assert report.results[0].reason_codes == ("offline_scientific_case_mismatch",)


def _generator():
    return {
        "id": PHYSICAL_WINDOW_GENERATOR_ID,
        "version": PHYSICAL_WINDOW_GENERATOR_VERSION,
        "config": DEFAULT_WINDOW_GENERATOR_CONFIG.to_dict(),
        "config_fingerprint": DEFAULT_WINDOW_GENERATOR_CONFIG.fingerprint,
    }


def test_nonready_case_preserves_missing_scope_and_qoi_as_null():
    provenance = compare.make_source_provenance(
        reviewed_campaign_id="campaign", master_session_key="missing", detector_id="detector",
        cohort="ordinary", evidence_tier="primary")
    case = OfflineCaseSnapshot(
        provenance["source_provenance_fingerprint"], _fp("missing"), provenance,
        None, None, None, "unassessable", ("qoi_missing",),
        authority_scopes=(("acquisition_selection", "ineligible"),
                          ("eig_authoritative", "ineligible"),
                          ("eig_shadow", "ineligible")))
    assert case.to_dict()["qoi_id"] is None
    assert case.to_dict()["campaign_configuration_scope"] is None


def test_population_identity_distinguishes_semantically_identical_sources():
    first = _case()
    provenance = compare.make_source_provenance(
        reviewed_campaign_id="other", master_session_key="master", detector_id="detector",
        cohort="ordinary", evidence_tier="primary")
    second = OfflineCaseSnapshot(
        provenance["source_provenance_fingerprint"], first.decision_snapshot_identity,
        provenance, first.campaign_configuration_scope, first.qoi_id, first.qoi_unit,
        first.status, first.reason_codes, authority_scopes=first.authority_scopes)
    assert first.decision_snapshot_identity == second.decision_snapshot_identity
    assert first.case_identity != second.case_identity


def test_cross_scope_overlap_is_ambiguous_without_specificity_precedence():
    case = _case()
    common = {
        "policy_kind": "isar.scalar_absolute_partitions/o36.5",
        "source_qoi": {"id": "omega0", "unit": "meV"},
        "provenance": {"kind": "explicit_user_analysis/1", "source_id": "owner",
                       "rationale": "test overlap"},
        "variants": (QoIDistinctionVariant(
            "base", {"origin": 0.0, "bin_width": 0.5, "offsets": [0.0]}),),
    }
    annotation = {"campaign_name": "fixture", "experiment_description": "overlap",
                  "date": "2026-08-03"}
    case_policy = QoIDistinctionPolicy(
        policy_id="case", scope={"kind": "case_identity",
                                  "fingerprint": case.case_identity,
                                  "annotation": annotation}, **common)
    campaign_policy = QoIDistinctionPolicy(
        policy_id="campaign", scope={"kind": "campaign_configuration",
                                      "fingerprint": case.campaign_configuration_scope["fingerprint"],
                                      "annotation": annotation}, **common)
    policies = QoIDistinctionPolicySet((campaign_policy, case_policy))
    assert matching_qoi_policies(case, policies) == (campaign_policy, case_policy)


def test_policy_set_validator_reauthenticates_nested_policy():
    case = _case()
    variant = QoIDistinctionVariant(
        "base", {"origin": 0.0, "bin_width": 0.5, "offsets": [0.0]})
    policy = QoIDistinctionPolicy(
        "policy", "isar.scalar_absolute_partitions/o36.5",
        {"id": "omega0", "unit": "meV"},
        {"kind": "case_identity", "fingerprint": case.case_identity,
         "annotation": {"campaign_name": "fixture", "experiment_description": "tamper",
                        "date": "2026-08-03"}},
        {"kind": "explicit_user_analysis/1", "source_id": "owner",
         "rationale": "test nested authentication"}, (variant,))
    policy_set = QoIDistinctionPolicySet((policy,))
    object.__setattr__(policy, "policy_fingerprint", "0" * 64)
    with pytest.raises(ValueError, match="policy_fingerprint"):
        compare.validate_qoi_distinction_policy_set(policy_set)


def test_real_producer_chain_evaluates_two_grids_once_and_reuses_them(monkeypatch):
    from isar.drive.acquisition.scan_design import PhysicalWindowGeneratorConfig, design_scan_windows
    from isar.eig.design_measure import build_scan_window_design_measure
    from isar.eig.scorer import CandidateEvaluationInput
    from isar.predictive import evaluate_master_predictive

    eig_helpers = runpy.run_path(str(Path(__file__).with_name("test_design_measure.py")))
    predictive, _projection, _qoi, eligibility = eig_helpers["_bundle"](monkeypatch)
    measure = build_scan_window_design_measure(predictive, eligibility)
    scan_helpers = runpy.run_path(str(
        Path(__file__).parents[1] / "drive" / "test_scan_design.py"))
    context = scan_helpers["_context"](scan_window_eig_mode="off")
    config = PhysicalWindowGeneratorConfig(core_fwhm_multiple=2.5)
    persisted = design_scan_windows(context, config)
    transform = compare.offline_shadow_context_transform(context, config.fingerprint)
    shadow_context = compare.replace(context, scan_window_eig_mode="authoritative")
    shadow = design_scan_windows(shadow_context, config)
    default_shadow = design_scan_windows(
        shadow_context, DEFAULT_WINDOW_GENERATOR_CONFIG)
    legacy = next(item for item in shadow.candidates
                  if item.candidate_family == "legacy_uniform/1")
    target = next(item for item in shadow.candidates
                  if item.candidate_family == "target_core/1")
    default_target = next(item for item in default_shadow.candidates
                          if item.candidate_family == "target_core/1")
    assert persisted.selected_candidate.energies == legacy.energies
    assert target.energies != default_target.energies
    pair = validate_offline_candidate_pair(
        {"candidate_id": persisted.selected_candidate.candidate_id,
         "family": persisted.selected_candidate.candidate_family,
         "energy_meV": persisted.selected_candidate.energies,
         "exposure_per_point": persisted.selected_candidate.ncount},
        {"candidate_id": legacy.candidate_id, "family": legacy.candidate_family,
         "energy_meV": legacy.energies, "exposure_per_point": legacy.ncount},
        {"candidate_id": target.candidate_id, "family": target.candidate_family,
         "energy_meV": target.energies, "exposure_per_point": target.ncount},
        regenerated_persisted_legacy={
            "candidate_id": persisted.selected_candidate.candidate_id,
            "family": persisted.selected_candidate.candidate_family,
            "energy_meV": persisted.selected_candidate.energies,
            "exposure_per_point": persisted.selected_candidate.ncount},
        generator_identity={"id": PHYSICAL_WINDOW_GENERATOR_ID,
                            "version": PHYSICAL_WINDOW_GENERATOR_VERSION,
                            "config": config.to_dict(),
                            "config_fingerprint": config.fingerprint})
    provenance = compare.make_source_provenance(
        reviewed_campaign_id="producer", master_session_key="master", detector_id="detector",
        cohort="ordinary", evidence_tier="primary")
    case = OfflineCaseSnapshot(
        provenance["source_provenance_fingerprint"], _fp("producer-decision"), provenance,
        {"producer_revision": "tests.scope/1", "fingerprint": _fp("producer-scope")},
        dict(measure.qoi)["id"], dict(measure.qoi)["unit"], "ready",
        source_fingerprints={
            "design_measure": measure.design_measure_fingerprint,
            "source_draw_identity": measure.source_draw_identity_fingerprint,
            "predictive_basis": dict(measure.source_identities)["predictive_basis_fingerprint"],
            "generator_config": config.fingerprint,
        }, context_transform=transform, candidate_pair=pair)
    variants = (
        QoIDistinctionVariant("coarse", {"origin": 0.0, "bin_width": 1.0, "offsets": [0.0]}),
        QoIDistinctionVariant("fine", {"origin": 0.0, "bin_width": 0.5, "offsets": [0.0]}),
    )
    policy = QoIDistinctionPolicy(
        "policy", "isar.scalar_absolute_partitions/o36.5",
        {"id": case.qoi_id, "unit": case.qoi_unit},
        {"kind": "case_identity", "fingerprint": case.case_identity,
         "annotation": {"campaign_name": "producer", "experiment_description": "real chain",
                        "date": "2026-08-03"}},
        {"kind": "explicit_user_analysis/1", "source_id": "owner",
         "rationale": "exercise producer chain"}, variants)
    calls = {"provider": 0, "evaluator": 0, "scorer": 0}
    original_scorer = compare.score_scan_window_candidates

    def provider(_case):
        calls["provider"] += 1
        inputs = []
        for candidate in (legacy, target):
            calls["evaluator"] += 1
            inputs.append(CandidateEvaluationInput(
                candidate.candidate_id,
                evaluate_master_predictive(
                    predictive, candidate.energies, candidate.ncount)))
        return measure, tuple(inputs)

    def counted_scorer(*args, **kwargs):
        calls["scorer"] += 1
        return original_scorer(*args, **kwargs)

    monkeypatch.setattr(compare, "score_scan_window_candidates", counted_scorer)
    report = score_offline_comparison(
        OfflineCorpusIndex(OfflineDiscoveryConfig(), (case,)),
        QoIDistinctionPolicySet((policy,)),
        OfflineScoringConfig(seed=17, simulation_count=32, block_count=4), provider)
    assert calls == {"provider": 1, "evaluator": 2, "scorer": 2}
    assert report.status == "complete"
    assert len(report.results) == 2
    assert all(result.score_evidence.evidence_fingerprint for result in report.results)

    tampered = report.to_dict()
    tampered["results"][0]["score_evidence"]["legacy_score"][
        "candidate_score_fingerprint"
    ] = "0" * 64
    with pytest.raises(ValueError):
        compare.OfflineComparisonReport.from_dict(tampered)
    with pytest.raises(ValueError, match="ordered and unique"):
        OfflineComparisonReport(
            report.corpus_fingerprint,
            report.policy_set_fingerprint,
            report.scoring_config_fingerprint,
            "complete",
            (report.results[0], report.results[0]),
        )
    with pytest.raises(ValueError, match="status is inconsistent"):
        OfflineComparisonReport(
            report.corpus_fingerprint,
            report.policy_set_fingerprint,
            report.scoring_config_fingerprint,
            "partial",
            report.results,
        )

    def forbidden_provider(_case):
        raise AssertionError("policy refusal must precede evaluation")

    missing = score_offline_comparison(
        OfflineCorpusIndex(OfflineDiscoveryConfig(), (case,)),
        QoIDistinctionPolicySet(()),
        OfflineScoringConfig(seed=17, simulation_count=32, block_count=4),
        forbidden_provider)
    assert missing.results[0].reason_codes == ("qoi_policy_missing",)

    unsupported_policy = QoIDistinctionPolicy(
        "unsupported", "isar.future_partitions/1",
        {"id": case.qoi_id, "unit": case.qoi_unit},
        {"kind": "case_identity", "fingerprint": case.case_identity,
         "annotation": {"campaign_name": "producer", "experiment_description": "unsupported",
                        "date": "2026-08-03"}},
        {"kind": "explicit_user_analysis/1", "source_id": "owner",
         "rationale": "exercise unsupported refusal"}, (variants[0],))
    unsupported = score_offline_comparison(
        OfflineCorpusIndex(OfflineDiscoveryConfig(), (case,)),
        QoIDistinctionPolicySet((unsupported_policy,)),
        OfflineScoringConfig(seed=17, simulation_count=32, block_count=4),
        forbidden_provider)
    assert unsupported.results[0].reason_codes == ("qoi_policy_unsupported",)
