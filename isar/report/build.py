"""`generate_report` — the one entry point: collate → model → figures → write HTML + Markdown + JSON.

The JSON sidecar (`<name>.json`) is the machine-readable companion: the full model, including every
removal's stable id, so the §8 review loop (a later chunk) can let a human accept/reject and feed the
result back into the card. It is always written; HTML/Markdown are selectable.
"""
from __future__ import annotations

import json
from pathlib import Path

from isar.reduce import collate as _collate
from isar.report import figures, html, markdown
from isar.report import model as _model
from isar.schema import _json


def generate_report(records, card, out_dir, *, formats=("html", "markdown"),
                    name: str = "report") -> dict:
    """Render a reduction report for `records` to `out_dir`. Returns the written paths by kind.

    Read-only over the records (nothing is mutated or deleted). `formats` selects HTML and/or
    Markdown; the JSON sidecar is always emitted.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    collation = _collate.collate(records, card)
    model = _model.build_report_model(records, collation, card)
    figs = figures.report_figures(records, collation)

    written: dict = {}
    sidecar = out / f"{name}.json"
    sidecar.write_text(json.dumps(_json.to_jsonable(model), indent=2), encoding="utf-8")
    written["json"] = sidecar

    if "html" in formats:
        p = out / f"{name}.html"
        p.write_text(html.render_html(model, figs), encoding="utf-8")
        written["html"] = p

    if "markdown" in formats:
        md_text, files = markdown.render_markdown(model, figs)
        p = out / f"{name}.md"
        p.write_text(md_text, encoding="utf-8")
        for rel, png in files.items():
            fp = out / rel
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_bytes(png)
        written["markdown"] = p

    return written
