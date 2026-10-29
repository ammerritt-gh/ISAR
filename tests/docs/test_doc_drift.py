"""Drift checks that keep the documentation tree honest about itself.

These test the *mechanically checkable* half of the documentation policy in `AGENTS.md`. They do
not judge prose. Each check below names the specific failure it prevents, because a doc test that
cannot say what it is for becomes a chore that people delete.

The model (2026-08-20 rebuild): every maintained document declares a **status** — `draft` (being
designed, do not rely on it), `live` (current and authoritative), or `terminal` (its terminal
trigger has fired; it is awaiting absorption and deletion) — and `docs/READING_GUIDE.md` is the
**single map**: every document is reachable from it, directly or through one linked index. There
is no separate register; git history is the archive.

Deliberately **not** tested:

* Whether a live document's *claims* still match the code — that is the absorption/review
  discipline, and no checker can judge prose against behaviour.
* A generated CLI reference. Check 1 compares against argparse rather than generating prose,
  because a generated command reference is a file nobody reads that still has to be regenerated.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GUIDE = "docs/READING_GUIDE.md"

STATUS_VALUES = ("draft", "live", "terminal")

# Paths exempt from the status-banner and map-coverage rules: generator-owned outputs and fixtures.
EXEMPT_PREFIXES = (
    "docs/stats/figures/",       # caption sidecars, written by docs/stats/make_figures.py
    "docs/assets/",              # promoted report output, written by the docs/*_check.py scripts
    "tests/data/golden/",        # byte-compared fixtures; a banner would break the comparison
    "docs/feedback/",            # session debrief records, written by tooling and deleted at harvest
    "docs/transcripts/",         # session transcripts, written by tooling
)
EXEMPT_FILES = (
    "CLAUDE.md",                 # a 12-byte `@AGENTS.md` stub
)
# `docs/DESIGN_GOALS.md` is owner-authored and off-limits to agent tooling, so no status banner is
# demanded of it — but it must still appear on the map; exempting it from coverage would let the
# most important document in the tree fall off the front door.
BANNER_ONLY_EXEMPT_FILES = (
    "docs/DESIGN_GOALS.md",
)
# Carve-outs: hand-written index files that happen to sit under an exempt prefix. Without these,
# adding one generated directory would silently stop checking its own README.
NEVER_EXEMPT = (
    "docs/assets/README.md",
)

# Link checking is exempt separately and far more narrowly. A file being generator-owned is a
# reason not to demand a banner of it; it is *not* a reason to let its links rot — a promoted
# FINDINGS.md whose figures were not promoted with it is precisely the failure this bundle
# exists to prevent.
LINK_EXEMPT_PREFIXES = (
    "tests/data/golden/",        # points into a generated workspace that is not in the tree
)

# Phrases that were wrong and were corrected. Each entry records what it prevents coming back.
# Applied only to maintained documents (see _maintained_docs) — a `terminal` record is allowed,
# indeed required, to keep the wording that was accurate on its date.
STALE_PHRASES = (
    (
        "keeps drawing the fast-pass fit",
        "G27 makes Scan(s) default to Fit: None and draw only the stored pass chosen by the user",
    ),
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
        "--deep-budget-minutes, default 60",
        "the deep pass has no time budget by default (2026-10-27); only a typed "
        "--deep-budget-minutes sets one",
    ),
    (
        "safety_budget_s",
        "the fast pass has no time budget (2026-10-27); the setting was removed",
    ),
    (
        "LLMM must be running",
        "same correction: state which profile needs LLMM rather than making it unconditional",
    ),
    (
        "Choose gpi folder",
        "the button is *gpi files…* since 2026-10-29 (USING_ISAR; design/gui.md §0nonies)",
    ),
    # Two forms, not one: `GUI_DESIGN.md` legitimately names its build phase "Phase A — read-only
    # viewer", so the bare noun phrase cannot be forbidden. What was wrong is the *claim* form,
    # which reads as an architectural boundary that campaign control appears to violate.
    (
        "read-only viewer over",
        "the GUI shows what ISAR has recorded and sends the user's actions to ISAR; it interprets "
        "no science (AGENTS.md hard rule 7, rewritten 2026-10-27)",
    ),
    (
        "read-only viewer against",
        "same correction as the entry above, README's repository-layout wording (AGENTS.md hard "
        "rule 7, rewritten 2026-10-27)",
    ),
    (
        "The GUI reads science, it never writes science",
        "hard rule 7 was replaced 2026-10-27: the GUI sends the user's actions to ISAR; the invariant "
        "is that what the user sees is what ISAR uses",
    ),
    (
        "awaits separate O42 release review",
        "H1 retired the O42 release review; the scan-window authoritative selector becomes "
        "default-on once the SCENE world suite exercises it",
    ),
    (
        "O42 release review pending",
        "same correction, the heading form used in the EIG slice list",
    ),
    # STAT-2 closeout (S13): H4 made the evidence posterior the production selection authority.
    # Narrow claim forms only — "retain-AIC disposition" as a historical fact about S8 stays legal.
    (
        "AIC remains production behavior",
        "H4 made the Bayesian evidence posterior the production selection authority; AIC is a "
        "persisted diagnostic (the D6 shadow lane stays AIC-labeled by design)",
    ),
    (
        "S8 retains AIC",
        "S8 was superseded by H4; its diagnostic content survives, the authority claim does not "
        "(S13 records the STAT-2 closeout)",
    ),
    (
        "STAT-2c remains the first possible authority switch",
        "the STAT-2c authority switch was executed as H4 (SCENE-1 PR-b); S13 closed STAT-2",
    ),
    (
        "content-addressed interpretation artifact",
        "H11(a) retired the per-scan interpretation artifact before it existed; the scene reads "
        "the persisted candidate rows and embeds them in the revision",
    ),
    (
        "invocation sites (PR 3c) remain Planned",
        "SCENE-3 PR 3c landed as H14: the store, the workspace_api read surface, the observe-only "
        "driver hook and `isar report --scene` are Implemented",
    ),
    (
        "parameter_within_tolerance/1 — **never closes in v1**",
        "SCENE-4.2a A2 gave parameter_within_tolerance/2 a real closure path off the A1 "
        "mode_intervals; the blanket v1 claim no longer describes the evaluator",
    ),
    (
        "unreachable for phenomenon-bearing campaigns until the EstimateParameter interval "
        "re-baseline",
        "EstimateParameter's interval re-baseline landed (A2 + PR 32 wave W1/W2); phenomenon-"
        "bearing campaigns reach `complete` like any other (docs/SCENE_DESIGN.md §5)",
    ),
    (
        "the confirmed tile's partition has a candidate declaring an explicit identity",
        "P1-G rescoped ClassifyIdentity's representability gate to the confirmed tile's own "
        "matched clusters over RETAINED alternatives, not a partition-wide raw-candidate read",
    ),
    (
        "authority carries the REALIZED `selected_for_delivery`",
        "P0-C made interval authority fit-conditional (`authority_basis == \"producer_sound\"`); "
        "delivery selection is not interval authority",
    ),
    (
        "where NO per-class track has actually DERIVED",
        "H19(b) superseded H18(f)'s derivation-keyed trigger with any-confirmed-tile-"
        "unclassified existence, full-partition span, and fully-classified suppression "
        "(docs/SCENE_DESIGN.md §5)",
    ),
    (
        "calls it observe-only after",
        "H20(a) superseded H14(d)'s observe-only driver hook: the analysis child builds no scene "
        "at all, and the campaign parent invokes the one canonical build per delivery through the "
        "supervised `isar scene-build` subprocess (docs/SCENE_DESIGN.md §6)",
    ),
    (
        "(resolution floor, bose_dead, instrument geometry",
        "H20(c) shrank the durable-blocker vocabulary to {instrument_geometry_infeasible, "
        "program_boundary}; resolution_floor and bose_dead were deleted from both pinned "
        "vocabularies — they are EIG worthwhileness questions (SCENE-5), never blockers. The "
        "bose_dead DETECTABILITY reason in the scene scorer is different vocabulary and survives",
    ),
    (
        "Still absent: the consultation channel",
        "the consultation channel and the production durable-blocker producer both landed in "
        "SCENE-4.3 (C7/C8, H20(c)/(f)); MEASUREMENT_STEERING.md §7.2b's gap list described the "
        "state at C5",
    ),
    (
        "counting currency is the named open item",
        "RULED 2026-08-18 (H20(e) amended in place): discovery exposure is 10^8 monitor neutrons "
        "via the frozen discovery_monitor_ncount threshold, declared directly in the wire's own "
        "currency — nothing left to convert",
    ),
    (
        "30 s intent is recorded verbatim in the proposal meta",
        "the 30 s/point intent and its proposal-meta stamp were superseded and deleted by the "
        "direct 10^8-neutron declaration (H20(e), C10) — no vestige remains",
    ),
    (
        "stays opt-in until SCENE-4 delivers the default-on flip",
        "SCENE-4.4 (H22, 2026-08-21) landed the flip: authoritative selection is default-on at "
        "compile time for newly compiled targeted objectives, not opt-in",
    ),
    (
        "the `\"shadow\"` mode",
        "H22(b) retired shadow as a `scan_window_eig_mode` value; the name now survives only on "
        "the (retained) scoring engine's modules",
    ),
)


# ---- shared helpers --------------------------------------------------------------------------

def _tracked_markdown() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "*.md"], cwd=str(REPO), capture_output=True, text=True, check=True
    ).stdout
    return sorted(line.strip() for line in out.splitlines() if line.strip())


def _is_banner_exempt(rel: str) -> bool:
    if rel in NEVER_EXEMPT:
        return False
    return (
        rel in EXEMPT_FILES
        or rel in BANNER_ONLY_EXEMPT_FILES
        or rel.startswith(EXEMPT_PREFIXES)
    )


def _is_map_exempt(rel: str) -> bool:
    """Exempt from the READING_GUIDE coverage rule. The guide does not need to map itself."""
    if rel in NEVER_EXEMPT:
        return False
    return rel == GUIDE or rel in EXEMPT_FILES or rel.startswith(EXEMPT_PREFIXES)


def _is_link_exempt(rel: str) -> bool:
    return rel.startswith(LINK_EXEMPT_PREFIXES)


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


_BANNER_RE = re.compile(r"^> \*\*Status:\*\*\s*(.+?)\s*$", re.M)


def _status(rel: str) -> str | None:
    """The document's declared status, or None if it declares no banner.

    A banner may carry a trailing parenthetical gloss (`live (this index)`); the value itself is
    the part before it, and must be exactly one of the three.
    """
    m = _BANNER_RE.search(_read(rel))
    if not m:
        return None
    return re.sub(r"\s*\(.*$", "", m.group(1)).strip()


def _maintained_docs() -> list[str]:
    """Documents a reader may rely on or is being asked to review — `draft` + `live`.

    `terminal` files are excluded on purpose: they are awaiting absorption and deletion, and
    rewriting them to match today's vocabulary destroys the record being absorbed.
    """
    return [
        rel for rel in _tracked_markdown()
        if not _is_banner_exempt(rel) and _status(rel) in ("draft", "live")
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
        if _is_banner_exempt(rel):
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


# ---- 2. stale phrases ------------------------------------------------------------------------

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
            head = phrase.split()[0]
            where = next(
                (f"{rel}:{i}" for i, line in enumerate(lines, 1) if head in line), rel
            )
            offenders.append(f"{where}: {phrase!r} — {why}")
    assert not offenders, "\n".join(["corrected wording has come back:", *offenders])


# ---- 3. link resolution ----------------------------------------------------------------------

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


# ---- 4. every document declares what it is ---------------------------------------------------

def test_every_non_exempt_document_declares_a_valid_status():
    """Prevents a new document entering the tree without saying whether it may be relied on.

    That ambiguity — not the file count — is what made the tree unreadable before the 2026-07
    cleanup, and the 2026-08 rebuild keeps the mechanical guarantee while shrinking the
    vocabulary to three values.
    """
    problems: list[str] = []
    for rel in _tracked_markdown():
        if _is_banner_exempt(rel):
            continue
        value = _status(rel)
        if value is None:
            problems.append(f"{rel}: no `> **Status:**` banner")
        elif value not in STATUS_VALUES:
            problems.append(
                f"{rel}: status {value!r} is not one of {list(STATUS_VALUES)} "
                "(a parenthetical gloss after the value is fine; a different value is not)"
            )
    assert not problems, "\n".join(problems)


def test_plans_declare_an_owner_and_exit_criteria():
    """A plan without both is a wish, not a plan.

    Prevents the failure the plans/ directory exists to stop — a document that looks like
    approved, tracked work but that nobody owns and nobody can declare finished, so it sits in
    the tree forever reading as a live commitment.
    """
    problems: list[str] = []
    for rel in _tracked_markdown():
        if not rel.startswith("docs/plans/") or _is_banner_exempt(rel):
            continue
        flat = re.sub(r"\s+", " ", _read(rel))
        for field in ("Owner", "Exit criteria"):
            if not re.search(rf"\*\*{field}[:.]?\*\*|{field}:", flat, re.I):
                problems.append(f"{rel}: no {field!r} declared")
    assert not problems, "\n".join(["plans missing required metadata:", *problems])


# ---- 5. the single map covers the tree -------------------------------------------------------

def _canonical_targets(rel: str) -> set[str]:
    """Repository-relative paths this document links to (files or directories)."""
    base = (REPO / rel).parent
    out: set[str] = set()
    for target in _link_targets(_read(rel)):
        if target.startswith("#") or _ABSOLUTE_RE.match(target):
            continue
        path = target.split("#", 1)[0]
        if not path:
            continue
        try:
            resolved = (base / path).resolve().relative_to(REPO.resolve())
        except ValueError:
            continue
        out.add(resolved.as_posix())
    return out


def test_every_document_is_reachable_from_the_reading_guide():
    """`docs/READING_GUIDE.md` is the single map: every document is on it, directly or through
    one linked index (`campaigns/README.md` covers its directory, and so on).

    Prevents the orphan-file failure the retired register used to catch: a document enters the
    tree, nothing routes a reader to it, and it silently stops being documentation. One hop is
    allowed so that a directory of records needs an index row, not a row per record.
    """
    direct = _canonical_targets(GUIDE)
    covered = set(direct)
    for rel in sorted(direct):
        if rel.endswith(".md") and (REPO / rel).exists():
            covered |= _canonical_targets(rel)

    orphans = [
        rel for rel in _tracked_markdown()
        if not _is_map_exempt(rel) and rel not in covered
    ]
    assert not orphans, "\n".join(
        [
            "tracked documents not reachable from docs/READING_GUIDE.md (directly or one hop):",
            *orphans,
        ]
    )


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
        "the bundle was collected without real_data/ present, so nothing records which inputs "
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
    run writes — so this fails on a citation of exactly the evidence that now lives in
    `docs/assets/`.
    """
    promoted = [group["name"] for group in _manifest()["groups"]]
    offenders: list[str] = []
    for rel in _maintained_docs():
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


# ---- 7. the checks are actually load-bearing --------------------------------------------------

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
