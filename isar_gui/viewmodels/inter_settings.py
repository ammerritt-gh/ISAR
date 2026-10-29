"""The rows and deltas of the Inter-spectra fitting settings dialog (S1U10). Qt-free; no fit logic.

The caller passes the settings table (``SETTING_DEFS``) in: the GUI's import contract admits the read
facade and the intake modules, not the analysis settings module. A save sends deltas only: each value
the user changed, and ``None`` where it goes back to its default (hard rule 7: the GUI never sends a
snapshot).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from isar_gui.inter_words import CALIBRATED_SYNTHETIC, UNCALIBRATED

# GUI wording per shown setting (hidden settings are never shown, so they have no label).
LABELS = {
    "q_tolerance_meV": "q tolerance",
    "t_tolerance_meV": "T tolerance",
    "flag_threshold": "Flag threshold",
    "shape_family": "Penalty shape",
    "step_exponent": "Temperature step exponent",
    "sweep_tol_meV": "Sweep tolerance",
    "sweep_cap": "Sweep cap",
}


@dataclass(frozen=True)
class Row:
    setting: Any            # one SETTING_DEFS entry (duck-typed: name, group, kind, default, unit, calibrated, help)
    label: str
    value: object           # the effective value now, defaults filled

    @property
    def mark(self) -> str:
        return CALIBRATED_SYNTHETIC if self.setting.calibrated else UNCALIBRATED


def rows(defs: Iterable[Any], values: Mapping[str, object]) -> tuple[Row, ...]:
    """The shown (not hidden) settings in table order, each with its effective value."""
    return tuple(Row(d, LABELS[d.name], values.get(d.name, d.default)) for d in defs if d.group != "hidden")


def _show(value: object) -> str:
    return f"{value:g}" if isinstance(value, float) else str(value)


def default_text(row: Row) -> str:
    unit = f" {row.setting.unit}" if row.setting.unit else ""
    return f"Default: {_show(row.setting.default)}{unit} · {row.mark}"


def changes(shown: Iterable[Row], edited: Mapping[str, object]) -> dict[str, object | None]:
    """The deltas to save: each edited value that differs from the effective one; ``None`` at the default."""
    by_name = {r.setting.name: r for r in shown}
    out: dict[str, object | None] = {}
    for name, value in edited.items():
        row = by_name[name]
        if value != row.value:
            out[name] = None if value == row.setting.default else value
    return out
