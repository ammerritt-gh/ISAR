"""`isar init <data>` — detect the instrument and scaffold an experiment workspace (the retired unification plan §5, git history; current contract: docs/WORKSPACE_AND_CLI.md).

Copies the matching card template **verbatim as text** (its comments are the documentation — no
tomllib round-trip), appends commented `[calibration]`/`[fitting]` scaffolds if the template
predates them, and writes empty state + session files. Prints the exact next command.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

from isar.parsers import registry
from isar.session.state import SessionState


def _fail(kind: str, message: str, as_json: bool) -> int:
    """Emit an exit-2 error: the JSON error object on stdout (``--json`` only) plus the unchanged
    ``error: <message>`` stderr line. ``message`` is exactly the stderr line's text (sans prefix)."""
    if as_json:
        print(json.dumps({"error": {"kind": kind, "message": message}}))
    print(f"error: {message}", file=sys.stderr)
    return 2

# Appended (as comments) only when the template lacks the section, so `isar init` always shows the
# user where guidelines and calibration policy live. Real cards carry uncommented versions.
_FITTING_SCAFFOLD = """
# ---------------------------------------------------------------------------
# [fitting] — your fitting guidelines (the retired unification plan §6, git history; current contract: docs/WORKSPACE_AND_CLI.md). All optional.
# [fitting]
# detectors = "enabled"        # or ["ana7"] — which spectra to fit
# omega_window = [0.0, 0.0]    # sanity window (meV); [0,0] = off
# monitor_frac = 0.9           # mask points whose monitor counted short (monitor mode)
# min_fit_points = 6           # undersampled guard
#
# [[fitting.expectations]]     # expected dispersion — seeds ICs; beats the auto prior
# q = {component = "k", min = 0.10, max = 0.35}
# omega0 = 5.2
# omega0_sigma = 0.5
#
# [[fitting.overrides]]        # per-scan control
# scan = 231
# exclude = true               # never fitted; listed in the report with reason
"""

_CALIBRATION_SCAFFOLD = """
# ---------------------------------------------------------------------------
# [calibration] — how the energy zero / resolution is established (the retired unification plan §3, git history; current contract: docs/WORKSPACE_AND_CLI.md).
# method = "bragg_companion" | "elastic_records" | "cooper_nathans"
# [calibration]
# method = "cooper_nathans"
"""


def _first_recognizable(path: Path):
    """A file `registry.detect` can classify: the file itself, or the first such in a directory."""
    candidates = [path] if path.is_file() else (
        sorted(f for f in path.rglob("*") if f.is_file()) if path.is_dir() else []
    )
    for f in candidates:
        # .json is NOT skipped: TAVI simulator scans are JSON and detect()
        # recognizes them structurally; unrelated JSON raises and is skipped.
        if f.suffix.lower() in {".nxs", ".log", ".png", ".tmp", ".unused"}:
            continue
        try:
            return f, registry.detect(f)
        except (ValueError, OSError):
            continue
    return None, None


def _has_section(text: str, name: str) -> bool:
    return any(line.strip().startswith(f"[{name}]") for line in text.splitlines())


def run(args) -> int:
    as_json = getattr(args, "json", False)
    data = Path(args.data)
    if not data.exists():
        return _fail("bad_path", f"no such path: {data}", as_json)

    probe, detected = _first_recognizable(data)
    if detected is None:
        return _fail("no_recognizable_data",
                     f"could not detect an instrument from {data} "
                     f"(no recognizable data file found)", as_json)
    facility, instrument, fmt = detected
    plugin_name = registry.default_plugin_name(facility, instrument, fmt)
    if plugin_name is None:
        return _fail("no_card_template",
                     f"detected {facility}/{instrument} ({fmt}) has no shipped card template yet; "
                     f"author a card + parser (see docs/ARCHITECTURE.md §6).", as_json)

    plugin = registry.plugin_by_name(plugin_name)
    template_text = Path(plugin._CARD_PATH).read_text(encoding="utf-8")
    if not _has_section(template_text, "calibration"):
        template_text += _CALIBRATION_SCAFFOLD
    if not _has_section(template_text, "fitting"):
        template_text += _FITTING_SCAFFOLD

    out = Path(args.out) if args.out else Path.cwd() / f"isar_{instrument}_{date.today():%Y%m%d}"
    if out.exists() and (out / "card.policy.toml").exists():
        return _fail("card_exists",
                     f"workspace already has a card: {out / 'card.policy.toml'} "
                     f"(remove it or choose another --out)", as_json)
    out.mkdir(parents=True, exist_ok=True)
    (out / "card.policy.toml").write_text(template_text, encoding="utf-8")
    SessionState().save(out / "session.json")
    # Empty machine state — written so the workspace is complete and inspectable.
    (out / "card.state.json").write_text("{}", encoding="utf-8")

    if as_json:
        print(json.dumps({"workspace": str(out), "facility": facility, "instrument": instrument,
                          "fmt": fmt, "plugin": plugin_name}))
        return 0

    print(f"Detected {facility}/{instrument} (format {fmt}) from {probe.name}")
    print(f"Created workspace: {out}")
    print(f"  card.policy.toml   <- template for '{plugin_name}' (edit: detectors, [fitting], [calibration])")
    print(f"  card.state.json, session.json  (empty)")
    print()
    print("Next:")
    print(f'  1. edit "{out / "card.policy.toml"}"  (confirm detectors; add expectations / known-bad scans)')
    print(f'  2. isar run "{data}" --workspace "{out}"')
    return 0
