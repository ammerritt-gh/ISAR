"""Choices shared by the export command and its GUI (no Qt or scientific imports)."""
from __future__ import annotations

import json
from pathlib import Path
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ExportOptions:
    spectra: bool = True
    curves: bool = True
    parameters_separate: bool = False
    parameters_combined: bool = True
    csv: bool = True
    origin: bool = False
    scale: str = "raw"
    split_sides: bool = False

    def __post_init__(self):
        for name, value in asdict(self).items():
            if name != "scale" and not isinstance(value, bool):
                raise ValueError(f"{name} must be true or false")
        if self.scale not in ("raw", "normalised"):
            raise ValueError("Choose raw counts or monitor-normalized intensity")
        if not (self.csv or self.origin):
            raise ValueError("Select at least one file format")
        if not (self.spectra or self.curves or self.parameters_separate or self.parameters_combined):
            raise ValueError("Select at least one output")

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, values):
        if not isinstance(values, dict):
            raise ValueError("Export options must be an object")
        unknown = values.keys() - cls.__dataclass_fields__.keys()
        if unknown:
            raise ValueError(f"Unknown export options: {', '.join(sorted(unknown))}")
        return cls(**values)


def check_destination(root, destination, overwrite=False) -> Path:
    """A replacement may target an export, never an experiment or an unrelated directory."""
    target = Path(destination).absolute()
    workspace = Path(root).resolve()
    if target.is_symlink() or target.resolve() == workspace or target.resolve() in workspace.parents:
        raise ValueError("Choose an export subfolder, not the experiment or one of its parents")
    if target.exists():
        if not target.is_dir():
            raise ValueError("The export folder name is already used by a file")
        found = False
        for filename, schema in (("export.json", "isar.export/2"), ("fits.json", "isar.export-fits/1")):
            path = target / filename
            if path.is_file():
                with path.open(encoding="utf-8", newline="") as stream:
                    record = json.load(stream)
                    found = isinstance(record, dict) and record.get("schema") == schema
                if found:
                    break
        if not found:
            raise ValueError("That folder is not an ISAR export. Choose a new export folder name")
        if not overwrite:
            raise FileExistsError(f"Export folder already exists: {target}")
    return target
