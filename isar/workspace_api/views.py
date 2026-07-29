"""Read-only view adapters for :mod:`isar.workspace_api`.

Small, Qt-free value objects the GUI (and a notebook) consume. Every field degrades to ``None``
rather than raising when an artifact predates a column: a :class:`FitView` built from an old
``results.csv`` row (no bayes / amplitudes columns) exposes those fields as ``None``, never a
``KeyError``. Views do no file I/O — they wrap already-parsed dicts, so constructing them is cheap
and repeatable (the parsing/caching lives in :mod:`isar.workspace_api.workspace`).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# small coercion helpers
# ---------------------------------------------------------------------------
def _f(v) -> float | None:
    """Best-effort float; ``None`` on empty / unparseable (never raises)."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _b(v) -> bool | None:
    """Coerce a stored bool / bool-repr string to ``bool``; ``None`` when absent/blank."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "1"):
        return True
    if s in ("false", "0"):
        return False
    return None


def _complete_tuple(*vals):
    """A tuple of the given values only when EVERY component is non-``None``; else ``None``.

    A partially-populated CSV row (e.g. ``interval_lo`` set, ``interval_hi`` blank) must NOT shadow a
    complete interval carried on the outcome — so a partial row yields ``None`` and the property
    falls through to the ledger form."""
    if any(v is None for v in vals):
        return None
    return tuple(vals)


def _seq_or_none(v):
    """Pass a list/tuple through as a tuple; ``None`` otherwise (so an interval stored as a list
    in ``session.json`` becomes a tuple, and a missing one stays ``None``)."""
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        return tuple(v)
    return None


def _json_value(v):
    """Decode an additive structured CSV cell while preserving already-decoded ledger values."""
    if not isinstance(v, str):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# FitView — one scan x detector fit, unified across results.csv and session.json
# ---------------------------------------------------------------------------
class FitView:
    """A single fit outcome, exposing the full ``FitOutcome`` surface read-only.

    Built either from a flattened ``results.csv`` row (``from_row``) — optionally enriched with the
    matching ``session.json`` detector payload — or straight from a stored ``FitOutcome`` dict
    (``from_outcome``). The two sources carry the same information in different shapes (the CSV
    splits an interval into ``*_lo``/``*_hi`` columns; the ledger keeps it as a list), and this view
    normalizes them to one contract. Missing fields are ``None``.
    """

    __slots__ = ("_row", "_outcome", "scan_number", "detector_id", "unit_id")

    def __init__(self, *, row: dict | None, outcome: dict | None,
                 scan_number: int | None, detector_id: str | None, unit_id: str | None = None):
        self._row = row or {}
        self._outcome = outcome or {}
        self.scan_number = scan_number
        self.detector_id = detector_id
        self.unit_id = unit_id

    # -- constructors --------------------------------------------------------
    @classmethod
    def from_row(cls, row: dict, outcome: dict | None = None) -> "FitView":
        scan = row.get("scan")
        try:
            scan = int(scan) if scan is not None and scan != "" else None
        except (TypeError, ValueError):
            scan = None
        return cls(row=row, outcome=outcome, scan_number=scan,
                   detector_id=row.get("detector") or (outcome or {}).get("detector_id"),
                   unit_id=row.get("scan_id") or None)

    @classmethod
    def from_outcome(cls, outcome: dict, *, scan_number: int | None,
                     unit_id: str | None = None) -> "FitView":
        return cls(row=None, outcome=outcome, scan_number=scan_number,
                   detector_id=outcome.get("detector_id"), unit_id=unit_id)

    # -- internal source picking --------------------------------------------
    def _pick(self, row_key: str | None, out_key: str | None):
        """Value from the row (preferred, it is the delivered/flattened form) else the outcome."""
        if row_key is not None and self._row.get(row_key) not in (None, ""):
            return self._row.get(row_key)
        if out_key is not None:
            return self._outcome.get(out_key)
        return None

    # -- identity / provenance ----------------------------------------------
    @property
    def sample(self) -> str | None:
        return self._pick("sample", "sample")

    @property
    def status(self) -> str | None:
        return self._row.get("status") or None

    @property
    def T(self) -> float | None:
        return _f(self._pick("T", "T"))

    @property
    def q(self) -> list | None:
        if self._outcome.get("q") is not None:
            return list(self._outcome["q"])
        h, k, l = self._row.get("h"), self._row.get("k"), self._row.get("l")
        if h is None and k is None and l is None:
            return None
        return [_f(h), _f(k), _f(l)]

    # -- verdict / disposition ----------------------------------------------
    @property
    def verdict(self) -> str | None:
        return self._pick("verdict", "verdict")

    @property
    def disposition(self) -> str | None:
        return self._pick("disposition", "disposition")

    @property
    def confidence(self) -> float | None:
        return _f(self._pick("confidence", "confidence"))

    @property
    def modality(self) -> str | None:
        # no CSV column; ledger-only.
        return self._outcome.get("modality")

    @property
    def best_label(self) -> str | None:
        return self._pick("best_model", "best_label")

    # -- primary numbers -----------------------------------------------------
    @property
    def omega0(self) -> float | None:
        return _f(self._pick("omega0", "omega0"))

    @property
    def omega0_err(self) -> float | None:
        return _f(self._pick("omega0_err", "omega0_err"))

    @property
    def gamma(self) -> float | None:
        return _f(self._pick("gamma", "gamma"))

    @property
    def gamma_provisional(self) -> bool | None:
        return _b(self._pick("gamma_provisional", "gamma_provisional"))

    @property
    def cost_per_dof(self) -> float | None:
        return _f(self._pick("cost_per_dof", "cost_per_dof"))

    @property
    def fit_kind(self) -> str | None:
        return self._pick("fit_kind", "fit_kind")

    @property
    def n_modes(self) -> int | None:
        v = self._pick("n_modes", "n_modes")
        try:
            return int(v) if v is not None and v != "" else None
        except (TypeError, ValueError):
            return None

    @property
    def secondary_omega0(self) -> float | None:
        return _f(self._pick("secondary_omega0", None))

    @property
    def secondary_omega0_err(self) -> float | None:
        return _f(self._pick("secondary_omega0_err", None))

    # -- interval ladder -----------------------------------------------------
    @property
    def interval(self):
        if self._row:
            t = _complete_tuple(_f(self._row.get("interval_lo")), _f(self._row.get("interval_hi")))
            if t is not None:
                return t
        return _seq_or_none(self._outcome.get("interval"))

    @property
    def interval_reliable(self) -> bool | None:
        return _b(self._pick("interval_reliable", "interval_reliable"))

    @property
    def omega0_profile(self):
        if self._row:
            t = _complete_tuple(_f(self._row.get("omega0_profile_lo")),
                                _f(self._row.get("omega0_profile_hi")))
            if t is not None:
                return t
        return _seq_or_none(self._outcome.get("omega0_profile"))

    @property
    def hesse_interval(self):
        if self._row:
            t = _complete_tuple(_f(self._row.get("hesse_lo")), _f(self._row.get("hesse_hi")))
            if t is not None:
                return t
        return _seq_or_none(self._outcome.get("hesse_interval"))

    @property
    def hesse_trusted(self) -> bool | None:
        return _b(self._pick("hesse_trusted", "hesse_trusted"))

    @property
    def bayes_interval(self):
        if self._row:
            t = _complete_tuple(_f(self._row.get("bayes_lo")), _f(self._row.get("bayes_med")),
                                _f(self._row.get("bayes_hi")))
            if t is not None:
                return t
        return _seq_or_none(self._outcome.get("bayes_interval"))

    @property
    def bayes_ess(self) -> float | None:
        return _f(self._pick("bayes_ess", "bayes_ess"))

    @property
    def bayes_freq_ratio(self) -> float | None:
        return _f(self._pick("bayes_freq_ratio", "bayes_freq_ratio"))

    @property
    def hesse_freq_ratio(self) -> float | None:
        return _f(self._pick("hesse_freq_ratio", "hesse_freq_ratio"))

    @property
    def log_evidence(self) -> float | None:
        return _f(self._pick("log_evidence", "log_evidence"))

    @property
    def log_evidence_err(self) -> float | None:
        return _f(self._pick("log_evidence_err", "log_evidence_err"))

    # -- pooled / master -----------------------------------------------------
    @property
    def pooled(self) -> bool | None:
        return _b(self._pick("pooled", "pooled"))

    @property
    def n_members(self) -> int | None:
        v = self._pick("n_members", "n_members")
        try:
            return int(v) if v is not None and v != "" else None
        except (TypeError, ValueError):
            return None

    @property
    def members(self) -> list | None:
        v = self._pick("members", "members")
        if v is None:
            return None
        if isinstance(v, (list, tuple)):
            return list(v)
        return None

    @property
    def spectrum_ref(self) -> str | None:
        value = self._pick("spectrum_ref", "spectrum_ref")
        return str(value) if value not in (None, "") else None

    @property
    def predictive_ref(self) -> str | None:
        value = self._pick("predictive_ref", "predictive_ref")
        return str(value) if value not in (None, "") else None

    @property
    def target_projection_ref(self) -> str | None:
        value = self._pick("target_projection_ref", "target_projection_ref")
        return str(value) if value not in (None, "") else None

    @property
    def master_revision(self) -> str | None:
        value = self._pick("master_revision", "master_revision")
        return str(value) if value not in (None, "") else None

    @property
    def acquisition_id(self) -> str | None:
        value = self._pick("acquisition_id", "acquisition_id")
        return str(value) if value not in (None, "") else None

    @property
    def target_branch(self) -> str | None:
        value = self._pick("target_branch", "target_branch")
        return str(value) if value not in (None, "") else None

    @property
    def model_interval(self) -> tuple | None:
        value = self._outcome.get("model_interval")
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            return tuple(_f(item) for item in value)
        lo = _f(self._row.get("model_interval_lo"))
        med = _f(self._row.get("model_interval_med"))
        hi = _f(self._row.get("model_interval_hi"))
        return (lo, med, hi) if lo is not None and hi is not None else None

    @property
    def model_robust(self) -> bool | None:
        return _b(self._pick("model_robust", "model_robust"))

    @property
    def model_entropy(self) -> float | None:
        return _f(self._pick("model_entropy", "model_entropy"))

    @property
    def gamma_at_bound(self) -> bool | None:
        return _b(self._pick("gamma_at_bound", "gamma_at_bound"))

    @property
    def linewidth_state(self) -> str | None:
        return self._pick("linewidth_state", "linewidth_state")

    @property
    def pool_consistent(self) -> bool | None:
        return _b(self._pick("pool_consistent", "pool_consistent"))

    @property
    def consistency_pvalue(self) -> float | None:
        return _f(self._pick("consistency_pvalue", "consistency_pvalue"))

    # -- branch --------------------------------------------------------------
    @property
    def branch_id(self) -> str | None:
        return self._pick("branch_id", "branch_id")

    @property
    def assignment_entropy(self) -> float | None:
        return _f(self._pick("assignment_entropy", "assignment_entropy"))

    @property
    def resolvable(self) -> bool | None:
        return _b(self._pick("resolvable", "resolvable"))

    @property
    def branch_weight(self) -> float | None:
        return _f(self._pick(None, "branch_weight"))

    @property
    def branch_candidates(self) -> list | None:
        v = _json_value(self._pick("branch_candidates", "branch_candidates"))
        return list(v) if isinstance(v, (list, tuple)) else None

    @property
    def selected_candidate_index(self) -> int | None:
        v = self._pick("selected_candidate_index", "selected_candidate_index")
        try:
            return int(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    @property
    def track_state(self) -> str | None:
        return self._pick("track_state", "track_state")

    @property
    def track_revision(self) -> int | None:
        v = self._pick("track_revision", "track_revision")
        try:
            return int(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    # -- structured payloads -------------------------------------------------
    @property
    def amplitudes(self) -> dict | None:
        v = self._pick("amplitudes", "amplitudes")
        return v if isinstance(v, dict) else None

    @property
    def model_manifest(self) -> dict | None:
        v = _json_value(self._pick("model_manifest", "model_manifest"))
        return dict(v) if isinstance(v, dict) else None

    @property
    def candidates(self) -> list | None:
        v = self._pick("candidates", "candidates")
        return v if isinstance(v, list) else None

    @property
    def calib(self) -> dict | None:
        v = self._outcome.get("calib")
        return v if isinstance(v, dict) else None

    @property
    def descriptor(self) -> dict | None:
        v = self._outcome.get("descriptor")
        return v if isinstance(v, dict) else None

    @property
    def intensity(self) -> dict | None:
        v = self._outcome.get("intensity")
        return v if isinstance(v, dict) else None

    @property
    def prior_used(self) -> dict | None:
        v = self._outcome.get("prior_used")
        return v if isinstance(v, dict) else None

    @property
    def modes(self) -> list | None:
        v = self._outcome.get("modes")
        return list(v) if isinstance(v, (list, tuple)) else None

    @property
    def secondary_features(self) -> list | None:
        v = self._outcome.get("secondary_features")
        return list(v) if isinstance(v, (list, tuple)) else None

    @property
    def reasons(self) -> list:
        v = self._outcome.get("reasons")
        if isinstance(v, (list, tuple)):
            return list(v)
        r = self._row.get("reasons")
        return [r] if r else []

    # -- resolution context (for the curve evaluator) -----------------------
    def resolution_context(self) -> dict | None:
        """``{center, gauss_fwhm, lorentz_fwhm, T}`` for :func:`isar.report.audit.reconstruct_curve`.

        Reads the flattened ``results.csv`` columns (``zero``/``gauss_fwhm``/``lorentz_fwhm``/``T``)
        first — exactly what the report's audit path passes today — and falls back to the ledger's
        ``FitOutcome.calib``. ``None`` when neither is available.
        """
        calib = self.calib or {}
        center = _f(self._row.get("zero"))
        if center is None:
            center = _f(calib.get("center"))
        gauss = _f(self._row.get("gauss_fwhm"))
        if gauss is None:
            gauss = _f(calib.get("gauss_fwhm"))
        lorentz = _f(self._row.get("lorentz_fwhm"))
        if lorentz is None:
            lorentz = _f(calib.get("lorentz_fwhm"))
        T = self.T
        if center is None or gauss is None:
            return None
        return {"center": center, "gauss_fwhm": gauss,
                "lorentz_fwhm": lorentz if lorentz is not None else 0.0,
                "T": T if T is not None else 0.0}

    def __repr__(self) -> str:
        return (f"FitView(unit={self.unit_id!r}, scan={self.scan_number}, det={self.detector_id!r}, "
                f"verdict={self.verdict!r}, omega0={self.omega0})")


# ---------------------------------------------------------------------------
# MasterGroupView — one pooled master plus resolved physical members
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MasterGroupView:
    master: FitView
    members: tuple[FitView, ...]
    unresolved_members: tuple[str, ...] = ()
    updated_at: float | None = None
    pending_members: tuple[FitView, ...] = ()
    pending_acquisitions: tuple[str, ...] = ()

    @property
    def stale_count(self) -> int:
        return len(self.pending_members) + len(self.pending_acquisitions)

    @property
    def q(self):
        return self.master.q

    @property
    def T(self):  # noqa: N802
        return self.master.T


# ---------------------------------------------------------------------------
# MasterAssessmentView — authoritative campaign state for one requested q
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MasterAssessmentView:
    """One immutable projection of the latest durable ``model_update.per_q`` row.

    Physical-scan conclusions are intentionally absent: this is the published pooled-master
    revision on which steering acted. ``raw`` retains additive future fields for consumers that
    understand them.
    """

    hkl: tuple[float, float, float]
    q: float | None = None
    state: str = "unmeasured"
    converged: bool = False
    omega0: float | None = None
    halfwidth: float | None = None
    tolerance: float | None = None
    master_revision: str | None = None
    n_members: int = 0
    track_state: str | None = None
    linewidth_state: str | None = None
    pool_consistent: bool | None = None
    consistency_pvalue: float | None = None
    uncertainty_method: str | None = None
    expected_next_information: dict | None = None
    diagnostics: tuple[str, ...] = ()
    reason: str | None = None
    raw: dict = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_dict(cls, value: dict) -> "MasterAssessmentView":
        hkl = tuple(float(item) for item in (value.get("hkl") or (0.0, 0.0, 0.0)))
        if len(hkl) != 3:
            hkl = (0.0, 0.0, 0.0)
        return cls(
            hkl=hkl, q=_f(value.get("q")),
            state=str(value.get("assessment_state") or "unmeasured"),
            converged=bool(value.get("converged", False)),
            omega0=_f(value.get("omega0")),
            halfwidth=_f(value.get("calibrated_halfwidth")
                         if value.get("calibrated_halfwidth") is not None
                         else value.get("master_halfwidth")),
            tolerance=_f(value.get("tolerance")),
            master_revision=(str(value["master_revision"])
                             if value.get("master_revision") is not None else None),
            n_members=int(value.get("master_n_members") or 0),
            track_state=value.get("track_state"),
            linewidth_state=value.get("linewidth_state"),
            pool_consistent=_b(value.get("pool_consistent")),
            consistency_pvalue=_f(value.get("consistency_pvalue")),
            uncertainty_method=value.get("uncertainty_method"),
            expected_next_information=(dict(value.get("expected_next_information"))
                                       if isinstance(value.get("expected_next_information"), dict)
                                       else None),
            diagnostics=tuple(str(item) for item in (value.get("diagnostics") or ())),
            reason=value.get("reason"), raw=dict(value))


@dataclass(frozen=True)
class CertificationStatusView:
    """Latest durable master-certification progress, independent of GUI log delivery."""

    fingerprint: str
    total: int = 0
    completed: int = 0
    reused_workspace: int = 0
    reused_checkpoint: int = 0
    newly_fitted: int = 0
    active: int = 0
    last_hkl: tuple | None = None
    phase: str = "fitting"
    remaining_analysis_seconds: float | None = None
    published: bool = False
    updated_ts: float | None = None
    raw: dict = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_dict(cls, value: dict) -> "CertificationStatusView":
        hkl = value.get("last_hkl")
        return cls(
            fingerprint=str(value.get("fingerprint") or ""),
            total=int(value.get("total") or 0), completed=int(value.get("completed") or 0),
            reused_workspace=int(value.get("reused_workspace") or 0),
            reused_checkpoint=int(value.get("reused_checkpoint") or 0),
            newly_fitted=int(value.get("newly_fitted") or 0), active=int(value.get("active") or 0),
            last_hkl=(tuple(hkl) if isinstance(hkl, (list, tuple)) else None),
            phase=str(value.get("phase") or "fitting"),
            remaining_analysis_seconds=_f(value.get("remaining_analysis_seconds")),
            published=bool(value.get("published", False)),
            updated_ts=_f(value.get("updated_ts")), raw=dict(value))


# ScanView — one ledger row (ScanEntry) with epoch-resolved HKL
# ---------------------------------------------------------------------------
@dataclass
class ScanView:
    scan_id: str
    scan_number: int
    scan_type: str
    status: str
    provenance: dict
    detectors: dict  # detector_id -> FitView
    hkl: list | None  # epoch-resolved when lattice_epochs exist, else acquisition-time hkl (or None)

    @property
    def experiment_id(self) -> str | None:
        parts = str(self.scan_id).split(":")
        return parts[0] if parts and parts[0] else None

    @property
    def T(self) -> float | None:
        """Sample temperature stamped at ingest/fit (``provenance["T"]``); ``None`` if absent."""
        return _f((self.provenance or {}).get("T"))

    @property
    def source_file(self) -> str | None:
        """The data file this scan was parsed from (``provenance["source_file"]``); ``None`` if absent."""
        v = (self.provenance or {}).get("source_file")
        return str(v) if v else None


# ---------------------------------------------------------------------------
# FileView — one ingest-manifest row (session.files)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FileView:
    """One classified data file from the session manifest (read-only)."""
    path: str
    size: int | None
    mtime: float | None
    classification: str | None      # "measurement" | "companion" | "foreign" | "unrecognized"
    n_scans: int | None             # measurement scans that produced ledger entries (== len(scan_ids))
    scan_ids: tuple = ()
    n_context: int | None = None    # in-file context scans (calibration/alignment/environment), not ledgered
    error: str | None = None        # "ExcType: msg" if this measurement file failed to parse (additive)


@dataclass(frozen=True)
class LastIngestView:
    """Read-only evidence for one completed ingest operation.

    Counts are deliberately scoped to that operation, not reconstructed from the whole workspace.
    The immutable tuple form prevents a GUI or notebook consumer from changing machine-owned state
    through this read-only facade.
    """
    completed_at: str | None = None
    logical_scan_count: int | None = None
    analyzer_unit_count: int | None = None
    failed_file_count: int | None = None
    gaps: tuple = ()


# ---------------------------------------------------------------------------
# CardView / GroupView / BranchModelView / CampaignView
# ---------------------------------------------------------------------------
@dataclass
class CardView:
    policy: Any          # isar.schema.card.CardPolicy
    state: Any           # isar.schema.card.CardState

    @property
    def lattice_epochs(self) -> list:
        return list(getattr(self.state, "lattice_epochs", None) or [])

    @property
    def collation(self) -> dict:
        return dict(getattr(self.policy, "collation", None) or {})


@dataclass
class GroupView:
    key: tuple
    key_fields: dict           # {key_name: display_value}
    members: list              # list[FitView]


@dataclass
class BranchModelView:
    dispersion_prior: list = field(default_factory=list)   # PriorPoint dicts
    branch_models: list = field(default_factory=list)
    branch_belief: list = field(default_factory=list)      # per-point entropy/resolvable
    branch_tracks: Any = field(default_factory=dict)       # additive segmented target/rival tracks


@dataclass
class CampaignView:
    objective: dict | None = None
    budget: dict | None = None
    stop: dict | None = None
    raw: dict | None = None    # the full campaign_summary.json, for anything not surfaced above
