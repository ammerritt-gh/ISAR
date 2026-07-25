#!/usr/bin/env python
"""Copy the cited validation evidence out of the disposable `out/` tree into `docs/assets/`.

The problem this solves: `README.md` and `docs/READING_GUIDE.md` cite `out/phase3_gold/FINDINGS.md`
and friends as the evidence that ISAR works, but `out/` is gitignored and disposable (DECISIONS
W1). A fresh clone therefore cannot see a single one of the numbers the front door quotes. This
script promotes a **declared, derived-only** subset to `docs/assets/`, where it is committed and
viewable, and writes `docs/assets/MANIFEST.json` recording where each file came from.

**Derived evidence only.** Every artifact below is a fit result, a summary table of fitted
parameters, or a rendered figure. No raw facility file is copied, and none may be added:
`test_data/` stays gitignored. The manifest records a fingerprint of the `test_data/` tree so a
holder of that data can confirm they have the same inputs, without the data being published here.

`docs/assets/` is **evidence**, not a contract: it records what a named revision produced on a
named date. It is not regenerated on every commit, and it is not a reproducibility claim —
regenerating any of it still requires the local `test_data/`.

Run from the repo root, after generating the reports it reads:

    python docs/in8_phase1_check.py
    python docs/phase3_step1_check.py
    python docs/phase3_gold_check.py
    python tools/docs/collect_evidence.py

Add `--check` to verify the committed bundle matches its manifest without copying anything.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "out"
ASSETS = REPO / "docs" / "assets"
MANIFEST = ASSETS / "MANIFEST.json"
TEST_DATA = REPO / "test_data"

SCHEMA_VERSION = 1

# The bundle, declared rather than globbed. A glob would silently sweep in whatever the last run
# happened to leave behind; every file here is one a document actually cites.
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
            "FINDINGS.md",
            "results_gold.csv",
            "figures/confusion.png",
            "figures/omega0_scatter.png",
            "figures/omega0_pull_hist.png",
            "figures/confidence_calibration.png",
            "figures/audit.png",
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
            "FINDINGS.md",
            "results_in8.csv",
            "results_in8_merged.csv",
            "results_s30.csv",
            "figures/055181.png",
            "figures/055182.png",
            "figures/055289.png",
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
        # The five WORKED_EXAMPLE.md walks through, plus the two its FINDINGS.md argues from. The
        # other five per-scan overlays are deliberately left in out/: they are the same figure at a
        # different Q, and committing them buys nothing a reader cannot get by rerunning.
        "files": [
            "FINDINGS.md",
            "results_step1.csv",
            "figures/energy_zero_series.png",
            "figures/s30_scan168_ana5.png",
            "figures/bootstrap_omega0_histograms.png",
            "figures/selection_akaike_weights.png",
            "figures/verdict_confidence.png",
            "figures/dispersion_summary.png",
            "figures/synthetic_fork.png",
        ],
    },
]


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def code_revision() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(REPO), capture_output=True, text=True, check=True
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"warning: could not read the code revision from git ({exc})", file=sys.stderr)
        return "unknown"


def test_data_fingerprint() -> dict:
    """A digest over the whole `test_data/` tree: name, size, and content of every file.

    This is how a reader who *has* the facility data confirms they hold the same inputs that
    produced the bundle, without the data itself being published. Recorded as one digest rather
    than a per-file list so the manifest does not become an index of unpublished filenames.
    """
    if not TEST_DATA.exists():
        return {"present": False, "note": "test_data/ absent when the bundle was collected"}
    entries = []
    for path in sorted(p for p in TEST_DATA.rglob("*") if p.is_file()):
        rel = path.relative_to(TEST_DATA).as_posix()
        entries.append(f"{rel}\0{path.stat().st_size}\0{sha256_of(path)}")
    rolled = hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()
    return {"present": True, "n_files": len(entries), "sha256": rolled}


# The two hand-written files at the top of docs/assets/. Excluded by exact path rather than by
# basename: a `README.md` *inside* a group is not an index, it is an undeclared file, and that is
# precisely what the unlisted-file check exists to catch.
UNMANAGED = ("MANIFEST.json", "README.md")


def bundle_files_on_disk() -> set[str]:
    """Every file under `docs/assets/` that the manifest is expected to account for."""
    unmanaged = {ASSETS / name for name in UNMANAGED}
    return {
        p.relative_to(ASSETS).as_posix()
        for p in ASSETS.rglob("*")
        if p.is_file() and p not in unmanaged
    }


def caption_sidecar(path: Path) -> Path | None:
    """The `<figure>.caption.md` a report figure carries, if it has one."""
    if path.suffix.lower() != ".png":
        return None
    sidecar = path.with_name(path.stem + ".caption.md")
    return sidecar if sidecar.exists() else None


def collect() -> int:
    missing: list[str] = []
    for group in BUNDLE:
        base = OUT / group["source"]
        for rel in group["files"]:
            if not (base / rel).exists():
                missing.append(f"out/{group['source']}/{rel}")
    if missing:
        print("missing source artifacts — generate the reports first:", file=sys.stderr)
        for item in missing:
            print(f"  {item}", file=sys.stderr)
        for group in BUNDLE:
            print(f"  ({group['command']})", file=sys.stderr)
        return 1

    # docs/assets/README.md promises a caption beside every figure, so a figure without one is a
    # broken promise rather than a minor omission: the whole point is that a figure is readable
    # away from its report.
    uncaptioned = [
        f"out/{group['source']}/{rel}"
        for group in BUNDLE
        for rel in group["files"]
        if rel.lower().endswith(".png") and caption_sidecar(OUT / group["source"] / rel) is None
    ]
    if uncaptioned:
        print("promoted figures with no .caption.md sidecar:", file=sys.stderr)
        for item in uncaptioned:
            print(f"  {item}", file=sys.stderr)
        return 1

    print("fingerprinting test_data/ …")
    fingerprint = test_data_fingerprint()

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "collected": date.today().isoformat(),
        "code_revision": code_revision(),
        "privacy": (
            "Derived evidence only: fit results, summary tables of fitted parameters, and rendered "
            "figures. No raw facility data file is included; test_data/ remains gitignored and "
            "unpublished. Note that a scan-overlay figure necessarily plots the measured points of "
            "the scan it fits (counts vs energy transfer, labelled with the scan or numor and its "
            "instrument command), so a handful of scans are readable off the plots at plot "
            "accuracy. That is deliberate — the figures exist so a reader can judge each call — "
            "and is the limit of what is exposed. Reviewed at collection time."
        ),
        "reproducibility": (
            "Regenerating any group requires the local test_data/ and the command below. This "
            "bundle makes the evidence viewable from a clean checkout; it does not make it "
            "reproducible from one."
        ),
        "test_data_fingerprint": fingerprint,
        "groups": [],
    }

    for group in BUNDLE:
        base = OUT / group["source"]
        # rmtree below is unrecoverable, so refuse anything that is not a plain child directory
        # name. BUNDLE is hand-edited; an empty or slash-bearing name would aim the delete at
        # docs/assets itself or at a sibling.
        name = group["name"]
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            print(f"refusing to collect: unsafe group name {name!r}", file=sys.stderr)
            return 1
        dest_root = ASSETS / name
        if dest_root.exists():
            shutil.rmtree(dest_root)  # a stale file left behind would be evidence of nothing
        records = []
        for rel in group["files"]:
            src = base / rel
            dest = dest_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            records.append({
                "path": f"{group['name']}/{rel}",
                "bytes": dest.stat().st_size,
                "sha256": sha256_of(dest),
            })
            sidecar = caption_sidecar(src)
            if sidecar is not None:
                sidecar_dest = dest.with_name(sidecar.name)
                shutil.copy2(sidecar, sidecar_dest)
                records.append({
                    "path": f"{group['name']}/{sidecar.relative_to(base).as_posix()}",
                    "bytes": sidecar_dest.stat().st_size,
                    "sha256": sha256_of(sidecar_dest),
                })
        manifest["groups"].append({
            "name": group["name"],
            "what": group["what"],
            "command": group["command"],
            "replaces": group["replaces"],
            "files": records,
        })
        print(f"  {group['name']}: {len(records)} files")

    ASSETS.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    total = sum(len(g["files"]) for g in manifest["groups"])
    print(f"wrote {MANIFEST.relative_to(REPO)} — {total} files across {len(BUNDLE)} groups")
    return 0


def check() -> int:
    """Verify the committed bundle against its manifest. Hashes only; copies nothing."""
    if not MANIFEST.exists():
        print(f"missing {MANIFEST}", file=sys.stderr)
        return 1
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    problems: list[str] = []
    listed: set[str] = set()
    for group in manifest["groups"]:
        for record in group["files"]:
            listed.add(record["path"])
            path = ASSETS / record["path"]
            if not path.exists():
                problems.append(f"missing: {record['path']}")
            elif sha256_of(path) != record["sha256"]:
                problems.append(f"changed since collection: {record['path']}")
    for extra in sorted(bundle_files_on_disk() - listed):
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
        help="verify the committed bundle against MANIFEST.json instead of collecting",
    )
    args = parser.parse_args()
    return check() if args.check else collect()


if __name__ == "__main__":
    raise SystemExit(main())
