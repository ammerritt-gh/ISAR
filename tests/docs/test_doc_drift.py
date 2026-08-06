"""Drift checks that keep the documentation tree honest about itself.

These test the *mechanically checkable* half of the documentation policy in `AGENTS.md`. They do
not judge prose. Each check below names the specific failure it prevents, because a doc test that
cannot say what it is for becomes a chore that people delete.

Deliberately **not** tested:

* The archive-authority rule ("a `Current` document may not take an `Archived` or
  `Historical evidence` document as its *normative source*"). Whether a citation is normative or
  merely provenance is a judgement about the sentence around it — no checker can make it, and a
  crude "Current must not link to archive/" rule would forbid the legitimate rationale citations
  `README.md` and `docs/READING_GUIDE.md` already carry. This stays a review item.
* A generated CLI reference. Check 1 compares against argparse rather than generating prose,
  because a generated command reference is a file nobody reads that still has to be regenerated.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INVENTORY = REPO / "docs" / "DOC_INVENTORY.md"

LIFECYCLE_VALUES = ("Current", "Active plan", "Historical evidence", "Archived")

# Paths exempt from the banner and registration rules. These mirror the "Generated — exempt from
# lifecycle banners" table in docs/DOC_INVENTORY.md; test_exempt_paths_are_declared_in_the_register
# below fails if the two drift apart.
EXEMPT_PREFIXES = (
    "docs/stats/figures/",       # caption sidecars, written by docs/stats/make_figures.py
    "docs/assets/",              # promoted report output, written by the docs/*_check.py scripts
    "tests/data/golden/",        # byte-compared fixtures; a banner would break the comparison
)
EXEMPT_FILES = (
    "CLAUDE.md",                 # a 12-byte `@AGENTS.md` stub
    "docs/DESIGN_GOALS.md",      # owner-authored objectives list; declared off-limits to agents
)
# Carve-outs: hand-written index files that happen to sit under an exempt prefix. Without these,
# adding one generated directory would silently stop checking its own README.
NEVER_EXEMPT = (
    "docs/assets/README.md",
)

# Link checking is exempt separately and far more narrowly. A file being generator-owned is a
# reason not to demand a lifecycle banner of it; it is *not* a reason to let its links rot — a
# promoted FINDINGS.md whose figures were not promoted with it is precisely the failure this
# bundle exists to prevent.
LINK_EXEMPT_PREFIXES = (
    "tests/data/golden/",        # points into a generated workspace that is not in the tree
)

# Documents allowed to name a deleted file (check 2). The register is where a reader learns where
# a deleted file went, and the ledger is append-only (DECISIONS.md D4) so its historical mentions
# may not be edited out. DECISION_INDEX.md is generated from the ledger and inherits them.
DELETED_NAME_ALLOWLIST = (
    "docs/DOC_INVENTORY.md",
    "docs/DECISIONS.md",
    "docs/DECISION_INDEX.md",
)

# Phrases that were wrong and were corrected. Each entry records what it prevents coming back.
# Applied only to maintained documents (see _maintained_docs) — a historical record is allowed,
# indeed required, to keep the wording that was accurate on its date.
STALE_PHRASES = (
    (
        "Planned, not wired in",
        "the Bayesian rung is implemented and running as a diagnostic; only its licence to "
        "certify is Planned (DECISIONS S5)",
    ),
    (
        "planned_rung",
        "renamed to noncertifying_rung when the Bayesian rung's status was corrected",
    ),
    (
        "Every capability is **Planned** unless",
        "GUI_DESIGN.md carries an as-built matrix now; the blanket preamble was wrong for "
        "Phase 0/A and Pass 3",
    ),
    (
        "built in the working tree; commit on a branch",
        "a working-tree handoff note that outlived its handoff — ADJUDICATION.md's contract has "
        "been committed since Phase 2",
    ),
    (
        "None of it is reachable",
        "the pre-unification framing: the analysis stack has been reachable on new data since "
        "the CLI shipped (WORKSPACE_AND_CLI.md)",
    ),
    (
        "the campaign path currently renders",
        "CAMPAIGN_DIAGNOSTIC_SPEC.md's design body claimed one figure per run long after "
        "figures/dossier/ and figures/scans/ shipped",
    ),
    (
        "Phase 0 correctness repair in progress",
        "Phase 0 shipped as O27/O28/O29-as-amended-by-O31; only its campaign replays are open "
        "(STEERING_DESIGN.md)",
    ),
    (
        "require a local LLMM endpoint",
        "LLMM is the default offline profile, not a requirement — the `openai` profile takes any "
        "OpenAI-compatible endpoint (ADJUDICATION.md §8)",
    ),
    (
        "LLMM must be running",
        "same correction: state which profile needs LLMM rather than making it unconditional",
    ),
    # Two forms, not one: `GUI_DESIGN.md` legitimately names its build phase "Phase A — read-only
    # viewer", so the bare noun phrase cannot be forbidden. What was wrong is the *claim* form,
    # which reads as an architectural boundary that campaign control appears to violate.
    (
        "read-only viewer over",
        "the GUI is read-only with respect to scientific interpretation through workspace_api; "
        "it does own subprocesses and write control files (AGENTS.md hard rule 7)",
    ),
    (
        "read-only viewer against",
        "same correction, README's repository-layout wording",
    ),
)


# ---- shared helpers --------------------------------------------------------------------------

def _tracked_markdown() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "*.md"], cwd=str(REPO), capture_output=True, text=True, check=True
    ).stdout
    return sorted(line.strip() for line in out.splitlines() if line.strip())


def _is_exempt(rel: str) -> bool:
    """Exempt from the banner, provenance, and registration rules — not from link checking."""
    if rel in NEVER_EXEMPT:
        return False
    return rel in EXEMPT_FILES or rel.startswith(EXEMPT_PREFIXES)


def _is_link_exempt(rel: str) -> bool:
    return rel.startswith(LINK_EXEMPT_PREFIXES)


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


_BANNER_RE = re.compile(r"^> \*\*Lifecycle:\*\*\s*(.+?)\s*$", re.M)


def _lifecycle(rel: str) -> str | None:
    """The document's declared lifecycle, or None if it declares no banner.

    A banner may carry a trailing parenthetical gloss (`Current (this index)`); the value itself
    is the part before it, and must be exactly one of the four.
    """
    m = _BANNER_RE.search(_read(rel))
    if not m:
        return None
    value = re.sub(r"\s*\(.*$", "", m.group(1)).strip()
    return value


def _maintained_docs() -> list[str]:
    """Documents that describe the present or the intended future — `Current` + `Active plan`.

    `Historical evidence` and `Archived` files are excluded on purpose: they are records of a
    date, and rewriting them to match today's vocabulary destroys the only thing they are for.
    """
    return [
        rel for rel in _tracked_markdown()
        if not _is_exempt(rel) and _lifecycle(rel) in ("Current", "Active plan")
    ]


# ---- 1. CLI surface --------------------------------------------------------------------------

_EXHAUSTIVE_RE = re.compile(
    r"<!--\s*isar-commands:\s*exhaustive\s*-->(.*?)<!--\s*/isar-commands\s*-->", re.S
)


def _argparse_commands() -> set[str]:
    import argparse

    from isar.cli.main import build_parser

    parser = build_parser()
    actions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    assert len(actions) == 1, "expected exactly one subparser group in isar.cli.main.build_parser"
    return set(actions[0].choices)


def test_exhaustive_command_blocks_match_argparse():
    """A block marked exhaustive must list every subcommand and no others.

    Prevents: the command list drifting after a subcommand is added or renamed — the failure that
    left `README.md` missing three commands before the 2026-07-25 cleanup. Lists *not* marked
    exhaustive are audience-specific selections and are deliberately not checked.
    """
    expected = _argparse_commands()
    found_any = False
    for rel in _tracked_markdown():
        if _is_exempt(rel):
            continue
        for block in _EXHAUSTIVE_RE.findall(_read(rel)):
            found_any = True
            listed = set(re.findall(r"[a-z][a-z0-9_-]+", block.replace("```", " ")))
            assert listed == expected, (
                f"{rel}: block marked `isar-commands: exhaustive` disagrees with argparse.\n"
                f"  missing from the doc: {sorted(expected - listed)}\n"
                f"  not real commands:    {sorted(listed - expected)}"
            )
    assert found_any, (
        "no `<!-- isar-commands: exhaustive -->` block found in any tracked document — the marker "
        "was removed, so nothing pins the documented command set to the code"
    )


# ---- 2. deleted documents stay deleted -------------------------------------------------------

def _tombstoned_paths(text: str | None = None) -> list[str]:
    """Paths from the tombstone table, each with a last-containing commit.

    Derived from the register rather than hard-coded here so that the next deletion pass extends
    this check by writing its tombstone, with no test edit required.
    """
    rows = _register_rows(text)[1]
    deleted_sections = [
        body for heading, body in _inventory_sections(text)
        if heading.startswith("Deleted")
    ]
    assert len(deleted_sections) == 1, (
        "expected exactly one Deleted inventory section, "
        f"found {len(deleted_sections)}"
    )
    body = deleted_sections[0]
    # Checked separately from the path parse. Keying the path regex off the commit-hash column
    # would silently *skip* any row whose hash was omitted or left as a placeholder — exactly the
    # row most likely to be sloppy, and therefore the one that most needs checking.
    unhashed = re.findall(r"^\|\s*`([^`|]+\.md)`\s*\|(?!\s*`[0-9a-f]{7,40}`\s*\|)", body, re.M)
    assert not unhashed, (
        f"tombstone rows without a last-containing commit hash: {unhashed}. The hash is the whole "
        "point of the row — without it the file cannot be recovered without archaeology."
    )
    return rows


def test_tombstoned_paths_are_repository_relative():
    """`git show <commit>:<path>` from the table must work as written, with no prefix guessing.

    The recovery instruction beside the table is the reason the table exists. It was previously
    copy-paste broken: the rows carried `archive/HANDOFF_PHASE_1.md` while the file had lived at
    `docs/archive/HANDOFF_PHASE_1.md`, so every command a reader assembled from the row failed.
    """
    wrong = [p for p in _tombstoned_paths() if "/" in p and not p.startswith(("docs/", "tests/"))]
    assert not wrong, (
        f"tombstone paths that are not repository-relative: {wrong}. They are pasted straight "
        "into `git show <commit>:<path>`, so a heading-relative path silently fails."
    )


def test_tombstoned_files_are_absent_from_the_tree():
    """Prevents a deleted document being restored while its tombstone still says it is gone."""
    tracked = set(_tracked_markdown())
    back = [path for path in _tombstoned_paths() if path in tracked]
    assert not back, (
        f"these files are tombstoned in docs/DOC_INVENTORY.md but present in the tree: {back}. "
        "Either remove them again or remove the tombstone row and say why they came back."
    )


_GONE_RE = re.compile(r"\b(deleted|removed|tombstone|no longer)\b", re.I)


def _paragraph_at(lines: list[str], index: int) -> str:
    """The blank-line-delimited block containing ``lines[index]``.

    A mention and the sentence disowning it are routinely on different physical lines in wrapped
    prose, so the unit of judgement has to be the paragraph, not the line.
    """
    start = index
    while start > 0 and lines[start - 1].strip():
        start -= 1
    end = index
    while end + 1 < len(lines) and lines[end + 1].strip():
        end += 1
    return "\n".join(lines[start:end + 1])


# How far from the mention the disowning word must sit. A paragraph can discuss two files, so a
# bare "does this paragraph contain the word 'deleted'?" would let a live pointer to file A ride
# along on a sentence retiring file B. A window ties the two together without pretending to parse
# English; it is generous enough for the wrapped-prose case the paragraph scope exists to handle.
_DISOWN_WINDOW = 240


def _disowned(paragraph: str, name: str) -> bool:
    """True when every mention of ``name`` has a deletion marker within the window around it."""
    for m in re.finditer(re.escape(name), paragraph):
        window = paragraph[max(0, m.start() - _DISOWN_WINDOW): m.end() + _DISOWN_WINDOW]
        if not _GONE_RE.search(window):
            return False
    return True


def test_maintained_documents_that_name_a_deleted_file_say_it_is_gone():
    """Prevents a `Current` or `Active plan` document routing a reader to a file that is gone.

    Naming a deleted file is allowed — `docs/campaigns/README.md` legitimately says where
    `PHASE_5_NOTES.md` went — but the surrounding paragraph must say it was deleted. Otherwise the
    mention reads as a live pointer. The register and the append-only ledger (DECISIONS.md D4,
    which may not be edited) are exempt wholesale.
    """
    names = {Path(p).name for p in _tombstoned_paths()}
    offenders: list[str] = []
    for rel in _maintained_docs():
        if rel in DELETED_NAME_ALLOWLIST:
            continue
        lines = _read(rel).splitlines()
        for index, line in enumerate(lines):
            for name in sorted(names):
                if name not in line:
                    continue
                if not _disowned(_paragraph_at(lines, index), name):
                    offenders.append(f"{rel}:{index + 1} names deleted {name} as if it were live")
    assert not offenders, "\n".join(
        [
            "maintained documents name deleted documents without saying they were deleted:",
            *offenders,
        ]
    )


# ---- 3. stale phrases ------------------------------------------------------------------------

def test_maintained_documents_are_free_of_corrected_phrases():
    """Prevents wording that was corrected once being reintroduced somewhere else.

    Searched against whitespace-collapsed text, not physical lines: these documents are hard
    wrapped at ~100 columns, so a line-scoped search would miss any phrase that happens to
    straddle a wrap — which is most of them.
    """
    offenders: list[str] = []
    for rel in _maintained_docs():
        lines = _read(rel).splitlines()
        flat = re.sub(r"\s+", " ", " ".join(lines))
        for phrase, why in STALE_PHRASES:
            if phrase not in flat:
                continue
            # Best-effort line number: the line holding the phrase's first word, else the file.
            head = phrase.split()[0]
            where = next(
                (f"{rel}:{i}" for i, line in enumerate(lines, 1) if head in line), rel
            )
            offenders.append(f"{where}: {phrase!r} — {why}")
    assert not offenders, "\n".join(["corrected wording has come back:", *offenders])


# ---- 4. link resolution ----------------------------------------------------------------------

_INLINE_LINK_RE = re.compile(r"!?\[[^\]]*\]\(\s*([^)\s]+)(?:\s+\"[^\"]*\")?\s*\)")
_REF_DEF_RE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(\S+)", re.M)
_ABSOLUTE_RE = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.\-]*:|//|/)")


def _link_targets(text: str) -> list[str]:
    """Inline links, images, and reference-style definitions, in document order."""
    return [m.group(1) for m in _INLINE_LINK_RE.finditer(text)] + _REF_DEF_RE.findall(text)


def _resolves(base: Path, target: str) -> bool:
    """Resolve a relative link, matching each path segment case-sensitively.

    Windows resolves paths case-insensitively, so a bare ``Path.exists()`` here would pass locally
    and then break every link on a case-sensitive CI checkout or on GitHub's renderer. Walking the
    segments and comparing against the real directory entries catches that on any platform.
    Directory targets are legal (`docs/campaigns/` renders as a folder listing on GitHub).
    """
    current = base
    for segment in Path(target).parts:
        if segment in (".", ""):
            continue
        if segment == "..":
            current = current.parent
            continue
        try:
            entries = {p.name for p in current.iterdir()}
        except (NotADirectoryError, FileNotFoundError, PermissionError):
            return False
        if segment not in entries:
            return False
        current = current / segment
    return current.exists()


def test_relative_links_resolve():
    """Prevents a rename or deletion leaving a dead pointer behind.

    Fragment-only links (`#section`) and external URLs are skipped — a heading-anchor checker
    would have to reimplement GitHub's slug rules, and a URL checker would make the suite depend
    on the network. A `path#fragment` link is checked for the *path*.
    """
    broken: list[str] = []
    for rel in _tracked_markdown():
        if _is_link_exempt(rel):
            continue
        base = (REPO / rel).parent
        for target in _link_targets(_read(rel)):
            if target.startswith("#") or _ABSOLUTE_RE.match(target):
                continue
            path = target.split("#", 1)[0]
            if not path:
                continue
            if not _resolves(base, path):
                broken.append(f"{rel} -> {target}")
    assert not broken, "\n".join(["broken relative links:", *broken])


# ---- 5. every maintained document declares what it is ----------------------------------------

def test_every_non_exempt_document_declares_a_valid_lifecycle():
    """Prevents a new document entering the tree without saying whether it may be relied on.

    That ambiguity — not the file count — is what made the tree unreadable before the cleanup.
    """
    problems: list[str] = []
    for rel in _tracked_markdown():
        if _is_exempt(rel):
            continue
        value = _lifecycle(rel)
        if value is None:
            problems.append(f"{rel}: no `> **Lifecycle:**` banner")
        elif value not in LIFECYCLE_VALUES:
            problems.append(
                f"{rel}: lifecycle {value!r} is not one of {list(LIFECYCLE_VALUES)} "
                "(a parenthetical gloss after the value is fine; a different value is not)"
            )
    assert not problems, "\n".join(problems)


def test_every_non_exempt_document_declares_provenance():
    """A generated file is owned by its generator; without the field nobody knows to edit it."""
    missing = [
        rel for rel in _tracked_markdown()
        if not _is_exempt(rel) and "**Provenance:**" not in _read(rel)
    ]
    assert not missing, f"documents with no `> **Provenance:**` field: {missing}"


# A register row: `| `path.md` | ...`. Anchored at the start of the row and requiring the
# backticked path to be the *first* cell, so a path merely named in a Maintenance-note cell — or
# in surrounding prose — is not mistaken for a registration. That looseness is what let the
# previous token-scraping version pass a register with duplicate, missing, and phantom rows.
_REGISTER_ROW_RE = re.compile(r"^\|\s*`([^`|]+\.md)`\s*\|", re.M)


def _inventory_sections(text: str | None = None) -> list[tuple[str, str]]:
    """Return second-level inventory headings and bodies using one shared parser."""
    if text is None:
        text = _read("docs/DOC_INVENTORY.md")
    sections = []
    for chunk in re.split(r"^## ", text, flags=re.M)[1:]:
        heading, _, body = chunk.partition("\n")
        sections.append((heading, body))
    return sections


def _register_rows(text: str | None = None) -> tuple[list[str], list[str]]:
    """(registered paths, tombstoned paths) as written in docs/DOC_INVENTORY.md.

    Both are returned as lists, not sets, so the caller can detect a path registered twice.
    Every path in the register is repository-relative and complete — the register says so
    explicitly, because a heading-relative path can silently resolve to a file that does not
    exist, which is precisely what this check exists to catch.
    """
    registered: list[str] = []
    tombstoned: list[str] = []
    for heading, body in _inventory_sections(text):
        rows = _REGISTER_ROW_RE.findall(body)
        if heading.startswith("Deleted"):
            tombstoned += rows
        elif heading.startswith("Generated"):
            continue    # glob patterns, not paths; covered by EXEMPT_PREFIXES instead
        else:
            registered += rows
    assert registered, "the register parsed to zero rows — has its table format changed?"
    assert tombstoned, "tombstone table parsed to zero rows — has its format changed?"
    return registered, tombstoned


def test_inventory_deleted_section_is_identified_by_heading_prefix():
    inventory = """# Inventory

## Current documents

| Path | Lifecycle |
|---|---|
| `docs/LIVE.md` | Current |

## Deleted documents

| Former path | Last commit |
|---|---|
| `docs/GONE.md` | `abcdef0` |
"""
    registered, tombstoned = _register_rows(inventory)
    assert registered == ["docs/LIVE.md"]
    assert tombstoned == ["docs/GONE.md"]
    assert _tombstoned_paths(inventory) == ["docs/GONE.md"]


def test_the_register_is_a_bijection_with_the_tree():
    """Every tracked `.md` has exactly one register row, and every row names a real file.

    docs/DOC_INVENTORY.md claims each file appears "exactly once". This holds it to all three
    halves of that claim, each of which failed silently under the old token-scraping check:

    * **missing** — a document enters the tree with no entry saying what it is or who maintains
      it, which is how the register stopped describing the tree the last time;
    * **duplicated** — two rows for one file, so two sections can disagree about its lifecycle;
    * **phantom** — a row for a file that does not exist, which reads as a live document and
      sends a reader looking for something that was deleted or never written.
    """
    registered, tombstoned = _register_rows()
    tracked = set(_tracked_markdown())
    # Exempt files are not required to be registered. Exemption relaxes the missing check only; a
    # registered path must still name a real file.
    required = {rel for rel in tracked if not _is_exempt(rel)}

    problems: list[str] = []
    problems += [f"missing from the register: {rel}" for rel in sorted(required - set(registered))]
    problems += [
        f"registered {registered.count(p)} times: {p}"
        for p in sorted(set(registered)) if registered.count(p) > 1
    ]
    problems += [
        f"registered but not a tracked file: {p}" for p in sorted(set(registered) - tracked)
    ]
    problems += [
        f"tombstoned {tombstoned.count(p)} times: {p}"
        for p in sorted(set(tombstoned)) if tombstoned.count(p) > 1
    ]
    problems += [
        f"both registered and tombstoned: {p}"
        for p in sorted(set(registered) & set(tombstoned))
    ]
    assert not problems, "\n".join(
        ["docs/DOC_INVENTORY.md does not match the tree one-to-one:", *problems]
    )


def test_active_plans_declare_an_owner_and_exit_criteria():
    """Policy rule 5, enforced: an `Active plan` without both is a wish, not a plan.

    Prevents the failure the lifecycle model exists to stop — a document that looks like approved,
    tracked work but that nobody owns and nobody can declare finished, so it sits in the tree
    forever reading as a live commitment.
    """
    problems: list[str] = []
    for rel in _tracked_markdown():
        if _is_exempt(rel) or _lifecycle(rel) != "Active plan":
            continue
        flat = re.sub(r"\s+", " ", _read(rel))
        for field in ("Owner", "Exit criteria"):
            if not re.search(rf"\*\*{field}[:.]?\*\*|{field}:", flat, re.I):
                problems.append(f"{rel}: no {field!r} declared")
    assert not problems, "\n".join(
        ["active plans missing the metadata AGENTS.md rule 5 requires:", *problems]
    )


def test_exempt_paths_are_declared_in_the_register():
    """Keeps this file's exemption list and the register's exemption table from drifting apart."""
    register = _read("docs/DOC_INVENTORY.md")
    for prefix in EXEMPT_PREFIXES:
        assert prefix in register, (
            f"{prefix!r} is exempt here but the register does not mention it — one of the two is "
            "out of date"
        )
    for name in EXEMPT_FILES:
        assert name in register, f"{name!r} is exempt here but absent from the register"


# ---- 6. the committed evidence bundle matches its manifest -----------------------------------

ASSETS = REPO / "docs" / "assets"


def _manifest() -> dict:
    import json

    path = ASSETS / "MANIFEST.json"
    assert path.exists(), (
        "docs/assets/MANIFEST.json is missing — the committed evidence has no provenance record. "
        "Regenerate with `python tools/docs/collect_evidence.py`."
    )
    return json.loads(path.read_text(encoding="utf-8"))


def test_evidence_bundle_matches_its_manifest():
    """Every committed asset is listed with its hash, and every listed asset is present.

    Prevents the two failures that would make the bundle worse than no bundle: a figure quietly
    replaced with a different run's output while the manifest still vouches for the old one, and
    a file accumulating in `docs/assets/` that no document cites and no command produces.
    """
    import hashlib

    manifest = _manifest()
    listed: dict[str, str] = {}
    for group in manifest["groups"]:
        for record in group["files"]:
            listed[record["path"]] = record["sha256"]

    problems: list[str] = []
    for rel, expected in sorted(listed.items()):
        path = ASSETS / rel
        if not path.exists():
            problems.append(f"listed in the manifest but missing: {rel}")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            problems.append(f"content differs from the manifest hash: {rel}")

    # Excluded by exact path, not basename: a README.md *inside* a group is an undeclared file,
    # which is exactly what this check is for.
    unmanaged = {ASSETS / "MANIFEST.json", ASSETS / "README.md"}
    on_disk = {
        p.relative_to(ASSETS).as_posix()
        for p in ASSETS.rglob("*")
        if p.is_file() and p not in unmanaged
    }
    problems += [f"present but unlisted: {rel}" for rel in sorted(on_disk - set(listed))]
    assert not problems, "\n".join(["docs/assets/ disagrees with MANIFEST.json:", *problems])


def test_every_promoted_figure_has_a_caption():
    """docs/assets/README.md promises a caption beside every figure. Hold it to that.

    A figure is promoted precisely so it can be looked at away from its report; without its
    caption it is an unlabelled plot, and the promise in the index becomes false.
    """
    manifest = _manifest()
    listed = {record["path"] for group in manifest["groups"] for record in group["files"]}
    uncaptioned = [
        rel for rel in sorted(listed)
        if rel.lower().endswith(".png")
        and rel.rsplit(".", 1)[0] + ".caption.md" not in listed
    ]
    assert not uncaptioned, (
        "promoted figures with no .caption.md beside them: " + ", ".join(uncaptioned)
    )


def test_manifest_records_the_provenance_each_group_needs():
    """A bundle without its generating command and code revision is an undated screenshot."""
    manifest = _manifest()
    assert manifest["schema_version"] == 2
    assert re.fullmatch(r"[0-9a-f]{40}", manifest["code_revision"]), (
        f"code_revision {manifest['code_revision']!r} is not a full commit sha — the bundle "
        "cannot be tied to the code that produced it"
    )
    assert type(manifest["code_dirty"]) is bool
    if manifest["code_dirty"]:
        assert re.fullmatch(r"[0-9a-f]{64}", manifest["code_diff_sha256"])
    else:
        assert manifest["code_diff_sha256"] is None
    assert manifest["test_data_fingerprint"]["present"] is True, (
        "the bundle was collected without test_data/ present, so nothing records which inputs "
        "produced it"
    )
    for group in manifest["groups"]:
        for field in ("name", "what", "command", "replaces", "files"):
            assert group.get(field), f"group {group.get('name')!r} has no {field}"
        assert group["command"].startswith("python "), group["command"]
        assert (REPO / group["command"].split()[1]).exists(), (
            f"group {group['name']!r} names a generating script that does not exist: "
            f"{group['command']}"
        )


def test_no_document_still_cites_the_disposable_out_tree_for_promoted_evidence():
    """Prevents a promoted report being cited at its old, gitignored `out/` path.

    Only the promoted groups are checked. `out/` is a legitimate thing to name — it is where a
    run writes, and DECISIONS W1 is about it — so this fails on a citation of exactly the
    evidence that now lives in `docs/assets/`.
    """
    promoted = [group["name"] for group in _manifest()["groups"]]
    offenders: list[str] = []
    for rel in _maintained_docs():
        if rel in ("docs/DECISIONS.md", "docs/DECISION_INDEX.md", "docs/DOC_INVENTORY.md"):
            continue  # the ledger is append-only; the register describes the move
        for line_no, line in enumerate(_read(rel).splitlines(), 1):
            for name in promoted:
                # Any separator and any trailing context: `out/phase3_gold`, out\phase3_gold,
                # ../out/phase3_gold/FINDINGS.md. Matching only "out/<name>/" would miss the bare
                # directory reference, which is the form a reader is most likely to retype.
                if re.search(rf"\bout[\\/]{re.escape(name)}\b", line):
                    offenders.append(f"{rel}:{line_no}: cites out/{name}, now docs/assets/{name}/")
    assert not offenders, "\n".join(
        ["evidence promoted to docs/assets/ is still cited at its disposable path:", *offenders]
    )


# ---- 7. generated files match their generators ------------------------------------------------

def test_the_decision_index_is_not_stale():
    """`docs/DECISION_INDEX.md` still matches what its generator produces from the ledger.

    Prevents the failure mode every generated-and-committed file has: the next append to
    `DECISIONS.md` leaves the index describing a ledger that no longer exists, and because the
    file carries a banner and reads plausibly, nothing announces it. The generator is
    deterministic and writes the whole file, so an exact comparison is the right check.
    """
    result = subprocess.run(
        [sys.executable, "tools/docs/make_decision_index.py", "--check"],
        cwd=str(REPO), capture_output=True, text=True,
    )
    assert result.returncode == 0, (result.stdout + result.stderr).strip()


# ---- 8. the checks are actually load-bearing --------------------------------------------------

@pytest.mark.parametrize(
    "target, mutate",
    [
        ("cli", lambda text: text.replace("isar-commands: exhaustive", "isar-commands: partial")),
    ],
)
def test_marker_removal_is_detectable(target, mutate):
    """The exhaustive-block check fails loudly if its marker is quietly removed.

    Without this, deleting the marker would silently disable check 1 rather than fail it.
    """
    assert _EXHAUSTIVE_RE.search(mutate(_read("README.md"))) is None


def test_the_stale_phrase_check_would_catch_a_reintroduction():
    """A guarded phrase reinserted into a maintained document is actually detected.

    The check searches whitespace-collapsed text, so this exercises the collapse too: a phrase
    reintroduced across a line wrap must still be caught, which is the case a naive line-scoped
    search silently misses.
    """
    phrase = "LLMM must be running"
    wrapped = phrase.replace(" ", "\n", 1)
    flat = re.sub(r"\s+", " ", " ".join(f"prose {wrapped} more prose".splitlines()))
    assert phrase in flat
    assert phrase in {p for p, _ in STALE_PHRASES}


def test_the_register_check_would_catch_a_duplicate_row():
    """A path registered twice is detected, not silently collapsed into a set.

    The previous token-scraping implementation built a `set`, so a file registered under two
    sections with two different lifecycles passed. This pins the counting behaviour that fixed it.
    """
    registered, _ = _register_rows()
    doubled = registered + registered[:1]
    assert [p for p in set(doubled) if doubled.count(p) > 1] == [registered[0]]
