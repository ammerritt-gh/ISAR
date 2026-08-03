"""Pure evidence and scoring seams for same-action scan-window EIG."""
from .design_measure import (
    ScanWindowDesignMeasure,
    build_scan_window_design_measure,
    validate_scan_window_design_measure,
)
from .eligibility import EligibilityInput, EligibilityResult, evaluate_eig_eligibility
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
)

__all__ = [
    "QoIProjectionEvidence",
    "CandidateEvaluationInput",
    "ScalarQoIDiscretization",
    "ScanWindowDesignMeasure",
    "ScanWindowScoreSet",
    "EligibilityInput",
    "EligibilityResult",
    "adapt_omega0_target_projection",
    "build_scan_window_design_measure",
    "evaluate_eig_eligibility",
    "score_scan_window_candidates",
    "validate_qoi_projection_evidence",
    "validate_scan_window_design_measure",
]
