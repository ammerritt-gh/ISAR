"""Self-contained HTML renderer: one file, every plot embedded as an inline base64 PNG, so it opens
in any browser with nothing to wire up — the "here is exactly what happened to your data" artifact.
"""
from __future__ import annotations

import base64
import html as _html

from isar.report import model as _model

_CSS = """
body{font-family:system-ui,Arial,sans-serif;max-width:1000px;margin:2em auto;padding:0 1em;color:#222}
h1{border-bottom:2px solid #333}h2{margin-top:1.6em;border-bottom:1px solid #ccc}
.note{background:#eef6ff;border-left:4px solid #2a6;padding:.6em 1em;border-radius:4px}
table{border-collapse:collapse;margin:.5em 0;font-size:.9em}
th,td{border:1px solid #ccc;padding:.25em .6em;text-align:left;vertical-align:top}
th{background:#f3f3f3}img{max-width:100%;height:auto;margin:.4em 0}
.removal{background:#fff7ec}.masked{background:#f6f6f6}
"""


def _esc(x) -> str:
    return _html.escape(str(x))


def _img(png, alt: str = "") -> str:
    if not png:
        return ""
    b = base64.b64encode(png).decode("ascii")
    return f'<img alt="{_esc(alt)}" src="data:image/png;base64,{b}">'


def _table(headers, rows, *, row_classes=None) -> str:
    th = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    body = []
    for i, r in enumerate(rows):
        cls = f' class="{row_classes[i]}"' if row_classes else ""
        body.append(f"<tr{cls}>" + "".join(f"<td>{_esc(c)}</td>" for c in r) + "</tr>")
    return f"<table><thead><tr>{th}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def _fmt(x) -> str:
    if x is None:
        return ""
    if isinstance(x, float):
        return f"{x:.4g}"
    return str(x)


def render_html(model, figures_map: dict) -> str:
    o: list = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        f"<title>ISAR reduction report — {_esc(model.experiment_id)}</title>",
        f"<style>{_CSS}</style></head><body>",
        f"<h1>ISAR reduction report — {_esc(model.experiment_id)}</h1>",
        f"<p class='note'>{_esc(model.note)}</p>",
        _table(["field", "value"], [
            ("facility / instrument", f"{model.facility} / {model.instrument}"),
            ("parser", f"{model.plugin} {model.plugin_version} ({model.format_version})"),
            ("rejection rule", model.rule),
            ("energy calibration", model.energy_status),
            ("scans / measurements", f"{model.n_scans} / {model.n_measurements}"),
        ]),
        "<h2>Summary</h2>",
        _table(["metric", "value"], [(k, _fmt(v)) for k, v in model.summary.items()]),
        "<h2>Q coverage</h2>",
        _img(figures_map.get(_model.FIG_COVERAGE), "Q coverage"),
    ]
    if model.coverage["gaps"]:
        o.append(f"<p><b>Coverage gaps</b> in {_esc(model.coverage['component'])}: "
                 f"{_esc(model.coverage['gaps'])}</p>")

    o.append("<h2>Catalogue</h2>")
    o.append(_table(["group key", "members", "merge"],
                    [(_esc(c["key_fields"]), c["n_members"], c["merge"]) for c in model.catalogue]))

    o.append("<h2>What was flagged &amp; removed</h2>")
    o.append(_table(["category", "counts"], [
        ("point flags", model.rollup["point_flags"]),
        ("spectrum flags", model.rollup["spectrum_flags"]),
        ("scan flags", model.rollup["scan_flags"]),
        ("miniscan passes rejected", model.rollup["passes_rejected"]),
        ("points with rejection", model.rollup["points_with_rejection"]),
        ("counts removed", _fmt(model.rollup["counts_removed"])),
    ]))

    for s in model.scans:
        o.append(f"<h2>Scan {s.scan_number} — {_esc(s.scan_type)} "
                 f"(T={_fmt(s.temperature)} K, E {_fmt(s.energy_min)}…{_fmt(s.energy_max)} meV)</h2>")
        if s.scan_flags:
            o.append(_table(["scan flag", "severity", "reason"],
                            [(f["type"], f["severity"], f["reason"]) for f in s.scan_flags]))
        for sp in s.spectra:
            o.append(f"<h3>{_esc(sp.detector_id)} — Q={_esc(sp.q)} ({_esc(sp.q_source)}); "
                     f"{sp.n_masked} of {sp.n_points} masked</h3>")
            if sp.flags:
                o.append(_table(["spectrum flag", "severity", "reason"],
                                [(f["type"], f["severity"], f["reason"]) for f in sp.flags]))
            o.append(_img(figures_map.get(_model.fig_spectrum(s.scan_number, sp.detector_id)),
                          sp.detector_id))
            if not sp.removals:
                continue
            rows = [(rm.stable_id, rm.kind, rm.point, _fmt(rm.energy), rm.passes or "",
                     _fmt(rm.magnitude), rm.rule or "", rm.reason) for rm in sp.removals]
            o.append(_table(["id", "kind", "point", "E (meV)", "passes", "counts removed",
                             "rule", "reason"], rows,
                            row_classes=["removal" if rm.kind == "cosmic_ray_rejected" else "masked"
                                         for rm in sp.removals]))
            for rm in sp.removals:
                if rm.kind == "cosmic_ray_rejected":
                    img = figures_map.get(_model.fig_miniscan(s.scan_number, sp.detector_id, rm.point))
                    if img:
                        o.append(f"<p>Passes at point {rm.point} (rejected shown in red):</p>")
                        o.append(_img(img, f"passes at p{rm.point}"))

    if model.other_scans:
        o.append("<h2>Alignment / calibration scans (captured, not reduced)</h2>")
        o.append(_table(["scan", "type", "scanned axis"],
                        [(x["scan_number"], x["scan_type"], x["axis"]) for x in model.other_scans]))

    o.append("</body></html>")
    return "\n".join(o)
