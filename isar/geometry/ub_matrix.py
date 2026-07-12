# Vendored from TAVI (sibling checkout C:\Users\AMM\Documents\Github\Science\TAVI),
# original path tavi/ub_matrix.py.  License: GPL-3.0 (TAVI). ISAR is GPL-3.0, compatible.
# Vendored 2026-07-11, adapted: (1) imports of reciprocal_basis_tas /
# instrument_q_to_component_q / q_instrument_from_angles rewritten from tavi.* to the
# vendored .sample_mount / .tas_geometry; (2) the "Training Mode" section (random-rotation
# exercise generator + XOR/base64 hash encode/decode + check_training_quality) dropped — it
# is a GUI teaching aid unused by the metric/UB layer; (3) 2026-07-12 `refine_lattice_from_peaks`
# replaced the single global-scale fit with a per-system constrained linear least-squares on
# |Q|^2 in the reciprocal metric (recovers anisotropic error for cubic/tetragonal/orthorhombic/
# hexagonal), falling back to the old uniform scale when under-determined; adds a `method` key.
"""UB Matrix calculations for TAVI.

Implements the Busing-Levy (1967) UB matrix formalism for crystal orientation
on a triple-axis neutron spectrometer.

The UB matrix transforms Miller indices (HKL) to mounted-sample Q vectors:
    Q_sample = UB @ [H, K, L]

where:
    B = reciprocal lattice metric matrix in the component's local sample frame
    U = static mount orientation matrix, determined from Bragg peaks
    UB = U @ B (combined transformation into the mounted sample frame)

Convention: scalar lattice parameters use the TAS component frame:
a* along x, b* in the horizontal xz-plane, c* vertical-ish along y.
"""
import math
import numpy as np
from dataclasses import dataclass

from .sample_mount import reciprocal_basis_tas
from .tas_geometry import instrument_q_to_component_q, q_instrument_from_angles


def compute_B_matrix(a, b, c, alpha, beta, gamma):
    """Compute the B matrix (Busing-Levy convention) from lattice parameters.

    The B matrix transforms Miller indices to Cartesian reciprocal-space coordinates:
        Q_crystal = B @ [H, K, L]

    Convention: a* along x, b* in the horizontal xz-plane, c* general.

    Args:
        a, b, c: Lattice parameters in Angstroms.
        alpha, beta, gamma: Lattice angles in degrees.

    Returns:
        np.ndarray: 3x3 B matrix.
    """
    return reciprocal_basis_tas(a, b, c, alpha, beta, gamma)


def angles_to_q_lab(sth, saz, stt, ki, kf):
    """Convert instrument angles to Q vector in lab frame.

    Uses the same public Q convention as PUMA_instrument_definition, then
    converts into mounted component coordinates for UB fitting.

    Args:
        sth: Sample theta (omega) in degrees.
        saz: Sample azimuthal angle in degrees.
        stt: Sample two-theta in degrees.
        ki: Incident wavevector (inverse Angstroms).
        kf: Scattered wavevector (inverse Angstroms).

    Returns:
        np.ndarray: [qx, qy, qz] in inverse Angstroms.
    """
    q_instrument = q_instrument_from_angles(sth, saz, stt, ki, kf)
    return instrument_q_to_component_q(q_instrument)


def validate_rotation_matrix(matrix: np.ndarray, atol: float = 1e-3) -> np.ndarray:
    """Return matrix as float array if it is a proper 3D rotation."""
    rotation = np.asarray(matrix, dtype=float)
    if rotation.shape != (3, 3):
        raise ValueError("Rotation matrix must be 3x3.")
    if not np.all(np.isfinite(rotation)):
        raise ValueError("Rotation matrix contains non-finite values.")
    if not np.allclose(rotation.T @ rotation, np.eye(3), atol=atol):
        raise ValueError("Derived U is not orthogonal; UB does not describe a physical mount rotation.")
    det = float(np.linalg.det(rotation))
    if not math.isclose(det, 1.0, abs_tol=atol):
        raise ValueError(f"Derived U must be a proper rotation with det=+1; got det={det:.6g}.")
    return rotation


@dataclass
class ObservedPeak:
    """A Bragg peak observation with HKL and instrument angles.

    Attributes:
        hkl: Miller indices (H, K, L).
        angles: Instrument angles (omega/sth, chi/saz, stt) in degrees.
        ki: Incident wavevector at observation (inverse Angstroms).
        kf: Scattered wavevector at observation (inverse Angstroms).
        locked: Whether this peak entry is locked from editing.
    """
    hkl: tuple = (0.0, 0.0, 0.0)
    angles: tuple = (0.0, 0.0, 0.0)  # (sth, saz, stt)
    ki: float = 0.0
    kf: float = 0.0
    locked: bool = False

    @property
    def q_lab(self) -> np.ndarray:
        """Compute Q in mounted sample frame from stored angles and wavevectors."""
        sth, saz, stt = self.angles
        if self.ki <= 0 or self.kf <= 0:
            return np.array([0.0, 0.0, 0.0])
        return angles_to_q_lab(sth, saz, stt, self.ki, self.kf)

    @property
    def is_valid(self) -> bool:
        """Check if this peak has enough data for UB calculation."""
        h, k, l = self.hkl
        if h == 0 and k == 0 and l == 0:
            return False
        if self.ki <= 0 or self.kf <= 0:
            return False
        q = self.q_lab
        return np.linalg.norm(q) > 1e-6

    def to_dict(self) -> dict:
        """Serialize to dictionary for JSON storage."""
        return {
            'hkl': list(self.hkl),
            'angles': list(self.angles),
            'ki': self.ki,
            'kf': self.kf,
            'locked': self.locked,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'ObservedPeak':
        """Deserialize from dictionary."""
        return cls(
            hkl=tuple(d.get('hkl', [0, 0, 0])),
            angles=tuple(d.get('angles', [0, 0, 0])),
            ki=d.get('ki', 0.0),
            kf=d.get('kf', 0.0),
            locked=d.get('locked', False),
        )


def calculate_U_two_peaks(peak1: ObservedPeak, peak2: ObservedPeak,
                          B: np.ndarray) -> np.ndarray:
    """Calculate the U orientation matrix from two observed Bragg peaks.

    Uses the Busing-Levy (1967) method:
    1. Compute Q vectors in crystal frame (B @ hkl) and lab frame (from angles).
    2. Build orthonormal triads in both frames.
    3. U = T_lab @ T_crystal^(-1)

    Args:
        peak1: First observed Bragg peak.
        peak2: Second observed Bragg peak.
        B: 3x3 B matrix.

    Returns:
        np.ndarray: 3x3 U matrix (orthogonal, det ~ +1).

    Raises:
        ValueError: If peaks are collinear or invalid.
    """
    # Crystal-frame Q vectors
    q1_c = B @ np.array(peak1.hkl)
    q2_c = B @ np.array(peak2.hkl)

    # Lab-frame Q vectors
    q1_l = peak1.q_lab
    q2_l = peak2.q_lab

    # Validate
    for label, v in [("peak1 crystal", q1_c), ("peak2 crystal", q2_c),
                     ("peak1 lab", q1_l), ("peak2 lab", q2_l)]:
        if np.linalg.norm(v) < 1e-8:
            raise ValueError(f"Zero-length Q vector for {label}.")

    # Build orthonormal triad in crystal frame
    t1_c = q1_c / np.linalg.norm(q1_c)
    cross_c = np.cross(q1_c, q2_c)
    if np.linalg.norm(cross_c) < 1e-8:
        raise ValueError("Peaks are collinear in crystal frame — cannot determine U.")
    t2_c = cross_c / np.linalg.norm(cross_c)
    t3_c = np.cross(t1_c, t2_c)
    T_crystal = np.column_stack([t1_c, t2_c, t3_c])

    # Build orthonormal triad in lab frame
    t1_l = q1_l / np.linalg.norm(q1_l)
    cross_l = np.cross(q1_l, q2_l)
    if np.linalg.norm(cross_l) < 1e-8:
        raise ValueError("Peaks are collinear in lab frame — cannot determine U.")
    t2_l = cross_l / np.linalg.norm(cross_l)
    t3_l = np.cross(t1_l, t2_l)
    T_lab = np.column_stack([t1_l, t2_l, t3_l])

    # U = T_lab @ T_crystal^(-1)
    # Since T_crystal is orthonormal, T_crystal^(-1) = T_crystal^T
    U = T_lab @ T_crystal.T

    return U


def refine_U_matrix(peaks: list, B: np.ndarray) -> np.ndarray:
    """Calculate U from multiple peaks using SVD-based Procrustes solution.

    Minimizes sum_i ||q_lab_i - U @ B @ hkl_i||^2 subject to U being orthogonal.

    Falls back to two-peak method if only 2 valid peaks.

    Args:
        peaks: List of ObservedPeak instances.
        B: 3x3 B matrix.

    Returns:
        np.ndarray: 3x3 U matrix (orthogonal, det ~ +1).
    """
    valid_peaks = [p for p in peaks if p.is_valid]
    if len(valid_peaks) < 2:
        raise ValueError(f"Need at least 2 valid peaks, got {len(valid_peaks)}.")
    if len(valid_peaks) == 2:
        return calculate_U_two_peaks(valid_peaks[0], valid_peaks[1], B)

    # Build paired point sets: q_crystal (P) and q_lab (Q)
    # We want U such that Q ~ U @ P
    P = np.zeros((3, len(valid_peaks)))
    Q = np.zeros((3, len(valid_peaks)))

    for i, peak in enumerate(valid_peaks):
        P[:, i] = B @ np.array(peak.hkl)
        Q[:, i] = peak.q_lab

    # Cross-covariance matrix
    H_mat = P @ Q.T  # Note: we want U s.t. Q = U @ P, so H = P @ Q^T

    # SVD
    Usvd, S, Vt = np.linalg.svd(H_mat)

    # Ensure proper rotation (det = +1, not reflection)
    d = np.linalg.det(Vt.T @ Usvd.T)
    D = np.diag([1, 1, np.sign(d)])

    U = Vt.T @ D @ Usvd.T

    return U


# Systems whose fixed angles make the reciprocal metric tensor known up to the free
# lengths, so |Q(hkl)|^2 is LINEAR in a small set of reciprocal-metric parameters and can be
# fit with a constrained linear least-squares (rather than a single global scale factor).
_METRIC_SYSTEMS = ("cubic", "tetragonal", "orthorhombic", "hexagonal")

_SYSTEM_ANGLES = {
    "cubic": (90.0, 90.0, 90.0),
    "tetragonal": (90.0, 90.0, 90.0),
    "orthorhombic": (90.0, 90.0, 90.0),
    "hexagonal": (90.0, 90.0, 120.0),
}

_SYSTEM_N_PARAMS = {"cubic": 1, "tetragonal": 2, "orthorhombic": 3, "hexagonal": 2}


def _metric_design_row(system: str, h, k, l) -> list:
    """Row of the |Q|^2 design matrix for one reflection, per crystal system.

    Coefficients multiply the reciprocal-metric parameters (squared reciprocal lengths).
    Derived from |Q(hkl)|^2 = (2*pi)^2 * [hkl] G^{-1} [hkl]^T with the system's fixed angles:
      * orthogonal (cubic/tetragonal/orthorhombic): G^{-1} diagonal -> h^2/a^2 etc.
      * hexagonal (gamma=120): the a-b block gives (h^2 + k^2 + hk) sharing one parameter.
    """
    h, k, l = float(h), float(k), float(l)
    if system == "cubic":
        return [h * h + k * k + l * l]
    if system == "tetragonal":
        return [h * h + k * k, l * l]
    if system == "orthorhombic":
        return [h * h, k * k, l * l]
    # hexagonal
    return [h * h + k * k + h * k, l * l]


def _lengths_from_metric_params(system: str, params) -> tuple:
    """Recover direct-lattice lengths from the fitted reciprocal-metric parameters.

    Each parameter is the coefficient of its design column. For the orthogonal systems that
    coefficient is (2*pi/length)^2 so length = 2*pi/sqrt(param). For hexagonal the shared
    a-b parameter is 4*(2*pi)^2/(3 a^2), so a = 2*(2*pi)/sqrt(3*param); c is orthogonal.
    """
    two_pi = 2.0 * math.pi
    if system == "cubic":
        a = two_pi / math.sqrt(params[0])
        return (a, a, a)
    if system == "tetragonal":
        a = two_pi / math.sqrt(params[0])
        c = two_pi / math.sqrt(params[1])
        return (a, a, c)
    if system == "orthorhombic":
        a = two_pi / math.sqrt(params[0])
        b = two_pi / math.sqrt(params[1])
        c = two_pi / math.sqrt(params[2])
        return (a, b, c)
    # hexagonal
    a = 2.0 * two_pi / math.sqrt(3.0 * params[0])
    c = two_pi / math.sqrt(params[1])
    return (a, a, c)


def _metric_lsq_refine(observed: list, crystal_system: str):
    """Constrained linear LSQ of |Q|^2 in reciprocal-metric parameters.

    Returns the refined ``(a, b, c, alpha, beta, gamma)`` tuple, or ``None`` to signal that
    the fit was under-determined (too few peaks / rank-deficient design, e.g. only (h,k,0)
    peaks for a tetragonal cell) or unphysical (a non-positive fitted parameter), so the
    caller should fall back to the uniform-scale path.
    """
    n_params = _SYSTEM_N_PARAMS[crystal_system]
    rows = [_metric_design_row(crystal_system, *hkl) for hkl, _ in observed]
    A = np.array(rows, dtype=float)
    y = np.array([q_obs * q_obs for _, q_obs in observed], dtype=float)
    if A.shape[0] < n_params or np.linalg.matrix_rank(A) < n_params:
        return None
    params, *_ = np.linalg.lstsq(A, y, rcond=None)
    if not np.all(np.isfinite(params)) or np.any(params <= 0):
        return None
    lengths = _lengths_from_metric_params(crystal_system, params)
    return (*lengths, *_SYSTEM_ANGLES[crystal_system])


def _uniform_scale_refine(observed: list, initial_lattice: tuple, crystal_system: str):
    """Legacy single-global-scale refinement (isotropic |Q| scaling).

    Returns the refined lattice tuple, or ``None`` when no peak has a usable calculated |Q|.
    Cannot capture genuinely anisotropic error (e.g. tetragonal a vs c); it is the fallback
    for under-determined metric fits and the only option for general/monoclinic/triclinic.
    """
    a0, b0, c0, al0, be0, ga0 = initial_lattice
    B0 = compute_B_matrix(a0, b0, c0, al0, be0, ga0)
    scale_ratios = []
    for hkl, q_obs in observed:
        q_calc = np.linalg.norm(B0 @ np.array(hkl, dtype=float))
        if q_calc > 1e-8:
            scale_ratios.append(q_obs / q_calc)
    if not scale_ratios:
        return None
    # |Q| ∝ 1/a, so avg_scale = mean(q_obs/q_calc) > 1 means observed |Q| is larger →
    # real-space lattice constants are smaller → divide by avg_scale.
    avg_scale = float(np.mean(scale_ratios))
    if crystal_system == "cubic":
        a_new = a0 / avg_scale
        return (a_new, a_new, a_new, 90.0, 90.0, 90.0)
    if crystal_system == "tetragonal":
        return (a0 / avg_scale, a0 / avg_scale, c0 / avg_scale, 90.0, 90.0, 90.0)
    if crystal_system == "hexagonal":
        return (a0 / avg_scale, a0 / avg_scale, c0 / avg_scale, 90.0, 90.0, 120.0)
    if crystal_system == "orthorhombic":
        return (a0 / avg_scale, b0 / avg_scale, c0 / avg_scale, 90.0, 90.0, 90.0)
    # General: uniform scaling of lengths, angles left untouched.
    return (a0 / avg_scale, b0 / avg_scale, c0 / avg_scale, al0, be0, ga0)


def refine_lattice_from_peaks(peaks: list, initial_lattice: tuple,
                              crystal_system: str = None) -> dict:
    """Refine lattice parameters from observed peak |Q| magnitudes.

    For the right-angled and hexagonal systems (cubic/tetragonal/orthorhombic/hexagonal)
    the fixed angles make |Q(hkl)|^2 linear in a few reciprocal-metric parameters, so a
    constrained linear least-squares recovers genuinely anisotropic error (e.g. tetragonal
    a vs c) that a single global scale factor cannot — ``method`` = ``"metric_lsq"``. If that
    fit is under-determined or unphysical it falls back to the isotropic single-scale path
    (``method`` = ``"uniform_scale_fallback"``). For ``crystal_system`` None or any other
    value (monoclinic/triclinic/general) the isotropic single-scale path is used directly
    (``method`` = ``"uniform_scale"``); note angles cannot be refined from |Q| magnitudes
    alone, so they are carried through unchanged in that case.

    Args:
        peaks: List of ObservedPeak instances.
        initial_lattice: (a, b, c, alpha, beta, gamma) initial guess.
        crystal_system: Optional crystal system name to constrain refinement.

    Returns:
        dict with 'lattice' (refined params), 'residuals' (per-peak), 'rms_error', and the
        additive 'method' key naming which path produced the result.
    """
    valid_peaks = [p for p in peaks if p.is_valid]
    if len(valid_peaks) < 1:
        raise ValueError("Need at least 1 valid peak for lattice refinement.")

    # Collect observed |Q| for each peak.
    observed = []
    for peak in valid_peaks:
        q_obs = np.linalg.norm(peak.q_lab)
        observed.append((tuple(peak.hkl), q_obs))

    refined = None
    method = None
    if crystal_system in _METRIC_SYSTEMS:
        refined = _metric_lsq_refine(observed, crystal_system)
        if refined is not None:
            method = "metric_lsq"
    if refined is None:
        refined = _uniform_scale_refine(observed, initial_lattice, crystal_system)
        method = ("uniform_scale_fallback" if crystal_system in _METRIC_SYSTEMS
                  else "uniform_scale")
        if refined is None:
            # No peak yields a usable calculated |Q| — nothing to refine against.
            return {
                'lattice': initial_lattice,
                'residuals': [],
                'rms_error': float('inf'),
                'method': method,
            }

    # Compute residuals with the refined lattice (recomputed from `refined`, as before).
    B_new = compute_B_matrix(*refined)
    final_residuals = []
    for hkl, q_obs in observed:
        q_calc = np.linalg.norm(B_new @ np.array(hkl, dtype=float))
        final_residuals.append({
            'hkl': hkl,
            'q_obs': q_obs,
            'q_calc': q_calc,
            'delta_q': q_obs - q_calc,
            'd_obs': 2*math.pi/q_obs if q_obs > 0 else 0,
            'd_calc': 2*math.pi/q_calc if q_calc > 0 else 0,
        })

    rms = math.sqrt(np.mean([(r['delta_q'])**2 for r in final_residuals]))

    return {
        'lattice': refined,
        'residuals': final_residuals,
        'rms_error': rms,
        'method': method,
    }


def get_scattering_plane_info(U: np.ndarray, B: np.ndarray) -> dict:
    """Analyze the scattering plane defined by the current UB matrix.

    The instrument scattering plane is the mounted sample xz-plane (qy=0).
    With U applied, crystal directions map to mounted sample frame via U @ B.

    Args:
        U: 3x3 orientation matrix.
        B: 3x3 B matrix.

    Returns:
        dict with scattering plane analysis.
    """
    UB = U @ B

    # The mounted sample y-axis (out of scattering plane) in crystal HKL coordinates.
    try:
        UB_inv = np.linalg.inv(UB)
    except np.linalg.LinAlgError:
        return {
            'plane_normal_hkl': (0, 0, 0),
            'in_plane_vector1_hkl': (0, 0, 0),
            'in_plane_vector2_hkl': (0, 0, 0),
            'chi_misalignment_deg': 0.0,
            'omega_offset_deg': 0.0,
        }

    y_sample = np.array([0.0, 1.0, 0.0])
    plane_normal_hkl = UB_inv @ y_sample

    # In-plane vectors: mounted sample x and z in HKL space.
    x_sample = np.array([1.0, 0.0, 0.0])
    z_sample = np.array([0.0, 0.0, 1.0])
    in_plane_v1 = UB_inv @ x_sample
    in_plane_v2 = UB_inv @ z_sample

    # Chi misalignment: angle between crystal c* axis and vertical sample y.
    # c* direction in crystal frame = B @ [0,0,1]
    c_star_crystal = B @ np.array([0.0, 0.0, 1.0])
    c_star_lab = U @ c_star_crystal
    c_star_lab_norm = c_star_lab / np.linalg.norm(c_star_lab)

    # Angle from horizontal plane (complement of angle with z)
    chi_mis = math.degrees(math.asin(np.clip(c_star_lab_norm[1], -1, 1)))

    # Omega offset: rotation of a* from sample x in the horizontal xz-plane.
    a_star_crystal = B @ np.array([1.0, 0.0, 0.0])
    a_star_lab = U @ a_star_crystal
    omega_offset = math.degrees(math.atan2(a_star_lab[2], a_star_lab[0]))

    return {
        'plane_normal_hkl': tuple(plane_normal_hkl),
        'in_plane_vector1_hkl': tuple(in_plane_v1),
        'in_plane_vector2_hkl': tuple(in_plane_v2),
        'chi_misalignment_deg': chi_mis,
        'omega_offset_deg': omega_offset,
    }


class UBMatrix:
    """Manages the UB matrix for crystal orientation on a TAS.

    The UB matrix transforms Miller indices to mounted-sample Q:
        Q_sample = UB @ [H, K, L]

    where B encodes the lattice and U encodes the crystal orientation.
    """

    def __init__(self, a=4.05, b=4.05, c=4.05, alpha=90, beta=90, gamma=90):
        """Initialize with lattice parameters. U defaults to identity."""
        self._lattice = (float(a), float(b), float(c),
                         float(alpha), float(beta), float(gamma))
        self._B = compute_B_matrix(*self._lattice)
        self._U = np.eye(3)
        self._UB = self._U @ self._B
        self.peaks: list = []

    @property
    def lattice(self) -> tuple:
        """Current lattice parameters (a, b, c, alpha, beta, gamma)."""
        return self._lattice

    @property
    def B(self) -> np.ndarray:
        """Current B matrix."""
        return self._B.copy()

    @property
    def U(self) -> np.ndarray:
        """Current U orientation matrix."""
        return self._U.copy()

    @property
    def UB(self) -> np.ndarray:
        """Current UB matrix."""
        return self._UB.copy()

    @property
    def is_identity(self) -> bool:
        """True if U is the identity matrix (no orientation set)."""
        return np.allclose(self._U, np.eye(3), atol=1e-6)

    def set_lattice(self, a, b, c, alpha, beta, gamma):
        """Update lattice parameters, recompute B and UB."""
        self._lattice = (float(a), float(b), float(c),
                         float(alpha), float(beta), float(gamma))
        self._B = compute_B_matrix(*self._lattice)
        self._UB = self._U @ self._B

    def set_U(self, U: np.ndarray):
        """Set the U orientation matrix directly."""
        U = validate_rotation_matrix(U)
        self._U = U.copy()
        self._UB = self._U @ self._B

    def set_UB(self, UB: np.ndarray):
        """Set the UB matrix directly, extract U = UB @ B^(-1)."""
        UB = np.asarray(UB, dtype=float)
        if UB.shape != (3, 3):
            raise ValueError("UB must be a 3x3 matrix.")
        try:
            B_inv = np.linalg.inv(self._B)
        except np.linalg.LinAlgError as exc:
            raise ValueError("Cannot extract U because the B matrix is singular.") from exc
        U = validate_rotation_matrix(UB @ B_inv)
        self._U = U.copy()
        self._UB = UB.copy()

    def reset_U(self):
        """Reset U to identity (no orientation)."""
        self._U = np.eye(3)
        self._UB = self._U @ self._B

    def hkl_to_q(self, H, K, L) -> tuple:
        """Convert Miller indices to lab-frame Q using the UB matrix.

        Args:
            H, K, L: Miller indices.

        Returns:
            tuple: (qx, qy, qz) in inverse Angstroms.
        """
        q = self._UB @ np.array([float(H), float(K), float(L)])
        return float(q[0]), float(q[1]), float(q[2])

    def q_to_hkl(self, qx, qy, qz) -> tuple:
        """Convert lab-frame Q to Miller indices using the UB matrix.

        Args:
            qx, qy, qz: Q components in inverse Angstroms.

        Returns:
            tuple: (H, K, L) Miller indices.

        Raises:
            np.linalg.LinAlgError: If UB matrix is singular.
        """
        q = np.array([float(qx), float(qy), float(qz)])
        hkl = np.linalg.solve(self._UB, q)
        return float(hkl[0]), float(hkl[1]), float(hkl[2])

    def calculate_U_from_peaks(self) -> np.ndarray:
        """Calculate U from stored peaks and apply it.

        Returns:
            np.ndarray: The calculated U matrix.
        """
        valid = [p for p in self.peaks if p.is_valid]
        if len(valid) < 2:
            raise ValueError(f"Need at least 2 valid peaks, have {len(valid)}.")

        if len(valid) == 2:
            U = calculate_U_two_peaks(valid[0], valid[1], self._B)
        else:
            U = refine_U_matrix(valid, self._B)

        self.set_U(U)
        return U

    def refine_lattice(self, crystal_system: str = None) -> dict:
        """Refine lattice parameters from stored peaks.

        Returns:
            dict with 'lattice', 'residuals', 'rms_error'.
        """
        return refine_lattice_from_peaks(self.peaks, self._lattice, crystal_system)

    def get_plane_info(self) -> dict:
        """Get scattering plane analysis."""
        return get_scattering_plane_info(self._U, self._B)

    def to_dict(self) -> dict:
        """Serialize UB matrix state for JSON storage."""
        return {
            'lattice': list(self._lattice),
            'U': self._U.tolist(),
            'peaks': [p.to_dict() for p in self.peaks],
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'UBMatrix':
        """Restore UB matrix state from dictionary."""
        lattice = tuple(d.get('lattice', [4.05, 4.05, 4.05, 90, 90, 90]))
        ub = cls(*lattice)
        U = d.get('U')
        if U is not None:
            ub.set_U(np.array(U))
        peaks_data = d.get('peaks', [])
        ub.peaks = [ObservedPeak.from_dict(p) for p in peaks_data]
        return ub
