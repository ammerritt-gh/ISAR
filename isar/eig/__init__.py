"""Pure eligibility seam for same-action scan-window EIG.

The package deliberately answers only whether a selected action may be considered
by the shadow EIG path.  Candidate construction, scoring, and authority changes
belong to later slices.
"""
from .eligibility import EligibilityInput, EligibilityResult, evaluate_eig_eligibility
from .qoi import QoIProjectionEvidence, adapt_omega0_target_projection

__all__ = [
    "QoIProjectionEvidence",
    "EligibilityInput",
    "EligibilityResult",
    "adapt_omega0_target_projection",
    "evaluate_eig_eligibility",
]
