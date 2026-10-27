"""The BL43 resolution-curve folder: where it is, what it holds, and whether it changed.

``resolution_location`` gives the folder in force: the saved experiment setting, else the card's folder.
``check_curves`` runs at each import and run start: it discovers an ``rf/`` beside the data when no folder
is saved, identifies the folder's curves by content, and writes ``report/curves.json``. Coverage decisions
are ``isar.calibrate.tabulated``'s helpers; this module locates and identifies only. The read side
(``read_curve_check``, ``CurveIndex``) is stdlib-only and never loads a parser.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from isar.analyze.resolution import ResolutionLibrary
from isar.intake.settings import current_settings, save_resolution_folder
from isar.session.atomic import replace_retrying

log = logging.getLogger(__name__)

CURVE_CHECK = "report/curves.json"
SCHEMA = "isar.curve-check/1"
NO_FOLDER = "no resolution folder is set (save one, or keep rf/ beside the data)"
NO_PATTERN = "the card names no resolution_folder and resolution_pattern"


@dataclass(frozen=True)
class CurveFile:
    detector: str            # "a03"
    setting: str | None      # "60x60"
    file: str                # the curve file's name in the folder
    sha256: str              # 64 lowercase hex of the file's bytes


@dataclass(frozen=True)
class OtherFolder:
    path: str                # an rf/ that holds curves and is not the folder in force
    beside: str              # the data folder it was found beside
    first_seen: str          # UTC ISO-8601


@dataclass(frozen=True)
class CurveCheck:
    checked_at: str
    checked_by: str                        # "import" | "run"
    folder: str | None                     # the folder in force, absolute
    pattern: str | None
    source: str                            # "setting" | "card" | "none"
    setting_source: str | None             # "user" | "discovered" when source == "setting"
    found_beside: str | None
    library: str | None                    # sarf.fingerprint; None when the folder cannot be used
    problem: str | None                    # why the folder cannot be used; None when it can (even with 0 curves)
    curves: tuple[CurveFile, ...]          # sorted by (detector, setting)
    other_folders: tuple[OtherFolder, ...]  # sorted by path
    discovered: str | None                 # the folder THIS check saved as discovered, else None

    def index(self) -> "CurveIndex":
        return CurveIndex(self.curves)

    def to_dict(self) -> dict:
        return {"schema": SCHEMA, **asdict(self)}

    @classmethod
    def from_dict(cls, d: dict) -> "CurveCheck":
        d = dict(d)
        if d.pop("schema", None) != SCHEMA:
            raise ValueError(f"not a {SCHEMA} record")
        return cls(**{**d, "curves": tuple(CurveFile(**c) for c in d["curves"]),
                      "other_folders": tuple(OtherFolder(**o) for o in d["other_folders"])})

    def lines(self) -> list[str]:
        out = []
        if self.discovered:
            out.append(f"resolution curves: found {self.discovered} beside {self.found_beside} "
                       "and saved it as the experiment's folder")
        if self.problem:
            out.append(f"resolution curves: {self.problem}")
        else:
            n_analyzers = len({c.detector for c in self.curves})
            out.append(f"resolution curves: {len(self.curves)} curves for {n_analyzers} analyzers in {self.folder}")
        for o in self.other_folders:
            out.append(f"resolution curves: {o.path} beside {o.beside} also holds curves; "
                       f"the experiment keeps {self.folder} (change it in the GUI)")
        return out


class CurveIndex:
    """The persisted curves as a coverage source (``get`` and ``settings``, as ResolutionLibrary has them)."""

    def __init__(self, curves):
        self._curves = {(c.detector, c.setting): c for c in curves}

    def get(self, detector_id: str, setting: str | None):
        return self._curves.get((detector_id, setting))

    def settings(self) -> list[str | None]:
        return sorted({s for _, s in self._curves}, key=lambda s: (s is not None, s or ""))


@dataclass(frozen=True)
class LoadedCurves:
    library: ResolutionLibrary
    fingerprint: str | None
    problem: str | None
    identity: dict           # {(detector, setting): {"file", "sha256"}}


def _unusable(problem: str) -> LoadedCurves:
    return LoadedCurves(ResolutionLibrary(), None, problem, {})


def load_curves(folder, pattern) -> LoadedCurves:
    """The folder's library, its fingerprint and each curve's content identity; ``problem`` says why it is unusable."""
    if folder is None:
        return _unusable(NO_FOLDER)
    if not pattern:
        return _unusable(NO_PATTERN)
    from isar.parsers import sarf   # imported here: the read side never loads a parser
    try:
        fp = sarf.fingerprint(folder, pattern)
        library = sarf.library_for(folder, pattern, fp)
    except (OSError, ValueError, re.error) as exc:
        log.warning("calibration(tabulated_elastic): resolution library unavailable (%s)", exc)
        return _unusable(f"resolution library unavailable ({exc})")
    return LoadedCurves(library, fp, None, sarf.curve_identity(folder, library))


def resolution_location(card, workspace_root) -> tuple[Path | None, str | None, str]:
    """``(folder, pattern, source)``: the saved setting if any, else the card's folder (relative to the
    workspace unless absolute), else none. ``workspace_root=None`` reads no setting."""
    cfg = card.policy.calibration or {}
    pattern = cfg.get("resolution_pattern")
    if workspace_root is not None:
        saved = current_settings(workspace_root).resolution_folder
        if saved is not None:
            return Path(saved.path), pattern, "setting"
    folder = cfg.get("resolution_folder")
    if folder:
        path = Path(folder)
        if not path.is_absolute() and workspace_root is not None:
            path = Path(workspace_root) / path
        return path, pattern, "card"
    return None, pattern, "none"


def _candidates(data_paths) -> list[tuple[Path, Path]]:
    """``(data folder, its rf/)`` for each data path in order, de-duplicated by the resolved rf/ folder."""
    out: list[tuple[Path, Path]] = []
    for raw in data_paths:
        p = Path(raw)
        d = (p if p.is_dir() else p.parent).resolve()
        rf = (d / "rf").resolve()
        if all(rf != r for _d, r in out):
            out.append((d, rf))
    return out


def _holds_curves(folder: Path, pattern) -> bool:
    if not pattern or not folder.is_dir():
        return False
    from isar.parsers import sarf
    try:
        return len(sarf.load_library(folder, pattern)) > 0
    except (OSError, ValueError, re.error) as exc:
        log.warning("resolution curves: %s holds no readable curves (%s)", folder, exc)
        return False


def _previous_other_folders(root: Path) -> list[OtherFolder]:
    try:
        prev = read_curve_check(root)
    except ValueError as exc:
        log.warning("resolution curves: previous check unreadable, its other folders not kept (%s)", exc)
        return []
    return [] if prev is None else list(prev.other_folders)


def check_curves(root, card, data_paths, *, by: str) -> CurveCheck | None:
    """Discover, locate, load and identify the folder's curves; write ``report/curves.json``.

    None unless the card's method is ``tabulated_elastic``. An ``rf/`` beside the data is saved as the
    experiment's folder only when none is saved; a saved folder is never switched here.
    """
    cfg = card.policy.calibration or {}
    if cfg.get("method") != "tabulated_elastic":
        return None
    root = Path(root)
    pattern = cfg.get("resolution_pattern")
    cands = _candidates(data_paths)
    holds = {rf: _holds_curves(rf, pattern) for _d, rf in cands}
    discovered = None
    if current_settings(root).resolution_folder is None:
        for d, rf in cands:
            if holds[rf]:
                res = save_resolution_folder(root, rf, source="discovered", found_beside=str(d))
                if res.ok:
                    discovered = res.value
                else:
                    log.warning("resolution curves: %s not saved as the experiment's folder (%s)", rf, res.reason)
                break
    folder, _pattern, source = resolution_location(card, root)
    saved = current_settings(root).resolution_folder
    loaded = load_curves(folder, pattern)
    problem = loaded.problem
    if folder is None:
        beside = ", ".join(dict.fromkeys(str(d) for d, _rf in cands))
        problem = f"no resolution folder is saved, and no rf/ folder holding curves was found beside {beside}"

    in_force = None if folder is None else folder.resolve()
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    kept = {o.path: o for o in _previous_other_folders(root) if o.path != str(in_force)}
    for d, rf in cands:
        if holds[rf] and rf != in_force:
            prior = kept.get(str(rf))
            kept[str(rf)] = OtherFolder(str(rf), str(d), prior.first_seen if prior else now)

    curves = tuple(CurveFile(det, setting, v["file"], v["sha256"]) for (det, setting), v in
                   sorted(loaded.identity.items(), key=lambda kv: (kv[0][0], kv[0][1] is not None, kv[0][1] or "")))
    from_setting = source == "setting" and saved is not None
    check = CurveCheck(
        checked_at=now, checked_by=by,
        folder=None if folder is None else str(folder), pattern=pattern, source=source,
        setting_source=saved.source if from_setting else None,
        found_beside=saved.found_beside if from_setting else None,
        library=loaded.fingerprint, problem=problem, curves=curves,
        other_folders=tuple(sorted(kept.values(), key=lambda o: o.path)), discovered=discovered)
    _write(root, check)
    return check


def _write(root: Path, check: CurveCheck) -> None:
    path = root / CURVE_CHECK
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(check.to_dict(), indent=2, sort_keys=True, allow_nan=False) + "\n",
                   encoding="utf-8", newline="\n")
    replace_retrying(tmp, path)


def read_curve_check(root) -> CurveCheck | None:
    """The last written check; None when never checked. ValueError when the file is malformed."""
    path = Path(root) / CURVE_CHECK
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        return CurveCheck.from_dict(json.loads(text))
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"{path} is not a curve check ({exc})") from exc
