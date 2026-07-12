"""Qt-free, ISAR-free command assembly for the operator on-ramp.

The New Experiment wizard and the Run action *shell the same engine* (``isar init`` / ``isar run`` /
``isar detect``) rather than import it — the GUI is a thin front door onto the ``isar`` CLI, and the
G16 import contract forbids ``isar_gui`` from importing engine internals (cli, parsers, …). So this
module builds argument vectors and parses the JSON the engine prints; it imports neither Qt nor
``isar``, which keeps it trivially unit-testable and boundary-clean.

Instrument detection is a subprocess (``isar detect --json``) whose output :func:`parse_detect_json`
turns into a :class:`DetectResult`, so the wizard preview is exactly what ``isar init`` will scaffold
— one detector, no drift.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class RunOptions:
    """The subset of ``isar run`` knobs the on-ramp exposes. Defaults mirror the CLI defaults, so a
    default ``RunOptions`` adds no flags to the command (``build_run_command`` stays minimal)."""

    llm: bool = False
    force: bool = False
    M: int = 200
    errors: str = "full"          # full | fast | cheap | bayes
    detectors: str | None = None  # comma-separated detector ids, or None for the card default


@dataclass(frozen=True)
class DetectResult:
    """What ``isar detect`` reported for a data path (the wizard preview). Pure data — no isar types."""

    facility: str
    instrument: str
    fmt: str
    plugin_name: str | None       # None => recognized instrument with no shipped card template yet
    probe: str                    # basename of the file the detection classified

    @property
    def supported(self) -> bool:
        """True when a card template exists — i.e. ``isar init`` can scaffold this workspace."""
        return self.plugin_name is not None


def build_init_command(python: str, data: str, out: str, *, json: bool = False) -> list[str]:
    """The argv for ``isar init <data> --out <out>`` run as ``<python> -m isar ...``.

    ``json=True`` appends ``--json`` so the engine emits one machine-readable object on stdout
    (used by :class:`~isar_gui.ingest_controller.IngestController` for the bootstrap ``init`` step).
    """
    cmd = [python, "-m", "isar", "init", str(data), "--out", str(out)]
    if json:
        cmd.append("--json")
    return cmd


def build_ingest_command(python: str, data_paths, workspace: str, *, json: bool = False) -> list[str]:
    """The argv for ``isar ingest <data>... --workspace <ws>`` run as ``<python> -m isar ...``.

    Parses data into the ledger (status ``ingested``) without fitting. Phase C uses this for the
    minimal inline ingest after a bootstrap and for a drop onto an already-ready workspace; a proper
    IngestController replaces the inline callers in Phase D.
    """
    cmd = [python, "-m", "isar", "ingest", *[str(p) for p in data_paths],
           "--workspace", str(workspace)]
    if json:
        cmd.append("--json")
    return cmd


def build_detect_command(python: str, data: str) -> list[str]:
    """The argv for ``isar detect <data> --json`` (the wizard's instrument preview)."""
    return [python, "-m", "isar", "detect", str(data), "--json"]


def build_run_command(python: str, data_paths, workspace: str, opts: RunOptions) -> list[str]:
    """The argv for ``isar run <data>... --workspace <ws> [opts]``.

    Only non-default options add flags, so the command stays legible in the log dock. Data paths are
    positional (one or more); ``--workspace`` is always present (the engine requires it).
    """
    cmd = [python, "-m", "isar", "run", *[str(p) for p in data_paths], "--workspace", str(workspace)]
    if opts.llm:
        cmd.append("--llm")
    if opts.force:
        cmd.append("--force")
    if opts.M != 200:
        cmd += ["--M", str(opts.M)]
    if opts.errors != "full":
        cmd += ["--errors", opts.errors]
    if opts.detectors:
        cmd += ["--detectors", opts.detectors]
    return cmd


def default_workspace_dir(data: str, instrument: str, today: str | None = None) -> str:
    """A suggested workspace directory next to the data: ``<data-parent>/isar_<instr>_<YYYYMMDD>``.

    Mirrors ``isar init``'s default name (``isar_<instr>_<date>``) but roots it beside the data
    rather than in the process CWD — friendlier for a mouse operator who dropped a folder. The
    wizard always passes an explicit ``--out``, so this is only the pre-filled suggestion.
    """
    today = today or f"{date.today():%Y%m%d}"
    p = Path(data)
    parent = p if p.is_dir() else p.parent
    return str(parent / f"isar_{instrument}_{today}")


def last_error_line(lines: list[str]) -> str | None:
    """The cause text from the last streamed line that starts with ``error:`` (after stripping),
    or ``None`` if no such line was seen — including when the ``error:`` prefix has no cause text
    after it, which is treated the same as no match.

    The engine prints a distinct ``error: ...`` line to stderr for each ``isar init`` exit-2 path;
    ``ProcessRunner`` merges stderr into the same line stream as stdout, so this picks the cause back
    out of the buffered lines for display in the dialog's failure message.
    """
    for line in reversed(lines):
        stripped = line.strip()
        if stripped.startswith("error:"):
            return stripped[len("error:"):].strip() or None
    return None


def parse_init_json(lines) -> dict | None:
    """The last JSON object emitted by ``isar init --json`` (a success object with ``workspace`` or
    an ``{"error": {...}}`` object), scraped from a list of streamed output lines, or ``None``.

    Tolerant like :func:`parse_detect_json`: non-JSON lines (progress text, banners) are skipped so
    a stray print never masks the real result object.
    """
    for line in reversed([str(ln).strip() for ln in lines if str(ln).strip()]):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def init_failure_cause(lines) -> str:
    """A human-readable cause for a failed ``isar init``: the JSON ``error.kind: error.message`` if
    the engine emitted one, else the last ``error:`` stderr line, else a generic fallback."""
    obj = parse_init_json(lines)
    if isinstance(obj, dict) and isinstance(obj.get("error"), dict):
        err = obj["error"]
        kind = str(err.get("kind", "")).strip()
        message = str(err.get("message", "")).strip()
        if kind and message:
            return f"{kind}: {message}"
        return message or kind or "init failed"
    return last_error_line(list(lines)) or "init failed (see log above)"


@dataclass(frozen=True)
class GuiIngestReport:
    """Pure-data mirror of the ``isar ingest --json`` success object (no isar types, no Qt).

    Named ``GuiIngestReport`` to avoid clashing with the engine's :class:`isar.session.ingest.
    IngestReport`; the GUI never imports the engine one (G16). Each ``files`` entry is a dict
    ``{"path","class","n_scans","scan_ids","n_context"}``; ``added``/``updated``/``unchanged`` are
    ``"exp:scan"`` id strings; ``deduped`` are ``{"scan","kept","superseded"}`` dicts; ``gaps`` are
    ``(exp, a, b)`` tuples.
    """

    files: tuple = ()
    added: tuple = ()
    updated: tuple = ()
    unchanged: tuple = ()
    deduped: tuple = ()
    gaps: tuple = ()


def parse_ingest_json(text) -> "GuiIngestReport | None":
    """The last ``isar ingest --json`` SUCCESS object, parsed into a :class:`GuiIngestReport`.

    Tolerant like :func:`parse_detect_json`: ``text`` may be a string or an iterable of lines; non-
    JSON lines (streamed progress/banners) are skipped so a stray print never masks the result. An
    ``{"error": {...}}`` object is NOT a success object (it has no ``files`` key) and yields ``None``
    — callers surface the failure through :func:`ingest_failure_cause` instead.
    """
    lines = text.splitlines() if isinstance(text, str) else list(text)
    for line in reversed([str(ln).strip() for ln in lines if str(ln).strip()]):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and "files" in obj:
            scans = obj.get("scans") or {}
            return GuiIngestReport(
                files=tuple(obj.get("files") or ()),
                added=tuple(scans.get("added") or ()),
                updated=tuple(scans.get("updated") or ()),
                unchanged=tuple(scans.get("unchanged") or ()),
                deduped=tuple(obj.get("deduped") or ()),
                gaps=tuple(tuple(g) for g in (obj.get("gaps") or ())),
            )
    return None


def ingest_failure_cause(lines) -> str:
    """A human cause for a failed ``isar ingest``: the JSON ``error.kind: error.message`` if the
    engine emitted one (e.g. ``no_workspace``), else the last ``error:`` stderr line, else a generic
    fallback. Mirrors :func:`init_failure_cause` for the ingest error shape."""
    obj = parse_init_json(lines)
    if isinstance(obj, dict) and isinstance(obj.get("error"), dict):
        err = obj["error"]
        kind = str(err.get("kind", "")).strip()
        message = str(err.get("message", "")).strip()
        if kind and message:
            return f"{kind}: {message}"
        return message or kind or "ingest failed"
    return last_error_line(list(lines)) or "ingest failed (see log above)"


def parse_detect_json(text: str) -> DetectResult | None:
    """Parse the JSON ``isar detect --json`` prints (the last JSON object on stdout), or ``None``.

    Tolerant: a non-JSON stream, an empty object (``{}`` = nothing detected), or a missing field all
    yield ``None`` so the wizard shows a "couldn't detect" state instead of raising.
    """
    obj = None
    for line in reversed([ln.strip() for ln in text.splitlines() if ln.strip()]):
        try:
            obj = json.loads(line)
            break
        except ValueError:
            continue
    if not isinstance(obj, dict) or not obj.get("instrument"):
        return None
    return DetectResult(
        facility=str(obj.get("facility", "")),
        instrument=str(obj["instrument"]),
        fmt=str(obj.get("fmt", "")),
        plugin_name=obj.get("plugin_name") or None,
        probe=str(obj.get("probe", "")),
    )
