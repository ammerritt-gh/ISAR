# Inter-spectra fitting: the second pass that couples neighbouring spectra

> **Status:** live
> **Authority:** canonical design for inter-spectra fitting (the second pass that couples neighbouring spectra)
> **Verified against code revision:** `9547e53e` (2026-10-29)

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
| Coupled refit of one spectrum: count and assignment stage, continuous stage, one shared objective, release fit | Implemented | `isar/analyze/coupled/refit.py`, `engine.py`, `rules.py` | `tests/analyze/test_coupled_engine.py`, `test_coupled_refit.py`, `test_coupled_rules.py` |
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
| Real-data proof and the timing record | Partial: written and gated (`ISAR_LETACON_INTER=1`), not yet run on the real set; the q side is its own module | `proofs/test_letacon_inter_spectra.py`, `proofs/inter_spectra_checks.py`, `proofs/test_letacon_inter_spectra_q.py`, `proofs/inter_spectra_q_checks.py` | `tests/test_inter_spectra_proof_checks.py`, `tests/test_inter_spectra_q_checks.py` |

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
- **Pricing is exact.** A cached price is reused only when every value it depends on is unchanged; the
  runner's cache keys hash those values. Reusing prices across a changed snapshot is a measured option for
  the operator and is not applied.
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
start. The anchors a batch used are recorded with it, so later edits to the anchors never change a past batch.

**Simultaneous batch.** Every selected spectrum pays the penalties on its links and curvature terms in one
shared objective: its own Baker–Cousins cost plus each term it appears in. The objective is solved in rounds.
Each round has two stages. Stage 1 compares mode counts and branch assignments against one frozen snapshot of
the neighbours; two slot orders of one assignment that reach the same fit count its evidence once, under the
order of its fitted energies. Stage 2 refines the continuous parameters, with counts and assignments held fixed, in sweeps of
per-spectrum refits over conflict-free parallel groups; each refit includes every term the spectrum is in.
Counts and assignments are reopened in later rounds. Iterated refits are not guaranteed to reach the same
answer from every start. A batch settles when counts and assignments stop changing and sweeps are quiet: no
mode energy moves by more than `sweep_tol_meV`, two quiet sweeps in a row, and the total cost no longer improves
by `cost_rel_tol`. A batch that reaches `sweep_cap` first keeps its results and is flagged `did_not_settle`.

**Member states.** While a batch runs, the runner reports every member's state: `waiting`, `fitting` (a pool
worker holds its job; at most `workers` at once), `done`, `refused`, `failed`, `not_reached`, or `held` (anchor,
valid lock or stale lock), with a reason for the last four and for a stale lock. It sends the whole map as a
`members` event, at most one per 0.25 s from pool transitions and at each phase boundary. The analysis service
relays it between the item's `started` and `finished` (protocol and ordering: the `analysis_service.py` module
docstring). The events change no fit, acceptance, save, count or progress record.

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

**Mode count and entry.** The count of modes is chosen by evidence. A neighbour's prediction narrows the prior
on each mode's energy, so a second mode that the neighbours show clearly can win in an ambiguous spectrum; the
count is not forced to be equal along a track (IS8). A mode that no neighbour matches pays the entry price
`birth_edge`, and `birth_mid` more when it appears in the middle of the energy window, not at its edges (IS9). A
mode the fast pass fitted as one, which the neighbours resolve into two, is reported as a neighbour-supported
split (severity repaired), not as a new branch. A branch that a neighbour predicts in the window and that is not seen here is flagged
`branch_lost_mid_window` and carries no fit cost (section 9).

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

**Time.** The 30-minute figure is a target, not a limit (IS13). Nothing stops, fails or is cut because of it; going
well over it is a reason to optimise. Section 10 says what has been measured.

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
| `q_tolerance_meV` | up front | 2 meV | 0 to 50 | uncalibrated | Anchored batches: the energy change along q that costs nothing before any penalty applies. |
| `t_tolerance_meV` | up front | 1 meV | > 0 to 50 | calibrated on synthetic tracks, not on real data | Temperature neighbours: the energy change that costs little at the reference temperature step. |
| `flag_threshold` | up front | 54 | > 0 to 1000 | calibrated on synthetic tracks, not on real data | A spectrum is flagged for review when its flag score reaches this value. |
| `shape_family` | advanced | welsch | one of welsch, quartic | uncalibrated | The shape of the penalty: welsch (bounded, the default) or quartic (for comparison). |
| `step_exponent` | advanced | 0.25 | 0 to 2 | calibrated on synthetic tracks, not on real data | How much the tolerated temperature change widens with the size of the temperature step. |
| `sweep_tol_meV` | advanced | 0.01 meV | > 0 to 1 | uncalibrated | A sweep that moves no mode energy by more than this is quiet; two quiet sweeps settle a batch. |
| `sweep_cap` | advanced | 20 sweeps | 2 to 1000 | uncalibrated | A simultaneous batch that has not settled after this many sweeps is kept and flagged as did not settle. |
| `q_change_scale_meV` | not in the dialog | 1 meV | > 0 to 50 | uncalibrated | Anchored q changes: the energy scale of the penalty beyond the zero zone. |
| `q_curvature_scale_meV` | not in the dialog | 1 meV | > 0 to 50 | uncalibrated | Simultaneous q links: the energy scale of the curvature residual along a line of three spectra. |
| `reference_T_K` | not in the dialog | 100 K | > 0 to 1000 | calibrated on synthetic tracks, not on real data | The temperature step, in kelvin, at which the temperature tolerance applies unchanged. |
| `t_spacing_form` | not in the dialog | widen | one of widen, divide | uncalibrated | How the temperature step enters the penalty: widen (the default) or divide (the literal form, for comparison). |
| `penalty_cap` | not in the dialog | 12 deviance | > 0 to 1000 | calibrated on synthetic tracks, not on real data | The largest price one penalty term can add to a spectrum's fit. |
| `birth_edge` | not in the dialog | 2 deviance | 0 to 1000 | uncalibrated | The entry price of a branch that no neighbour predicts, at any energy in the window. |
| `birth_mid` | not in the dialog | 12 deviance | 0 to 1000 | uncalibrated | The extra entry price of such a branch when it appears in the middle of the window. |
| `cost_rel_tol` | not in the dialog | 1e-05 | > 0 to 1 | uncalibrated | A sweep whose total cost improves by less than this relative amount is not improving. |
| `collinearity_tol` | not in the dialog | 0.01 | 0 to 1 | uncalibrated | How far, relative to its length, the middle of three q points may sit off the straight line through the outer two. |
| `composition_limit` | not in the dialog | 4 | 0 to 100 | uncalibrated | How many ranked alternative compositions are priced per mode count, beyond the incoming and all-unmatched ones. |
| `confidence` | not in the dialog | 0.8 | 0.5 to 1 | uncalibrated | A mode count or assignment whose probability is below this is reported as ambiguous. |
| `close_call_nats` | not in the dialog | 2 nats | 0 to 20 | uncalibrated | Alternatives within this much log evidence of the winner are kept for review. |
| `evidence_hysteresis_nats` | not in the dialog | 0.1 nats | 0 to 5 | uncalibrated | A count or assignment change must win the evidence by this margin, or the incoming one stays. |
| `linewidth_ratio` | not in the dialog | 2 | > 1 to 100 | uncalibrated | A linewidth change is reported when a mode's damping ratio to its fast solo fit passes this. |
| `full_search` | not in the dialog | false |  | uncalibrated | Price every allowed count and composition, not only the triggered ones (slow; for calibration and proofs). |

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
settings, preview, fixed and not-reached counts, terms, metric, core, `intake_seq` and lattice epochs; `stage`
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
| `isar/analyze/coupled/engine.py` | the coupled engine: the mode energies, priors and penalties of one refit |
| `isar/analyze/coupled/refit.py` | one spectrum's two-stage refit, its release fit, flags and error bars |
| `isar/analyze/coupled/rules.py` | the pure decision rules: acceptance order, settling, flag score |
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
| `isar/session/inter_spectra/runner.py` | the batch runner: rings, rounds, sweeps and acceptance hand-over |
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
record are joined; an ellipsis marks an elision in the record). Each is dated 2026-10-29. Each names what it
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

## 9. Out of scope and open

**Out of scope, by ruling.**

- Coupling across series taken at different q settings: "we don't need to go across those scans, I don't think, right now" and "an issue for another time" (operator, 2026-10-29).
- A fitted ω(q, T) surface: "we get Omega(q, T) and Omega(q', T) and Omega (q, T') but not Omega (q', T')" (operator, 2026-10-29).
- Any automatic start, and any toggle for one (IS14); new windows (IS15).
- The campaign side (`isar drive`): its running dispersion prior and its branch identity are unchanged, and inter-spectra results do not reach a campaign.
- A full joint selection of the mode count along a track, and a deep pass on inter-spectra fits (IS12).

**Open, with who decides.**

- *Calibration.* Seventeen settings are uncalibrated starting values. Their calibration is not done.
- *Branch loss.* Built as a flag only, with no fit cost: a working assumption from the expert pair, not yet ruled by the operator. Whether a genuine loss should carry a fit cost mirroring the entry price is his to decide.
- *Amplitude prior.* The amplitude-prior test was not run in planning. Amplitude stays free and there is no amplitude setting.
- *Branch identity through crossings.* An unresolved crossing stays ambiguous and is flagged. The identity lives in `isar/analyze/coupled/identity.py`; extending `isar/session/branch_tracking.py` (`track_path`) is not done.
- *Reports and exports, plan decision D6.* The fast-pass projection of `results.csv`, `modes.csv` and `report.json` is the built reading of the design's default; the operator is to confirm it.
- *Settling time.* Whether a simultaneous batch settles in useful time on a real row is not measured (section 10).
- *q-links and curvature on real data.* On the BL43LXU gpi triplets, measured in the hkl metric, the triplets lie off the straight line, so whether a curvature term forms depends on `collinearity_tol`. Its default is now 0.01, a provisional value, still uncalibrated and under the operator's review. The q proof (section 1) reports the curvature count when it is run.

**Known effects, not fixed.**

- The scan inspector's Fast column keeps showing the stored fast pass, so it can disagree with the Scan(s) plot when a refit is authoritative.
- The scan list shows a Lock column; Lock itself is set in the Scan(s) view.
- Two scans of one q setting within the temperature rounding step at one temperature get no links to each other or across; a note names them. A series with repeats gets fewer T-links, never a wrong one.
- Grid adjacency has no diagonals (assumption A1).
- Stage 1 outcomes of a simultaneous round are saved inside the next sweep's refit events, so a stop between stage 1 and the first sweep loses them. A stop in the middle of a ring loses that ring's unfinished refits; rings are saved whole.

## 10. Measured so far

Nothing about this pass has been measured on real data, and no timing has been taken on a real row. The
real-data proof and the timing record are Partial (section 1): the proof is written and has not been run. When either is run, a dated line goes here
with its timing and flag results and no data values, because measured values never enter committed text
(AGENTS.md hard rule 10).
