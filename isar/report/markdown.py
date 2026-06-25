"""Markdown renderer: a VCS-diffable `.md` plus sibling PNG files. Returns the markdown text and a
`{relative_path: png_bytes}` map the build step writes next to it (no embedding — files are linked).
"""
from __future__ import annotations

from isar.report import model as _model

_FIGDIR = "figures"


def _safe(fig_id: str) -> str:
    return fig_id.replace(":", "_")


def _fmt(x) -> str:
    if x is None:
        return ""
    if isinstance(x, float):
        return f"{x:.4g}"
    return str(x)


def _cell(x) -> str:
    return str(x).replace("|", "\\|").replace("\n", " ")


def _table(headers, rows) -> str:
    out = ["| " + " | ".join(_cell(h) for h in headers) + " |",
           "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        out.append("| " + " | ".join(_cell(c) for c in r) + " |")
    return "\n".join(out)


def render_markdown(model, figures_map: dict):
    """Return (markdown_text, files) where files maps `figures/<id>.png` -> PNG bytes."""
    files: dict = {}

    def img(fig_id: str, alt: str) -> str:
        png = figures_map.get(fig_id)
        if not png:
            return ""
        rel = f"{_FIGDIR}/{_safe(fig_id)}.png"
        files[rel] = png
        return f"![{alt}]({rel})"

    md: list = [
        f"# ISAR reduction report — {model.experiment_id}",
        "",
        f"> {model.note}",
        "",
        _table(["field", "value"], [
            ("facility / instrument", f"{model.facility} / {model.instrument}"),
            ("parser", f"{model.plugin} {model.plugin_version} ({model.format_version})"),
            ("rejection rule", model.rule),
            ("energy calibration", model.energy_status),
            ("scans / measurements", f"{model.n_scans} / {model.n_measurements}"),
        ]),
        "",
        "## Summary",
        _table(["metric", "value"], [(k, _fmt(v)) for k, v in model.summary.items()]),
        "",
        "## Q coverage",
        img(_model.FIG_COVERAGE, "Q coverage"),
    ]
    if model.coverage["gaps"]:
        md.append(f"\n**Coverage gaps** in {model.coverage['component']}: {model.coverage['gaps']}")

    md += ["", "## Catalogue",
           _table(["group key", "members", "merge"],
                  [(c["key_fields"], c["n_members"], c["merge"]) for c in model.catalogue]),
           "", "## What was flagged & removed",
           _table(["category", "counts"], [
               ("point flags", model.rollup["point_flags"]),
               ("spectrum flags", model.rollup["spectrum_flags"]),
               ("scan flags", model.rollup["scan_flags"]),
               ("miniscan passes rejected", model.rollup["passes_rejected"]),
               ("points with rejection", model.rollup["points_with_rejection"]),
               ("counts removed", _fmt(model.rollup["counts_removed"])),
           ])]

    if model.merged:
        md += ["", "## Merged spectra (combine-before-divide)",
               "Repeat scans at the same (Q, T) are combined by summing raw counts AND monitors, "
               "then dividing only if you wish — never averaging already-normalized rates (§7.5). "
               "Each contributing scan is still shown in full below."]
        for i, m in enumerate(model.merged):
            members = ", ".join(str(mem[1]) for mem in m.members)
            md += ["", f"### {m.key_fields} — {m.n_members} scans, {m.n_points} points",
                   img(m.fig_id, "merged spectrum"), f"combined scans: {members}"]

    for s in model.scans:
        md += ["", f"## Scan {s.scan_number} — {s.scan_type} "
                   f"(T={_fmt(s.temperature)} K, E {_fmt(s.energy_min)}…{_fmt(s.energy_max)} meV)"]
        if s.scan_flags:
            md.append(_table(["scan flag", "severity", "reason"],
                             [(f["type"], f["severity"], f["reason"]) for f in s.scan_flags]))
        for sp in s.spectra:
            md += ["", f"### {sp.detector_id} — Q={sp.q} ({sp.q_source}); "
                       f"{sp.n_masked} of {sp.n_points} masked"]
            if sp.flags:
                md.append(_table(["spectrum flag", "severity", "reason"],
                                 [(f["type"], f["severity"], f["reason"]) for f in sp.flags]))
            md.append(img(_model.fig_spectrum(s.scan_number, sp.detector_id), sp.detector_id))
            if not sp.removals:
                continue
            md.append(_table(
                ["id", "kind", "point", "E (meV)", "passes", "counts removed", "rule", "reason"],
                [(rm.stable_id, rm.kind, rm.point, _fmt(rm.energy), rm.passes or "",
                  _fmt(rm.magnitude), rm.rule or "", rm.reason) for rm in sp.removals]))
            for rm in sp.removals:
                if rm.kind == "cosmic_ray_rejected":
                    cell = img(_model.fig_miniscan(s.scan_number, sp.detector_id, rm.point),
                               f"passes at p{rm.point}")
                    if cell:
                        md += [f"\nPasses at point {rm.point} (rejected in red):", cell]

    if model.other_scans:
        md += ["", "## Alignment / calibration scans (captured, not reduced)",
               _table(["scan", "type", "scanned axis", "flags"],
                      [(x["scan_number"], x["scan_type"], x["axis"], x.get("flags", ""))
                       for x in model.other_scans])]

    return "\n".join(md) + "\n", files
