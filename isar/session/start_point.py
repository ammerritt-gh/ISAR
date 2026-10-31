"""A BL43 run's per-scan start point: the moment the scan's first unit is handed to a worker.

There the run reads the saved entries from disk (``intake/entries.jsonl``), builds the scan's effective
record (``isar.intake.inputs.apply``) and freezes it for the scan's calibration and every analyzer's fit:
a save committed before this moment is used, one committed after leaves the running calculation
unchanged. The calibration is reused only on an equal inputs key, else recomputed here against the run's
curve snapshot; carry or fit is decided per unit against the frozen inputs. Each taken unit gets its
started lines here and its finished lines in :meth:`StartPoint.finish` (``report/fit_inputs.jsonl``).

Nothing here changes ``card.state`` or the run's calibration view: a pooled task is pickled after
``submit`` returns, so shared state changed by a later scan's start would reach it. Recomputed states
wait in ``recalibrated`` for the driver to merge after the fit stage.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from isar.calibrate import tabulated
from isar.intake import fit_inputs, inputs, store
from isar.schema import _json
from isar.session import calibrations, guidelines as guide_mod, scan_delivery, unit_data
from isar.session.state import canonical_hash

_FAST = ("verdict", "disposition", "omega0", "omega0_err", "best_label")
_DEEP = ("state", "reason", "best_count", "p_best")


def _declared(what: str, c: inputs.Condition) -> str:
    return f"{what} (declared, entry {c.entry}" + (f": {c.reason})" if c.reason else ")")


def _plain(obj):
    """Strict JSON for the log: plain types, non-finite floats as None."""
    return inputs._finite(_json.to_jsonable(obj))


@dataclass(frozen=True)
class UnitPlan:
    """One unit as its scan's start point sees it (:meth:`StartPoint.plan`), before carry or fit is decided.
    ``task`` is the fit task tuple on the frozen effective record; ``condition`` the excluded entry of a unit
    the user declared out, else None; ``started`` its started lines ([] for a condition entry)."""
    unit: str
    det: str
    task: tuple
    condition: object
    chash: str
    ghash: str
    calfp: str | None
    inputs_hash: str
    started: list


class StartPoint:
    """``start_scan`` and ``on_finish`` of the fit stage (:func:`scan_delivery._run_tasks`) for a BL43 run.

    ``parsed``, ``rows`` and ``data_revision`` are keyed by scan (``"exp:scan"``): the parsed records, their
    import rows and their ``content_hash``. ``is_fresh(prev, det, *, chash, ghash, calfp)`` is today's
    freshness test; ``write_stub(task)`` writes a unit's pre-dispatch measurement stub; ``loaded`` is the
    run's curve snapshot; ``checkpoint`` the deadline-resume store, or None; ``gpi`` (scan key -> its entry of the
    gpi check the caller just ran) what each scan's inputs take from its gpi file."""

    def __init__(self, root, card, g, runcal, *, parsed: dict, rows: dict, data_revision: dict,
                 passes, run_id: str, session, is_fresh, write_stub, loaded, checkpoint=None, gpi=None):
        self.root = Path(root)
        self.card, self.g, self.runcal = card, g, runcal
        self.parsed, self.rows, self.data_revision = parsed, rows, data_revision
        self.passes, self.run_id, self.session = tuple(passes), run_id, session
        self.is_fresh, self.write_stub, self.loaded, self.checkpoint = is_fresh, write_stub, loaded, checkpoint
        self.gpi = dict(gpi or {})
        self.carried: set[str] = set()                # unit ids the start point carried
        self.recalibrated: dict[str, dict] = {}       # scan key -> calibration state computed at a start point
        self.frozen: dict = {}                        # scan key -> the effective record the scan started with
        self.views: dict = {}                         # scan key -> its frozen calibration view (None: the run's)
        self._units: dict[str, tuple] = {}            # unit id -> (inputs hash, fallbacks, is a condition entry)

    def _calibration(self, key: str, eff, si: inputs.ScanInputs):
        """The scan's calibration state and frozen view; recomputed when its inputs key changed."""
        if (self.card.policy.calibration or {}).get("method") != "tabulated_elastic":
            return None, None
        state = self.runcal.tabulated.get(key)
        if si.excluded is None:              # an excluded scan takes part in no use
            chash = self.data_revision[key]
            k = calibrations.tabulated_key(self.card, eff, data_hash=chash, base_dir=self.root, loaded=self.loaded)
            fp = None if self.loaded is None else self.loaded.fingerprint
            if not tabulated.calibration_reusable(state, content_hash=chash, library=fp, inputs_key=k):
                state = calibrations.calibrate_scan_state(self.card, eff, data_hash=chash, base_dir=self.root,
                                                          loaded=self.loaded)
                self.recalibrated[key] = state
        return state, calibrations.scan_view(self.runcal, key, state, calibrations.curves_for(eff, self.loaded))

    def _reused(self, scan_id, chash, ghash, h):
        """A deadline-resume checkpoint of this unit made with these very inputs, else None."""
        if self.checkpoint is None:
            return None
        entry = self.checkpoint.load(f"scan:{scan_id}", content_hash=chash, guidelines_hash=ghash)
        if entry is not None and ((entry.provenance or {}).get("fit_inputs") or {}).get("hash") == h:
            return entry
        return None

    def plan(self, group: list) -> list[UnitPlan]:
        """The scan's freeze and each unit's hashes, condition entry and started lines; writes nothing.
        Fills ``frozen``, ``views``, ``recalibrated`` and the units :meth:`finished_lines` stamps."""
        key = group[0][5].rsplit(":", 1)[0]
        read = store.read_all(self.root, store.ENTRIES)            # the freeze: the saves on disk now
        si = inputs.scan_inputs(self.rows[key], inputs.index_entries(read.entries, lines=read.lines),
                                self.gpi.get(key), data_dir=self.root / "data")
        eff = inputs.apply(self.parsed[key], si)
        state, view = self._calibration(key, eff, si)
        chash = self.data_revision[key]
        T = unit_data.scan_temperature(eff)
        out = []
        for _rec, det, _q, _T, sample, scan_id, _chash, _ghash, seed in group:
            q = inputs.analyzer_q(self.rows[key], si, det)[0]      # an accepted gpi HKL, else the file's Q
            ghash = canonical_hash(guide_mod.guidelines_signature(self.g, scan_number=eff.scan_number, q=q, T=T))
            unit = inputs.unit_inputs(self.rows[key], si, det, state)
            h = inputs.inputs_hash(unit)
            reason = (_declared("excluded scan", si.excluded) if si.excluded is not None
                      else _declared("dead analyzer", si.dead[det]) if det in si.dead else None)
            calfp = None if reason else calibrations.calibration_fingerprint(view or self.runcal, eff, det)
            fallbacks = inputs.fallbacks(unit)
            self._units[scan_id] = (h, fallbacks, reason is not None)
            condition = (None if reason is None else scan_delivery.condition_entry(
                eff, det, q, T, sample, scan_id, self.card, chash, ghash, reason))
            started = ([] if reason is not None else
                       [{"unit": scan_id, "scan": key, "pass": p, "inputs": unit, "hash": h,
                         "fallbacks": list(fallbacks)} for p in self.passes])
            out.append(UnitPlan(scan_id, det, (eff, det, q, T, sample, scan_id, chash, ghash, seed), condition,
                                chash, ghash, calfp, h, started))
        self.frozen[key] = eff
        self.views[key] = view
        return out

    def __call__(self, group: list) -> scan_delivery.ScanStart:
        units = self.plan(group)
        key = group[0][5].rsplit(":", 1)[0]
        items, started, stubs = [], [], []
        for u in units:
            prev = self.session.get(u.unit)
            if not (self.is_fresh(prev, u.det, chash=u.chash, ghash=u.ghash, calfp=u.calfp)
                    or ((prev.provenance or {}).get("fit_inputs") or {}).get("hash") != u.inputs_hash):
                self.carried.add(u.unit)
                items.append(None)
                continue
            if u.condition is not None:
                items.append(u.condition)
                continue
            started += u.started
            hit = self._reused(u.unit, u.chash, u.ghash, u.inputs_hash)
            if hit is not None:
                items.append(scan_delivery.Reused(hit))
                continue
            items.append(u.task)
            stubs.append(u.task)
        fit_inputs.write_started(self.root, self.run_id, started)
        for task in stubs:
            self.write_stub(task)
        return scan_delivery.ScanStart(items, self.views[key])

    def finish(self, entry) -> None:
        """Stamp what the unit's result was made with; a fit's finished lines, one per pass of the run."""
        lines = self.finished_lines(entry)
        if lines:
            fit_inputs.write_finished(self.root, self.run_id, lines)

    def finished_lines(self, entry) -> list[dict]:
        """:meth:`finish` without the write: stamps ``entry.provenance["fit_inputs"]`` and returns the lines."""
        info = self._units.get(entry.scan_id)
        if info is None:
            return []
        h, fallbacks, condition = info
        kept = list(fallbacks) if entry.status in ("fitted", "non_result") and not condition else []
        entry.provenance = {**(entry.provenance or {}),
                            "fit_inputs": {"run": self.run_id, "hash": h, "fallbacks": kept}}
        if condition:                         # a declaration, not a fit: no line
            return []
        scan, _, det = entry.scan_id.rpartition(":")
        out = (entry.detectors or {}).get(det)
        lines = []
        for p in self.passes:
            if p == "fast":
                result = None if not out else {k: out.get(k) for k in _FAST}
            else:
                dp = (out or {}).get("deep_pass")
                result = {k: dp.get(k) for k in _DEEP} if isinstance(dp, dict) else None
            lines.append({"unit": entry.scan_id, "scan": scan, "pass": p, "status": entry.status,
                          "fallbacks": kept, "result": _plain(result)})
        return lines
