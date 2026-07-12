# HANDOFF — The ISAR operator workflow: where the mouse path ends today

**Written:** 2026-07-11, at the close of the GUI Phase 0+A build (branch `ISAR-initial-GUI`).
**Purpose:** discussion document for planning the next round. The question on the table is not
"what does the GUI do" but **"what is the deliverable?"** — the complete workflow by which a
human with a mouse gets into ISAR and actually uses it. This document walks that workflow step by
step, marks honestly where the mouse path exists and where it dead-ends into a terminal, and ends
with the decisions to make.

---

## 0. Fresh-session orientation (read this first)

- **Branch state:** `ISAR-initial-GUI`, 33 commits over `main` (base `1dbaf9b`), UNMERGED,
  fully review-gated, 1246 not-slow tests green. Final whole-branch review verdict: mergeable.
  Landing decision (merge vs PR) is still open with the owner.
- **What the branch adds:** GUI Phases 0+A per `docs/GUI_DESIGN.md` — a Qt-free core
  (`isar/workspace_api` read facade, `isar/geometry` vendored TAS math + lattice epochs,
  `isar/plotspec` shared rendering-semantics layer) and a read-only PySide6 viewer (`isar_gui`:
  workspace browser, fit inspector, plane heatmap, dispersion, waterfall, live file-watch).
  Phase B (measurement planner) and Phase C (directives/steering) are designed but NOT built.
- **Key records:** design = `docs/GUI_DESIGN.md` (+ §0bis corrections) and `docs/DECISIONS.md`
  G1–G17; build ledger = `.superpowers/sdd/progress.md` (per-task outcomes + deferred minors);
  plan file = `~\.claude\plans\begin-planning-the-isar-shimmering-garden.md`; session memory =
  `isar-gui-phase0a` (architecture anchors).
- **Verify anything:** tests run ONLY via
  `%LOCALAPPDATA%\micromamba\micromamba.exe run -n tavi-dev python -m pytest -q -n 30 -m "not slow"`
  (bare env python crashes numpy on DLL delay-load). GUI launch: double-click `run-isar-gui.bat`.
  Reference workspace: `C:\Users\AMM\Documents\ISAR_baselines\mvp_zone_test16` (read-only).

---

## 1. The deliverable, stated properly

ISAR's deliverable is **a triage system a scientist operates during a beamtime**: data goes in,
honest fits and verdicts come out, the human corrects and steers, and eventually ISAR proposes
and drives measurements itself. The GUI built this round is the *window* onto that system. The
question for this discussion: **how much of the system's front door should be mouse-operable,
and in what order do we build it?**

The per-repo Operator Contract sets the bar: an operator (possibly a non-technical colleague)
must be able to run the thing cold, mouse-only, and find the output. Below is the whole journey
measured against that bar.

## 2. The workflow, step by step (human with a mouse)

Legend: ✅ mouse-only today · ⚠️ terminal required today · ❌ not built (designed for B/C)

**Step 0 — Get it running.** ✅ Double-click `run-isar-gui.bat` → the GUI opens (last workspace,
or an empty window with File → Open Workspace…). Failure stays visible (console pauses).
⚠️ *But*: on a clean machine there is no environment-rebuild `.bat` — reaching "running" from
nothing requires micromamba/pip commands. Operator Contract gap.

**Step 1 — Create an experiment.** ⚠️ `isar init "D:\beamtime\numors\055182" --out my_experiment`
in a terminal. It detects the instrument, scaffolds the workspace + card, prints next steps.
The GUI cannot create a workspace, and File → Open only opens *existing* ones. This is the
first hard dead-end for a mouse-only operator — the front door is CLI.

**Step 2 — Describe the experiment.** ⚠️ Edit `card.policy.toml` in a text editor: detector
list, known phonon energies, bad scans, fitting guidelines. The card is deliberately
human-owned and well-commented (this is genuinely decent UX for a text file), but it is still
a text file. No GUI card editor exists (and a full one is likely Phase-C-adjacent, since card
edits are the correction channel).

**Step 3 — Analyze data.** ⚠️ `isar run "D:\beamtime\numors" --workspace my_experiment` in a
terminal; re-run the same command as scans land overnight (idempotent, only new scans fit).
The GUI cannot trigger a run. Note the design boundary here: the GUI is deliberately not a
second fitter of record (G2/G7) — but *triggering* the engine is not the same as *being* the
engine, and "run ISAR on this folder" is the single most operator-shaped action in the system.

**Step 4 — See what ISAR found.** ✅ This is what we just built, and the mouse path is complete:
open the workspace → browse groups → click a scan → data + fit + residuals, interval ladder
labeled by tier, AIC candidate table, verdict/provenance → heatmap with declared projection,
measured strips, gaps as gaps, entropy-colored ω̂ overlay → dispersion with prior/branch
curves → waterfall by temperature. If a campaign or `isar run` is writing the workspace
*right now*, the views grow live (file watch). All rendering rules (unreliable-never-whiskers,
pooled honesty, truth-gated legends) are enforced structurally and by tests.

**Step 5 — Correct and steer the analysis.** ⚠️ Today: edit the card (add an exclusion, a
guideline, a prior hint) in a text editor, re-run `isar run`. The GUI shows everything but
changes nothing — by design, until Phase C's directive channel (append-only inbox → driver
validates → ledger ack) gives corrections a mouse path with provenance.

**Step 6 — Plan measurements.** ❌ Phase B: the reciprocal-space planner (click two points in
the oriented plane, see spacing, amend, approve → objective spec that seeds `isar drive`).
Designed (C6, geometry layer already built for it), not started.

**Step 7 — Run an autonomous campaign.** ⚠️ `isar drive --goal "..." --workspace DIR` in a
terminal, with TAVI and LLMM running (each launched separately; TAVI has its own run-bat).
The GUI *can* watch a live campaign (step 4 live mode) but cannot start, nudge, or stop one
(❌ Phase C: drive dashboard, directives, NL command bar).

**Step 8 — Take away results.** ✅/⚠️ `report/report.html` + `results.csv` land in the
workspace at predictable paths (Contract satisfied on findability); the GUI shows the live
figures, and the report is regenerated by runs — but there's no "open report" button or
export-current-view action in the GUI yet (small).

## 3. The honest summary

**The mouse journey today starts at Step 4.** Everything before it — install, create, describe,
run — requires a terminal, and everything after it (correct, plan, drive) is designed but
unbuilt. What we shipped this round is a complete, honest, live *observatory*; the operator's
*hands* are still CLI. That was the deliberate Phase-A scope (read-only first, so nothing could
destabilize the pending TAVI SVP work) — but it means "can a colleague use ISAR cold,
mouse-only?" is still **no** at the front door.

## 4. Options for the next round (to discuss)

**(a) Operator on-ramp first** — make Steps 0–3 mouse-only *without* violating G2 (the GUI stays
a thin shell that invokes the same engine):
   - New Experiment wizard: pick data file/dir → GUI calls the `isar init` machinery → opens the
     scaffolded workspace → surfaces the card in an editor pane (even a raw-TOML pane with the
     card's own comments is workable v1).
   - "Run analysis" action: pick data dir (remembered), run `isar run` as a subprocess with
     live log in the log dock + progress; re-run button. Watch layer already makes results
     appear live as the run writes.
   - Environment-rebuild bat + (optional) TAVI/LLMM co-launch buttons with health checks.
   - Cheap, high leverage, mostly plumbing to code that already exists. Fills the Operator
     Contract gap end-to-end for the *analysis* half of ISAR.

**(b) Phase B planner next** (per the standing plan): the measurement-authoring half — RS map,
two-click line, forbidden-zone preview (kinematics-only until dark-angle data exists),
approve → objective spec. Makes the *beamtime* story mouse-first, but the operator still can't
get data in or runs started without a terminal.

**(c) Phase C directives** — the biggest lift (driver inbox/queue/priorities don't exist), and
the design itself says to finalize payloads against code at that time. Probably still last.

My read: (a) then (b). (a) is small (roughly one to two of this round's task-sizes), directly
answers "how does a human actually get INTO the system," and every later phase benefits from a
GUI that can open, create, and run. (b) remains the plan of record after it. But this is
exactly the decision this handoff exists to discuss.

## 5. Questions for the owner

1. **Who is the operator?** You at the beamline; a colleague; both? (Sets how much of Step 2 —
   card editing — needs GUI affordances vs "a commented TOML is fine.")
2. **Scope of (a):** wizard + run button + rebuild bat — anything else that belongs in the
   on-ramp? (e.g., "open report.html" button, export-view-as-PNG, TAVI/LLMM co-launch?)
3. **Raw data files in the GUI:** you asked to "load workspaces, data files or anything" — is
   dragging a raw numor/spec file onto ISAR (auto-init a workspace around it) the desired v1
   of that, or is folder-based init enough?
4. **Landing:** the branch is merge-ready — local merge to main, or PR? And should the next
   round branch from main-after-merge or continue on this branch?
5. **The full S30 M=200 validation gate** (pre-existing, still pending per USING_ISAR.md) —
   schedule it alongside GUI work or keep decoupled?

## 6. Pointers (for whichever session picks this up)

- GUI design + corrections: `docs/GUI_DESIGN.md` · decisions: `docs/DECISIONS.md` (G1–G17)
- CLI workflow the GUI wraps: `docs/USING_ISAR.md` · reading order: `docs/READING_GUIDE.md`
- Build ledger with all deferred minors: `.superpowers/sdd/progress.md`
- Import boundaries (enforced): `tests/test_import_contract.py`, `tests/test_qt_free_core.py`
- Launch: `run-isar-gui.bat` · engine entry the GUI would wrap for (a): `isar/cli/main.py`
  (`init_cmd`, `run_cmd` — the wizard shells the same code paths, never reimplements)

---

## 7. DECISIONS (owner ruling, 2026-07-11) — binding for the next round

**Direction: option (a) first, then (b).** Scope and doctrine below.

### D1 — The TAVI seam (dependency doctrine)

Anything in ISAR that touches TAVI requires **TAVI installed on its own** — ISAR
never bundles, vendors, or manages TAVI. Build cleanly around that seam:

- Separate, explicitly, "runs on data / on a machine without TAVI" (parse, fit,
  verdict, viewer, reports) from "requires TAVI" (simulations, campaigns,
  `isar drive`).
- ISAR owns its own environment spec and rebuild path; it must not depend on
  `tavi-dev` existing. TAVI-requiring features detect a TAVI installation and
  degrade to a clear "TAVI not found — these features need TAVI installed"
  state, never a crash.
- Health-check indicators + a launch button that invokes TAVI's own `run-*.bat`
  are in scope; lifecycle ownership of TAVI is not.

### D2 — Who the operator is (Q1)

Both, but the binding constraint is **physics-literate, program-naive**. Training
is acceptable; hidden mechanics are not — the workflow must be visibly clear in
the GUI itself. Card stays a commented TOML with a raw editor pane in v1 (a real
card editor is Phase-C-adjacent, since card edits are the correction channel).

### D3 — Scope of (a) (Q2)

Wizard + Run action (subprocess, live log dock, visible failure state, retained
logs, "Re-run (only new scans)") + environment-rebuild bat (per D1: ISAR-own,
micromamba-flavored; steal TAVI's installer bootstrap pattern) + the cheap
Contract wins: **Open report** button, **export current view as PNG**.

**LLM flexibility is a hard requirement:** other machines most likely will NOT
run LLMM. Wherever ISAR calls an LLM, route through a configurable
OpenAI-compatible endpoint; LLMM at `127.0.0.1:8766` is the *default profile*,
not an assumption. Every feature must declare itself LLM-optional or
LLM-required and degrade visibly (mirror the D1 seam pattern: absent backend →
clear status, never a crash).

### D4 — Physicality doctrine (Q3) — promoted, not sugar

The owner's framing, now binding: physicality means **anything that maintains
persistent state and a relative configuration is an object you can pick up and
move** — membership, ordering, grouping, placement. Files are the most familiar
instance, not the definition (Dungeon Weaver's roster — drag a character
between groups, out, or in — and drag-to-reorder prompts are the house
reference). The arrangement on screen *is* the state: round-trip honest, read
back from persistence, never a drifted view. Where state is expressible as
position/membership/order, prefer manipulating the representation over a config
field. Concretely for (a):

- Dragging a data folder or raw file onto the ISAR window is the **front door**:
  the drop invokes the New Experiment wizard, prefilled. The wizard also exists
  standalone (File → New Experiment…), but the drop is the headline path.
- "Workspace" must be presented in human terms: *an experiment = your pile of
  data files, wrapped*. The GUI should never require the operator to already
  know what a workspace is before they can create one.
- Study Dungeon Weaver's direct-manipulation patterns before designing the
  drop/wizard flow.
- Forward note (not (a)-scope): the doctrine's natural ISAR extensions live in
  Phase B/C — e.g. dragging scans out of an analysis as the exclusion gesture,
  or reordering measurement priorities — and should be designed as directives
  with provenance, not as silent state edits.

### D5 — Landing (Q4)

**PR, not local merge** — owner will put reviewers on it. Tag the pre-PR branch
point. Next round branches from main after the PR lands.

### D6 — Validation gate (Q5)

The full S30 M=200 validation is **decoupled from (a)** (pure plumbing, never
touches fitting) but is a **hard gate on Phase C**: no steering autonomy on an
engine that hasn't passed full validation. Sequence: (a) → validation → (b) → C,
validation parallel to (a) if machine time permits.
