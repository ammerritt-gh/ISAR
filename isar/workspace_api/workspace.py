"""The read-only workspace facade (P0-1) — the GUI's data layer and the notebook interface.

``Workspace.open(path)`` returns a lazy, cached, **read-only** handle over an ISAR workspace
directory. It aggregates the existing readers (``isar.drive.isar_io.readers``, ``isar.session.state``,
``isar.schema.card``, ``isar.drive.session_log``) and never re-fits, re-collates from raw, or writes.

Robustness contract (a live campaign writes these files while we read):
  * each artifact is parsed at most once until :meth:`refresh` (laziness + caching);
  * a parse error retries once, then serves the last-good cached value if any, else a safe default,
    and appends a string to :attr:`warnings` — it never raises out of a property;
  * unknown/missing fields degrade to ``None``; extra unknown fields are ignored.

Qt-free and, for ``Workspace.open`` + the read surfaces, scipy-free (only the ``evaluate_model``
fallback pulls scipy, lazily).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from isar.predictive import TARGET_PROJECTION_SCHEMA
from isar.schema.card import CardState, load_policy, load_state
from isar.session.state import SessionState
from isar.session.workspace import (POLICY_NAME, SESSION_NAME, STATE_NAME, REPORT_DIRNAME)

from .evaluate import evaluate_model as _evaluate_model
from .scan_keys import split_key
from .views import (BranchModelView, CampaignView, CardView, CertificationStatusView, FileView,
                    FitView, GroupView, LastIngestView, MasterAssessmentView, ScanView)

# The read-surface contract version. Bump when the FitView/ScanView/etc. surface changes in a way
# a consumer must branch on (additive fields do NOT require a bump).
SCHEMA_VERSION = 1

_CAMPAIGN_NAME = "campaign_summary.json"
_SESSION_LOG_NAME = "session_log.jsonl"


class WorkspaceNotFound(Exception):
    """Raised by :meth:`Workspace.open` when the directory is not an ISAR workspace."""


class Workspace:
    """Read-only, lazily-cached handle over one workspace directory."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.warnings: list[str] = []
        self._raw: dict = {}          # file -> parsed data (tolerant); the parse-once cache
        self._last_good: dict = {}    # file -> last successful parse (partial-write fallback)
        self._views: dict = {}        # derived views (built from _raw), also parse-once

    # ---- lifecycle --------------------------------------------------------
    @classmethod
    def open(cls, path) -> "Workspace":
        root = Path(path)
        if not (root / POLICY_NAME).exists():
            raise WorkspaceNotFound(f"not an ISAR workspace (no {POLICY_NAME}): {root}")
        return cls(root)

    def refresh(self) -> None:
        """Drop all caches so the next access re-reads from disk (the watch task uses this).

        Keeps ``_last_good`` so a torn write during the refreshed read can still degrade gracefully.
        """
        self._raw.clear()
        self._views.clear()
        self.warnings.clear()

    @property
    def schema_version(self) -> int:
        return SCHEMA_VERSION

    # ---- paths ------------------------------------------------------------
    @property
    def _report_dir(self) -> Path:
        return self.root / REPORT_DIRNAME

    @property
    def _data_dir(self) -> Path:
        return self.root / "data"

    # ---- tolerant parse-once core -----------------------------------------
    def _parse(self, key: str, loader, default):
        """Parse-once with retry + last-good fallback. ``loader`` is a zero-arg callable that reads
        and parses one artifact; a ``FileNotFoundError`` means "not written yet" (default, no warning);
        any other error retries once then degrades to last-good/default with a :attr:`warnings` entry.
        """
        if key in self._raw:
            return self._raw[key]
        last_exc = None
        for attempt in (1, 2):
            try:
                val = loader()
                self._raw[key] = val
                self._last_good[key] = val
                return val
            except FileNotFoundError:
                self._raw[key] = default
                return default
            except Exception as exc:  # noqa: BLE001 — degrade, never raise from a read surface
                last_exc = exc
        if key in self._last_good:
            self.warnings.append(f"{key}: parse failed ({last_exc}); serving last good value")
            val = self._last_good[key]
        else:
            self.warnings.append(f"{key}: parse failed ({last_exc}); serving default")
            val = default
        self._raw[key] = val
        return val

    # ---- raw artifacts ----------------------------------------------------
    def _policy(self):
        return self._parse("policy", lambda: load_policy(self.root / POLICY_NAME), None)

    def _state(self) -> CardState:
        # load_state returns CardState() for a missing file; a corrupt file raises -> degrades here.
        return self._parse("state", lambda: load_state(self.root / STATE_NAME), CardState())

    def _session(self) -> SessionState:
        return self._parse("session", lambda: SessionState.load(self.root / SESSION_NAME),
                           SessionState())

    def _results_rows(self) -> list:
        from isar.drive.isar_io.readers import read_results_csv
        return self._parse("results", lambda: read_results_csv(self.root), [])

    def _campaign_raw(self):
        def _load():
            p = self._report_dir / _CAMPAIGN_NAME
            if not p.exists():
                raise FileNotFoundError(p)
            return json.loads(p.read_text(encoding="utf-8"))
        return self._parse("campaign", _load, None)

    # ---- derived identity indexes -----------------------------------------
    def _outcome_index(self) -> dict:
        """Exact ``scan_id -> FitOutcome`` index. Never collapse experiment identities."""
        if "outcome_index" in self._views:
            return self._views["outcome_index"]
        idx: dict = {}
        for entry in self._session().scans.values():
            for det_id, oc in (entry.detectors or {}).items():
                idx[entry.scan_id] = oc
        self._views["outcome_index"] = idx
        return idx

    def _outcome_pair_index(self) -> dict:
        """Legacy ``(scan, detector) -> [(unit_id, outcome)]`` candidates.

        Bare results produced before ``scan_id`` was added remain enrichable only when that pair is
        unique. Multiple experiments with the same scan number must stay ambiguous, never select a
        plausible-but-wrong detector outcome.
        """
        if "outcome_pair_index" in self._views:
            return self._views["outcome_pair_index"]
        pairs: dict = {}
        for entry in self._session().scans.values():
            for det_id, oc in (entry.detectors or {}).items():
                pairs.setdefault((entry.scan_number, det_id), []).append((entry.scan_id, oc))
        self._views["outcome_pair_index"] = pairs
        return pairs

    # ---- public surfaces --------------------------------------------------
    @property
    def card(self) -> CardView:
        if "card" not in self._views:
            self._views["card"] = CardView(policy=self._policy(), state=self._state())
        return self._views["card"]

    @property
    def snapshot(self) -> SessionState | None:
        sess = self._session()
        return sess if sess.scans or sess.files else None

    @property
    def last_ingest(self) -> LastIngestView | None:
        """Read-only evidence for the latest completed ingest, or ``None`` for older workspaces.

        Coercion is deliberately local and tolerant: malformed optional summary data must not
        prevent the normal file and scan surfaces from loading.
        """
        if "last_ingest" in self._views:
            return self._views["last_ingest"]
        raw = getattr(self._session(), "last_ingest", None)
        if not isinstance(raw, dict) or not raw:
            self._views["last_ingest"] = None
            return None

        def _count(name):
            value = raw.get(name)
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        gaps = []
        for gap in raw.get("gaps") or []:
            if not isinstance(gap, (list, tuple)) or len(gap) != 3:
                continue
            try:
                gaps.append((str(gap[0]), int(gap[1]), int(gap[2])))
            except (TypeError, ValueError):
                continue
        completed = raw.get("completed_at")
        view = LastIngestView(
            completed_at=str(completed) if completed else None,
            logical_scan_count=_count("logical_scan_count"),
            analyzer_unit_count=_count("analyzer_unit_count"),
            failed_file_count=_count("failed_file_count"),
            gaps=tuple(gaps))
        self._views["last_ingest"] = view
        return view

    @property
    def scans(self) -> list:
        if "scans" in self._views:
            return self._views["scans"]
        epochs = list(getattr(self._state(), "lattice_epochs", None) or [])
        out = []
        for entry in self._session().scans.values():
            detectors = {det_id: FitView.from_outcome(oc, scan_number=entry.scan_number,
                                                      unit_id=entry.scan_id)
                         for det_id, oc in (entry.detectors or {}).items()}
            out.append(ScanView(
                scan_id=entry.scan_id, scan_number=entry.scan_number,
                scan_type=entry.scan_type, status=entry.status,
                provenance=dict(entry.provenance or {}), detectors=detectors,
                hkl=self._resolve_hkl(entry, detectors, epochs)))
        self._views["scans"] = out
        return out

    @property
    def files(self) -> list:
        """The ingest manifest as :class:`FileView` rows (``session.files``), parsed once and cached.

        Degrades per the module contract: a malformed manifest row is skipped (never raises), and a
        session that fails to parse serves the last-good/empty session via :meth:`_parse`.
        """
        if "files" in self._views:
            return self._views["files"]
        out = []
        for path, row in (self._session().files or {}).items():
            row = row or {}
            scan_ids = row.get("scan_ids") or []
            out.append(FileView(
                path=str(path), size=row.get("size"), mtime=row.get("mtime"),
                classification=row.get("format"),
                n_scans=row.get("n_scans"),
                scan_ids=tuple(scan_ids) if isinstance(scan_ids, (list, tuple)) else (),
                n_context=row.get("n_context"),
                error=row.get("error")))
        self._views["files"] = out
        return out

    @property
    def results(self) -> list:
        if "results" in self._views:
            return self._views["results"]
        idx = self._outcome_index()
        pairs = self._outcome_pair_index()
        out = []
        for row in self._results_rows():
            scan = row.get("scan")
            det = row.get("detector")
            try:
                scan_i = int(scan) if scan is not None and scan != "" else None
            except (TypeError, ValueError):
                scan_i = None
            unit_id = row.get("scan_id") or None
            outcome = idx.get(unit_id) if unit_id else None
            if unit_id is not None and outcome is None:
                self.warnings.append(
                    f"results: no ledger outcome for atomic scan_id {unit_id!r}; row left unresolved")
            elif scan_i is not None:
                candidates = pairs.get((scan_i, det), [])
                if len(candidates) == 1:
                    unit_id, outcome = candidates[0]
            view = FitView.from_row(row, outcome)
            if view.unit_id is None and unit_id is not None:
                view.unit_id = unit_id
            out.append(view)
        self._views["results"] = out
        return out

    @property
    def groups(self) -> list:
        if "groups" in self._views:
            return self._views["groups"]
        self._views["groups"] = self._build_groups()
        return self._views["groups"]

    @property
    def master_groups(self) -> list:
        """Pooled masters with member fits resolved from their provenance labels."""
        if "master_groups" in self._views:
            return self._views["master_groups"]
        from .views import MasterGroupView

        physical = [fit for fit in self.results if not fit.pooled]
        by_unit = {fit.unit_id: fit for fit in physical if fit.unit_id}
        by_scan: dict[int, list] = {}
        for fit in physical:
            if fit.scan_number is not None:
                by_scan.setdefault(int(fit.scan_number), []).append(fit)
        events = self._session_events()
        analyzed_jobs = {
            str((event.get("payload") or {}).get("job_id"))
            for event in events if event.get("kind") == "analyze"
            and (event.get("payload") or {}).get("job_id") is not None
        }
        failed_jobs = {
            str((event.get("payload") or {}).get("job_id"))
            for event in events if event.get("kind") == "exception"
            and (event.get("payload") or {}).get("action") == "scan_not_done"
            and (event.get("payload") or {}).get("job_id") is not None
        }
        pending_by_q: dict[tuple[float, float, float], list[str]] = {}
        seen_pending_jobs: set[str] = set()
        for event in events:
            if event.get("kind") != "physical_submission":
                continue
            payload = event.get("payload") or {}
            job_id = payload.get("job_id")
            hkl = payload.get("hkl")
            if job_id is None or not isinstance(hkl, (list, tuple)) or len(hkl) != 3:
                continue
            job_key = str(job_id)
            if job_key in analyzed_jobs or job_key in failed_jobs or job_key in seen_pending_jobs:
                continue
            try:
                q_key = tuple(round(float(value), 8) for value in hkl)
            except (TypeError, ValueError):
                continue
            pending_by_q.setdefault(q_key, []).append(job_key)
            seen_pending_jobs.add(job_key)
        groups = []
        for master in (fit for fit in self.results if fit.pooled):
            resolved, unresolved = [], []
            for raw_label in master.members or ():
                label = str(raw_label)
                match = by_unit.get(label)
                if match is None:
                    try:
                        candidates = by_scan.get(int(label), [])
                    except ValueError:
                        candidates = []
                    match = candidates[0] if len(candidates) == 1 else None
                if match is None:
                    unresolved.append(label)
                elif match not in resolved:
                    resolved.append(match)
            updated_at = None
            if master.spectrum_ref:
                spectrum_path = (self.root / master.spectrum_ref).resolve()
                pooled_root = (self.root / "data" / "pooled").resolve()
                try:
                    spectrum_path.relative_to(pooled_root)
                    updated_at = spectrum_path.stat().st_mtime
                except (ValueError, OSError) as exc:
                    self.warnings.append(
                        f"pooled spectrum metadata unavailable for {master.spectrum_ref!r}: {exc}")
            try:
                master_q_key = tuple(round(float(value), 8) for value in master.q)
            except (TypeError, ValueError):
                master_q_key = ()
            groups.append(MasterGroupView(
                master=master, members=tuple(resolved),
                unresolved_members=tuple(unresolved), updated_at=updated_at,
                pending_members=tuple(
                    fit for fit in physical if fit not in resolved and fit.q == master.q
                    and (fit.T == master.T or fit.T is None or master.T is None)),
                pending_acquisitions=tuple(pending_by_q.get(master_q_key, ()))))
        self._views["master_groups"] = groups
        return groups

    def pooled_spectrum(self, fit_or_ref):
        """Load one validated pooled-spectrum blob; return ``None`` for a legacy master."""
        from types import SimpleNamespace
        import numpy as np

        ref = (fit_or_ref.spectrum_ref if hasattr(fit_or_ref, "spectrum_ref") else fit_or_ref)
        if not ref:
            return None
        candidate = (self.root / str(ref)).resolve()
        pooled_root = (self.root / "data" / "pooled").resolve()
        try:
            candidate.relative_to(pooled_root)
        except ValueError as exc:
            raise ValueError(f"pooled spectrum reference escapes data/pooled: {ref!r}") from exc
        payload = json.loads(candidate.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"pooled spectrum payload must be an object in {ref!r}")
        if payload.get("schema") != "isar.pooled_spectrum/1":
            raise ValueError(f"unsupported pooled spectrum schema in {ref!r}")
        try:
            energy = np.asarray(payload.get("energy") or [], dtype=float)
            counts = np.asarray(payload.get("counts") or [], dtype=float)
            variance = np.asarray(payload.get("variance") or [], dtype=float)
            monitor = np.asarray(payload.get("monitor") or [], dtype=float)
            raw_mask = payload.get("mask") or []
            if not all(isinstance(value, (bool, np.bool_)) for value in raw_mask):
                raise ValueError("mask values must be booleans")
            mask = np.asarray(raw_mask, dtype=bool)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"malformed pooled spectrum arrays in {ref!r}: {exc}") from exc
        arrays = (energy, counts, variance, monitor, mask)
        if any(array.ndim != 1 for array in arrays):
            raise ValueError(f"pooled spectrum arrays must be one-dimensional in {ref!r}")
        if len({array.shape for array in arrays}) != 1:
            raise ValueError(f"pooled spectrum arrays are not aligned in {ref!r}")
        if (not np.all(np.isfinite(energy)) or not np.all(np.isfinite(counts))
                or not np.all(np.isfinite(variance)) or not np.all(np.isfinite(monitor))):
            raise ValueError(f"pooled spectrum arrays contain non-finite values in {ref!r}")
        if np.any(counts < 0.0) or np.any(variance < 0.0) or np.any(monitor < 0.0):
            raise ValueError(f"pooled spectrum counts, variance, and monitor must be nonnegative in {ref!r}")
        return SimpleNamespace(
            x=energy, counts=counts,
            yerr=np.sqrt(np.clip(variance, 0.0, None)), variance=variance,
            monitor=monitor, mask=mask,
            members=tuple(tuple(item) for item in payload.get("members") or ()))

    def _side_artifact(self, ref, subdirectory: str, label: str):
        """Load one JSON side artifact, refusing any reference that escapes its own blob root."""
        candidate = (self.root / str(ref)).resolve()
        root = (self.root / "data" / subdirectory).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"{label} reference escapes data/{subdirectory}: {ref!r}") from exc
        payload = json.loads(candidate.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"{label} payload must be an object in {ref!r}")
        return payload

    def master_predictive(self, fit_or_ref):
        """Load one strictly validated, Qt-free posterior-predictive artifact.

        Validation is the ONE shared gate (:func:`isar.predictive.validate_master_predictive_artifact`,
        ADR-020) — the same checks the arbitrary-grid evaluator and the drive loop apply, so no
        reader can accept a basis another reader would refuse. That includes recomputing the stored
        ``predictive_basis_fingerprint``. /2 only (ADR-006/013): a stale `/1` artifact refuses here
        and never blocks opening the workspace or reading its scans and fits.
        """
        from isar.predictive import PredictiveArtifactError, validate_master_predictive_artifact

        ref = (fit_or_ref.predictive_ref if hasattr(fit_or_ref, "predictive_ref") else fit_or_ref)
        if not ref:
            return None
        payload = self._side_artifact(ref, "master_predictive", "master predictive")
        try:
            validate_master_predictive_artifact(payload)
        except PredictiveArtifactError as exc:
            raise ValueError(f"{exc.message} in {ref!r}") from exc
        return payload

    def target_projection(self, fit_or_ref):
        """Load one ``isar.target_projection/2`` artifact (ADR-019), or ``None`` when unpublished.

        The projection is bound to the authority basis that decided it (master revision, track
        revision, candidate identity/state, calibration and policy fingerprints). A consumer whose
        current state differs on any of those holds a stale projection and must re-resolve it —
        this reader deliberately does not decide that for it.
        """
        ref = (fit_or_ref.target_projection_ref
               if hasattr(fit_or_ref, "target_projection_ref") else fit_or_ref)
        if not ref:
            return None
        payload = self._side_artifact(ref, "target_projection", "target projection")
        if payload.get("schema") != TARGET_PROJECTION_SCHEMA:
            raise ValueError(f"unsupported target projection schema in {ref!r}")
        if not isinstance(payload.get("authority"), dict):
            raise ValueError(f"target projection carries no authority basis in {ref!r}")
        if not isinstance(payload.get("models"), list):
            raise ValueError(f"target projection models must be a list in {ref!r}")
        semantics = payload.get("statistical_semantics")
        if semantics is not None:
            from isar.schema.statistics import (
                SemanticsConstructionError,
                validate_predictive_semantics,
            )

            try:
                validated = validate_predictive_semantics(semantics)
            except (SemanticsConstructionError, TypeError, ValueError, OverflowError) as exc:
                reason = (
                    exc.reason_code
                    if isinstance(exc, SemanticsConstructionError)
                    else type(exc).__name__
                )
                raise ValueError(
                    f"target projection statistical semantics are invalid in {ref!r}: {reason}"
                ) from exc
            for name in (
                "statistical_semantics_fingerprint",
                "source_predictive_semantics_fingerprint",
                "target_projection_set_fingerprint",
            ):
                if payload.get(name) != validated.get(name):
                    raise ValueError(
                        f"target projection {name} does not match its authenticated semantics "
                        f"in {ref!r}"
                    )
        return payload

    @property
    def branch_model(self) -> BranchModelView:
        if "branch_model" not in self._views:
            st = self._state()
            self._views["branch_model"] = BranchModelView(
                dispersion_prior=list(getattr(st, "dispersion_prior", None) or []),
                branch_models=list(getattr(st, "branch_models", None) or []),
                branch_belief=list(getattr(st, "branch_belief", None) or []),
                branch_tracks=getattr(st, "branch_tracks", None) or {})
        return self._views["branch_model"]

    @property
    def campaign(self) -> CampaignView | None:
        if "campaign_view" in self._views:
            return self._views["campaign_view"]
        raw = self._campaign_raw()
        view = None
        if isinstance(raw, dict):
            view = CampaignView(objective=raw.get("objective"), budget=raw.get("budget"),
                                stop=raw.get("stop"), raw=raw)
        self._views["campaign_view"] = view
        return view

    @property
    def scene(self):
        """The stored scene revision (``SceneRevisionView``), or ``None`` if there is none.

        Deliberately NOT routed through ``_parse``: absence is normal and reads as ``None``, but a
        corrupt scene store raises ``ValueError`` out of this property instead of degrading into a
        warning. The two states mean opposite things — "no scene yet" vs "the scene on disk cannot
        be vouched for" — and burying the second in ``ws.warnings`` is how it would go unnoticed.
        """
        if "scene" not in self._views:
            from .scene_view import scene_revision
            self._views["scene"] = scene_revision(self)
        return self._views["scene"]

    @property
    def agenda(self):
        """The stored agenda revision (``AgendaRevisionView``), or ``None`` if there is none.

        Same posture as :attr:`scene`: absence is normal and reads as ``None``; a corrupt agenda
        store raises ``ValueError`` instead of degrading into a warning. Freshness against the
        scene/program/registry state is NOT computed here — call ``ws.agenda.freshness()``, which
        reads those other stores lazily and only on demand.
        """
        if "agenda" not in self._views:
            from .agenda_view import agenda_revision
            self._views["agenda"] = agenda_revision(self)
        return self._views["agenda"]

    @property
    def master_assessments(self) -> list[MasterAssessmentView]:
        """Latest durable pooled-master assessment snapshot, in objective-path order."""
        if "master_assessments" in self._views:
            return self._views["master_assessments"]
        per_q = []
        for record in reversed(self._session_events()):
            if record.get("kind") != "decide":
                continue
            payload = record.get("payload") or {}
            if payload.get("event") == "model_update" or payload.get("action") == "model_update":
                per_q = payload.get("per_q") or []
                break
        views = [MasterAssessmentView.from_dict(value) for value in per_q if isinstance(value, dict)]
        self._views["master_assessments"] = views
        return views

    @property
    def certification_status(self) -> CertificationStatusView | None:
        """Newest durable certification status; checkpoint layout stays behind the facade."""
        if "certification_status" in self._views:
            return self._views["certification_status"]

        def _load():
            root = self._report_dir / "certification"
            candidates = list(root.glob("*/progress.json"))
            if not candidates:
                raise FileNotFoundError(root)
            rows = []
            for path in candidates:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if payload.get("schema") != "isar.certification.progress/1":
                    continue
                rows.append(payload)
            if not rows:
                raise FileNotFoundError(root)
            latest = max(rows, key=lambda value: float(value.get("updated_ts") or 0.0))
            current_path = root / "current.json"
            if current_path.exists():
                current = json.loads(current_path.read_text(encoding="utf-8"))
                expected = current.get("published_hashes") or {}
                paths = {
                    "session.json": self.root / "session.json",
                    "card.state.json": self.root / "card.state.json",
                    "results.csv": self._report_dir / "results.csv",
                    "card.policy.toml": self.root / "card.policy.toml",
                }
                hashes_match = bool(expected) and all(
                    path.is_file()
                    and hashlib.sha256(path.read_bytes()).hexdigest() == expected.get(name)
                    for name, path in paths.items())
                if current.get("fingerprint") == latest.get("fingerprint") and hashes_match:
                    latest = {**latest, "published": True, "phase": "completed"}
            return latest

        raw = self._parse("certification_status", _load, None)
        view = CertificationStatusView.from_dict(raw) if isinstance(raw, dict) else None
        self._views["certification_status"] = view
        return view

    # ---- session events ---------------------------------------------------
    def _session_events(self) -> list:
        """Parse ``report/session_log.jsonl`` once (through the parse-once cache) into a list of good
        records. A truncated/invalid line is dropped with a single :attr:`warnings` entry recorded at
        parse time (not on every read). A missing file degrades to ``[]`` (no warning); any other read
        error propagates so :meth:`_parse` retries once then degrades with a warning like the rest.
        """
        def _load():
            path = self._report_dir / _SESSION_LOG_NAME
            text = path.read_text(encoding="utf-8")  # FileNotFoundError -> [] default via _parse
            records = []
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    self.warnings.append("session_log: dropped a truncated/invalid line")
            return records

        return self._parse("session_events", _load, [])

    def session_events(self, kind=None, since=None):
        """Iterate ``report/session_log.jsonl`` records, filtered by ``kind`` and/or ``since`` (ts).

        The file is parsed at most once (cached until :meth:`refresh`); this only filters the cached
        records in memory, so repeated calls do not re-read the file and the truncated-line warning
        is recorded exactly once. Missing file -> empty iterator.
        """
        for rec in self._session_events():
            if kind is not None and rec.get("kind") != kind:
                continue
            if since is not None:
                ts = rec.get("ts")
                if ts is None or ts < since:
                    continue
            yield rec

    # ---- change notifications ---------------------------------------------
    def watch(self, callback, *, debounce_ms: float = 200,
              poll_interval_s: float = 1.0, backend: str | None = None):
        """Start watching the workspace artifacts and return a running :class:`~.watch.Watcher`.

        ``callback(changed: set[str])`` is invoked (coalesced within ``debounce_ms``) with the
        logical names that changed — a subset of ``{"results", "events", "report", "card_state",
        "snapshot"}``. It runs on a background daemon thread owned by the watcher (never this thread
        or a GUI thread), so a GUI must marshal it onto its own loop. The watcher does not refresh
        this ``Workspace``; call ``ws.refresh()`` inside the callback before re-reading. Supports
        ``with ws.watch(cb) as w:``. See :mod:`isar.workspace_api.watch` for the full thread model.
        """
        from .watch import Watcher
        return Watcher(self, callback, debounce_ms=debounce_ms,
                       poll_interval_s=poll_interval_s, backend=backend).start()

    # ---- curve evaluation -------------------------------------------------
    def evaluate_model(self, scan_id, detector_id, x_eval=None):
        """Evaluate the delivered model curve for one scan x detector.

        ``scan_id`` may be a full ledger scan_id (``"tavi:469:det"``), a ``"tavi:469"`` prefix, or a
        bare scan number; ``detector_id`` selects the detector. Returns
        ``SimpleNamespace(x, total, components, pure)``.
        """
        fit = self._find_fit(scan_id, detector_id)
        if fit is None:
            raise KeyError(f"no fit for scan_id={scan_id!r} detector={detector_id!r}")
        return self.evaluate_fit(fit, x_eval=x_eval)

    def evaluate_fit(self, fit, x_eval=None):
        """Evaluate a supplied :class:`FitView`, including a pooled master.

        Unlike :meth:`evaluate_model`, this interface performs no scan-identity lookup.  When the
        caller omits ``x_eval`` for a pooled fit, its persisted combined spectrum supplies the grid.
        """
        qe = None
        pol = self._policy()
        if pol is not None:
            qe = getattr(pol, "quasielastic_fwhm", None)
        if x_eval is None and getattr(fit, "pooled", False):
            pooled = self.pooled_spectrum(fit)
            if pooled is not None:
                x_eval = pooled.x
        return _evaluate_model(fit, self._data_dir, quasielastic_fwhm=qe, x_eval=x_eval)

    def scan_data(self, scan_id):
        """The measured spectrum for one scan as ``(energy, counts, yerr)`` numpy arrays.

        Re-reads the scan's ``data/*.json`` through the same finite/valid masking the fit itself and
        the ``evaluate_model`` fallback use, so the arrays line up with the delivered curve. ``yerr``
        is the Poisson ``sqrt(counts)`` whisker (clipped at 0). Returns ``(None, None, None)`` when the
        scan has no re-parseable data file (never raises). Read-only; used by the C2 fit inspector to
        draw the data + residuals without hacking at the private evaluator internals.
        """
        import numpy as np

        from .evaluate import _load_scan_arrays
        energy, counts = _load_scan_arrays(self._data_dir, self._scan_number_of(scan_id))
        if energy is None or counts is None:
            return None, None, None
        yerr = np.sqrt(np.clip(counts, 0.0, None))
        return energy, counts, yerr

    def unit_data(self, scan_id, detector_id=None):
        """The measured spectrum for one atomic *scan × detector* unit, or ``None``.

        Reads the per-unit ingest file (``data/ingest/ingest-*.json``, ``isar.unit_data/1``) written
        at ingest, returning ``SimpleNamespace(x, counts, yerr, mask, axis)`` — ``x`` the RAW scanned
        abscissa (never energy-zero-shifted; the ``axis`` block carries the 3-state calibration so a
        caller labels honestly), ``yerr`` the Poisson ``sqrt(clip(variance, 0, None))`` whisker (σ=0
        stays 0, D3). ``scan_id`` may be a full ``exp:scan:det`` ledger id (``detector_id`` then
        redundant) or an ``exp:scan``/bare scan with ``detector_id`` supplied.

        Falls back to the drive loop's legacy single-detector ``data/j-*.json`` (axis defaults to
        absolute — correct for TAVI) when no per-unit file exists. ``None`` when neither is present.
        Read-only; never raises.
        """
        import numpy as np
        from types import SimpleNamespace

        from isar.session.unit_data import read_unit_data_for_unit, unit_data_artifact_exists

        exp, scan_number, det = self._split_unit_id(scan_id, detector_id)
        if exp is not None and scan_number is not None and det is not None:
            # This validates schema, aligned arrays, and payload identity.  A corrupt or legacy
            # collision file is therefore treated as missing rather than plotted as a plausible
            # neighbour; the helper still reads a valid legacy filename for migration continuity.
            rec = read_unit_data_for_unit(self._data_dir, exp, scan_number, det)
            if rec is not None:
                counts = np.asarray(rec.get("counts") or [], dtype=float)
                var = np.asarray(rec.get("variance") or [], dtype=float)
                yerr = np.sqrt(np.clip(var, 0.0, None)) if var.size else np.zeros(counts.shape)
                mask = rec.get("mask")
                mask = (np.asarray(mask, dtype=bool) if mask is not None
                        else np.ones(counts.shape, dtype=bool))
                return SimpleNamespace(
                    x=np.asarray(rec.get("x") or [], dtype=float), counts=counts,
                    yerr=yerr, mask=mask, axis=dict(rec.get("axis") or {}))
            # A present but invalid/mismatched atomic artifact needs repair.  Do not conceal it by
            # falling through to the scan-level drive file: that file has no detector identity and
            # can silently display a neighbouring analyzer's counts.
            if unit_data_artifact_exists(self._data_dir, exp, scan_number, det):
                return None

        # Fallback: legacy single-detector drive file; axis defaults absolute (TAVI is calibrated).
        from .evaluate import _load_scan_arrays
        energy, counts = _load_scan_arrays(self._data_dir, scan_number)
        if energy is None or counts is None:
            return None
        counts = np.asarray(counts, dtype=float)
        return SimpleNamespace(
            x=np.asarray(energy, dtype=float), counts=counts,
            yerr=np.sqrt(np.clip(counts, 0.0, None)),
            mask=np.ones(counts.shape, dtype=bool),
            axis={"name": "energy", "kind": "energy", "units": "meV",
                  "raw_abscissa_kind": "energy", "calibration_status": "absolute"})

    def _split_unit_id(self, scan_id, detector_id=None):
        """``(experiment_id, scan_number, detector_id)`` from a unit id in any accepted form.

        Delegates to the shared, right-anchored Qt-free codec. A supplied ``detector_id`` overrides
        a legacy scan-level key's missing detector, but never changes a full atomic identity.
        """
        exp, scan_number, parsed_det = split_key(scan_id)
        det = parsed_det if parsed_det is not None else detector_id
        return exp, scan_number if isinstance(scan_number, int) else None, det

    def _find_fit(self, scan_id, detector_id) -> FitView | None:
        # Use the raw codec result here (rather than _split_unit_id): that helper fills a missing
        # detector from ``detector_id`` for legacy prefixes, which must not make a prefix look like
        # a caller-supplied full atomic identity.
        exp, scan_number, parsed_det = split_key(scan_id)
        scan_number = scan_number if isinstance(scan_number, int) else None
        det = parsed_det or detector_id
        unit_id = str(scan_id) if exp is not None and scan_number is not None and parsed_det else None
        # Prefer the results.csv-backed FitView (carries flattened resolution columns) by exact
        # identity. This is the normal fitted-inspector path.
        if unit_id is not None:
            for fv in self.results:
                if fv.unit_id == unit_id and fv.detector_id == det:
                    return fv
            oc = self._outcome_index().get(unit_id)
            if oc is not None:
                return FitView.from_outcome(oc, scan_number=scan_number, unit_id=unit_id)
            self.warnings.append(
                f"evaluate_model: no fit for atomic scan_id {unit_id!r} detector {det!r}")
            return None

        # Legacy bare/prefix lookup only succeeds when the pair identifies exactly one unit.
        candidates = self._outcome_pair_index().get((scan_number, det), [])
        if len(candidates) == 1:
            candidate_id, oc = candidates[0]
            for fv in self.results:
                if fv.unit_id == candidate_id:
                    return fv
            return FitView.from_outcome(oc, scan_number=scan_number, unit_id=candidate_id)
        # A pre-scan_id CSV with no ledger is still usable only when it has one matching row. This
        # preserves old drive workspaces without reviving the unsafe first-match behavior.
        legacy = [fv for fv in self.results
                  if fv.unit_id is None and fv.scan_number == scan_number and fv.detector_id == det]
        if len(legacy) == 1:
            return legacy[0]
        return None

    @staticmethod
    def _scan_number_of(scan_id):
        _exp, scan_number, _det = split_key(scan_id)
        return scan_number if isinstance(scan_number, int) else None

    # ---- HKL resolution ---------------------------------------------------
    def _resolve_hkl(self, entry, detectors, epochs):
        """Epoch-resolved HKL when ``lattice_epochs`` and an instrument-Q are present; else the
        acquisition-time HKL already on the record (``FitOutcome.q``). Never raises -> ``None`` if
        unresolvable. Until absolute coordinates land (Phase B), the epoch path only fires when a
        record carries ``provenance['q_instrument']``; without it this is identity through the
        acquisition epoch, i.e. the stored HKL.
        """
        acq = self._acquisition_hkl(detectors, entry.provenance)
        if not epochs:
            return acq
        qxyz = (entry.provenance or {}).get("q_instrument")
        if not qxyz or len(qxyz) != 3:
            return acq
        try:
            from isar.geometry import q_to_hkl, resolve_epoch, sample_state_from_epoch
            epoch = resolve_epoch(epochs, scan_ref=str(entry.scan_number))
            if epoch is None:
                return acq
            sample = sample_state_from_epoch(epoch)
            out = q_to_hkl(sample, float(qxyz[0]), float(qxyz[1]), float(qxyz[2]))
            hkl = out.get("hkl_rlu")
            return list(hkl) if hkl is not None else acq
        except Exception:  # noqa: BLE001 — geometry must never crash the scan surface
            return acq

    @staticmethod
    def _acquisition_hkl(detectors, provenance=None):
        """Acquisition-time HKL: a detector FitView's Q when a fit carries one, else the ledger
        entry's ``provenance["q"]`` (the ingest-stamped Q — the only source for an ingested-only,
        not-yet-fitted scan). ``None`` when neither is present."""
        for fv in detectors.values():
            if fv.q is not None:
                return list(fv.q)
        q = (provenance or {}).get("q")
        if isinstance(q, (list, tuple)) and q and not any(v is None for v in q):
            return list(q)
        return None

    # ---- groups (reuse the canonical collation key) -----------------------
    def _build_groups(self) -> list:
        """Group the measurement results by the card's collation key.

        Reuses the canonical key derivation from ``isar.reduce.collate`` (``_group_component`` over
        ``_q_key``/``_t_key`` with the same ``key``/``t_round``/``hkl_round_step`` the reducer reads)
        so "a dataset" here means exactly what it means in the reduction/report — no second
        definition. Only non-pooled results with a Q are grouped (the reducer groups measurement
        spectra); a pooled master row is a fit *of* a group, not a member.
        """
        from types import SimpleNamespace
        from isar.reduce.collate import _group_component, _q_key, _t_key

        pol = self._policy()
        collation = dict(getattr(pol, "collation", None) or {}) if pol is not None else {}
        key_names = collation.get("key", ["sample", "q", "T"])
        t_round = collation.get("t_round", 0.1)
        step = (getattr(pol, "hkl_round_step", None) or 0.05) if pol is not None else 0.05

        groups: dict = {}
        for fv in self.results:
            if fv.pooled:
                continue
            q = fv.q
            if q is None or any(v is None for v in q):
                continue
            t = fv.T if fv.T is not None else float("nan")
            ref = SimpleNamespace(
                experiment_id=fv.sample, q=list(q), q_key=_q_key(q, step),
                temperature=t, t_key=_t_key(t, t_round))
            resolved = [_group_component(n, ref) for n in key_names]
            gkey = tuple(k for k, _disp in resolved)
            if gkey not in groups:
                fields = {n: disp for n, (_k, disp) in zip(key_names, resolved)}
                groups[gkey] = GroupView(key=gkey, key_fields=fields, members=[])
            groups[gkey].members.append(fv)
        return list(groups.values())
