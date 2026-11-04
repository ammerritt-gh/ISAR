# Inter-spectra fitting: the second pass that couples neighbouring spectra

> **Status:** live
> **Authority:** canonical design for inter-spectra fitting (the second pass that couples neighbouring spectra)
> **Verified against code revision:** `00be8fa1` (2026-11-04)

The fast pass fits every spectrum alone. This pass lets neighbouring spectra share what they show:
spectra at nearby momentum transfer q, and the same analyzer at adjacent temperatures T. A mode that is
clean in one spectrum seeds its neighbours; a mode that moves unexpectedly between neighbours pays a
penalty and is flagged for a person. The purpose, in the operator's words (IS1): alert the operators when
interesting things happen, so that only the hard stuff is brought to their attention.

This file owns the system: what is built, its invariants, its code locations, its storage and the dated
operator rulings. Other documents point here and do not restate the rulings. Section 1 is the status of
each capability; anything not marked Implemented is not yet in the code or not yet established.

## 1. As built

**Implemented** means built and covered by the tests named. **Partial** means built and tested, but not
established on real data, or not calibrated. **Planned** means not built. (AGENTS.md, Verification 4.)

| Capability | Status | Code | Tests |
|---|---|---|---|
| Spectrum keys, series (one q setting, several temperatures), T order, eligibility | Implemented | `isar/session/inter_spectra/nodes.py` | `tests/session/inter_spectra/test_nodes.py` |
| T-links between temperature neighbours of one series | Implemented | `isar/session/inter_spectra/graph.py`, `schedule.py` | `tests/session/inter_spectra/test_graph.py`, `test_groups.py` |
| q-links from grid adjacency (4 × 7 grid, no diagonals) | Implemented: built and tested; coordinates are each analyzer's accepted per-analyzer hkl, and a lattice epoch, where its scan has one, only refines the spacing metric; no real-data run has been made (section 9) | `layout.py`, `graph.py`, `q_provider.py` | `tests/session/inter_spectra/test_graph_q.py`, `test_q_provider.py` |
| Curvature terms (three collinear q points, simultaneous batches) | Partial: built and tested; on the BL43LXU gpi triplets, measured in the hkl metric (where a scan has no lattice epoch), the triplets lie off the straight line, and the collinearity tolerance (provisional default 0.01, uncalibrated, under the operator's review) decides whether a curvature term forms (section 9) | `graph.py`, `schedule.py` | `tests/session/inter_spectra/test_graph_q.py` |
| Coupled refit of one spectrum: the fixed mode set (stage 1 of a batch, mode count and entry), the continuous stage in three pool steps (fit, close-pair restarts, finish), one shared objective with the tie-breaker penalty, release fit | Implemented on synthetic spectra; not established on real data | `isar/analyze/coupled/refit.py`, `engine.py`, `rules.py` | `tests/analyze/test_coupled_engine.py`, `test_coupled_refit.py`, `test_coupled_rules.py`, `test_coupled_fixed_fit.py` |
| Fixed mode set per spectrum: the fit in use's modes with the link's listed adds and splits entered by a priced comparison (never its drops; the unpriced entry is retired 2026-11-04), frozen for the batch; both batch kinds | Implemented on synthetic spectra; not established on real data | `isar/analyze/coupled/refit.py` (`evidence_assignment`, `fixed_assignment`), `isar/session/inter_spectra/runner.py` (`fixed_sets`) | `tests/analyze/test_coupled_fixed.py`, `tests/session/test_inter_spectra_runner.py` |
| Tie-breaker penalty between close modes: closed-form separability; one penalty in the solo fast pass's prior and count evidence, and in the coupled objective, its release fit and its gradient | Partial: built and tested on synthetic spectra; uncalibrated, provisional settings; the closed form's known limit is recorded (`STATS_PRIMER.md` §2.5) | `isar/analyze/pair_penalty.py`, `isar/analyze/fast_pass.py` (`pair_terms`, `close_pairs`), `isar/analyze/coupled/engine.py` | `tests/analyze/test_pair_penalty.py`, `tests/analyze/test_fast_pass_penalty.py`, `tests/analyze/test_coupled_pair_penalty.py`, `tests/analyze/test_coupled_gradient.py` |
| Close-pair restarts: four starts per close pair that keep the count (intensity moved either way, separation wider or narrower), run as separate pool calls; the lowest J is kept | Implemented on synthetic spectra | `isar/analyze/coupled/refit.py` (`_restart_starts`, `refit_step`) | `tests/analyze/test_coupled_fixed_fit.py`, `tests/session/test_inter_spectra_runner.py` |
| Flags `connection_ambiguous` and `width_at_limit` (both look) | Implemented on synthetic spectra | `isar/analyze/coupled/refit.py` (`_connection_reasons`, `_width_limits`), `types.py`, `isar_gui/inter_words.py` | `tests/analyze/test_coupled_fixed_fit.py` |
| Energy priors from both sides in an anchored batch, each neighbour through the fit in use | Implemented on synthetic spectra | `isar/session/inter_spectra/runner.py` (`anchored`) | `tests/session/test_inter_spectra_runner.py` |
| Per-spectrum log line: one `inter fit` line as each spectrum's refit finishes (ring or round, mode set, each listed change priced and whether it entered, restarts, seconds, flags) | Implemented; written through the module logger, not to a per-batch file (section 4, Pool) | `isar/session/inter_spectra/runner.py` (`log_fit`) | `tests/session/test_inter_spectra_runner.py` |
| q-change penalty: Welsch from zero, tolerance = tolerated slope x link spacing, neighbour uncertainty propagated (`s_eff^2 = s^2 + (C/2) sigma^2`) | Implemented | `isar/analyze/coupled/engine.py`, `refit.py`, `settings.py` | `tests/analyze/test_coupled_engine.py`, `test_coupled_refit.py` |
| A mode hidden under another: guard against twin merges, evidence by integrating over the intensity split, undetermined status | Kept in the repository, not run by a batch (2026-11-04); the split integral is the search's | `isar/analyze/coupled/engine.py`, `refit.py` | `tests/analyze/test_coupled_hidden.py` |
| Listed-candidate search: the incoming candidate, subsets of the changes the link lists for the spectrum (add, split, drop) and each crossing alternative, priced in one pass; a search record of every candidate | Kept in the repository, not run by a batch (2026-11-04); built and tested on synthetic cases | `isar/analyze/coupled/search.py`, `refit.py`, `types.py` | `tests/analyze/test_coupled_search.py`, `tests/analyze/test_coupled_search_real.py` |
| Joint objective and gradient in the coupled pricing (ruling A): analytic derivatives of the Poisson term, heights, background, priors and split coordinates, in the fast pass's z coordinate (widths as t on their own range, IS27); column differences for mode energies and widths; curvature from differenced gradients; one-pass split polish. The solo fast pass keeps its finite differences | Partial: built and tested; it agrees with the finite-difference path on the real ordinary and bound cases, and two real hidden pairs miss the plan's 0.02-nat reading (section 10), accepted by IS25 | `isar/analyze/coupled/engine.py`, `refit.py`, `isar/analyze/prior.py` (`hessian_from_grad`), `isar/analyze/fast_pass.py` (the hook that returns none) | `tests/analyze/test_coupled_gradient.py` |
| Count-first decision: a count's total probability over its distinct labellings, a count change held by `evidence_hysteresis_nats`, label confidence judged within the chosen count | Kept in the repository, not run by a batch (2026-11-04) | `isar/analyze/coupled/rules.py` (`decide`), `search.py` | `tests/analyze/test_coupled_rules.py`, `tests/analyze/test_coupled_search_real.py` |
| Loss price for a branch that disappears: a branch a neighbour predicts in the window and the candidate does not name pays the mirror of the entry price, once per spectrum per snapshot | Partial: no longer acts in a batch (2026-11-04), which has no candidates; kept with the search; its size reuses `birth_edge` and `birth_mid`, uncalibrated | `isar/analyze/coupled/refit.py` (`_Spec.loss_terms`), `engine.py` | `tests/analyze/test_coupled_loss.py` |
| Width at the floor and the quasi-elastic line: a batch keeps the fit in use's width mask and model line (2026-11-04). A new (added or split-off) mode whose free width rails to the floor is priced as its own resolution-limited (delta) twin when that twin fits; a new mode railed with no usable twin is no answer and stays out. The other twins (the elastic line, a zero background or QE) are priced only by the search, which no batch runs | Implemented on synthetic spectra (the other twins: kept, not run by a batch) | `isar/analyze/coupled/refit.py` (`_fit_set`), `engine.py`, `rules.py` | `tests/analyze/test_coupled_width_floor.py`, `tests/analyze/test_coupled_search_real.py` |
| Counts above six: refinement, the release fit and storage accept a count beyond six; a batch's count is the fixed mode set's, which has no ceiling of its own | Implemented on synthetic spectra | `isar/analyze/coupled/search.py`, `refit.py`, `isar/session/inter_spectra/runner.py` | `tests/analyze/test_coupled_search_real.py`, `tests/analyze/test_coupled_search.py` |
| Refit pool: each spectrum's fit, its close-pair restarts and its finish stream through one pool as three steps, sized to the item's cores (`ISAR_WORKERS`, when set, caps it); a failed step fails only its spectrum; the timing is reported in the batch's `end` record | Implemented; the timing of the built batch is not yet measured (section 10) | `isar/session/inter_spectra/runner.py`, `isar/analyze/coupled/refit.py` (`refit_step`), `tools/inter_timing.py` | `tests/session/test_inter_spectra_runner.py`, `tests/test_inter_timing_tool.py`, `tests/session/test_inter_service_e2e.py` |
| Mode linking core: each link's modes matched as a whole (the one-to-one matching of least total mismatch on the peak energy, width and intrinsic intensity of each mode), the matched pairs joined grid-wide, contradictions, near ties, predictions from neighbours, listed changes (add, split, drop), markers at two sigma, JSON form | **Partial**: built and tested on synthetic grids; the tolerances are set from one run on a copy of the operator's experiment (section 10) and stay uncalibrated (section 5); saved and queued (rows below); run by every batch as its linking stage (section 4) | `isar/analyze/coupled/linking.py` | `tests/analyze/test_coupled_linking.py` |
| Branch identity: persistent ids, loops, returns, cumulative T drift | Implemented | `isar/analyze/coupled/identity.py` | `tests/analyze/test_coupled_identity.py` |
| Anchored batch: rings fixed by distance, seams, not reached | Implemented | `isar/session/inter_spectra/rings.py`, `runner.py` | `tests/session/inter_spectra/test_rings.py`, `tests/session/test_inter_spectra_runner.py` |
| Simultaneous batch: one go per start (one link, the priced mode sets, one sweep, the end link); no settling test, sweep cap or did-not-settle outcome (2026-11-04) | Implemented on synthetic spectra; the one-go path is not run on real data | `isar/session/inter_spectra/runner.py`, `schedule.py` | `tests/session/test_inter_spectra_runner.py` |
| Batch as one queue item (pass kind `inter`), run by the analysis service | Implemented | `isar/session/analysis_service.py`, `analysis_queue.py`, `isar_gui/queue_controller.py` | `tests/session/test_analysis_service.py`, `test_analysis_queue.py`, `tests/gui/test_queue_panel_inter.py` |
| Mode linking as a queue item (pass kind `link`): one item on the link lock, one core, its result saved by the one writer | Partial: built and tested at the queue and the service; the Link modes button and the automatic toggle start it | `isar/session/analysis_queue.py`, `analysis_service.py`, `isar/session/inter_spectra/linking.py` (`run_link`), `isar_gui/queue_controller.py` | `tests/session/test_analysis_queue.py`, `tests/session/test_link_service.py` |
| Mode linking: saved result in the machine store (kind `link`) | Partial: built and tested; the 2026-11-02 run on a copy of the experiment is recorded in section 10 | `isar/session/writer.py` (`save_links`), `isar/session/inter_spectra/records.py` | `tests/session/test_inter_links_store.py` |
| Mode linking as a batch stage: each ring of an anchored batch and each go of a simultaneous batch links on the frozen fits before its refits, and the batch links once more at its end; a stopped batch is linked at its end too, by a queued link item (2026-11-04); each link is saved with the batch (a batch links afresh; a saved record enters only as name continuity) | Partial: built and tested; not yet run in a real batch | `isar/session/inter_spectra/runner.py` (`link_stage`), `isar/session/inter_spectra/linking.py`, `isar_gui/queue_controller.py` | `tests/session/test_inter_spectra_runner.py` |
| Machine store: index, payloads, one writer under the workspace lock | Implemented | `isar/session/inter_spectra/records.py`, `isar/session/writer.py` (`save_inter`) | `tests/session/test_inter_records.py`, `test_inter_records_gpi.py` |
| Authority: the fit in use, picks, locks, acceptance, staleness | Implemented | `isar/session/inter_spectra/authority.py` | `tests/session/test_inter_authority.py` |
| User values (anchors, picks, locks, settings) with checked writers | Implemented | `isar/intake/inter_spectra.py`, `isar/intake/inter_values.py` | `tests/intake/test_inter_user.py` |
| Flags: score, severity, reasons, mode status | Implemented | `isar/analyze/coupled/refit.py`, `types.py` | `tests/analyze/test_coupled_refit.py` |
| Error bars, labelled conditional on the neighbours and the chosen model | Implemented | `isar/analyze/coupled/refit.py`, `isar_gui/inter_words.py` | `tests/analyze/test_coupled_refit.py`, `tests/gui/test_inter_words.py` |
| Settings: one table, checked reader, dialog | Implemented | `isar/analyze/coupled/settings.py`, `isar_gui/views/inter_settings_dialog.py` | `tests/gui/test_inter_settings_dialog.py`, `tests/intake/test_inter_user.py` |
| Calibration of the settings | Partial: five rows calibrated on synthetic tracks; the other rows uncalibrated (section 5) | `isar/analyze/coupled/settings.py` | — |
| Width and amplitude coupling (each linked mode's width and intrinsic intensity get a prior centred on its neighbours' prediction; a settled link lifts the width ceiling) | Partial: built and tested on synthetic spectra; uncalibrated (the tolerances are rows of section 5); the intensity prior is on the intrinsic intensity (IS28); a slot with no prediction keeps the fit's own priors | `isar/analyze/coupled/engine.py` (`SlotCoupling`), `isar/analyze/coupled/refit.py` (`slot_lifted`) | `tests/analyze/test_coupled_coupling.py`, `tests/analyze/test_coupled_intensity.py`, `tests/analyze/test_width_ceiling_coupled.py` |
| Read facade, Qt-free and read-only | Implemented | `isar/workspace_api/inter_spectra.py` | `tests/workspace_api/test_inter_facade.py` |
| Link read facade: each spectrum's newest saved link record read against the fits in use (names, stale, markers, changes, ties); the fit in use per unit | Implemented: built and tested; the views below read it for their names (no view draws its markers, PR #87) | `isar/workspace_api/inter_spectra.py`, `isar/session/inter_spectra/records.py` (`newest_link_by_scan`) | `tests/workspace_api/test_link_view.py` |
| Scan list: Anchor, Flag, Lock and Inter-spectra fit columns; Set as anchor, Remove anchor | Implemented; tested offscreen, no person walk-through yet | `isar_gui/views/scan_list.py`, `isar_gui/viewmodels/scan_list.py` | `tests/gui/test_inter_scan_list.py` |
| Processing dock: anchors list, direction, preview line, the two Start buttons, the Link modes button and the automatic linking toggle | Implemented; tested offscreen, no person walk-through yet | `isar_gui/views/inter_batch_box.py`, `isar_gui/viewmodels/inter_batch.py`, `isar_gui/queue_controller.py` | `tests/gui/test_inter_batch_box.py`, `tests/gui/test_queue_controller.py` |
| Scan(s) view: flat fit dropdown with Up/Down navigation, Make authoritative, Lock, reasons, error-bar label, waterfall | Implemented; tested offscreen, no person walk-through yet | `isar_gui/views/inter_fit_bar.py`, `isar_gui/viewmodels/scans_page.py`, `inter_fits.py` | `tests/gui/test_inter_fit_bar.py`, `test_inter_fits.py`, `test_scans_page_inter.py` |
| Phonon labels in the Scan(s) view and the scan inspector: every mode of a spectrum whose links apply is its phonon (P<n>, lone ones included), one colour per phonon; "Label modes in energy order" restores Mode N; names only on the fit the links were made on, and out-of-date links say so. The link notes are drawn nowhere (the record keeps them; PR #87); the scan list's Flag column holds review flags only; read only, no link editing | Implemented; tested offscreen, no person walk-through yet | `isar_gui/viewmodels/scans_page.py`, `isar/plotspec/builders.py`, `isar_gui/viewmodels/scan_list.py`, `isar_gui/views/scan_inspector.py` | `tests/gui/test_link_scans_page.py`, `tests/plotspec/test_builders_scans.py`, `tests/gui/test_link_scan_list.py`, `tests/gui/test_link_inspector.py` |
| Config > Inter-spectra fitting settings | Implemented | `isar_gui/main_window.py`, `isar_gui/views/inter_settings_dialog.py` | `tests/gui/test_inter_settings_dialog.py` |
| Analyzer array shift- and ctrl-click path gestures (no link strip), and the analyzer path as the whole Dispersion view of an analyzer-array experiment (one series per phonon at E*, one temperature) | Implemented: built and tested offscreen with the real widgets; no person walk-through yet | `isar_gui/viewmodels/analyzer_path.py`, `isar/workspace_api/analyzer_path.py`, `isar/plotspec/path_builders.py`, `isar_gui/views/dispersion_pane.py`, `isar_gui/views/analyzer_array_dock.py` | `tests/workspace_api/test_analyzer_path.py`, `tests/plotspec/test_path_builders.py`, `tests/gui/test_analyzer_path_gestures.py`, `tests/gui/test_analyzer_path_dispersion.py`, `tests/gui/test_link_array_cells.py` |
| Export and run report show the authoritative fit, labelled | Implemented | `isar/report/export.py`, `isar/report/run_report.py` | `tests/report/test_export_inter.py`, `tests/session/test_run_report_inter.py` |
| Real-data proof and the timing record | Partial: the proof is written and gated (`ISAR_LETACON_INTER=1`) and not yet run on the real set; the q side is its own module. A finish-line timing run on a copy of the experiment is recorded in section 10; it is not the proof | `proofs/test_letacon_inter_spectra.py`, `proofs/inter_spectra_checks.py`, `proofs/test_letacon_inter_spectra_q.py`, `proofs/inter_spectra_q_checks.py` | `tests/test_inter_spectra_proof_checks.py`, `tests/test_inter_spectra_q_checks.py` |

## 2. Terms

- **Spectrum**: one analyzer's data in one scan, keyed `<experiment>:<scan_number>:<detector>`.
- **Fast solo fit**: the spectrum's stored fast-pass result. This pass never changes it.
- **Neighbours**: two spectra are q-neighbours when they are adjacent analyzers of one scan on the 4 × 7
  grid (same row, one column apart, or same column, one row apart; no diagonals). They are T-neighbours
  when they are the same analyzer at adjacent temperatures among scans of one series. No link joins two
  spectra that differ in both q and T.
- **Series**: the code's word for the design's *scan set*: scans at one q setting, at several temperatures.
- **Penalty**: a cost added to a spectrum's fit for disagreeing with a neighbour. It is soft: it makes a
  move expensive and never forbids it.
- **q-change penalty**: what an anchored matched mode pays for its energy change from the neighbour branch it
  continues. It is Welsch from zero, so every nonzero shift pays something, and it levels off at the cap.
- **Tolerated slope**: the energy change per unit of q distance that a q-change penalty tolerates, in meV per
  r.l.u. when the scan has no lattice loaded and meV per inverse angstrom when it has one. Times the q distance
  between two spectra it gives their tolerance; each slope is used only in its own case.
- **Curvature term**: a q triplet of collinear spectra; the middle one's mode energy is penalised for
  leaving the straight line through the outer two.
- **Anchor**: a spectrum the user trusts, from which an anchored batch grows. Only a user chooses one.
- **Ring**: in an anchored batch, a spectrum's distance in neighbour steps to the nearest anchor of the
  batch. **Seam**: a link joining two spectra of the same ring that were reached from different anchors.
- **Anchored batch**: grows outward from the anchors, ring by ring. **Simultaneous batch**: every selected
  spectrum is refitted against its neighbours in one go (one round and one sweep per start, section 4). **Sweep**: one pass of refits over a
  simultaneous batch's free spectra.
- **Refit**: one fit of one spectrum inside a batch. Each completed refit stays in the spectrum's history.
- **Authoritative fit**: the fit ISAR uses for a spectrum: the fast solo fit, a user's pick, or the newest
  accepted refit. Neighbours in later batches see it, and reports and exports use it.
- **Lock**: a user mark that stops every batch from refitting a spectrum. A locked fit is never replaced.
- **Flag**: a mark sending a spectrum to a person, with its reasons and their severity.
- **Stale**: a refit whose inputs (calibration, hkl, resolution, or a neighbour's fit it was made from)
  have changed since it was made. A stale fit is never authoritative.
- **Mode status**: per mode, one of `determined_by_own_data`, `held_by_neighbours` or `poorly_determined`.
- **Neighbour-informed prior**: the same coupling as a penalty, seen as a prior on a spectrum's mode energies when
  the mode count is chosen. A neighbour's own uncertainty widens its prediction.
- **Not observed here**: a branch a neighbour predicts that this spectrum does not show. It is recorded as not
  observed, never as a death followed by a birth.
- **Did not settle**: a simultaneous batch stopped at its sweep cap before its fits settled. Its results are kept
  and the spectrum is flagged. Retired 2026-11-04: no batch raises it; the read facade still shows one saved before then.
- **Mode linking**: the step that decides which modes of neighbouring spectra are one branch, before any coupled
  fitting. It chooses links and starting values only; its scores never enter a fit's likelihood.
- **Linked branch**: modes of two or more spectra that mode linking joined, with one grid-wide name (`B<n>`).
  An anchored mode is a branch of its own id even alone.
- **Phonon**: every fitted mode of a spectrum is one. A mode on a branch carries the branch's name; a mode on no branch
  is a lone phonon, named from the same counter (`B<n>`, shown `P<n>`). Lone names label only: a lone phonon predicts
  nothing into its neighbours, and the branches, changes and notes are the same with or without it.
- **Peak energy E\***: where a mode's unconvolved intensity peaks, meV. For a sharp mode it is omega0; an
  overdamped or broad mode is matched by E\*, never by its omega0.
- **Near tie**: two consistent ways of linking whose summed mismatch differs by less than `tie_margin`, reported as
  one local group (a swap at a crossing, or two rivals for one mode), never forced to one answer.
- **Contradiction**: a link that would put two modes of one spectrum on one branch, or join two anchored
  branches. It is refused, and reported only when the link's matching chose the pair and its two modes were each
  other's best match; a refused worse alternative is not reported.
- **Change marker**: a mode on a branch whose energy, width or intensity differs from what its neighbours predict by
  more than twice the combined uncertainty (the tolerance and the mode's own uncertainty in quadrature).
- **Settled mode**: a mode on a branch that is in no near tie and no contradiction.
- **Fixed mode set**: a spectrum's modes in one batch: its fit in use's modes with those of the link's listed adds and splits that the priced comparison enters (never its drops), set the first time the batch fits it and frozen for the batch.
- **Close pair**: two modes of one spectrum that the tie-breaker penalty acts on (the fade of their equal-split separability, `exp(−D_eq / pair_penalty_d0)`, is at least 0.05). The coupled refit runs restarts on each.
- **Tie-breaker penalty**: the soft cost, in nats, of two modes that the data cannot tell apart from one mode. It is part of the model wherever two modes are close (section 4; `docs/STATS_PRIMER.md` §2.5).

## 3. Invariants

Each line names the failure it prevents.

- **The fast solo fit is never changed by this pass.** It is the stored Stage II result that `results.csv`,
  `modes.csv` and campaigns read. `save_inter` never touches `session.json`, `results.csv` or a fast solo fit.
- **ISAR never writes an anchor, a pick, a lock or a setting on its own** (AGENTS.md hard rule 1). They are
  entries of `intake/inter_spectra.jsonl`, written only by the GUI in process through `isar/intake/inter_values.py`.
- **The machine store is append-only, has one writer and deletes nothing.** `save_inter` and `save_links` append
  under the workspace write lock and write the index last, so a payload that no index line names is never read.
- **One resolver decides the fit in use.** `isar/session/inter_spectra/authority.py` (`resolve`, `effective`)
  serves the export, the run report and the read facade, so the views cannot disagree. A user pick and a
  machine acceptance are ordered by the intake `seq`, never by wall-clock time.
- **The ledger is parsed once per change of the file.** The facade, the node and q readers and the anchor save read
  `session.json` through `isar.session.state.load_shared`, a one-slot parse keyed on the file's mtime, size and inode
  (every write is an `os.replace`), so an anchor click and the reads it triggers parse it zero times on a warm cache.
  The shared object is read-only; code that saves the ledger keeps `SessionState.load`.
- **A stale fit is never authoritative.** A stale locked fit stays locked, is flagged stale, and never
  conditions a new fit.
- **A locked spectrum is never refitted.** A refused or failed refit never becomes authoritative; the
  previous authoritative fit stays.
- **Acceptance is checked when the refit is saved**, against the current locks and input versions. A refit
  that finished is not yet authoritative (section 4).
- **Rings are fixed by distance before a batch runs.** A ring conditions only on the state frozen when it
  starts, so a spectrum that finishes early never changes what its ring-mates see.
- **Anchors are not refitted and pay no penalty** in an anchored batch.
- **The statistics are unchanged.** Counts are raw Poisson with the Baker–Cousins likelihood and no σ floor
  (hard rule 3). Anti-Stokes amplitude is derived from the Stokes amplitude, never fit (hard rule 2). Each
  spectrum keeps its own resolution, calibration and exposure. The central quasi-elastic line of a BL43LXU
  fit is a per-spectrum nuisance and is never coupled.
- **Pricing is exact where it is done.** The price cache belongs to the candidate search, which no batch runs (2026-11-04). Where it ran, a cached price was reused only when every value it depends on was unchanged (the runner's cache keys hashed those values, including each job's starts and the batch's merged branch names), and the cache lived within one batch. Reusing prices across a changed snapshot stays a measured option for the operator and is not applied.
- **Error bars are conditional.** A refit's error bars use the fast pass's covariance method at the refit's
  own solution. They are labelled "Error bars are conditional on the neighbours and the chosen model.", and
  later sweeps never treat them as independent precision.
- **The layers stay Qt-free**: `isar/analyze/coupled`, `isar/session/inter_spectra`, `isar/intake/inter_spectra.py`
  and `isar/workspace_api/inter_spectra.py` import no Qt (hard rule 6). The GUI computes nothing: it shows what
  the facade reads and sends the user's actions down (hard rule 7). `tests/test_import_contract.py` lists the
  names the GUI may import from this work.
- **Links are frozen within a ring or round.** A ring or round links once, on the fits it froze at its start, and every refit in it reads that one link, so a ring-mate never sees a relabelling half-way through a stage (section 4, link stage).
- **A batch's mode sets are frozen.** A spectrum's mode set is set the first time its batch fits it, and no later ring or round adds, drops or removes a mode, so the links a neighbour was fitted on still hold. The failure this prevents: a later ring that removes a mode silently breaks the links its neighbours were fitted on (IS37, IS45). The set's entries are made by the priced comparison of section 4, once, at that first fit.
- **A linking score never enters a likelihood.** The matching costs are built from the same counts a fit uses, so putting one in a likelihood would count the evidence twice; a neighbour's predicted width and intensity enter only as the priors of section 4.
- **The fit chosen in the Scan(s) view is session-local and is not saved**, so a stored choice is never a
  second authority on which fit ISAR uses.

**Browsing fits (2026-10-31).** The Inter-spectra fitting dropdown lists every completed fit of the active
spectrum directly, oldest first: Fast solo, then refits with their batch and ring or sweep names. There
are no batch submenus. Unchanged refits remain visible and labelled; authoritative and stale fits are
marked, and the displayed fit is checked. In the open dropdown, Up/Down immediately displays the
highlighted fit without closing the list or requiring Enter. Esc closes the list and leaves that fit
displayed. Mouse hover alone does not change the displayed fit. With the dropdown button focused and
its list closed, Up/Down shows the
previous or next fit directly, including across batches, stopping at either end. Browsing only changes
the displayed fit; Make authoritative and Lock this fit remain explicit actions in the existing bar.

## 4. How a batch fits

**Anchored batch.** The user sets anchors, then starts an anchored batch with one direction: spread in q (every analyzer of each anchor's scan), spread in T (each anchor's analyzer at every temperature of its series), or spread in q and T. Rings are the distance in neighbour steps along the links that direction permits. Ring 1 is refitted first, each spectrum against the anchors and the neighbours already fitted in this batch. Ring k starts only when ring k−1 is complete. A seam is measured when its ring completes, and flagged `seam_disagreement` when the two sides disagree. A locked spectrum in scope counts as fitted from the start. The anchors a batch used are recorded with it, so later edits to the anchors never change a past batch. Each ring opens with a link stage (described below), and each spectrum's stage 1 in a ring is its fixed mode set (described under mode count and entry). Energy priors come from both sides: a spectrum's energy prior is built from each neighbour on either side, through the fit in use (its solo fit, an earlier batch's refit, or this batch's refit once its ring is done); the anchors only determine which spectra are held fixed (IS40). Width and intensity priors come from the link's predictions, as before.

**Simultaneous batch.** Every selected spectrum pays the penalties on its links and curvature terms in one shared objective: its own Baker–Cousins cost, each term it appears in, and the tie-breaker penalty on its close pairs. A start runs one go, which is one round of two stages. Stage 1 fixes each spectrum's mode set by the priced entry of section 4 (mode count and entry): the fit in use's modes, with those of the link's listed adds and splits that enter by evidence, under the link's labels. No labelling is compared, and counts are not reopened after stage 1 (IS37). Stage 2 refines the continuous parameters with the mode sets held fixed, in one sweep of per-spectrum refits over conflict-free parallel groups; each refit includes every term the spectrum is in. A term shared by several spectra has one definition, held by every member's refit once the neighbour on the other end carries its branch; a branch that neighbour gains in this sweep enters at the next go, and until then the refit is flagged `incomplete_comparison`. The go then ends with its end-of-batch link. It does not iterate: there is no further round, no settling test, no sweep cap and no `did_not_settle` outcome (2026-11-04, section 8). Iterated refits are not guaranteed to reach the same answer from every start, so the operator's next go starts from the fits then in use, and he sees and investigates any movement.

**Link stage.** Mode linking (`isar/analyze/coupled/linking.py`) runs first in every ring and every go, on the fits frozen when it starts, and once more when the batch ends, on the fits then in use. Each run is saved with the batch as a `links` event (stage `ring<r>`, `round<n>` or `end`), and the refits of that stage read that one link: its labels relabel the frozen map (a mode the link leaves on no branch keeps its id unless the link uses that id for a branch, and is then given a new one, so no unlinked mode is fitted or coupled on a branch; an id that unlinked modes of two or more spectra of the stage all carry is given up by each of them, since the link no longer ties them), its listed adds and splits enter the mode sets only where the priced comparison enters them (mode count and entry), and its settled modes lift the width ceiling. A batch links afresh from its frozen fits; a saved record enters only as name continuity, so a branch keeps its name across rings and rounds where its links hold. A batch the operator stops still ends with a link (2026-11-04, section 8): the GUI queues one link item on the fits then in use, once every result the batch handed to the writer is saved, so the names are on the refits the batch kept. A finished batch makes this link inside its runner, as before.

**Member states.** While a batch runs, the runner reports every member's state: `waiting`, `fitting` (a pool
worker holds its job; at most `workers` at once), `done`, `refused`, `failed`, `not_reached`, or `held` (anchor,
valid lock or stale lock), with a reason for the last four and for a stale lock. It sends the whole map as a
`members` event, at most one per 0.25 s from pool transitions, and at each phase boundary and as each pool run ends (a
returned job is never left showing `fitting`). If a worker dies mid-run, the jobs not yet sent fail with its error and the
stage is still saved; the next stage's first submit raises, as it did before. The analysis service
relays it between the item's `started` and `finished` (protocol and ordering: the `analysis_service.py` module
docstring). The events change no fit, acceptance, save, count or progress record. The GUI's Analyzer array dock follows
each member's state from the queue controller's copy of this map (`design/gui.md` §0decies).

**Acceptance.** A completed refit becomes authoritative only if, at save time, none of these holds: its
spectrum is locked (`locked`); the user picked a fit for it since the batch started (`picked`; the batch then
refits it no more, and only a pick older than the batch start is one the batch replaces); the inputs it was
fitted from do not match the experiment's record at save time (`inputs_changed`); the inputs of a neighbour it was
conditioned on do not match (`neighbour_inputs_changed`); or a neighbour's fit it used is no longer
the one in use (`neighbour_fit_changed`). The writer compares the hash stamped at the freeze with one computed from
the stored rows, so it can say that they differ, never when or why. A refused refit stays in the history with its
reason, and the batch row shows it as "fitted but not saved". A locked spectrum whose locked fit is out of date is
neither refitted nor used, so it counts with the fixed spectra, not as not reached, and the end record lists it
under `stale_locks`. Refit outcomes
are `accepted`, `unchanged`, `rejected` or `failed`; only `accepted` and `unchanged` are offered as fits. A
batch's newest accepted refit of a spectrum becomes its authoritative fit unless the spectrum is locked. A
batch the user starts replaces an unlocked spectrum's authoritative fit; ISAR never changes a pick or a lock.

**Mode count and entry.** A spectrum's count in a batch is the fixed mode set's: the fit in use's modes plus those of the link's listed adds and splits that the priced entry below enters, set the first time the batch fits the spectrum and never changed by the batch (IS37, IS31). An add inserts a mode at a predicted branch's energy. A split replaces one mode by the modes of the predicted branches it names (two in the usual case; where several splits name one mode, one mode per distinct branch, with no cap), at their predicted energies, its height divided in proportion to the predictions' intrinsic intensities. A drop is never applied. Each added or split-off mode starts with a free width and the link's predicted width as its prior; the fit in use's width masks and model line are kept (IS39). Every mode carries the link's best branch (IS38). A mode that no neighbour matches pays the entry price `birth_edge`, and `birth_mid` more when it appears in the middle of the energy window, not at its edges (IS9); the entry price stays in the fit's objective. A mode the link leaves on no branch stays in the fit with its own priors and predicts nothing, since nothing matches it (IS45). A batch's count moves only by priced entry, and `count_changed_by_neighbours` and `neighbour_supported_split` are not raised by a batch. The count search (the incoming fit and each subset of the listed changes, priced by evidence, with a count change held by `evidence_hysteresis_nats`; the crossing alternatives; the loss price of a branch that disappears (IS19); the split integral of a hidden pair) is kept in the repository and no batch runs it (2026-11-04). The operator's goal for the pass is to link the modes and answer the easy questions, and the comparisons the search made are for a later targeted pass.

**Priced entry (2026-11-04).** Stage 1 of each ring and each go prices the listed adds and splits before the stage's refits, through the refit pool, one map per ring or go (`refit.evidence_assignment`, beside `fixed_assignment`). For each spectrum, the incumbent (the fit in use's modes under the link's labels) is fitted against the neighbours frozen when the ring or go started, and its bounded-Laplace log evidence, ln Z, is taken once. The listed adds and splits are then tried one at a time, the strongest predicted intensity first (sequential, not every subset: 2^m fits for m changes is the cost that made the old search take hours). Each try fits the incumbent plus that one change with every continuous parameter reoptimised, and compares ln Z by the same bounded Laplace approximation and the same priors the pass already uses (the neighbour energy, width and intensity priors, the tie-breaker penalty, the coordinate Jacobians and the model weights, each counted once). A change enters when its ln Z exceeds the incumbent's by more than `evidence_hysteresis_nats` (0.1 nat by default, strictly). That margin is a provisional stability margin, not a derived significance level. An entered change becomes the incumbent, and the next change is compared against it. Every split is priced like any other, with no cap on how many enter; a listed change that names a branch the spectrum already carries is not tried again. The loss price of IS19 is not charged here. Only the mode count changes: each candidate keeps the fit in use's width masks, model line and background form (IS39), and no structural variant is explored. A change whose comparison has no answer does not enter. An unresolved split, usually two modes within a resolution width where the likelihood is nearly flat along their intensity trade, flags the spectrum `connection_ambiguous` (look), naming the split, for the operator to judge; no split integral is run here. An unresolved add is left out without a flag, and the log line says why. If the incumbent itself has no answer, no change is tried, the spectrum keeps its incoming mode set, and the log line says so; its refit still goes ahead. The set is then frozen (IS37). The labels stay the linker's (IS38): evidence decides how many modes a spectrum has, never which branch a mode belongs to. Each try is named in the spectrum's log line (IS47) and kept in the saved refit record. Cost: one fit of the incumbent plus about one per listed change, roughly 5 to 10 extra fits per spectrum on the 302 K data (an estimate from the counted changes, not timed). It stops further unsupported modes; it does not clean up fits that earlier batches inflated.

**The q-change penalty.** In an anchored batch a matched mode pays `C(1 - exp(-z^2/2))` for its energy change from the neighbour branch it continues, where `z` is the shift over the tolerance (IS17). The curve rises from zero, so every nonzero shift pays something: small shifts very little, the steepest rise at the tolerance, and a levelling off at `penalty_cap`, so a large shift never costs without bound. The tolerance is the tolerated slope of the scan's metric times the q distance to that neighbour (IS18): `q_slope_meV_per_rlu` when the scan has no lattice loaded, `q_slope_meV_per_invA` when it has one, and each batch records which slope it used. The width is `s_eff^2 = tolerance^2 + (C/2) sigma^2`, where `sigma` is the held neighbour's energy uncertainty. In an anchored batch the spectrum being refitted adds none of its own; a simultaneous batch adds both ends'. Near its centre the Welsch curve acts like a bell curve about 2.5 times narrower than its width setting, which is why the factor is `C/2`. The same width rule applies to the temperature and curvature penalties, whose `shape_family` (welsch or quartic) still governs only those two; the q-change penalty is always Welsch. A shift of exactly zero costs exactly zero.

**A mode hidden under another.** The split integral is not run by a batch (2026-11-04). A weak branch under a strong mode that the fit in use already carries keeps its own mode in the fixed set, with the link's label and its priors from the neighbours on both sides (IS16, amended). When that weak branch's mode would come only from a listed split, the priced entry of section 4 decides it: if the comparison has no answer (the evidence cannot separate the pair), the mode is left out and the spectrum is flagged `connection_ambiguous` (look), naming the split; if the comparison says it does not pay, it is left out with no flag. The intensity division between the two is set first by the spectrum's own counts (its Poisson cost settles it when the pair shows a shoulder or a width difference) and, where the shapes are alike, by those priors; the tie-breaker penalty's restarts only help the fit find the division, and no integral over the split is taken. Each mode reports its own fitted intensity and the fit's error bars, however close it sits to another mode; no group or "not separately determined" report is made (IS42). The split integral (`integrate_split`) stays in the repository for the later targeted pass.

**A width at the floor.** A mode whose width reaches the fitting floor is resolution limited. A batch keeps the width mask of the fit in use (IS39), so an incoming mode's width is never repriced: a set whose fit rails an incoming mode has no answer, and the incumbent's no answer keeps the incoming set (section 4). A new mode (added or split off) whose free width rails to the floor is priced as its own resolution-limited reading, the engine's delta twin (2026-11-04, `refit._fit_set`), and the comparison of section 4 uses that price. A new mode railed with no usable twin, or a fit still holding a free width at the floor after its twin, is no answer and stays out. A mode priced as resolution limited has a pinned width, so it is not flagged; a free width that ends at the floor is flagged `width_at_limit` (look), with the floor and the ceiling read by the same rule as `at_width_cap`.

**A width at its ceiling.** The coupled refit allows each mode the width the fast pass allows (ruling IS27): at most
min(4 meV, ω0), with the width prior proper on that mode's own range, fitted, priced and given its covariance in the
fast pass's coordinate (the width as a fraction t of its range). A mode a settled link vouches for is lifted to the old 20 meV ceiling (`CoupledEngine(lifted=...)`, one flag per slot, set from the link's settled modes); an applied add or split slot is never lifted, since it is new and on no settled link yet. A refit's peak whose width sits at its ceiling carries `at_width_cap`, as a fast-pass peak does; a mode below
4 meV at its ceiling has Γ = ω0 and reads overdamped.

**Width and intensity coupling.** A linked mode's width and its intrinsic intensity (`matching_intensity`: the phonon's intensity with the Bose factor and the analyzer's efficiency divided out) each get a prior centred on what its neighbours predict (`Prediction.w` and `Prediction.A`). Both are soft in the family of the energy coupling, Welsch from zero and levelling off at `penalty_cap`, with the tolerances `width_tolerance_ln` and `intensity_tolerance_ln`, and each is a proper density, so a candidate with more linked modes is not charged an arbitrary extra cost. A slot with no prediction keeps the fit's own width and height priors. Neither prior adds a flag.

**The tie-breaker penalty.** Each close pair's penalty (`docs/STATS_PRIMER.md` §2.5) is part of the coupled objective J, of its release fit and of the continuous stage's gradient (by central differences over the pair's term), so it acts as a tie-breaker inside the fit. Evidence from the neighbours overcomes it (IS43). Each close pair then gets up to four restarts that keep the count: intensity moved from one mode toward the other, the reverse, the separation nudged wider, and narrower. A restart never moves a pinned energy or a delta width; with both energies of a pair pinned, only the two intensity starts are made (`refit._restart_starts`). The restarts are separate pool calls that run in parallel behind their spectrum's fit; the lowest J of the main solution and its restarts is kept, a tie keeping the earlier. This is what gets a fit out of two identical modes stacked on one another, which the smooth penalty cannot separate on its own.

**Flags.** A refit is flagged when its score is above `flag_threshold`, or when it carries any reason other than
the four notes (`neighbour_precision_unavailable`, `incomplete_comparison`, `release_fit_failed`,
`errors_unavailable`) and a neighbour-supported split, whatever its score. A split is a repair: it is shown
as repaired and kept off the review list, so the list is not flooded by successes. The score is F = R + U: R is the cost of coupling, how much worse the
spectrum fits its own counts coupled than in a release fit at the same count with its neighbours removed; U is
the largest unlevelled mismatch statistic on any link touching the spectrum, taken before its penalty levels off, so a
penalty that levels off cannot hide a jump. The change from
the fast solo fit is shown beside the score. Each reason carries a severity, shown as a plain word: serious,
look (warn) or repaired (info). The severities are:

- serious: `score_over_threshold`, `identity_ambiguous`, `loop_inconsistent`, `unresolved_return`,
  `did_not_settle`, `stale_locked_fit`;
- look: `count_changed_by_neighbours`, `unmatched_branch_mid_window`, `branch_lost_mid_window`,
  `seam_disagreement`, `linewidth_change`, `overdamped_entry`, `t_drift_cumulative`, `incomplete_comparison`,
  `release_fit_failed`, `connection_ambiguous`, `width_at_limit`;
- repaired: `neighbour_supported_split`, `neighbour_precision_unavailable`, `errors_unavailable`.

`connection_ambiguous` (look, 2026-11-04) names each near tie the link reports for the spectrum, with the branches the tied labelling moves, and each split the priced entry could not decide (section 4); `width_at_limit` (look) names each fitted width that ends on its floor or on its ceiling. A batch's count moves only by priced entry, so `count_changed_by_neighbours` and `neighbour_supported_split` are not raised by it.

`t_drift_cumulative` reports the total change of one branch across the temperature range, since a drift can
pass unflagged in single steps. `linewidth_change` and `overdamped_entry` report damping changes without moving
the energy; the width prior of a linked mode adds no flag of its own. A `seam_disagreement` flag from a measured seam gives the mismatch and the threshold it passed; one from an identity
seam gives the identity issue.

**Pricing arithmetic.** The continuous stage evaluates its joint objective and gradient (`gradients`, on by default; `False` keeps the finite differences), with the analytic parts: the Poisson term, the heights and background, the priors (coupling potentials included) and the split coordinates, all in the optimiser's z coordinate (each width as t in [0, 1] on its own range, chained back to meV; a split pair's widths as s in [0, 1] on the same range in the width prior's log coordinate, so the soft-log prior stays exactly flat there). Each mode's energy and width, and the quasi-elastic width, take a finite difference of only that mode's complete height-normalised, resolution-convolved column, at a step of `COLUMN_FD_REL` (1e-5) times the larger of the value and the width. The tie-breaker penalty's gradient is a central difference of its own pair terms (`_pair_penalty_grad`), taken only where a pair is in range. Curvature is the full observed Hessian, the central difference of the gradient, not a Gauss-Newton form. Fully analytic line-shape derivatives are not built. The solo fast pass keeps its finite differences: its objective-and-gradient hook returns nothing, so it calls the optimiser as before. The split integral's node-parallel arithmetic (IS26, D1) belongs to the search and is not run by a batch.

**Error bars.** The error bar of a refit is the fast pass's covariance method recomputed at the refit's own
solution, never the fast solo fit's numbers. It is labelled "Error bars are conditional on the neighbours and the
chosen model." Where the calculation fails it reads "Error bars unavailable for this fit." A deep pass on an
inter-spectra fit is not part of this work.

**Reports and exports.** The Scan(s) view, the run report's markdown and HTML, and `isar export` show the
authoritative fit with its source labelled ("Fast solo", "Batch 2 · sweep 3", "Your pick", "Locked"). `isar
export` writes the inter-spectra fit as a third pass, `inter`, beside `fast` and `deep`, and the authoritative
fit is marked. `results.csv`, `report/modes.csv` and the run report's `report.json` stay the fast-pass projection:
the frozen Stage II/III contract that `isar drive` reads. Campaigns are out of scope, so this is the reading of
the design's default "the authoritative fit is what reports, results tables and exports use" that plan decision
D6 records; section 9 lists it as awaiting the operator's confirmation.

**Pool.** Each ring or go streams its spectra through one refit pool as three steps. A spectrum's fit goes in first; when it returns, its close-pair restarts go in as separate calls, which run in parallel with the other spectra's steps; its finish (the lowest J, acceptance, error bars, release fit, flags and record) runs once its restarts are back. A failure at any step is that spectrum's failed refit, and the others go on. Each ring's or go's stage 1 prices its mode sets through the same pool, as one map before the stage's refits; a spectrum whose pricing fails fails alone, as its fixed set did. Rings stay sequential, because a ring conditions on the saved fits of the rings before it; identity reconciliation and saving are unchanged. The pool's workers are the item's cores, capped by `ISAR_WORKERS` when set. The per-spectrum log line is written as each finish returns.

**Every worker busy.** Not on the batch path (2026-11-04). The task scheduler that sent a candidate's inner fits, starts and split nodes to the pool (`isar/analyze/coupled/tasks.py`, the runner's `_Sched`) served the search's pricing; no batch calls the search, so no batch uses the scheduler. A batch's workers are the refit pool's (Pool, above).

**Time.** The 30-minute figure is a target, not a limit (IS13). Nothing stops, fails or is cut because of it; going well over it is a reason to optimise. The batch's timing report sits in the `timing` block of its `end` record, printed by `tools/inter_timing.py`: per spectrum and ring, the runner's tallies; per batch, the pool's utilisation and the slowest spectrum and ring. The priced-entry batches have no timing record yet: the operator measures them (IS46). Section 10 says what has been measured.

## 5. Settings

Settings belong to one experiment (they are intake values, not global settings); the dialog says so. A save
writes only the values the user changed, and a value sent back to its default is written as a reset. Each batch
records the full effective values it ran with. Five rows are calibrated on synthetic tracks, not on real data;
every other row is uncalibrated. No row has been calibrated on real data.

The table mirrors `SETTING_DEFS` in `isar/analyze/coupled/settings.py`. Its group column says where the dialog
shows the setting: *up front* (the dialog's main section), *advanced* (the advanced section) or *not in the
dialog*. `tests/docs/test_inter_spectra_docs.py` keeps the names, marks and help sentences equal to the code.

| Setting | Group | Default | Bounds | Mark | What it does |
|---|---|---|---|---|---|
| `q_slope_meV_per_rlu` | up front | 10.6 meV/r.l.u. | > 0 to 1000 | uncalibrated | Anchored batches, scans with no lattice loaded: the energy change per reciprocal lattice unit of distance between neighbours at which the q-change penalty rises most steeply. |
| `q_slope_meV_per_invA` | up front | 8 meV/Å⁻¹ | > 0 to 1000 | uncalibrated | Anchored batches, scans with a lattice loaded: the energy change per inverse angstrom of distance between neighbours at which the q-change penalty rises most steeply. |
| `t_tolerance_meV` | up front | 1 meV | > 0 to 50 | calibrated on synthetic tracks, not on real data | Temperature neighbours: the energy change that costs little at the reference temperature step. |
| `flag_threshold` | up front | 54 | > 0 to 1000 | calibrated on synthetic tracks, not on real data | A spectrum is flagged for review when its flag score reaches this value. |
| `shape_family` | advanced | welsch | one of welsch, quartic | uncalibrated | The shape of the temperature and curvature penalties: welsch (bounded, the default) or quartic (for comparison); the q-change penalty is always welsch. |
| `step_exponent` | advanced | 0.25 | 0 to 2 | calibrated on synthetic tracks, not on real data | How much the tolerated temperature change widens with the size of the temperature step. |
| `q_curvature_scale_meV` | not in the dialog | 1 meV | > 0 to 50 | uncalibrated | Simultaneous q links: the energy scale of the curvature residual along a line of three spectra. |
| `reference_T_K` | not in the dialog | 100 K | > 0 to 1000 | calibrated on synthetic tracks, not on real data | The temperature step, in kelvin, at which the temperature tolerance applies unchanged. |
| `t_spacing_form` | not in the dialog | widen | one of widen, divide | uncalibrated | How the temperature step enters the penalty: widen (the default) or divide (the literal form, for comparison). |
| `penalty_cap` | not in the dialog | 12 deviance | > 0 to 1000 | calibrated on synthetic tracks, not on real data | The largest price one penalty term can add to a spectrum's fit. |
| `birth_edge` | not in the dialog | 2 deviance | 0 to 1000 | uncalibrated | The entry price of a branch that no neighbour predicts, at any energy in the window. |
| `birth_mid` | not in the dialog | 12 deviance | 0 to 1000 | uncalibrated | The extra entry price of such a branch when it appears in the middle of the window. |
| `collinearity_tol` | not in the dialog | 0.01 | 0 to 1 | uncalibrated | How far, relative to its length, the middle of three q points may sit off the straight line through the outer two. |
| `confidence` | not in the dialog | 0.8 | 0.5 to 1 | uncalibrated | A mode count or assignment whose probability is below this is reported as ambiguous. |
| `close_call_nats` | not in the dialog | 2 nats | 0 to 20 | uncalibrated | Alternatives within this much log evidence of the winner are kept for review. |
| `evidence_hysteresis_nats` | not in the dialog | 0.1 nats | 0 to 5 | uncalibrated | A count or assignment change must win the evidence by this margin, or the incoming one stays. |
| `linewidth_ratio` | not in the dialog | 2 | > 1 to 100 | uncalibrated | A linewidth change is reported when a mode's damping ratio to its fast solo fit passes this. |
| `link_slope_meV_per_rlu` | not in the dialog | 3 meV/r.l.u. | > 0 to 1000 | uncalibrated | Mode linking, scans with no lattice loaded: how far a mode's energy may move per reciprocal lattice unit of distance between neighbours before it counts as one full tolerance (the refit's q-slope is separate). |
| `link_slope_meV_per_invA` | not in the dialog | 2.3 meV/Å⁻¹ | > 0 to 1000 | uncalibrated | Mode linking, scans with a lattice loaded: how far a mode's energy may move per inverse angstrom of distance between neighbours before it counts as one full tolerance (the refit's q-slope is separate). |
| `width_tolerance_ln` | not in the dialog | 0.3 | > 0 to 5 | uncalibrated | Mode linking: how far a mode's width may differ between neighbours, as the natural log of the ratio (0.3 is about 35%), before it counts as one full tolerance. |
| `intensity_tolerance_ln` | not in the dialog | 1 | > 0 to 5 | uncalibrated | Mode linking: how far a mode's intrinsic intensity (Bose-corrected, divided by the analyzer's efficiency) may differ between neighbours, as the natural log of the ratio (1 is about a factor of 2.7), before it counts as one full tolerance. |
| `link_threshold` | not in the dialog | 9 | > 0 to 1000 | uncalibrated | Mode linking: two modes of neighbouring spectra are linked as one branch only when their summed mismatch in energy, width and intensity, each in squared tolerances, is at most this. |
| `tie_margin` | not in the dialog | 2 | 0 to 100 | uncalibrated | Mode linking: two ways of linking a crossing are reported as a near tie when their summed mismatch differs by less than this. |

A saved value of `q_tolerance_meV`, `q_change_scale_meV`, `composition_limit`, `stop_margin_nats`, `full_search`, `full_search_max_count`, `sweep_tol_meV`, `sweep_cap` or `cost_rel_tol` is logged and ignored, never reported as damaged, and has no effect; the two q slopes start at their defaults. The two q slopes are provisional until sample handling is written out. The rows marked calibrated were calibrated before the propagation of section 4 existed. That propagation also widens the temperature and curvature penalties, so for the same neighbour uncertainty those penalties, and the flag statistic they feed, are lower. Their marks stand and the calibration is not redone.

The tie-breaker penalty's two settings, `pair_penalty_cap` (1 nat) and `pair_penalty_d0` (4), are fast-pass settings in `[fitting.fast_pass]`, not rows of this table; both are provisional and uncalibrated (`docs/STATS_PRIMER.md` §2.5).

## 6. Storage

Paths are relative to the workspace folder. Every line is one JSON object.

| File | Written by | Holds |
|---|---|---|
| `intake/inter_spectra.jsonl` | the GUI, in process, through `isar/intake/inter_values.py` | user values: kinds `anchor`, `pick`, `lock`, `settings`; append-only, the latest wins |
| `report/inter_spectra.jsonl` | `save_inter` and `save_links` | the machine index: `batch_start`, `refit`, `stage` and `batch_end` lines, and `link` lines (one saved mode-linking result each); append-only |
| `report/inter_spectra/refits/<batch_id>.jsonl` | `save_inter` only | one payload per refit with a payload: `schema`, `fit`, `unit`, `came_in`, `came_out`, `overlay`; read by byte offset |
| `report/inter_spectra/links/<link_id>.jsonl` | `save_links` only | one payload per `link` line: `link` and `result` (the linking result as JSON, `isar.mode-links/1`); read by byte offset |

Example lines, with synthetic values:

An intake anchor (`kind` `anchor`; `declared` true sets the anchor, false removes it):

```json
{"id": "e3f1a09c7b2d", "seq": 4, "at": "2026-10-29T18:02:11.000000+00:00", "kind": "anchor", "declared": true, "targets": ["exp1:12:a03"], "reason": null}
```

A refit line of the index (`kind` `refit`; `payload` is the file, byte offset and length of its payload):

```json
{"schema": "isar.inter-log/1", "kind": "refit", "batch": "b5c0ffee12ab", "at": "2026-10-29T18:05:40.000000+00:00", "fit": "f0123456789ab", "unit": "exp1:13:a03", "stage": "ring", "index": 1, "status": "accepted", "reason": null, "seed": "fast", "inputs": "9d2c4e1f0a7b", "conditioned_on": [{"unit": "exp1:12:a03", "fit": "fast", "inputs": "7a1b3c5d9e0f"}], "flag": {"score": 3.2, "flagged": false, "reasons": []}, "why_changed": "the mode energy moved by 0.4 meV", "accept": {"decision": "authoritative", "reason": null, "detail": null, "intake_seq": 4}, "payload": ["b5c0ffee12ab.jsonl", 0, 1234]}
```

The other index kinds: `batch_start` carries the batch's number, kind (`batch_kind`), scope, anchors, rings, seams,
settings, preview, fixed and not-reached counts, terms, metric, `q_slope` (per scan with a q link: the metric and slope an anchored batch used; empty for a simultaneous batch), core, `intake_seq` and lattice epochs; `stage`
carries `stage`, `index`, `round`, `progress`, `seams`, `not_reached` and `link` (the link record its refits used);
`batch_end` carries how the batch finished, and `link` (the link made when it ended; null after a failed batch). Each refit payload holds what the refit came in with (`came_in`) and came out with (`came_out`, with
the branches not observed in it under `dormant`) and its `overlay`, so a history entry can be read in full. The
overlay carries the calibration, resolution and temperature the refit was made with (`calib`, `T`), so a locked
or older refit is drawn and exported in its own context, never in a later fast solo fit's.

A batch saves each link it uses with a `links` payload event (`{stage, scope, result}`, stage `ring<r>`, `round<n>` or
`end`) beside `start`, `stage` and `end`; the writer answers with the link's id. Each ring's or go's refit payload
carries it as `came_in.links`.

A `link` line names the scan keys it covers, the fits it was made from (`made_from`: fit and inputs hash per spectrum)
and their stamps, the next branch-name number, and where its payload is. A new branch's name continues the store's
names, so a result saved after another never refuses on names; its ids stay. The payload's `lone` gives each mode on no
branch its phonon's name (null on a branch); a payload saved before lone names has no `lone` and reads as out of date
until Link modes runs again.

```json
{"schema": "isar.inter-log/1", "kind": "link", "link": "l3a9f10c2e7d4", "at": "2026-11-02T09:14:03.000000+00:00", "scope": ["exp1:12", "exp1:13"], "batch": null, "stage": null, "made_from": {"exp1:12:a03": ["fast", "9d2c4e1f0a7b"], "exp1:13:a03": ["fast", "7a1b3c5d9e0f"]}, "stamps": {"exp1:12:a03": "5b0c1d2e3f4a", "exp1:13:a03": "6c1d2e3f4a5b"}, "next_name": 4, "digest": "0f1e2d3c4b5a6978", "counts": {"spectra": 56, "branches": 3, "ties": 1, "contradictions": 0, "excluded": 0}, "payload": ["l3a9f10c2e7d4.jsonl", 0, 18342]}
```

## 7. Code locations

| Module | Owns |
|---|---|
| `isar/analyze/coupled/settings.py` | the settings table (`SETTING_DEFS`) and the checked reader (`InterSettings`) |
| `isar/analyze/coupled/types.py` | the core's boundary types, flag codes and severities |
| `isar/analyze/coupled/engine.py` | the coupled engine: the mode energies, priors and penalties of one refit, and the width and intensity couplings (`SlotCoupling`, `_coupled_width`, `_intensity_per_height`); the joint objective and gradient (`objective_and_gradient`, `_value_and_grad`); the tie-breaker penalty's terms in J (`penalty_terms`, the `pair:` entries) and its gradient (`_pair_penalty_grad`); the split integral (`integrate_split`, `split_evidence`, `_polish`), not run by a batch |
| `isar/analyze/coupled/refit.py` | one spectrum's refit: the fixed mode set (`fixed_assignment`) and its priced form beside it (`evidence_assignment`: the incumbent's and each listed change's evidence, section 4), the continuous stage as three steps (`refit_step`: fit, close-pair restarts, finish; `refit` runs them inline), its release fit, flags and error bars; the search's `price` and `assign`, not run by a batch |
| `isar/analyze/prior.py` | the Laplace evidence; `hessian_from_grad`, the Hessian as a central difference of a gradient; the width ceiling and its density (`width_ceiling`, `ln_width_given`) |
| `isar/analyze/fast_pass.py` | the solo fast pass, with the tie-breaker penalty in its prior and count evidence (`pair_terms`, `pair_penalty`, `close_pairs`); its `objective_and_gradient` hook returns nothing, so the solo pass keeps its finite differences; `_ZMap`, the width coordinate t the coupled engine fits in too |
| `isar/analyze/pair_penalty.py` | the tie-breaker penalty's pure core (numpy only): `PairMode`, `in_range`, the closed-form `separability`, `fade` and `share_factor` |
| `isar/analyze/coupled/rules.py` | the pure decision rules: acceptance order, settling, flag score, the count-first decision |
| `isar/analyze/coupled/search.py` | one spectrum's listed-candidate search: the incoming fit, subsets of the listed changes and the crossing alternatives (`Search.feed`, `_decide`), the search record, and `crossing_flags`; not run by a batch since 2026-11-04, kept for the later targeted pass |
| `isar/analyze/coupled/identity.py` | persistent branch identity across neighbours |
| `isar/analyze/coupled/linking.py` | mode linking: the pure core, its result and JSON form |
| `isar/session/inter_spectra/nodes.py` | spectrum keys, series keys and temperatures |
| `isar/session/inter_spectra/layout.py` | the 4 × 7 analyzer grid and its adjacency |
| `isar/session/inter_spectra/graph.py` | links, curvature terms and the batch scope |
| `isar/session/inter_spectra/q_provider.py` | accepted per-analyzer hkl as q points |
| `isar/session/inter_spectra/rings.py` | anchored rings and seams |
| `isar/session/inter_spectra/schedule.py` | simultaneous terms and conflict-free parallel groups |
| `isar/session/inter_spectra/preview.py` | the batch preview line and the reason Start waits |
| `isar/session/inter_spectra/records.py` | the machine store's reader, and links |
| `isar/session/inter_spectra/linking.py` | mode linking, session side: the fits in use and what a link leaves out and why, the temperature series, the link queue item's run (`run_link`) |
| `isar/session/inter_spectra/authority.py` | the one resolver of the fit in use |
| `isar/session/inter_spectra/runner.py` | the batch runner: rings, rounds, sweeps and acceptance hand-over; `fixed_sets` (each spectrum's mode set, priced through `refit.evidence_assignment`), `refits` (the refit pool's three streamed steps), `log_fit` (the per-spectrum log line); `_Run.assign` and `_Sched` belong to the search and no batch calls them |
| `isar/analyze/coupled/tasks.py` | the task port the search's pricing sends its inner work through (no batch calls it since 2026-11-04), and the worker side's engine cache |
| `tools/inter_timing.py` | times one batch headless and prints its timing report (`report.txt`, `timing.json` with `--out`) |
| `isar/session/writer.py` | `save_inter` and `save_links`, the one writer of the machine store |
| `isar/session/analysis_service.py` | runs an `inter` and a `link` queue item in the analysis process |
| `isar/session/analysis_queue.py` | the queue's bookkeeping for `inter` and `link` queue items |
| `isar/intake/inter_spectra.py` | the user values, read (`fold`, `read_user`) |
| `isar/intake/inter_values.py` | the checked writers of the user values |
| `isar/workspace_api/inter_spectra.py` | the read facade the GUI and notebooks use, and the saved mode links read by `link_view` (the newest record of each spectrum's scan, its stale rule, the fit in use by `_in_use`, and each branch's members kept only from the record newest for their scan) |
| `isar/report/export.py` | `isar export`: the `inter` pass and the authoritative marks |
| `isar/report/run_report.py` | the run report: the authoritative fit in the markdown and HTML |
| `isar_gui/inter_words.py` | the plain words the views show |
| `isar_gui/queue_controller.py` | the GUI side of an `inter` queue item, and the end link queued after a stopped batch's saves (2026-11-04) |
| `isar_gui/viewmodels/inter_state.py` | the GUI thread's one holder of inter-spectra reads and writers |
| `isar_gui/viewmodels/inter_batch.py` | the Processing box's state: what each control shows and why it is greyed |
| `isar_gui/viewmodels/inter_fits.py` | the Scan(s) fit bar: greyed-button rules, info line |
| `isar_gui/viewmodels/inter_settings.py` | the rows and deltas of the settings dialog |
| `isar_gui/viewmodels/scan_list.py` | the scan list's Anchor, Flag, Lock and Inter-spectra fit columns |
| `isar_gui/viewmodels/scans_page.py` | the Scan(s) page's inter-spectra state |
| `isar/plotspec/builders.py` | the Scan(s) plot's mode labels (`ScansMark`: a phonon or Mode N at each mode's peak) and the colour of each phonon (`ScansCurve.phonon`) |
| `isar_gui/views/scan_inspector.py` | the scan inspector's fit table: the phonon of each Mode row in its Fast column (the Mode heading is shared with Deep and names nothing), and one line when the links are out of date or name another fit |
| `isar_gui/views/inter_batch_box.py` | the Processing dock's inter-spectra box |
| `isar_gui/views/inter_fit_bar.py` | the Scan(s) flat fit dropdown, Make authoritative, Lock and Waterfall of fits |
| `isar_gui/views/inter_settings_dialog.py` | Config > Inter-spectra fitting settings |
| `isar_gui/main_window.py` | the Config menu entry and the window wiring |
| `isar_gui/viewmodels/analyzer_array.py` | the analyzer array's picks, its gestures, and `path_keys` (the path of the picks) |
| `isar_gui/views/analyzer_array_dock.py` | the Analyzer array dock: the path line and the Plot path in Dispersion button |
| `isar/workspace_api/analyzer_path.py` | the analyzer path read: the picks of one scan as stops at cumulative q, the fit in use per stop, the phonon names from the saved links |
| `isar/plotspec/path_builders.py` | the analyzer path's plot spec, built from plain stops: one series per phonon |
| `isar_gui/viewmodels/analyzer_path.py` | the Dispersion pane's analyzer path: the picks, the line, the spec while the pane shows it, and the words of an empty plot |
| `isar_gui/views/dispersion_pane.py` | the Dispersion view of an analyzer-array experiment: the analyzer path alone (no Plot row, no fitted plot) |

## 8. Rulings

Each ruling is the operator's own words as the design records them, quoted verbatim (line breaks in the
record are joined; an ellipsis marks an elision in the record). Each carries its date. Each names what it
decided. None is restated here as a new requirement. Ruling IDs are stable names; the glossary's Ruling IDs
entry points here.

**IS1 — Purpose.** Decides what the pass is for: flagging, not replacing a person's judgement.
"our goal is to alert the operators when interesting things happen … we get the easy stuff right so only the hard stuff is brought up to their attention."

**IS2 — Hybrid batches.** Decides that an anchored batch comes first and a simultaneous batch after it, with anchors penalty-free only in the first.
"the first go with the first iteration is just pick one, work your way out from there. Then the second time … we do it all simultaneously if that's feasible … The anchor pays no penalty. That's only then for the very first run when we set the anchor after that, right, for the later simultaneous runs, then the anchor never matters."

**IS3 — Rings fixed by distance.** Decides that the frontier of an anchored batch is set before it runs, by distance only.
"the frontier and the meeting point between them should always be clear from the beginning and never vary based on processing time. It's based only on distance to the anchors along next nearest neighbors."

**IS4 — q and T together.** Decides that q-links and T-links act in one fit, along any number of directions.
"if we use two orthogonal links with T and Q, that's fine. And the same way that we can take two different Q directions or three different Q directions"

**IS5 — Penalties, not expectations; bounded shape.** Decides that neighbours share information through a penalty, and that the penalty is not linear and does not grow without bound.
"two scans should share information, so that a phonon that moves between them in unexpected ways is paying a penalty." And: "it shouldn't be linear. It should be smaller near zero and get larger further away … this isn't anything where the penalty has to grow exponentially."

**IS6 — Curvature across q, change across T.** Decides that simultaneous batches penalise curvature along q, and anchored batches penalise change from the one fitted neighbour. Amended 2026-11-04: the anchored half is superseded. An anchored batch's energy prior is built from both neighbours, each through the fit in use (IS40); the simultaneous half stands.
"curvature is better for dispersion … without neighbors we can't use it (we have only one end of the curve), but in simultaneous fitting it's far better."

**IS7 — Step size.** Decides that a larger temperature step widens the tolerated change, with a fourth-root starting point that is to be tested.
"I think the astros the fourth root rule is probably reasonable, not make it too large. I'm not married to it being like a linear penalty, a linear inverse penalty. We can see how that works out then."

**IS8 — Mode count.** Decides that neighbours may move a spectrum's mode count, option (ii). Amended 2026-11-04: a spectrum's count moves now come only from the link stage. The count is the fixed mode set's, the fit in use's count plus the link's listed adds and splits (IS37; section 4). Amended 2026-11-04 again: those adds and splits count only where the priced entry enters them, so the count is the fit in use's count plus the ones that enter.
"So (ii), and keep what I said in mind. I think it is right that two neighbors can talk each other into two spectra. That's very possible. I think that's fine because exactly we're sharing information."

**IS9 — A new mode costs more in the middle.** Decides that a mode entering at the edges of the window costs less than one entering in the middle.
"introducing a new mode has to have a cost, and it should especially have a cost when it's deep within the energy range … there's a big penalty if a new mode just pops in in the middle of our spectrum. And there's much less of a penalty if it pops in at the edges."

**IS10 — New branch, not a newly resolved one.** Design ruling, which the operator agreed to on 2026-10-29 (agreeing to the external review's point "(b)"). The design's words: "when a spectrum the fast pass fitted as one broad mode is recovered as two because its neighbours show both, a component has been added to that spectrum but no new phonon has appeared, and it does not pay the mid-window cost."

**IS11 — History, authority and locks.** Decides that every fit is kept and selectable, that a pick and a lock are the user's, and that an unlocked fit may be overwritten by a batch.
"we will want to be able to preserve the history of the fits for each analyzer scan so that we can see, first, how they've changed over time, and second, we can set one as authoritative … no, don't take fit number three, take fit number two … And then we'll need to have options like lock this fit for this particular analyzer scan." And: "If it's not locked, then it can be overwritten. That's completely fine … You set, I think this one's more right, then you do the simultaneous, and it corrects that … If the user wants a fit to stay authoritative and never change, then they lock it."

**IS12 — Error bars use the fast pass.** Decides that the refit's error bars keep the fast pass's method, and that no deep pass is done on these fits.
"For the error bars, yes we keep the fast pass, the deep pass is not working well so we won't worry about it right now."

**IS13 — The 30-minute figure is a target.** Decides that the timing figure never stops, fails or cuts a batch.
"We have the 30-minute target. Don't make it a limit on us, right? And take my exact words. This is a target, not a limit. It does not stop us at any point."

**IS14 — No automatic start; the user picks the anchor.** Decides that nothing starts by itself and ISAR never chooses an anchor. Amended 2026-11-01: the automatic toggle belongs to mode linking (IS28), not to batches; IS14 still holds for batches.
"I don't think there's any need for an automatic toggle" and "ISAR doesn't know what the cleanest spectrum is. The user picks an anchor"

**IS15 — No new windows.** Decides that the feature lives in the existing views.
"I don't want new windows or new big new platforms … use what we have right now"

**IS16 — A hidden mode is priced by integrating over the split.** Decides that a weak branch hidden exactly under a strong mode is priced by a numerical integral over the intensity split, not by the bell-curve approximation that fails along that flat direction. Dated 2026-10-30. Amended 2026-11-04: a batch no longer prices a hidden pair by the split integral, which stays in the repository and is not run by a batch. Both modes are kept, and their division is set by the neighbours' priors and the penalty's restarts (section 4).
"Ok, let's integrate over the split then."

**IS17 — The q-change penalty rises from zero.** Decides that no shift of a matched mode is free: the flat zone goes, the q-change penalty is Welsch, and the neighbour's uncertainty is propagated into its width as `s_eff^2 = s^2 + (C/2) sigma^2`. Restores IS5. Dated 2026-10-30.
"we need to not have a perfectly flat zone … there is always some penalty." And: "It was a bit sigmoidal shaped; so gentle curve inside, rising more at it, but levelling off so it doesn't go to infinity." Offered Welsch or the code's quartic family, the operator chose "Welsch". The propagation was proposed by the agent from the external review; the operator answered "Ok".

**IS18 — The q tolerance is a slope times the spacing.** Decides that a q-change tolerance scales with the distance to each neighbour: a tolerated slope with one setting per metric, meV per r.l.u. with no lattice loaded and meV per inverse angstrom with one, so a number never changes meaning when a lattice is loaded. The slopes are provisional. Dated 2026-10-30.
"Your recommendation is fine, we'll have to handle sample characteristics more thoroughly in the future so this is provisional anyway." And: "The spacings could vary a lot, e.g. sample with very different in-plane versus out-of-plane lattice constants … But I think this is fine, we'll see how it does with the data we have now." Asked whether the dialog should show the saved values of retired settings, the operator answered "Ignore it, don't worry about old experiments".

**IS19 — A branch that disappears pays a price.** Decides that a branch a neighbour predicts in the window and a candidate does not name pays the mirror of its entry price, `-L/2` nats with `L = birth_edge + B(E_exp)`, so losing a mode costs what gaining one costs. The price is charged once per spectrum per snapshot, frozen for a round, and applies to anchored and simultaneous batches alike. Accepted: one branch dying while another is born far away costs more than a branch that simply moves (about 14 nats against at most about 6); the mismatch statistic still flags it. Dated 2026-10-30.
"I would think that killing the mode is fine to include a penalty for." Size: "Keep the same, it should be mirrored, I don't see any reason moving between scans that birth and death would be more or less likely". Accepted: "Ok, accept". Scope: "Yes, it applies to all". Amended 2026-11-04: a batch's priced entry of a listed add or split does not charge this price (section 4, priced entry). Asked whether to charge it, the operator answered "Leave it out then". Charged, the price would let a predicted branch enter mid-window unless the counts argued against it by more than about 7 nats, so a mode with almost no intensity would still enter. The price stays in the repository with the search, which no batch runs.

**IS20 — Count, then labelling.** Decides that the count is chosen first, by its total probability over its distinct labellings, and the labelling is chosen within that count; a count change must beat the incoming count by `evidence_hysteresis_nats`. Dated 2026-10-30. Amended 2026-11-01: the total is taken over the few listed alternatives (section 4), not over every labelling. Amended 2026-11-04: no batch chooses a count or a labelling by evidence. The count is the fixed mode set's and the labelling is the link's (IS37, IS38). The count-first decision stays in the repository. Amended 2026-11-04 (priced entry): for the listed adds and splits only, a count change again has to beat the incumbent's evidence by `evidence_hysteresis_nats`; a listed change enters when its ln Z exceeds the incumbent's by more than that margin (section 4). No count is walked or searched, and no labelling is chosen by evidence (IS38).
"Count then labelling."

**IS21 — The search starts at the seeds and the evidence stops it.** Decides that the candidate search starts at the neighbours' predictions and the spectrum's own fit and moves outward only as far as the evidence pulls: no count range, no candidate cap and no count ceiling in this pass. The stopping margin, 5 nats, is an uncalibrated start, and `full_search` remains as a check. Dated 2026-10-30. Amended 2026-11-01: superseded for the free count walk by the predicted-changes list (IS31); the stopping margin and `full_search` are removed.
"I will absolutely reject the idea that it's only within one mode. There's a penalty cost for having a lot more modes already, so it shouldn't go from two to six without good reason to do so." And: "I don't even like having six modes here". On the intermediate counts: "We can try this and see how it works on the data".

**IS22 — A width at its floor no longer gets a candidate refused.** Decides that the coupled refit prices a mode whose width sits at the fitting floor as a resolution-limited mode, as the fast pass does, so its candidate is not refused; and that the two defects PR #70 deferred (a change of the resolution-limited choice alone, and a shared coupling term left out of a three-spectrum chain) are fixed in this job. Dated 2026-10-30.
"Yes, include it." And, for the two deferred defects: "Fix both then".

**IS23 — A QE line squeezed to its narrowest width is the elastic line.** Decides that a quasi-elastic line whose width reaches its lower bound is a degenerate twin of the elastic line, so the candidate becomes the model without a quasi-elastic line. It lives in the shared fitting engine and applies whenever the width rails, so fast-pass results change for any spectrum whose QE width rails. Dated 2026-10-30.
"If the elastic line and QE can trade off, there is something wrong, similar to a width below the resolution; we should be railing it, forcing it to a degenerate mode". On placement: "In the shared engine now".

**IS24 — Screen first, full price for contenders.** Decides that each count step and labelling move of the candidate
search is priced first as a screen; that a screen is promoted to a full price whenever a count or label choice could
otherwise change; and that seeds and failed screens are always full. The 5-nat safety allowance
(`SCREEN_ALLOWANCE_NATS`) is an uncalibrated algorithm setting in code, not a dialog setting. The plan kept it at 5 nats
because the ring-1 calibration met its criterion on two runs, the second after the count-total record fix (section 10); the operator may set another value from those
numbers. The same day's two speed-ups are the joint gradient (ruling A), a speed-up whose results agree on ordinary
and bound cases and differ on some real hidden-pair basins (section 10; accepted by IS25), and no barrier between spectra (ruling B),
which changes no results. Dated 2026-10-31. Amended 2026-11-01: superseded with the listed-candidate search, which has no screens and no promotions; `SCREEN_ALLOWANCE_NATS` is removed.
"I think this is reasonable. … I think it is right for us to try this now, see how well it works on the data that we
have, and tune as we need to as we encounter problems" (operator, 2026-10-31).

**IS25 — The joint gradient lands although some hidden-pair evidence moved.** Decides that ruling A's "same results"
is accepted as not holding exactly on nearly flat hidden-pair basins, and that the gradient path is the default
there too. On two real hidden-pair jobs the gradient comparison (section 10) found job evidence moving by up to 0.15
nat, single basins by up to about 1 nat, one split basin lost and one gained; the large differences sit in basins
with several near-zero curvature eigenvalues, where the Laplace evidence depends on where an optimiser stops in a
flat valley, so the old values are no reliable reference. Rejected: keeping the finite-difference arithmetic inside
the split integral, which would restore identical results there at part of the speed-up. Dated 2026-10-31.
Asked to land as is or keep the old arithmetic in the split integral, the operator answered "Land as is".

**IS26 — Every worker busy: inner work and lookahead on the one pool.** Decides that when a ring's search leaves
fewer candidates in flight than there are workers, the spare workers run work inside a candidate (its starts, and the
node sets of its split integral, larger sets computed at once instead of after the agreement test) and price the
search's next likely promotions ahead into the exact-job cache. D1: parallel split nodes start from the nearest node
solved when they are sent, so a flat-valley hidden pair's ln Z, and rarely a count decision near the stopping margin,
can differ slightly from the one-worker value; a node that fails is retried once from the continuation start, and
failing again the integral runs serially. D2, as built: requested work is always sent first, and lookahead goes only
to a free worker with nothing requested waiting; a lookahead task already running finishes, so a requested task can
wait for the shortest running one. The cost is wall time only, never correctness; the alternative is a cooperative
abort flag checked between optimiser steps, which still waits up to one optimiser run, and the way back is a
follow-up that adds it to lookahead tasks in `runner.py`, with no data or format change. Acceptance is correctness
tests and the routine checks, with no timing or comparison campaign: "No measuring." Dated 2026-10-31. Amended 2026-11-01 (S4): the lookahead is removed with the listed-candidate search (IS21 and IS24 amended); requested work still goes first, and a candidate's inner work still runs on the one pool.
"You get the workers all working, so that when the pool of scans decreases the workers are churning on it as they should." "Inside one candidate we run more." "No measuring." On D1: "D1 accept". On D2: "D2 yes".

**IS27 — A lone spectrum's mode is at most min(4 meV, ω0) wide, in the coupled refit too.** Decides that the width
cap of the shared prior recipe (`isar.mode-prior-recipe/7`) holds wherever a mode is fitted, so the coupled refit
fits, prices and takes covariance in the fast pass's measure: each width's prior proper on its own range
[g_lo, min(4, ω0)], fitted as a fraction of that range. A mode linked to a branch its neighbours carry may have the
cap lifted to the old 20 meV, its neighbours' width then its prior; only a settled link lifts it (the hook is built,
the linking that fills it is later work). Rejected: Γ < ω0 alone, which leaves a 10 meV mode free to be 10 meV wide.
Dated 2026-11-01.
"we'll need to limit overdamped modes in the fast passes; very large widths are being used to grab extra background
intensity, and aren't well supported on their own; they should come through as priors from neighboring scans." On
where: "Everywhere it runs." On the value: "Yes, we can take that limit; we can always tune it later if need be." On
the lifted ceiling of 20 meV: "Sure". Amended 2026-11-02 (S4U3): the linking that fills the hook is built, so a settled linked mode is lifted to the 20 meV ceiling (section 4).

**IS28 — Mode linking is a step of its own, run on request or by a toggle.** Decides that mode linking (section 2.2) is a third step beside the anchored and simultaneous batches, which stay. The Processing dock's **Link modes** button queues one short link item for the scans selected in the scan list. The switch *Link modes after each fast pass* is off by default and stored with the experiment; when it is on, the settling of a scan's last fast pass queues one series link item for that scan. Turning it on links nothing already present. Linking changes no fit, and no control edits a link. IS14 still holds for batches. Dated 2026-11-01.
"I view this is a basic "how many modes should we even be trying, and where can we draw links for the same mode across different zones to do the fitting right in the next iteration"" (what linking is for). "No need, this is a different thing entirely from the anchor->ring moving. It may survive as the second step; once we aren't choosing modes, that's cut a huge amount of the time, we can link directly and focus on the harder questions there rather than trying to check every candidate" and "So: don't rip out the anchors and rings, this is a third thing." (a third step). "No user modification or interaction right now, that comes later once the base is working ok." (no editing). "A button is good. Make a toggle to run it automatically, like the fast-pass, so users can run a fast pass and synchronize new data automatically." (button and toggle).
On 2026-11-01 the operator answered the intensity questions in these words: "A b. B Yes, we compare the intrinsic intensity of the phonon which removes external factors (correct for temperature, analyzer efficiency, etc.) C Ok, yes" (A answered the analyzer-efficiency question, B the temperature question, C the named removals). The intensity scale is `intensity_scale` in `isar/session/inter_spectra/linking.py`.

**IS29 — Linking settles the easy modes first.** Decides that mode linking takes the modes the neighbours agree on and leaves the hard ones for the fit, which reaches them by fitting everything well defined first and then using the neighbours. Its "what linking is for" quote is recorded in IS28. "We work by process of elimination: tackle the easy modes, that leaves the hard ones that are difficult to distinguish." Dated 2026-11-01.

**IS30 — Widths and intensities are coupled like energies.** Decides that a linked mode's width and intrinsic intensity are coupled to its neighbours as its energy is: soft, continuous, and not held to a match (section 4). "assume they stay fairly similar, prove otherwise." Dated 2026-11-01.

**IS31 — Only neighbour-predicted changes are listed.** Decides that the second pass adds a mode only where a neighbour predicts one, and that the free search for a leftover bump is not repeated (IS21 is superseded for the count walk). "I think A is right, we won't add new peaks until we're very sure about the easier ones. The bigger additions, adding things to explain what the other passes didn't catch, comes later." Dated 2026-11-01. Amended 2026-11-04: the listed changes are not searched. The link's listed adds and splits are applied once to each spectrum's mode set, and its drops never (IS37). Amended 2026-11-04 (priced entry): each listed add or split enters a spectrum's mode set only when its priced comparison enters it (section 4). The listed changes are still not searched, and drops never apply.

**IS32 — Three kinds of unexplained.** Decides that the link record keeps three kinds of unexplained: unexplained intensity, an unexplained change (a mode departing from its neighbours' prediction, or at the width cap), and an unlinked mode. Amended 2026-11-02: unexplained intensity is not computed, recorded or shown (IS35, IS36); the other two stay in the link record and are drawn on no view (PR #87). "Those are reasonable. Unexplained intensity is an undescribed phenomena, which could be another mode or could be something else." Dated 2026-11-01.

**IS33 — The shift-click path and its Dispersion view.** Decides that an analyzer path is picked on the Analyzer array dock and plotted in the Dispersion view (PR #86 and #87). "shift-clicking between two analyzers that are in the same row or column it selects those two and everything in between, if they're not on the same axis then it doesn't do anything." Dated 2026-11-01.

**IS34 — Build order over speed.** Decides that the work is built in the order that gives quality, not the order that gives results soonest. "I'm not worried about getting results soonest, that's not important; efficiency in coding is more, ensuring we're doing things in the right order for quality and not trying to rush things out." Dated 2026-11-01.

**IS35 — No unexplained-intensity marker.** Decides that no view marks unexplained intensity: the user sees where the fit does not match the intensity, by eye (amends IS32). "I don't think we need an unexplained intensity marker at all really; a user will see that the fit doesn't match the intensity somewhere and know that it's not covered by the model." Dated 2026-11-02.

**IS36 — The calculation goes too.** Decides that unexplained intensity is not computed, recorded or given a setting: no kind-1 code, no payload field and no setting. Asked whether the calculation goes too, the operator answered "drop it". Dated 2026-11-02.

**IS37 — Each spectrum's mode set is set once and frozen.** Decides that a batch fixes each spectrum's mode set the first time it fits the spectrum (its fit in use with the link's listed adds and splits applied, never its drops), and changes it nowhere else in the batch; no count or labelling is searched or compared. Dated 2026-11-04. Operator: "Agreed, frozen". Amended 2026-11-04 (priced entry): the listed adds and splits enter by the priced comparison of section 4, not unpriced; the freeze stands and the unpriced entry is replaced. The operator's rule: "In ISAR, we pay with evidence and nats, not vibes and hard rules." Asked sequential or every subset: "sequential".

**IS38 — The link's labelling is used everywhere.** Decides that each mode carries the link's best branch, that no labelling is compared by evidence anywhere, and that a near tie the link reports is flagged `connection_ambiguous`. Dated 2026-11-04. Operator: "Yes, everywhere".

**IS39 — Structural choices are frozen.** Decides that a batch keeps the fit in use's width mask (floor or free), its model line (quasi-elastic or elastic only) and its background form, and that an added or split-off mode starts with a free width and the link's predicted width as its prior. Dated 2026-11-04. Operator: "I think those are reasonable to freeze".

**IS40 — Two-sided energy priors in anchored batches.** Decides that an anchored spectrum's energy prior is built from its neighbours on both sides, each through the fit in use (its solo fit, an earlier batch's refit, or this batch's refit once its ring is done), and that the anchors only determine which spectra are held fixed. Supersedes the anchored half of IS6. Dated 2026-11-04. Operator: "I think we can use it both-sided here, using the solo fits outside (or whatever fit we have, we may be doing rounds of fitting remember); the anchored one just determines which are fixed."

**IS41 — Both batch kinds stay.** Decides that the anchored and the simultaneous batch both remain, and that a user sets the authoritative anchors. Dated 2026-11-04. Operator: "Both batch kinds, allowing a user to set authoritative anchors is helpful."

**IS42 — No groups in a report.** Decides that a mode close to another reports its own fitted intensity and the fit's error bars, with no group report and no "not separately determined" marker. Dated 2026-11-04. Operator: "we don't want to try reporting "groups"; we believe there are two there, our fit gives X and Y intensity of each, the operator has to look at it."

**IS43 — The tie-breaker penalty between close modes.** Decides that two modes the data cannot tell from one mode pay a soft penalty in the solo fast pass and in the coupled refit, of the form in `docs/STATS_PRIMER.md` §2.5, with the count comparison included (option (a)) and the provisional settings of 1 nat and d0 = 4. Dated 2026-11-04. Operator: "a tie-breaking item, or to force it out of a metastable state"; "small, and drop off quickly"; "useful in the fast solo fits and in the shared fits both"; "an analytical item, not based on vibes … This depends not just on relative closeness, but amplitude and linewidth of each as well." On the form: "1. Yes 2. Ok … 4. We can trial these 5. Ok, we'll see." and "3. (a) then".

**IS44 — The merged line and the closed form.** Decides that a close pair is judged against one peak that may take a different but still coherent linewidth. The code uses the closed form, whose known limit is recorded in `docs/STATS_PRIMER.md` §2.5, and the operator judges its acceptance in practice; the 25% band is not a gate. Dated 2026-11-04. Operator: "(B). Yes, multiple peaks together can produce not only one peak that has the same shape and changes intensity, but one peak with a different but still coherent linewidth than either alone (the Rayleigh criterion in action, basically). B is not necessarily *Gaussian* broadening (do two Lorentzians next to each other produce a Gaussian broadening?), but the principle remains the same." And: "(b) is fine. I'll judge acceptance in practice once i can see it in action."

**IS45 — No "not observed here" rule.** Decides that in this pass no mode is labelled "not observed here" and none is withheld from predictions, whatever its intensity: a mode at or near zero stays in the fit with its priors and feeds the link as today, superseding the operator's earlier "4. Agreed". Dated 2026-11-04. Operator: "It can't cause any issues at the next stage if we have scans on both sides; if the issue is in scan 2, scans 1 and 3 either show it or don't, if it's in 1 and not 2 OR 3, then there shouldn't be a strong prediction in 2 or 3, right?" Asked whether to drop the rule: "Yes".

**IS46 — The operator runs the measurements.** Decides that no agent starts a real-data or timing run for this pass: the operator runs batches on the 302 K experiment after landing and stops them as he sees fit. Dated 2026-11-04. Operator: "I'd rather run the measurements myself, so I can stop it"; "Measurements must be targeted, well-scoped with a clear purpose, and well-bounded."

**IS47 — One log line per spectrum.** Decides that each spectrum's refit writes one line as it finishes (its ring or round, mode set, restarts, seconds and flags), so a batch can be followed while it runs. Dated 2026-11-04. Operator, asked for one line per candidate showing what was tried, how long it took and whether it won: "Yes."

**Simultaneous batch: one go per click, and the stopped batch's link (no ruling ID; the rule is the "Simultaneous batch" paragraph of section 4).** Amended 2026-11-04: a simultaneous batch runs one round and one sweep per start, then its end link, and it does not settle. The operator: "Each click should be one 'go'", and "we do one iteration of the simultaneous fitting". On the settling test and the sweep cap: "Let's not 'report if the fits moved' because they likely will, we let the user see and investigate." On a stopped batch: "We can add the recovery as well." (its end link, section 4, link stage).

## 9. Out of scope and open

**Out of scope, by ruling.**

- Coupling across series taken at different q settings: "we don't need to go across those scans, I don't think, right now" and "an issue for another time" (operator, 2026-10-29).
- A fitted ω(q, T) surface: "we get Omega(q, T) and Omega(q', T) and Omega (q, T') but not Omega (q', T')" (operator, 2026-10-29).
- Any automatic start of a batch, and any toggle for one (IS14); new windows (IS15). Mode linking has its own automatic toggle (IS28).
- The campaign side (`isar drive`): its running dispersion prior and its branch identity are unchanged, and inter-spectra results do not reach a campaign.
- A full joint selection of the mode count along a track, and a deep pass on inter-spectra fits (IS12).

**Open, with who decides.**

- *Calibration.* Eighteen settings are uncalibrated starting values, and five are calibrated on synthetic tracks (section 5). Their calibration is not done. The operator's tuning list, after landing: the two tolerated slopes (`q_slope_meV_per_rlu`, `q_slope_meV_per_invA`) and the loss size (`birth_edge`, `birth_mid`) are starting values, provisional until tuned on data. The tie-breaker penalty's two settings are provisional and uncalibrated (`docs/STATS_PRIMER.md` §2.5).
- *Speed.* The 2026-10-31 timing (about 58 minutes against the 30-minute yardstick, IS13, section 10) was taken before the listed-candidate search, which removed the screens and the count walk. No timing of the built search is recorded here (IS26). The priced-entry batches have no timing yet: the operator measures them (IS46).
- *Branch identity through crossings.* An unresolved crossing stays ambiguous and is flagged. The identity lives in `isar/analyze/coupled/identity.py`; extending `isar/session/branch_tracking.py` (`track_path`) is not done.
- *Reports and exports, plan decision D6.* The fast-pass projection of `results.csv`, `modes.csv` and `report.json` is the built reading of the design's default; the operator is to confirm it.
- *Go time.* How long one simultaneous go takes on a real row is not measured (section 10); the operator measures it (IS46).
- *Priced entry on real data (2026-11-04).* Three things the operator's first runs will show, none measured here. How often a new mode's width rails to the floor and is left out (no usable twin): shown by the spectrum's log line (`no answer` for the change) and by the saved pricing record's tried rows with outcome `no_answer`, not by `width_at_limit`, since a left-out candidate is absent from the saved refit's flags. How often the hidden-pair rule leaves a split undecided: shown by the `connection_ambiguous` flags and the unresolved splits in the log. The size of single-start noise against the 0.1-nat margin: the same spectrum fitted from two starts, its ln Z difference set beside the margin.
- *Mode linking tolerances.* `link_slope_meV_per_rlu`, `link_slope_meV_per_invA`, `width_tolerance_ln`, `intensity_tolerance_ln`, `link_threshold` and `tie_margin` are uncalibrated (section 5). The slopes and the width tolerance were set on 2026-11-02 from the spread of linked neighbours in one run on a copy of the operator's 302 K experiment (section 10), one temperature only; temperature links keep the refit's temperature tolerance, which no linking run has tested. Tolerances that set themselves from each run's measured spread were discussed and not built.
- *Skip pairs.* A branch is linked across one missing spectrum only (the single-gap limit): the two graph links on either side of the middle spectrum must lie on one line through it. A gap of two or more spectra is not bridged.
- *Flags.* Deliberately last: "Let's leave the flags for the *end* when everything is tuned and working". The flag `branch_lost_mid_window` (look) is kept; the flag layout and any relabelling of severities are later work.
- *q-links and curvature on real data.* On the BL43LXU gpi triplets, measured in the hkl metric, the triplets lie off the straight line, so whether a curvature term forms depends on `collinearity_tol`. Its default is now 0.01, a provisional value, still uncalibrated and under the operator's review. The q proof (section 1) reports the curvature count when it is run.

**Known effects, not fixed.**

- The split integral covers one overlapping pair per fit (the search's; not run by a batch since 2026-11-04): a stack of three overlapping modes reads `undetermined`, not integrated.
- A matched branch whose intensity vanishes stays in the fit: a batch drops no mode, so it sits at zero or near-zero intensity with its priors and feeds the link as today (IS45). The loss price no longer acts in a batch (section 4); its old behaviour, a branch dropped on strong statistics and kept as a phantom on weak ones, is what `tests/analyze/test_coupled_loss.py` records for the search, which still runs under its tests.
- The closed-form separability fades early for broader pairs (section 4; `docs/STATS_PRIMER.md` §2.5). The penalty's numbers are provisional.
- A fast pass stored before the tie-breaker penalty reads as not current: the fast pass's implementation version is 4, and its settings enter the card's guidelines signature. Run the fast pass again before a batch; stored results are not rewritten.
- The scan inspector's Fast column keeps showing the stored fast pass, so it can disagree with the Scan(s) plot when a refit is authoritative.
- The scan list shows a Lock column; Lock itself is set in the Scan(s) view.
- Two scans of one q setting within the temperature rounding step at one temperature get no links to each other or across; a note names them. A series with repeats gets fewer T-links, never a wrong one.
- Grid adjacency has no diagonals (assumption A1).
- Stage 1 outcomes of a simultaneous go are saved inside its sweep's refit events, so a stop between stage 1 and the sweep loses them; the go's end link is then made on the fits in use before it. A stop in the middle of a ring loses that ring's unfinished refits; rings are saved whole.
- Skipping is within one batch only (no cross-batch skip): a rerun of an interrupted batch repeats its work.
- The fast pass's own six-mode peak-count ceiling is not changed by this work (noted, not built). On the finish-line copy, 10 of its 28 fast passes reached it.
- After the degenerate-QE rule (IS23), a BL43LXU fast pass stored by older code is stale (the recipe version `isar.central-qe-recipe/2`): the analysis queue refits it when asked, and a batch refuses a member without a current fast pass until then. Stored results are not rewritten; an experiment changes only when its fast pass is re-run.
- After the fast-pass width cap (IS27, recipe `isar.mode-prior-recipe/7`), every fast pass stored by older code is stale: the analysis queue refits it when asked, and a batch refuses a member without a current fast pass until it is re-run. Stored results are not rewritten. Mode linking leaves such a spectrum out with the reason "fitted with an older fitting recipe; run its fast pass again".

## 10. Measured so far

The real-data proof has not been run (Partial, section 1). The finish-line runs below were run on purpose on a copy of the operator's experiment, never on his own folder. They are the only real-data timing so far. Each dated line gives counts, timings and booleans, never a data value (AGENTS.md hard rule 10).

- **2026-10-31, the fast pass re-run on the copy (change 8).** All 28 analyzers were re-fitted through the analysis queue's own service, and none was refused or failed to save. Before the re-run none had a current fast pass; after it, all 28 do. The stored mode model changed for 16 of 28 analyzers and the preferred mode count for 9 of 28. 10 of 28 reached the fast pass's own six-peak ceiling, which this work does not change.
- **2026-10-31, the design's items 1 and 2 (ring 1 of an anchored batch, replayed on the copy from the fast fits, three pool workers, 1,098 s).** Item 1 holds: the count equals the spectrum's own fast count and the anchor's count; the labelling is the order-preserving one; no identity flag; the refit carried no serious flag. Item 2 holds: no candidate was refused for a railed width; every attempted candidate carries a price or a stated reason; the own count has finite prices; every final mode is matched to an anchor branch within the tolerance in force, with no unused branch left within tolerance. The inline replay (one worker) exceeded its 20-minute harness guard and was killed; its criteria are the three-worker run's.
- **2026-10-31, item 3 (the full anchored batch, 28 spectra, 8 rings, from the anchor, five pool workers): stopped by the operator at about three hours; not finished.** Twelve of 28 spectra were saved (rings 1 to 3); ring 4 was unfinished and 16 spectra were not reached. Ring wall times were about 14, 49 and 67 minutes, and ring 4 ran past 45 minutes. The 30-minute target was missed by a wide margin. That is a finding (IS13), and the operator has ruled speed work a follow-up.
- **Timing of the saved spectra (the batch's own refit payloads; the end record was never written).** Fresh prices by ring: 143, 363 and 513. Optimizer fits: 2,200, 5,572 and 7,187. Failed prices: 4, 12 and 11, with the reasons mostly "Hessian not positive definite". Evidence seconds (CPU across workers): about 2,710, 12,304 and 17,618. Refinement took under three seconds in total, so evidence was essentially all of the compute. The largest single spectrum was in ring 3. Pool utilisation, the ship time and the branches-seen figure were not recorded for the partial batch.
- **Stop reasons, over the 12 saved spectra.** Downward walks: 11 stopped behind the margin, 1 had no parent. Upward walks: 9 stopped behind the margin, 1 had no parent, 2 were unresolved at the ceiling of the walk. Five of the 12 ended with a count other than their fast fit's count (ring 1: 2 of 3; ring 2: 1 of 4; ring 3: 2 of 5).
- **Flags by severity, over the 12 saved refits.** Serious: `identity_ambiguous` on 4 spectra (rings 2 and 3). Look: `count_changed_by_neighbours` 3, `linewidth_change` 7, `unmatched_branch_mid_window` 1. Repaired: `neighbour_supported_split` 2. Eight of the 12 were flagged. The ring-1 spectra carried no serious flag.
- **2026-10-31, the gradient comparison (`tools/inter_gradient_compare.py`, a scratch copy, 297 s).** Seven cases priced on
  both paths at full depth: six real (two ordinary, two on a bound, two hidden pairs, all from one analyzer's spectra) and
  one synthetic flat-width hidden pair. All four classes were present. Role: the plan's agreement target of 0.02 nat per
  basin, the plan's reading of the design's "far below the 0.1-nat hysteresis", not an operator number. **Not met.** The
  ordinary, bound and synthetic cases agree; the largest basin difference is about 0.016 nat. Two real hidden pairs do
  not: a basin differs by about 0.35 nat in `a03:6ae1930e`, and by about 0.95 nat in `a03:d8da39b7`, where one split basin
  the finite-difference path priced comes back undetermined on the gradient path (Hessian not positive definite). The
  coordinator's reading: the large differences are near-singular basins, where the two optimisers stop at different
  points of a flat valley, and the finite-difference path's split curvature carries a small bias that the gradient
  reduces. That was one investigation, and no code was changed. Speed on the same cases: about 1.2 to 1.4 times faster
  on the real cases (the two real hidden pairs 36 s to 28 s and 151 s to 118 s), and about 2 times on the synthetic pair.
- **2026-10-31, the gradient comparison rerun after the review repairs (the same seven cases, a scratch copy, about 4.5
  minutes).** Role: the same 0.02-nat reading. **Not met, as before.** Ordinary, bound and synthetic cases agree (the
  largest basin difference about 0.016 nat). The two real hidden pairs miss. In `a03:d8da39b7` a split basin the
  finite-difference path priced is undetermined on the gradient path, a flat-valley basin differs by about 0.95 nat (a
  correction: the gradient path reaches a slightly lower objective at a positive-definite mode), and the job's ln Z by
  about 0.15 nat. In `a03:6ae1930e` two split basins differ by about 0.04 and 0.11 nat with no better optimum (misses),
  and a flat-valley basin differs by about 0.35 nat (a correction on an objective gain of about 2e-6, a borderline
  call). Pairing is now by the search's basin identity, so one basin the gradient path leaves undetermined reads as
  absent in its reason text; its verdict is unchanged. Speed is as above. The operator accepted the miss and kept the
  gradient path as the default (IS25).
- **2026-10-31, the screen calibration (`tools/inter_screen_calibration.py`, ring 1, shadow mode, a scratch copy, 44
  minutes).** Six ring-1 spectra. Role: the plan's allowance criterion, the plan's reading of "with room to spare": every
  observed underrating (full price minus screen; positive is the dangerous sign) of a split basin and of each count's
  total at most half the 5-nat allowance, and no shadow decision diverging. **Met.** Split basins (417): median about
  minus 0.5 nat, 90th percentile about 0.06 nat, largest about 0.4 nat. Count totals (24): largest about 0.05 nat. Whole
  candidates (241): largest about 0.5 nat. Shadow decisions that diverge: none. Decisions the screens could not settle
  alone, so that they would promote: 54. The allowance stays at 5 nats for this ring's frozen priors. This is a
  diagnostic of the screens, for that ring only.
- **2026-10-31, the screen calibration rerun after the count-total record fix (`tools/inter_screen_calibration.py`, ring 1,
  shadow mode, a scratch copy, about 38 minutes).** Six ring-1 spectra, no errors. Role: as above. **Met, as before.**
  The numbers match the earlier run to the printed precision: split basins (417) median about minus 0.5 nat, 90th
  percentile about 0.06 nat, largest about 0.4 nat; count totals (24) largest about 0.05 nat; whole candidates (241)
  largest about 0.5 nat; no divergence; 54 decisions would promote. The fix changes how the count totals are read, not
  these values: on this ring the shadow view and the candidate rows agree.
- **2026-10-31, the timing (`tools/inter_timing.py`, the full 28-spectrum anchored batch, two anchors, spread in q, six
  rings, 16 workers, headless, unprofiled, a scratch copy).** Role: IS13's 30-minute figure, a yardstick and never a gate.
  **Missed.** The batch completed in about 58 minutes (3,501 s), about twice the yardstick. Rings, in order: about 12, 8,
  5, 10, 12 and 11 minutes. Pricing is essentially all of the compute: about 9.3 CPU hours over 2,643 priced candidates,
  with search, refinement and saving taking seconds. Of the priced jobs, 2,346 were screens and 238 were promotions to
  full prices (from the 26 saved refit records; the two anchors are not refitted). 167 fits failed, mostly on a Hessian
  that is not positive definite. Pool utilisation was 0.60, and one ring-1 spectrum's search alone took about 6,600 CPU
  seconds over 246 candidates.
- **Where the time goes, and the next step then proposed (history).** Superseded 2026-11-01: the listed-candidate search (section 4) is the built cut of the count walk and the screens; its timing is not recorded here (IS26). The time is in the number of candidates each search
  prices: most priced jobs are screens of count steps and labelling moves, not the split integral that the screens cut
  (the coordinator's reading of the saved records). The options the design names that cut that number are the ones with
  room: a cheaper screen for labelling moves (re-scoring a parent's fit), or a lighter anchored search. The coordinator
  estimates the column differences at about 45 per cent of a hidden-pair job, so analytic line-shape derivatives would cut
  at most that share, and cannot reach the yardstick on their own. The operator decides which, if any, to build.
- **2026-11-02, mode linking on a copy of the operator's 302 K experiment (28 spectra, 45 q-links, one temperature,
  under a tenth of a second).** At the first defaults (the refit's q-slope as the energy tolerance, width tolerance 0.7,
  markers at one sigma, cheapest pairs joined first, every refused candidate a contradiction): 9 branches, 48 of 139
  modes settled, 44 near ties, 34 contradictions, change markers 8 energy, 2 width, 16 intensity. The energy and width
  tolerances were then about three times the spread of linked neighbours. With linking's own slopes, width tolerance
  0.3, markers at two sigma, each link matched as a whole and only best-match refusals reported: 11 branches, 93 of 139
  settled, 6 near ties, 17 contradictions, change markers 11 energy, 2 width, 2 intensity. Listed changes rose (adds 22
  to 28, splits 17 to 31): with the tighter energy tolerance more branches are predicted near a single mode. The
  unsettled modes are mostly two groups where two modes sit about a resolution width apart.
- **2026-11-02, the removals (S4U2).** The three measurement tools (`tools/inter_screen_calibration.py`, `tools/inter_ring1.py`, `tools/inter_gradient_compare.py`) were removed with the screens and the count walk. The measurements above were taken with them and stay here as history.
