"""BL43LXU SARF resolution files: the catalogue entry's reader for the beamline's measured curves.

A SARF file holds one analyzer's measured resolution at one analyzer-slit setting: comment lines
(`#`), then energy (meV) and the smooth SARF curve, then the fit to the measurement, which is
ignored. A `.gpi` file beside it summarises the measurement (widths at half, tenth and hundredth
maximum); its values ride on the curve as facts. The `.lines` file is never read.

`load_library(folder, pattern)` builds the shared `ResolutionLibrary` (keyed by detector and
setting) from every file whose whole name matches the card's `resolution_pattern`, a regular
expression with the groups `analyzer` and, for a setting-dependent resolution, `setting`. A setting
is labelled by `metadata.slit_label`, the same spelling the reader gives the `slit` fact, so a
declared slit and a curve file of the same opening always meet. An unreadable file is logged and
left out; it never stops the library. `load_named(folder, pattern, name)` holds only the set a gpi
file names (`<name>_<analyzer>`), which the slit lookup's first-wins rule could otherwise hide.
"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

import numpy as np

from isar.analyze.resolution import ResolutionLibrary, TabulatedResolution, read_table
from isar.parsers import metadata
from isar.parsers.sp8_bl43 import detector_id

log = logging.getLogger(__name__)

GPI_KEYS = ("res_fwhm", "res_fwtm", "res_fwm100")
_GPI_LINE = re.compile(r"^\s*(\w+)\s*=\s*([-+0-9.eE]+)\s*$")
# One library per (folder, pattern) per process; refreshed when the files' fingerprint differs.
_LIBRARIES: dict = {}
# One named-set library per (folder, pattern, name) per process; refreshed when the fingerprint differs.
_NAMED: dict = {}


def _matches(folder: Path, pattern: str) -> list[tuple[Path, str, str | None]]:
    """``(path, detector_id, setting)`` for every file of `folder` the pattern matches in full."""
    rx = re.compile(pattern)
    if "analyzer" not in rx.groupindex:
        raise ValueError(f"resolution_pattern {pattern!r} must name an (?P<analyzer>...) group")
    out = []
    for path in sorted(p for p in folder.iterdir() if p.is_file()):
        m = rx.fullmatch(path.name)
        if m is None:
            continue
        setting = m.groupdict().get("setting")
        if setting is not None:
            setting = metadata.normalise_slit(setting)
            if setting is None:
                log.warning("sarf: %s: setting is not an opening of two numbers; left out", path.name)
                continue
        digits = re.sub(r"\D", "", m.group("analyzer"))
        if not digits:
            log.warning("sarf: %s: no analyzer number in %r; left out", path.name, m.group("analyzer"))
            continue
        out.append((path, detector_id(int(digits)), setting))
    return out


def fingerprint(folder, pattern: str) -> str:
    """A short hash of the folder's absolute path, the pattern, and each matched file's name, size, modification
    time and content (and its `.gpi`): a moved folder, or a curve replaced in place with its size and mtime
    kept, is a different library to every calibration and fit that recorded it."""
    folder = Path(folder)
    rows = []
    for path, _det, _setting in _matches(folder, pattern):
        for p in (path, path.with_name(path.name + ".gpi")):
            if p.is_file():
                try:
                    st = p.stat()
                    rows.append((p.name, st.st_size, st.st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest()))
                except OSError as exc:       # one locked file must not hold every analyzer; marked, so it changes back
                    log.warning("sarf: %s unreadable for the fingerprint (%s); its row is marked", p.name, exc)
                    rows.append((p.name, None, None, "unreadable"))
    return hashlib.sha256(repr((str(folder.resolve()), pattern, sorted(rows))).encode("utf-8")).hexdigest()[:16]


def curve_identity(folder, library) -> dict:
    """``{(detector, setting): {"file", "sha256"}}`` for each curve of ``library``, read from ``folder``: the
    bytes the curve was loaded from. A curve whose file cannot be read is logged and left out."""
    out = {}
    for curve in library:
        path = Path(folder) / curve.source
        try:
            out[(curve.detector_id, curve.setting)] = {"file": path.name,
                                                       "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        except OSError as exc:
            log.warning("sarf: %s unreadable for its identity (%s); left out", path.name, exc)
    return out


def _gpi_facts(path: Path) -> dict:
    gpi = path.with_name(path.name + ".gpi")
    if not gpi.is_file():
        return {}
    facts = {}
    for line in gpi.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _GPI_LINE.match(line)
        if m and m.group(1) in GPI_KEYS:
            facts[m.group(1)] = {"value": float(m.group(2)), "source": "sidecar"}
    return facts


def read_curve(path, detector: str, setting: str | None) -> TabulatedResolution:
    """One SARF file as a `TabulatedResolution`; ValueError/OSError when it cannot be read."""
    return read_table(path, detector, setting, facts=_gpi_facts(Path(path)))


def load_library(folder, pattern: str) -> ResolutionLibrary:
    """Every readable curve of `folder` matching `pattern`. FileNotFoundError when there is no folder."""
    folder = Path(folder)
    if not folder.is_dir():
        raise FileNotFoundError(f"resolution folder {folder} not found")
    return _library(_matches(folder, pattern))


def _named_matches(folder: Path, pattern: str, name: str) -> list[tuple[Path, str, str | None]]:
    """The matches whose file name is exactly ``<name>_<analyzer>`` (the analyzer as the pattern captures it)."""
    rx = re.compile(pattern)
    prefix = f"{name}_"
    return [(path, det, setting) for path, det, setting in _matches(folder, pattern)
            if path.name.startswith(prefix) and rx.fullmatch(path.name).group("analyzer") == path.name[len(prefix):]]


def _load_named(folder: Path, pattern: str, name: str, fp: str) -> ResolutionLibrary:
    key = (str(folder), pattern, name)
    hit = _NAMED.get(key)
    if hit is None or hit[0] != fp:
        _NAMED[key] = hit = (fp, _library(_named_matches(folder, pattern, name)))
    return hit[1]


def load_named(folder, pattern: str, name: str, fp: str | None = None) -> ResolutionLibrary:
    """Only the curves of the named set (a gpi file's ``sarf_name``): files named exactly ``<name>_<analyzer>``,
    keyed (detector, setting) as ``load_library``, so another set at the same slit never hides them.
    Loaded once per process per state of the folder. ``fp``: the folder's fingerprint from the caller's own
    snapshot, so no second hash of every file. FileNotFoundError when there is no folder."""
    folder = Path(folder)
    if not folder.is_dir():
        raise FileNotFoundError(f"resolution folder {folder} not found")
    return _load_named(folder, pattern, name, fp or fingerprint(folder, pattern))


def _library(matches) -> ResolutionLibrary:
    curves, seen = [], {}
    for path, det, setting in matches:
        if (det, setting) in seen:
            log.warning("sarf: %s and %s both give %s at %s; the first is used", seen[(det, setting)],
                        path.name, det, setting)
            continue
        try:
            curves.append(read_curve(path, det, setting))
        except (OSError, ValueError) as exc:
            log.warning("sarf: %s unreadable (%s); left out of the library", path.name, exc)
            continue
        seen[(det, setting)] = path.name
    return ResolutionLibrary(curves)


def library_for(folder, pattern: str, expected: str | None = None) -> ResolutionLibrary:
    """The library of `folder`, loaded once per process. With `expected` (the fingerprint a calibration
    or a fit was made under), ValueError when the files have changed since: a different curve would
    silently turn a recorded result into a different one."""
    key = (str(Path(folder)), pattern)
    hit = _LIBRARIES.get(key)
    if hit is None or (expected is not None and hit[0] != expected):
        _LIBRARIES[key] = hit = (fingerprint(folder, pattern), load_library(folder, pattern))
    if expected is not None and hit[0] != expected:
        raise ValueError(f"the resolution files in {folder} changed since they were last read")
    return hit[1]


def curve_from_reference(ref: dict) -> TabulatedResolution:
    """The curve a fit was made with, from its recorded reference (folder, pattern, fingerprint,
    detector, setting; ``named`` when the curve was chosen by a gpi file's set name). ValueError when the
    files changed or the curve is gone."""
    if ref.get("named"):
        folder = Path(ref["folder"])
        if not folder.is_dir():
            raise FileNotFoundError(f"resolution folder {folder} not found")
        fp = fingerprint(folder, ref["pattern"])
        if ref.get("fingerprint") is not None and fp != ref["fingerprint"]:
            raise ValueError(f"the resolution files in {folder} changed since they were last read")
        lib = _load_named(folder, ref["pattern"], ref["named"], fp)
    else:
        lib = library_for(ref["folder"], ref["pattern"], ref.get("fingerprint"))
    curve = lib.get(ref["detector"], ref["setting"])
    if curve is None:
        raise ValueError(f"no resolution curve for {ref['detector']} at {ref['setting']} in {ref['folder']}")
    return curve
