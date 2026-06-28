"""Score an adjudication against the injected synthetic truth, and run the local-vs-cloud bake-off.

The synthetic core records every injected defect in ``scan.truth["quirks"]`` (§4). This module maps
each quirk to the decision it warrants, then measures, per model:

- **recall** — of the decisions the deterministic layer flagged, how many did the model get right;
- **destructive errors** — masks/drops on targets with no injected defect (the failure mode the
  design exists to prevent: destroying genuine data) — the metric that disqualifies a model;
- **schema adherence** — did the model return valid structured output (vs being held for the human).

``bakeoff`` runs a real :class:`~isar.reduce.llm_client.Dispatcher` over (record, quirks) pairs and
aggregates the scores; the gated live test drives it for the local 9B and the NanoGPT comparator.
"""
from __future__ import annotations

from dataclasses import dataclass

from isar.reduce import adjudicate, compact_view

_REMOVAL_DECISIONS = ("mask", "drop_detector")


@dataclass
class ScoreCard:
    expected: int = 0
    matched: int = 0
    destructive: int = 0
    schema_ok: bool = True


def expected_decisions(quirks) -> dict:
    """Each injected quirk → the set of acceptable decisions on its target (ground truth)."""
    exp: dict = {}
    for q in quirks:
        t = q.get("type")
        if t == "spike":
            exp[f"point:{q['detector']}:{q['index']}"] = {"mask"}
        elif t == "stale_head":
            for i in q.get("indices", []):
                exp[f"point:{q['detector']}:{i}"] = {"mask"}
        elif t in ("dead_detector", "empty"):
            exp[f"spectrum:{q['detector']}"] = {"drop_detector", "hold_for_human"}
        elif t == "sensor_divergence":
            exp.setdefault("scan", set()).update({"hold_for_human", "keep"})
        elif t == "truncated":
            exp.setdefault("scan", set()).update({"keep", "hold_for_human"})
        # miniscan_cosmic_ray / monitor_dropout are handled deterministically — not adjudicator targets
    return exp


def score_adjudication(adjudication, quirks) -> ScoreCard:
    schema_ok = adjudication.narrative != adjudicate.FAILURE_NARRATIVE
    exp = expected_decisions(quirks)
    actual = {d.target: d.decision for d in adjudication.decisions}
    matched = sum(1 for target, ok in exp.items() if actual.get(target) in ok)
    destructive = sum(1 for d in adjudication.decisions
                      if d.decision in _REMOVAL_DECISIONS and d.target not in exp)
    return ScoreCard(expected=len(exp), matched=matched, destructive=destructive, schema_ok=schema_ok)


def aggregate(cards) -> dict:
    expected = sum(c.expected for c in cards)
    matched = sum(c.matched for c in cards)
    destructive = sum(c.destructive for c in cards)
    n = len(cards) or 1
    return {
        "n_scans": len(cards),
        "expected": expected,
        "matched": matched,
        "destructive": destructive,
        "recall": (matched / expected) if expected else 0.0,
        "schema_adherence": sum(1 for c in cards if c.schema_ok) / n,
    }


def bakeoff(pairs, client, *, temperature: float = 0.2, max_tokens: int = 1500) -> dict:
    """Adjudicate each (record, quirks) pair with ``client`` and return the aggregate score.

    ``pairs`` is a list of ``(record, quirks)``; the record is adjudicated in place (a throwaway
    audit list collects the corrections). Only flagged records are dispatched.
    """
    cards = []
    sink = _CorrectionSink()
    for record, quirks in pairs:
        if not compact_view._has_flag(record):
            continue
        adjudicate.adjudicate_records([record], sink, client,
                                      temperature=temperature, max_tokens=max_tokens)
        if record.adjudication is not None:
            cards.append(score_adjudication(record.adjudication, quirks))
    return aggregate(cards)


def format_table(results: dict) -> str:
    """Render ``{pin: aggregate}`` as a compact bake-off table."""
    head = f"{'model':40} {'scans':>5} {'recall':>7} {'destructive':>12} {'schema':>7}"
    lines = [head, "-" * len(head)]
    for pin, agg in results.items():
        lines.append(f"{pin:40} {agg['n_scans']:>5} {agg['recall']:>7.2f} "
                     f"{agg['destructive']:>12} {agg['schema_adherence']:>7.2f}")
    return "\n".join(lines)


class _CorrectionSink:
    """A stand-in card that only needs ``state.corrections`` (the bake-off discards the audit)."""
    def __init__(self) -> None:
        self.state = type("_S", (), {"corrections": []})()
