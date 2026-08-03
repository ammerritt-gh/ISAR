"""Pure evidence seams for same-action scan-window EIG.

Candidate construction, scoring, and authority changes belong to later slices.
"""
from .design_measure import ScanWindowDesignMeasure, build_scan_window_design_measure
from .eligibility import EligibilityInput, EligibilityResult, evaluate_eig_eligibility
from .qoi import (
    QoIProjectionEvidence,
    adapt_omega0_target_projection,
    validate_qoi_projection_evidence,
)

__all__ = [
    "QoIProjectionEvidence",
    "ScanWindowDesignMeasure",
    "EligibilityInput",
    "EligibilityResult",
    "adapt_omega0_target_projection",
    "build_scan_window_design_measure",
    "evaluate_eig_eligibility",
    "validate_qoi_projection_evidence",
]
