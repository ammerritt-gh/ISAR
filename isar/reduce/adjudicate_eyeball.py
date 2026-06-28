"""Adjudication eyeball: a real LLM-adjudicated report written to ``out/``, so the reasoning is
visible (§7.3, §10 eyeball pause).

Trusting an LLM to make QC decisions depends on *seeing its reasoning*. This generator builds a few
synthetic scans with injected, deterministically-flagged defects, runs the real adjudicator over them
(local 9B by default), and writes a full report — HTML + Markdown + the JSON sidecar + the
``corrections`` audit — to ``out/adjudication_eyeball/`` (gitignored, easy to find). Because the
quirks are injected with known truth, it also prints recall + the destructive-error count so you can
gauge the model at a glance.

Run it:  ``python -m isar.reduce.adjudicate_eyeball``  (LLMM must be up with the model launched).
Override the model/endpoint with ``ISAR_LLM_MODEL`` / ``ISAR_LLM_ENDPOINT``.
"""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

from isar.parsers import spec_s30
from isar.reduce import adjudicate, adjudicate_score, qc
from isar.reduce.llm_client import LLMMClient
from isar.report import build as report_build
from isar.synth_format import quirks, run_builder

_REPO = Path(__file__).resolve().parents[2]
_OUT = _REPO / "out" / "adjudication_eyeball"
_DEFAULT_ENDPOINT = "http://127.0.0.1:8766"
_DEFAULT_PIN = "local_desktop/qwen3p5-9b"


def _llmm_up(endpoint: str) -> bool:
    try:
        with urllib.request.urlopen(endpoint + "/health", timeout=2) as r:   # nosec B310 (localhost)
            return r.status == 200
    except Exception:
        return False


def _build_run():
    """Two synthetic measurements carrying defects the deterministic layer flags (with truth)."""
    run = run_builder.build_s30_run("ADJ_EYEBALL", n_scans=2, seed0=7, target_peak_counts=300.0)
    quirks.inject_stale_head(run.scans[0], "ana5", n=3)          # leading stuck points → mask
    quirks.inject_dead_detector(run.scans[0], "ana3")            # flat analyzer → drop_detector
    quirks.inject_sensor_divergence(run.scans[0], value=298.0)   # disconnected T sensor → hold
    quirks.inject_dead_detector(run.scans[1], "ana7")            # one dead analyzer, else clean
    truth = {s.scan_number: s.truth.get("quirks", []) for s in run.scans}
    return run, truth


def main(endpoint: str | None = None, pin: str | None = None) -> dict | None:
    endpoint = endpoint or os.environ.get("ISAR_LLM_ENDPOINT", _DEFAULT_ENDPOINT)
    pin = pin or os.environ.get("ISAR_LLM_MODEL", _DEFAULT_PIN)
    _OUT.mkdir(parents=True, exist_ok=True)

    if not _llmm_up(endpoint):
        print(f"LLMM not reachable at {endpoint} — start it (and launch the model), then re-run.")
        return None

    card = spec_s30.card_template()
    run, truth = _build_run()
    src = _OUT / "ADJ_EYEBALL.spec_supplementary.dat"
    src.write_bytes(spec_s30.emit_file(run, card))
    records = spec_s30.parse_file(src, card)
    for r in records:
        qc.qc_record(r, card)

    client = LLMMClient(endpoint, pin, timeout=120)
    adjudicate.adjudicate_records(records, card, client)

    written = report_build.generate_report(records, card, _OUT, name="report")
    audit = _OUT / "corrections.json"
    audit.write_text(json.dumps(card.state.corrections, indent=2), encoding="utf-8")

    cards = [adjudicate_score.score_adjudication(r.adjudication, truth.get(r.scan_number, []))
             for r in records if r.scan_type == "measurement" and r.adjudication]
    agg = adjudicate_score.aggregate(cards)

    bar = "=" * 78
    print(bar)
    print(f"ADJUDICATION EYEBALL  —  model={pin}")
    print(f"  recall={agg['recall']:.2f}   destructive={agg['destructive']}   "
          f"schema_adherence={agg['schema_adherence']:.2f}   scans={agg['n_scans']}")
    print("  open the report:")
    for kind, path in written.items():
        print(f"    {kind:9}: {path}")
    print(f"    audit    : {audit}")
    print(bar)
    return written


if __name__ == "__main__":
    main()
