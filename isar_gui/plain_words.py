"""Plain sentences for the words the backend hands the screen.

The backend's labels, ids and error texts are exact and stay exact in its records, its tests and the Log;
the window shows a physicist at 3 AM a sentence instead, and keeps the backend's own text for the hover.
Nothing here decides anything: a text it does not know passes through unchanged. No Qt.
"""
from __future__ import annotations

import re
from pathlib import Path

# Fallbacks: the backend's id (a fact row) or label (a readiness read) -> what the fit does instead.
_STAND_IN = {
    "bose_t0_limit": "no temperature known: the fit assumes T → 0 (no thermal population)",
    "zero-temperature (T → 0⁺) Bose limit": "no temperature known: the fit assumes T → 0 (no thermal population)",
    "slit_inferred": "no slit in the files: inferred from the elastic lines",
    "slit inferred from the elastic lines": "no slit in the files: inferred from the elastic lines",
    "slit_broadest_curve": "no matching slit: the broadest resolution curve is used",
    "broadest-curve fallback": "no matching slit: the broadest resolution curve is used",
    "slit_pending": "no slit yet: it is chosen from the elastic lines when the analysis starts",
    "zero_file_axis": "no clear elastic line: the file's own energy axis is used as it is",
    "file-axis zero (no significant elastic line)": "no clear elastic line: the file's own energy axis is used as it is",
    "no monitor; exposure recorded as missing": "no monitor in the file: the exposure is recorded as missing",
    "facts from the .var are missing": "the .var sidecar is missing: the facts it holds are unknown",
    "counts_accepted": "you accepted these counts as they are",
    "counts accepted by the user": "you accepted these counts as they are",
}

STAND_IN_MARK = "stand-in value used"
STAND_IN_TIP = ("The fit runs with a stand-in instead of a measured value. Entering the value replaces it.")

# Where a file fact was read from (FactRow.file_source).
_SOURCE = {"header": "from the header", "trailer": "from the trailer", "sidecar": "from the sidecar",
           "filename": "from the file name", "card": "from the card", "inferred": "inferred",
           "gpi": "from the gpi file", "file": "from the file", "members": "mean of the pool's members",
           "reported": "reported in the file", "card_offset": "from the card's Q offset"}   # the hkl's q_source words

# The remedy of an attention group, as a sentence naming what to press or do (backend action id -> text).
FIX_PHRASE = {
    "enter_value": "tick the scan sets, then enter the value",
    "locate_folder": "choose the folder that holds the resolution curves",
    "start_analysis": "press Fast-pass all ready",
    "reverse_declaration": "tick the analyzers, then use them again",
    "accept_counts": "tick the analyzers, then accept their counts as they are",
    "import_again": "add the data files again",
    "tell_operator": "see the reasons below; ISAR cannot fix this by itself",
    "wait": "wait for the analysis to finish",
    "none": "",
}

# What a group affects, in plain words (backend use id -> text).
USE_WORDS = {"read": "reading the files", "reduce": "reduction and export",
             "calibrate": "resolution calibration", "fit": "the fit", "steer": "campaign planning"}

# Group titles the backend words as pipeline stages.
GROUP_TITLE = {"q_missing": "Q missing (only campaign planning needs it)",
               "calibration_pending": "Not calibrated yet (energy zero and resolution)",
               "gpi_rejected": "gpi file not used", "gpi_curves_dropped": "gpi curve set not used"}
# The remedy where the action's own sentence names the wrong one: the action names a button, this names the fix.
KIND_FIX = {"gpi_rejected": "fix the gpi file, then add the data again or start the analysis",
            "gpi_folder_changed": "add the data again or start the analysis, so the gpi files are read again"}

_NO_FOLDER = "no resolution folder is saved"
NO_CURVES = "No resolution curves yet. Choose the folder that holds them."
NO_CURVES_TIP = ("ISAR also looks for a folder named rf next to your data and found none.\n"
                 "Choose the folder that holds the measured resolution curves.")


_HOLD_NO_FOLDER = re.compile(r"no resolution folder is saved(?:, and no rf/ folder holding curves was found beside [^;]+)?")


def plain_hold(reason: str | None) -> str:
    """A readiness reason with the "no resolution folder is saved ... beside <path>" sentence made short
    (the path stays in the hover); any other reason is returned as given."""
    return _HOLD_NO_FOLDER.sub("no resolution curves chosen yet", reason) if reason else ""


def drop_echo(word: str, reason: str | None) -> str | None:
    """``reason`` without a leading "<word>: " (the word is shown beside it already); ``None`` when nothing is left."""
    if reason and reason.startswith(f"{word}: "):
        reason = reason[len(word) + 2:]
    return reason or None


# What a readiness state means, in a sentence (backend state id -> text).
STATE_WORDS = {
    "ready": "nothing is missing",
    "fallback": "can run, with a stand-in for something that is missing",
    "pending": "will be done when the analysis starts",
    "held": "waiting for something that is missing; it is tried again when that arrives",
    "unreadable": "its file could not be read",
}


def stand_in(text: str | None) -> str:
    """A fallback id or label as a sentence; an unknown one is returned as given."""
    return _STAND_IN.get(text, text) if text else ""


def source_words(source: str) -> str:
    """Where a file fact was read from, in words ("from the file name")."""
    return _SOURCE.get(source, source)


def fix_sentence(action: str) -> str:
    return FIX_PHRASE.get(action, action)


def affects(uses) -> str:
    return ", ".join(USE_WORDS.get(u, u) for u in uses)


def group_title(kind: str, summary: str, scope: str) -> str:
    """The attention group's title: the backend's summary with the scan-set noun, the stage names
    plainly worded, and a long path out of the line (it stays in the hover)."""
    if kind == "resolution_folder" and summary.partition(": ")[2].startswith(_NO_FOLDER):
        return "Resolution curves: none chosen yet"
    if kind == "gpi_folder_changed":       # the backend's sentence repeats the title; the hover keeps it
        return "gpi folder changed: its values are set aside"
    if kind in GROUP_TITLE:
        rest = summary.partition(": ")[2]
        summary = f"{GROUP_TITLE[kind]}: {rest}" if rest else GROUP_TITLE[kind]
    if scope in ("scan", "unit"):      # "on 2 scans" / "3 scans": the backend counts scan sets
        summary = re.sub(r"\b(\d+) scans\b", r"\1 scan sets", summary)
        summary = re.sub(r"\b1 scan\b$", "1 scan set", summary)
    return summary


def curve_problem(text: str | None) -> str | None:
    """The curve check's problem as a sentence that names the next step."""
    if text is not None and text.startswith(_NO_FOLDER):
        return NO_CURVES
    return text


def plain_reason(reason: str) -> str:
    """The human part of an unread file's reason: no exception class, no file-set stem, no inner Python
    message ("ValueError: S_bad: no analyzer could be read (a03: energy column is not numeric (could not
    convert ...))" -> "a03: energy column is not numeric"). The caller keeps the whole text for the hover."""
    text = re.sub(r"^\w*(?:Error|Exception|Warning): ", "", reason.strip())
    text = re.sub(r"^[^\s:]+: (?=no analyzer could be read)", "", text)       # the file-set stem
    inner = re.fullmatch(r"no analyzer could be read \((.*)\)", text)
    if inner:
        text = inner.group(1)
    outer = re.fullmatch(r"(.+?) \((?:could not|invalid literal|unsupported|cannot)[^()]*(?:\([^()]*\)[^()]*)*\)", text)
    if outer:
        text = outer.group(1)
    return text or reason


_VAR_LOST = ("Without the .var the per-analyzer efficiency, polarisation and elastic-peak values are unknown, and the "
             "bin size is worked out from the data's own spacing. Put the .var in the same folder as the analyzer "
             "files, then add the data again.")


def _names(names) -> str:
    names = list(names)
    head = ", ".join(names[:3])
    return head if len(names) <= 3 else f"{head} and {len(names) - 3} more"


def _sets(n: int) -> str:
    return f"{n} scan set" if n == 1 else f"{n} scan sets"


def _n_sets(c: dict) -> int:
    s = c["sidecar"]
    return len(s["read"]) + len(s["missing"]) + len(s["unreadable"])


def _sidecar_line(s: dict, n: int) -> str:
    if not s["missing"] and not s["unreadable"]:
        return f".var: found and read for all {_sets(n)}."
    bits = [f"found and read for {len(s['read'])} of {_sets(n)}"]
    if s["missing"]:
        bits.append(f"missing for {_names(s['missing'])}")
    if s["unreadable"]:
        bits.append(f"could not be read for {_names(s['unreadable'])}, so treated as missing")
    return ".var: " + "; ".join(bits) + ". " + _VAR_LOST


def _gpi_line(g: dict) -> str:
    if g["error"]:
        return f"gpi: the check failed ({g['error']})."
    if g["folder"] is None:
        return "gpi: no gpi folder is saved or found beside the data."
    found, total = len(g["found"]), len(g["found"]) + len(g["missing"])
    line = f"gpi: found for {found} of {_sets(total)} in {g['folder']}"
    if g["missing"]:
        line += f"; missing for {_names(g['missing'])}"
    if g["not_used"]:
        line += f"; not used for {_names(g['not_used'])} (see Needs attention)"
    return line + "."


def _curves_line(c: dict) -> str:
    if c["error"]:
        return f"Resolution curves (rf): the check failed ({c['error']})."
    if c["folder"] is None:
        return f"Resolution curves (rf): {curve_problem(c['problem']) or NO_CURVES}"
    if c["problem"]:
        return f"Resolution curves (rf): {curve_problem(c['problem'])}"
    return f"Resolution curves (rf): {c['n_curves']} curves for {c['n_analyzers']} analyzers in {c['folder']}."


def companion_lines(c: dict) -> list[str]:
    """One log line each for what an import found beside the analyzer files: the .var, the gpi, the curves."""
    n = _n_sets(c)
    if not n:
        return []
    lines = [_sidecar_line(c["sidecar"], n), _gpi_line(c["gpi"])]
    if c["curves"] is not None:
        lines.append(_curves_line(c["curves"]))
    return lines


def _sidecar_clause(s: dict, n: int) -> str:
    bits = []
    if s["read"]:
        bits.append(f"found {len(s['read'])}/{n}")
    if s["missing"]:
        bits.append(f"missing {len(s['missing'])}/{n}")
    if s["unreadable"]:
        bits.append(f"unreadable {len(s['unreadable'])}/{n}")
    return ".var " + ", ".join(bits)


def _gpi_clause(g: dict) -> str:
    if g["error"]:
        return "gpi check failed"
    if g["folder"] is None:
        return "no gpi"
    total = len(g["found"]) + len(g["missing"])
    return f"gpi {len(g['found'])}/{total}" + (f", {len(g['not_used'])} not used" if g["not_used"] else "")


def _curves_clause(c: dict | None) -> str:
    if c is None:
        return ""
    if c["error"]:
        return "rf check failed"
    if c["folder"] is None:
        return "no rf folder"
    name = Path(c["folder"]).name
    if c["problem"]:
        return f"rf folder {name} has a problem"
    return f"rf folder {name} ({c['n_curves']} curves)"


def companion_clause(c: dict) -> str:
    """The status-bar form, e.g. ".var found 2/2 · gpi 0/2 · rf folder rf (112 curves)"."""
    n = _n_sets(c)
    if not n:
        return ""
    parts = (_sidecar_clause(c["sidecar"], n), _gpi_clause(c["gpi"]), _curves_clause(c["curves"]))
    return " · ".join(p for p in parts if p)
