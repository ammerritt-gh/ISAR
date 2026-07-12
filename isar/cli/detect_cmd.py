"""`isar detect <data>` — report the instrument ISAR detects from a data path.

The GUI's New Experiment wizard shells this (``--json``) to preview the instrument before creating a
workspace, so it never imports the parser registry itself (G16). Detection reuses ``isar init``'s
own ``_first_recognizable`` + the registry, so the preview matches exactly what ``isar init`` would
scaffold — one detector, no drift.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from isar.cli import init_cmd
from isar.parsers import registry


def run(args) -> int:
    data = Path(args.data)
    as_json = getattr(args, "json", False)
    if not data.exists():
        if as_json:
            print("{}")
        else:
            print(f"error: no such path: {data}", file=sys.stderr)
        return 2

    probe, detected = init_cmd._first_recognizable(data)
    if detected is None:
        if as_json:
            print("{}")
        else:
            print(f"could not detect an instrument from {data} (no recognizable data file found)",
                  file=sys.stderr)
        return 2

    facility, instrument, fmt = detected
    plugin_name = registry.default_plugin_name(facility, instrument, fmt)
    if as_json:
        print(json.dumps({"facility": facility, "instrument": instrument, "fmt": fmt,
                          "plugin_name": plugin_name, "probe": probe.name}))
    else:
        supported = "" if plugin_name else "  (no card template ships for it yet)"
        print(f"Detected {facility}/{instrument} (format {fmt}) from {probe.name}{supported}")
    return 0
