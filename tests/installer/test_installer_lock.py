"""Routine guard for installer/isar-win-64.lock: the pinned Windows environment matches pyproject.toml.

The checker reads the lock the way the installer will (micromamba explicit format, conda-forge URLs
with a checksum) and asks whether each base, GUI and build-system requirement is locked at a version
its specifier allows. The broken copies make sure the checker can actually fail.
"""
import re
import subprocess
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement

REPO = Path(__file__).resolve().parents[2]
LOCK = REPO / "installer" / "isar-win-64.lock"
PYPROJECT = REPO / "pyproject.toml"

URL = re.compile(r"https://conda\.anaconda\.org/conda-forge/\S+#(?:[0-9a-f]{32}|[0-9a-f]{64})$")
# pyproject name -> conda name; every other name is its lowercase.
CONDA_NAME = {"matplotlib": "matplotlib-base", "PySide6": "pyside6"}
INSTALL_TOOLS = ("python", "pip", "setuptools", "git")


def lock_problems(lock_path, pyproject_path):
    problems = []
    data = lock_path.read_bytes()
    if b"\r" in data:
        problems.append("lock has CR bytes; it must be LF")
    lines = data.decode("utf-8").split("\n")
    if lines[0] != "# platform: win-64":
        problems.append("line 1 is not '# platform: win-64'")
    if "@EXPLICIT" not in lines:
        return problems + ["no @EXPLICIT line"]
    start = lines.index("@EXPLICIT")
    if any(not line.startswith("#") for line in lines[:start]):
        problems.append("content before @EXPLICIT other than # comments")

    packages = {}
    for line in filter(None, lines[start + 1:]):
        if not URL.match(line):
            problems.append("not a conda-forge URL with a checksum: " + line[:100])
            continue
        filename = line.split("#")[0].rsplit("/", 1)[1]
        stem = filename.removesuffix(".conda").removesuffix(".tar.bz2")
        name, version, _build = stem.rsplit("-", 2)
        if name in packages:
            problems.append(name + " is locked twice")
        packages[name] = version

    for name in INSTALL_TOOLS:
        if name not in packages:
            problems.append(name + " is missing from the lock")
    if "python" in packages and not packages["python"].startswith("3.11."):
        problems.append("python is %s, expected 3.11.x" % packages["python"])

    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    project = pyproject["project"]
    requirements = [*project["dependencies"], *project["optional-dependencies"]["gui"],
                    *pyproject["build-system"]["requires"]]
    for raw in requirements:
        req = Requirement(raw)
        conda = CONDA_NAME.get(req.name, req.name.lower())
        if conda not in packages:
            problems.append("%s: %s is missing from the lock" % (raw, conda))
        elif not req.specifier.contains(packages[conda], prereleases=True):
            problems.append("%s: locked at %s" % (raw, packages[conda]))
    return problems


def test_lock_matches_pyproject():
    assert lock_problems(LOCK, PYPROJECT) == []


BREAKS = {
    "requirement dropped": lambda t: "\n".join(l for l in t.split("\n") if "/dynesty-" not in l),
    "version out of range": lambda t: re.sub(r"(/dynesty-)\d[^-]*", r"\g<1>4.0.0", t),
    "python not 3.11": lambda t: re.sub(r"(/python-)3\.11\.\d+", r"\g<1>3.12.1", t),
    "checksum dropped": lambda t: re.sub(r"(conda-forge/\S+\.conda)#\w+", r"\1", t, count=1),
    "other channel": lambda t: t.replace("conda-forge/", "bioconda/", 1),
}


@pytest.mark.parametrize("mutate", BREAKS.values(), ids=BREAKS.keys())
def test_checker_catches_broken_copy(tmp_path, mutate):
    broken = tmp_path / "isar-win-64.lock"
    with open(LOCK, encoding="utf-8", newline="") as f:
        text = f.read()
    with open(broken, "w", encoding="utf-8", newline="\n") as f:
        f.write(mutate(text))
    assert lock_problems(broken, PYPROJECT), "a broken copy passed the checker"


def test_installer_bytes_are_stored_as_is():
    # `installer/** -text` (D3): git must not rewrite the lock's line endings or its checksums' bytes.
    out = subprocess.run(["git", "check-attr", "text", "--", "installer/isar-win-64.lock"],
                         cwd=REPO, capture_output=True, text=True, encoding="utf-8", check=True).stdout
    assert out.strip().endswith("text: unset"), out
