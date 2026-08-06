#!/usr/bin/env python
"""Generate and promote the declared validation evidence into ``docs/assets/``.

The default command owns report generation. It requires the unpublished ``test_data/`` tree,
fingerprints it around every generator invocation, stages the complete prospective bundle, and
only then replaces the committed groups and manifest. Add ``--check`` to validate the committed
bundle without generating or copying anything.

Security boundary: bundle declarations and manifest paths are treated as untrusted path input.
They must be portable, Windows-safe relative paths, remain inside their declared roots, and name
plain files/directories (never links, reparse points, or special files). Sources are revalidated
immediately before copying. A local process with write access can still race the final validation
and copy; eliminating that TOCTOU window would require OS-specific handle-relative file APIs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import string
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "out"
ASSETS = REPO / "docs" / "assets"
MANIFEST = ASSETS / "MANIFEST.json"
TEST_DATA = REPO / "test_data"

SCHEMA_VERSION = 2
REPARSE_POINT = 0x400
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
WINDOWS_INVALID_CHARS = frozenset('<>"|?*')

# Declared rather than globbed: only evidence cited by a maintained document is promoted.
BUNDLE = [
    {
        "name": "phase3_gold",
        "source": "phase3_gold",
        "command": "python docs/phase3_gold_check.py",
        "what": "83-scan S30 gold set scored against the curated FeSe_1 human fits — the headline "
                "real-data check.",
        "replaces": [
            "README.md — 'Current evidence: out/phase3_gold/FINDINGS.md'",
            "docs/READING_GUIDE.md — 'the headline real-data check is the gold report'",
            "docs/READING_GUIDE.md — 'Does it agree with a human on real data?'",
        ],
        "files": [
            "FINDINGS.md", "results_gold.csv", "figures/confusion.png",
            "figures/omega0_scatter.png", "figures/omega0_pull_hist.png",
            "figures/confidence_calibration.png", "figures/audit.png",
            "figures/resolution_overlay.png",
        ],
    },
    {
        "name": "in8_phase1",
        "source": "in8_phase1",
        "command": "python docs/in8_phase1_check.py",
        "what": "The same fitting core on a triple-axis instrument (ILL IN8), with a real S30 IXS "
                "positive control for contrast.",
        "replaces": [
            "docs/READING_GUIDE.md — 'Does the same core work on another instrument?'",
            "README.md — 'out/in8_phase1/FINDINGS.md (same core on a triple-axis instrument)'",
        ],
        "files": [
            "FINDINGS.md", "results_in8.csv", "results_in8_merged.csv", "results_s30.csv",
            "figures/055181.png", "figures/055182.png", "figures/055289.png",
            "figures/s30_scan168_ana5.png",
        ],
    },
    {
        "name": "phase3_step1",
        "source": "phase3_step1",
        "command": "python docs/phase3_step1_check.py",
        "what": "The structure-hypothesis loop on six curated S30 control scans, reproducing the "
                "frozen Phase-1 fitter, plus the error and model-selection layers.",
        "replaces": [
            "docs/READING_GUIDE.md — 'Does the engine reproduce the trusted fitter + behave?'",
            "docs/WORKED_EXAMPLE.md — the five figures it walks through",
        ],
        "files": [
            "FINDINGS.md", "results_step1.csv", "figures/energy_zero_series.png",
            "figures/s30_scan168_ana5.png", "figures/bootstrap_omega0_histograms.png",
            "figures/selection_akaike_weights.png", "figures/verdict_confidence.png",
            "figures/dispersion_summary.png", "figures/synthetic_fork.png",
        ],
    },
    {
        "name": "stat2b",
        "source": "stat2b_evidence",
        "command": "python tools/docs/export_stat2b_evidence.py",
        "what": "Corrected STAT-2b.1 development evidence, delivery-separation comparison, and "
                "the 4/8/16/24/28-worker scaling preflight.",
        "replaces": [
            "docs/plans/STAT2_SELECTION_CONSISTENCY_PLAN.md — corrected STAT-2b.1 "
            "development and scaling evidence",
        ],
        "files": [
            "2026-07-30-stat2b1/campaign-spec.toml",
            "2026-07-30-stat2b1/run_manifest.json",
            "2026-07-30-stat2b1/decision_summary.json",
            "2026-07-30-stat2b1/cell_summaries.csv",
            "2026-07-30-stat2b1/FINDINGS.md",
            "2026-07-30-stat2b1/proposed_owner_gate.json",
            "2026-07-30-stat2b1/development-comparison.json",
            "2026-07-30-stat2b1/scaling-summary.json",
            "2026-07-30-stat2b1/scaling-summary.csv",
            "2026-07-30-stat2b1/figures/predictive_loss_by_arm.png",
            "2026-07-30-stat2b1/figures/critical_strata_predictive_difference.png",
        ],
    },
]

UNMANAGED = ("MANIFEST.json", "README.md")


class EvidenceError(RuntimeError):
    """A user-visible validation failure."""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_output(args: list[str]) -> bytes:
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(REPO), capture_output=True, check=True
        )
        return result.stdout
    except (subprocess.CalledProcessError, OSError) as exc:
        raise EvidenceError(f"could not read git provenance ({' '.join(args)}): {exc}") from exc


def code_revision() -> str:
    """Return the full lowercase HEAD SHA, or fail rather than misattribute evidence."""
    try:
        revision = _git_output(["rev-parse", "HEAD"]).decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise EvidenceError("git returned a non-ASCII HEAD revision") from exc
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise EvidenceError(f"git returned an invalid HEAD revision: {revision!r}")
    return revision


def code_provenance() -> tuple[str, bool, str | None]:
    """Return HEAD plus a deterministic digest of every non-HEAD worktree input.

    The digest frames ``git diff --binary HEAD`` and each untracked, nonignored file's raw Git
    path bytes and contents. Ignored report output is deliberately excluded. Untracked links and
    special files are refused rather than followed outside the checkout.
    """
    revision = code_revision()
    diff = _git_output(["diff", "--binary", "HEAD", "--"])
    raw_names = _git_output(["ls-files", "--others", "--exclude-standard", "-z"])
    names = sorted(name for name in raw_names.split(b"\0") if name)
    if not diff and not names:
        return revision, False, None

    digest = hashlib.sha256()
    digest.update(b"git-diff\0")
    digest.update(len(diff).to_bytes(8, "big"))
    digest.update(diff)
    for raw_name in names:
        relative = os.fsdecode(raw_name)
        # Git emits slash-separated repository paths with -z. Applying the same portable path
        # rules as the evidence boundary also prevents drive/ADS traversal on Windows.
        normalized = _validate_relative(relative, label="untracked git path")
        path = _contained(REPO, normalized, label="untracked git file")
        _require_regular(path, label="untracked git file")
        content = path.read_bytes()
        digest.update(b"untracked\0")
        digest.update(len(raw_name).to_bytes(8, "big"))
        digest.update(raw_name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return revision, True, digest.hexdigest()


def _is_reparse(path_stat: os.stat_result) -> bool:
    return bool(getattr(path_stat, "st_file_attributes", 0) & REPARSE_POINT)


def _validate_relative(value: object, *, label: str, single: bool = False) -> str:
    if not isinstance(value, str) or not value:
        raise EvidenceError(f"{label} must be a non-empty string")
    if (
        "\\" in value
        or any(ord(char) < 32 for char in value)
        or any(char in WINDOWS_INVALID_CHARS for char in value)
    ):
        raise EvidenceError(f"unsafe {label}: {value!r}")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise EvidenceError(f"unsafe {label}: {value!r}")
    parts = value.split("/")
    if single and len(parts) != 1:
        raise EvidenceError(f"unsafe {label}: {value!r}")
    for part in parts:
        if not part or part in {".", ".."} or part.endswith((" ", ".")) or ":" in part:
            raise EvidenceError(f"unsafe {label}: {value!r}")
        if part.split(".", 1)[0].upper() in WINDOWS_RESERVED:
            raise EvidenceError(f"unsafe {label}: {value!r}")
    return "/".join(parts)


def _contained(root: Path, relative: str, *, label: str) -> Path:
    candidate = root.joinpath(*relative.split("/"))
    # Reject links before resolve() follows them. A link that happens to point back inside the root
    # is still outside the collector's plain-tree contract.
    cursor = root
    for component in (None, *relative.split("/")):
        if component is not None:
            cursor /= component
        if cursor.exists() or cursor.is_symlink():
            cursor_stat = cursor.lstat()
            if stat.S_ISLNK(cursor_stat.st_mode) or _is_reparse(cursor_stat):
                raise EvidenceError(f"{label} must not use a symlink or reparse point: {cursor}")
    root_resolved = root.resolve()
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise EvidenceError(f"{label} escapes {root}") from exc
    return candidate


def _lstat(path: Path, *, label: str) -> os.stat_result:
    try:
        path_stat = path.lstat()
    except FileNotFoundError as exc:
        raise EvidenceError(f"missing {label}: {path}") from exc
    if stat.S_ISLNK(path_stat.st_mode) or _is_reparse(path_stat):
        raise EvidenceError(f"{label} must not be a symlink or reparse point: {path}")
    return path_stat


def _require_directory(path: Path, *, label: str) -> None:
    path_stat = _lstat(path, label=label)
    if not stat.S_ISDIR(path_stat.st_mode):
        raise EvidenceError(f"{label} is not a directory: {path}")


def _require_regular(path: Path, *, label: str) -> None:
    path_stat = _lstat(path, label=label)
    if not stat.S_ISREG(path_stat.st_mode):
        raise EvidenceError(f"{label} is not a regular file: {path}")


def _validate_plain_tree(root: Path, *, label: str) -> None:
    _require_directory(root, label=label)
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        for name in [*dirnames, *filenames]:
            child = current / name
            child_stat = _lstat(child, label=label)
            if not (stat.S_ISDIR(child_stat.st_mode) or stat.S_ISREG(child_stat.st_mode)):
                raise EvidenceError(f"{label} contains a non-regular entry: {child}")


def test_data_fingerprint() -> dict[str, Any]:
    """Digest names, sizes, and contents of all regular files in required ``test_data/``."""
    if not TEST_DATA.exists():
        raise EvidenceError("test_data/ is required to generate the evidence bundle")
    _validate_plain_tree(TEST_DATA, label="test_data")
    entries: list[str] = []
    for path in sorted((p for p in TEST_DATA.rglob("*") if p.is_file()), key=lambda p: p.as_posix()):
        rel = path.relative_to(TEST_DATA).as_posix()
        _validate_relative(rel, label="test_data path")
        _require_regular(path, label="test_data file")
        entries.append(f"{rel}\0{path.stat().st_size}\0{sha256_of(path)}")
    rolled = hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()
    return {"present": True, "n_files": len(entries), "sha256": rolled}


def _caption_rel(rel: str) -> str | None:
    path = PurePosixPath(rel)
    return path.with_name(path.stem + ".caption.md").as_posix() if path.suffix.lower() == ".png" else None


def _command_script(group: dict[str, Any]) -> Path:
    command = group.get("command")
    if not isinstance(command, str):
        raise EvidenceError(f"group {group.get('name')!r} has no command")
    pieces = command.split()
    if len(pieces) != 2 or pieces[0] != "python":
        raise EvidenceError(f"unsupported generator command {command!r}; expected 'python <script>'")
    script_rel = _validate_relative(pieces[1], label="generator script")
    script = _contained(REPO, script_rel, label="generator script")
    _require_regular(script, label="generator script")
    return script


def _declarations() -> list[dict[str, Any]]:
    seen_groups: set[str] = set()
    seen_sources: set[str] = set()
    seen_asset_paths: set[str] = set()
    validated: list[dict[str, Any]] = []
    for raw in BUNDLE:
        if not isinstance(raw, dict):
            raise EvidenceError("every BUNDLE group must be an object")
        group = dict(raw)
        name = _validate_relative(group.get("name"), label="group name", single=True)
        source = _validate_relative(group.get("source"), label="source name", single=True)
        folded_name, folded_source = name.casefold(), source.casefold()
        if folded_name in seen_groups:
            raise EvidenceError(f"duplicate/case-colliding group name: {name}")
        if folded_source in seen_sources:
            raise EvidenceError(f"duplicate/case-colliding source name: {source}")
        seen_groups.add(folded_name)
        seen_sources.add(folded_source)
        if not isinstance(group.get("what"), str) or not isinstance(group.get("replaces"), list):
            raise EvidenceError(f"group {name!r} has invalid descriptive metadata")
        files = group.get("files")
        if not isinstance(files, list) or not files:
            raise EvidenceError(f"group {name!r} must declare files")
        local: set[str] = set()
        normalized: list[str] = []
        for raw_rel in files:
            rel = _validate_relative(raw_rel, label=f"{name} artifact path")
            folded = rel.casefold()
            if folded in local:
                raise EvidenceError(f"duplicate/case-colliding artifact in {name}: {rel}")
            local.add(folded)
            normalized.append(rel)
            for promoted in (rel, _caption_rel(rel)):
                if promoted is None:
                    continue
                full = f"{name}/{promoted}".casefold()
                if full in seen_asset_paths:
                    raise EvidenceError(f"duplicate/case-colliding promoted path: {name}/{promoted}")
                if any(
                    full.startswith(existing + "/") or existing.startswith(full + "/")
                    for existing in seen_asset_paths
                ):
                    raise EvidenceError(
                        f"file/directory prefix collision in promoted path: {name}/{promoted}"
                    )
                seen_asset_paths.add(full)
        group["name"], group["source"], group["files"] = name, source, normalized
        group["_script"] = _command_script(group)
        _contained(OUT, source, label="source directory")
        _contained(ASSETS, name, label="asset group")
        validated.append(group)
    return validated


def _remove_plain_tree(path: Path, *, label: str) -> None:
    if not path.exists() and not path.is_symlink():
        return
    _validate_plain_tree(path, label=label)
    shutil.rmtree(path)


def _run_generators(groups: list[dict[str, Any]], fingerprint: dict[str, Any]) -> None:
    for group in groups:
        before = test_data_fingerprint()
        if before != fingerprint:
            raise EvidenceError(f"test_data/ changed before {group['command']}")
        source_root = _contained(OUT, group["source"], label="source directory")
        _remove_plain_tree(source_root, label=f"stale output {group['source']}")
        print(f"running {group['command']} …")
        try:
            subprocess.run([sys.executable, str(group["_script"])], cwd=str(REPO), check=True)
        except (subprocess.CalledProcessError, OSError) as exc:
            raise EvidenceError(f"generator failed: {group['command']} ({exc})") from exc
        after = test_data_fingerprint()
        if after != fingerprint:
            raise EvidenceError(f"test_data/ changed while running {group['command']}")


def _source_records(groups: list[dict[str, Any]]) -> dict[str, list[tuple[str, Path]]]:
    result: dict[str, list[tuple[str, Path]]] = {}
    for group in groups:
        base = _contained(OUT, group["source"], label="source directory")
        _require_directory(base, label=f"generated output {group['source']}")
        records: list[tuple[str, Path]] = []
        for rel in group["files"]:
            source = _contained(base, rel, label="source artifact")
            _require_regular(source, label="source artifact")
            records.append((rel, source))
            caption = _caption_rel(rel)
            if caption is not None:
                sidecar = _contained(base, caption, label="caption sidecar")
                _require_regular(sidecar, label="caption sidecar")
                records.append((caption, sidecar))
        result[group["name"]] = records
    return result


def _manifest(groups: list[dict[str, Any]], fingerprint: dict[str, Any]) -> dict[str, Any]:
    revision, dirty, diff_sha256 = code_provenance()
    return {
        "schema_version": SCHEMA_VERSION,
        "collected": date.today().isoformat(),
        "code_revision": revision,
        "code_dirty": dirty,
        "code_diff_sha256": diff_sha256,
        "privacy": (
            "Derived evidence only: fit results, parameter summaries, and rendered figures. No "
            "raw facility file is included; scan overlays expose measured points at plot accuracy."
        ),
        "reproducibility": (
            "Regeneration requires local test_data/. The bundle is viewable from a clean checkout, "
            "not reproducible from one."
        ),
        "test_data_fingerprint": fingerprint,
        "groups": [
            {
                "name": group["name"],
                "source": group["source"],
                "what": group["what"],
                "command": group["command"],
                "replaces": group["replaces"],
                "input_fingerprint": fingerprint,
                "files": [],
            }
            for group in groups
        ],
    }


def _stage(
    stage: Path,
    groups: list[dict[str, Any]],
    sources: dict[str, list[tuple[str, Path]]],
    fingerprint: dict[str, Any],
) -> dict[str, Any]:
    manifest = _manifest(groups, fingerprint)
    for group, manifest_group in zip(groups, manifest["groups"], strict=True):
        for rel, source in sources[group["name"]]:
            _require_regular(source, label="source artifact (copy revalidation)")
            destination = _contained(stage / group["name"], rel, label="staged artifact")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() or destination.is_symlink():
                raise EvidenceError(f"staged artifact destination already exists: {destination}")
            shutil.copy2(source, destination)
            _require_regular(destination, label="staged artifact")
            manifest_group["files"].append({
                "path": f"{group['name']}/{rel}",
                "bytes": destination.stat().st_size,
                "sha256": sha256_of(destination),
            })
    (stage / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def _promote(stage: Path, groups: list[dict[str, Any]]) -> None:
    _require_directory(ASSETS.parent, label="asset parent")
    ASSETS.mkdir(parents=True, exist_ok=True)
    _require_directory(ASSETS, label="asset root")
    backup = Path(tempfile.mkdtemp(prefix=".evidence-backup-", dir=ASSETS.parent))
    old_moved: list[tuple[Path, Path]] = []
    promoted: list[Path] = []
    preserve_backup = False
    try:
        targets = [(_contained(ASSETS, group["name"], label="asset group"), backup / group["name"])
                   for group in groups]
        targets.append((MANIFEST, backup / "MANIFEST.json"))
        # Complete the preflight before moving the first tracked path. Rollback is a last-resort
        # guard for I/O failure during promotion, not a substitute for validation.
        _require_directory(stage, label="staging root")
        _require_regular(stage / "MANIFEST.json", label="staged manifest")
        for group in groups:
            _validate_plain_tree(stage / group["name"], label=f"staged group {group['name']}")
        for target, saved in targets:
            if target.exists() or target.is_symlink():
                if target == MANIFEST:
                    _require_regular(target, label="existing manifest")
                else:
                    _validate_plain_tree(target, label="existing asset group")
        for target, saved in targets:
            if target.exists() or target.is_symlink():
                os.replace(target, saved)
                old_moved.append((target, saved))
        for group in groups:
            target = _contained(ASSETS, group["name"], label="asset group")
            os.replace(stage / group["name"], target)
            promoted.append(target)
        os.replace(stage / "MANIFEST.json", MANIFEST)
        promoted.append(MANIFEST)
    except BaseException as exc:
        rollback_errors: list[str] = []
        for target in reversed(promoted):
            try:
                target_stat = target.lstat()
                if stat.S_ISLNK(target_stat.st_mode):
                    target.unlink()
                elif _is_reparse(target_stat):
                    os.rmdir(target)
                elif stat.S_ISDIR(target_stat.st_mode):
                    _remove_plain_tree(target, label="partially promoted group")
                else:
                    target.unlink()
            except FileNotFoundError:
                pass
            except BaseException as rollback_exc:
                rollback_errors.append(
                    f"{type(rollback_exc).__name__}: {rollback_exc}"
                )
        for target, saved in reversed(old_moved):
            try:
                if saved.exists():
                    os.replace(saved, target)
            except BaseException as rollback_exc:
                rollback_errors.append(
                    f"{type(rollback_exc).__name__}: {rollback_exc}"
                )
        preserve_backup = bool(rollback_errors)
        detail = f"; rollback errors: {'; '.join(rollback_errors)}" if rollback_errors else ""
        if preserve_backup:
            detail += f"; recovery backup retained at {backup}"
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            if detail:
                exc.add_note(f"Evidence promotion rollback status{detail}")
            raise
        raise EvidenceError(f"promotion failed and was rolled back ({exc}){detail}") from exc
    finally:
        if backup.exists() and not preserve_backup:
            try:
                shutil.rmtree(backup)
            except OSError as exc:
                print(f"warning: could not remove promotion backup {backup} ({exc})", file=sys.stderr)


def collect() -> int:
    stage: Path | None = None
    try:
        groups = _declarations()
        fingerprint = test_data_fingerprint()
        _run_generators(groups, fingerprint)
        if test_data_fingerprint() != fingerprint:
            raise EvidenceError("test_data/ changed after report generation")
        sources = _source_records(groups)
        _require_directory(ASSETS.parent, label="asset parent")
        stage = Path(tempfile.mkdtemp(prefix=".evidence-stage-", dir=ASSETS.parent))
        manifest = _stage(stage, groups, sources, fingerprint)
        _promote(stage, groups)
    except (EvidenceError, OSError) as exc:
        print(f"evidence collection failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if stage is not None and stage.exists():
            try:
                shutil.rmtree(stage)
            except OSError as exc:
                print(f"warning: could not remove staging directory {stage} ({exc})", file=sys.stderr)
    total = sum(len(group["files"]) for group in manifest["groups"])
    print(f"wrote {MANIFEST.relative_to(REPO)} — {total} files across {len(groups)} groups")
    return 0


def _expected_paths(group: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for rel in group["files"]:
        result.append(f"{group['name']}/{rel}")
        caption = _caption_rel(rel)
        if caption is not None:
            result.append(f"{group['name']}/{caption}")
    return result


def _valid_fingerprint(value: object) -> bool:
    if not isinstance(value, dict) or value.get("present") is not True:
        return False
    count = value.get("n_files")
    digest = value.get("sha256")
    return (
        type(count) is int
        and count >= 0
        and isinstance(digest, str)
        and len(digest) == 64
        and all(character in string.hexdigits for character in digest)
    )


def _valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in string.hexdigits for character in value)
    )


def _semantic_manifest(manifest: object, groups: list[dict[str, Any]]) -> tuple[list[str], set[str]]:
    problems: list[str] = []
    listed: set[str] = set()
    if not isinstance(manifest, dict):
        return ["manifest root is not an object"], listed
    if manifest.get("schema_version") != SCHEMA_VERSION:
        problems.append(
            f"unsupported schema_version: {manifest.get('schema_version')!r} "
            f"(expected {SCHEMA_VERSION})"
        )
    collected = manifest.get("collected")
    try:
        collected_valid = (
            isinstance(collected, str)
            and date.fromisoformat(collected).isoformat() == collected
        )
    except ValueError:
        collected_valid = False
    if not collected_valid:
        problems.append("collected must be an ISO date in YYYY-MM-DD form")
    revision = manifest.get("code_revision")
    if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        problems.append("code_revision must be a full lowercase 40-hex HEAD SHA")
    dirty = manifest.get("code_dirty")
    diff_sha256 = manifest.get("code_diff_sha256")
    if type(dirty) is not bool:
        problems.append("code_dirty must be a boolean")
    elif dirty and not _valid_sha256(diff_sha256):
        problems.append("dirty code requires a 64-hex code_diff_sha256")
    elif not dirty and diff_sha256 is not None:
        problems.append("clean code requires code_diff_sha256 to be null")
    fingerprint = manifest.get("test_data_fingerprint")
    if not _valid_fingerprint(fingerprint):
        problems.append("test_data_fingerprint is invalid or does not describe present inputs")
    actual_groups = manifest.get("groups")
    if not isinstance(actual_groups, list):
        return [*problems, "groups is not a list"], listed
    if len(actual_groups) != len(groups):
        problems.append(f"group count differs: {len(actual_groups)} != {len(groups)}")
    seen: set[str] = set()
    for index, expected in enumerate(groups):
        if index >= len(actual_groups) or not isinstance(actual_groups[index], dict):
            problems.append(f"missing or invalid group at index {index}: {expected['name']}")
            continue
        actual = actual_groups[index]
        for key in ("name", "source", "what", "command", "replaces"):
            if actual.get(key) != expected[key]:
                problems.append(f"{expected['name']}: manifest {key} does not match BUNDLE")
        if actual.get("input_fingerprint") != fingerprint:
            problems.append(f"{expected['name']}: input_fingerprint differs from bundle fingerprint")
        records = actual.get("files")
        if not isinstance(records, list):
            problems.append(f"{expected['name']}: files is not a list")
            continue
        actual_paths: list[str] = []
        for record in records:
            if not isinstance(record, dict):
                problems.append(f"{expected['name']}: invalid file record")
                continue
            raw_path = record.get("path")
            try:
                path = _validate_relative(raw_path, label="manifest artifact path")
            except EvidenceError as exc:
                problems.append(str(exc))
                continue
            folded = path.casefold()
            if folded in seen:
                problems.append(f"duplicate/case-colliding manifest path: {path}")
            seen.add(folded)
            listed.add(path)
            actual_paths.append(path)
            if type(record.get("bytes")) is not int or record["bytes"] < 0:
                problems.append(f"{path}: invalid byte count")
            digest = record.get("sha256")
            if not _valid_sha256(digest):
                problems.append(f"{path}: invalid sha256")
        expected_paths = _expected_paths(expected)
        if actual_paths != expected_paths:
            problems.append(f"{expected['name']}: file list does not exactly match BUNDLE")
    if len(actual_groups) > len(groups):
        for extra in actual_groups[len(groups):]:
            problems.append(f"unexpected group: {extra.get('name') if isinstance(extra, dict) else extra!r}")
    return problems, listed


def _disk_files_and_problems() -> tuple[set[str], list[str]]:
    files: set[str] = set()
    problems: list[str] = []
    unmanaged = set(UNMANAGED)
    if not ASSETS.exists():
        return files, [f"missing asset root: {ASSETS}"]
    try:
        _require_directory(ASSETS, label="asset root")
    except EvidenceError as exc:
        return files, [str(exc)]
    for directory, dirnames, filenames in os.walk(ASSETS, followlinks=False):
        current = Path(directory)
        for name in [*dirnames, *filenames]:
            path = current / name
            rel = path.relative_to(ASSETS).as_posix()
            if rel in unmanaged:
                continue
            try:
                _validate_relative(rel, label="asset path")
                path_stat = _lstat(path, label="asset entry")
                if stat.S_ISREG(path_stat.st_mode):
                    files.add(rel)
                elif not stat.S_ISDIR(path_stat.st_mode):
                    problems.append(f"non-regular asset entry: {rel}")
            except EvidenceError as exc:
                problems.append(str(exc))
    return files, problems


def check() -> int:
    """Verify manifest semantics and then committed file hashes; copy nothing."""
    try:
        groups = _declarations()
    except EvidenceError as exc:
        print(f"invalid BUNDLE declaration: {exc}", file=sys.stderr)
        return 1
    if not MANIFEST.exists():
        print(f"missing {MANIFEST}", file=sys.stderr)
        return 1
    try:
        _require_regular(MANIFEST, label="manifest")
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (EvidenceError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"invalid manifest: {exc}", file=sys.stderr)
        return 1

    problems, listed = _semantic_manifest(manifest, groups)
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1
    disk_files, disk_problems = _disk_files_and_problems()
    problems.extend(disk_problems)
    if isinstance(manifest, dict) and isinstance(manifest.get("groups"), list):
        for group in manifest["groups"]:
            if not isinstance(group, dict) or not isinstance(group.get("files"), list):
                continue
            for record in group["files"]:
                if not isinstance(record, dict) or not isinstance(record.get("path"), str):
                    continue
                try:
                    rel = _validate_relative(record["path"], label="manifest artifact path")
                    path = _contained(ASSETS, rel, label="manifest artifact")
                    _require_regular(path, label="manifest artifact")
                except EvidenceError as exc:
                    message = str(exc)
                    if message not in problems:
                        problems.append(str(exc))
                    continue
                if isinstance(record.get("bytes"), int) and path.stat().st_size != record["bytes"]:
                    problems.append(f"changed size since collection: {rel}")
                if isinstance(record.get("sha256"), str) and sha256_of(path) != record["sha256"]:
                    problems.append(f"changed since collection: {rel}")
    for extra in sorted(disk_files - listed):
        problems.append(f"present but unlisted: {extra}")
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    print(f"bundle matches the manifest — {len(listed)} files")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true",
        help="verify the committed bundle against MANIFEST.json instead of regenerating",
    )
    args = parser.parse_args()
    return check() if args.check else collect()


if __name__ == "__main__":
    raise SystemExit(main())
