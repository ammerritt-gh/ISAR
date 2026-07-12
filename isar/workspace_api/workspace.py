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

import json
from pathlib import Path

from isar.schema.card import CardState, load_policy, load_state
from isar.session.state import SessionState
from isar.session.workspace import (POLICY_NAME, SESSION_NAME, STATE_NAME, REPORT_DIRNAME)

from .evaluate import evaluate_model as _evaluate_model
from .views import (BranchModelView, CampaignView, CardView, FileView, FitView, GroupView, ScanView)

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

    # ---- derived index (scan_number, detector_id) -> FitOutcome dict ------
    def _outcome_index(self) -> dict:
        if "outcome_index" in self._views:
            return self._views["outcome_index"]
        idx: dict = {}
        scan_types: dict = {}
        for entry in self._session().scans.values():
            scan_types[entry.scan_number] = entry.scan_type
            for det_id, oc in (entry.detectors or {}).items():
                idx[(entry.scan_number, det_id)] = oc
        self._views["outcome_index"] = idx
        self._views["scan_types"] = scan_types
        return idx

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
    def scans(self) -> list:
        if "scans" in self._views:
            return self._views["scans"]
        epochs = list(getattr(self._state(), "lattice_epochs", None) or [])
        out = []
        for entry in self._session().scans.values():
            detectors = {det_id: FitView.from_outcome(oc, scan_number=entry.scan_number)
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
        out = []
        for row in self._results_rows():
            scan = row.get("scan")
            det = row.get("detector")
            try:
                scan_i = int(scan) if scan is not None and scan != "" else None
            except (TypeError, ValueError):
                scan_i = None
            outcome = idx.get((scan_i, det)) if scan_i is not None else None
            out.append(FitView.from_row(row, outcome))
        self._views["results"] = out
        return out

    @property
    def groups(self) -> list:
        if "groups" in self._views:
            return self._views["groups"]
        self._views["groups"] = self._build_groups()
        return self._views["groups"]

    @property
    def branch_model(self) -> BranchModelView:
        if "branch_model" not in self._views:
            st = self._state()
            self._views["branch_model"] = BranchModelView(
                dispersion_prior=list(getattr(st, "dispersion_prior", None) or []),
                branch_models=list(getattr(st, "branch_models", None) or []),
                branch_belief=list(getattr(st, "branch_belief", None) or []))
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
        qe = None
        pol = self._policy()
        if pol is not None:
            qe = getattr(pol, "quasielastic_fwhm", None)
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

        from isar.session.unit_data import read_unit_data, unit_data_path

        exp, scan_number, det = self._split_unit_id(scan_id, detector_id)
        if exp is not None and scan_number is not None and det is not None:
            rec = read_unit_data(unit_data_path(self._data_dir, exp, scan_number, det))
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

        The scan number is the first all-digit ``:``-token; everything before it joins as the
        experiment id, the token after it (if any) is the detector, else ``detector_id`` is used.
        A bare number resolves scan_number only (exp/det unknown -> legacy fallback).
        """
        exp, det, scan_number = None, detector_id, None
        if isinstance(scan_id, int):
            return None, scan_id, det
        parts = str(scan_id).split(":")
        idx = next((i for i, p in enumerate(parts) if p.isdigit()), None)
        if idx is not None:
            scan_number = int(parts[idx])
            if idx > 0:
                exp = ":".join(parts[:idx])
            if idx + 1 < len(parts):
                det = parts[idx + 1]
        else:
            scan_number = self._scan_number_of(scan_id)
        return exp, scan_number, det

    def _find_fit(self, scan_id, detector_id) -> FitView | None:
        scan_number = self._scan_number_of(scan_id)
        # Prefer the results.csv-backed FitView (carries the flattened resolution columns).
        for fv in self.results:
            if fv.scan_number == scan_number and fv.detector_id == detector_id:
                return fv
        # Fall back to the ledger detector directly.
        oc = self._outcome_index().get((scan_number, detector_id))
        if oc is not None:
            return FitView.from_outcome(oc, scan_number=scan_number)
        return None

    @staticmethod
    def _scan_number_of(scan_id):
        if isinstance(scan_id, int):
            return scan_id
        s = str(scan_id)
        for part in s.split(":"):
            if part.isdigit():
                return int(part)
        try:
            return int(s)
        except (TypeError, ValueError):
            return None

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
