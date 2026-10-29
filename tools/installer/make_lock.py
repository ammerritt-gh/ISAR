"""Regenerate installer/isar-win-64.lock: the whole Windows environment, pinned.

    python tools/installer/make_lock.py [--micromamba <path to micromamba.exe>]

Solves the install specs below into a throwaway root prefix under %TEMP% (never a named
environment, never the developer's root prefix), exports the result in micromamba's explicit
format with checksums, and writes it to installer/isar-win-64.lock. Run it by hand when the
pinned set should change; tests/installer/test_installer_lock.py checks the lock against
pyproject.toml. The scratch folder is removed afterwards, pass or fail.
"""
import argparse
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LOCK = REPO / "installer" / "isar-win-64.lock"
MICROMAMBA = Path.home() / "AppData" / "Local" / "micromamba" / "micromamba.exe"
# The interpreter and install tools, then pyproject's base and GUI requirements under their
# conda names (matplotlib -> matplotlib-base, PySide6 -> pyside6, the rest lowercased).
SPECS = [
    "python=3.11", "pip", "setuptools>=64", "git",
    "numpy>=1.26", "scipy>=1.11", "matplotlib-base>=3.8", "iminuit>=2.25",
    "dynesty>=3,<4", "threadpoolctl>=3.1",
    "pyside6", "pyqtgraph>=0.13,<0.15", "watchdog",
]
CALL_TIMEOUT_S = 1800  # per micromamba call; a stalled download ends the run, it does not hang it


def _clean_env(profile):
    # Conda and mamba settings must not leak into the solve (the developer's rc files and variables).
    # USERPROFILE moves into the scratch folder: micromamba cannot be told not to register the prefix
    # in %USERPROFILE%\.conda\environments.txt, nor conda-forge's git not to add a Start Menu shortcut.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("CONDA", "MAMBA"))}
    env["USERPROFILE"] = str(profile)
    return env


def _run(args, profile):
    proc = subprocess.run(args, env=_clean_env(profile), capture_output=True, timeout=CALL_TIMEOUT_S)
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()[-15:]
        sys.exit("micromamba failed (exit %d):\n%s" % (proc.returncode, "\n".join(tail)))
    return proc.stdout.decode("utf-8")


def _remove(folder):
    # conda marks cached package files read-only, which shutil.rmtree refuses on Windows.
    for p in [folder, *folder.rglob("*")]:
        p.chmod(stat.S_IWRITE)
    shutil.rmtree(folder)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--micromamba", type=Path, default=MICROMAMBA)
    args = parser.parse_args()
    mm = str(args.micromamba)
    if not args.micromamba.is_file():
        sys.exit("micromamba not found: %s (pass --micromamba)" % mm)

    scratch = Path(tempfile.mkdtemp(prefix="isar-lock-"))
    root, prefix = scratch / "mamba", scratch / "env"
    started = time.monotonic()
    try:
        _run([mm, "create", "-y", "--no-rc", "--no-shortcuts", "--override-channels", "-c", "conda-forge",
              "-r", str(root), "-p", str(prefix), *SPECS], scratch / "profile")
        export = _run([mm, "env", "export", "--explicit", "--no-rc", "-r", str(root), "-p", str(prefix)],
                      scratch / "profile")
    finally:
        _remove(scratch)

    lines = export.splitlines()
    if "# platform: win-64" not in lines or "@EXPLICIT" not in lines:
        sys.exit("export is not a win-64 explicit lock; lock not written")
    body = [line for line in lines[lines.index("@EXPLICIT"):] if line.strip()]
    with open(LOCK, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(["# platform: win-64", *body]) + "\n")
    print("wrote %s: %d packages in %.0f s" % (LOCK, len(body) - 1, time.monotonic() - started))


if __name__ == "__main__":
    main()
