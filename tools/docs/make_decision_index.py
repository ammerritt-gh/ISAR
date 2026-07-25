#!/usr/bin/env python
"""Generate `docs/DECISION_INDEX.md` from the append-only ledger `docs/DECISIONS.md`.

The ledger is append-only (D4) and its IDs are stable citation anchors (D2), so it may not be
reordered or pruned. That makes it hard to answer the one question a reader usually has: *which
rulings are still in force?* This script answers it without touching the ledger.

A ruling is flagged **amended** when a later ruling's own preamble says it amends, supersedes, or
resolves against it. That is deliberately conservative: it reports what the ledger claims about
itself rather than trying to judge which rulings still bind.

Run from the repo root:

    python tools/docs/make_decision_index.py            # write docs/DECISION_INDEX.md
    python tools/docs/make_decision_index.py --check    # verify it is up to date, write nothing

Idempotent; safe to re-run after every ledger append. `--check` is what `tests/docs/` runs, so an
append to the ledger that leaves the index stale fails the suite rather than rotting silently.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LEDGER = REPO / "docs" / "DECISIONS.md"
OUT = REPO / "docs" / "DECISION_INDEX.md"

RULING = re.compile(r"^- \*\*([A-Z]+\d+)\.\*\*\s*(.*)$", re.M)
AMENDS = re.compile(r"(\S+\s+\S+\s+)?\b(?:amends?|supersed(?:es|ing)|resolves it against)\b([^.)]*)", re.I)
IDREF = re.compile(r"\b([A-Z]{1,2}\d+)\b")
# "does not amend O24" must not register as amending O24.
NEGATION = re.compile(r"\b(?:not|never|without|rather than|instead of|no)\b\s*$", re.I)

SERIES = {
    "V": "Vocabulary & framing",
    "P": "Provenance & data model",
    "R": "Reporting",
    "S": "Statistics",
    "O": "Objective, budget, acquisition",
    "T": "Simulation tiers & TAVI",
    "W": "Workspace hygiene",
    "D": "Docs architecture",
    "G": "GUI",
    "U": "Unification",
}


def split_meta(rest: str) -> tuple[str, str]:
    """Split a leading parenthesised preamble from the ruling body, honouring nested parens."""
    if not rest.startswith("("):
        return "", rest
    depth = 0
    for i, ch in enumerate(rest):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return rest[1:i], rest[i + 1:].lstrip()
    return "", rest  # unbalanced — treat the whole thing as body


def parse(text: str) -> list[tuple[str, str, str]]:
    """-> [(id, meta-in-parens, one-line summary)] in ledger order.

    A ruling's prose wraps over several lines, so each block runs from its own ``- **ID.**`` to
    the next one (or to the next heading). Matching a single line truncates every wrapped ruling.
    """
    rows = []
    starts = list(RULING.finditer(text))
    for i, m in enumerate(starts):
        rid = m.group(1)
        end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        block = text[m.end(1) + len(".**"): end]
        # stop at a heading that interrupts before the next ruling
        block = re.split(r"\n#{2,} ", block)[0]
        rest = re.sub(r"\s+", " ", block).strip()
        meta, body = split_meta(rest)
        bold = re.match(r"\*\*(.+?)\*\*", body)
        summary = bold.group(1) if bold else body
        summary = re.sub(r"\s+", " ", summary).strip().rstrip(".")
        if len(summary) > 160:
            summary = summary[:157].rstrip() + "…"
        rows.append((rid, meta, summary))
    return rows


def amendments(rows) -> dict[str, list[str]]:
    """-> {amended_id: [amending_ids]}, read from each ruling's own preamble.

    Deliberately conservative on both sides. A target must be an ID the ledger actually defines,
    so prose tokens that merely look like IDs cannot invent an amendment; and a clause negated
    immediately before the verb ("does **not** amend O24") is skipped, so a ruling that
    distinguishes itself from an earlier one is not recorded as overriding it.
    """
    known = {rid for rid, _, _ in rows}
    out: dict[str, list[str]] = {}
    for rid, meta, summary in rows:
        for preceding, clause in AMENDS.findall(meta + " " + summary):
            if NEGATION.search(preceding or ""):
                continue
            for target in IDREF.findall(clause):
                if target != rid and target in known:
                    out.setdefault(target, []).append(rid)
    return out


def series_of(rid: str) -> str:
    return re.match(r"([A-Z]+)", rid).group(1)


def render() -> tuple[str, int, int]:
    """-> (the whole file's text, ruling count, amended count). Writes nothing."""
    rows = parse(LEDGER.read_text(encoding="utf-8"))
    if not rows:
        raise SystemExit("parsed no rulings — has the ledger format changed?")
    amended = amendments(rows)

    lines = [
        "# Decision index",
        "",
        "> **Lifecycle:** Current",
        "> **Authority:** none — `docs/DECISIONS.md` is the ledger; this is a generated pointer",
        "> **Provenance:** generated by `python tools/docs/make_decision_index.py`",
        "",
        f"{len(rows)} rulings, newest last within each series. `docs/DECISIONS.md` is append-only "
        "(D4) and its IDs are stable citation anchors (D2), so nothing there is ever reordered or "
        "removed — which makes *\"is this still in force?\"* hard to answer by reading it. This "
        "index answers that question and nothing else.",
        "",
        "**Amended** means a later ruling's own preamble says it amends, supersedes, or resolves "
        "against this one. Read both. An amended ruling is not void: usually only part of it moved.",
        "",
        "Do not edit this file — edit the ledger and re-run the generator.",
        "",
    ]

    for prefix in sorted({series_of(r[0]) for r in rows}, key=lambda p: list(SERIES).index(p) if p in SERIES else 99):
        group = [r for r in rows if series_of(r[0]) == prefix]
        if not group:
            continue
        lines.append(f"## {prefix}-series — {SERIES.get(prefix, 'other')}")
        lines.append("")
        lines.append("| ID | Status | Ruling |")
        lines.append("|---|---|---|")
        for rid, meta, summary in sorted(group, key=lambda r: int(re.search(r"\d+", r[0]).group())):
            if rid in amended:
                status = "amended by " + ", ".join(sorted(set(amended[rid])))
            else:
                status = "in force"
            lines.append(f"| **{rid}** | {status} | {summary} |")
        lines.append("")

    return "\n".join(lines), len(rows), len(amended)


def check() -> int:
    """Verify the committed index against a fresh render. Writes nothing.

    Modelled on `collect_evidence.py --check`, and for the same reason: a generated file with no
    freshness check is a file that goes stale on the next append and stays wrong until somebody
    happens to re-run its generator.
    """
    if not OUT.exists():
        print(f"missing {OUT.relative_to(REPO)} — run this script without --check", file=sys.stderr)
        return 1
    expected, n_rows, _ = render()
    actual = OUT.read_text(encoding="utf-8")
    if actual == expected:
        print(f"{OUT.relative_to(REPO)} is up to date — {n_rows} rulings")
        return 0
    exp_lines, act_lines = expected.splitlines(), actual.splitlines()
    for i in range(max(len(exp_lines), len(act_lines))):
        want = exp_lines[i] if i < len(exp_lines) else "<end of file>"
        got = act_lines[i] if i < len(act_lines) else "<end of file>"
        if want != got:
            print(
                f"{OUT.relative_to(REPO)} is stale — regenerate with "
                f"`python tools/docs/make_decision_index.py`.\n"
                f"  first difference at line {i + 1}:\n"
                f"    committed: {got}\n"
                f"    generated: {want}",
                file=sys.stderr,
            )
            break
    return 1


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not LEDGER.exists():
        print(f"missing ledger: {LEDGER}", file=sys.stderr)
        return 1
    if argv == ["--check"]:
        return check()
    if argv:
        print(f"usage: {Path(__file__).name} [--check]", file=sys.stderr)
        return 2
    text, n_rows, n_amended = render()
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(REPO)} — {n_rows} rulings, {n_amended} amended")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
