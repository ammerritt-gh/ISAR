# ISAR GUI — Design

> **Status:** live
> **Authority:** canonical GUI + workspace_api presentation design
> **Verified against code revision:** `7b71614` (2026-10-28)

**Status:** Design draft, 2026-07-11, substantially built. This document follows the discipline of
`../ARCHITECTURE.md`: nothing is described in the implemented voice unless it is. Because roughly the first
half of the plan has shipped and the second half has not, the as-built matrix below records
verified implementation detail and mirrors the capability status in `../ARCHITECTURE.md` §15. It does not
override that canonical status table. The Planned voice of the body text predates the build and is
preserved as the design record.

### As-built status (verified against `isar_gui/` and `isar/`, 2026-10-28)

| Phase / capability | Status | Evidence |
|---|---|---|
| **Phase 0** — Qt-free workspace read API + geometry layer | **Implemented** | `isar/workspace_api/` (`workspace.py`, `views.py`, `watch.py`, `evaluate.py`), `isar/geometry/`, Qt-free-core test |
| **Phase A** — read-only viewer, surviving surfaces C1–C5; Waterfall page superseded by G27 | **Implemented** | `views/browser_dock.py`, `inspector_widget.py`, `heatmap_pane.py`, `dispersion_pane.py` |
| **Pass 3** — workflow layout (center view host, versioned dock layout) | **Implemented** (G18); its window arrangement and layout fallback are superseded by G24 | `views/center_host.py`, `main_window.py` |
| **Pass 3** — campaign workspace: launcher, live feed, cooperative stop | **Implemented** (G19) | `views/campaign_dock.py`, `control/stop-<token>.json` protocol |
| **Pass 3** — compact campaign monitor | **Implemented** (G20) | `views/campaign_monitor.py` |
| **Desktop window** — one arrangement in every state: scan list on the left, Scan(s)/Plane/Dispersion/Sandbox in the centre, Processing and Campaign on the right, Log and Output below (G24, G27) | **Implemented** (2026-10-28); laptop human walkthrough remains unrun | `main_window.py`, `views/experiment_strip.py`, `views/output_bar.py`, `views/processing_panel.py`, `tests/gui/test_window_frame.py` |
| **Experiment as a real folder** — drop-to-create, Rename, Save experiment copy…, Export results… (G25) | **Implemented** (2026-10-27) | `workspace_manager.py`, `views/files_strip.py`, `export_action.py`, `isar/report/export.py` |
| **Scan list and scan inspector** — BL43 scan-set tree, shared multi-selection, status words, filter and sort, Fit section and stored tables in the left inspector (G24, G27) | **Implemented** (2026-10-28; generated-data widget tests; no person acceptance claimed) | isar/workspace_api/scan_list.py, viewmodels/scan_list.py, views/scan_list.py, views/scan_inspector.py, theme.py, tests/gui/test_scan_list_selection.py, tests/gui/test_inspector_fit_section.py |
| **Scan(s) spectrum view** — shared selection, up to ten spectra, stored Fast/Deep curves, explicit not-drawn reasons, raw and monitor-normalised scales (G27) | **Implemented** (2026-10-28; generated-data widget coverage; person walkthrough not yet run) | `workspace_api/spectra.py`, `workspace_api/evaluate.py`, `session/deep_delivery.py`, `plotspec/builders.py`, `viewmodels/scans_page.py`, `views/scans_page.py`, `tests/gui/test_scans_page.py` |
| **Left side, stage two** — setup section, *Choose curves folder…*, *Experiment info…*, needs-attention list, fact table, out-of-date marks, readiness line (G24, G25) | **Implemented** (2026-10-27; job `ja0cd202ecb67`, on the intake backend of job `jfada74237892`); user values for Q and other facts stay pinned, and one layout limit is open (§0quinquies) | `viewmodels/intake.py`, `views/setup_section.py`, `views/details_dialog.py`, `views/attention_list.py`, `views/value_entry.py`, `views/fact_table.py`, `views/readiness_line.py`, `tests/gui/test_stage2_walkthrough.py` |
| **Analysis queue** — the Processing panel's queue: analyzer files queued by hand or as they arrive, fast before deep, continuous on the cores ISAR is given, one writer per experiment; BL43LXU only (G26, §0sexies) | **Implemented** (2026-10-28), tested offscreen and at the engine. The walk-through at the GUI threshold by a person is **not yet run**. Open: a queued fit does not refresh the curves check the setup section shows (§0sexies) | `isar_gui/queue_controller.py`, `isar_gui/views/queue_panel.py`, `isar/session/analysis_queue.py`, `isar/session/analysis_service.py`, `isar/session/file_fit.py`, `isar/session/writer.py`, `isar/session/currentness.py`; `tests/gui/test_window_queue.py`, `test_queue_panel.py`, `test_queue_controller.py` |
| **Read-only display grammar** — WISH-1/2/3/9 | **Implemented** (G23) | `isar/workspace_api/display.py`, `isar/plotspec/`, plane/dispersion/inspector viewmodels and panes |
| **Phase B** — planner (C6): objective authoring, forbidden-zone rendering | **Planned** | no objective editor or planner module exists |
| **Phase C** — directives: inbox, drain protocol, NL compilation, dispersion editor | **Planned** | no directive or inbox machinery exists |
| **Phase C** — sandbox interactive refitting (G7) | **Partial** | `views/sandbox_pane.py` + `viewmodels/sandbox.py` exist; no refit path implemented |
| **Phase C** — drive dashboard, per-q live state | **Partial** | campaign observation shipped ahead of Phase C (§0ter); the per-q half is GUI-6 in `TODO.md`, blocked on an event-contract change |
| §16 roadmap (7 items) | **Planned** | tracked as GUI-1…GUI-7 in `TODO.md` |

Corrections found during the Phase 0/A build are in §0bis; the Pass-3 rulings (G18–G20) are in
§0ter; §16 lists what Pass 3 deliberately left designed-but-unbuilt.

**Audience.** This document is a self-contained handoff. It assumes the reader knows `../ARCHITECTURE.md`
(the master design) but has **no access to the design conversation** that produced this document.
All rulings from that conversation are restated here in full; nothing load-bearing is implied.

**Relationship to other documents.** Subordinate to `../ARCHITECTURE.md` (its principles P1–P6 bind this
document). Sibling to `docs/WORKSPACE_AND_CLI.md` (workspace/session internals the GUI reads)
and `design/steering.md` (the drive loop the GUI observes and nudges). Decision IDs
introduced here use the **G-series** as stable names; rulings are recorded in this document as
dated amendments (the retired ledger's originals are in git history).

---

## 0. Summary of rulings (the fixed plan)

These were decided in design review and are the constitution of this document. Each is expanded
in the referenced section.

| ID | Ruling | § |
|---|---|---|
| G1 | GUI lives in the ISAR repo as a separate package, optional install extra, launched `isar view`. Boundary is enforced in **imports**, not repo topology. | §2 |
| G2 | The GUI is a **viewer and a proposer, never a third source of truth**. It reads workspace artifacts; it writes only append-only directives through the existing correction/mutation channel. | §3 |
| G3 | Three artifact classes: **immutable observations**, **versioned interpretations**, **append-only directives**. The GUI writes only class three. | §3 |
| G4 | Every GUI write compiles to exactly one of **four directive types**: objective edit, prior edit, fit-structure correction, direct proposal. Natural language is a fifth *input device*, not a fifth type. | §4 |
| G5 | Transport is a **directive inbox**: `workspace/inbox/` of timestamped JSON, drained by the driver at the top of each decide step, moved to the ledger. Atomic write-temp-then-rename. | §5 |
| G6 | **The driver remains the sole owner of the instrument.** Human directives are queue entries with priority `human` that still pass the driver's validate step (triangle closure, dark angles, limits). Append and nudge; never seize. | §5 |
| G7 | **Sandbox exception**: for interactive refitting the GUI may import the deterministic fit engine's frozen-config entry point, in-process, for live preview. Sandbox results are ephemeral and visually marked; **commit** writes a directive and the official number comes from the workspace re-run. | §6 |
| G8 | GUI snapshots and report/diagnostic figures share PlotSpec semantics and plot builders, with matplotlib and pyqtgraph renderers. Scan(s), Plane and Dispersion use the same read-only plotting grammar. | §7 |
| G9 | 4D honesty: every 2D projection in Plane or Dispersion is declared on screen (plane plus integration window). Measured strips render as strips, gaps as gaps; modeled surfaces are labeled. Scan(s) plots individual 1D spectra and does no projection. | §8 |
| G10 | Semantic grouping is the card's collation policy; changing it is a card edit. Scan(s) Offset/Overlay, pass choice and Raw/Normalised scale are display state only and never alter collation or fit records. | §9 |
| G11 | Coordinate math: a **metric/UB layer** (lattice + orientation vectors u, v → scattering plane, r.l.u. ↔ Å⁻¹, non-rectilinear axes) lives in **core, Qt-free**, sourced from the TAS_MCP / TAVI math. TAVI stays the sole resolution authority; the GUI grows **no third opinion** on scattering triangles — feasibility is consulted, not reimplemented. | §10 |
| G12 | Donor repositories: ACIDD (cut gesture, coordinate-layer-under-image-view, templates, session persistence), PUFFIN (live-parameter fitting grammar, MVVM + worker threads), ISAR (everything that thinks). Scan(s) shared selection and per-experiment group chips use ISAR's own contracts; all three donors are GPL-3. | §11 |
| G13 | Phasing: Phase 0 workspace read API → Phase A read-only viewer → Phase B planner → Phase C directives/sandbox/live dashboard. Read-only first so nothing destabilizes the pending TAVI SVP. | §13 |
| G23 | **Individual fits show evidence; pooled masters show the current answer; only a revision-matched assessment can certify.** State colouring is the default; one shared persisted action switches plane and dispersion to mutually exclusive entropy colouring. Strip colour and latest-acquisition highlighting have one backend-neutral semantic contract. | §7.3, §8 |
| G24 | One arrangement in every state: scan list and inspector on the left, four central pages (Scan(s), Plane, Dispersion, Sandbox), Processing and Campaign on the right, Log and Output below. | §0quinquies, §0septies |
| G25 | **The experiment is a real folder.** Drop-to-create under `Documents\ISAR`, Rename, Save copy as…, Export… through `isar export`, no temporary workspace. | §0quinquies |
| G27 | Scan(s) is the spectra view over the scan list's shared selection; it draws stored data and chosen stored Fast/Deep curves only, with explicit limits and no scientific fallback. Saved groups are per-experiment view controls. | §0septies |
| G26 | **The Processing panel is the analysis queue.** Analyzer files are queued by hand or as they arrive and run continuously on the cores ISAR is given, one writer per experiment; no queued pass has a time limit; every control has a plain tooltip (the GUI threshold). | §0sexies |

---

## 0bis. Corrections from Phase 0/A implementation (2026-07-11)

Phase 0 + Phase A implementation surfaced five points where the design record above needed
correction. These layer on top of the original text (a design record is not rewritten); each
affected § carries a one-line pointer back here.

1. **The ledger is two artifacts, not one** (§4.4, §5.1, §7.1). `session.json` is the analysis
   **snapshot** (`SessionState`, rebuildable from re-runs). The append-only event stream is a
   separate file, `report/session_log.jsonl` (driver events); `ws.session_events()` reads the
   JSONL, not `session.json`. The future `directive.consumed` event (Phase C) is a new event
   kind in the JSONL stream, not a field added to `session.json`.
2. **Lattice/orientation is a versioned epoch series, not card policy** (§10). This supersedes
   "from the card's lattice": scans stay in absolute instrument coordinates, immutable.
   `CardState.lattice_epochs` (a class-two artifact, precedent: `resolution_versions`) maps
   absolute → HKL. Epochs change mid-experiment (transition, realignment, plane swap) and can be
   corrected retroactively — append a new epoch, re-derive HKL, never edit the scans — and
   analysis/grouping keys on HKL, not the acquisition-time orientation. Implemented: schema +
   stateless resolver in `isar/geometry/epochs.py` (identity-through-acquisition-epoch until
   absolute coordinates land in Phase B). See G15.
3. **`evaluate_model`/`reconstruct_curve` reality** (§7.2). `reconstruct_curve` re-solves
   amplitudes by NNLS against counts — it is not a pure evaluator of stored parameters.
   Resolution: `FitOutcome.amplitudes` is now persisted additively; `ws.evaluate_model` is pure
   (stored-params → curve, no refit) when amplitudes exist on the fit outcome, and falls back to
   the counts-based `reconstruct_curve` path on older ledgers that predate the field. Both share
   `assemble_curve` (`isar/report/audit.py`) so the two paths cannot diverge in math.
4. **matplotlib is a hard dependency, not Qt-free-by-extension** (§2, §14). The campaign path is
   matplotlib-free at import time only. `tests/test_qt_free_core.py` enforces Qt-freeness
   (`PySide6`/`pyqtgraph` never load on `import isar`) and a scipy-free `isar.workspace_api`
   import (scipy loads lazily, only on the `evaluate_model` audit fallback) — it does not assert
   matplotlib-freeness, since `isar/plotspec/backends/mpl.py` is explicitly allowed to import
   matplotlib.
5. **Dark angles / mechanical limits have no numeric data source yet** (§10). TAS_MCP's PUMA
   limits are `None`/soft placeholders; TAVI has no dark-angle configuration; TAVI's assembled
   `/validate` verdict is Qt-entangled (HTTP-only, not importable in-process). Consequence:
   planner forbidden-zone previews (Phase B) will be kinematics-only (triangle closure / Ewald
   sphere) until a per-instrument config is authored TAVI-side — the HTTP `/validate` call stays
   the sole authority for the real go/no-go, exactly as G11 already ruled.

---

## 0ter. Pass-3 rulings (2026-07-16) — layout + campaign workspace

GUI Pass 3 reshaped the window into the left-to-right operator workflow (data enters top-left →
scans organized on the left → display surfaces in the center → the campaign workspace on the
right → a compact Log ticker) and built the campaign launcher/dashboard. Its rulings, in the
G-series (this document is their canonical home; the retired ledger's originals are in git
history):

- **G18 — Center view host (original page set, superseded by G27).** The original fit-inspector, Waterfall, Plane, Dispersion and Sandbox surfaces were pages of one central host, not five competing docks. G27 replaces that page set with Scan(s), Plane, Dispersion and Sandbox and keeps the fit inspector in the left panel. Auto-follow selection to Scan(s) and campaign start to Plane remains user-overridable; display switching is view state. Saved dock layouts remain version-gated and fall back atomically to the current-screen default.
- **G19 — Campaign observation and control.** The GUI *observes* a campaign exclusively through
  workspace artifacts (`ws.session_events()` over `report/session_log.jsonl` — the §7 watch
  promise, now exercised; the terminal `campaign_summary.json` only after the loop ends) and
  *controls* it only as the sole-writer `isar drive` subprocess behind the single-writer
  coordinator. **Stop is a cooperative, token-scoped, append-only control request**: the
  coordinator mints a per-invocation token (`--control-token`), Stop writes an immutable
  `report/control/stop-<token>.json`, and the loop honors its own token at the iteration
  boundary, exiting with the first-class `operator_stopped` state (CLI exit 4) — certification
  skipped, final grid recorded honestly as `grid_state`, nothing ever deleted. Tree-kill
  (Job-Object `kill_tree`, best-effort with a synchronous-taskkill fallback) is an explicit
  operator escalation, never automatic. This is a *control request*, not a directive — the
  Phase-C directive inbox/schema is untouched (§15 item 4's door stays open; this is its
  minimal precursor). Resume never re-compiles a goal: `--resume-latest` reuses the frozen
  objective from the last `campaign_start` event, and start fingerprints
  (`source: {kind, sha256}`) make Resume-vs-Start-anyway an honest, comparable decision.
  Fingerprints decide *confidence of sameness*, never resumability: an explicit
  "Resume last campaign" control stays reachable when no comparable fingerprint exists
  (pre-fingerprint logs, a moved spec file, a freshly typed goal), since the frozen objective
  alone is sufficient. Tier is frozen with the objective — the form disables it when the
  primary action is a verified Resume, and the launcher never emits `--tier` on a resume.
  **A campaign needs no data and no workspace** — the drive engine is cold-start capable (lazy
  `isar init` after its first scan), so the Campaign dock exists in every window state and a
  Start with no workspace at all runs the create → launch → promote → adopt lifecycle: the
  window first creates an ISAR-named experiment, as a drop does (amended 2026-10-27, G25; it
  reserved a temporary directory before), launches the campaign into it, and polls
  `WorkspaceManager.promote_pending_if_ready` (all three init files, identity-checked against
  the armed root+generation) until the workspace the campaign created opens; the coordinator
  then rebases the running job onto the new generation (`adopt_drive_binding` — control token
  untouched, so cooperative Stop spans the promotion). The window never inspects workspace
  files; readiness detection lives in the manager. A campaign that dies before its first scan
  leaves the experiment pending and retryable.
- **G20 — Compact campaign monitor.** Compact mode is an explicit mode (*View ▸ Compact mode*), not a
  responsive collapse; a laptop-sized window does not start in it (G24): same window, dock layout stashed in memory, central stack swapped to a glanceable
  card (status, current scan, budget, mini plane, log ticker, cooperative Stop). Read-only; the
  session file always carries the full layout.

Live campaign state shows **only what the ledger supports** (iteration per invocation,
converged-of-grid aggregates, whole-log spend, scan verdicts from results rows). Per-q live
state needs an additive event-contract change first (§16.6).

### 0quater. Read-only display grammar (2026-07-27)

G23 implements WISH-1, WISH-2, WISH-3, and WISH-9 without changing fitting, steering, workspace
interpretation, or scientific persistence. The Qt-free `isar.workspace_api.display` module owns
assessment matching, interval authority, fit-display state, and latest-completed-acquisition
selection. `isar.plotspec` carries directional errors, composed glyph tokens, semantic regions,
and strip colour-map ownership; matplotlib and pyqtgraph map those tokens without reclassifying
science. The GUI contributes only session-persisted display mode and colourbar interaction.

### 0quinquies. The left-side arrangement and the experiment (2026-10-27)

The design for the le Tacon beamtime was approved with four amendments on 2026-10-27. The committed
mockup, `mockups/2026-10-27-isar-left-side.html` with its laptop frame
`mockups/2026-10-27-isar-left-side-laptop.png`, is the reference for look and arrangement; where it
differs from this text, this text governs. This section supersedes G18's programmatic default and its
empty/ready layout pair, and amends G19 as stated below.

- **G24 — one arrangement in every state.** The window keeps the same arrangement from first launch
  onward; nothing rearranges when the first data arrives. Left column, top to bottom: the experiment
  strip, the files strip, then the scan list, which takes the remaining height and runs past the
  bottom bar. Centre: the display (the G18 page set as it stood then; G27 supersedes it with Scan(s), Plane, Dispersion and Sandbox). Right panel:
  *Processing* (the default tab) and *Campaign* (the G19 content, unchanged), which collapses to a
  slim *Processing ‹* rail. Bottom bar, under the centre and the right panel only: Log on the left,
  Output on the right (G25). The scan inspector is part of the left panel: opening it widens the left
  panel beside the scan list, and its ✕ pulls the panel back; the display gives and regains that
  width. The old Files, Scans, Browser, Card and Scan Table panels are parked, hidden by default and
  reachable from *View ▸ Panels*; their code is unchanged. The centre's unit-mode metadata panel now
  lives in the scan inspector, and a raw unit in the display shows its counts plot alone (operator,
  amendment 2: "like the LHS panel just extending out a bit when inspecting a scan ... It's not a
  column on its own"). The first start picks the arrangement from the screen's available area:
  desktop only at 1800 × 1000 logical px or more, otherwise laptop, which starts with the right panel
  collapsed. A saved arrangement is restored only when it is version 4, its recorded size fits the
  screen and Qt accepts it; otherwise the default for the current screen is used and one Log line says
  why. *View ▸ Reset layout* returns to the default for the current screen. `run-isar-gui.bat` starts
  the GUI with `pythonw`, so no console window opens.
  **Known limit, accepted (2026-10-27; superseded by G27).** The earlier 86 px plot measurement applied to the former fit page while its side tables shared the centre column. G27 moves stored fit tables to the left inspector and gives Scan(s) the plot area. No new narrow-screen plot width is claimed here; the laptop-size human walkthrough remains unrun.

- **G25 — the experiment is a real folder.** With no experiment open, a drop, *Add files…* or
  *Add folder…* creates one at once. It is named after the dropped folder (or, for loose files, their
  common name), placed under `Documents\ISAR\` (Windows' Documents folder), with a numeric suffix when
  the name is taken, and the files are read into it. A single dropped experiment folder with no
  experiment open is opened, not copied. A one-time note in the experiment strip says ISAR named it and
  offers *Rename*; it can be dismissed. *New experiment…* creates `Experiment_<YYYY-MM-DD>` the same way.
  There is no temporary workspace and no unsaved state: the folder exists from creation, and what ISAR
  computes is written there as it arrives (the Output bar says where and when). With an experiment
  open, a drop anywhere in the window is read into it.
  - *Rename* changes the folder's name and the experiment's name together, completely or not at all.
    It is refused, with the reason shown, while an engine command or an export writes in the folder,
    and when the experiment holds its own source data files, since its manifest names them inside the
    folder and a rename would leave ISAR unable to find them. When the experiment has fits and its
    resolution curves sit inside its folder, the Rename dialog says in one line that adding those
    spectra to the queue again will refit them: ISAR recognises a curve set by its folder's path (operator,
    amendment 1: accepted).
  - *Save copy as…* copies the whole folder to a new one. The original stays the active experiment and
    new data keeps going to it; the copy is offered with *Open copy*, and the Log says what the copy
    still reads from outside its folder.
  - *Export…* runs `isar export` through the operation queue and names the new folder when it is
    done. Both passes are exported side by side, each labelled, and nothing picks one (operator,
    amendment 3).
  - *Save copy as…* and *Export…* are unavailable, with the reason beside them, while there is no
    experiment, while an ingest, a campaign or an export is writing to it, and while nothing has been
    read into it yet. (Amended 2026-10-28: a running analysis no longer makes them unavailable; they hold
    the queue's writer instead, §0sexies.)
  - Campaign *Start* with no experiment open creates an ISAR-named experiment first, as a drop does,
    and launches the campaign into it (G19, amended above).
- **Scan list (G24).** One row per scan set, labelled by its scan number when its files carry exactly
  one and otherwise by the set's name; the analyzers sit beneath it. Each status is a coloured dot and
  a word: the plain word of the display table `isar_gui/status_words.py` (clear, close call, search limit
  reached, held: needs …), with the fitting code's word and a one-line meaning on hover, and the code's word
  in the export (amendment 7; the facade still returns the code's word verbatim, and the filter matches
  both). A deep count made on provisional sampler settings reads with the mark `(provisional)` in its word and
  its hover, which says it is not yet to be trusted. *Parsed* claims only that the file was read:
  never calibration, a resolution curve or a fit. *Fast pass* and *Deep pass* show each pass's stored
  state, or `not run` where nothing is stored, side by side, with no result picked as "the" result; each
  column switches on by itself when the first stored result of that pass exists (amendment 7), unless the
  user switched it off by hand (`scan_list.hidden` in the session), which the auto rule then respects. The
  G27 supersedes that rule: Scan(s) starts at Fit: None and draws only the stored pass the user selects. A missing temperature or Q shows a dash with its reason
  on hover, and a background update keeps selection, expansion and scroll.
- **Scan inspector (G24, G27).** The inspector remains in the left panel and retains recorded scan/analyzer facts, QC flags, target Q, campaign-era command, counting mode, calibration and other unit metadata. G27 removes its former raw-counts plot and adds a Fit section for the stored pass summaries and mode values; the Scan(s) page owns spectrum plotting. Pooled-master numbers and tables remain inspectable even though Scan(s) does not draw pooled curves.

- **Stage two (Implemented, 2026-10-27; job `ja0cd202ecb67`, on the intake backend of job
  `jfada74237892`).** The left column now runs: experiment strip, setup section, needs-attention list,
  files strip, scan list. Everything below is BL43LXU only and shows what the intake backend returns;
  the window computes no gap, no readiness and no value of its own, and keeps no second store.
  - *Setup section* (`views/setup_section.py`): what was read (scan sets, analyzers, incomplete sets,
    files not read; the last two open in place) and where the resolution curves were found, with
    *Locate…*, which saves the folder the user picks at once as the experiment's resolution folder. The
    last curve check's problem is shown in the warning colour on one line, shortened with … to the
    column's width and shown in full on hover (so the scan list keeps S1's 400 px minimum with the
    attention list open); a saved folder is marked
    unchecked until the next import or `isar run` checks it, which is when a curve added or replaced in it
    is seen. *Details…* (`views/details_dialog.py`, from the experiment strip): the name with
    *Rename…*, the folder, and six optional descriptive fields (title, proposal, sample, people,
    dates, notes) that no fit reads.
  - *Needs-attention list* (`views/attention_list.py`): the backend's groups in its order and words.
    The user ticks the scan sets (or analyzers) a gap concerns and enters one value and an optional
    reason; *Save* is one all-or-nothing write on exactly the ticks, and a group closes only when the
    worker's re-read says so. Analyzer groups offer *Mark dead* and *In use*; the "Counts may not be
    raw" group also offers *Counts accepted*. After the backend's groups the list keeps up to three of
    its own, not counted: "Your temperature values", "Your slit values" (so *Use the file's value*
    stays reachable after the gap closes) and "Your accepted counts" (with *Withdraw*). It scrolls
    inside 160 px so the scan list keeps its room.
  - *Fact table* (`views/fact_table.py`) in the scan inspector: for temperature, slit, Q, the count
    origin and the analyzer, three cells (from the files, yours, used) and the per-use readiness. A
    value of yours that differs from the file's is drawn in the warning colour with both numbers on
    hover. Temperature and slit apply to the whole scan set and offer *Set my value…* and *Use the
    file's value*; the analyzer offers *Mark dead* / *In use* and, for counts that may not be raw,
    *Accept counts…* / *Withdraw acceptance*. Q and the count origin are shown only; when the files
    carry no hkl, Q shows the scan list's |Q| and says steering needs the hkl.
  - *Out-of-date mark* (`viewmodels/scan_list.py`, `views/scan_list.py`): a *Fast pass* or *Deep
    pass* word whose recorded inputs differ from today's keeps its stored text, is drawn in the
    warning colour, and names each changed input, from and to, on hover. The two passes are judged
    separately; a pass with no finished input record is not judged and never marked. The mark says
    the fit is out of date; nothing refits.
  - *Help* (`views/help_window.py`, amendment 7): *Quick guide* (`isar_gui/resources/quick_guide.html`, read
    at runtime) and *What the status words mean* (generated from `isar_gui/status_words.py`, so it cannot
    drift from the display), shown in one reused non-modal window (`show()`/`raise_()`, never `exec()`).
  - *Fast-pass all ready (N)* (`views/setup_section.py`, amendment 7, rebound 2026-10-28): a button on the setup section's button row, with a muted line for the count or the reason it is off. It holds no logic and calls `MainWindow.start_analysis()`, the one entry point that the Run menu entry, the *Processing* button and the needs-attention and readiness lists also call. It adds a fast pass for every ready analyzer file that has no current one (§0sexies). The amber curves line, when shown, stands in for the muted line only when the missing curves are the whole reason it is off; any other reason keeps its line, even where a second line squeezes the counts at 1108 px.
  - *Readiness line* (`views/readiness_line.py`) in Processing: counts of spectra ready, ready with a
    fallback, waiting for resolution curves, needing their counts accepted, running and so on, each
    count opening the list behind it with the backend's reason and the button that resolves it.
  - *Writes and reads.* The GUI writes user values, conditions and settings only through the intake
    backend's writers, in process, and never edits a value a file supplies (operator ruling,
    2026-10-27: user values are only ever added; a later value supersedes an earlier one and both stay
    on record). `IntakeState` (`viewmodels/intake.py`) is the one holder of the reads and writers. It
    re-reads on the intake watch events, after every save of its own, and after each queued result is saved (§0sexies); the fit-inputs and needs-attention reads run on a worker thread and a
    result from before a save is dropped. Every save is refused, with the reason shown, while an
    export runs or another process holds the store ("busy, try again").
  - *Counts accepted* (design amendment 5, operator-approved): the declaration that an `_i` analyzer's
    counts are raw lifts the hold on them and changes no count and no flag (an `_lr` analyzer stays
    held whatever is saved); every fit made that way records it, and *Withdraw* takes it back.
- **Pinned for later, not built here:** the analyzer overview in the centre, automatic
  export to an outside folder, `gpi.*` parsing for per-analyzer Q, and user values for Q and for facts
  other than temperature, slit and dead analyzers.

---

### 0sexies. The analysis queue and the GUI threshold (2026-10-28)

The analysis queue was designed on 2026-10-27 (job `j926265e2189c`: design steps 1–24 and the operator's decisions) and built in two slices: the engine under `isar/session/` and the window under `isar_gui/`. This section describes it as built, read from the code; where the design's wording differs, this text governs. The reference picture is `mockups/2026-10-27-isar-right-side-queue.html`, under the same rule.

- **G26 — the Processing panel is the analysis queue.** Analyzer files of a BL43LXU experiment are queued by hand, as a *Fast pass* or a *Deep pass*, or as a fast pass when they arrive. Queued work runs continuously on the cores ISAR is given: two passes of one file never run together, fast items start before deep ones, a running deep pass is stopped only by the user, and no queued pass has a time limit. A result counts as done only once the one writer has saved it in the experiment folder. Queued analysis fits analyzer files and nothing else: no pooled masters, branch identity or dispersion prior, and no language model. The window computes no validity of its own; it shows what the engine reports.
- **The GUI threshold (operator, 2026-10-27).** Every part of the window is judged against one reader: someone who knows inelastic scattering and the beamtime, perhaps knows broadly what ISAR is, has had at most a short explanation, and at 3 AM cannot reach the operator and has to work out what things do from the screen. So every control, status word, count and mark has a plain tooltip; a control that cannot be used says why in place; an essential action also has a visible label; and nothing is shown that this reader does not need to steer the analysis. The rule governs the whole GUI, not only the queue. **Status:** the tooltips are in the code. A person has **not** yet walked through the window at the laptop size with only the screen and its tooltips; that walk-through is still to be run, and its findings are recorded here when it is.
- **The panel, top to bottom.**
  - The switch *Fast-pass new scans as they arrive*. It starts on for a new experiment and is stored with that experiment. While it is on, every analyzer file read in from then on (from the watched folder or a drop, new or re-read) joins the fast queue, marked *auto*. It never adds deep passes, never touches what you added by hand, and never sweeps back over older files.
  - The selection line: one or more analyzer files from the shared scan-list selection, for example “2 analyzers selected (97 K: a06, a07)”. Pooled masters are named and not queued.
  - *Fast pass* and *Deep pass*, under *Add to queue:*. Each adds one item per selected analyzer file for that pass; pooled masters are excluded. Both are greyed, with the reason in a line beneath, when no experiment is open or no analyzer file is selected.
  - A banner in the warning colour: *Paused*, with *Resume*; "Waiting: a campaign is running in this experiment. Nothing starts until it ends."; or the analysis process's failure, with the log's location.
  - The *Fast queue* and the *Deep queue*, each headed by its counts ("1 running · 4 saving · 13 waiting", or "nothing queued"). Under each heading, one group per scan set: its label, *auto* where it was added automatically, and "N of M done" for what was added since the experiment was opened. Each analyzer is a line with a dot and a word. A group longer than six shows "… N more" and expands on click.
  - One standing note under the *Deep queue* heading while the deep pass's sampler calibration is provisional: "Deep-pass evidence calibration is provisional: all deep results carry this". It is shown once, not per item. A deep count in the scan list reads "(provisional)", and its hover points here.
  - A running analyzer shows its progress, its elapsed time and *■ Stop*; a waiting one shows *✕ Remove*. A waiting deep pass whose file's fast pass has not finished says "waits for this analyzer's fast pass".
  - *Finished*: one line per scan set and pass with its counts ("27 done · 1 held"), then each analyzer that needs a look (held, failed, stopped or done with a warning, each with its reason), then each finished deep pass with *Show*. *Clear list* tidies this list only; it changes no result and no count.
  - A footer beside *Pause* (or *Resume*): "5 analyzers running on 14 cores", with the number being saved.
- **Progress, from the steps the engine has completed.** Nothing is a percentage or an estimate, and no item is declared stalled. A fast pass shows its phase ("starting values", "trying 3 peaks (up to 5)", "comparing models", "checking the fit", "choosing the peak count", "error bars"); the peak count is a ceiling, since the search can stop short of it. A deep pass shows its stage and model: "stage 1 of 3 · model 4 of 9 · this model 3:10". The last stage's length is shown once it starts ("stage 3 · model 1 of 2"). "This model" is the time since the latest step the engine reported. A step much longer than the earlier ones may mean a stuck fit, and *Stop* ends it.
- **Adding work.** A file that already has a current result for the chosen pass, or is already waiting or running for it, is not added. The Log says which and why ("Not added: 97 K a03, a04 already have a current fast pass"). There is no "run again anyway"; a result whose inputs have changed is not current, so it is added. *Fast-pass all ready (N)* (the button at the top of the left column, the one in *Processing*, the *Run* menu entry, and the buttons in the needs-attention and readiness lists) adds a fast pass for every ready analyzer file without a current one; its label gives the count. Adding from the panel or that button first asks about an unsaved card draft. The automatic switch does not ask.
- **How the queue runs.** When a core frees, the oldest runnable fast item starts before any deep one, and a deep item starts only while no fast item runs. A fast item takes one core. A deep item takes the idle cores, then gives back those its widest sampling stage cannot use; the next item takes them, fast first. A lowered cores setting changes no running item. An item fixes its inputs when it starts, not when it is added: it reads the experiment's saved values then, and finishes as already done if its result is current at that moment. A deep item whose file has no current fast pass runs that fast stage first, in the same item; a fast result saved before the deep stage is stopped stays saved. An analyzer file is occupied from the start of an item until its result is saved, so the next item on that file starts from the saved result.
- **Done means saved.** A result reads *saving* until the writer confirms it. The writer is one thread per experiment: it saves whatever has queued up, in arrival order, under the workspace write lock, and changes only that analyzer file's result, so an import or a corrected value made meanwhile survives. A result is refused, with its reason, if its file was read in again with different data, or its fast pass was replaced, while it was being fitted. If ISAR stops before a result is saved, that result is lost and the analyzer is added again by hand. A saved result is never lost.
- **Stop, pause, remove, leave.** *✕ Remove* takes a waiting item out and asks nothing. *■ Stop* ends one running item; the others carry on. Stopping a fast pass asks nothing. Stopping a deep pass asks first ("Stop the deep pass?"), names the work that will be discarded, and says the fast-pass result stays once saved. *Pause* lets running items finish and starts nothing new until *Resume*; arrivals still join and wait. Closing ISAR, *Open…* or *Recent*, and *New experiment…* ask first when anything is running, saving or waiting, and say what would be lost ("…Leaving stops the running analyzers, discarding their work, and forgets the waiting ones."). The analysis process is then stopped in order: results already handed to the writer are saved first. Nothing from one experiment can land in another, and the queue is forgotten when the experiment is left.
  - *Rename* is refused while analyzers run or save ("press Pause and wait for the running analyzers to finish"). Once they have finished, Rename stops the idle analysis process and keeps the waiting items and the Finished list.
- **Save copy and Export while analysis runs.** Both stay available. Each holds the queue's writer: a result being saved finishes first, and results that arrive meanwhile wait as *saving* until the copy has been made, or until the export has read the results (its first progress line). Measured on the development machine, a synthetic experiment of 16,804 files and 312 MB copied in 7.4–9.3 s; a laptop has not been measured. A copy that takes about a minute or more is a reason to revisit this rule.
- **Campaigns.** A campaign does not start while analyzers are running, saving or waiting in the experiment, and the queue starts nothing while a campaign runs. Starting a campaign says why ("analyzers are waiting in the analysis queue; remove them or let them finish first"). Compiling a campaign goal is not refused; only starting it is. `isar run` and `isar drive` run by hand do not take the queue's lock and are not coordinated with it (`docs/WORKSPACE_AND_CLI.md`).
- **Cores.** *Config ▸ Cores for analysis… (N of M)*, in the menu bar, sets how many cores ISAR may use, from one to this machine's count. It defaults to the machine's cores less two (at least one) and is remembered on this machine. A lower setting takes effect as running analyzers finish; a deep pass already running keeps its cores until it is stopped.
- **Selection.** The scan list, Scan(s), inspector, saved groups and Processing queue read one shared selection. Selecting spectra alone queues nothing. *Show* in Finished replaces the shared selection with that analyzer; with *Auto* on it switches to Scan(s), and with *Auto* off it leaves the centre page unchanged.
- **Scan list marks.** While an analyzer file is in the queue, its *Fast pass* or *Deep pass* cell reads *queued*, *running* or *saving* (the panel says *waiting* for *queued*). A scan-set row's coverage ("1/28 deep") counts the analyzers that hold a current stored result for that pass, and its hover says so. Coverage is not outstanding work, and it differs from the panel's counts by design.
- **Status words.** Each is a dot and a word, never colour alone.

  | Word | Meaning |
  |---|---|
  | waiting (scan list: queued) | Added; starts when cores free up, fast passes first |
  | running | ISAR is fitting this analyzer file now |
  | saving | Finished; ISAR is writing the result into the experiment folder |
  | done | Finished and saved in the experiment folder |
  | done with a warning | Finished and saved, but ISAR reports a limit on this result; the reason is shown beside it |
  | held | ISAR did not fit this file because something it needs is missing; the reason says what. Add the file again once it is fixed |
  | failed | The fit or the save went wrong; the reason says what. Add the file again to retry |
  | stopped | Stopped by you before it finished; anything already saved stays |

- **Limits, not clocks.** No queued pass has a time limit: a pass completes its full comparison of candidate models. The fast pass's per-spectrum safety budget and the deep pass's start budget are gone (2026-10-27). A deep pass has a clock only when a budget is typed on `isar run` (`--deep-budget-minutes`); the window never sets one. A result the engine reports as limited finishes *done with a warning* and says why: the peak-count ceiling was reached, a model's sampling stayed suspect after restarts, or the engine recorded another flag. A result stored before 2026-10-27 may carry an earlier time-limit reason; it is shown with that warning as stored.
- **Held files.** A file ISAR will not fit without something it needs finishes *held* with the engine's reason, and is not retried by itself. For a missing energy zero, the reason says the energy-zero series is built by `isar run`, which the queue does not do.
- **Scope, and what is not in this version.** BL43LXU only: other workspaces get what the same code gives, and nothing is built for them. Not in this version: moving an item to the front; a queue that survives leaving the experiment; a setting for one analyzer file; out-of-date marks in the queue; automatic retry of held items; “run again anyway”; a declared stall; a precision control; the deep pass's own speed and use of many cores (fitting optimisation work).
- **Open items, as built.**
  - A saved curves folder is not checked when queued analysis starts. Queued fits read the folder's curves as each item starts, but the check the setup section shows (`report/curves.json`) runs at the next import or `isar run`. After a queued analysis has used the folder, the setup section can therefore still say the folder is not checked yet.
  - Two windows on one experiment are not refused. Each has its own queue, and the writer loses no save, but the same file may be fitted twice.
  - The human walk-through at the GUI threshold (above) has not been run.

### 0septies. Scan(s) spectrum view (2026-10-28, G27)

G27 replaces the old Waterfall central page with a four-page centre: Scan(s), Plane (q, E), Dispersion ω(q) and Sandbox. Scan(s) is the individual-spectrum view; the fit inspector stays in the left panel. This supersedes the old page list and Waterfall display examples in §0ter, §0quinquies, §1.3 and §8. The plane/dispersion projection rules and pooled-data exposure convention in §8 remain in force.

- **G27 — one shared selection.** The scan list, Scan(s), inspector, saved groups and Processing queue use one Selection. The list owns membership and active-spectrum choice; Scan(s) reads selected keys and never fits or enqueues on its own. Clicking a spectrum row selects it. Ctrl-click adds or removes it; Shift-click selects the visible range from the anchor. The row gutter opens or closes a scan set without changing selection; clicking the set row selects its analyzers. Set and analyzer rows show partial-selection counts. A filter can hide selected spectra without removing them; the list says how many selected spectra are outside the filter. With the *Auto* switch on (the default), list gestures and group recall switch the centre to Scan(s); manually choosing a centre page turns *Auto* off, so later selection changes leave that page open.
- **Plot limit.** Scan(s) draws up to ten selected spectra. With more than ten selected, it reads no spectrum bundles and shows a read-only, scan-set-grouped list of selected spectra instead. The list is not another selection editor: change the set in the scan list or recall a saved group. Returning to ten or fewer returns to the plot.
- **Controls.** Fit starts at **None**; **Fast** and **Deep** draw the corresponding stored pass. Layout is **Offset** or **Overlay**. Offset stacks traces using an automatic step or a user-entered step in the current intensity unit. Overlay compares measured spectra with distinct colours and symbols and draws no fit; Fit is disabled there. One selected spectrum always uses the single-spectrum view regardless of the multi-spectrum layout setting. Fit/layout/scale choices are view state and are saved in the GUI session.
- **Scale and uncertainty.** **Raw** shows counts N with Poisson error √N, including 0 ± 0. **Normalised** shows N R/M, where M is the recorded monitor at that point and R is the experiment's one frozen reference monitor. R is the nearest power of ten to the median of valid positive kept monitors across the experiment; it is saved when first established, not recalculated for each selection. Until then, unchanged unit files without usable monitors are remembered so list refreshes do not repeatedly parse them; a changed file is retried. Error propagation is σ = (R/M) √(V + N²/M), with V the stored count variance; for V=N and N>0 this is y √(1/N + 1/M). Zero-count points remain 0 ± 0, with no floor. Only points with positive usable monitors appear in this scale. If a spectrum has no usable monitor, Normalised names why it is not drawn; Raw can still show its counts when the energy axis is calibrated.
- **Fits and residuals.** Fast total/component curves are solid; Deep curves are dashed and background components are dotted. A legend identifies each selected spectrum and curve, and marks the active spectrum. Outdated input changes are shown beside each trace. The Curves header marks displayed Deep curves that include an untrusted result, and the Fit tooltip gives its reason. Where deep currentness cannot be judged, the trace says currentness not recorded. In the single-spectrum view, a separate residual panel shows measured minus the selected total fit on the measured energy grid, in the current displayed intensity unit, with the data errors. It is not divided by σ. No residual is drawn for Fit: None or an unavailable curve.
- **No scientific fallback.** A spectrum needing an energy zero is not drawn until the recorded calibration supplies one, including an interpolated S30 calibration-series zero; the view never substitutes E₀=0. A pooled master is named as not drawn, never replaced with its member spectra. Missing or unusable data and monitor are named and counted in the not-drawn line; an unavailable pass or curve is named on its spectrum trace. The view does not fit, alter inputs, or silently substitute another result.
- **Deep curves use the selected deep model.** A new Deep pass stores the actual chosen model manifest beside its chosen parameters, and the read layer evaluates that manifest. It does not try to reconstruct a Deep curve from a guessed Fast-pass structure. A legacy or ambiguous Deep result without enough stored structure keeps its mode table and state, but its curve is named unavailable. Older records without monitor exposure metadata cannot be normalised until an analysis run stores it; if the source monitor itself is missing, the data must be read again with that monitor present.
- **Inspector.** The left inspector follows the active spectrum without changing the selected set. It shows “k of n selected” and previous/next controls; stepping changes only the active spectrum. Its Fit section shows stored Fast/Deep state, trust/currentness, model label and mode count; each mode lists energy E₀, linewidth Γ (DHO damping, HWHM), and amplitude h with the stored symmetric or asymmetric interval. Missing uncertainty remains “± —” or “—”, never an invented error. The four existing collapsed tables—Interval ladder, Model candidates, Verdict and Provenance—remain below the Fit section. Pooled masters still show their recorded numbers and fit tables even though they have no Scan(s) curve.
- **Saved groups and the queue.** The group chips save or recall up to nine per-experiment selections. Click a numbered chip or press 1–9 when no text editor has focus to recall; Ctrl+1–Ctrl+9 saves or replaces that slot, and + uses the lowest free slot. Only the group controls (chip Save/Undo, +) or Ctrl+number change a saved group; ordinary list selection does not. Partial groups name missing members, and recalling one drops those members with a notice. Scan(s) does not replace the Processing queue: queue buttons still add the selected eligible analyzer files, one item per file, and selection alone adds nothing. Queue order, one writer and saved-result behavior remain G26.
- **Known limits.** Pooled masters are not plotted in Scan(s); their recorded numbers and tables remain in the inspector. Normalised display needs stored monitor exposure and a calibrated energy axis. A Deep curve from an older chosen record with no recoverable model manifest is unavailable even though its tables remain. An outdated stored curve is displayed with its warning; in Normalised scale it is placed in the current unit's recorded exposure frame and the experiment's frozen R, without a refit. The G27 widget tests use generated workspaces; a human walkthrough at the GUI threshold has not been run. At ten traces on the 1108 × 851 laptop layout, the approved top-right legend overlaps much of the plot; the bounds and controls are usable, but this crowded view still needs the human walkthrough.

**Offscreen acceptance and redraw measurements (2026-10-28).** The real MainWindow passed the generated-data A1–A7 checks at 1108 × 851 and 1920 × 1040 with Segoe UI 9, including keyboard group recall/save, active-only inspector stepping, live-update zoom/selection preservation, nine group slots and legend/end-label bounds (`tests/gui/test_scans_view_acceptance.py`). After the initial ten-spectrum draw, selection replacement / Deep-to-Fast / Offset-to-Overlay redraws, including synchronous viewport painting, measured 0.100 / 0.157 / 0.102 s with Voigt resolution and 0.045 / 0.052 / 0.040 s with the committed tabulated resolution curves. These are build-machine diagnostics against the roughly 0.5 s target, not timing gates or a human acceptance claim.

---

## 1. Purpose & scope

### 1.1 Why a GUI

ISAR's console-and-report workflow hides the thing the user actually needs to *see*: data. What
are the fits versus the data; what are the error bars per interval-ladder tier; what did model
selection choose and why; how is the grouped data supporting a dispersion; where is the driver
planning to measure next and on what evidence. Buried in an after-the-fact report, none of that
gives the user control or visibility *during* the experiment.

### 1.2 The two halves

The GUI is two products with one window, and the split is load-bearing:

1. **Read-only** — see what ISAR is doing: current fit, results so far, growing coverage,
   the drive loop's reasoning. Side-effect-free by construction. Bonus: because GUI snapshots
   and report figures share rendering code (G8), building this half *improves the reports* —
   what the user wants to watch live is exactly what goes into the diagnostic, just as smaller
   snapshots.
2. **Directive** — nudge ISAR: edit objectives, adjust priors, correct fit structures, insert
   scans. Not merely a friendly way to compose run flags, but the channel by which a user runs
   ISAR *during* an experiment. Everything in this half is append-only (G2/G3) and passes
   through the driver's own validation (G6).

### 1.3 MVP user journey (the six items)

The MVP scenario is **"measure this phonon dispersion from X to Y."** These six items are the
acceptance narrative; §12 maps them to components.

1. Load a reciprocal-space map of the material (tied to its space group — non-rectilinear cells
   must not trip the display), click two points in the oriented scattering plane (e.g. HK0),
   see suggested scan spacing, amend, and **approve** the series. Data lives in the full
   (Q₃, E) 4D space; views are projections (G9).
2. Swap to e.g. the (H,1,0) plane with energy on the y-axis; watch the heatmap grow as scans
   land; see per-point phonon-energy estimates with error bars.
3. Select a struggling Q-E spectrum to inspect its raw data and errors. In Scan(s), choose a stored Fast or Deep pass when you want to see its curve and residual; the page does not fit or change saved results.
4. Monitor progress; see ISAR's running guess at the dispersion; edit it in the GUI to steer
   subsequent scans.
5. At any time, issue natural-language commands: "scan at this point, fit the peak visible
   here, bring the error on the energy center within 0.1 meV."
6. Swap to a different temperature; compare dispersions; compare selected 1D spectra in Scan(s)
   by temperature.

### 1.4 Non-goals

- The GUI is **not** a beamline control program. It never talks to the instrument; only the
  driver does (G6).
- The GUI is **not** a second fitter of record. Sandbox fits (§6) never become delivered
  numbers directly.
- No paper-grade figure editor. Export what the report renders; final figures are made later
  from frozen config, per `../ARCHITECTURE.md`.
- No web frontend in v1. Desktop Qt, same as PUFFIN/ACIDD, same box as the workspace.

---

## 2. Placement & packaging (G1)

> Corrected — see §0bis item 4: the Qt-free-core test does not assert matplotlib-freeness;
> matplotlib is a hard dependency and only the campaign path is matplotlib-free at import time.

**Decision: same repo, separate package, contract-coupled.**

- Package: `isar_gui/` at repo top level (peer of `isar/`, not inside it). Installed via an
  optional extra: `pip install -e .[gui]` (adds PySide6, pyqtgraph, watchdog).
- Launch: `isar view <workspace>` — the CLI subcommand does a guarded import of `isar_gui` and
  errors helpfully if the extra is missing.
- **Import boundary, enforced by test:** `import isar` must never pull in PySide6/pyqtgraph.
  The campaign path is deliberately matplotlib-free; it stays Qt-free with prejudice. Add a
  unit test: import `isar`, assert `"PySide6" not in sys.modules`.
- `isar_gui` may import from `isar` only: the workspace read API (§7), the metric layer (§10),
  the directive schemas (§4), and the sanctioned sandbox entry point (§6). It must **never**
  import `isar.analyze` internals, `isar.drive` internals, or parser plugins directly. Enforce
  with an import-linter contract or a grep test.
- **G26 adds two names to this rule**, both listed with their reasons in `tests/test_import_contract.py`: `isar.session.analysis_queue` (the scheduler's bookkeeping: stdlib only, no I/O, clock or threads) and the single name `isar.session.analysis_service._WinJob` (the Job Object wrapper, stdlib-only at import, so the window's process runner and the analysis service share one copy). The window imports nothing else from `analysis_service` and never runs the service in process; the service runs as a subprocess.

**Why not a separate repo:** the contract surface will churn while the GUI is young (per-scan
curves, candidate-model tables, EIG maps are new artifact surface); one repo gives version
lockstep for free. Because the coupling is through files and a small read API, extraction later
is cheap *if* the GUI ever earns independence (e.g. becomes a general TAS cockpit fronting
TAVI). Boundary in imports, not topology.

---

## 3. Artifact classes & the trust story (G2, G3)

Three classes of artifact exist in a workspace. The GUI's rights differ per class:

| Class | Examples | Mutability | GUI rights |
|---|---|---|---|
| **Immutable observations** | `ScanRecord`s, raw counts + monitor, QC flags as recorded | Never edited | Read only |
| **Versioned interpretations** | fits, intervals, verdicts, branch model / dispersion prior, collation products | Replaced by re-runs, never edited in place | Read only |
| **Append-only directives** | corrections, objective specs, priors nudges, human proposals, adjudication records | Append only | **The only thing the GUI writes** |

This is the GUI restatement of the existing pipeline truths: *scans are immutable; our guesses
about what they mean are not.* Rejection marks, never deletes; re-fits version, never overwrite;
directives append, never mutate. The GUI cannot corrupt a workspace because it holds no pen for
classes one and two.

Consequence worth stating: because human interventions are files in the provenance chain, **a
campaign is replayable with its human interventions included**. The morning diagnostic can (and
should) attribute beam spend to `human`-priority directives like any other proposer — "the human
spent 40 minutes here" appears in the ledger honestly.

---

## 4. The directive taxonomy (G4)

Every GUI action with side effects — every button, drag, approved plan, or typed sentence —
compiles to exactly one of **four directive types**. If a proposed feature does not reduce to
one of these, that is a design smell alarm: stop and re-derive.

### 4.1 The four types

1. **Objective edit** (`objective_edit`) — changes what the campaign is trying to achieve.
   "Measure this line at this spacing." "Tolerance 0.1 meV on ω₀ at these q." "Over-count this
   (Q, E) region for a paper figure." Legality already established: the objective spec is data,
   and mid-run re-specification is a session mutation (`../ARCHITECTURE.md` §8, renegotiation door).
2. **Prior edit** (`prior_edit`) — re-seeds interpretation, never delivered numbers. Nudge
   `prior_omega`/`prior_sigma` at a q; assert branch separation where two branches are close;
   redraw the running dispersion guess (item 4). Affects the *next* fits via the branch model.
3. **Fit-structure correction** (`fit_correction`) — the existing §11 (`../ARCHITECTURE.md`) correction
   channel: "there is a second phonon here, refit," "scan 223 is bad," "that ω was wrong."
   Card edits + re-run with ledger invalidation, exactly as human review corrections do today.
4. **Direct proposal** (`scan_proposal`) — "scan at this point now." Injected into the
   acquisition queue with provenance/priority `human`, which outranks EIG ranking but **still
   passes validate** (G6).

### 4.2 Natural language is an input device, not a type (item 5)

`goal_to_program` already compiles a natural-language goal into a frozen `isar.campaign-program/1` at
campaign start (the GUI's campaign source key is `program_path`). Extend it (as
`directive_from_text` or similar) to parse *incremental* directives into
the same four schemas. LLM proposes structured output, schema validates, artifact freezes,
driver disposes — P1 intact. The GUI shows the compiled directive for confirmation **before**
writing it to the inbox (the user approves the artifact, not the sentence). Routing through
LLMM as usual; a failed/ambiguous parse is shown to the user, never guessed into the inbox.

### 4.3 Directive schema (sketch)

One envelope, four payloads. JSON, one file per directive.

```json
{
  "directive_id": "d_2026-07-11T21-04-13Z_a3f2",
  "created": "2026-07-11T21:04:13Z",
  "source": "gui",
  "source_detail": {"view": "planner", "user_action": "approve_line", "nl_text": null},
  "type": "objective_edit | prior_edit | fit_correction | scan_proposal",
  "payload": { "...type-specific..." },
  "requires_ack": true
}
```

Payload sketches (to be finalized against existing internal structures — the objective spec,
the branch-model prior format, and the correction format already exist; **reuse those shapes,
do not invent parallel ones**):

- `objective_edit`: `{ "op": "add_line | set_tolerance | add_overcount_region | amend",
  "line": {"q_start": [h,k,l], "q_end": [h,k,l], "n_points": N, "spacing_rlu": s},
  "tolerance": {"param": "omega0", "halfwidth_mev": 0.1, "applies_to": [...]},
  "region": {"q_range": ..., "e_range_mev": ..., "target": "counts | tolerance"} }`
- `prior_edit`: `{ "q": [h,k,l], "prior_omega_mev": x, "prior_sigma_mev": s,
  "branch_hint": "acoustic | optic | separated", "scope": "point | line | model" }`
- `fit_correction`: reuse the existing correction record format verbatim; the GUI is just
  another author of it.
- `scan_proposal`: `{ "q": [h,k,l], "e_range_mev": [lo, hi], "e_step_mev": d,
  "counting": {"mode": "monitor | time", "value": v}, "reason_text": "...",
  "then": {"fit": true, "tolerance": {...}} }` — the `then` clause covers item 5's compound
  command ("scan, fit, bring error within 0.1 meV") as a proposal plus an objective edit; the
  compiler may emit **two** directives from one sentence.

### 4.4 Acknowledgement

> Corrected — see §0bis item 1: the ledger is two artifacts (`session.json` snapshot +
> `report/session_log.jsonl` event stream); `directive.consumed` lands in the JSONL stream.

Directives are fire-and-forget at the file level, but the GUI needs feedback. The driver, on
draining a directive, appends a structured ledger event (`directive.consumed`, with
`directive_id`, outcome `accepted | rejected_validate | deferred`, and reason). The GUI watches
the ledger (§7) and surfaces the outcome. A rejected human proposal (triangle can't close, dark
angle) renders with the driver's reason — the same refusal logging discipline as
`recenter_refused_elastic`.

---

## 5. The inbox & ownership (G5, G6)

### 5.1 Transport

> Corrected — see §0bis item 1: `session.json` is the snapshot; the ledger events referenced
> below live in `report/session_log.jsonl`, a separate file.

```
workspace/
  inbox/                  # GUI (or any proposer) writes here
    d_<timestamp>_<id>.json
  inbox/consumed/         # driver moves drained directives here (or ledger-only, see below)
  session.json            # ledger gains directive.consumed events
```

- **Write protocol:** write to `inbox/.tmp_<id>`, then atomic rename into place. Windows note:
  use `os.replace`, and the driver must tolerate (skip, retry next cycle) files that are
  mid-rename or locked.
- **Drain protocol:** at the top of each decide step, the driver lists `inbox/`, processes
  directives in timestamp order, validates each, applies or rejects, logs
  `directive.consumed`, and moves the file to `inbox/consumed/`. The move is bookkeeping; the
  ledger event is the record.
- Restart-safe by the same idempotency argument as everything else: a directive is either in
  `inbox/` (pending) or in the ledger (decided). Re-running the drain is harmless.

### 5.2 Ownership ruling (G6) — restated because it is the whole safety story

**The GUI can only ever append to the scan queue and nudge ISAR; it never grabs control.**

- The driver is the sole owner of the instrument/backend connection. The GUI holds no socket
  to the loop, imports no driver internals, and cannot pause/step/abort the loop in v1.
  (A `stop_request` directive is conceivable later; it would still be a directive the driver
  honors at a safe point, not a seizure.)
- Human directives enter the same propose→validate→run pipeline as the driver's own proposals,
  with priority `human`. Validation is **not waived** for humans: triangle closure, dark
  angles, limit checks run on the user's clicks exactly as on EIG proposals. The human being
  wrong about feasibility at 3 a.m. is an expected input, and the planner should already have
  shown forbidden zones before approval (§10), making validate rejections rare but still real.
- Budget honesty: human-priority scans consume budget and are ledgered like any other. The
  campaign diagnostic attributes them.
- Concurrency model: **none needed** beyond inbox atomicity. No locks, no shared mutable
  state, no two-master problem. This is a direct dividend of files-as-truth.

---

## 6. The sandbox exception (G7) — interactive refitting

Item 3's interactive half ("nudge the priors, try a structure, see the fit *now*") cannot ride
`isar run` subprocess round-trips; it needs sub-second feedback. Resolution:

- **Sanctioned import:** the GUI may import the deterministic fit engine through **one**
  sanctioned entry point — the same frozen-config interface `isar run` uses (build a candidate
  config → run the fitter → get fit result + intervals + candidate table). Nothing deeper. This is
  the *only* place the GUI touches analysis code in-process.
- **Sandbox results are ephemeral and marked.** Rendered with an unmistakable visual treatment
  (e.g. dashed curves + "SANDBOX — not a result" banner). Never written into class-two
  artifacts, never exported as results, discarded on session close (the sandbox *config* may
  be saved as a draft directive).
- **Commit = directive.** "Keep this" compiles the sandbox config delta into a `prior_edit`
  and/or `fit_correction` directive; the official number appears only after the workspace
  re-run re-fits from frozen config. Play freely; bless deterministically. P1 survives: the
  GUI is a proposer whose proposals freeze before the fitter of record runs them.
- **Threading:** sandbox fits run in a worker thread (PUFFIN's worker pattern), UI never
  blocks; a slow bootstrap tier is not run in sandbox by default — sandbox uses the fast tiers
  (Hessian/profile) and *says so*, since certifying intervals are the re-run's job anyway.

---

## 7. The workspace read API (Phase 0) — the GUI's data layer

A deterministic, **Qt-free** module in core: `isar/workspace_api/` (name open; it belongs in
`isar/` because scripts, notebooks, and the report all want it too — it is the notebook
interface as much as the GUI's data layer).

### 7.1 Surface (sketch)

> Corrected — see §0bis item 1: `session_events` reads `report/session_log.jsonl`, not
> `session.json`.

```python
ws = Workspace.open(path)                    # read-only handle
ws.card                                      # policy (TOML, parsed) + state (JSON)
ws.scans                                     # ScanRecords w/ provenance tags, QC masks
ws.groups                                    # collation products per card key
ws.results                                   # fits: params, interval ladder per tier,
                                             #   interval_reliable, model-candidate table
                                             #   (model, P(model), ΔAIC, n_modes),
                                             #   verdict, disposition, confidence, provenance,
                                             #   pooled members
ws.branch_model                              # dispersion prior + branch posterior +
                                             #   assignment_entropy per point
ws.session_events(kind=None, since=None)     # ledger: decide.action, escalate_fit,
                                             #   refusals, directive.consumed, budget ticks
ws.campaign                                  # objective spec, per-q states, budget,
                                             #   stop reason (if ended)
ws.evaluate_model(params, x)                 # deterministic curve eval for rendering —
                                             #   generalizes reconstruct_curve (no refit)
ws.watch(callback)                           # file-watcher: change notifications
                                             #   (results, ledger, state), debounced
```

### 7.2 Notes

> Corrected — see §0bis item 3: `reconstruct_curve` re-solves amplitudes by NNLS against counts;
> `evaluate_model` is pure only when `FitOutcome.amplitudes` exist, else it falls back to
> `reconstruct_curve`. Both share `assemble_curve` (`isar/report/audit.py`).

- `evaluate_model` and `reconstruct_curve` must be the same math; the existing
  reconstruct-at-render-time decision (`../ARCHITECTURE.md` §12) already established stored-params →
  curve without refit. Expose it; do not duplicate it.
- `watch` is how the live views work (items 2 and 4, and the drive dashboard): watchdog on
  the workspace directory, debounce, re-read only what changed. Because Stage III writes
  artifacts as it goes, watching files **is** watching the campaign, with zero coupling to
  the loop. The GUI must tolerate partial/in-progress writes (retry-on-parse-error, read on
  next event) — same tolerance the driver extends to the inbox.
- This module is a prerequisite shared with the planned residual-driven-refit vision tier
  (which needs per-scan figures) — building it pays twice.
- **Version the read surface.** The Stage II ↔ III contract is frozen-and-additive; the read
  API inherits that discipline. GUI displays the workspace schema version and degrades
  gracefully (hide panes, don't crash) on missing newer fields.

### 7.3 Rendering unification (G8)

One figure module, two consumers: the report/diagnostic renderers and the GUI. Concretely —
a shared layer that turns read-API objects into *plot specifications* (data series, bands,
annotations, styling semantics like `(unreliable)`), with two backends: matplotlib (reports)
and pyqtgraph (GUI). The **semantics** live in the shared layer so the two views can never
disagree about what an unreliable interval or a pooled master looks like. This also fixes figure hygiene: the render path becomes first-class instead of stranded in `out/`.

G27 applies that same PlotSpec layer to Scan(s): the read API supplies stored measured values and curves; the Qt renderer only presents them.

**Rendering rules (binding, inherited from `../ARCHITECTURE.md` §12):**
- `interval_reliable=False` suppresses the bootstrap interval, whose ladder row renders as
  `(unreliable)` rather than numbers. A valid profile or trusted-HESSE fallback still certifies
  at its own rung; only a fit with no such fallback uses the explicitly unreliable individual
  glyph, with no whiskers.
- A fit may use its own certifying interval. A pooled master may instead use
  `omega0 ± calibrated_halfwidth` only from the latest `MasterAssessmentView` whose populated
  `master_revision` exactly matches. HKL fallback is limited to legacy data where a revision is
  absent; differing populated revisions never match. Inspector metadata names the interval
  source and method and records its endpoints.
- The inspector's phonon-energy interval is horizontal at `x=ω₀`; it emits no count-axis
  uncertainty. Plane and dispersion energy whiskers preserve separate lower and upper spans
  rather than collapsing an asymmetric interval to a half-width. Symmetric legacy `yerr`
  remains a rendering compatibility input.
- Pooled masters are labeled **pooled master** and enumerate members on inspection; a rejected
  pooled master is "pooled master — uncertifiable," never junk.
- Interval-ladder comparison shows tiers side by side, *labeled by tier* (Hessian / profile /
  bootstrap), with the certifying tier visually primary.
- The model-candidate table (model, P(model), ΔAIC, n_modes — posterior drives, ΔAIC is the
  retained diagnostic since H4) is the standing answer to
  "what did ISAR choose and why" — render it wherever a fit is shown.
- A plot specification owns its strip colour map (`viridis` by default). The Qt colourbar is
  attached only to a private proxy image; level and lookup-table edits propagate to every
  current strip. Re-render and stripless transitions clear all current-strip references, so
  neither stale images nor backend-default black-and-white strips can survive.

---

## 8. Views & 4D honesty (G9)

G27 retires Waterfall as a central page. Scan(s) is a per-spectrum 1D view; Plane and Dispersion retain the explicit projection declarations below. The Waterfall-specific history in this section is retained as history, not a second live page.

Data lives in (Q₃, E); every 2D view is a projection and must say so on screen, always:
a persistent legend like `plane (H, 1, 0) × E — integrating |L| < 0.02, |K−1| < 0.02`.

- **Heatmap (item 2):** measured strips render as strips; gaps render as gaps. **No
  interpolation by default.** An optional smoothed/kriged surface is a *model overlay*,
  toggled explicitly and labeled as a model — the same discipline as everywhere else in ISAR.
  Conservative-by-construction applies to pixels. **The column at a q is the collated master**
  (2026-09-05): its persisted ΣN expressed as counts per one plane-wide reference monitor (the
  median kept ΣM over all displayed masters), so columns pooling different member counts and
  exposures are comparable. The master is located through its members in the ledger, never
  through its fit row, so a withheld or failed master fit still shows its data. Per-scan
  columns draw only at a q with no persisted master (separate collation, or a workspace
  predating the pooled-spectrum artifact); before this rule the member columns of one q stacked
  at the same x and the most recent scan silently covered the rest. **Data first, fits as they
  land** (2026-09-08): the Plane population remains `projection.population(ws)`; the retired Waterfall page used the same population
  — every fitted result plus every ledger scan still at status `ingested` (counts on disk, Q and
  T stamped by the drive's early registration before its fit runs), the latter as a data-only view
  with no ω̂ glyph. Before this both views read only `ws.results`, so the first scan of a campaign
  was invisible for the whole of its fit and the feed said "scan landed" over an empty plot.
  The persisted master fit curve follows its fit exposure frame:
  the master fit models ΣN_i as
  (ΣM_i / reference) × f(E_i) on the member-union grid, so the persisted curve f(E) is the
  expected count *per reference monitor*, and raw ΣN drawn against it is off by the local
  exposure ratio (3.3× on a real 8-member optic master). **The colour scale is set by the
  inelastic signal** (2026-09-05): the spec carries `strip_levels`, which the plane view model
  takes from the cells with |E| beyond 1.5 resolution FWHM whenever a column reaches the elastic
  band; the elastic line saturates and an annotation says so. Both renderers and the colourbar read
  the spec's levels, and the colourbar stays draggable. Without this a single Bragg-elastic cell
  (2000× a phonon cell) painted every phonon black. One high-contrast unfilled double
  outline marks the globally latest physical submission with a completed matching analysis,
  joined to visible strips by `acquisition_id` — directly, or through the master column that
  pools it; pending/failed work and certification-only analyses are excluded. The outline spans
  the union of displayed strip widths and measured energy extrema. If that global acquisition is
  outside the selected temperature/projection slice, nothing is drawn. Physical-result order is
  a legacy fallback only when detailed event identity is absent.
- **Overlay grammar:** each result is one composed glyph, classified by the shared Qt-free
  display state:
  - small filled green circle — usable individual evidence;
  - small hollow grey circle — individual evidence without a certifying interval;
  - small hollow red diamond — explicitly unreliable individual evidence;
  - large hollow green circle — target-matched pooled master, uncertified;
  - large hollow amber diamond — unresolved or ambiguous target identity;
  - large hollow red diamond — explicit target mismatch;
  - large heavy blue square — certified pooled master;
  - large hollow grey square — legacy pooled master without a matching assessment;
  - small neutral point — unselected candidate.
- Target identity comes from the branch-track target and falls back to persisted
  `FitView.target_branch`; `FitView.branch_id` establishes an explicit match or mismatch but
  does not invent the target. Certification requires a revision-matched assessment whose state
  is `certified` or whose `converged` flag is true. The dispersion status ribbon consumes that
  same state, so ribbon and plot cannot disagree.
- **Colour modes:** state-first categorical colour is the default. A single **Colour by
  entropy** action appears in both plane and dispersion controls, is shared between them, and
  persists in GUI session state. Entropy mode replaces categorical point colour with the
  existing diagnostic ramp while shape, size, and fill retain authority/state; the two colour
  modes are mutually exclusive. Prior and branch curves stay visually neutral. Legends use a
  fixed semantic order.
- **Out-of-plane awareness:** points whose Q falls outside the declared integration window
  are excluded from the view, with a visible count of exclusions ("14 scans outside this
  projection") so the projection can't silently hide data.

---

## 9. Grouping (G10)

- **Semantic grouping is the card.** The GUI's group browser *is* the card's `collation.key`
  and merge policy made visible. If the user wants different grouping, the GUI edits the card
  (a class-three write via `fit_correction`/card-edit channel) and triggers re-collation on
  re-run. There is exactly one definition of "a dataset."
- **Display choices are view state.** Scan(s) layout, scale, and stored-pass choice are saved in the GUI session. They do not change card collation or fit results.

---

## 10. Coordinate & feasibility layer (G11)

> Corrected — see §0bis item 2: lattice/orientation is a versioned epoch series
> (`CardState.lattice_epochs`), not fixed card policy; scans stay in absolute instrument
> coordinates and epochs re-derive HKL retroactively. See §0bis item 5 and G14 on feasibility
> wiring: dark angles/limits have no numeric data source yet, so forbidden-zone previews will be
> kinematics-only until TAVI-side config exists.

A **metric/UB module in core** (Qt-free; suggested `isar/geometry/` or inside the schema
package next to the card, since lattice is card policy):

- Reciprocal metric tensor + B matrix from the card's lattice; orientation from u, v vectors;
  scattering-plane basis; r.l.u. ↔ Å⁻¹; distances/angles in non-rectilinear cells; in-plane ↔
  (H,K,L) mapping for the planner and all views.
- **Source the math from TAS_MCP / TAVI** — the triangle-closure and angle computations exist
  and are tested there. Port or depend; do not rewrite from scratch. (License compatible;
  everything relevant is GPL-3 — see §11.)
- **No third opinion on feasibility.** The planner *draws*; feasibility (triangle closure at
  (Q, E) for the card's kf, dark angles, limits) is consulted from the existing authority the
  driver's validate step uses. The planner pre-renders forbidden zones in the plane view
  **before** approval so users don't author doomed scans; validate remains the final word.
- Non-rectilinear display: a linear transform on the pyqtgraph view/ImageItem driven by the
  metric layer (hexagonal and monoclinic cells shear correctly; axes labeled in r.l.u.).
  Modest, contained work.

---

## 11. Donor map (G12) — assembly, not invention

All three donors and ISAR are **GPL-3** (verified 2026-07-11), so code may be lifted between
them freely with copyright/attribution notices. Strategy: copy patterns shamelessly, lift small
widgets where they fit, factor a shared widget library later **only if it hurts**.

G27's Scan(s) selection and saved groups are local ISAR behavior: groups save stable spectrum identities in the per-experiment GUI store, not cut templates or card collation policy.

### ACIDD (`C:\Users\AMM\Documents\Github\Science\ACIDD`)
Donates the **cut/plan gesture** and the image-view scaffolding:
- Make Cut interaction: click start → live crosshair + dashed preview → click end → draggable
  endpoint markers → live-updating profile. This **is** the planner's "click two points" (item
  1) and the viewer's cut-extraction gesture.
- `Step along cut (∥)` spinbox ≡ suggested scan spacing with amendment. `Cut width (⊥)` ≡
  integration width for view-cuts, and the over-count region gesture (item 3) for directives.
- Units toggle (pixels ↔ r.l.u.) ≡ the coordinate-layer-under-the-view pattern with live HKL
  readout under the cursor. **Note:** ACIDD's `pixel_to_hkl` math itself (CrysAlisPro
  `.par`/`.dcunwarp` + goniometer R) is detector-specific and does **not** transfer — keep the
  architecture (swappable coordinate layer, everything downstream speaks r.l.u.), slot in the
  TAS metric layer (§10).
- Cut **templates** (save set, re-apply to other files, auto-apply) ≡ "run this same line at
  the next temperature" (item 6) and re-usable measurement plans.
- Session persistence (`config/session.json`: files, cuts, dock layout restored on start) ≡
  the GUI session-state pattern (§9).

**The semantic inversion, stated plainly:** in ACIDD a cut is a *query* through dense existing
data; in the planner the same gesture through unmeasured space is a *directive*. One cut tool,
two artifact outputs, mode explicit on screen — the user must always know whether they are
browsing or authoring beamtime. Draw through measured region → view (side-effect-free); draw
through terra incognita + approve → `objective_edit` directive to the inbox.

### PUFFIN (`C:\Users\AMM\Documents\Github\Science\PUFFIN`)
Donates the **live fitting grammar** and app skeleton:
- MVVM layering (`view/` docks + `viewmodel/` + `worker/` threads) — adopt wholesale.
- Parameter docks with live preview; exclusion mode (click/box-drag to toggle points);
  F-to-fit; component selection (1–9, Q/E cycling); Docks menu; log dock.
- Composite model assembly + save-model-dialog patterns inform the sandbox's structure editor
  (add a phonon = add a component), though the sandbox builds **ISAR fit configs**, not
  PUFFIN model YAML.

### ISAR
Donates **everything that thinks**: parsing, QC, collation, the fitter and interval ladder,
model selection, branch model, verdicts, the driver, the ledger. The GUI adds no analysis.

---

## 12. Component inventory & MVP mapping

| # | Component | MVP items | Reads | Writes | Phase |
|---|---|---|---|---|---|
| C1 | Workspace browser (open, card summary, group tree per collation key) | — | card, groups | — | A |
| C2 | Scan inspector: recorded facts; stored pass and mode tables; interval ladder, candidates, verdict and provenance | 3 (view) | results, assessments, scans | — | A |
| C3 | Plane heatmap view: declared projection, viridis strips-and-gaps with one proxy-owned colourbar, latest completed-acquisition outline, state/entropy fit grammar with asymmetric energy errors, neutral branch curves, live growth via watch | 2 | results, assessments, branch_model, session_events, watch | — | A |
| C4 | Scan(s) spectrum view: selected 1D spectra, stored Fast/Deep curves, residuals, and Offset/Overlay comparison | 6 | selected scans, results, spectrum_bundles | — | A |
| C5 | Dispersion view: ω vs q with authoritative asymmetric intervals, the shared state/entropy fit grammar and matching status ribbon, neutral prior curve, off-curve flags | 3, 4 (view) | branch_model, assessments, results | — | A |
| C6 | Planner: RS map from card lattice/space group, oriented plane, forbidden zones, two-click line, spacing spinbox, scan-series preview, approve | 1 | card, geometry, feasibility | `objective_edit` | B |
| C7 | Directive composer + inbox monitor: pending/consumed/rejected with driver reasons | 1, 3, 4, 5 | session_events | all four types | C |
| C8 | Sandbox refit panel — **Partial:** live fast-tier preview, manual E₀, SANDBOX banner implemented; structure editor, prior sliders, Commit → directive planned | 3 (interactive) | `isar.sandbox_api` | `prior_edit`, `fit_correction` (planned) | C |
| C9 | NL command bar: text → compiled directive(s) via LLMM → user confirms artifact → inbox | 5 | — | any (via compiler) | C |
| C10 | Drive dashboard — **Partial (Pass 3 + EIG-4 Slice 9):** live decision feed, aggregate progress, scan verdicts, stop states, latest restricted EIG selection, and selector-predicate failure counts implemented; full EIG landscape, per-q state machine, general refusal log, and proposal queue still Planned (blocked on the per-q event contract, §16.6) | 4 | campaign, session_events, watch | — | C |
| C11 | Dispersion-guess editor: drag the prior curve / control points → `prior_edit` scoped to line/model | 4 | branch_model | `prior_edit` | C |
| C12 | Campaign launcher — **Implemented (Pass 3 + EIG-4 Slice 9):** setup form → `isar drive` subprocess behind the coordinator; explicit off/shadow/restricted-authoritative authoring; frozen-mode Resume; maintained O41 example creation; cooperative stop + force escalation (G19) | 4 | card, campaign, session_events | none directly — the sole-writer drive subprocess performs mutations; the stop control request is the one sanctioned GUI write | Pass 3 |

---

## 13. Phasing (G13)

Ordered by risk; each phase is independently useful and independently shippable.

**Phase 0 — workspace read API + geometry layer.** Core-only, Qt-free, no GUI. Deliverables:
`isar/workspace_api/`, `isar/geometry/` (metric/UB sourced from TAS_MCP/TAVI math), the
Qt-free-core test, `evaluate_model` unification with `reconstruct_curve`. Worth building even
if the GUI stalls — it is also the notebook interface and the vision-tier prerequisite.

**Phase A — read-only viewer (C1–C5).** Against *finished* workspaces first (the frozen MVP-16
certification workspace is the natural characterization fixture), then live via `watch`. No new
contracts, no directives, cannot destabilize anything — deliberately safe while the TAVI SVP
(realistic clocks, McStas rung) is still pending, and immediately useful for SVP post-mortems.
Exit criterion: open the frozen certification workspace and walk items 2, 3(view), and 6.

**Phase B — planner (C6).** Only *authors campaign configs* — click, amend, approve, out comes
the objective spec / `objective_edit` the user would otherwise write by hand. Works before any
live plumbing exists (the artifact can seed `isar drive` at launch rather than mid-run). Exit
criterion: item 1 end-to-end, including forbidden-zone rendering on a non-rectilinear cell.

**Phase C — directives live (C7–C11).** The inbox + drain protocol in the driver, the sandbox,
NL compilation, the drive dashboard, the dispersion editor. The genuinely new machinery, last,
when A has proven the rendering and B has proven the geometry. Exit criterion: the full MVP
journey (§1.3) against TAVI-as-backend.

> Pass-3 note (2026-07-16): the read-only half of C10 plus the C12 launcher shipped ahead of
> Phase C (see §0ter) — campaign observation needed no directive machinery, only the existing
> event log. Directives themselves remain entirely Planned.

---

## 14. Testing

> Corrected — see §0bis item 4: the Qt-free core test also asserts a scipy-free
> `isar.workspace_api` import; it does not assert matplotlib-freeness (matplotlib is a hard
> dependency of the shared plotspec layer).

- **Qt-free core:** `import isar` never imports PySide6/pyqtgraph (unit test, Phase 0).
- **Import contract:** `isar_gui` imports only the sanctioned `isar` surfaces (lint/grep test).
- **Characterization:** Phase A views pinned against the frozen MVP-16 workspace — same
  discipline as the campaign diagnostic. Golden plot-spec snapshots (compare the shared-layer
  plot *specification*, not pixels, so both backends are covered by one fixture).
- **Directive round-trip:** schema validate → inbox write (atomic, Windows `os.replace`) →
  driver drain → ledger event → GUI ack surfaced. Include the rejected-by-validate path.
- **Sandbox discipline:** a sandbox session must produce zero writes outside `inbox/` and the
  GUI's own session file (assert on a tempdir workspace).
- **4D honesty:** projection legend always present; exclusion count correct on a synthetic
  workspace with deliberate out-of-plane scans.
- **Authority grammar:** revision-mismatched assessments cannot certify a master or supply its
  interval; every fit-display state maps to one glyph in fixed legend order; plane,
  dispersion, and the status ribbon agree. State and entropy colours are exclusive and the
  shared mode round-trips through GUI session state.
- **Directional uncertainty:** horizontal inspector endpoints and asymmetric vertical
  plane/dispersion spans are exact; explicitly unreliable series have no whiskers.
- **Strip colour ownership:** every strip shares the plot-spec colour map and current levels;
  colourbar gradient/level changes reach all strips; re-render and stripless transitions retain
  no stale image references.
- **Latest acquisition:** chronology covers retries, pending/failed jobs, duplicate detector
  rows, pooled and certification exclusion, legacy fallback, exact union bounds, and the
  selected no-output behavior when the global latest lies outside the slice.
- **Idempotent drain:** duplicate/re-listed directives are not applied twice (keyed by
  `directive_id` in the ledger).

---

## 15. Open questions

1. **Read-API module naming and exact home** (`isar/workspace_api/` vs `isar/session/read.py`
   growth) — decide against the current `session/` internals when Phase 0 starts.
2. **Feasibility authority wiring:** consult TAS_MCP as a library dependency, port the
   functions into `isar/geometry/`, or route through TAVI? Decide with the `tas4d` wiring
   (`../ARCHITECTURE.md` §15 open item) since both touch the TAVI boundary.
3. **Directive payload finalization** must be done against the real objective-spec, prior, and
   correction structures in code — the sketches in §4.3 are shapes, not schemas. First task of
   Phase C (or late B for `objective_edit`).
4. **`stop_request` directive** (pause/finish-early as an append-only ask the driver honors at
   a safe point) — out of scope for v1, door open. *Pass 3 shipped its minimal precursor as a
   token-scoped control request (G19), deliberately outside the directive schema; folding it
   into the inbox when Phase C lands is the remaining work.*
5. **Mid-run renegotiation UI** ("±0.15 meV at these four q — accept?") — lands naturally as a
   driver-authored question artifact + GUI dialog + `objective_edit` answer, once the
   renegotiation protocol itself (Planned, `../ARCHITECTURE.md` §8) exists.
6. **Shared widget library** across ACIDD/PUFFIN/ISAR-GUI — explicitly deferred; copy first,
   factor when duplication hurts.
7. **GUI naming** — user-facing name for `isar view` (working title: none; the river does not
   need a tributary yet).

---

## 16. Roadmap (designed in Pass 3, not built)

Each item was designed far enough to be picked up cold; none is implemented.

1. **PUFFIN masking port.** Port `PUFFIN/view/view_box.py` (`CustomViewBox`: click-to-toggle
   point exclusion, drag-box exclusion with visual feedback, exclude-mode signals) as
   `isar_gui/views/mask_viewbox.py`. First consumer: the Sandbox pane — masks feed the
   in-process preview refit (ephemeral, SANDBOX-marked); a "commit" of a mask waits for the
   Phase-C `fit_correction` directive. **Hard Rule 3 restated as binding:** this is a view-layer
   lift only — PUFFIN's σ≥1 variance floor must never cross into ISAR (σ=√N, σ=0 legal).
2. **PUFFIN handoff contract.** For close-inspection fitting in PUFFIN itself: request
   `{scan: {x[], y[], sigma[], meta{unit_key, q, E, T}}}` → response
   `{mask: bool[], components: [{type, params}], provenance}`; file-pair or local-HTTP
   transport (PUFFIN currently has no headless surface — that is the PUFFIN-side work). ISAR
   compiles responses into `fit_correction`/`prior_edit` directives; σ is informational to
   PUFFIN and never round-trips.
3. **Combine-scans ("same spot") UX.** Multi-select → pooled *preview* overlay (pure view
   state); persistence is a card collation-policy edit via directive (G10), never GUI-local
   merge state. Blocked on Phase C.
4. **S30 energy-zero carry-forward.** Change `isar/calibrate/energy_zero.py`
   (`resolve_zero`/`ZeroSeries`) from interpolation-bracketing to authoritative step-function
   carry-forward: the most-recent-preceding elastic supplies E0 until the next elastic; later
   elastics never retroactively change earlier scans; pre-first-elastic scans stay held (never
   E0=0); every carried assignment logged. Engine change → new D-series decision +
   characterization updates in the calibrate tests; the GUI then shows "E0 carried from scan N"
   provenance.
5. **Import window.** A dedicated detect-preview import dialog (`build_detect_command` /
   `DetectResult` are retained for it) as the long-term replacement for the compact Files dock.
6. **Per-q live campaign state.** An additive event-contract change (per-q identifiers + status
   on `model_update` or a new payload field) to unlock the per-q half of the C10 dashboard
   honestly; today's ledger carries only aggregates (G19/AD-9).
7. **Reclaimable in-flight scans.** Resume re-attaches to a submitted TAVI job via the `run`
   event's `job_id` instead of re-proposing, closing the force-stop double-spend window; needs
   TAVI job-query support.
