# LLM QC Adjudication — as-built (Phase 2, build-order step 7)

> **Status:** live
> **Authority:** canonical for the adjudication trust contract and the LLM endpoint setup reference
> **Verified against code revision:** `d0ea1df` (2026-07-25)

**Status:** built, TDD-tested, and live-verified 2026-06-28. This is the as-built record for the LLM
adjudication layer designed in `archive/PHASE_2_DESIGN.md` §7.3 (and DESIGN §0.5). It documents what exists,
the decision contract, where the reasoning is visible, the model-selection evidence (the §7.3-mandated
probe), how to run it, and the known gaps. Read `archive/PHASE_2_DESIGN.md` §7.3 for the *why*; this for the *what*.

---

## 1. Purpose and the trust architecture

The deterministic QC layer (`reduce/qc_points.py`, `reduce/qc_scans.py`) **flags** candidates
(cosmic-ray spikes, stale heads, truncated/empty scans, dead detectors, sensor divergence) but never
decides or deletes. The adjudicator is **stage 2**: per flag, an LLM returns a structured decision, a
peak-shape judgment, a one-line reason, and a per-scan narrative. The human is stage 3.

Three properties make this trustworthy:

- **Non-destructive.** Adjudication writes `record.adjudication` and appends to the append-only
  `card.state.corrections` audit. It **never** alters `mask`/`intensity`; a later §8 correction loop
  applies confirmed corrections under human review. The LLM is off the critical path — never in front
  of a fitted number.
- **Additive, never a replacement.** The deterministic flags stay in the report regardless of what the
  LLM says. When the model misses a flag (it happens — see §7), the flag is still shown, un-hidden.
- **Reasoning is visible.** Every decision carries a `reason` + `confidence`, and every scan a
  `narrative`, rendered in the report and logged in the audit (§5). Trusting the model is conditional
  on being able to read why it decided.

## 2. Decision contract

A target is one of `"scan"`, `"spectrum:<det>"`, or `"point:<det>:<index>"`.

| decision | meaning |
|---|---|
| `mask` | confirm a bad point (e.g. a cosmic ray) |
| `keep` | the data is genuine |
| `drop_detector` | the analyzer is dead / non-physical |
| `hold_for_human` | ambiguous — defer (the safe default) |

`shape` ∈ `whole_peak | clipped_peak | no_peak | not_applicable` (for peak-shape candidates).

The wire schema is `adjudicate.DECISION_SCHEMA`:
`{"decisions":[{"target","decision","shape?","reason","confidence"}], "scan_narrative"}` with strict
enums and `additionalProperties:false`. It is deliberately a GBNF-expressible subset so local
llama.cpp can hard-enforce it (§6).

## 3. Pipeline insertion

Runtime order: `qc.qc_record()` → **`adjudicate.adjudicate_records()`** → report. The adjudicator
consumes the reduced, QC-flagged, not-yet-collated records and only dispatches records that carry a
flag (clean scans cost no call). It mutates records in place and returns them.

> **Auto-wired since the session layer landed.** `isar run --llm` (or `[adjudication] enabled` in
> `card.policy.toml`) turns it on, and `isar/session/driver.py` calls it between routing and
> collation via `_maybe_adjudicate` — the pipeline the deferred `driver/reduce_run.py` was meant to
> provide is now the session driver itself. Adjudication is skipped entirely during master
> certification. Failure is contained by design: `_maybe_adjudicate` catches any exception, records
> `"LLM adjudication skipped (no consult): …"` in the run context, and lets the batch finish — a
> dead LLMM endpoint degrades the run to deterministic QC rather than stalling it.
>
> It remains callable as a library step (`adjudicate_eyeball.main()`) for offline scoring work.

## 4. Components (all under `isar/reduce/`, plus schema/report edits)

| file | role |
|---|---|
| `llm_client.py` | stdlib-`urllib` clients. `DispatchResult`, the injectable `Dispatcher` protocol, `LLMMClient` (one pinned `POST /v1/dispatch`) and `OpenAIClient` (any OpenAI-compatible endpoint, bearer token from `ISAR_LLM_API_KEY`). Transport is injectable so tests never hit the network. No new deps. |
| `compact_view.py` | `compact_view(record)` → the downsampled, LLM-facing picture of a flagged scan (~600–900 tokens): trace + summary + **each flagged point with its neighbours**, so the model can *see* what it judges. `_flagged_records` selects which scans warrant a call. |
| `adjudicate.py` | `DECISION_SCHEMA`, the `SYSTEM` prompt (schema + worked example embedded inline), `_conforms` (dependency-free validator), `_extract`/parse, and `adjudicate_records`. Also `AdjudicationConfig`/`adjudication_config`, `FallbackDispatcher`, `make_client`. |
| `adjudicate_score.py` | scoring vs injected truth: `expected_decisions`, `score_adjudication`, `aggregate`, `bakeoff`, `ScoreCard`. |
| `adjudicate_eyeball.py` | `python -m isar.reduce.adjudicate_eyeball` — writes a real live-adjudicated report to `out/adjudication_eyeball/`. |

Schema/report edits: `schema/scan.py` adds `Decision`/`Adjudication` dataclasses + optional
`ScanRecord.adjudication` (round-trips via the JSON codec); `schema/card.py` adds
`CardPolicy.adjudication: dict`; `report/model.py` fills `ScanSection.narrative`/`.decision`/
`.adjudication`; `report/html.py` + `report/markdown.py` render the per-scan `[adjudication]` table.

## 5. Where the reasoning is visible

Four surfaces, all produced from one run:
- **HTML / Markdown report** — a per-scan narrative line + an `[adjudication]` table
  (target · decision · shape · reason · confidence), rendered right after the deterministic scan-flag
  table.
- **JSON sidecar** (`<workspace>/report/report.json`) — the decisions inline in the report model.
- **`card.state.corrections`** — the append-only, machine-readable audit; each entry is
  `{source:"llm-adjudicate", scan_number, target, decision, shape, reason, confidence, model_id}` —
  the input to the §8 human review.
- **Eyeball** (§10) — `out/adjudication_eyeball/` with all of the above for a live run.

## 6. Robustness — "was the schema honored?"

- **Local llama.cpp hard-enforces** the forwarded schema (constrained decoding). LLMM's adapter also
  validates the parse and returns `structured=null` on a miss.
- **Cloud (NanoGPT) does not enforce** — its connection is `json_object`, so the in-prompt schema +
  worked example is the only lever, and some models ignore it (§7). The adjudicator therefore always:
  prefer `structured` (if it conforms) → else lenient-parse the raw text → validate with `_conforms`
  → **one repair retry** → else synthesize a single `hold_for_human` decision. A flagged scan is
  **never silently dropped**.
- **`FallbackDispatcher`** dispatches the primary pin and, on a transport/dispatch error, retries the
  fallback pin once — so an unlaunched local model falls back to the cloud comparator without changing
  calling code.

## 7. Model selection — the evidence (the §7.3-mandated probe, 2026-06-28)

15 models were probed via the Prompter MCP on two adjudication scenarios scored against known-correct
answers. The discriminating tests: **clipped-vs-dead** (don't destroy real data) and **restraint**
(don't invent flags). The metric that *disqualifies* a model is the **destructive-error rate** (a
`mask`/`drop_detector` on genuine data), not raw accuracy.

| model | role | recall | destructive | notes |
|---|---|---|---|---|
| **`local_desktop/qwen3p5-9b`** (GPU) | **default (offline-first)** | ~0.80 | **0** | schema-honored; correct clipped-vs-dead; ~5 s. Adequate. |
| **`nanogpt/openai/gpt-oss-120b`** | **cloud comparator / fallback** | 1.00 | 0 | best — also decides scan-level reliably; ~6–8 s. |
| `nanogpt/minimax/minimax-m3` | co-equal cloud option | 1.00 | 0 | strongest reasoning. |
| `nanogpt/mistralai/mistral-small-4-119b` | fast cloud option | ~0.9 | 0 | ~2 s; tends to omit the scan-level decision. |
| `nanogpt/google/gemma-4-31b-it` | fast cloud option | ~0.9 | 0 | as above. |
| `nanogpt/deepseek/deepseek-v4-pro` | usable | ~0.9 | 0 | honors the schema — unlike the v3.x / v4-flash line. Slow (~12 s). |
| `local_desktop/qwen3p5-2b` (CPU) | **rejected** | — | **>0** | dropped a real clipped peak — destructive. The local tier is the 9B only. |
| `zai-org/glm-4.7-flash` | **rejected** | — | **>0** | fast but dropped a real clipped peak as "dead". |
| deepseek v3.2 / v4-flash / v3.1-terminus, llama-4-maverick, hermes-4-405b | **rejected** | — | — | **ignore the schema** (return their own shapes) on NanoGPT's json_object. |

Conclusion: the **GPU 9B clears the design's adequacy bar** (zero destructive errors, full schema
adherence) and is the offline default; **gpt-oss-120b** is the proven cloud comparator/fallback. Use
DeepSeek **v4-pro**, not v3.x. The recall gap (9B ~0.80 vs cloud 1.00) is the 9B occasionally omitting
a scan-level decision (e.g. the sensor-divergence `hold`) — the deterministic flag still shows, so the
human catches it; tightening the prompt to always emit a scan-level decision is a tracked improvement.

**Update (2026-07-16): the recommended fallback model — *if* fallback is enabled — is now
`nanogpt/deepseek/deepseek-v4-flash`.** Cloud fallback itself remains off by default (§8); this
names which model to pin when an operator turns it on (`DEFAULT_CLOUD_FALLBACK_MODEL` in
`isar/reduce/adjudicate.py`).
gpt-oss-120b's NanoGPT serving degraded to empty plain-text replies (reasoning-channel issue — the
same failure that moved the drive supervisor to GLM 5.2) with 3–8 s latency swings. v4-flash's
2026-06-28 rejection ("ignores the schema") was measured through a different dispatch path
(json_object); re-probed live through LLMM's `structured_output_schema` with the production
`SYSTEM` contract it scored **6/6 schema-valid, 0 destructive** across the cosmic-ray/dead-detector
and clipped-peak-restraint scenarios at a consistent ~3 s. It is also deliberately a different
model family from the supervisor's GLM 5.2, so one serving outage cannot take down both LLM roles
(council-reviewed choice). The table above is retained as the 2026-06-28 record.

## 8. Configuration

The card `[adjudication]` policy block (read by `adjudication_config(card)`; all optional):

```toml
[adjudication]
enabled      = false                          # opt-in
profile      = "llmm"                         # "llmm" (default) | "openai" (D3)
model_pin    = "local_desktop/qwen3p5-9b"     # offline/privacy-first default
# fallback_pin = "nanogpt/deepseek/deepseek-v4-flash" # optional cloud comparator/fallback (llmm profile)
endpoint     = "http://127.0.0.1:8766"
temperature  = 0.2
max_tokens   = 1500
```

**Two profiles.** `llmm` is the default: a local LLMM dispatch endpoint with a pinned model and,
optionally, a distinct fallback pin. `openai` points at any OpenAI-compatible HTTPS endpoint with
a bearer token, and has no automatic fallback — the LLMM dual-pin concept does not map, so a single
portable model is configured instead (`make_client`, D3). LLMM is the offline default, not a
requirement.

Cloud fallback is off by default. Set `fallback_pin` or a non-empty `ISAR_LLM_FALLBACK_MODEL` only when
the operator explicitly accepts that flagged-scan context may leave the machine; an explicitly empty
`ISAR_LLM_FALLBACK_MODEL` disables a card fallback. Env overrides `ISAR_LLM_ENDPOINT` /
`ISAR_LLM_MODEL` / `ISAR_LLM_PROFILE` configure the primary service. Remote endpoints must be HTTPS;
plain HTTP is limited to literal `127.0.0.0/8` or `::1` addresses; `localhost` is excluded because its
name resolution can be changed. For OpenAI-compatible services, provide the bearer token only through
`ISAR_LLM_API_KEY`; cards may not contain `api_key` or arbitrary `api_key_env` names.

## 9. Scoring vs synthetic truth

`scan.truth["quirks"]` (the synthetic core, §4) records every injected defect, so decisions are scored
against ground truth. `expected_decisions(quirks)` maps each quirk to its acceptable decision(s)
(spike/stale_head→`mask`; dead_detector/empty→`drop_detector`/`hold`; sensor_divergence→`hold`/`keep`;
truncated→`keep`/`hold`). `score_adjudication` yields per-scan recall, the **destructive-error count**
(masks/drops on targets with no injected defect), and schema-adherence; `aggregate`/`bakeoff` roll them
up per model.

## 10. How to run

- **Eyeball (findable artifact):** `python -m isar.reduce.adjudicate_eyeball` → writes a live report +
  audit to `out/adjudication_eyeball/`; prints `recall / destructive / schema_adherence`. Point at the
  cloud model with `ISAR_LLM_MODEL=nanogpt/deepseek/deepseek-v4-flash`.
- **Gated bake-off test:** `tests/real_data/test_adjudicate_bakeoff.py` (marked `slow`; auto-skips when
  LLMM is unreachable) runs local-9B-vs-cloud and asserts the local 9B is non-destructive when
  serviceable.

An LLM endpoint must be configured and reachable. On the default `llmm` profile that means LLMM
running at `http://127.0.0.1:8766` with the model launched; on the `openai` profile it means an
OpenAI-compatible HTTPS endpoint plus `ISAR_LLM_API_KEY` (§8).

## 11. Historical evaluation (as of 2026-07-16)

Dated evidence from the selection and integration work, kept as the record behind §7's model
choices. These are **not** current verification status — the live suite is the authority on what
passes today.

- **Unit tests at the time:** 257 passing, 35 of them adjudication-specific (fake `Dispatcher`, no
  network), 0 regressions.
- **Live bake-off passed:** local 9B recall 0.80 / destructive 0 / schema 1.00; gpt-oss-120b 1.00 / 0 /
  1.00.
- New `ScanRecord.adjudication` defaults to `None`, so every existing record/test is unaffected.

## 12. Known gaps / next

- ~~**No end-to-end driver yet**~~ — closed. `isar run --llm` threads the setting through
  `isar/cli/run_cmd.py` into `isar/session/driver.py`, which invokes the adjudicator in the batch
  pipeline. The originally planned standalone `driver/reduce_run.py` was never needed.
- **9B scan-level recall** — prompt nudge to always emit a scan-level decision would close the 0.80→1.0
  gap.
- **Local serviceability** — the local pin needs the model *launched*. A cloud fallback is an explicit
  operator choice, not an unattended default. (Probe timeouts during selection were just the
  un-launched models.)
