"""Time one inter-spectra batch headless (S1U13): the batch runner on a workspace, no service, no GUI.

    python tools/inter_timing.py <workspace> --kind simultaneous [--workers 4] [--out <folder>]
    python tools/inter_timing.py <workspace> --kind anchored --direction T --anchors <key> [<key> ...]

The batch is the one the GUI would start: its members come from the facade's batch preview and its id from
``records.new_batch_id()``. ``runner.run_batch`` runs in this process with the service's emit and ack played
here, each payload saved through ``writer.save_inter``; the batch records are the only thing written to the
workspace. ``--out`` gets a copy of the report (``report.txt``, ``timing.json``).

The report is read from the batch's own ``timing`` block (C-timing-report): every stage-1 search's record per
spectrum and ring or round, and the refit pool's use. The 30-minute target is reported, never enforced.

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
TOP_REASONS = 3


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


def _sum(recs: list, key: str):
    return sum(r[key] for r in recs)


def _ratio(a: float, b: float) -> float | None:
    return a / b if b else None


def _fmt(x, spec: str) -> str:
    return "-" if x is None else format(x, spec)


def _report(spec, n_members: int, results: list, end: dict, workers: int, note):
    """The report lines and the JSON copy of them, both read from the batch's end payload (C-timing-report)."""
    t, recs, pool = end["timing"], end["timing"]["spectra"], end["timing"]["pool"]
    sweeps = Counter(r.payload["stage"]["round"] for r in results
                     if r.payload and r.payload["event"] == "stage" and r.payload["stage"]["stage"] == "sweep")
    labels = list(dict.fromkeys(r["stage"] for r in recs))             # stage-1 stages, in the order they ran
    stage1 = {}
    for label in labels:
        mine = [r for r in recs if r["stage"] == label]
        stage1[label] = {"spectra": len(mine),
                         "searched": sum(1 for r in mine if r["candidates"] + r["cached"] > 0),
                         "candidates": _sum(mine, "candidates"), "cached": _sum(mine, "cached")}
    candidates, starts = _sum(recs, "candidates"), _sum(recs, "starts")
    evidence, refine = _sum(recs, "evidence_s"), _sum(recs, "refine_s")
    reasons = Counter()
    for r in recs:
        for why, n in r["failure_reasons"]:
            reasons[why] += n

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
        line = f"stage {s['kind']}{s['index']}: wall_s={s['wall_s']:.3f} width={s['width']} n_refits={s['n_refits']}"
        if s["kind"] != "sweep":                                        # a sweep's refits carry no search of their own
            mine = [r for r in recs if r["stage"] == f"{s['kind']}{s['index']}"]
            line += (f" candidates={_sum(mine, 'candidates')} cached={_sum(mine, 'cached')} fits={_sum(mine, 'fits')}"
                     f" failures={_sum(mine, 'failures')} evidence_s={_sum(mine, 'evidence_s'):.3f}"
                     f" refine_s={_sum(mine, 'refine_s'):.3f}")
        lines.append(line)
    lines += [f"{name}: {t[name]:.3f}" for name in ("prepare_s", "ship_s", "assign_s", "likelihood_s",
                                                    "continuous_s", "uncertainty_s", "release_s", "save_s", "search_s")]
    lines += [f"candidates_priced: {candidates}", f"cache_hits: {_sum(recs, 'cached')}",
              f"starts_per_candidate: {_fmt(_ratio(starts, candidates), '.2f')}",
              f"optimizer_fits: {_sum(recs, 'fits')}", f"failures: {_sum(recs, 'failures')}",
              "failure_reasons: " + ("; ".join(f"{why} x {n}" for why, n in reasons.most_common(TOP_REASONS)) or "-"),
              f"evidence_s: {evidence:.3f}", f"refine_s: {refine:.3f}",
              f"evidence_share: {_fmt(_ratio(evidence, evidence + refine), '.2f')}"]
    for label, v in stage1.items():
        lines.append(f"searched_fraction {label}: {v['searched'] / v['spectra']:.2f} "
                     f"({v['searched']} of {v['spectra']})")
    for label in labels:
        if label.startswith("round"):
            lines.append(f"sweeps_to_settle {label}: {sweeps.get(int(label[5:]), 0)}")
    lines.append(f"pool_utilisation: {_fmt(_ratio(pool['busy_s'], pool['map_wall_s'] * pool['workers']), '.2f')}")
    sp, st = t["slowest"]["spectrum"], t["slowest"]["stage"]
    lines.append("slowest_spectrum: " + ("-" if sp is None else f"{sp['stage']} {sp['unit']} {sp['seconds']:.3f}s"))
    lines.append("slowest_stage: " + ("-" if st is None else f"{st['kind']}{st['index']} wall_s={st['wall_s']:.3f}"))
    for r in sorted(recs, key=lambda r: r["evidence_s"] + r["refine_s"], reverse=True):
        lines.append(f"spectrum {r['stage']} {r['unit']}: candidates={r['candidates']} cached={r['cached']} "
                     f"starts_per_candidate={_fmt(r['starts_per_candidate'], '.2f')} fits={r['fits']} "
                     f"failures={r['failures']} failure_reasons={json.dumps(r['failure_reasons'])} "
                     f"evidence_s={r['evidence_s']:.3f} refine_s={r['refine_s']:.3f} "
                     f"branches_seen={r['branches_seen']} dormant_seen={r['dormant_seen']} "
                     f"error={json.dumps(r['error'])} stopped={json.dumps(r['stopped'])}")
    members = {r["unit"] for r in recs}
    per_member = (evidence + refine) / len(members) if members else 0.0
    lines.append(f"means: per_member_compute_s={per_member:.3f}")
    for w in PROJECT_WORKERS:
        lines.append(f"projection_{PROJECT_SPECTRA}_spectra_{w}_workers_s: {PROJECT_SPECTRA * per_member / w:.1f} "
                     "(estimate: this run's compute per member at its means, perfectly packed)")
    data = {"batch_id": spec.batch_id, "kind": spec.kind, "members": n_members, "workers": workers, "note": note,
            "end": end, "stage1": stage1, "sweeps_by_round": {str(k): v for k, v in sweeps.items()}}
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
    core, service = _core(args.core), _Service(root, note)
    saved = q_provider.q_coordinates
    if args.synthetic_grid_q:
        q_provider.q_coordinates = _synthetic_q               # the runner's own call; the facade is not timed
    try:
        item = runner.BatchItem("inter-timing", f"inter:{spec.batch_id}:batch", "inter", args.workers, spec.to_json())
        print(pv.line, file=sys.stderr, flush=True)
        runner.run_batch(root, item, progress=_progress, emit=service.emit, ack=service.ack, core=core)
    finally:
        q_provider.q_coordinates = saved
    ends = [r.payload["end"] for r in service.results if r.payload and r.payload["event"] == "end"]
    if not ends:
        reasons = [r.reason for r in service.results if r.status == "failed"]
        print("no batch end: " + ("; ".join(map(str, reasons)) or "the batch did not run"), file=sys.stderr)
        return 1
    lines, data = _report(spec, len(pv.members), service.results, ends[-1], max(1, args.workers), note)
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
