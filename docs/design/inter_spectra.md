# Inter-spectra fitting: the second pass that couples neighbouring spectra

> **Status:** live
> **Authority:** canonical design for inter-spectra fitting (the second pass that couples neighbouring spectra)
> **Verified against code revision:** `d859bd52` (2026-10-31)

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
| Coupled refit of one spectrum: stage 1 (the candidate search and the count-first decision, below), continuous stage, one shared objective, release fit | Implemented | `isar/analyze/coupled/refit.py`, `engine.py`, `rules.py` | `tests/analyze/test_coupled_engine.py`, `test_coupled_refit.py`, `test_coupled_rules.py` |
| q-change penalty: Welsch from zero, tolerance = tolerated slope x link spacing, neighbour uncertainty propagated (`s_eff^2 = s^2 + (C/2) sigma^2`) | Implemented | `isar/analyze/coupled/engine.py`, `refit.py`, `settings.py` | `tests/analyze/test_coupled_engine.py`, `test_coupled_refit.py` |
| A mode hidden under another: guard against twin merges, evidence by integrating over the intensity split, undetermined status | Implemented on synthetic spectra, not established on real data | `isar/analyze/coupled/engine.py`, `refit.py` | `tests/analyze/test_coupled_hidden.py` |
| Candidate search: seeds (each neighbour's own prediction and the spectrum's incoming fit), counts stepped both ways while within the stopping margin, labellings moved within a count, one ledger of distinct fitted basins, and a search record of every candidate and why each direction stopped. No count ceiling and no candidate cap in this pass | Partial: built and tested on synthetic cases; the stopping margin (5 nats) is an uncalibrated start, and the first real-data run is in section 10 | `isar/analyze/coupled/search.py`, `refit.py`, `types.py` | `tests/analyze/test_coupled_search.py`, `tests/analyze/test_coupled_search_real.py` |
| Screen first, full price for contenders: each count step and labelling move is priced first as a screen (every split integral cut to its first node set); screened candidates are promoted to full prices until no optimistic remainder can change the decision. Seeds and failed screens are always full | Partial: built and tested; the 5-nat allowance is uncalibrated and was checked on one ring-1 shadow run (section 10) | `isar/analyze/coupled/search.py`, `engine.py` (`integrate_split`, `screen`), `refit.py` (`price`) | `tests/analyze/test_coupled_search_screens.py`, `tests/analyze/test_coupled_screen_price.py` |
| Joint objective and gradient in the coupled pricing (ruling A): analytic derivatives of the Poisson term, heights, background, priors and split coordinates; column differences for mode energies and widths; curvature from differenced gradients; one-pass split polish. The solo fast pass keeps its finite differences | Partial: built and tested; it agrees with the finite-difference path on the real ordinary and bound cases, and two real hidden pairs miss the plan's 0.02-nat reading (section 10) | `isar/analyze/coupled/engine.py`, `refit.py`, `isar/analyze/prior.py` (`hessian_from_grad`), `isar/analyze/fast_pass.py` (the hook that returns none) | `tests/analyze/test_coupled_gradient.py` |
| Count-first decision: a count's total probability over its distinct labellings, a count change held by `evidence_hysteresis_nats`, label confidence judged within the chosen count | Implemented | `isar/analyze/coupled/rules.py` (`decide`), `search.py` | `tests/analyze/test_coupled_rules.py`, `tests/analyze/test_coupled_search_real.py` |
| Loss price for a branch that disappears: a branch a neighbour predicts in the window and the candidate does not name pays the mirror of the entry price, once per spectrum per snapshot | Partial: its size reuses `birth_edge` and `birth_mid`, uncalibrated | `isar/analyze/coupled/refit.py` (`_Spec.loss_terms`), `engine.py` | `tests/analyze/test_coupled_loss.py` |
| Width at the floor and the quasi-elastic line: a railed width is a resolution-limited alternative that is priced; a QE line railed at its narrowest width is the elastic line (shared engine); the model line is chosen by evidence beyond the hysteresis margin | Implemented on synthetic spectra | `isar/analyze/coupled/refit.py`, `engine.py`, `rules.py` | `tests/analyze/test_coupled_width_floor.py`, `tests/analyze/test_coupled_search_real.py` |
| Counts above six: pricing, refinement, the release fit and storage accept a count beyond six; `full_search` prices a stated finite domain as a check | Implemented on synthetic spectra | `isar/analyze/coupled/search.py`, `refit.py`, `isar/session/inter_spectra/runner.py` | `tests/analyze/test_coupled_search_real.py`, `tests/analyze/test_coupled_search.py` |
| Search, price and resume across the refit pool: the runner feeds each spectrum's search as its own jobs return, caches prices within one batch, sizes the pool to the item's cores, and reports timing in the batch's `end` record | Implemented; the timing is measured in section 10 | `isar/session/inter_spectra/runner.py`, `tools/inter_timing.py` | `tests/session/test_inter_spectra_runner.py`, `tests/test_inter_timing_tool.py`, `tests/session/test_inter_service_e2e.py` |
| Branch identity: persistent ids, loops, returns, cumulative T drift | Implemented | `isar/analyze/coupled/identity.py` | `tests/analyze/test_coupled_identity.py` |
| Anchored batch: rings fixed by distance, seams, not reached | Implemented | `isar/session/inter_spectra/rings.py`, `runner.py` | `tests/session/inter_spectra/test_rings.py`, `tests/session/test_inter_spectra_runner.py` |
| Simultaneous batch: rounds, sweeps, quiet-sweep settling, sweep cap (did not settle) | Implemented | `isar/session/inter_spectra/runner.py`, `schedule.py` | `tests/session/test_inter_spectra_runner.py` |
| Batch as one queue item (pass kind `inter`), run by the analysis service | Implemented | `isar/session/analysis_service.py`, `analysis_queue.py`, `isar_gui/queue_controller.py` | `tests/session/test_analysis_service.py`, `test_analysis_queue.py`, `tests/gui/test_queue_panel_inter.py` |
| Machine store: index, payloads, one writer under the workspace lock | Implemented | `isar/session/inter_spectra/records.py`, `isar/session/writer.py` (`save_inter`) | `tests/session/test_inter_records.py`, `test_inter_records_gpi.py` |
| Authority: the fit in use, picks, locks, acceptance, staleness | Implemented | `isar/session/inter_spectra/authority.py` | `tests/session/test_inter_authority.py` |
| User values (anchors, picks, locks, settings) with checked writers | Implemented | `isar/intake/inter_spectra.py`, `isar/intake/inter_values.py` | `tests/intake/test_inter_user.py` |
| Flags: score, severity, reasons, mode status | Implemented | `isar/analyze/coupled/refit.py`, `types.py` | `tests/analyze/test_coupled_refit.py` |
| Error bars, labelled conditional on the neighbours and the chosen model | Implemented | `isar/analyze/coupled/refit.py`, `isar_gui/inter_words.py` | `tests/analyze/test_coupled_refit.py`, `tests/gui/test_inter_words.py` |
| Settings: one table, checked reader, dialog | Implemented | `isar/analyze/coupled/settings.py`, `isar_gui/views/inter_settings_dialog.py` | `tests/gui/test_inter_settings_dialog.py`, `tests/intake/test_inter_user.py` |
| Calibration of the settings | Partial: five rows calibrated on synthetic tracks; the other rows uncalibrated (section 5) | `isar/analyze/coupled/settings.py` | — |
| Amplitude coupling (a prior on mode amplitude) | Planned: not coupled. Amplitude stays free, there is no amplitude setting, and the amplitude-prior test was not run in planning | — | — |
| Read facade, Qt-free and read-only | Implemented | `isar/workspace_api/inter_spectra.py` | `tests/workspace_api/test_inter_facade.py` |
| Scan list: Anchor, Flag, Lock and Inter-spectra fit columns; Set as anchor, Remove anchor | Implemented; tested offscreen, no person walk-through yet | `isar_gui/views/scan_list.py`, `isar_gui/viewmodels/scan_list.py` | `tests/gui/test_inter_scan_list.py` |
| Processing dock: anchors list, direction, preview line, the two Start buttons | Implemented; tested offscreen, no person walk-through yet | `isar_gui/views/inter_batch_box.py`, `isar_gui/viewmodels/inter_batch.py` | `tests/gui/test_inter_batch_box.py` |
| Scan(s) view: Inter-spectra fitting menu, Make authoritative, Lock, reasons, error-bar label, waterfall | Implemented; tested offscreen, no person walk-through yet | `isar_gui/views/inter_fit_bar.py`, `isar_gui/viewmodels/scans_page.py`, `inter_fits.py` | `tests/gui/test_inter_fit_bar.py`, `test_inter_fits.py`, `test_scans_page_inter.py` |
| Config > Inter-spectra fitting settings | Implemented | `isar_gui/main_window.py`, `isar_gui/views/inter_settings_dialog.py` | `tests/gui/test_inter_settings_dialog.py` |
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
  spectrum is refitted against its neighbours until the fits settle. **Sweep**: one pass of refits over a
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
  and the spectrum is flagged.
- **Screen**: a candidate's price with every split integral cut to its first node set. It is an evidence in the
  same units as a full price, but a screened basin's term is only bounded (section 4), so a screen never stands
  as a full price.
- **Promotion**: asking a screened candidate for its full price. The full price replaces the screen in the
  search's ledger; it is never added to it.

## 3. Invariants

Each line names the failure it prevents.

- **The fast solo fit is never changed by this pass.** It is the stored Stage II result that `results.csv`,
  `modes.csv` and campaigns read. `save_inter` never touches `session.json`, `results.csv` or a fast solo fit.
- **ISAR never writes an anchor, a pick, a lock or a setting on its own** (AGENTS.md hard rule 1). They are
  entries of `intake/inter_spectra.jsonl`, written only by the GUI in process through `isar/intake/inter_values.py`.
- **The machine store is append-only, has one writer and deletes nothing.** `save_inter` appends under the
  workspace write lock and writes the index last, so a payload that no index line names is never read.
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
- **Pricing is exact.** A cached price is reused only when every value it depends on is unchanged; the runner's cache keys hash those values, including each job's starts and the batch's merged branch names. The cache lives within one batch. Reusing prices across a changed snapshot is a measured option for
  the operator and is not applied.
- **A screen never stands in for a full price.** A screen's job key carries a `|screen` suffix, so the cache
  never answers a full-price request with a screen, and a full price replaces its screen in the ledger instead of
  adding to it. A screened basin's term is only bounded, at its screen plus the allowance, never read as exact;
  without this a screened term could be counted twice or trusted as exact.
- **Error bars are conditional.** A refit's error bars use the fast pass's covariance method at the refit's
  own solution. They are labelled "Error bars are conditional on the neighbours and the chosen model.", and
  later sweeps never treat them as independent precision.
- **The layers stay Qt-free**: `isar/analyze/coupled`, `isar/session/inter_spectra`, `isar/intake/inter_spectra.py`
  and `isar/workspace_api/inter_spectra.py` import no Qt (hard rule 6). The GUI computes nothing: it shows what
  the facade reads and sends the user's actions down (hard rule 7). `tests/test_import_contract.py` lists the
  names the GUI may import from this work.
- **The fit chosen in the Scan(s) view is session-local and is not saved**, so a stored choice is never a
  second authority on which fit ISAR uses.

## 4. How a batch fits

**Anchored batch.** The user sets anchors, then starts an anchored batch with one direction: spread in q
(every analyzer of each anchor's scan), spread in T (each anchor's analyzer at every temperature of its
series), or spread in q and T. Rings are the distance in neighbour steps along the links that direction
permits. Ring 1 is refitted first, each spectrum against the anchors and the neighbours already fitted in
this batch. Ring k starts only when ring k−1 is complete. A seam is measured when its ring completes, and
flagged `seam_disagreement` when the two sides disagree. A locked spectrum in scope counts as fitted from the
start. The anchors a batch used are recorded with it, so later edits to the anchors never change a past batch. Each spectrum's stage 1 in a ring is the candidate search described under the simultaneous batch below.

**Simultaneous batch.** Every selected spectrum pays the penalties on its links and curvature terms in one
shared objective: its own Baker–Cousins cost plus each term it appears in. The objective is solved in rounds.
Each round has two stages. Stage 1 searches mode counts and branch assignments against one frozen snapshot of the neighbours (the search is described under mode count and entry): it starts at the seeds, steps the count and the labelling outward while the evidence stays within the stopping margin, and decides count first, then labelling (IS20, IS21). Two slot orders of one fit count their evidence once, under the order of its fitted energies. Stage 2 refines the continuous parameters, with counts and assignments held fixed, in sweeps of
per-spectrum refits over conflict-free parallel groups; each refit includes every term the spectrum is in.
A term shared by several spectra has one definition, held by every member's refit once the neighbour on the
other end carries its branch; a branch that neighbour gains in this very stage enters from the next sweep, and
until then the refit is flagged `incomplete_comparison`.
Counts and assignments are reopened in later rounds. Iterated refits are not guaranteed to reach the same
answer from every start. A batch settles when counts and assignments stop changing and sweeps are quiet: no
mode energy moves by more than `sweep_tol_meV`, two quiet sweeps in a row, and the total cost no longer improves
by `cost_rel_tol`. A batch that reaches `sweep_cap` first keeps its results and is flagged `did_not_settle`.

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

**Mode count and entry.** The count of modes is chosen by total probability: each count's distinct labellings add up, and a count change must beat the incoming count by `evidence_hysteresis_nats` (IS20). The search starts at the seeds and stops where the evidence does: a count is stepped to only while it stays within `stop_margin_nats` of the best found, and it is judged behind only after the first wave of its best labelling's moves is priced (IS21). There is no count ceiling and no candidate cap in this pass. A better count reachable only through counts that are each more than the margin behind is not found, the accepted risk of section 9; `full_search` prices a stated finite domain as a check. A neighbour's prediction narrows the prior on each mode's energy, so a second mode that the neighbours show clearly can win in an ambiguous spectrum; the count is not forced to be equal along a track (IS8). A mode that no neighbour matches pays the entry price
`birth_edge`, and `birth_mid` more when it appears in the middle of the energy window, not at its edges (IS9). A
mode the fast pass fitted as one, which the neighbours resolve into two, is reported as a neighbour-supported
split (severity repaired), not as a new branch. A branch that a neighbour predicts in the window and that a candidate does not name pays the
mirror of the entry price: `-L/2` nats in that candidate's evidence, `L = birth_edge + B(E_exp)` from the same entry price
(`birth_mid` only away from a physical window edge, `birth_edge` alone at one), so a branch that disappears costs what one
that appears costs. Every branch any neighbour carries in the frozen ring or round snapshot is eligible, under its merged
name, with or without a coupling term; a branch a neighbour only remembers, or predicts only outside the window, pays nothing.
`E_exp` is the mean over the neighbours of their own predictions inside the window, one vote per neighbour. The price is
charged once per spectrum however many neighbours predict the branch, sits inside every basin term outside the normalised
densities (so every slot order of a composition carries it equally), is frozen with the snapshot (it is not part of the refinement
objective), and is recorded with each candidate (`loss`, `loss_nats`). The flag `branch_lost_mid_window` is kept (section 9).

**Screens and promotions.** Each count step and labelling move is first priced as a screen: the ordinary price
with every split integral cut to its first node set, everything else as in a full price. Seeds are always priced in
full. A failed screen is promoted to a full price at once, unless its status is `degenerate_twin` or `no_structure`,
which stand as they are (no price and no reason). Each decision keeps two totals: the evidence of what is fully
priced, and an optimistic remainder in which each screened basin sits at its screen plus `SCREEN_ALLOWANCE_NATS`
(5 nats). A count or labelling choice is taken only when the remainder cannot change it: the decision is taken again
twice per level, with that level's screened rows at their lowest and the rest at their highest, then the reverse, and
every pass must give the same winner, mask, variant, confidence and close calls, with the selected basin determined
and the probabilities within `PROB_TOL` (0.01) across those extremes. Otherwise the screened candidates it reads are
promoted, the largest optimistic contribution first, `PROMOTE_WAVE` (6) a pass. The search record (version 2) carries
the screen summary: mode, allowance, promote wave, screened and promoted counts, and the remainder. The environment
variable `ISAR_INTER_SCREENS` sets the mode: `on` (the default) in production, and `shadow` for the calibration tool,
which prices everything in full and records each comparison the screens would have judged differently. It is a
developer switch, not a setting.

**The q-change penalty.** In an anchored batch a matched mode pays `C(1 - exp(-z^2/2))` for its energy change from the neighbour branch it continues, where `z` is the shift over the tolerance (IS17). The curve rises from zero, so every nonzero shift pays something: small shifts very little, the steepest rise at the tolerance, and a levelling off at `penalty_cap`, so a large shift never costs without bound. The tolerance is the tolerated slope of the scan's metric times the q distance to that neighbour (IS18): `q_slope_meV_per_rlu` when the scan has no lattice loaded, `q_slope_meV_per_invA` when it has one, and each batch records which slope it used. The width is `s_eff^2 = tolerance^2 + (C/2) sigma^2`, where `sigma` is the held neighbour's energy uncertainty. In an anchored batch the spectrum being refitted adds none of its own; a simultaneous batch adds both ends'. Near its centre the Welsch curve acts like a bell curve about 2.5 times narrower than its width setting, which is why the factor is `C/2`. The same width rule applies to the temperature and curvature penalties, whose `shape_family` (welsch or quartic) still governs only those two; the q-change penalty is always Welsch. A shift of exactly zero costs exactly zero.

**A mode hidden under another.** A weak branch the neighbours support, sitting under a strong mode in this
spectrum, is priced by integrating over how the intensity divides between the two (one pair per fit, the
height prior as the fit has it, no amplitude coupling). The evidence then says the neighbours support a second
branch here and that this spectrum cannot size it, and the weaker mode reads held by neighbours (IS16). Distinct
branches the neighbours support are not merged away by the twin screens merely because their peaks are close;
repeated starts reaching one solution are still merged. A fit whose parameters stay undetermined has no
evidence and says so (`undetermined`), never a made-up finite number.

**A width at the floor.** A mode whose width reaches the fitting floor is resolution limited (narrower than the
instrument resolves). It is priced as its own model, the delta alternative, with its own weight; a free width
pegged at the floor is never priced as a model of its own, and an unpriced candidate's reason is the terminal
failure of its twin chain, not the head. The width mask is chosen after the count and the labelling, on the
chosen labelling's evidence: another mask replaces the incoming one only when it wins by more than
`evidence_hysteresis_nats`, or at once when the incoming mask has no priced row. The model line (quasi-elastic, elastic or background) follows the same rule: a different line replaces the incoming one only by more than `evidence_hysteresis_nats`, or at once when the incoming one has no priced row. Such a change (same count, same
labels) is a discrete change: the continuous stage accepts it by the evidence rule, it is no relabelling for the
identity, and the saved fit keeps the mask through later sweeps. The coupled refit inherits the fast pass's twin
screens, among them the rule that a central quasi-elastic line whose width reaches its lower bound is the
elastic line, so its candidate is the model without a quasi-elastic line (`docs/STATS_PRIMER.md` section 2).

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
  `release_fit_failed`;
- repaired: `neighbour_supported_split`, `neighbour_precision_unavailable`, `errors_unavailable`.

`t_drift_cumulative` reports the total change of one branch across the temperature range, since a drift can
pass unflagged in single steps. `linewidth_change` and `overdamped_entry` report damping changes without moving
the energy; widths are not coupled. A `seam_disagreement` flag from a measured seam gives the mismatch and the threshold it passed; one from an identity
seam gives the identity issue.

**Pricing arithmetic.** `refit.price` evaluates a labelling on a joint objective and gradient (`gradients`, on by
default; `False` keeps the finite differences as before). The analytic parts are the Poisson term, the heights and
background, the priors (coupling potentials included) and the split coordinates. Each mode's energy and width, and the
quasi-elastic width, take a finite difference of only that mode's complete height-normalised, resolution-convolved
column, at a step of `COLUMN_FD_REL` (1e-5) times the larger of the value and the width. Curvature is the full observed
Hessian, the central difference of the gradient, not a Gauss-Newton form. The split integral runs each node's
optimiser as one pass on the joint gradient, with a retry from its result only when that pass reports failure or ends
on a non-finite value; the node-set ladder and its 0.1-nat agreement rule are unchanged. Fully analytic line-shape
derivatives are not built. The solo fast pass keeps its finite differences: its objective-and-gradient hook returns
nothing, so it calls the optimiser as before.

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

**Pool.** Stage 1 of a ring or a round runs on one refit pool, and each spectrum's search waits only for its own
jobs. When the last job of a spectrum's current pass returns, its search is fed that pass's prices in its own job
order, and its next pass goes into the pool at once while other spectra's jobs still run. A promotion is decided from
that spectrum's own state, never from whichever job returns first, so the same inputs give the same candidates in any
completion order. Rings stay sequential, because a ring conditions on the saved fits of the rings before it; identity
reconciliation, continuous refits and saving are unchanged. Prices are cached within one batch, under keys that hash
every value a price depends on.

**Time.** The 30-minute figure is a target, not a limit (IS13). Nothing stops, fails or is cut because of it; going well over it is a reason to optimise. The batch's timing report sits in the `timing` block of its `end` record, printed by `tools/inter_timing.py`: per spectrum and ring, the candidates priced, cache hits, starts per candidate, optimizer fits, failures by reason, evidence seconds against refinement seconds and why the search stopped; per batch, the pool's utilisation and the slowest spectrum and ring. Section 10 says what has been measured.

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
| `sweep_tol_meV` | advanced | 0.01 meV | > 0 to 1 | uncalibrated | A sweep that moves no mode energy by more than this is quiet; two quiet sweeps settle a batch. |
| `sweep_cap` | advanced | 20 sweeps | 2 to 1000 | uncalibrated | A simultaneous batch that has not settled after this many sweeps is kept and flagged as did not settle. |
| `q_curvature_scale_meV` | not in the dialog | 1 meV | > 0 to 50 | uncalibrated | Simultaneous q links: the energy scale of the curvature residual along a line of three spectra. |
| `reference_T_K` | not in the dialog | 100 K | > 0 to 1000 | calibrated on synthetic tracks, not on real data | The temperature step, in kelvin, at which the temperature tolerance applies unchanged. |
| `t_spacing_form` | not in the dialog | widen | one of widen, divide | uncalibrated | How the temperature step enters the penalty: widen (the default) or divide (the literal form, for comparison). |
| `penalty_cap` | not in the dialog | 12 deviance | > 0 to 1000 | calibrated on synthetic tracks, not on real data | The largest price one penalty term can add to a spectrum's fit. |
| `birth_edge` | not in the dialog | 2 deviance | 0 to 1000 | uncalibrated | The entry price of a branch that no neighbour predicts, at any energy in the window. |
| `birth_mid` | not in the dialog | 12 deviance | 0 to 1000 | uncalibrated | The extra entry price of such a branch when it appears in the middle of the window. |
| `cost_rel_tol` | not in the dialog | 1e-05 | > 0 to 1 | uncalibrated | A sweep whose total cost improves by less than this relative amount is not improving. |
| `collinearity_tol` | not in the dialog | 0.01 | 0 to 1 | uncalibrated | How far, relative to its length, the middle of three q points may sit off the straight line through the outer two. |
| `confidence` | not in the dialog | 0.8 | 0.5 to 1 | uncalibrated | A mode count or assignment whose probability is below this is reported as ambiguous. |
| `close_call_nats` | not in the dialog | 2 nats | 0 to 20 | uncalibrated | Alternatives within this much log evidence of the winner are kept for review. |
| `evidence_hysteresis_nats` | not in the dialog | 0.1 nats | 0 to 5 | uncalibrated | A count or assignment change must win the evidence by this margin, or the incoming one stays. |
| `stop_margin_nats` | not in the dialog | 5 nats | 0 to 100 | uncalibrated | The candidate search keeps stepping to a neighbouring mode count or labelling while it is no more than this many nats behind the best found, and stops at the first one further behind. |
| `linewidth_ratio` | not in the dialog | 2 | > 1 to 100 | uncalibrated | A linewidth change is reported when a mode's damping ratio to its fast solo fit passes this. |
| `full_search` | not in the dialog | false |  | uncalibrated | Price every mode count up to the full-search count and every labelling of each, with no pruning (slow; a check for calibration and proofs). |
| `full_search_max_count` | not in the dialog | 4 modes | 0 to 12 | uncalibrated | With the full search on: the largest mode count it prices (every count from none up to this). The number of labellings grows factorially with it and nothing else limits the run. |

A saved value of `q_tolerance_meV`, `q_change_scale_meV` or `composition_limit` is logged and ignored, never reported as damaged, and has no effect; the two q slopes start at their defaults, and the stopping margin does the work of the composition limit. The two q slopes are provisional until sample handling is written out. The rows marked calibrated were calibrated before the propagation of section 4 existed. That propagation also widens the temperature and curvature penalties, so for the same neighbour uncertainty those penalties, and the flag statistic they feed, are lower. Their marks stand and the calibration is not redone.

## 6. Storage

Paths are relative to the workspace folder. Every line is one JSON object.

| File | Written by | Holds |
|---|---|---|
| `intake/inter_spectra.jsonl` | the GUI, in process, through `isar/intake/inter_values.py` | user values: kinds `anchor`, `pick`, `lock`, `settings`; append-only, the latest wins |
| `report/inter_spectra.jsonl` | `save_inter` only | the machine index: `batch_start`, `refit`, `stage` and `batch_end` lines; append-only |
| `report/inter_spectra/refits/<batch_id>.jsonl` | `save_inter` only | one payload per refit with a payload: `schema`, `fit`, `unit`, `came_in`, `came_out`, `overlay`; read by byte offset |

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
carries `stage`, `index`, `round`, `progress`, `seams` and `not_reached`; `batch_end` carries how the batch
finished. Each refit payload holds what the refit came in with (`came_in`) and came out with (`came_out`, with
the branches not observed in it under `dormant`) and its `overlay`, so a history entry can be read in full. The
overlay carries the calibration, resolution and temperature the refit was made with (`calib`, `T`), so a locked
or older refit is drawn and exported in its own context, never in a later fast solo fit's.

## 7. Code locations

| Module | Owns |
|---|---|
| `isar/analyze/coupled/settings.py` | the settings table (`SETTING_DEFS`) and the checked reader (`InterSettings`) |
| `isar/analyze/coupled/types.py` | the core's boundary types, flag codes and severities |
| `isar/analyze/coupled/engine.py` | the coupled engine: the mode energies, priors and penalties of one refit; the joint objective and gradient (`objective_and_gradient`, `_value_and_grad`); the split integral and its screen (`integrate_split`, `split_evidence`, `_polish`) |
| `isar/analyze/coupled/refit.py` | one spectrum's two-stage refit, its release fit, flags and error bars; `price`, at a job's depth (full or screen) |
| `isar/analyze/prior.py` | the Laplace evidence; `hessian_from_grad`, the Hessian as a central difference of a gradient |
| `isar/analyze/fast_pass.py` | the solo fast pass; its `objective_and_gradient` hook returns nothing, so the solo pass is unchanged |
| `isar/analyze/coupled/rules.py` | the pure decision rules: acceptance order, settling, flag score, the count-first decision |
| `isar/analyze/coupled/search.py` | one spectrum's adaptive candidate search: seeds, count steps, labelling moves, screens and promotions (`Search.feed`, `_final`, `_rank`), the search record and the shadow record |
| `isar/analyze/coupled/identity.py` | persistent branch identity across neighbours |
| `isar/session/inter_spectra/nodes.py` | spectrum keys, series keys and temperatures |
| `isar/session/inter_spectra/layout.py` | the 4 × 7 analyzer grid and its adjacency |
| `isar/session/inter_spectra/graph.py` | links, curvature terms and the batch scope |
| `isar/session/inter_spectra/q_provider.py` | accepted per-analyzer hkl as q points |
| `isar/session/inter_spectra/rings.py` | anchored rings and seams |
| `isar/session/inter_spectra/schedule.py` | simultaneous terms and conflict-free parallel groups |
| `isar/session/inter_spectra/preview.py` | the batch preview line and the reason Start waits |
| `isar/session/inter_spectra/records.py` | the machine store's reader |
| `isar/session/inter_spectra/authority.py` | the one resolver of the fit in use |
| `isar/session/inter_spectra/runner.py` | the batch runner: rings, rounds, sweeps and acceptance hand-over; `_Run.assign`, the stage-1 pool that feeds each spectrum's search as its own jobs return |
| `tools/inter_timing.py` | times one batch headless and prints its timing report (`report.txt`, `timing.json` with `--out`) |
| `tools/inter_ring1.py` | opens the anchored batch headless and stops at the end of ring 1's stage 1; shared by the two measurement tools below |
| `tools/inter_gradient_compare.py` | compares the gradient path with the finite-difference path on picked real-shaped jobs, per class (`summary.txt`, `table.md`, `compare.json` with `--out`) |
| `tools/inter_screen_calibration.py` | shadow run of ring 1's stage 1: screen errors, count-total errors and shadow decisions (`calibration.json`, `calibration.md` with `--out`) |
| `isar/session/writer.py` | `save_inter`, the one writer of the machine store |
| `isar/session/analysis_service.py` | runs an `inter` queue item in the analysis process |
| `isar/session/analysis_queue.py` | the queue's bookkeeping for `inter` items |
| `isar/intake/inter_spectra.py` | the user values, read (`fold`, `read_user`) |
| `isar/intake/inter_values.py` | the checked writers of the user values |
| `isar/workspace_api/inter_spectra.py` | the read facade the GUI and notebooks use |
| `isar/report/export.py` | `isar export`: the `inter` pass and the authoritative marks |
| `isar/report/run_report.py` | the run report: the authoritative fit in the markdown and HTML |
| `isar_gui/inter_words.py` | the plain words the views show |
| `isar_gui/queue_controller.py` | the GUI side of an `inter` queue item |
| `isar_gui/viewmodels/inter_state.py` | the GUI thread's one holder of inter-spectra reads and writers |
| `isar_gui/viewmodels/inter_batch.py` | the Processing box's state: what each control shows and why it is greyed |
| `isar_gui/viewmodels/inter_fits.py` | the Scan(s) fit menu and bar: grouping, greyed-button rules, info line |
| `isar_gui/viewmodels/inter_settings.py` | the rows and deltas of the settings dialog |
| `isar_gui/viewmodels/scan_list.py` | the scan list's Anchor, Flag, Lock and Inter-spectra fit columns |
| `isar_gui/viewmodels/scans_page.py` | the Scan(s) page's inter-spectra state |
| `isar_gui/views/inter_batch_box.py` | the Processing dock's inter-spectra box |
| `isar_gui/views/inter_fit_bar.py` | the Scan(s) fit menu, Make authoritative, Lock and Waterfall of fits |
| `isar_gui/views/inter_settings_dialog.py` | Config > Inter-spectra fitting settings |
| `isar_gui/main_window.py` | the Config menu entry and the window wiring |

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

**IS6 — Curvature across q, change across T.** Decides that simultaneous batches penalise curvature along q, and anchored batches penalise change from the one fitted neighbour.
"curvature is better for dispersion … without neighbors we can't use it (we have only one end of the curve), but in simultaneous fitting it's far better."

**IS7 — Step size.** Decides that a larger temperature step widens the tolerated change, with a fourth-root starting point that is to be tested.
"I think the astros the fourth root rule is probably reasonable, not make it too large. I'm not married to it being like a linear penalty, a linear inverse penalty. We can see how that works out then."

**IS8 — Mode count.** Decides that neighbours may move a spectrum's mode count, option (ii).
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

**IS14 — No automatic start; the user picks the anchor.** Decides that nothing starts by itself and ISAR never chooses an anchor.
"I don't think there's any need for an automatic toggle" and "ISAR doesn't know what the cleanest spectrum is. The user picks an anchor"

**IS15 — No new windows.** Decides that the feature lives in the existing views.
"I don't want new windows or new big new platforms … use what we have right now"

**IS16 — A hidden mode is priced by integrating over the split.** Decides that a weak branch hidden exactly under a strong mode is priced by a numerical integral over the intensity split, not by the bell-curve approximation that fails along that flat direction. Dated 2026-10-30.
"Ok, let's integrate over the split then."

**IS17 — The q-change penalty rises from zero.** Decides that no shift of a matched mode is free: the flat zone goes, the q-change penalty is Welsch, and the neighbour's uncertainty is propagated into its width as `s_eff^2 = s^2 + (C/2) sigma^2`. Restores IS5. Dated 2026-10-30.
"we need to not have a perfectly flat zone … there is always some penalty." And: "It was a bit sigmoidal shaped; so gentle curve inside, rising more at it, but levelling off so it doesn't go to infinity." Offered Welsch or the code's quartic family, the operator chose "Welsch". The propagation was proposed by the agent from the external review; the operator answered "Ok".

**IS18 — The q tolerance is a slope times the spacing.** Decides that a q-change tolerance scales with the distance to each neighbour: a tolerated slope with one setting per metric, meV per r.l.u. with no lattice loaded and meV per inverse angstrom with one, so a number never changes meaning when a lattice is loaded. The slopes are provisional. Dated 2026-10-30.
"Your recommendation is fine, we'll have to handle sample characteristics more thoroughly in the future so this is provisional anyway." And: "The spacings could vary a lot, e.g. sample with very different in-plane versus out-of-plane lattice constants … But I think this is fine, we'll see how it does with the data we have now." Asked whether the dialog should show the saved values of retired settings, the operator answered "Ignore it, don't worry about old experiments".

**IS19 — A branch that disappears pays a price.** Decides that a branch a neighbour predicts in the window and a candidate does not name pays the mirror of its entry price, `-L/2` nats with `L = birth_edge + B(E_exp)`, so losing a mode costs what gaining one costs. The price is charged once per spectrum per snapshot, frozen for a round, and applies to anchored and simultaneous batches alike. Accepted: one branch dying while another is born far away costs more than a branch that simply moves (about 14 nats against at most about 6); the mismatch statistic still flags it. Dated 2026-10-30.
"I would think that killing the mode is fine to include a penalty for." Size: "Keep the same, it should be mirrored, I don't see any reason moving between scans that birth and death would be more or less likely". Accepted: "Ok, accept". Scope: "Yes, it applies to all".

**IS20 — Count, then labelling.** Decides that the count is chosen first, by its total probability over its distinct labellings, and the labelling is chosen within that count; a count change must beat the incoming count by `evidence_hysteresis_nats`. Dated 2026-10-30.
"Count then labelling."

**IS21 — The search starts at the seeds and the evidence stops it.** Decides that the candidate search starts at the neighbours' predictions and the spectrum's own fit and moves outward only as far as the evidence pulls: no count range, no candidate cap and no count ceiling in this pass. The stopping margin, 5 nats, is an uncalibrated start, and `full_search` remains as a check. Dated 2026-10-30.
"I will absolutely reject the idea that it's only within one mode. There's a penalty cost for having a lot more modes already, so it shouldn't go from two to six without good reason to do so." And: "I don't even like having six modes here". On the intermediate counts: "We can try this and see how it works on the data".

**IS22 — A width at its floor no longer gets a candidate refused.** Decides that the coupled refit prices a mode whose width sits at the fitting floor as a resolution-limited mode, as the fast pass does, so its candidate is not refused; and that the two defects PR #70 deferred (a change of the resolution-limited choice alone, and a shared coupling term left out of a three-spectrum chain) are fixed in this job. Dated 2026-10-30.
"Yes, include it." And, for the two deferred defects: "Fix both then".

**IS23 — A QE line squeezed to its narrowest width is the elastic line.** Decides that a quasi-elastic line whose width reaches its lower bound is a degenerate twin of the elastic line, so the candidate becomes the model without a quasi-elastic line. It lives in the shared fitting engine and applies whenever the width rails, so fast-pass results change for any spectrum whose QE width rails. Dated 2026-10-30.
"If the elastic line and QE can trade off, there is something wrong, similar to a width below the resolution; we should be railing it, forcing it to a degenerate mode". On placement: "In the shared engine now".

**IS24 — Screen first, full price for contenders.** Decides that each count step and labelling move of the candidate
search is priced first as a screen; that a screen is promoted to a full price whenever a count or label choice could
otherwise change; and that seeds and failed screens are always full. The 5-nat safety allowance
(`SCREEN_ALLOWANCE_NATS`) is an uncalibrated algorithm setting in code, not a dialog setting. The plan kept it at 5 nats
because the one ring-1 calibration run met its criterion (section 10); the operator may set another value from those
numbers. The same day's two speed-ups are the joint gradient (ruling A), a speed-up whose results agree on ordinary
and bound cases and differ on some real hidden-pair basins (section 10), and no barrier between spectra (ruling B),
which changes no results. Dated 2026-10-31.
"I think this is reasonable. … I think it is right for us to try this now, see how well it works on the data that we
have, and tune as we need to as we encounter problems" (operator, 2026-10-31).

## 9. Out of scope and open

**Out of scope, by ruling.**

- Coupling across series taken at different q settings: "we don't need to go across those scans, I don't think, right now" and "an issue for another time" (operator, 2026-10-29).
- A fitted ω(q, T) surface: "we get Omega(q, T) and Omega(q', T) and Omega (q, T') but not Omega (q', T')" (operator, 2026-10-29).
- Any automatic start, and any toggle for one (IS14); new windows (IS15).
- The campaign side (`isar drive`): its running dispersion prior and its branch identity are unchanged, and inter-spectra results do not reach a campaign.
- A full joint selection of the mode count along a track, and a deep pass on inter-spectra fits (IS12).

**Open, with who decides.**

- *Calibration.* Eighteen settings are uncalibrated starting values, and five are calibrated on synthetic tracks. Their calibration is not done. The operator's tuning list, after landing: the stopping margin (`stop_margin_nats`), the two tolerated slopes (`q_slope_meV_per_rlu`, `q_slope_meV_per_invA`) and the loss size (`birth_edge`, `birth_mid`) are starting values, provisional until tuned on data.
- *Speed.* The full anchored batch took about 58 minutes against the 30-minute yardstick (IS13, section 10). The next
  step is not built. The options are a cheaper screen for labelling moves, a lighter anchored search, or analytic
  line-shape derivatives; the operator chooses.
- *Gradient agreement.* Two real hidden pairs differ from the finite-difference path beyond the plan's 0.02-nat reading,
  and one split basin the finite-difference path priced is undetermined on the gradient path (section 10). Not decided
  here: whether the reading or the paths change.
- *Screen allowance.* The 5-nat allowance was checked on one ring-1 shadow run, for that ring's frozen priors. It does
  not establish the same error for a simultaneous batch.
- *Amplitude prior.* The amplitude-prior test was not run in planning. Amplitude stays free and there is no amplitude setting.
- *Branch identity through crossings.* An unresolved crossing stays ambiguous and is flagged. The identity lives in `isar/analyze/coupled/identity.py`; extending `isar/session/branch_tracking.py` (`track_path`) is not done.
- *Reports and exports, plan decision D6.* The fast-pass projection of `results.csv`, `modes.csv` and `report.json` is the built reading of the design's default; the operator is to confirm it.
- *Settling time.* Whether a simultaneous batch settles in useful time on a real row is not measured (section 10).
- *Search risk accepted.* A better count reachable only through counts that are each more than the stopping margin behind is not found (IS21). The operator: "We can try this and see how it works on the data."
- *Flags.* Deliberately last: "Let's leave the flags for the *end* when everything is tuned and working". The flag `branch_lost_mid_window` (look) is kept; the flag layout and any relabelling of severities are later work.
- *q-links and curvature on real data.* On the BL43LXU gpi triplets, measured in the hkl metric, the triplets lie off the straight line, so whether a curvature term forms depends on `collinearity_tol`. Its default is now 0.01, a provisional value, still uncalibrated and under the operator's review. The q proof (section 1) reports the curvature count when it is run.

**Known effects, not fixed.**

- The split integral covers one overlapping pair per fit: a stack of three overlapping modes reads `undetermined`, not integrated.
- A matched branch whose intensity vanishes with no partner under it is priced as before (often `undetermined`); the loss price of section 4 now decides its recovery, so a branch that really vanished is dropped or kept as a phantom according to the statistics: on synthetic data, a branch absent from a spectrum is dropped on strong statistics and kept as a phantom on weak ones, and a phantom hidden exactly under a strong mode costs only a few nats at any statistics, so the loss price keeps it there (`tests/analyze/test_coupled_loss.py`).
- The scan inspector's Fast column keeps showing the stored fast pass, so it can disagree with the Scan(s) plot when a refit is authoritative.
- The scan list shows a Lock column; Lock itself is set in the Scan(s) view.
- Two scans of one q setting within the temperature rounding step at one temperature get no links to each other or across; a note names them. A series with repeats gets fewer T-links, never a wrong one.
- Grid adjacency has no diagonals (assumption A1).
- Stage 1 outcomes of a simultaneous round are saved inside the next sweep's refit events, so a stop between stage 1 and the first sweep loses them. A stop in the middle of a ring loses that ring's unfinished refits; rings are saved whole.
- Skipping is within one batch only (no cross-batch skip): a rerun of an interrupted batch repeats its work.
- The fast pass's own six-mode peak-count ceiling is not changed by this work (noted, not built). On the finish-line copy, 10 of its 28 fast passes reached it.
- After the degenerate-QE rule (IS23), a BL43LXU fast pass stored by older code is stale (the recipe version `isar.central-qe-recipe/2`): the analysis queue refits it when asked, and a batch refuses a member without a current fast pass until then. Stored results are not rewritten; an experiment changes only when its fast pass is re-run.

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
- **2026-10-31, the screen calibration (`tools/inter_screen_calibration.py`, ring 1, shadow mode, a scratch copy, 44
  minutes).** Six ring-1 spectra. Role: the plan's allowance criterion, the plan's reading of "with room to spare": every
  observed underrating (full price minus screen; positive is the dangerous sign) of a split basin and of each count's
  total at most half the 5-nat allowance, and no shadow decision diverging. **Met.** Split basins (417): median about
  minus 0.5 nat, 90th percentile about 0.06 nat, largest about 0.4 nat. Count totals (24): largest about 0.05 nat. Whole
  candidates (241): largest about 0.5 nat. Shadow decisions that diverge: none. Decisions the screens could not settle
  alone, so that they would promote: 54. The allowance stays at 5 nats for this ring's frozen priors. This is a
  diagnostic of the screens, for that ring only.
- **2026-10-31, the timing (`tools/inter_timing.py`, the full 28-spectrum anchored batch, two anchors, spread in q, six
  rings, 16 workers, headless, unprofiled, a scratch copy).** Role: IS13's 30-minute figure, a yardstick and never a gate.
  **Missed.** The batch completed in about 58 minutes (3,501 s), about twice the yardstick. Rings, in order: about 12, 8,
  5, 10, 12 and 11 minutes. Pricing is essentially all of the compute: about 9.3 CPU hours over 2,643 priced candidates,
  with search, refinement and saving taking seconds. Of the priced jobs, 2,346 were screens and 238 were promotions to
  full prices (from the 26 saved refit records; the two anchors are not refitted). 167 fits failed, mostly on a Hessian
  that is not positive definite. Pool utilisation was 0.60, and one ring-1 spectrum's search alone took about 6,600 CPU
  seconds over 246 candidates.
- **Where the time goes, and a next step (proposed, not built).** The time is in the number of candidates each search
  prices: most priced jobs are screens of count steps and labelling moves, not the split integral that the screens cut
  (the coordinator's reading of the saved records). The options the design names that cut that number are the ones with
  room: a cheaper screen for labelling moves (re-scoring a parent's fit), or a lighter anchored search. The coordinator
  estimates the column differences at about 45 per cent of a hidden-pair job, so analytic line-shape derivatives would cut
  at most that share, and cannot reach the yardstick on their own. The operator decides which, if any, to build.
