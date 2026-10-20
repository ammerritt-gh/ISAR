# HANDOFF — tests on artificial data only

> **Status:** live
> **Owner:** Adrian
> **Purpose:** the starting point for a fresh design session: take every test off real data,
> decide how the artificial replacement data is made, and move the sealed golden gate out of the
> test suite. Delete when that work lands.

## 1. How this came up (2026-10-20)

Landing the conference-demo branch, the gate suite ran in a fresh job worktree and 40 tests
failed: every one a `FileNotFoundError` under `test_data/`, which is gitignored and so absent from
any checkout but the main one. The first proposals (skip when absent; find the data in the main
checkout; an opt-in real-data lane) were all rejected by the operator as the wrong approach.

## 2. Rulings so far (operator, 2026-10-20 — the starting point, not up for re-litigation)

- **No test relies on real data, flat out.** "It should not—no tests in our repo should rely on
  real data, flat out. It's not a test then. It's something we can proof against, but it's not a
  test." Tests are "things that we use to check the validity of our code routinely."
- **Anything a real file proves, artificial data can prove.** A parser check needs only the
  format: "we just write the data in a format once that we need ... we can mix up some artificial
  sets here that have the same format and just different data, and then we can commit it." An
  agreement check is made the same way: "If we want to agree with data, we make up artificial data
  that matches what we need and check it that way." Cosmic rays: "We can make an artificial cosmic
  ray." The operator will not accept "artificial data cannot do this" as an argument.
- **New data gets an artificial copy.** "Whenever we get new data that we want to check, we make
  an artificial copy of it ... We can preserve things like headers and get it basically exactly the
  same." Artificial fixtures are committed; `test_data/` stays gitignored.
- **The sealed golden gate leaves the test suite (option 2).** "This is no longer a test ... It's a
  proof against the data that we have, and it's kind of up on my end here ... It's a campaign or
  whatever you want to call it." It runs on the operator's say-so, never routinely. This changes
  how ruling S12 is housed (it is no longer a pytest marker), not what it guards.

## 3. Open for the design session

The operator asked for a real discussion of what the tests are for, what they check against, and
how the artificial data is generated — not a small design call. Questions already visible:

1. **What a test asserts.** Many current real-file tests pin facts of one file (90 scans, 41
   points, TT = 298.772 K). With artificial data the truth is known by construction — what does
   each test assert against, and where does that truth live?
2. **How artificial data is generated.** `isar/synth_format/` already emits SPEC and numor files
   (`run_builder.py`) with real-file oddities (`quirks.py`), and `tests/roundtrip/` uses it. Is a
   committed fixture a file that generator writes once, a file regenerated at test time, or a
   hand-built excerpt with real headers and invented counts? Who regenerates it, and how is drift
   between generator and fixture caught?
3. **Inverse crime.** The round-trip tests prove emitter and parser are consistent inverses. The
   real-file smoke tests were added to guard the case where both share a misreading. Under the
   rulings that guard becomes: the artificial copy preserves the real file's header and layout.
   How faithful the copy must be, and how that is checked when the copy is made, is to be settled.
4. **Where proofing lives.** The golden gate (and any other real-data check that survives as
   proofing) needs a home outside `testpaths`, a name, and a way to run it.
5. **Neighbours that also read `test_data/`** (decide in or out of scope):
   `tools/docs/collect_evidence.py` builds the `docs/assets/` evidence bundle from `test_data/`
   and `tests/docs/test_doc_drift.py:587` asserts the bundle recorded it;
   `isar/synth_format/roundtrip.py:23-25` hardcodes real paths for its human eyeball pause.

## 4. Inventory (at `7457adc`)

Tests that read `test_data/` and what each actually checks:

| File | Checks | Planned fate |
|---|---|---|
| `tests/test_registry.py` (4 tests) | detect S30 vs IN8, dispatch | artificial fixture |
| `tests/test_spec_base.py` (5) | SPEC block structure, #V↔#H map, column lookup | artificial fixture |
| `tests/test_numor_base.py` (~5) | numor keyword header, columns by name | artificial fixture |
| `tests/reduce/test_combine.py` (2) | `reduce_record` fills the reject mask | artificial fixture |
| `tests/real_data/test_s30_smoke.py`, `test_s30_raw_smoke.py`, `test_in8_smoke.py` | parser sanity on facility files (Gate B) | artificial copies with real headers |
| `tests/real_data/test_s30_reduction_vs_supplementary.py` | our σ-clip reproduces the beamline's 5σ/3σ decisions; ana4 anomaly | artificial ensembles with injected cosmic rays; the ana4 fact moves to a doc |
| `tests/real_data/test_in8_resolution.py` | Cooper–Nathans width ≈ 0.58 meV from a numor header | header values written into the test |
| `tests/real_data/test_in8_gold_smoke.py` | gold table lists existing numors | data hygiene, not a test — delete |
| `tests/session/test_calibrations.py` (2, skip-if-absent) | `resolve()` reproduces the gold-check recipe | artificial set with a generated gold table |
| `tests/session/test_driver_smoke.py` (3, skip-if-absent) | driver end-to-end on IN8 anchors | artificial set |
| `tests/real_data/test_golden_scenarios.py` | sealed held-out gate (S12) | moves out of the suite (§2) |

`tests/real_data/test_adjudicate_bakeoff.py` reads no `test_data/` (it needs LLMM) and is not in
scope. Also fold in: `tests/workspace_api/test_watch.py::test_two_rapid_changes_coalesce` is
timing-flaky under `-n 16` (debounce 150 ms vs a 60 ms gap; fix is a larger debounce for that
test only).

When this lands: `AGENTS.md` gains the hard rule (no test reads `test_data/`), and its Quick
Reference lines about `tests/real_data/` and the `golden` marker are rewritten to match.
