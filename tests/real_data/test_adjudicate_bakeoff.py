"""Gated live bake-off: local 9B vs the NanoGPT comparator on QC-flagged synthetic scans (§7.3).

This is the design's adequacy gate — run only when a real LLMM is reachable. It builds synthetic
scans with injected, deterministically-flagged defects (stale head → mask, dead detector → drop,
divergent sensor → hold), adjudicates them with each real model, and scores recall + the
destructive-error rate (the metric that decides "adequate"). It prints the table for the human and
hard-asserts the load-bearing bar: a serviceable local 9B must destroy no genuine data.

Skipped automatically when LLMM is not up (CI / offline). Marked slow; deselect with -m "not slow".
"""
import urllib.request

import pytest

from isar.parsers import spec_s30
from isar.reduce import adjudicate_score, qc
from isar.reduce.adjudicate import DEFAULT_CLOUD_FALLBACK_MODEL
from isar.reduce.llm_client import LLMMClient
from isar.synth_format import quirks, run_builder

ENDPOINT = "http://127.0.0.1:8766"
LOCAL = "local_desktop/qwen3p5-9b"
CLOUD = DEFAULT_CLOUD_FALLBACK_MODEL   # the comparator is whatever the adjudicator would fall back to


def _llmm_up() -> bool:
    try:
        with urllib.request.urlopen(ENDPOINT + "/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not _llmm_up(), reason=f"LLMM not reachable at {ENDPOINT}"),
]


def _scenario(tmp_path, name, seed):
    """One reduced + QC'd synthetic measurement, paired with its injected ground-truth quirks."""
    card = spec_s30.card_template()
    run = run_builder.build_s30_run(name, n_scans=1, seed0=seed, target_peak_counts=300.0)
    sc = run.scans[0]
    quirks.inject_stale_head(sc, "ana5", n=3)           # → mask points 0,1,2
    quirks.inject_dead_detector(sc, "ana3")             # → drop_detector
    quirks.inject_sensor_divergence(sc, value=298.0)    # → hold_for_human (scan)
    truth = {s.scan_number: s.truth.get("quirks", []) for s in run.scans}
    path = tmp_path / f"{name}.spec_supplementary.dat"
    path.write_bytes(spec_s30.emit_file(run, card))
    records = spec_s30.parse_file(path, card)
    for r in records:
        qc.qc_record(r, card)
    return [(r, truth.get(r.scan_number, [])) for r in records if r.scan_type == "measurement"]


def test_local_vs_nanogpt_bakeoff(tmp_path):
    results = {}
    for pin in (LOCAL, CLOUD):
        sub = tmp_path / pin.replace("/", "_")
        sub.mkdir(parents=True, exist_ok=True)
        pairs = _scenario(sub, "BAKE1", 7) + _scenario(sub, "BAKE2", 21)
        client = LLMMClient(ENDPOINT, pin, timeout=120)
        results[pin] = adjudicate_score.bakeoff(pairs, client)

    print("\n" + adjudicate_score.format_table(results))

    for pin, agg in results.items():
        assert agg["n_scans"] > 0, f"{pin}: no scans adjudicated"

    local = results[LOCAL]
    if local["schema_adherence"] > 0:                   # the 9B was serviceable
        assert local["destructive"] == 0, (
            f"local 9B made {local['destructive']} destructive error(s) — "
            "masked/dropped genuine data, the bar the design forbids"
        )
