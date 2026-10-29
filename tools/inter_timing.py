"""Time one inter-spectra batch headless (S1U13): the batch runner on a workspace, no service, no GUI.

    python tools/inter_timing.py <workspace> --kind simultaneous [--workers 4] [--out <folder>]
    python tools/inter_timing.py <workspace> --kind anchored --direction T --anchors <key> [<key> ...]

The batch is the one the GUI would start: its members come from the facade's batch preview and its id from
``records.new_batch_id()``. ``runner.run_batch`` runs in this process with the service's emit and ack played
here, each payload saved through ``writer.save_inter``; the batch records are the only thing written to the
workspace. ``--out`` gets a copy of the report (``report.txt``, ``timing.json``).

The report prints the batch's own ``timing`` block, the stage-1 decisions (read by a probe on the core, so the
quiet round that ends a batch counts), and a projection for 56 spectra on 4 and 16 workers, an estimate. The
30-minute target is reported, never enforced.

``--synthetic-grid-q`` places each spectrum on the BL43 grid (q = (col, row, 0) x 0.1 rlu) to time the curvature
path only; the report and the batch's metric say so. ``--core module[:attr]`` injects a core (the tests' fake).
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections import Counter
from pathlib import Path

if __package__ in (None, ""):                      # run as a script from anywhere in the repo
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from isar.session import writer  # noqa: E402
from isar.session.inter_spectra import q_provider, records, runner  # noqa: E402
from isar.session.inter_spectra.layout import BL43_GRID  # noqa: E402
from isar.session.inter_spectra.nodes import read_nodes  # noqa: E402
from isar.workspace_api.inter_spectra import BatchRequest, batch_preview  # noqa: E402

SYNTHETIC = "synthetic q coordinates: timing only"
PROJECT_SPECTRA, PROJECT_WORKERS = 56, (4, 16)
BAD_STATUSES = ("no_evidence", "degenerate_twin")


class _Probe:
    """The runner's core, delegated, noting what stage 1 decides: the tool's only view of a quiet final round,
    which saves no payload."""

    def __init__(self, core) -> None:
        self._core, self.__name__ = core, core.__name__        # the pool imports the core by this name
        self.stage1: dict[str, dict] = {}                      # stage id -> counts
        self._cached: dict[str, set] = {}                      # unit -> keys offered as cached prices

    def __getattr__(self, name):
        return getattr(self._core, name)

    def assign_jobs(self, snapshot, *, cached=None):
        a, jobs = self._core.assign_jobs(snapshot, cached=cached or {})
        s = self.stage1.setdefault(snapshot.stage_id, {"snapshots": 0, "triggered": 0, "jobs": 0, "fresh": 0,
                                                       "bad": 0, "hits": 0})
        s["snapshots"] += 1
        s["triggered"] += int(a is None)
        s["jobs"] += len(jobs)
        self._cached[snapshot.unit] = set(cached or {})
        return a, jobs

    def assign_decide(self, snapshot, priced):
        cached = self._cached.pop(snapshot.unit, set())
        s = self.stage1[snapshot.stage_id]
        fresh = [p for p in priced if p.key not in cached]
        s["fresh"] += len(fresh)
        s["bad"] += sum(p.status in BAD_STATUSES for p in fresh)
        s["hits"] += len(priced) - len(fresh)
        return self._core.assign_decide(snapshot, priced)


class _Service:
    """The service's side in this process: each payload is saved at once and its verdict handed back."""

    def __init__(self, root: Path, note: str | None) -> None:
        self.root, self.note, self.results, self.verdicts = root, note, [], {}

    def emit(self, r) -> None:
        self.results.append(r)
        if r.payload is None or r.ack is None:
            return
        if self.note and r.payload["event"] == "start":
            r.payload["start"]["metric"]["note"] = self.note
        verdict = writer.save_inter(self.root, [r.payload])[0]
        self.verdicts[r.ack] = verdict if isinstance(verdict, dict) else {"saved": False, "error": verdict}

    def ack(self, token: int) -> dict:
        return self.verdicts.pop(token)


def _progress(p: dict) -> None:
    print(f"  {p['stage']} {p['index']} {p['phase']}: {p['done']} done of {p['total']}", file=sys.stderr, flush=True)


def _synthetic_q(root, keys) -> q_provider.QCoordinates:
    """BL43-grid coordinates, timing only (the same signature as ``q_provider.q_coordinates``)."""
    nodes = read_nodes(root).nodes
    points, reasons = {}, {}
    for k in keys:
        pos = BL43_GRID.position(nodes[k].detector) if k in nodes else None
        if pos is None:
            reasons[k] = "unknown spectrum" if k not in nodes else "no accepted hkl"
            continue
        row, col = pos
        points[k] = q_provider.QPoint((col * 0.1, row * 0.1, 0.0), "synthetic", None, None)
    return q_provider.QCoordinates(points=points, reasons=reasons, lattices={})


def _core(text: str):
    name, _, attr = text.partition(":")
    mod = importlib.import_module(name)
    return getattr(mod, attr) if attr else mod


def _report(spec, n_members: int, probe: _Probe, results: list, end: dict, workers: int, note):
    """The report lines and the JSON copy of them."""
    t, s1 = end["timing"], probe.stage1
    sweeps = Counter(r.payload["stage"]["round"] for r in results
                     if r.payload and r.payload["event"] == "stage" and r.payload["stage"]["stage"] == "sweep")
    jobs = sum(v["jobs"] for v in s1.values())
    fresh = sum(v["fresh"] for v in s1.values())
    bad = sum(v["bad"] for v in s1.values())
    triggered = sum(v["triggered"] for v in s1.values())
    hits = sum(v["hits"] for v in s1.values())
    refits = sum(s["n_refits"] for s in t["stages"])
    per_job = t["likelihood_s"] / jobs if jobs else 0.0
    per_refit = (t["continuous_s"] + t["uncertainty_s"] + t["release_s"]) / refits if refits else 0.0
    per_member = (jobs / n_members) * per_job + (refits / n_members) * per_refit
    proj = {w: PROJECT_SPECTRA * per_member / w for w in PROJECT_WORKERS}

    lines = [f"batch {spec.batch_id}  kind {spec.kind}  members {n_members}  workers {workers}"]
    if note:
        lines.append(note)
    lines += [f"batch_wall_s: {t['wall_s']:.3f}", f"target_s: {t['target_s']:.0f}",
              f"over_target: {t['over_target']}",
              f"end: {end['state']}" + (f" ({end['reason']})" if end.get("reason") else ""),
              f"rounds: {end['rounds'] if end['rounds'] is not None else '-'}",
              f"sweeps: {end['sweeps'] if end['sweeps'] is not None else '-'}",
              f"rings: {end['rings_done'] if end['rings_done'] is not None else '-'}"]
    for s in t["stages"]:
        n_jobs = s1.get(f"{spec.batch_id}:{s['kind']}{s['index']}", {}).get("jobs", 0)
        lines.append(f"stage {s['kind']}{s['index']}: wall_s={s['wall_s']:.3f} width={s['width']} "
                     f"n_refits={s['n_refits']} n_jobs={n_jobs}")
    lines += [f"{name}: {t[name]:.3f}" for name in ("prepare_s", "ship_s", "assign_s", "likelihood_s",
                                                    "continuous_s", "uncertainty_s", "release_s", "save_s")]
    for sid, v in s1.items():
        label = sid.split(":", 1)[1]
        lines.append(f"triggered_fraction {label}: {v['triggered'] / v['snapshots']:.2f} "
                     f"({v['triggered']} of {v['snapshots']})")
    for sid in s1:
        label = sid.split(":", 1)[1]
        if label.startswith("round"):
            lines.append(f"sweeps_to_settle {label}: {sweeps.get(int(label[5:]), 0)}")
    lines.append(f"price_jobs_per_triggered_spectrum: {jobs / triggered:.2f}" if triggered else
                 "price_jobs_per_triggered_spectrum: -")
    lines.append(f"cache_hits: {hits}")
    lines.append(f"no_evidence_or_twin_fraction: {bad / fresh:.2f} ({bad} of {fresh} new prices)" if fresh else
                 "no_evidence_or_twin_fraction: -")
    lines.append(f"means: per_price_job_s={per_job:.4f} per_refit_s={per_refit:.4f}")
    for w in PROJECT_WORKERS:
        lines.append(f"projection_{PROJECT_SPECTRA}_spectra_{w}_workers_s: {proj[w]:.1f} "
                     "(estimate: this run's per-spectrum prices and refits at its means, perfectly packed)")
    data = {"batch_id": spec.batch_id, "kind": spec.kind, "members": n_members, "workers": workers, "note": note,
            "end": end, "stage1": s1, "sweeps_by_round": {str(k): v for k, v in sweeps.items()},
            "projection_s": {str(w): proj[w] for w in PROJECT_WORKERS}}
    return lines, data


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Time one inter-spectra batch headless and print its timing report.")
    p.add_argument("workspace", help="a workspace folder with its fast solo fits (the batch records are written)")
    p.add_argument("--kind", required=True, choices=("anchored", "simultaneous"))
    p.add_argument("--direction", choices=("q", "T", "qT"), help="anchored batches only")
    p.add_argument("--anchors", nargs="+", default=[], metavar="KEY", help="anchored batches only: spectrum keys")
    p.add_argument("--workers", type=int, default=4, help="refit pool width (the item's cores)")
    p.add_argument("--out", help="folder for a copy of the report (report.txt, timing.json)")
    p.add_argument("--synthetic-grid-q", action="store_true", help="BL43-grid q coordinates, timing only")
    p.add_argument("--core", default=runner.CORE, help=argparse.SUPPRESS)     # module[:attr]: the tests' fake core
    return p


def main(argv=None) -> int:
    p = _parser()
    args = p.parse_args(argv)
    if args.kind == "simultaneous" and (args.direction or args.anchors):
        p.error("--direction and --anchors apply to anchored batches only")
    root = Path(args.workspace).resolve()
    if args.kind == "anchored":
        req = BatchRequest("anchored", args.direction, tuple(args.anchors), ())
    else:
        req = BatchRequest("simultaneous", None, (), tuple(sorted(read_nodes(root).nodes)))
    pv = batch_preview(root, req)
    if not pv.can_start:
        print(f"cannot start: {pv.reason}", file=sys.stderr)
        return 2
    spec = runner.BatchSpec.from_json({"batch_id": records.new_batch_id(), "kind": args.kind,
                                       "direction": req.direction, "anchors": list(req.anchors),
                                       "members": list(pv.members)})
    note = SYNTHETIC if args.synthetic_grid_q else None
    probe, service = _Probe(_core(args.core)), _Service(root, note)
    saved = q_provider.q_coordinates
    if args.synthetic_grid_q:
        q_provider.q_coordinates = _synthetic_q               # the runner's own call; the facade is not timed
    try:
        item = runner.BatchItem("inter-timing", f"inter:{spec.batch_id}:batch", "inter", args.workers, spec.to_json())
        print(pv.line, file=sys.stderr, flush=True)
        runner.run_batch(root, item, progress=_progress, emit=service.emit, ack=service.ack, core=probe)
    finally:
        q_provider.q_coordinates = saved
    ends = [r.payload["end"] for r in service.results if r.payload and r.payload["event"] == "end"]
    if not ends:
        reasons = [r.reason for r in service.results if r.status == "failed"]
        print("no batch end: " + ("; ".join(map(str, reasons)) or "the batch did not run"), file=sys.stderr)
        return 1
    lines, data = _report(spec, len(pv.members), probe, service.results, ends[-1], max(1, args.workers), note)
    print("\n".join(lines))
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        (out / "timing.json").write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8",
                                         newline="\n")
    return 1 if ends[-1]["state"] == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
