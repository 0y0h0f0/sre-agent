# Agent Run LLM / Web Search Speedup Execution Cards

Status: reviewed execution breakdown, ready for one-card-at-a-time implementation.
Source plan: `plans/12-latency/agent-run-llm-web-search-speedup-plan.md`.
Review: neutral sub-agent `Euclid` returned `CONDITIONAL PASS` with no blocking issues. Required additions are incorporated below.
Last updated: 2026-06-22

## How Agents Should Use These Cards

- Execute one card per PR unless the user explicitly asks to combine cards.
- Read the source plan and referenced docs before editing code.
- Preserve defaults: FakeLLM or disabled LLM, fixture executor, M9/Web disabled.
- Do not add real LLM/Web/Kubernetes/DB/cloud calls to CI.
- Do not treat provider prompt cache, app/tool cache, and segment keys as the same metric.
- Keep every new external call behind feature flag, M9 gate where applicable, timeout, redaction, metric/audit, degraded fallback, and secret leakage tests.
- If a card requires a DB schema change, stop and propose a revised card with migration, test, and rollback notes first.
- Do not continue to the next card if the current card changes public API or report schema without explicit approval.

## Global Done Criteria

For every card:

- Focused unit/integration tests pass.
- Default config remains offline and deterministic.
- No metric label contains prompt, raw query, URL path, secret, internal domain, customer identifier, or high-cardinality payload.
- Evidence IDs and runbook chunk IDs remain traceable where touched.
- Docs are updated when behavior, config, metrics, or test expectations change.
- Rollback is either a feature flag/config switch or a localized revert.

## Cross-Card Acceptance Rules

- Provider cache status is tri-state everywhere: `hit`, `miss`, `unknown`.
- `unknown` is excluded from miss-rate denominators and must never be silently persisted as `miss`.
- LLM metrics must have exactly one authoritative emission path per call, or tests must prove no duplicate token/duration/cache observations.
- Web result URLs may be retained only as validated traceability fields; URL path must not appear in labels, cache keys, logs, audit summaries, or high-cardinality metrics.
- Any new Web cache must specify backend, TTL, key fields, value fields, and rollback path before implementation.
- Agent-run Web context, if ever added, must introduce an explicit non-draft purpose, remain under `M9_EXTENSIONS_ENABLED` plus `RUNBOOK_WEB_SEARCH_ENABLED` plus its own default-off gate, and remain context-only.
- Parallel diagnosis must aggregate specialist outputs and metadata without shared `last_metadata` races or shared DB sessions.
- Default FakeLLM/fixture/offline behavior must be asserted in every card that touches provider, Web, report, or profile routing paths.

## LAT-01: Parse Safe LLM Usage Metadata

Phase: 0.
Dependency: none.
Parallelization: can run before all other implementation cards.

Owner files:

- `packages/agent/llm/openai_adapter.py`
- `tests/unit/test_llm_providers.py`
- Docs if semantics change: `docs/02-agent/llm-and-prompts.md`

Goal: parse provider usage metadata needed for latency and cache observability.

Scope:

- Extract safe fields from OpenAI-compatible responses:
  - prompt tokens
  - completion tokens
  - total tokens
  - cached prompt tokens when explicit provider usage details are present
  - reasoning token fields when present
  - service tier
  - finish reason
  - call duration
- Add a normalized provider cache status field: `hit`, `miss`, or `unknown`.
- Prefer explicit usage detail over `finish_reason == "cache_hit"`.
- Providers without explicit cache metadata must report `provider_cache_status="unknown"`.
- Preserve current request behavior and existing tests.

Out of scope:

- Worker aggregation.
- DB schema changes.
- New provider implementation.

Tests:

- Mock response with `prompt_tokens_details.cached_tokens > 0` records `hit` and cached token count.
- Mock response with explicit zero cached tokens and cache detail present records `miss`.
- Mock response with no cache detail records `unknown`.
- Reasoning token detail and `service_tier` are recorded only as safe metadata.

Acceptance:

- Existing provider tests pass.
- No raw prompt, raw completion, or response body enters metadata.
- Unknown cache status is distinguishable from miss.

## LAT-02: Whitelist LLM Call Audit Metadata

Phase: 0.
Dependency: LAT-01 recommended.
Parallelization: can run after LAT-01 or independently if it does not assume new fields.

Owner files:

- `packages/agent/llm/reasoning.py`
- `tests/unit/test_reasoning_layering.py`
- Docs if semantics change: `docs/00-overview/llm-prompt-fakellm-provider-boundaries-deep-dive.md`

Goal: ensure `llm_calls` stores only approved fields and never stores raw reasoning.

Scope:

- Add an explicit allowlist for metadata recorded by `record_llm_call()`.
- Allow latency/token/cache fields from LAT-01.
- Continue stripping `reasoning_summary` and any raw reasoning fields.
- Keep record shape backward-compatible for existing worker aggregation.

Out of scope:

- Prometheus metric definitions.
- Worker aggregation logic.

Tests:

- Allowed fields survive.
- Unknown metadata fields are dropped.
- `reasoning_summary` and raw reasoning variants are dropped.
- Existing `llm_calls` tests remain compatible.

Acceptance:

- No prompt, raw completion text, raw query, or raw reasoning can be recorded through `record_llm_call()`.
- Empty safe metadata is ignored.

## LAT-03: Add Low-Cardinality LLM Runtime Metrics

Phase: 0.
Dependency: LAT-01 and LAT-02.
Parallelization: after LAT-01/LAT-02.

Owner files:

- `packages/common/metrics.py`
- `packages/agent/llm/openai_adapter.py` if adapter emission changes
- `tests/unit/test_llm_providers.py`
- Docs if metrics change: `docs/02-agent/llm-and-prompts.md`, `docs/07-testing/testing-strategy.md`

Goal: emit useful LLM latency/cache/token metrics without sensitive labels.

Scope:

- Add or adjust Prometheus metrics for call duration, prompt tokens, completion tokens, cached prompt tokens, and provider cache status.
- Labels may include only sanitized low-cardinality `provider`, `model`, and optionally `node` if emitted with a known node name outside the adapter.
- Do not add prompt, query, URL, service, incident, or customer labels.
- Choose one authoritative emission path per LLM call. If both adapter-level metrics and shared collector helpers remain, tests must prove they do not double-count.
- Keep compatibility with existing metrics where possible.

Out of scope:

- Engineering metrics API aggregation.
- Web search metrics.

Tests:

- Metric increments/observations are emitted for mock provider calls.
- Labels are sanitized and do not include prompt/query content.
- Unknown cache status does not increment miss counters.
- Token/duration/cache observations are not duplicated for one call.

Acceptance:

- Metrics do not introduce high-cardinality or sensitive labels.
- Existing metrics consumers are not broken.
- Exactly one emission path is documented in code or tested.

## LAT-04: Aggregate LLM Metrics In Worker Without Cache Conflation

Phase: 0.
Dependency: LAT-01, LAT-02.
Parallelization: after metadata shape is stable.

Owner files:

- `apps/worker/tasks.py`
- `tests/integration/test_worker_task.py`
- `tests/integration/test_engineering_metrics_api.py` if API output changes
- Docs if aggregation semantics change: `docs/05-memory/memory-cache-compression.md`

Goal: summarize LLM latency/token/cache data at run level while keeping provider and app/tool cache separate.

Scope:

- Update `_populate_run_metrics()` to aggregate LLM total duration, prompt/completion/cached token totals, and provider cache hit/miss/unknown counts from `state["llm_calls"]`.
- Preserve existing app/tool cache counters from `RequestLocalToolCache`.
- Current DB model has provider hit/miss fields but no unknown field. Keep unknown in run state/debug JSON or Prometheus-only metrics unless a separate migration card is approved.
- Do not fold unknown into miss.

Out of scope:

- DB migration.
- Frontend UI changes.

Tests:

- Aggregation counts hit/miss/unknown separately.
- Unknown is excluded from miss-rate denominator.
- App/tool cache counts are unaffected by provider cache counts.
- FakeLLM run remains offline and deterministic.

Acceptance:

- Run metrics do not imply provider cache hit when provider data is unavailable.
- No DB schema change is introduced by this card.

## LAT-05: Add Web Search Observability For Existing Fake/Disabled Providers

Phase: 0.
Dependency: none.
Parallelization: can run with LAT-01/LAT-02 if file conflicts are managed.

Owner files:

- `packages/rag/runbook_web_context.py`
- `packages/common/metrics.py`
- `tests/unit/test_web_search_safety.py`
- Docs if metrics change: `docs/m9-rollout.md`, `docs/04-rag/runbook-rag.md`

Goal: measure existing Web context path latency and outcomes without adding a real provider.

Scope:

- Record safe provider latency, result count, blocked count, cache hit placeholder if applicable, and query redaction count.
- Keep provider support limited to current `disabled` and `fake` providers.
- Ensure metric reason labels use fixed reason codes.

Out of scope:

- Real Web search provider.
- Web cache implementation.
- Agent-run Web context integration.

Tests:

- Disabled provider records config/degraded metrics without external call.
- Fake provider records latency/result count deterministically.
- Secret-bearing query does not appear in logs, metrics labels, result payload, or cache keys.
- `query_redacted` and any diagnostic text do not reintroduce secrets or internal topology.

Acceptance:

- Default production still performs no provider call.
- No raw query or URL path appears in metrics labels.

## LAT-06: Stabilize Existing Prompt Prefixes And Segment Keys

Phase: 1.
Dependency: LAT-01 through LAT-04 recommended so cache effects can be measured.
Parallelization: should be sequential with LAT-07 because both touch prompt/context behavior.

Owner files:

- `packages/memory/context_builder.py`
- `packages/agent/prompts.py` only if a missing static key/version must be added
- `tests/unit/test_memory.py`
- Docs if behavior changes: `docs/05-memory/memory-cache-compression.md`

Goal: verify and tighten existing stable prompt-prefix behavior without a broad prompt rewrite.

Scope:

- Confirm current `ContextBuilder` keeps system/schema content in stable system messages and dynamic incident/evidence content in user messages.
- Add missing versioned segment keys for static prompt/schema/runbook chunks where needed.
- Add a test helper that computes stable prefix hash from built messages.
- Decide and document message-boundary handling before implementation:
  - keep current `generate_json(prompt, schema)` shape and document exact stable prefix, or
  - stop and split a new card for message-based JSON calls.
- Avoid broad prompt rewrites unless tests prove the current structure cannot support stable prefixes.

Out of scope:

- Actual Redis/application prompt segment cache hit path.
- Report compression.
- Rewriting all prompts.

Tests:

- Same static content produces the same stable prefix hash.
- Changed alert/evidence data leaves static prefix hash unchanged.
- Changed schema/prompt version intentionally changes static prefix hash.

Acceptance:

- No raw logs are introduced into prompts.
- Existing graph flow tests still pass.
- Static/dynamic message boundaries are explicitly documented.

## LAT-07: Compress Report Generation Inputs

Phase: 1.
Dependency: LAT-06 recommended.
Parallelization: sequential with LAT-06 if both touch report prompt behavior.

Owner files:

- `packages/agent/nodes/generate_report.py`
- `packages/memory/compressor.py`
- `tests/unit/test_agent_nodes.py`
- `tests/unit/test_memory.py`
- Docs if behavior changes: `docs/05-memory/memory-cache-compression.md`

Goal: prevent report generation from carrying large raw evidence while preserving traceability.

Scope:

- Add deterministic compression for report run trajectory and evidence summaries.
- Preserve retained and omitted evidence IDs.
- Update `generate_report` prompt to use compressed summaries.
- Keep fallback report behavior.

Out of scope:

- Public report schema changes.
- LLM profile routing.

Tests:

- Large evidence inputs are compressed before report prompt construction.
- Report/fallback still includes evidence IDs and chunk IDs.
- Raw logs do not appear in report prompt snapshots.

Acceptance:

- Existing report API shape remains compatible.
- Evidence traceability survives compression.

## LAT-08: Add LLM Profile Configuration Plumbing

Phase: 2.
Dependency: LAT-01/LAT-02.
Parallelization: can run before LAT-09, but settings conflicts should be avoided.

Owner files:

- `packages/common/settings.py`
- `packages/agent/llm/factory.py`
- optional new `packages/agent/llm/profiles.py`
- `tests/unit/test_llm_providers.py`
- `tests/unit/test_settings_production_defaults.py`
- Docs if config changes: `docs/11-reference/configuration.md`, `docs/02-agent/llm-and-prompts.md`

Goal: add profile configuration without changing default behavior.

Scope:

- Define profiles such as `fast_json`, `diagnose_reasoning`, and `report`.
- Add optional settings for node/model/max-token overrides.
- Ensure defaults preserve current provider/model/max-token behavior.
- Profile routing may change model/options only; it must not alter provider allow rules, external-provider gates, or redaction wrapper behavior.

Out of scope:

- Routing nodes to profiles.
- Changing prompts or schemas.

Tests:

- Defaults match current behavior.
- Overrides parse correctly.
- Production safety defaults do not enable real providers or Web.

Acceptance:

- No real provider becomes enabled by default.
- FakeLLM/disabled provider paths still work.

## LAT-09: Route Nodes To Profiles And Conditional Reasoning

Phase: 2.
Dependency: LAT-08.
Parallelization: sequential after LAT-08.

Owner files:

- `packages/agent/nodes/diagnose.py`
- `packages/agent/nodes/plan_actions.py`
- `packages/agent/nodes/generate_report.py`
- `packages/agent/llm/reasoning.py` if helper logic changes
- `tests/unit/test_reasoning_layering.py`
- `tests/unit/test_agent_nodes.py`
- Docs if behavior changes: `docs/02-agent/llm-and-prompts.md`

Goal: use fast paths for simple nodes and reserve deeper reasoning for justified diagnosis cases.

Scope:

- Route `plan_actions` to `fast_json` when configured, with deterministic fallback unchanged.
- Route `generate_report` to a report profile when configured.
- Gate deeper diagnosis reasoning on existing config plus evidence conflict, P0 severity, cascade suspicion, missing evidence, or explicit operator override.
- Profile routing may change model/options only; it must not loosen provider allow rules or external-provider gates.

Out of scope:

- Compact schema changes.
- Multi-perspective parallelization.

Tests:

- Default node behavior unchanged.
- `plan_actions` can use fast profile independently of `diagnose`.
- Conditional reasoning enables only for intended cases.
- `reasoning_summary` never persists.

Acceptance:

- Safety and guardrails are unaffected.
- Real provider calls still require explicit allowed configuration.

## LAT-10: Track JSON Repair And Fallback Reasons

Phase: 3.
Dependency: LAT-01/LAT-02.
Parallelization: can run before LAT-11.

Owner files:

- `packages/agent/nodes/diagnose.py`
- `packages/agent/nodes/plan_actions.py`
- `packages/agent/nodes/generate_report.py`
- `packages/common/metrics.py`
- `tests/unit/test_agent_nodes.py`
- Docs if metrics change: `docs/02-agent/llm-and-prompts.md`

Goal: make repair/fallback overhead visible and safe.

Scope:

- Record JSON repair attempt count and fallback reason as safe metadata or metrics.
- Use fixed fallback reason codes only.
- Never use raw exception strings, prompt text, or model output as metric labels.
- Preserve existing repair/fallback behavior.

Out of scope:

- Changing schemas.
- Changing fallback decisions.

Tests:

- Forced malformed JSON increments repair count.
- Forced repair failure records deterministic fallback reason code.
- Raw exception text and prompt text do not enter metrics labels.

Acceptance:

- Existing fallback behavior remains stable.
- Repair/fallback overhead is observable.

## LAT-11: Compact Internal LLM Output Schemas

Phase: 3.
Dependency: LAT-10 recommended.
Parallelization: sequential with LAT-12 if touching report behavior.

Owner files:

- `packages/agent/schemas.py`
- `packages/agent/prompts.py`
- `packages/agent/nodes/diagnose.py`
- `tests/unit/test_agent_nodes.py`
- `tests/integration/test_graph_flow.py`
- Docs if schema semantics change: `docs/02-agent/llm-and-prompts.md`

Goal: reduce generated tokens while preserving external contracts and traceability.

Scope:

- Introduce compact internal schemas only where they reduce LLM output without losing evidence IDs/chunk IDs.
- Map compact internal output back to current state/public structures.
- Keep `rank_hypotheses` deterministic; do not add a new LLM call.

Out of scope:

- API/report response schema changes.
- Guardrail/action schema changes unless explicitly required and reviewed.

Tests:

- Compact schema parses valid outputs.
- Mapping preserves evidence IDs, chunk IDs, confidence, hypothesis text, and root cause summary.
- Graph flow and smoke eval remain valid.

Acceptance:

- Public API/report compatibility is preserved.
- JSON validity and high-risk block eval gates remain 100%.

## LAT-12: Add Opt-In Deterministic Report Mode

Phase: 3.
Dependency: LAT-07 and LAT-08 recommended.
Parallelization: after LAT-07/LAT-08.

Owner files:

- `packages/agent/nodes/generate_report.py`
- `packages/common/settings.py` if adding a mode/gate
- `tests/unit/test_agent_nodes.py`
- `tests/integration/test_graph_flow.py`
- Docs if config changes: `docs/11-reference/configuration.md`, `docs/02-agent/workflow.md`

Goal: provide a configurable way to avoid long report-generation LLM calls while preserving current defaults.

Chosen default behavior:

- Preserve current report-generation behavior by default.
- Add an explicit deterministic report mode/gate that is opt-in and default-off.
- Do not silently change whether default FakeLLM or real-provider paths call `generate_report` LLM in this card.

Scope:

- Add deterministic report mode only behind explicit config.
- Keep report versioning and report schema unchanged.
- Ensure FakeLLM/disabled paths remain offline and deterministic regardless of mode.

Out of scope:

- Report API changes.
- LLM provider changes.
- Changing the default report mode.

Tests:

- Default behavior matches current report LLM invocation behavior.
- Explicit deterministic report mode avoids report LLM call.
- Explicit report LLM path still works with a mock provider.
- Report versions are still appended, not overwritten.

Acceptance:

- Default path remains compatible.
- Deterministic mode is available as a safe latency option.
- Report evidence references are preserved.

## LAT-13: Implement Safe Web Context Cache And Offline-First Lookup

Phase: 4.
Dependency: LAT-05.
Parallelization: sequential with any future Web provider card.

Owner files:

- `packages/rag/runbook_web_context.py`
- `packages/rag/web_search_provider.py` if cache status result fields are needed
- `tests/unit/test_web_search_safety.py`
- `tests/integration/test_runbook_web_context_draft.py`
- Docs if cache behavior changes: `docs/04-rag/runbook-rag.md`, `docs/11-reference/configuration.md`

Goal: avoid repeated Web provider calls for equivalent safe queries.

Backend decision required before implementation:

- Preferred backend: existing Redis via `settings.redis_url`, using `memory://` style tests if supported by local test settings.
- TTL: `settings.runbook_web_search_cache_ttl_seconds`.
- If no safe existing Redis/cache helper can be used without broad new infrastructure, stop and split a dedicated cache-backend card with config, migration/no-migration decision, tests, and rollback notes.
- Do not add a persistent DB table in this card.

Scope:

- Add redacted query normalization and safe cache key construction.
- Include provider, allowlist policy hash, blocklist policy hash, redacted query hash, recency bucket, and search budget/context size in the key.
- Cache values may retain only validated traceability fields allowed by the M9 contract:
  - title
  - validated source URL
  - validated final URL
  - snippet/excerpt after size limit and redaction
  - content hash
  - provider
  - redaction version
  - retrieved time
- URL paths are allowed inside validated result payloads only for traceability. They must not appear in labels, cache keys, logs, audit summaries, or high-cardinality metrics.
- Cache unavailable behavior must degrade safely or continue without cache; it must not leak secrets.

Out of scope:

- Real Web provider.
- Agent-run Web context integration.
- New persistent DB table unless separately approved.

Tests:

- Equivalent redacted query hits cache.
- Cache hit avoids provider call.
- Cache key contains no raw secret/internal host/path.
- Safety validation still runs for cached records, or cached records store only previously validated safe outputs.
- Redis/cache failure does not leak query or secret data.

Acceptance:

- Production default-off still performs no provider call.
- Cached results preserve required traceability fields safely.
- Backend, TTL, key fields, value fields, and rollback path are documented in code comments or docs.

## LAT-14: Add Agent-Run Web Context Gate Only If Explicitly Requested

Phase: 4 optional.
Dependency: LAT-13.
Parallelization: do not run unless the user explicitly asks to attach Web context to agent runs.

Owner files:

- likely `packages/common/settings.py`
- likely `packages/agent/nodes/retrieve_runbook.py` or context assembly code, only after reading current flow
- `packages/rag/runbook_web_context.py` if adding a new purpose
- tests selected after exact attachment point is chosen
- Docs if added: `docs/02-agent/workflow.md`, `docs/04-rag/runbook-rag.md`, `docs/11-reference/configuration.md`

Goal: if requested later, attach Web context to agent run as optional evidence/context only.

Scope:

- Add explicit default-off gate such as `AGENT_WEB_CONTEXT_ENABLED=false`.
- Gate must require `M9_EXTENSIONS_ENABLED=true` and `RUNBOOK_WEB_SEARCH_ENABLED=true`.
- Introduce an explicit non-draft safe purpose in `RunbookWebContextBuilder`; do not bypass the current `purpose="draft_enrichment"` safety check.
- Attach results only as context/evidence.
- Web context must not influence guardrail authorization, risk level, approval requirement, or executor selection.

Out of scope:

- Real Web provider.
- Any remediation authorization change.

Tests:

- All gates off means no provider call.
- M9 off with agent Web on means no provider call and warning/metric.
- New non-draft purpose is explicitly validated.
- Web context cannot affect action risk level or approval requirement.

Acceptance:

- Default behavior unchanged.
- Web context is traceable and context-only.

## LAT-15: Parallelize Multi-Perspective Specialists Safely

Phase: 5 optional.
Dependency: LAT-01/LAT-02; after other latency cards.
Parallelization: do not run in parallel with other `diagnose.py` changes.

Owner files:

- `packages/agent/nodes/diagnose.py`
- `packages/agent/llm/base.py` and adapters if adding call-local metadata API
- `tests/unit/test_diagnose_multi_perspective.py`
- `tests/unit/test_reasoning_layering.py`
- Docs if behavior changes: `docs/02-agent/workflow.md`, `docs/02-agent/llm-and-prompts.md`

Goal: reduce wall-clock latency of optional multi-perspective diagnosis.

Scope:

- Run metrics/logs/traces specialists concurrently only when `LLM_MULTI_PERSPECTIVE_ENABLED=true` and the new parallel switch is enabled if added.
- Worker threads must not mutate shared `state`.
- Worker threads must not read shared `deps.llm.last_metadata`.
- Use call-local metadata via new `invoke_with_metadata()` / `generate_json_with_metadata()` or isolated adapters.
- Main thread aggregates specialist outputs and records `llm_calls`.
- Keep synthesizer sequential after specialists complete.
- Preserve failure isolation.
- Do not share DB sessions in worker threads.

Out of scope:

- Default single-call diagnosis path.
- Report generation.
- Web context.

Tests:

- One specialist timeout/failure does not block other results.
- Metadata does not cross-contaminate.
- Main thread records `llm_calls`.
- No DB session is shared in threads.
- Default multi-perspective disabled path unchanged.

Acceptance:

- Wall-clock for three mocked specialists is near max delay, not sum delay.
- Raw reasoning does not persist.
- Turning off the switch restores current behavior.

## Recommended Execution Order

1. LAT-01
2. LAT-02
3. LAT-03
4. LAT-04
5. LAT-05
6. LAT-06
7. LAT-07
8. LAT-08
9. LAT-09
10. LAT-10
11. LAT-11
12. LAT-12
13. LAT-13
14. LAT-14 only if explicitly requested
15. LAT-15 optional, last

## Cards That May Be Parallelized

Only parallelize if owners are distinct and the branch owner coordinates file conflicts:

- LAT-01 and LAT-05 can run in parallel.
- LAT-03 and LAT-04 should not both modify `packages/common/metrics.py` without explicit coordination.
- LAT-08 can start while LAT-06/LAT-07 are in review, but LAT-09 must wait for LAT-08.
- LAT-14 and LAT-15 should not be parallelized with adjacent cards.

## Stop Conditions

Stop and ask for review if:

- A card requires a DB migration not listed here.
- A card would enable real external LLM or real Web search by default.
- A card changes public API/report schema.
- A card would store raw prompts, raw Web queries, secrets, internal URLs/domains, or URL paths in disallowed locations.
- A card would let Web context influence guardrail authorization or executor choice.
- Focused tests require network access.
