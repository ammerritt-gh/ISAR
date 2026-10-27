"""The window's plain words for the fitting code's status words, and what each means. No Qt.

The scan-list facade returns the fitting code's word verbatim (``Status.word``, written by the fast pass, the
deep pass and the results writer; the export keeps it too). This table is the GUI's display layer over it: the
scan list cell and the inspector show the plain word, and their hover gives the code word and one line of
meaning. Help > *What the status words mean* is generated from the same table, so the two cannot drift.

A word not in the table is shown as stored, with no meaning: nothing is hidden or renamed on a guess. Meanings
are read against ``docs/USING_ISAR.md`` ("Reading the peak count") and ``docs/GLOSSARY.md`` (close call, count
state, fast pass).
"""
from __future__ import annotations

import html
from typing import NamedTuple

PARSED, FAST, DEEP = "parsed", "fast", "deep"          # the three status columns (the same ids as theme.py)
TITLES = {PARSED: "Parsed", FAST: "Fast pass", DEEP: "Deep pass"}
COLUMN_INTRO = {
    PARSED: "Whether ISAR read the file. It claims nothing more: not calibration, not a resolution curve, not a fit.",
    FAST: "The quick fit every spectrum gets: it tries every peak count and says how settled the count is.",
    DEEP: "The slower, sampled fit you send a spectrum to when the fast pass is not settled. While its sampler "
          "settings are provisional (not yet calibrated), a count is marked (provisional) and is not to be trusted yet.",
}
HELD = "held"
PROVISIONAL = "(provisional)"
PROVISIONAL_MEANING = "The deep pass ran on provisional sampler settings, so this count is not yet to be trusted."


class Entry(NamedTuple):
    plain: str        # what the window shows
    meaning: str      # one line, for the hover and the Help page


TABLE: dict[str, dict[str, Entry]] = {
    PARSED: {
        "ok": Entry("ok", "The file was read and holds a temperature and a Q."),
        "no T": Entry("no T", "The file was read but holds no temperature."),
        "no Q": Entry("no Q", "The file was read but holds no Q."),
        "no T, no Q": Entry("no T, no Q", "The file was read but holds neither a temperature nor a Q."),
        "not read": Entry("not read", "The file could not be read; the reason is shown beside it."),
    },
    FAST: {
        "resolved": Entry("clear", "The peak count is settled: the preferred count has probability 0.80 or more."),
        "close_call": Entry("close call", "Two peak counts are close: the preferred count has probability below "
                                          "0.80, so the alternatives carry the rest. The count is kept out of "
                                          "the dispersion."),
        "unresolved": Entry("unresolved", "A rival model could not be evaluated, so no winner is declared. A "
                                          "failed calculation is never counted against a model."),
        "search_limited": Entry("search limit reached", "The search stopped at its peak-count ceiling or its time "
                                                       "budget; the count is the best found and there may be "
                                                       "more peaks."),
        "held": Entry("held", "Not fitted yet: something it needs is missing (the reason says what). It is "
                              "retried on the next run once that is added."),
        "not fitted": Entry("not fitted", "No fit was made: the spectrum was excluded, undersampled or the fit "
                                          "failed. The reason says which."),
        "not run": Entry("not run", "The analysis has not fitted this spectrum yet."),
        "not recorded": Entry("not recorded", "A fit is stored but it has no fast-pass record (it was written "
                                              "before the fast pass existed); a re-run refits it."),
    },
    DEEP: {
        "resolved": Entry("clear", "The deep pass settles the peak count: the preferred count has probability "
                                   "0.80 or more."),
        "close_call": Entry("close call", "The deep pass still finds two peak counts close: the preferred count "
                                          "has probability below 0.80."),
        "search_limited": Entry("search limit reached", "The deep pass ran out of its time budget, or a model "
                                                       "stayed suspect after restarts; the probabilities cover "
                                                       "only the models it sampled."),
        "refused": Entry("refused", "The deep pass could not run on this spectrum; the reason says why."),
        "not run": Entry("not run", "The deep pass has not been run on this spectrum."),
    },
}

# What a hold reason is waiting for, first match wins: (words in the reason, what to say it needs).
_NEEDS = (
    (("resolution folder", "resolution curve", "resolution library", "curve at"), "resolution curves"),
    (("slit",), "a slit setting"),
    (("counts may not be raw",), "its counts accepted"),
    (("energy zero", "uncalibrated", "elastic", "zero"), "an energy zero"),
)
_NON_RAW = "non-raw counts"


def held_word(reason: str | None) -> str:
    """``held: needs <what>`` from a hold reason; a reason it does not know is not guessed at."""
    low = (reason or "").casefold()
    if _NON_RAW in low:
        return "held: counts are not raw"
    for words, needs in _NEEDS:
        if any(w in low for w in words):
            return f"held: needs {needs}"
    return "held: see the reason"


def plain_word(column: str, word: str, reason: str | None = None, provisional: bool = False) -> str:
    """The word the window shows for the fitting code's ``word`` in ``column``; ``provisional`` (a deep count made
    on provisional sampler settings) adds the mark."""
    if column == FAST and word == HELD:
        return held_word(reason)
    entry = TABLE.get(column, {}).get(word)
    shown = entry.plain if entry is not None else word
    return f"{shown} {PROVISIONAL}" if provisional else shown


def meaning(column: str, word: str) -> str:
    """One line on what the word means; "" for a word not in the table."""
    entry = TABLE.get(column, {}).get(word)
    return entry.meaning if entry is not None else ""


def tooltip(column: str, word: str, reason: str | None = None, provisional: bool = False) -> str:
    """The hover: the plain word with the fitting code's word, the meaning, then the stored reason."""
    shown = plain_word(column, word, reason, provisional)
    head = shown if shown == word else f"{shown}  (the fitting code's word: {word})"
    lines = [head, meaning(column, word), PROVISIONAL_MEANING if provisional else ""]
    if reason and reason != word:
        lines.append(reason)
    return "\n".join(line for line in lines if line)


def matches(term: str, column: str, word: str, reason: str | None = None) -> bool:
    """Whether the filter ``term`` (lower case) is in the plain word or the fitting code's word."""
    return term in word.casefold() or term in plain_word(column, word, reason).casefold()


def page_html() -> str:
    """Help > *What the status words mean*, generated from :data:`TABLE`."""
    out = ["<h2>What the status words mean</h2>",
           "<p>The scan list and the inspector show these plain words. Hover a word to see the fitting code's own "
           "word beside it. <b>Export results</b> writes the fitting code's words, not these.</p>"]
    for column, entries in TABLE.items():
        out.append(f"<h3>{html.escape(TITLES[column])}</h3><p>{html.escape(COLUMN_INTRO[column])}</p>")
        out.append('<table cellspacing="0" cellpadding="4" border="0">'
                   "<tr><th align=\"left\">Shown</th><th align=\"left\">Fitting code</th>"
                   "<th align=\"left\">Meaning</th></tr>")
        for word, entry in entries.items():
            shown = "held: needs …" if (column, word) == (FAST, HELD) else entry.plain
            out.append(f"<tr><td><b>{html.escape(shown)}</b></td><td>{html.escape(word)}</td>"
                       f"<td>{html.escape(entry.meaning)}</td></tr>")
        out.append("</table>")
    out.append("<h3>Counts</h3><p>A <b>scan set</b> is one scan with one file per analyzer. A <b>spectrum</b> "
               "is one analyzer of one scan set. <i>Analyzers read</i> counts the spectra.</p>")
    out.append("<h3>Stand-in value</h3><p>When a value is missing (for example the temperature), the fit may run "
               "on a stated stand-in instead. Entering the value replaces it; fits made before it are marked out of "
               "date until you run the analysis again.</p>")
    return "\n".join(out)
