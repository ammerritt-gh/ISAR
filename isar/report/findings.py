"""FindingsDoc — the shared skeleton for every generated FINDINGS report.

One builder so the three reports (gold scoring, step-1 control, IN8 anchor) read the same way and an
outsider can move between them: plain-language sections first, each figure embedded *with* its caption
(pulled from `captions.CAPTIONS`, so nothing ships uncaptioned), known weaknesses raised as prominent
`caveat` blocks rather than buried in a tail, and a PASS/FAIL `gate`.

Suggested section order (triage-first):
  What this checks (and what it does NOT claim) → Triage summary (delivered / flagged / declined) →
  Plain-language takeaway → How to read this → Step-by-step method → Each figure explained →
  Honest limitations → Gate.
The builder doesn't force the order; the check scripts call the sections in it.
"""
from __future__ import annotations

from isar.report import captions as _captions
from isar.report.markdown import _table

_ICONS = {"warn": "⚠", "info": "ℹ", "stop": "⛔"}


class FindingsDoc:
    def __init__(self, title: str):
        self._parts: list[str] = [f"# {title}", ""]

    def section(self, heading: str) -> "FindingsDoc":
        self._parts += ["", f"## {heading}", ""]
        return self

    def para(self, text: str) -> "FindingsDoc":
        self._parts += [text, ""]
        return self

    def bullet(self, text: str) -> "FindingsDoc":
        self._parts.append(f"- {text}")
        return self

    def bullets(self, items) -> "FindingsDoc":
        for it in items:
            self.bullet(it)
        self._parts.append("")
        return self

    def table(self, headers, rows) -> "FindingsDoc":
        self._parts += [_table(headers, rows), ""]
        return self

    def caveat(self, text: str, *, severity: str = "warn") -> "FindingsDoc":
        """A visually-distinct blockquote for a known weakness — kept prominent, never buried."""
        icon = _ICONS.get(severity, _ICONS["warn"])
        self._parts += [f"> {icon} **Caveat —** {text}", ""]
        return self

    def figure_block(self, fig_id: str, rel_png: str, *, alt: str | None = None) -> "FindingsDoc":
        """Embed a figure followed by its registry caption (title + 'what good looks like' + terms)."""
        cap = _captions.caption_for(fig_id)
        alt = alt or (cap.title if cap else fig_id)
        self._parts += [f"![{alt}]({rel_png})", ""]
        if cap is not None:
            self._parts += [cap.as_markdown(), ""]
        return self

    def gate(self, items, *, heading: str = "Gate") -> "FindingsDoc":
        """Render a PASS/FAIL gate from `items` = [(name, ok: bool, detail: str), ...]."""
        self._parts += ["", f"## {heading}", ""]
        all_ok = True
        for name, ok, detail in items:
            all_ok = all_ok and bool(ok)
            self._parts.append(f"- [{'PASS' if ok else 'FAIL'}] **{name}** — {detail}")
        self._parts += ["", f"**{'PASS' if all_ok else 'FAIL'}**"]
        return self

    def raw(self, text: str) -> "FindingsDoc":
        """Append already-formatted markdown verbatim (escape hatch for one-off blocks)."""
        self._parts.append(text)
        return self

    def render(self) -> str:
        return "\n".join(self._parts).rstrip() + "\n"
