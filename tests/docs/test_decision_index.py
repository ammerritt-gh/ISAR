"""Focused tests for the generated decision-index parser."""
from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "docs" / "make_decision_index.py"
SPEC = importlib.util.spec_from_file_location("make_decision_index", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
make_decision_index = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(make_decision_index)


def test_amendments_reject_plain_and_markdown_emphasized_negation():
    rows = [
        ("O1", "", "original"),
        ("O2", "does not amend O1", "plain negation"),
        ("O3", "does **not** amend O1", "emphasized negation"),
        ("O4", "amends O1", "actual amendment"),
    ]

    assert make_decision_index.amendments(rows) == {"O1": ["O4"]}


def test_amendments_read_the_passive_voice_a_headline_supersession_uses():
    """H4 writes "S8 is superseded", not "supersedes S8" — the same claim, so it must count.

    The negated form must still be refused: the participle has to follow the copula immediately,
    which is what keeps "is not superseded" (plain or emphasized) out.
    """
    rows = [
        ("S8", "", "retain AIC"),
        ("S9", "", "S8 is not superseded by this ruling"),
        ("S10", "", "S8 is **not** superseded here either"),
        ("H4", "", "AIC becomes a persisted diagnostic. S8 is superseded"),
    ]

    assert make_decision_index.amendments(rows) == {"S8": ["H4"]}


def test_amendments_accept_only_targets_earlier_in_ledger_order():
    rows = [
        ("O1", "amends O2 and O999 and O1", "invalid forward, unknown, and self references"),
        ("O2", "", "later target"),
        ("O3", "supersedes O1", "valid backward reference"),
    ]

    assert make_decision_index.amendments(rows) == {"O1": ["O3"]}
