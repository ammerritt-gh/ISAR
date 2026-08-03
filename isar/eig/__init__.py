"""Pure evidence and scoring seams for same-action scan-window EIG."""
from .design_measure import (
    ScanWindowDesignMeasure,
    build_scan_window_design_measure,
    validate_scan_window_design_measure,
)
from .eligibility import EligibilityInput, EligibilityResult, evaluate_eig_eligibility
from .policy import compile_qoi_distinction_policy
from .qoi import (
    QoIProjectionEvidence,
    adapt_omega0_target_projection,
    validate_qoi_projection_evidence,
)
from .scorer import (
    CandidateEvaluationInput,
    ScalarQoIDiscretization,
    ScanWindowScoreSet,
    score_scan_window_candidates,
    validate_scan_window_score_set,
)
from .shadow_inventory import (
    ScanDesignShadowScoring,
    ScanDesignShadowVariant,
    ShadowSeedEvidence,
    derive_shadow_seed,
    score_shadow_inventory,
    validate_scan_design_shadow_scoring,
)
from isar.schema.eig_policy import (
    QoIDistinctionPolicy,
    QoIDistinctionPolicySet,
    QoIDistinctionVariant,
    ScanWindowEIGConfig,
    ScanWindowShadowScoringConfig,
    matching_qoi_policies,
    validate_qoi_distinction_policy,
    validate_qoi_distinction_policy_set,
    validate_scan_window_eig_config,
    validate_scan_window_shadow_scoring_config,
)

__all__ = [
    "QoIProjectionEvidence",
    "QoIDistinctionPolicy",
    "QoIDistinctionPolicySet",
    "QoIDistinctionVariant",
    "CandidateEvaluationInput",
    "ScalarQoIDiscretization",
    "ScanWindowDesignMeasure",
    "ScanWindowScoreSet",
    "EligibilityInput",
    "EligibilityResult",
    "ScanWindowEIGConfig",
    "ScanWindowShadowScoringConfig",
    "ScanDesignShadowScoring",
    "ScanDesignShadowVariant",
    "ShadowSeedEvidence",
    "adapt_omega0_target_projection",
    "build_scan_window_design_measure",
    "evaluate_eig_eligibility",
    "matching_qoi_policies",
    "compile_qoi_distinction_policy",
    "score_scan_window_candidates",
    "score_shadow_inventory",
    "derive_shadow_seed",
    "validate_qoi_projection_evidence",
    "validate_qoi_distinction_policy",
    "validate_qoi_distinction_policy_set",
    "validate_scan_window_eig_config",
    "validate_scan_window_shadow_scoring_config",
    "validate_scan_window_design_measure",
    "validate_scan_window_score_set",
    "validate_scan_design_shadow_scoring",
]
