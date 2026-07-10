"""The run report (UNIFICATION_DESIGN §7) — the product of `isar run`.

Built from the session ledger (no refitting): a header of counts, a per-scan triage table, the
dispersion summary, held/declined explanations, gaps, and a machine-readable JSON sidecar. The
`results.csv` is written by the driver; this adds the human-readable report.md/.html + report.json
and the dispersion figure. No gold CSV anywhere (that is `isar score`).

`isar run --no-render` skips this entire step — including the report.json sidecar — because nothing
mid-campaign consumes it (the driver reads only results.csv / card.state.json). The campaign re-renders
once at the end via `isar report`, which refreshes report.md/.html/.json + figures from persisted state.
"""
from __future__ import annotations

import json
from pathlib import Path

from isar.analyze import gold as gold_mod
from isar.report import fit_figures
from isar.report.findings import FindingsDoc


def _outcomes(session):
    """Flatten the ledger to (scan_number, detector, outcome-dict), fitted scans first."""
    out = []
    for e in sorted(session.scans.values(), key=lambda e: e.scan_number):
        for det, oc in (e.detectors or {}).items():
            out.append((e.scan_number, det, e.status, oc))
    return out


def _scan_json(scan, det, status, oc) -> dict:
    """One report.json `scans` entry. Additive: carries a per-mode `modes` list (mode, omega0,
    omega0_err, gamma, amplitude) only for multi-mode fits (`n_modes` > 1); pooled masters carry
    their member provenance (`pooled`, `n_members`, `members`)."""
    d = {"scan": scan, "detector": det, "status": status,
         **{k: oc.get(k) for k in ("verdict", "disposition", "omega0", "confidence", "best_label")}}
    if (oc.get("n_modes") or 1) > 1 and oc.get("modes"):
        d["n_modes"] = oc.get("n_modes")
        d["modes"] = [{"mode": i, "omega0": m.get("omega0"), "omega0_err": m.get("omega0_err"),
                       "gamma": m.get("gamma"), "amplitude": m.get("amplitude")}
                      for i, m in enumerate(oc.get("modes"))]
    if oc.get("pooled"):
        members = oc.get("members")
        if members is not None and len(members) != int(oc.get("n_members") or -1):
            raise ValueError(
                f"master scan {scan}: n_members={oc.get('n_members')} != len(members)="
                f"{len(members)} — member provenance corrupt")
        d["pooled"] = True
        d["n_members"] = oc.get("n_members")
        d["members"] = members
    return d


def _fmt(x, nd=3):
    if x is None:
        return "—"
    try:
        return f"{float(x):.{nd}f}"
    except (TypeError, ValueError):
        return str(x)


def write_run_report(session, card, workspace, *, formats=("html", "markdown")) -> dict:
    formats = tuple(formats) if formats else ("markdown",)
    rd = workspace.report_dir
    rd.mkdir(parents=True, exist_ok=True)
    fig_dir = rd / "figures"
    fig_dir.mkdir(exist_ok=True)

    pol = card.policy
    counts_status, counts_disp = {}, {}
    for e in session.scans.values():
        counts_status[e.status] = counts_status.get(e.status, 0) + 1
        for oc in (e.detectors or {}).values():
            d = oc.get("disposition")
            if d:
                counts_disp[d] = counts_disp.get(d, 0) + 1

    doc = FindingsDoc(f"ISAR run report — {pol.facility}/{pol.instrument}")
    doc.section("Summary")
    doc.para(f"Experiment plugin `{pol.plugin}`, format {pol.format_version}. "
             f"{len(session.scans)} scans in the ledger.")
    doc.bullets([f"**{k}**: {v}" for k, v in sorted(counts_status.items())] or ["(no scans yet)"])
    if counts_disp:
        doc.para("Dispositions (delivered = resolved + flagged): "
                 + ", ".join(f"{k}={v}" for k, v in sorted(counts_disp.items())))

    gaps = (session.last_run or {}).get("scan_gaps") or []
    if gaps:
        doc.section("Scan-number gaps")
        doc.para("Holes in the scan sequence — a scan that never arrived? Worth a glance.")
        doc.bullets(gaps)

    # Dispersion figure (from the ledger; no refitting).
    all_oc = [oc for _n, _d, _s, oc in _outcomes(session)]
    if all_oc:
        png = fit_figures.dispersion_figure(all_oc)
        (fig_dir / "dispersion.png").write_bytes(png)
        doc.section("Dispersion summary")
        doc.figure_block("run/dispersion", "figures/dispersion.png",
                         alt="omega0 vs Q, colored by disposition")

    # Triage table.
    doc.section("Triage table")
    rows = []
    for scan, det, status, oc in _outcomes(session):
        iv = oc.get("interval")
        ivs = (f"[{_fmt(iv[0],2)}, {_fmt(iv[-1],2)}]" if iv else "—")
        pr = oc.get("prior_used") or {}
        cal = oc.get("calib") or {}
        verdict_cell = oc.get("verdict", "")
        disp_cell = oc.get("disposition", "")
        if oc.get("pooled"):
            # Triage-view fix: a pooled master must never render in the junk/reject section beside a
            # "junk"/"reject" label. Override the DISPLAY cells only (the underlying disposition value
            # is unchanged — dispositions are not renamed anywhere). A rejected master is
            # "pooled — uncertifiable", distinct from junk; otherwise "pooled (master, N members)".
            members = oc.get("members")
            if gold_mod.delivered(oc.get("disposition")) and not members:
                raise ValueError(
                    f"pooled master {scan} is delivered but carries no member provenance — "
                    f"re-run isar run on this workspace to backfill members")
            verdict_cell = "pooled"
            disp_cell = ("pooled — uncertifiable" if oc.get("disposition") == "reject"
                         else f"pooled (master, {oc.get('n_members')} members)")
        rows.append([
            scan, det, verdict_cell, disp_cell,
            _fmt(oc.get("omega0"), 3), ivs, _fmt(oc.get("confidence"), 2),
            oc.get("best_label") or "—",
            (f"{pr.get('source')}~{_fmt(pr.get('omega0'),2)}" if pr else "—"),
            cal.get("resolution_source", "—"),
        ])
    if rows:
        doc.table(["scan", "det", "verdict", "disp", "ω₀", "68% interval", "conf",
                   "model", "prior", "resolution"], rows)
    else:
        doc.para("No fitted scans this run.")

    # Pooled masters (chain of custody): name the member scans behind every pooled ω₀ so the
    # delivered-value provenance is auditable end to end.
    pooled = [(scan, oc) for scan, det, status, oc in _outcomes(session) if oc.get("pooled")]
    if pooled:
        doc.section("Pooled masters (chain of custody)")
        crows = []
        for scan, oc in pooled:
            members = oc.get("members")
            q = oc.get("q") or [None, None, None]
            hkl = ", ".join(_fmt(v, 2) for v in q)
            crows.append([scan, hkl, _fmt(oc.get("T"), 1), oc.get("best_label") or "—",
                          oc.get("n_members"), ("; ".join(members) if members else "—")])
        doc.table(["scan", "hkl", "T", "label", "n_members", "members"], crows)

    # Held / declined explanations.
    held = [(e.scan_number, e.status, "; ".join(e.reasons))
            for e in sorted(session.scans.values(), key=lambda e: e.scan_number)
            if e.status in ("held_for_calibration", "held_for_sample", "excluded", "non_result")]
    if held:
        doc.section("Held / declined (nothing deleted — surfaced for you)")
        doc.table(["scan", "status", "reason"], [[s, st, r] for s, st, r in held])

    if pol.energy_status == "absolute":
        doc.caveat("Triple-axis Γ is resolution-limited (1-D Voigt, no 4-D convolution): trust ω₀, "
                   "not Γ. Error bars are statistical-only.", severity="warn")

    # Render outputs.
    written = {}
    md = doc.render()
    if "markdown" in formats or "md" in formats:
        p = rd / "report.md"
        p.write_text(md, encoding="utf-8")
        written["markdown"] = p
    if "html" in formats:
        p = rd / "report.html"
        p.write_text(_markdown_to_html(md, title=f"ISAR run report — {pol.instrument}"),
                     encoding="utf-8")
        written["html"] = p
    # JSON sidecar always.
    pj = rd / "report.json"
    pj.write_text(json.dumps({
        "instrument": pol.instrument, "facility": pol.facility, "plugin": pol.plugin,
        "counts_status": counts_status, "counts_disposition": counts_disp,
        "scan_gaps": gaps,
        "scans": [_scan_json(s, d, st, oc) for s, d, st, oc in _outcomes(session)],
    }, indent=2), encoding="utf-8")
    written["json"] = pj
    return written


# ---------------------------------------------------------------------------
# Minimal, dependency-free markdown -> HTML for the FindingsDoc subset we emit
# (headings, tables, images, blockquotes, bullet lists, bold, paragraphs).
# ---------------------------------------------------------------------------
def _inline(s: str) -> str:
    import html
    import re
    s = html.escape(s, quote=False)
    s = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r'<img alt="\1" src="\2" style="max-width:100%">', s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return s


def _markdown_to_html(md: str, *, title: str) -> str:
    lines = md.splitlines()
    out, i, n = [], 0, len(lines)
    while i < n:
        ln = lines[i]
        if not ln.strip():
            i += 1
            continue
        if ln.startswith("### "):
            out.append(f"<h3>{_inline(ln[4:])}</h3>"); i += 1; continue
        if ln.startswith("## "):
            out.append(f"<h2>{_inline(ln[3:])}</h2>"); i += 1; continue
        if ln.startswith("# "):
            out.append(f"<h1>{_inline(ln[2:])}</h1>"); i += 1; continue
        if ln.lstrip().startswith(">"):
            out.append(f'<blockquote>{_inline(ln.lstrip()[1:].strip())}</blockquote>'); i += 1; continue
        if ln.startswith("|"):                                   # a markdown table
            tbl, j = [], i
            while j < n and lines[j].startswith("|"):
                tbl.append(lines[j]); j += 1
            out.append(_html_table(tbl)); i = j; continue
        if ln.lstrip().startswith(("- ", "* ")):                 # a bullet list
            items, j = [], i
            while j < n and lines[j].lstrip().startswith(("- ", "* ")):
                items.append(_inline(lines[j].lstrip()[2:])); j += 1
            out.append("<ul>" + "".join(f"<li>{it}</li>" for it in items) + "</ul>"); i = j; continue
        out.append(f"<p>{_inline(ln)}</p>"); i += 1

    body = "\n".join(out)
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{title}</title>"
            "<style>body{font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:960px;"
            "margin:2rem auto;padding:0 1rem;line-height:1.45;color:#222}"
            "table{border-collapse:collapse;margin:1rem 0}th,td{border:1px solid #ccc;"
            "padding:4px 8px;text-align:left;font-size:0.9rem}th{background:#f2f5f8}"
            "blockquote{border-left:4px solid #e0a000;margin:1rem 0;padding:.3rem 1rem;"
            "background:#fffaf0}code{background:#f0f0f0;padding:1px 4px;border-radius:3px}"
            "h1{border-bottom:2px solid #ddd;padding-bottom:.3rem}</style></head>"
            f"<body>{body}</body></html>")


def _html_table(rows: list[str]) -> str:
    def cells(line):
        return [c.strip() for c in line.strip().strip("|").split("|")]
    if len(rows) < 2:
        return ""
    head = cells(rows[0])
    body = rows[2:]                                              # rows[1] is the |---| separator
    thead = "<tr>" + "".join(f"<th>{_inline(h)}</th>" for h in head) + "</tr>"
    trs = []
    for r in body:
        c = cells(r)
        trs.append("<tr>" + "".join(f"<td>{_inline(x)}</td>" for x in c) + "</tr>")
    return f"<table>{thead}{''.join(trs)}</table>"
