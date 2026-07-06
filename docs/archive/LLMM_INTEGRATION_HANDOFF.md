# Handoff: LLMM ↔ ISAR integration (QC adjudication consumer)

**From:** ISAR build session (the data-reduction pipeline that will *consume* LLMM)
**To:** LLMM maintainer / next LLMM work session
**Date:** 2026-06-26
**Status:** ISAR is ready to build its LLM "adjudication" layer (`isar/reduce/adjudicate.py`, Phase 2 build-order step 7). Before writing the ISAR client I probed LLMM live. LLMM works, but several things block or complicate a clean integration. This doc is the list of what I need and what I hit.

> **RESOLVED (2026-06-28).** The blockers below are cleared and the adjudicator is **built + live-verified**.
> - **P1 (schema enforcement):** local llama.cpp now hard-enforces a forwarded `json_schema`; cloud stays
>   `json_object` so ISAR parse-validate-repairs and holds-for-human on a miss. (LLMM PR #9.)
> - **P3/P5 (local online + adequacy):** the GPU `qwen3p5-9b` is serviceable and **adequate** (zero
>   destructive errors in the bake-off); the CPU `qwen3p5-2b` was ruled out (destructive).
> - **P2/P6 (routing):** ISAR pins one model via `routing={"mode":"pinned","pinned":{"ref":…}}` — no
>   profile (avoids the slow-flagship timeout); `gen_params.max_tokens`, `input.thinking=false`.
> - The full as-built record (contract, components, model evidence, how to run) lives in
>   **`docs/ADJUDICATION.md`**. The detail below is kept as the original probe log.

---

## 1. Context — what ISAR wants from LLMM

ISAR turns raw neutron/x-ray scans into QC'd, provenance-tagged records. A deterministic layer *flags* candidates (cosmic-ray spikes, truncated scans, empty spectra, dead/off detectors, sensor divergence) but never decides or deletes. The next stage is an **LLM adjudicator** that, per flag, returns a structured decision: `mask` / `keep` / `drop_detector` / `hold_for_human`, plus a peak-shape judgment (`whole_peak`/`clipped_peak`/`no_peak`) and a one-line reason. Decisions are written to a machine-readable audit log; a human reviews later. The LLM is **off the critical path** — it never sits in front of a fitted number.

**ISAR will call LLMM directly over HTTP** (production code, not the Prompter MCP — the MCP is just how *I* probed it). The intended client is a thin **stdlib `urllib`** wrapper around `POST /v1/dispatch` — ISAR is deliberately dependency-minimal (numpy/scipy/matplotlib only), so no `requests`/`openai`/`httpx`.

### Workload profile (so LLMM can be tuned for it)
| Property | Value |
|---|---|
| Prompt size | small — ~600–900 input tokens (a compact downsampled view of one flagged scan) |
| Output size | small — ~200–1000 tokens of JSON |
| Shape | **structured JSON out**, schema known in advance |
| Batching | one call per flagged scan; a beamtime batch is **dozens to low-hundreds** of scans |
| Latency tolerance | not interactive — seconds-to-minutes per *batch* is fine; but **~48 s/scan is not** |
| Determinism | low temperature (~0.2), reproducible-ish judgments preferred |
| Deployment modes | **two**: (a) NanoGPT tier for quality; (b) **local models for offline / privacy at the beamline** |

---

## 2. What worked (so we keep it)

- `POST /v1/dispatch` with `{input:{messages, system, structured_output_schema}, generation_params, ...}` → `DispatchResponse{output, structured, model_id, usage, error}`. Clean, plain-JSON, no auth on `127.0.0.1:8766`. This is a good fit for a stdlib client.
- Pinning a specific NanoGPT model worked and was fast & correct. **`nanogpt/openai/gpt-oss-120b`** adjudicated a realistic 3-flag test perfectly (kept a flat-but-off-dispersion analyzer, dropped a railed/stuck channel, kept a truncated scan, judged peak shapes right, didn't touch unflagged detectors) in **~8 s** (p50 7991 ms, 128 tok/s).
- `/v1/connections` and `/v1/models/available` give honest health/latency telemetry that's genuinely useful for picking a model.

---

## 3. Problems / blockers I hit (numbered, with evidence)

### P1 — `structured_output_schema` is not enforced (highest impact)
I passed a JSON-schema (`{decisions:[{target,decision,reason,confidence,shape}], scan_narrative}`). **The model ignored it and invented its own field names.**
- `gpt-oss-120b` returned `{adjudications:[{target,action}], peak_shapes:[...]}` — not my schema, and missing `reason`/`confidence`.
- `granite-4.1-8b` returned `{adjudicated_flags:[{target,adjudication:{severity,reason}}]}` — again its own shape.
- The `structured` field in the response just echoed the raw text; no validation happened.

**What I need:** clarity + ideally enforcement.
- Does LLMM forward `response_format`/json-schema to each backend, or drop it? For which providers/models is it actually enforced vs silently ignored?
- For **local llama.cpp** models, GBNF/json-schema **constrained decoding** is available — is `structured_output_schema` wired to it? If so, local could give *guaranteed* schema conformance that the API models don't.
- Minimum viable: have the response **report whether the schema was honored** (a boolean / the validated object vs null), so ISAR knows when to fall back to lenient parsing.
- If LLMM won't enforce, that's fine — but then ISAR must parse-validate-repair, and I'd want that decision documented so I don't build on a false assumption.

### P2 — default profile routing times out
`health_check(profile=interactive_quality)` live completion → **`timeout: Request exceeded configured ceiling`** at ~21 s. Root cause is visible in `/v1/connections`: that profile's tier-4 primary is **Kimi K2.5 with p50 latency ~48 s** (47985 ms, 0.04 tok/s right now). So the out-of-the-box profile points at a model far too slow for this workload.

**What I need:**
- A profile (or documented recommendation) tuned for **"fast, cheap, structured JSON, low latency"** — something defaulting to a ~8 s model like `gpt-oss-120b`, not a 48 s flagship. I'd rather target a *role/profile* than hardcode a model ref in ISAR, so you can re-point it later.
- What is the request timeout ceiling, and is it per-request configurable from `/v1/dispatch`?

### P3 — local models are offline/cold; the dispatch-to-cold-model contract is unclear
The two models the ISAR owner specifically wants tested are **`local_desktop/qwen3p5-9b`** (Qwen3.5 9B, GPU, llama.cpp, Q4_K_M, ctx 8192) and **`local_desktop/qwen3p5-2b`** (Qwen3.5-2B, CPU, Q8_0). In `/v1/connections` **both show `lifecycle: offline`, `serviceable: false`** right now. (`/health` separately claimed `local_desktop 2/4 serviceable` — the two endpoints disagree; **which is authoritative?**)

**What I need:**
- The contract for dispatching to a cold local model: does `POST /v1/dispatch` **auto-start** it and block until ready, or must ISAR call `/v1/connections/{c}/models/{m}/start` and poll lifecycle first? (I see start/stop/lifecycle endpoints in the OpenAPI.)
- Cold-start + warm p50 latency and tok/s for `qwen3p5-9b` (GPU) and `qwen3p5-2b` (CPU). Offline beamline use is the whole reason local matters, so cold-start cost is load-bearing.
- **Please bring `qwen3p5-9b` and `qwen3p5-2b` online** so I can run the actual adjudication probe against them (see P5).

### P4 — local models aren't programmatically discoverable via the catalog API
- `get_model_catalog(connection_id=local_desktop)` → **HTTP 400 "Connection 'local_desktop' does not expose a provider catalog."**
- `list_available_models` only returns the curated **NanoGPT shortlist (3 models)** — no local models at all.
- I only recovered the local model ids by reading `/v1/connections` directly.

**What I need:** the blessed discovery path for ISAR. Is `/v1/connections` the intended "what can I dispatch to" endpoint (local + api)? Or should ISAR just hardcode pins from a card config? A single documented "list all dispatchable refs" endpoint would let ISAR's pre-beamtime check verify its configured model is actually present.

### P5 — small local models look inadequate, but the real ones are untested
The adjudication task needs genuine reasoning about scan shape and context. Evidence from the probe:
- **`granite-4.1-8b`** (an 8B size-proxy for the local 9B) **failed** — it parroted the input flags back, made **no decisions**, mismatched a scan number, and ignored the peak-shape task. Output was 164 tokens of regurgitation.
- `gpt-oss-120b` (120B) nailed everything.

The actual `local_desktop/qwen3p5-9b` and `qwen3p5-2b` are **untested because they're offline**. This is exactly the question ISAR's design said must be answered with evidence before trusting a local tier. **I need them online to find out whether the local GPU 9B is good enough for offline beamline adjudication, or whether NanoGPT quality is required.**

### P6 — pin vs profile vs routing semantics are muddy for the direct API
Pinning worked, but the response still reported `profile=interactive_quality` even when pinned, and `DispatchRequest` exposes `profile`, `routing` (RoutingSpec), **and** generation params. For a stdlib client I want **one canonical minimal recipe**.

**What I need:** a documented minimal `/v1/dispatch` body that: pins exactly one model, sets temperature + max_tokens deterministically, and passes a structured schema — plus the `RoutingSpec` shape. (The Prompter MCP hides this; production ISAR can't use the MCP.)

---

## 4. Proposed integration contract — please confirm or correct

ISAR plans to add `isar/reduce/llm_client.py`:

```
POST http://127.0.0.1:8766/v1/dispatch
body = {
  "input": {
    "system": "<adjudicator system prompt>",
    "messages": [{"role":"user","content":"<compact view JSON>"}],
    "structured_output_schema": { ...ISAR decision schema... }
  },
  "routing": { ...pin one model... },        # ← need the exact shape
  "generation_params": {"temperature":0.2, "max_tokens":1500},
  "id": "<uuid>",
  "metadata": {"source":"isar-adjudicate"}
}
# read response.structured if present & valid, else lenient-parse response.output;
# validate against ISAR schema; one repair retry; else mark hold_for_human.
```

Confirm: (a) this is the right endpoint/shape for production; (b) the API is stable/versioned enough to build against; (c) no auth needed for localhost; (d) concurrency limits ISAR should respect (saw `nanogpt max_in_flight=5`, `local max_concurrent=1`); (e) whether you'd rather ISAR depend on a shared LLMM client lib than hand-roll urllib.

---

## 5. The concrete asks, prioritized

1. **(P1)** Tell me the real structured-output behavior — enforced where? Wire local llama.cpp grammar/json-schema? Or confirm ISAR must parse-and-repair.
2. **(P3/P5)** Bring `qwen3p5-9b` and `qwen3p5-2b` online and tell me the start/lifecycle dispatch contract + their latencies, so I can probe them.
3. **(P2)** A fast low-latency profile/role for structured QC (default ~8 s model, not the 48 s flagship), so ISAR targets a profile not a hardcoded model.
4. **(P6/P4)** One documented minimal `/v1/dispatch` pin recipe + `RoutingSpec` shape, and the blessed model-discovery endpoint.
5. **(P3)** Resolve the `/health` vs `/v1/connections` serviceability disagreement — which is authoritative for "can I dispatch now?"

Once 1–4 land, ISAR builds the adjudicator against a stable contract and runs the local-vs-NanoGPT quality bake-off the design calls for.

---

## 6. Notes for whoever picks this up on the ISAR side afterward
- Insertion point is settled: `qc.qc_record()` → **adjudicate** → `collate()`. Empty report slots (`ScanSection.narrative`/`.decision`) and the `CardState.corrections` append-only audit already exist. Card gets a new optional `[adjudication]` table (same pattern as `[qc]`).
- The prompt must **embed the exact output schema + a worked example in-prompt** (P1 proved schema-passing alone doesn't constrain the model).
- Scoring harness: synthetic quirks already record ground truth in `scan.truth["quirks"]`, so adjudicator decisions can be scored against injected truth (did it mask exactly the cosmic ray, keep the short scan, etc.).
- Client must be dependency-injectable so unit tests use a fake and never hit LLMM; a single real-LLMM integration test stays gated/optional.

---

## Appendix — reference data captured this session (2026-06-26)

**LLMM:** `http://127.0.0.1:8766`, FastAPI "LLM Manager" v0.1.0, no auth on localhost.

**Dispatchable model refs observed** (from `/v1/connections`):
- `local_desktop/qwen3p5-9b` — Qwen3.5 9B, local_gpu, tier 2, llama.cpp (registry), default `q4_k_m` (also q6_k, q8_0), ctx 8192. **offline.**
- `local_desktop/qwen3p5-2b` — Qwen3.5-2B, local_cpu, tier 1, `q8_0`, ctx 262144, n_gpu_layers 0. **offline.**
- `local_desktop/qwen3p6-27b` — tier 3, local_gpu, `q4_k_m`, ctx 8196. **offline.**
- `local_desktop/qwen-9b-lmstudio` — LM Studio backend. **offline.**
- `nanogpt/openai/gpt-oss-120b` — **reachable, p50 7991 ms, 128 tok/s** (recommended default tier for this workload).
- `nanogpt/moonshotai/kimi-k2.5` — reachable but **p50 47985 ms** (the profile-timeout culprit).
- `nanogpt/ibm-granite/granite-4.1-8b` — reachable, p50 1706 ms, 96 tok/s (fast but failed the task).
- Other reachable NanoGPT: `deepseek/deepseek-v4-pro(:thinking)`, `zai-org/glm-5.2(:thinking)`, `qwen/qwen3-max`, `Qwen/Qwen3.6-35B-A3B`, `moonshotai/kimi-k2.6(:thinking)`, `mistralai/mistral-large-3-675b…`, `meta-llama/llama-4-maverick`, `google/gemma-4-31b-it(:thinking)`, etc.

**Endpoints that exist:** `/health`, `/v1/connections`, `/v1/models/available`, `/v1/dispatch` (POST), `/v1/dispatch/stream`, `/v1/route/plan`, `/v1/route/roster`, `/v1/profiles`, `/v1/registry/*` (incl. model start/stop/launch), `/v1/budget`, `/v1/telemetry/recent`, `/v1/requests/{id}`. Swagger at `/docs`, schema at `/openapi.json`.

**`DispatchRequest`** = `{id, profile?, routing?(RoutingSpec), stream=false, generation_params?(GenerationParams), unsafe_force_extra_body?, queue_override?, depends_on[], input(DispatchInput), metadata(RequestMetadata)}`.
**`DispatchInput`** = `{messages[(MessageParam)], system?, thinking?, chat_template_kwargs?, structured_output_schema?}` (required: `messages`).
**`DispatchResponse`** = `{request_id, model_id, tier, primary_model_id?, resolved_model_id?, resolved_variant_id?, routing_mode?, fallback_level, fallback_reason?, output?, structured?, usage(UsageInfo), telemetry(TelemetryInfo), error?(ErrorDetail)}`.
(`RoutingSpec` / `GenerationParams` field shapes not yet captured — part of ask #4.)
