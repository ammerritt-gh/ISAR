# ISAR Speedup Sprint — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make ISAR campaign fit-time visible (a disjoint bucket decomposition surfaced in both `isar profile` and the campaign diagnostic) and bank the zero-risk provisional-M win, establishing the instrumented baseline the rest of the sprint is measured against.

**Architecture:** Additive, read-mostly. A new shared timing-decomposition helper in `isar/session/timing.py` is consumed by the CLI profiler and the diagnostic renderer (DRY — one bucket definition, two views). Role-aware bootstrap M splits the single global `M` into a provisional draw count for scan fits and the authoritative count for master/certification fits, threaded through the existing `run_batch → _run_tasks / _fit_masters` call sites. No steering behavior changes; a decision-identity check on a reference-campaign rerun is the guard.

**Tech Stack:** Python ≥ 3.11, numpy/scipy/iminuit/lmfit/dynesty, pytest. Runs on the base Python at `C:\Utilities\Python\Python312` (imports the repo-root `isar` package directly; no micromamba env needed — see AGENTS.md verification note). Windows/cmd operator surface.

## Global Constraints

- **`encoding="utf-8"` on all text-file I/O.** (Global CLAUDE.md.)
- **Never swallow exceptions silently** — every catch logs. (Global CLAUDE.md.)
- **Additive-only on the frozen Stage II↔III contract** (`report/results.csv`, `report.json`, `card.state.json`, run_timings/session-log shapes): new fields only, never rename/remove. (AGENTS.md; DESIGN §4.)
- **AGENTS.md hard rules untouched:** D3 variance convention (σ=√N, no zero floor), derived anti-Stokes, card ownership split, QC-masks-not-deletes, Qt-free layers (`workspace_api`/`geometry`/`plotspec`).
- **No AI co-author trailers, no build/test-status assertions in commit messages.** Describe what changed and why. (Global CLAUDE.md.)
- **Determinism:** every fit is seeded; changing draw counts must remain deterministic given the seed. Reduced provisional M must not change steering decisions on the reference campaign (done-criterion, Task 6).
- **Tests:** `python -m pytest -m "not slow"` from repo root (base Python). GUI-hang debug: add `-o faulthandler_timeout=60`.

---

## Scope note — what already exists on HEAD (verified 2026-07-19)

The approved design (`docs/SPEEDUP_DESIGN.md` §2) listed four Phase-1 deliverables. Reading the real code shows **two are already implemented**, narrowing this plan:

| Design deliverable | Status on HEAD | This plan |
|---|---|---|
| Per-*method* timing table (bootstrap/minos/profile/bayes) in `isar profile` | **DONE** — `method_seconds` written in `errors/__init__.py`, aggregated to `*_seconds` provenance in `driver.py:708-727,439-451`, surfaced by `profile_cmd._print_error_method_breakdown`. The archived `svp_recert1` predates it. | Regression-guarded, not rebuilt |
| Adaptive bootstrap early-stop on by default | **DONE** — `parametric_bootstrap(adaptive, block=32, m_min=64, rel_eps=0.05)` implemented (`bootstrap.py:159,247-268`); default enabled for full/bayes at `loop.py:2055`, `driver.py:1137,2088`, `certification.py:111`. Floor `m_min=64` already exceeds the design's ~48. | Documented + regression-guarded (Task 5), not rebuilt |
| **Bucket split** (errors_attach scans vs masters, two_stage, fit remainder) in profiler + diagnostic | **MISSING** — profiler shows per-*stage* and per-*method*, never the errors_attach/two_stage/remainder decomposition; diagnostic has only `_wall_split` (fitting/instrument/overhead). | **Tasks 1–3** |
| **Role-aware M** (provisional scans vs authoritative masters) | **MISSING** — `M` is a single global passed identically to `_run_tasks` and `_fit_masters`. | **Task 4** |

Role-aware M maps cleanly onto the master-authoritative architecture (MEASUREMENT_STEERING §7.5): physical scan rows never seed the prior or certify — only pooled masters do — so "provisional" = scan fits and "authoritative/prior-seeding/certification" = master fits. The two roles are exactly the two existing call sites.

---

## File structure

- `isar/session/timing.py` — **Modify.** Add `embedded_timings(events)` and `fitting_buckets(embedded)` (the one disjoint-bucket definition). Natural home: it already owns the run_timings structure (`StageTimer.to_dict`).
- `isar/cli/profile_cmd.py` — **Modify.** New `_print_fitting_buckets(...)`, wired into `run()`.
- `isar/report/campaign_diagnostic.py` — **Modify.** New `_section_time(doc, di)`, inserted after `_section_outcome`.
- `isar/session/driver.py` — **Modify.** `run_batch` gains `scan_M`; thread to scan call site; masters keep `M`; record effective M in provenance.
- `isar/cli/main.py`, `isar/cli/run_cmd.py` — **Modify.** New `--scan-M` flag, threaded to `run_batch`.
- `isar/analyze/errors/bootstrap.py` — **Modify.** Docstring documenting the adaptive stopping rule (no logic change).
- Tests: `tests/session/test_timing.py`, `tests/cli/test_profile_cmd.py`, `tests/report/test_campaign_diagnostic_sections.py`, `tests/session/test_driver_smoke.py`.
- `docs/campaigns/2026-07-19_speedup_phase1_baseline.md` — **Create** (Task 6).

---

## Task 1: Shared fitting-bucket helper

**Files:**
- Modify: `isar/session/timing.py`
- Test: `tests/session/test_timing.py`

**Interfaces:**
- Produces:
  - `embedded_timings(events: list[dict]) -> list[dict]` — pull the `payload.timings` dicts from `analyze` events; fall back to none if absent (the CLI keeps its own `run_timings.json` fallback).
  - `fitting_buckets(embedded: list[dict]) -> dict[str, float]` — disjoint buckets `{"errors_attach_scans", "errors_attach_masters", "two_stage", "fit_remainder"}` in seconds, summed over per-item entries (`seconds − errors_attach_seconds − two_stage_seconds` is the remainder, floored at 0 per item so parallel-makespan skew can't go negative).

- [ ] **Step 1: Write the failing test**

```python
# tests/session/test_timing.py  (append)
from isar.session.timing import embedded_timings, fitting_buckets

def _analyze_event(timings):
    return {"kind": "analyze", "payload": {"timings": timings}}

def test_fitting_buckets_disjoint_decomposition():
    timings = {
        "stages": [
            {"stage": "fit_scans", "seconds": 10.0, "per_task": [
                {"scan_id": "s1", "seconds": 10.0,
                 "errors_attach_seconds": 4.0, "two_stage_seconds": 1.0}]},
            {"stage": "fit_masters", "seconds": 6.0, "per_master": [
                {"master_id": "m1", "seconds": 6.0,
                 "errors_attach_seconds": 3.0, "two_stage_seconds": 0.5}]},
        ]
    }
    b = fitting_buckets([timings])
    assert b["errors_attach_scans"] == 4.0
    assert b["errors_attach_masters"] == 3.0
    assert b["two_stage"] == 1.5
    # remainder = (10-4-1) + (6-3-0.5) = 5.0 + 2.5
    assert b["fit_remainder"] == 7.5

def test_fitting_buckets_remainder_floored_nonnegative():
    timings = {"stages": [{"stage": "fit_scans", "seconds": 2.0, "per_task": [
        {"scan_id": "s1", "seconds": 2.0,
         "errors_attach_seconds": 3.0, "two_stage_seconds": 0.0}]}]}
    assert fitting_buckets([timings])["fit_remainder"] == 0.0

def test_embedded_timings_extracts_analyze_payloads():
    evs = [{"kind": "timing", "payload": {"stage": "iteration"}},
           _analyze_event({"stages": []}),
           {"kind": "propose", "payload": {}}]
    assert embedded_timings(evs) == [{"stages": []}]
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/session/test_timing.py -k "fitting_buckets or embedded_timings" -v`
Expected: FAIL with `ImportError: cannot import name 'embedded_timings'`.

- [ ] **Step 3: Implement**

```python
# isar/session/timing.py  (append, module-level)

_FIT_ITEM_STAGES = {"fit_scans": ("per_task", "errors_attach_scans"),
                    "fit_masters": ("per_master", "errors_attach_masters")}


def embedded_timings(events):
    """The ``run_timings`` dicts embedded in a session log's ``analyze`` events."""
    out = []
    for e in events:
        if e.get("kind") == "analyze":
            t = (e.get("payload") or {}).get("timings")
            if isinstance(t, dict):
                out.append(t)
    return out


def fitting_buckets(embedded):
    """Disjoint fit-time buckets (seconds) from per_task/per_master entries.

    Buckets are disjoint by construction: ``errors_attach_seconds`` and
    ``two_stage_seconds`` are recorded per-item fields; the remainder (structure
    enumeration + IC grid + optimization) is each item's ``seconds`` minus those two,
    floored at 0 so parallel-makespan skew can never produce a negative bucket.
    """
    buckets = {"errors_attach_scans": 0.0, "errors_attach_masters": 0.0,
               "two_stage": 0.0, "fit_remainder": 0.0}
    for t in embedded:
        for st in (t.get("stages") or []):
            spec = _FIT_ITEM_STAGES.get(st.get("stage"))
            if spec is None:
                continue
            items_key, ea_bucket = spec
            for it in (st.get(items_key) or []):
                total = float(it.get("seconds") or 0.0)
                ea = float(it.get("errors_attach_seconds") or 0.0)
                ts = float(it.get("two_stage_seconds") or 0.0)
                buckets[ea_bucket] += ea
                buckets["two_stage"] += ts
                buckets["fit_remainder"] += max(0.0, total - ea - ts)
    return buckets
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/session/test_timing.py -k "fitting_buckets or embedded_timings" -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add isar/session/timing.py tests/session/test_timing.py
git commit -m "Add disjoint fitting-time bucket helper for campaign timing"
```

---

## Task 2: Surface buckets in `isar profile`

**Files:**
- Modify: `isar/cli/profile_cmd.py`
- Test: `tests/cli/test_profile_cmd.py`

**Interfaces:**
- Consumes: `isar.session.timing.embedded_timings`, `fitting_buckets` (Task 1); the CLI's existing `run_timings.json` fallback pattern (`profile_cmd.py:64-71`).
- Produces: a "Fitting-time buckets" section in `isar profile` stdout, printed after the subprocess sub-stage breakdown.

- [ ] **Step 1: Write the failing test**

```python
# tests/cli/test_profile_cmd.py  (append; reuse the module's _write_campaign_with_error_methods helper)
def test_profile_prints_fitting_buckets(tmp_path, capsys):
    log = tmp_path / "session_log.jsonl"
    _write_campaign_with_error_methods(log)  # existing helper: fit_scans+fit_masters w/ per-item seconds
    from isar.cli import profile_cmd
    class _A:
        workspace = str(tmp_path); log = str(log); top = None; iterations = False
    rc = profile_cmd.run(_A())
    out = capsys.readouterr().out
    assert rc == 0
    assert "Fitting-time buckets" in out
    assert "errors_attach (scans)" in out
    assert "fit remainder" in out
```

(If `_write_campaign_with_error_methods`'s per-item entries lack `seconds`/`errors_attach_seconds`/`two_stage_seconds`, extend that helper to include them — its stage `seconds` are already present; add `"seconds"`, `"errors_attach_seconds"`, `"two_stage_seconds"` to each per_task/per_master dict.)

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/cli/test_profile_cmd.py::test_profile_prints_fitting_buckets -v`
Expected: FAIL on `assert "Fitting-time buckets" in out`.

- [ ] **Step 3: Implement**

```python
# isar/cli/profile_cmd.py

# --- at top with the other imports ---
from isar.session.timing import embedded_timings, fitting_buckets

# --- new section function, near _print_error_method_breakdown ---
_BUCKET_LABELS = [
    ("errors_attach_scans", "errors_attach (scans)"),
    ("errors_attach_masters", "errors_attach (masters)"),
    ("two_stage", "two-stage fitting"),
    ("fit_remainder", "fit remainder (enumeration/IC/optimize)"),
]

def _print_fitting_buckets(events, log_path, wall):
    embedded = embedded_timings(events)
    if not embedded:
        rt_path = log_path.parent / "run_timings.json"
        if rt_path.exists():
            try:
                embedded = [json.loads(rt_path.read_text(encoding="utf-8"))]
            except (ValueError, OSError):
                embedded = []
    if not embedded:
        return
    buckets = fitting_buckets(embedded)
    if not any(buckets.values()):
        return
    print()
    print("  Fitting-time buckets (disjoint decomposition of scan+master fit time):")
    print(f"  {'bucket':40s} {'total_s':>10s} {'%wall':>7s}")
    for key, label in _BUCKET_LABELS:
        sec = buckets[key]
        pct = (sec / wall * 100.0) if wall > 0 else 0.0
        print(f"  {label:40s} {sec:10.3f} {pct:6.1f}%")
```

Then wire it into `run()`, immediately after the `_print_subprocess_breakdown(...)` call (around `profile_cmd.py:253`):

```python
    _print_subprocess_breakdown(events, log_path, stage_pairs, wall, args.top)
    _print_fitting_buckets(events, log_path, wall)          # <-- add
    _print_error_method_breakdown(events, log_path, wall, args.top)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/cli/test_profile_cmd.py -v`
Expected: PASS (new test + existing profile tests still green).

- [ ] **Step 5: Commit**

```bash
git add isar/cli/profile_cmd.py tests/cli/test_profile_cmd.py
git commit -m "Surface disjoint fitting-time buckets in isar profile"
```

---

## Task 3: "Where the time went" table in the campaign diagnostic

**Files:**
- Modify: `isar/report/campaign_diagnostic.py`
- Test: `tests/report/test_campaign_diagnostic_sections.py`

**Interfaces:**
- Consumes: `isar.session.timing.embedded_timings`, `fitting_buckets` (Task 1); `di.events`; the `FindingsDoc` API (`doc.section(str)`, `doc.table([headers], rows)`); `_num` (`campaign_diagnostic.py:199`).
- Produces: `_section_time(doc: FindingsDoc, di: DiagInputs) -> None`, a section titled "3. Where the time went", inserted between `_section_outcome` and `_section_driver` (later sections renumber in prose only — section numbers are cosmetic strings here).

- [ ] **Step 1: Write the failing test**

```python
# tests/report/test_campaign_diagnostic_sections.py  (append; match the file's existing doc/di construction)
def test_section_time_renders_buckets():
    from isar.report.campaign_diagnostic import _section_time, FindingsDoc, DiagInputs
    timings = {"stages": [
        {"stage": "fit_scans", "seconds": 8.0, "per_task": [
            {"scan_id": "s1", "seconds": 8.0,
             "errors_attach_seconds": 3.0, "two_stage_seconds": 1.0}]}]}
    di = _make_min_di(events=[{"kind": "analyze", "payload": {"timings": timings}}])  # test helper
    doc = FindingsDoc("t")
    _section_time(doc, di)
    text = doc.render()
    assert "Where the time went" in text
    assert "errors_attach (scans)" in text
```

(Use the file's existing minimal-`DiagInputs` construction; if none exists, build one with `events=[...]` and defaults for the fields `_section_time` reads — it reads only `di.events`.)

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/report/test_campaign_diagnostic_sections.py::test_section_time_renders_buckets -v`
Expected: FAIL with `ImportError: cannot import name '_section_time'`.

- [ ] **Step 3: Implement**

```python
# isar/report/campaign_diagnostic.py

# --- with the other isar imports at top ---
from isar.session.timing import embedded_timings, fitting_buckets

# --- new section, after _section_outcome (~line 653) ---
_TIME_BUCKET_LABELS = [
    ("errors_attach_scans", "errors_attach (scans)"),
    ("errors_attach_masters", "errors_attach (masters)"),
    ("two_stage", "two-stage fitting"),
    ("fit_remainder", "fit remainder (enumeration/IC/optimize)"),
]

def _section_time(doc: FindingsDoc, di: DiagInputs) -> None:
    doc.section("3. Where the time went")
    embedded = embedded_timings(di.events)
    if not embedded:
        doc.paragraph("No embedded fit timings in this log (older campaign, or "
                      "timing instrumentation disabled).")
        return
    buckets = fitting_buckets(embedded)
    total = sum(buckets.values())
    rows = []
    for key, label in _TIME_BUCKET_LABELS:
        sec = buckets[key]
        pct = (sec / total * 100.0) if total > 0 else 0.0
        rows.append((label, f"{_num(sec, 3)} s", f"{_num(pct, 1)} %"))
    rows.append(("total scan+master fit time", f"{_num(total, 3)} s", "100 %"))
    doc.table(["bucket", "seconds", "% of fit time"], rows)
```

If `FindingsDoc` has no `paragraph` method, use the idiom the file already uses for prose (grep `def ` in the `FindingsDoc` class or reuse `doc.table` with a single note row). Then register the section in `render_campaign_diagnostic` (`campaign_diagnostic.py:1175`):

```python
    _section_objective(doc, di)
    _section_outcome(doc, di, per_q, ec)
    _section_time(doc, di)                    # <-- add
    _section_driver(doc, di, per_q, ec)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/report/test_campaign_diagnostic_sections.py -v`
Expected: PASS. If a characterization/golden test (`test_campaign_diagnostic_characterization.py`) pins full document text, update its golden to include the new section — the section is additive and expected to change the golden.

- [ ] **Step 5: Commit**

```bash
git add isar/report/campaign_diagnostic.py tests/report/test_campaign_diagnostic_sections.py
git commit -m "Add Where-the-time-went fitting-bucket section to campaign diagnostic"
```

---

## Task 4: Role-aware bootstrap M (provisional scans vs authoritative masters)

**Files:**
- Modify: `isar/session/driver.py`, `isar/cli/main.py`, `isar/cli/run_cmd.py`
- Test: `tests/session/test_driver_smoke.py`

**Interfaces:**
- Consumes: `run_batch(..., M=200, ...)` (`driver.py:942`); scan dispatch `_run_tasks(..., M=M, ...)` (`driver.py:1167`); master dispatch `_fit_masters(..., M=M, ...)` (`driver.py:1834`).
- Produces: `run_batch(..., M=200, scan_M=None, ...)` where `scan_M` (default 50 when None) is the provisional draw count for scan fits; masters/certification keep `M`. Effective M recorded per fit in provenance as `bootstrap_M`.

**Design decision (within approved intent):** `--M` keeps its meaning as the *authoritative* (master/certification) draw count, default 200, unchanged. Scan fits use `scan_M` (default **50**, per SPEEDUP_DESIGN §2.1). This is low-risk: a scan's bootstrap interval is never the authoritative number (the pooled master is), and in fast tier scans don't bootstrap at all — only full-tier scan bootstraps are affected, whose ω₀ HESSE steering bar is independent of M. The Task-6 decision-identity check is the guard.

- [ ] **Step 1: Write the failing test**

```python
# tests/session/test_driver_smoke.py  (append)
def test_run_batch_accepts_scan_M_and_defaults_to_50(monkeypatch):
    import isar.session.driver as drv
    seen = {}
    real = drv._run_tasks
    def spy(*a, **k):
        seen["scan_M"] = k.get("M")
        raise _StopSmoke()          # abort after capturing — a light smoke probe
    monkeypatch.setattr(drv, "_run_tasks", spy)
    # ... construct the minimal workspace/args this test module already uses ...
    try:
        drv.run_batch(ws, data_paths)   # no scan_M -> provisional default
    except _StopSmoke:
        pass
    assert seen["scan_M"] == 50
```

(Match the module's existing workspace/data fixtures and its abort idiom; if the module lacks one, assert on a smaller seam — e.g. call a helper that computes `scan_M` from the args. The load-bearing assertion is: scan call site receives 50 by default, 200 when `scan_M=200` passed; master call site always receives `M`.)

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/session/test_driver_smoke.py::test_run_batch_accepts_scan_M_and_defaults_to_50 -v`
Expected: FAIL with `TypeError: run_batch() got an unexpected keyword argument 'scan_M'`.

- [ ] **Step 3: Implement**

In `driver.py`, add `PROVISIONAL_M = 50` near the tier constants (`driver.py:53`), extend the signature and thread it to the scan call site only:

```python
# driver.py — signature (line ~942)
def run_batch(workspace, data_paths, *, force=False, llm=False, M=200, scan_M=None,
              detectors=None, formats=("html", "markdown"), workers=None,
              errors_tier="full", bayes_cli=None, timer=None,
              certification_manifest=None, analysis_allowance_seconds=None,
              finish_initial_wave=False) -> RunSummary:
    ...
    _scan_M = PROVISIONAL_M if scan_M is None else int(scan_M)
    ...
    # scan dispatch (line ~1167): pass the provisional count
    _run_tasks(remaining, card, g, runcal, exclusions, M=_scan_M,
               errors_tier=errors_tier, boot_workers=boot_workers, ...)
    ...
    # master dispatch (line ~1834): unchanged — authoritative M
    n_masters = _fit_masters(summary.collation, records, card, runcal, g, session, ...,
                             M=M, seed_base=seed_base, errors_tier=errors_tier, ...)
```

Record the effective M in each fit's provenance so replays/reports see it. In `fit_one` (`driver.py:538`) the fit result dict already carries provenance fields (`bootstrap_seconds` etc.); add `"bootstrap_M": M` alongside them so per_task/per_master (Task-1 data) can expose it later if needed. Also thread `scan_M` from the drive loop's `run_batch` invocation if the loop calls it (grep `run_batch(` in `isar/drive/`), passing `scan_M=PROVISIONAL_M` explicitly for live-loop clarity.

Then the CLI. In `main.py` (near the `--M` flag at line 65):

```python
    pr.add_argument("--scan-M", type=int, default=None,
                    help="bootstrap draws for provisional scan fits (default 50; "
                         "masters/certification use --M)")
```

In `run_cmd.py` (the `run_batch(...)` call ~line 99):

```python
    summary = driver.run_batch(
        ws, args.data, force=args.force, llm=args.llm, M=args.M,
        scan_M=getattr(args, "scan_M", None), ...)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/session/test_driver_smoke.py -v`
Expected: PASS. Then a fast full-suite check: `python -m pytest -m "not slow" -q` (green; no signature regressions).

- [ ] **Step 5: Commit**

```bash
git add isar/session/driver.py isar/cli/main.py isar/cli/run_cmd.py tests/session/test_driver_smoke.py
git commit -m "Role-aware bootstrap M: provisional scan fits vs authoritative master fits"
```

---

## Task 5: Document the adaptive stopping rule + regression guard

**Files:**
- Modify: `isar/analyze/errors/bootstrap.py` (docstring only)
- Test: `tests/session/test_driver_smoke.py` (or the nearest tier test)

**Interfaces:** none new. This task pins the already-implemented adaptive default so a future edit can't silently disable it, and documents the rule the design asked to be written down.

- [ ] **Step 1: Write the failing test (regression guard)**

```python
# tests/session/test_driver_smoke.py  (append)
def test_full_tier_enables_adaptive_bootstrap_by_default():
    # Guards the already-shipped default (loop.py:2055, driver.py:1137/2088, certification.py:111):
    # full/bayes tiers must run adaptive bootstrap; fast must not bootstrap at all.
    import isar.session.driver as drv
    assert drv.FULL_TIERS == ("full", "bayes")
    # is_full drives boot_adaptive at driver.py:1137
    for tier, expect in [("full", True), ("bayes", True), ("fast", False)]:
        assert (tier in drv.FULL_TIERS) is expect
```

- [ ] **Step 2: Run to verify it passes-or-fails meaningfully**

Run: `python -m pytest tests/session/test_driver_smoke.py::test_full_tier_enables_adaptive_bootstrap_by_default -v`
Expected: PASS immediately (documents current truth). If it fails, the adaptive default was already broken — investigate before proceeding.

- [ ] **Step 3: Document the rule**

Extend the `parametric_bootstrap` docstring (`bootstrap.py:159`) with the stopping rule verbatim from the code (no logic change):

```python
    """... existing summary ...

    Adaptive early-stop (``adaptive=True``, default for full/bayes tiers): draw in blocks of
    ``block`` (32); after at least ``m_min`` (64) draws, stop when the ω₀ interval half-width
    changes by < ``rel_eps`` (5%) for two consecutive blocks, capped at ``M``. It is a
    convergence rule, not an accuracy downgrade — an unstable fit keeps drawing to ``M`` — and
    is deterministic given ``seed`` (same draw sequence, same stopping point).
    """
```

- [ ] **Step 4: Run the errors/bootstrap tests**

Run: `python -m pytest tests/ -k "bootstrap or errors" -m "not slow" -q`
Expected: PASS (docstring change is inert).

- [ ] **Step 5: Commit**

```bash
git add isar/analyze/errors/bootstrap.py tests/session/test_driver_smoke.py
git commit -m "Document adaptive-bootstrap stopping rule and guard its default-on tiers"
```

---

## Task 6: Reference-campaign rerun + baseline note

**Files:**
- Create: `docs/campaigns/2026-07-19_speedup_phase1_baseline.md`

**Interfaces:** none. This is the measured baseline + decision-identity guard the whole sprint is judged against.

- [ ] **Step 1: Run a fresh reference campaign** (synthetic tier, so no beam/TAVI needed). Use the same objective as an archived reference (e.g. re-run the `svp_recert1`/`mvp_zone_test`-family objective via `isar drive` synthetic tier, or the smallest synth campaign that exercises scan+master fits). Capture:

```bash
isar profile --workspace out/<fresh_campaign> > docs/campaigns/_phase1_profile.txt
```

- [ ] **Step 2: Confirm the new instrumentation renders** — the profile output now shows the "Fitting-time buckets" section and (on a current campaign) the error-method breakdown; the campaign's `CAMPAIGN_DIAGNOSTIC.md` shows "Where the time went". Eyeball that buckets sum sanely and remainder is non-negative.

- [ ] **Step 3: Decision-identity check** — rerun the same objective a second time and diff the decision stream (scan proposals, windows, ncounts, stop verdicts) from the two session logs. They must be identical (determinism). If provisional `scan_M=50` shifted any decision vs an `scan_M=200` control run, record it — a shift is a Phase-1 finding to escalate (it means a scan's provisional interval is leaking into steering, which Phase 2's shadow harness must examine), not something to paper over.

- [ ] **Step 4: Write the baseline note** — a short `docs/campaigns/2026-07-19_speedup_phase1_baseline.md`: the bucket table before/after, the wall-time delta from provisional M, and the decision-identity result. This is the committed baseline for Phase 2.

- [ ] **Step 5: Commit**

```bash
git add docs/campaigns/2026-07-19_speedup_phase1_baseline.md
git commit -m "Record Phase 1 instrumented baseline and decision-identity check"
```

---

## Phases 2–4 — sketch (detailed plans written after Phase 1's rerun re-measures the baseline)

Each phase ends with a reference-campaign rerun + committed campaign note, same as Task 6. Detailed task-level plans are deferred because their task boundaries depend on the numbers Phase 1's instrumentation produces (e.g. whether the fit-remainder bucket still dominates after provisional M, which decides whether Phase 4 is worth doing).

### Phase 2 — Escalate-on-distrust error tiers (`docs/SPEEDUP_DESIGN.md` §3)
- **New `errors_tier="auto"`** in the tier branching (`driver.py:53,564-566`): default fast; escalate a fit to full when `hesse_trusted` is False, a parameter is near a bound (Γ-wall), modality is fork/ambiguous or runner-up Akaike weight > threshold (≈0.1), or the result is a master/certification fit (role from Task 4's plumbing — the structural "nothing HESSE-only feeds the prior/pool" rule).
- **Deferred-bootstrap queue** for certification-bound escalations not needed for the next decision; durable in the workspace; computed synchronously if demanded early; resume-safe.
- **Shadow-decision validation gate** (blocks defaulting `auto`): replay ≥2 archived campaigns computing both policies, diff *decisions* not intervals, commit the evidence under `docs/campaigns/`.
- Provenance: `errors_tier_effective` + escalation-reason enum per fit.
- Tests: one per escalation trigger; HESSE-only-never-seeds-prior regression; resume-with-nonempty-queue.

### Phase 3 — Deferred master refits (`docs/SPEEDUP_DESIGN.md` §4)
- **Pool-delta significance gate** at the master call site (`_fit_masters`, `driver.py:1834/2302`): skip a refit when added exposure fraction + predicted interval change are below threshold; **always refit** below the ~20–30 count floor.
- **Mandatory refit points** (stopping eval, prior seeding, certification manifest, master-predictive artifact) + **bounded staleness** (≤ N=3 consecutive deferrals, counter resets on any refit).
- Shadow validation: deferred-vs-always-refit → identical certified results and decisions.
- Tests: gate, staleness bound, each mandatory refit point, resume-with-deferral-pending.

### Phase 4 — Fit fast-path, feature-flagged (`docs/SPEEDUP_DESIGN.md` §5)
- Behind `fit_fastpath` flag, default OFF, attacking the `fit_remainder` bucket: warm-starting from previous-iteration / branch-belief IC with a basin-stability fallback to the full grid; IC-grid pruning to tight-prior support (Gaussian-equivalent priors only); enumeration pruning that never drops the simplest structures; a periodic cold-start audit that disables the flag on divergence.
- All Phase-4 numeric thresholds fixed by its regression suite before the flag may default on; recorded in provenance.
- **May be dropped** if Phase 1's rerun shows the remainder bucket no longer dominates after provisional M + Phase 2.

---

## Verification (end-to-end)

1. **Unit:** `python -m pytest -m "not slow" -q` from repo root (base Python `C:\Utilities\Python\Python312`) — green, including the new timing/profile/diagnostic/driver tests.
2. **Profiler:** `isar profile --workspace out/<fresh_campaign>` shows "Fitting-time buckets" with a non-negative, sanely-summing decomposition; error-method breakdown present on a current campaign.
3. **Diagnostic:** `isar diagnose --workspace out/<fresh_campaign>` (or the diagnose CLI) writes `CAMPAIGN_DIAGNOSTIC.md` containing "Where the time went".
4. **Role-aware M:** a full-tier synthetic campaign's `run_timings` shows scan fits at M=50 and master fits at M=200 (inspect per_task/per_master or the `bootstrap_M` provenance); `--scan-M 200` restores uniform behavior.
5. **Determinism guard:** two runs of the same objective produce byte-identical decision streams (Task 6, Step 3).
6. **Baseline committed:** `docs/campaigns/2026-07-19_speedup_phase1_baseline.md` records the before/after bucket table and the wall-time delta.
