"""The committed BL43LXU resolution curves (``resolution_curves/SP8 BL43LXU/``).

Verbatim copies of the le Tacon beamtime's SARF curves, the one measured data the repository carries
(AGENTS.md hard rule 10's exception). They are read by the same reader as the real ``rf/`` folder,
with the card's own ``resolution_pattern``, so a test that needs a real curve gets the real reader.
Qt-free.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

from isar.analyze.resolution import TabulatedResolution
from isar.parsers import sarf

CURVES_DIR = Path(__file__).resolve().parents[2] / "resolution_curves" / "SP8 BL43LXU"
CARD = Path(__file__).resolve().parents[1] / "parsers" / "cards" / "bl43lxu.policy.toml"
SETTING = "40x40"  # the slit both le Tacon files use (gpi_files: sarf_24Nov10_s50_40_40)
# shortcut: the folder is found from this file's checkout path, so an installed wheel has no curves;
# upgrade by packaging the folder as package data when a wheel must read them.


def library():
    with CARD.open("rb") as f:
        pattern = tomllib.load(f)["calibration"]["resolution_pattern"]
    return sarf.library_for(CURVES_DIR, pattern)


def curve(analyzer: str = "a06", setting: str = SETTING) -> TabulatedResolution:
    found = library().get(analyzer, setting)
    if found is None:
        raise KeyError(f"no committed BL43LXU resolution curve for {analyzer} at {setting}")
    return found
