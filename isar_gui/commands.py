"""Qt-free, ISAR-free command assembly for the operator on-ramp.

The Files-dock bootstrap flow (drop data → ``isar init`` + ``isar ingest``) and the Run action
*shell the same engine* (``isar init`` / ``isar run`` / ``isar ingest`` / ``isar detect``) rather than
import it — the GUI is a thin front door onto the ``isar`` CLI, and the G16 import contract forbids
``isar_gui`` from importing engine internals (cli, parsers, …). So this module builds argument vectors
and parses the JSON the engine prints; it imports neither Qt nor ``isar``, which keeps it trivially
unit-testable and boundary-clean.

Instrument detection is a subprocess (``isar detect --json``) whose output :func:`parse_detect_json`
turns into a :class:`DetectResult`. It has no production caller today (the New-Experiment wizard that
drove it was removed in favour of the drop-onto-Files-dock bootstrap); it is retained for a planned
detect-preview in the Files dock — one detector, no drift, exactly what ``isar init`` will scaffold.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# A Python exception's final traceback line: "ValueError: bad", "isar.foo.MyError: x", etc. Matches a
# dotted-qualified type name ending in Error/Exception, followed by an optional ": message".
_EXC_LINE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception))\b(.*)$")


@dataclass(frozen=True)
class RunOptions:
    """The subset of ``isar run`` knobs the on-ramp exposes. Defaults mirror the CLI defaults, so a
    default ``RunOptions`` adds no flags to the command (``build_run_command`` stays minimal)."""

    llm: bool = False
    force: bool = False
    M: int = 200
    errors: str = "fast"          # fast | full | cheap | bayes (the CLI default; omitted from the command)
    detectors: str | None = None  # comma-separated detector ids, or None for the card default


@dataclass(frozen=True)
class DetectResult:
    """What ``isar detect`` reported for a data path. Pure data — no isar types.

    Retained (with :func:`build_detect_command` / :func:`parse_detect_json` / :func:`default_workspace_dir`)
    for a planned detect-preview in the Files dock; it lost its production caller when the New-Experiment
    wizard was replaced by the drop-onto-Files-dock bootstrap. Deliberately kept, not dead code."""

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

    Parses data into the ledger (status ``ingested``) without fitting. Used by
    :class:`~isar_gui.ingest_controller.IngestController` for the ingest step of the bootstrap
    sequence and for a drop/watched-folder ingest onto an already-ready workspace.
    """
    cmd = [python, "-m", "isar", "ingest", *[str(p) for p in data_paths],
           "--workspace", str(workspace)]
    if json:
        cmd.append("--json")
    return cmd


def build_export_command(python: str, workspace: str) -> list[str]:
    """The argv for ``isar export --workspace <ws> --json``: stored results only, no refit."""
    return [python, "-m", "isar", "export", "--workspace", str(workspace), "--json"]


def build_detect_command(python: str, data: str) -> list[str]:
    """The argv for ``isar detect <data> --json``. Retained for a planned Files-dock detect-preview
    (no production caller today — see :class:`DetectResult`)."""
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
    if opts.errors != "fast":
        cmd += ["--errors", opts.errors]
    if opts.detectors:
        cmd += ["--detectors", opts.detectors]
    return cmd


@dataclass(frozen=True)
class DriveOptions:
    """The subset of ``isar drive`` knobs the Drive dock exposes. Defaults mirror the CLI defaults, so
    a default option adds no flag to the command (``build_drive_command`` stays minimal).

    Exactly one campaign *source* must be chosen — a natural-language ``goal``, a
    ``program_path`` (``isar.campaign-program/1`` JSON authoring input), a
    ``compiled_program_path`` (the program ``--compile-only`` just wrote, whose objective the
    operator confirmed), or ``resume_latest`` (rebuild the frozen objective from the workspace
    session log). They are mutually exclusive; :func:`build_drive_command` enforces that (matching
    the CLI's ``required=True`` mutually-exclusive group). The two program fields both emit
    ``--program``; they differ only in whether the GUI still treats tier and budget as editable.
    """

    goal: str | None = None
    program_path: str | None = None
    compiled_program_path: str | None = None
    resume_latest: bool = False
    tier: str = "deterministic"       # deterministic | mcstas
    budget: str | None = None         # '<amount><unit>' e.g. '8h', '1e10n'; None => from the program
    analysis_limit: str | None = None # duration or 'none'; None => tier default/frozen objective
    seed: int = 0
    tavi_url: str | None = None
    calibration: str | None = None
    worker: bool = True               # False => --no-worker (fresh subprocess per iteration)
    control_token: str | None = None
    # None => inherit the compiler default (the flag is OMITTED); "off" / "authoritative" are
    # explicit operator commands and ALWAYS emit --scan-window-eig-mode, even when the value
    # happens to equal what the compiler would have picked anyway (never ignore a user command).


def build_drive_command(python: str, workspace: str, opts: DriveOptions) -> list[str]:
    """The argv for ``isar drive --<source> --workspace <ws> [opts]`` run as ``<python> -m isar ...``.

    Validates (raising ``ValueError`` — unit-testable without Qt) that exactly ONE campaign source is
    set (``goal`` / ``program_path`` / ``compiled_program_path`` / ``resume_latest``), matching the
    CLI's mutually-exclusive source group. Only non-default options add flags, so the command stays legible in the log dock.
    """
    sources = [opts.goal is not None, opts.program_path is not None,
               opts.compiled_program_path is not None, opts.resume_latest]
    n_sources = sum(1 for s in sources if s)
    if n_sources != 1:
        raise ValueError(
            "exactly one campaign source must be set (goal, program_path, compiled_program_path, "
            f"or resume_latest); got {n_sources}")

    cmd = [python, "-m", "isar", "drive"]
    if opts.goal is not None:
        cmd += ["--goal", opts.goal]
    elif opts.program_path is not None:
        cmd += ["--program", str(opts.program_path)]
    elif opts.compiled_program_path is not None:
        cmd += ["--program", str(opts.compiled_program_path)]
    else:
        cmd.append("--resume-latest")
    cmd += ["--workspace", str(workspace)]
    # Tier and budget are frozen in a confirmed compiled program. Resume keeps its established tier
    # but may still receive an explicit budget override for the new invocation.
    if opts.tier != "deterministic" and not (opts.resume_latest or opts.compiled_program_path):
        cmd += ["--tier", opts.tier]
    if opts.budget:
        cmd += ["--budget", opts.budget]
    if opts.analysis_limit:
        cmd += ["--analysis-limit", opts.analysis_limit]
    if opts.calibration:
        cmd += ["--calibration", str(opts.calibration)]
    if opts.tavi_url:
        cmd += ["--tavi", opts.tavi_url]
    if opts.seed != 0:
        cmd += ["--seed", str(opts.seed)]
    if opts.control_token:
        cmd += ["--control-token", opts.control_token]
    if not opts.worker:
        cmd.append("--no-worker")
    return cmd


def build_compile_command(python: str, opts: DriveOptions, output_path: str, *,
                          workspace: str | None = None) -> list[str]:
    """Compile goal/program authoring input against a ready workspace without TAVI contact."""
    if (opts.goal is None) == (opts.program_path is None):
        raise ValueError("compile needs exactly one of goal or program_path")
    cmd = [python, "-m", "isar", "drive"]
    cmd += (["--goal", opts.goal] if opts.goal is not None
            else ["--program", str(opts.program_path)])
    if workspace:
        cmd += ["--workspace", str(workspace)]
    cmd += ["--compile-only", str(output_path)]
    if opts.tier != "deterministic":
        cmd += ["--tier", opts.tier]
    if opts.budget:
        cmd += ["--budget", opts.budget]
    if opts.analysis_limit:
        cmd += ["--analysis-limit", opts.analysis_limit]
    return cmd


def source_fingerprint(*, goal: str | None = None, program_path: str | None = None) -> str:
    """The sha256 hexdigest identifying a campaign source, for Resume-vs-Start comparison in the dock.

    Exactly one of ``goal`` / ``program_path`` must be given (``ValueError`` otherwise). For a goal
    it is the sha256 of the goal TEXT encoded utf-8. For a program it is the ``source.sha256``
    embedded in the file when the program carries one (a ``--compile-only`` program keeps the
    fingerprint of the authoring input it was compiled from), otherwise the sha256 of the file's
    raw BYTES (never decoded — a program with non-ASCII content hashes its bytes, not its text).

    CONTRACT: this must match the ``source.sha256`` the CLI freezes into the ``campaign_start`` event
    byte-for-byte (``isar/cli/main.py``: ``sha256(goal.encode("utf-8"))`` for a goal; for a program
    ``dict(program.source) or {"kind": "program", "sha256": sha256(read_bytes())}`` — the embedded
    block wins whenever it is non-empty, and only a source-less hand-written program is identified
    by its own bytes). The dock compares this against a resumed campaign's stored fingerprint to
    tell the operator whether a Resume would continue the same objective or a Start would launch a
    different one; a drift here silently breaks that comparison.

    A compiled program whose embedded ``sha256`` is absent or not a 64-hex digest falls back to the
    file-bytes hash. The CLI stores ``None`` in that case, so no candidate can ever equal it: the
    comparison degrades to "different", which is the safe direction (offer Start, not Resume).
    """
    if (goal is None) == (program_path is None):
        raise ValueError("exactly one of goal or program_path must be given")
    if goal is not None:
        return hashlib.sha256(goal.encode("utf-8")).hexdigest()
    raw = Path(program_path).read_bytes()
    try:
        document = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        document = None
    if isinstance(document, dict) and isinstance(document.get("source"), dict) and document["source"]:
        embedded = document["source"].get("sha256")
        if isinstance(embedded, str) and re.fullmatch(r"[0-9a-f]{64}", embedded):
            return embedded
    return hashlib.sha256(raw).hexdigest()


def default_workspace_dir(data: str, instrument: str, today: str | None = None) -> str:
    """A suggested workspace directory next to the data: ``<data-parent>/isar_<instr>_<YYYYMMDD>``.

    Mirrors ``isar init``'s default name (``isar_<instr>_<date>``) but roots it beside the data
    rather than in the process CWD — friendlier for a mouse operator who dropped a folder. Retained
    for a planned Files-dock detect-preview (no production caller today — see :class:`DetectResult`).
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


def last_exception_line(lines) -> str | None:
    """The last line that looks like a Python exception's traceback tail
    (``ValueError: could not convert string to float: 'da3p'``), or ``None``.

    A subprocess that dies with a bare traceback (no ``error:`` line, no JSON) would otherwise fall
    through to the generic "see log above". This lifts the actual exception line out of the buffered
    output so the operator sees the real cause. ``error:``-prefixed lines are left to
    :func:`last_error_line`; only unqualified exception lines are matched here."""
    for line in reversed([str(ln).strip() for ln in lines if str(ln).strip()]):
        if line.startswith("error:"):
            continue
        if _EXC_LINE.match(line):
            return line
    return None


def parse_last_json_object(lines) -> dict | None:
    """The last JSON *object* on a stream of output lines, or ``None``.

    Shared by both ``isar init`` and ``isar ingest`` result parsing: each emits its result (a success
    object, or an ``{"error": {...}}`` object) as the last JSON line on stdout. Tolerant like
    :func:`parse_detect_json` — non-JSON lines (progress text, banners) are skipped so a stray print
    never masks the real result object.
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
    obj = parse_last_json_object(lines)
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
    last_ingest: dict | None = None
    import_record_error: str | None = None      # BL43: the import record was not written


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
                last_ingest=(dict(obj["last_ingest"])
                             if isinstance(obj.get("last_ingest"), dict) else None),
                import_record_error=obj.get("import_record_error") or None,
            )
    return None


def ingest_failure_cause(lines) -> str:
    """A human cause for a failed ``isar ingest``: the JSON ``error.kind: error.message`` if the
    engine emitted one (e.g. ``no_workspace``), else the last ``error:`` stderr line, else a generic
    fallback. Mirrors :func:`init_failure_cause` for the ingest error shape."""
    obj = parse_last_json_object(lines)
    if isinstance(obj, dict) and isinstance(obj.get("error"), dict):
        err = obj["error"]
        kind = str(err.get("kind", "")).strip()
        message = str(err.get("message", "")).strip()
        if kind and message:
            return f"{kind}: {message}"
        return message or kind or "ingest failed"
    lines = list(lines)
    return (last_error_line(lines)
            or last_exception_line(lines)
            or "ingest failed (see log above)")


def parse_detect_json(text: str) -> DetectResult | None:
    """Parse the JSON ``isar detect --json`` prints (the last JSON object on stdout), or ``None``.

    Tolerant: a non-JSON stream, an empty object (``{}`` = nothing detected), or a missing field all
    yield ``None`` so the caller shows a "couldn't detect" state instead of raising.
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
