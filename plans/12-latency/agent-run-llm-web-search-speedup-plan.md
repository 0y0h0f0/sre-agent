# Agent Run LLM / Web Search Speedup Plan

Status: reviewed plan, ready for implementation slicing.
Review: neutral sub-agent `Poincare` returned `CONDITIONAL PASS` with no blocking issues. The required acceptance notes are incorporated in this file.
Last updated: 2026-06-22

## Purpose

Reduce agent run latency where the bottleneck is LLM generation and Web search, without changing the project's safety model.

This plan does not implement a new orchestration framework, does not add new live write paths, and does not make real LLM or real Web search part of CI.

## Existing Context

Current implementation already has these relevant pieces:

- `collect_all_evidence` parallelizes metrics, logs, traces, deployment, Kubernetes and database evidence collection.
- Runtime LLM calls are concentrated in `diagnose`, `plan_actions`, and `generate_report`.
- Optional multi-perspective diagnosis exists, but specialist calls are currently sequential.
- `state["llm_calls"]` and `apps/worker/tasks.py::_populate_run_metrics()` already aggregate part of token/cache metadata.
- M9 Web context has `RunbookWebContextBuilder`, disabled/fake providers, URL safety checks, production allowlist checks, draft-only usage, and metrics.
- `ContextBuilder` creates prompt segment cache keys, but application prompt segment caching is not yet a real hit path.

## Non-Negotiable Boundaries

- Default local, tests, smoke eval, and CI remain FakeLLM or disabled LLM with fixture/fake backends.
- M9/Web capabilities remain default-off. Any future agent-run Web context gate must still be controlled by `M9_EXTENSIONS_ENABLED` and the existing Web sub-feature gate, plus its own explicit default-off switch if introduced.
- LLM output is advisory only. Deterministic guardrails, approvals, executor backend, and verify gates remain authoritative.
- Web search may provide optional context/evidence only. It must not auto-publish runbooks, decide execution permission, or act as the sole basis for high-risk remediation.
- No raw secret, raw prompt, raw query, internal domain, URL path, customer identifier, or high-cardinality payload may enter metrics labels, cache keys, logs, audit records, DB state, or prompts.
- Provider prompt cache, application/tool cache, and prompt segment keys must remain separate concepts.
- Provider cache status is tri-state: `hit`, `miss`, or `unknown`. `unknown` must not be counted as `miss` unless the provider explicitly reports a miss.
- Every phase must be independently testable and independently reversible.

## Phase 0: Latency Baseline And Observability

Goal: identify whether each slow run is dominated by input prefill, output generation, reasoning tokens, JSON repair, report generation, or Web search.

Implementation scope:

- Update `packages/agent/llm/openai_adapter.py` to parse safe usage metadata from OpenAI-compatible responses:
  - `prompt_tokens`
  - `completion_tokens`
  - `total_tokens`
  - `prompt_tokens_details.cached_tokens` when available
  - reasoning token fields when available
  - `service_tier`
  - `finish_reason`
  - call `duration_ms`
- Update `packages/agent/llm/reasoning.py` so `record_llm_call()` uses an explicit safe metadata allowlist and still strips `reasoning_summary`.
- Update `apps/worker/tasks.py::_populate_run_metrics()` to aggregate:
  - total LLM duration
  - per-node token and latency summaries
  - provider prompt cache hit/miss/unknown counts
  - cached prompt token counts
  - existing app/tool cache counters unchanged
- Extend `packages/common/metrics.py` with low-cardinality metrics:
  - LLM call duration by node/model/provider
  - prompt/completion/cached token totals
  - JSON repair count and fallback count
  - Web search duration/result count/cache hit/block count
- Update `packages/rag/runbook_web_context.py` to record provider latency, result count, blocked count, and query redaction count without adding a real provider.

Tests:

- `tests/unit/test_llm_providers.py` for cached token, reasoning token, service tier, and duration metadata parsing.
- `tests/unit/test_reasoning_layering.py` for metadata allowlist and `reasoning_summary` removal.
- `tests/unit/test_web_search_safety.py` for Web search metrics without query secret leakage.
- `tests/integration/test_worker_task.py` or `tests/integration/test_engineering_metrics_api.py` for run metric aggregation and cache-source separation.

Acceptance:

- A FakeLLM run performs no network call and records safe metric fields with provider cache as zero or unknown.
- Mock OpenAI-compatible responses surface cached prompt token counts.
- Provider cache `unknown` is not included in miss-rate denominators.
- No metric label includes prompt text, query text, URL path, raw service topology, secret, or customer data.
- No DB schema change is introduced unless a later implementation PR proves it is necessary and includes a migration/test plan.

## Phase 1: Prompt Cache Friendly Structure And Input Compression

Goal: reduce prefill cost by making stable prompt prefixes actually stable and by keeping large evidence out of prompts.

Implementation scope:

- Update `packages/memory/context_builder.py` to keep stable content before variable content:
  - static system instructions
  - output schema
  - stable safety rules
  - stable node rules
  - variable incident ID, timestamps, evidence summaries, and Web results at the end
- Add versioned segment keys for static prompt, schema, and runbook chunks.
- Update `packages/agent/prompts.py` to split static prompt blocks from node-specific variable blocks.
- Add an explicit message-boundary decision before implementation:
  - either keep current `generate_json(prompt, schema)` shape and document the exact stable prefix,
  - or introduce a message-based JSON call path so static system/schema messages can be reused consistently.
- Update `packages/agent/nodes/generate_report.py` to pass compressed run trajectory and evidence summaries rather than raw large evidence.
- Extend `packages/memory/compressor.py` for report-generation trajectory compression, preserving retained and omitted evidence IDs.

Do not:

- Treat app/tool cache hit rate as provider prompt cache hit rate.
- Put raw logs or raw Web content into prompt input.
- Break external API/report schemas because internal LLM JSON is shortened.

Tests:

- `tests/unit/test_memory.py` for stable prefix hash, segment keys, and evidence ID retention.
- `tests/unit/test_agent_nodes.py` for report generation and fallback preserving evidence references.
- `tests/integration/test_graph_flow.py` for full diagnose-to-report flow.

Acceptance:

- Repeated builds with the same static prompt/schema/runbook content produce the same stable prefix hash.
- Changing alert/evidence data does not change the static prefix hash.
- Report output still references evidence IDs and runbook chunk IDs.
- Raw logs and raw Web payloads are absent from prompt snapshots.

## Phase 2: LLM Node Profiles And Reasoning Effort Routing

Goal: use fast settings for simple structured nodes and reserve deeper reasoning for complex diagnosis cases.

Implementation scope:

- Add `packages/agent/llm/profiles.py` or extend the existing factory/options path with profiles:
  - `fast_json`
  - `diagnose_reasoning`
  - `report`
- Add optional settings with current behavior as default:
  - `LLM_FAST_MODEL`
  - `LLM_REPORT_MODEL`
  - `LLM_NODE_MODEL_OVERRIDES`
  - `LLM_DEFAULT_MAX_TOKENS`
  - `LLM_NODE_MAX_TOKENS`
- Keep all external provider use behind existing allow flags and redaction wrappers.
- Update `diagnose` so deeper reasoning is used only when configured and justified by evidence conflict, P0 severity, cascade suspicion, missing evidence, or explicit operator configuration.
- Update `plan_actions` to allow the `fast_json` profile while preserving deterministic fallback.
- Update `generate_report` to allow a report profile or deterministic report mode, avoiding long free-text generation on default demo paths.

Tests:

- Settings parsing and defaults.
- `tests/unit/test_reasoning_layering.py` for node/profile/effort routing.
- `tests/unit/test_llm_providers.py` for per-call max token and reasoning effort propagation.
- Smoke eval remains FakeLLM and offline.

Acceptance:

- Default configuration behaves like the current implementation.
- `plan_actions` can be moved to a fast profile without changing `diagnose`.
- Real provider calls still require explicit configuration and redaction.
- Raw reasoning and reasoning summaries do not enter state, DB, audit, logs, or prompt cache records.

## Phase 3: Reduce Output Tokens And LLM Round Trips

Goal: reduce decode latency and repair retries.

Implementation scope:

- Use compact internal JSON schemas for LLM outputs where safe, while mapping back to existing public schemas.
- Keep `rank_hypotheses` deterministic; do not add an LLM call there.
- Ensure `diagnose` output contains all fields needed by deterministic ranking and later report generation.
- Record JSON repair count and fallback reason as safe metadata.
- Prefer deterministic reports for FakeLLM, disabled provider, and normal demo paths unless an explicit report-LLM flag is enabled.
- Configure conservative per-node max output tokens.

Tests:

- JSON parser, repair, and fallback tests.
- `tests/integration/test_graph_flow.py` for full graph report generation.
- Smoke eval: JSON validity remains 100%, high-risk action block rate remains 100%.

Acceptance:

- LLM output token P50/P95 can be compared against the Phase 0 baseline.
- JSON repair and fallback counts are visible.
- Evidence ID and runbook chunk ID traceability survives compact internal schemas.
- External API and incident report schemas remain compatible.

## Phase 4: Web Search Gating, Cache, And Offline-First Behavior

Goal: make Web search a bounded, cached, degradable context source instead of an unbounded slow path.

Implementation scope:

- Update `packages/rag/runbook_web_context.py` with:
  - normalized query hashing after redaction
  - max queries per request/run
  - max results
  - timeout and degraded behavior
  - blocked reason accounting
  - result token budget
  - cache/offline-first lookup path
- Cache key must include only safe normalized fields:
  - provider
  - allowed domain policy hash
  - blocked domain policy hash
  - redacted query hash
  - recency bucket
  - search context size or equivalent budget setting
- Cache/storage must not store raw query, raw secret, internal domain, raw URL path, credentials, or customer identifiers.
- Keep `packages/rag/web_search_provider.py` default providers as `disabled` and `fake`.
- Any real provider must be a separate PR with feature flag, M9 gate, timeout, redaction, audit, metric, degraded fallback, and secret leakage tests.
- If Web context is later attached to agent run, add an explicit default-off gate and keep it under M9/Web gate control.

Tests:

- `tests/unit/test_web_search_safety.py` for cache key safety, redaction, allowlist, blocklist, private IP, redirects, response size, timeout, and degraded responses.
- Integration tests proving Web context attaches only as draft/context evidence and not to approved runbooks or executor permission.
- Production default-off tests.

Acceptance:

- Production default-off means no provider call.
- Fake provider remains deterministic.
- Cache hit avoids provider call.
- Every accepted result has source URL, final URL, content hash, provider, redaction version, and retrieval time.
- Web results cannot change deterministic guardrail authorization or executor backend selection.

## Phase 5: Optional Multi-Perspective Diagnosis Parallelization

Goal: reduce specialist wall-clock latency only when `LLM_MULTI_PERSPECTIVE_ENABLED=true`.

Implementation scope:

- Keep this as the last, optional phase.
- Update `packages/agent/nodes/diagnose.py` so metrics/logs/traces specialists can run concurrently.
- Do not read shared `deps.llm.last_metadata` from multiple threads.
- Prefer `invoke_with_metadata()` / `generate_json_with_metadata()` or isolated adapter instances so metadata is returned per call.
- Preserve current behavior: one failed specialist does not fail the main flow, and the synthesizer waits for available specialist outputs.
- Ensure no DB session is shared by worker threads.

Tests:

- One specialist timeout/failure does not block other specialist results.
- Specialist `llm_calls` metadata does not cross-contaminate.
- FakeLLM and adapters are either thread-safe for this path or isolated per concurrent call.
- Default `LLM_MULTI_PERSPECTIVE_ENABLED=false` path is unchanged.

Acceptance:

- Specialist wall-clock time approaches max(single specialist duration), not sum(specialist durations).
- No raw reasoning leakage.
- Metadata is call-local.
- Closing the parallelization switch returns to the current single-call or sequential path.

## Implementation Order

1. Phase 0: observability and safe metadata.
2. Phase 1: stable prompt prefixes and compression.
3. Phase 2: node profiles and reasoning effort routing.
4. Phase 3: output token and round-trip reduction.
5. Phase 4: Web search cache and gating.
6. Phase 5: optional multi-perspective parallelization.

## Review Gate For Each PR

Each implementation PR must report:

- Files changed.
- Tests added or updated.
- Focused tests run.
- Whether FakeLLM/fixture defaults remain intact.
- Whether external calls remain default-off and gated.
- Whether provider/app/tool cache metrics remain separated.
- Rollback switch or rollback procedure.
- Any remaining latency data gap.

## Rollback

- Phase 0: ignore or disable new metrics; no behavior change expected.
- Phase 1: revert prompt version or stable-prefix builder changes.
- Phase 2: clear node profile overrides and return to current provider settings.
- Phase 3: fall back to previous schema/prompt and deterministic report path.
- Phase 4: set `RUNBOOK_WEB_SEARCH_ENABLED=false` and any future agent Web context gate to false.
- Phase 5: disable `LLM_MULTI_PERSPECTIVE_ENABLED` or the new parallelization switch.

## Non-Goals

- No OpenAI Agents SDK migration.
- No replacement of LangGraph or Celery.
- No real Kubernetes, database, cloud, or cache write path.
- No CI dependency on real LLM or real Web search.
- No default-on M9/Web/external provider behavior.
- No Web-search-only remediation decision.
