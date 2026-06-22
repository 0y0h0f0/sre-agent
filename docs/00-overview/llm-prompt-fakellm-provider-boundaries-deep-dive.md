# LLM、Prompt、FakeLLM 与 Provider 边界技术深挖

**最后更新：** 2026-06-23

本文从当前代码路径解释 Agent 如何调用 LLM、如何保持 FakeLLM 确定性、真实 provider 什么时候允许出网、prompt 如何保持可追踪和脱敏，以及 M9 LLM 能力为什么只能生成待审草稿。它补充 [LLM 与提示词](../02-agent/llm-and-prompts.md)、[评测体系](../09-evals/evaluation.md)、[Runbook RAG](../04-rag/runbook-rag.md)、[记忆、缓存与压缩](../05-memory/memory-cache-compression.md)、[Runbook 草稿、版本与 Amendment 生命周期技术深挖](runbook-draft-version-amendment-lifecycle-deep-dive.md) 和 [M9 发布计划](../m9-rollout.md)。

## 阅读目标

读完本文应能回答：

- worker 和 eval harness 如何构造当前 LLM provider。
- `fake`、`disabled`、`vllm`、`openai`、`deepseek`、`anthropic` 的边界差异。
- FakeLLM 覆盖哪些告警，未知告警如何回退。
- 哪些 Agent 节点会调用 LLM，失败时如何修复 JSON 或 deterministic fallback。
- `llm_calls`、token usage、provider cache 计数和 app/tool cache 计数如何区分。
- reasoning flag 和 multi-perspective 诊断如何影响调用，但不保存 raw reasoning。
- M9 runbook draft 和 incident diff 为什么只能进入 review queue。
- 修改 prompt、provider 或 eval case 时需要同步哪些测试和文档。

## 代码入口

| 主题 | 入口 |
|------|------|
| provider 工厂 | `packages/agent/llm/factory.py` |
| provider 协议和 JSON parser | `packages/agent/llm/base.py` |
| deterministic fake adapter | `packages/agent/llm/fake_adapter.py`、`packages/agent/fake_llm.py` |
| disabled adapter | `packages/agent/llm/disabled_adapter.py` |
| OpenAI-compatible adapter | `packages/agent/llm/openai_adapter.py` |
| Anthropic adapter | `packages/agent/llm/anthropic_adapter.py` |
| cloud redaction wrapper | `packages/agent/llm/redacting_adapter.py` |
| reasoning metadata | `packages/agent/llm/reasoning.py` |
| deterministic rules fallback | `packages/agent/rules_fallback.py` |
| runtime prompts | `packages/agent/prompts.py` |
| diagnosis node | `packages/agent/nodes/diagnose.py` |
| action planning node | `packages/agent/nodes/plan_actions.py` |
| report node | `packages/agent/nodes/generate_report.py` |
| worker LLM construction | `apps/worker/tasks.py` 的 `_build_deps()` |
| eval harness settings | `packages/evals/datasets/harness.py` |
| M9 runbook generation | `packages/rag/llm_runbook_generator.py` |
| M9 runbook prompt builder | `packages/rag/runbook_prompt_builder.py` |
| M9 incident diff | `packages/rag/incident_diff.py` |
| runbook service persistence | `apps/api/services/runbook_service.py` |

## 当前 LLM 模型

Agent 对 LLM 的依赖是同步 `LLMProvider` 协议：

```text
LLMProvider
  -> invoke(messages, thinking=False)
  -> generate_json(prompt, output_schema, thinking=False)
  -> invoke_with_metadata(messages, thinking=False)
  -> generate_json_with_metadata(prompt, output_schema, thinking=False)
```

普通节点仍使用 `invoke()` / `generate_json()` 并通过 adapter 的 `last_metadata` 兼容字段读取调用元数据；并发 specialist 使用 `*_with_metadata()` 返回值作为 call-local metadata，不读取或更新共享 `last_metadata`。节点只依赖这个协议，不直接创建 HTTP client、读取 API key 或选择 provider。provider 在 worker 依赖构造时创建：

```text
apps/worker/tasks.py::_build_deps()
  -> settings = get_settings()
  -> llm = build_llm(settings)
  -> AgentDeps(llm=llm, settings=settings, ...)
```

Eval harness 也通过 `build_llm(settings)` 走同一套 provider factory，但默认 settings 被固定成离线 deterministic 路径。

## Provider Matrix

| `LLM_PROVIDER` | Adapter | 是否出网 | 主要用途 | 额外约束 |
|----------------|---------|----------|----------|----------|
| `fake` | `FakeLLMAdapter` | 否 | 本地 demo、测试、CI smoke eval | 默认本地路径。 |
| `disabled` | `DisabledLLMAdapter` | 否 | 生产未显式配置 provider 时的安全默认 | 委托 FakeLLM deterministic 逻辑，metadata 标记 `provider=disabled`。 |
| `vllm` | `OpenAICompatibleAdapter` | 是，自托管 endpoint | 手动 demo/eval 或本地私有模型 | operator 必须显式配置 endpoint/model/timeout；不受 cloud allow 开关控制。 |
| `openai` | `RedactingLLMAdapter(OpenAICompatibleAdapter)` | 是，云 provider | 手动 full eval 或受控 M9 功能 | 需要 `LLM_EXTERNAL_PROVIDER_ALLOWED=true`。 |
| `deepseek` | `RedactingLLMAdapter(OpenAICompatibleAdapter)` | 是，云 provider | 手动 full eval 或受控 M9 功能 | 需要 `LLM_EXTERNAL_PROVIDER_ALLOWED=true`；thinking 默认会被显式启停。 |
| `anthropic` | `RedactingLLMAdapter(AnthropicAdapter)` | 是，云 provider | 手动 full eval 或受控 M9 功能 | 需要 `LLM_EXTERNAL_PROVIDER_ALLOWED=true` 和 API key。 |

生产环境还有一个 settings 层安全默认：`APP_ENV=production` 且没有显式提供 `llm_provider` 时，`Settings` 会把 provider 改成 `disabled`。如果 operator 显式设置了 `LLM_PROVIDER=fake` 或真实 provider，settings 会尊重该配置；真实 provider 仍受 factory 和 feature gate 约束。

未知 provider 会在 `build_llm()` 中抛 `ValidationAppError`，不会回退到真实默认 provider。

## FakeLLM 确定性边界

FakeLLM 是 CI 和 smoke eval 的稳定基础。它不出网、没有随机性，按 `alert_name` 从 `packages/agent/rules_fallback.py` 读取诊断和动作映射。

当前覆盖 15 类告警：

| 类别 | 说明 |
|------|------|
| `DatabaseConnectionExhaustion` | 数据库连接耗尽。 |
| `High5xxAfterDeploy` | 发布后高 5xx。 |
| `RedisCacheAvalanche` | Redis cache avalanche。 |
| `PodRestartLoop` | Pod restart loop。 |
| `CPUThrottling` | CPU throttling。 |
| `MemoryLeak` | memory leak。 |
| `DiskFull` | disk full。 |
| `CertificateExpiry` | certificate expiry。 |
| `DNSFailure` | DNS failure。 |
| `MessageQueueLag` | message queue lag。 |
| `RateLimitTriggered` | rate limit triggered。 |
| `SlowAPI` | slow API。 |
| `ErrorBudgetBurn` | error budget burn。 |
| `P0SiteOutage` | P0 site outage。 |
| `DownstreamTimeout` | downstream timeout。 |

未知告警 fallback 到 `High5xxAfterDeploy`。这符合当前安全边界：alert ingestion 可以接受任意 alert name，但 deterministic FakeLLM 不为未知故障生成随机诊断。

FakeLLM 还会从 prompt 中提取：

- `evi_` 或 `evd_` 开头的 evidence ID，并回填到 diagnosis/root cause/hypotheses。
- `chk_` 开头的 runbook chunk ID，并回填到 runbook 引用字段。
- `[perspective:metrics|logs|traces|synthesizer]` 标签，用于 multi-perspective deterministic 输出。

因此新增 eval case 或新的 alert class 时，应优先更新 `rules_fallback.py` 和 FakeLLM 相关测试，而不是让 CI 依赖真实 provider。

## 节点调用路径

当前 Agent 运行时主要 LLM 调用点：

| 节点/组件 | 调用方式 | 失败策略 |
|-----------|----------|----------|
| `diagnose` single-call | `generate_json(prompt, CompactDiagnosisOutput)` 后映射为 `DiagnosisOutput` | 失败后 repair prompt，再失败用 rules fallback。 |
| `diagnose_metrics` / `diagnose_logs` / `diagnose_traces` | `generate_json(tagged_prompt, CompactDiagnosisOutput)`；并发路径使用 `generate_json_with_metadata()` | specialist 失败或超时返回空 `DiagnosisOutput`，不阻塞主流程。 |
| `diagnose_synthesize` | `generate_json(tagged_prompt, CompactDiagnosisOutput)` 后映射为 `DiagnosisOutput` | 失败后 repair prompt，再失败回退 single-call，保留成功 specialist 输出。 |
| `plan_actions` | `generate_json(prompt, list[PlannedAction])`，可用 `fast_json` profile 覆盖模型/token | 失败后用 `_ACTIONS_MAP` deterministic fallback。 |
| `generate_report` | `invoke()` + `extract_json()`；可用 `report` profile；`LLM_DETERMINISTIC_REPORT_ENABLED=true` 时跳过 LLM | LLM/JSON 失败后生成 deterministic fallback report。 |
| M9 `LLMRunbookGenerator` | `invoke()` | 失败返回 `degraded`，不持久化 draft。 |
| M9 `IncidentDiffAnalyzer` | `invoke()` + `extract_json()` | 调用失败返回 `degraded`；解析失败合成 low-confidence reviewer note。 |

LLM 不是权限决策者。它输出 root cause、hypotheses、action proposal 或 draft content；动作权限仍由 deterministic guardrail、approval、executor backend 和 verify gate 决定。

## Prompt 文件和版本边界

Agent runtime prompts 在 `packages/agent/prompts.py`：

| Prompt | 用途 |
|--------|------|
| `SYSTEM_PROMPT` | 总规则：只用证据、引用 evidence ID、JSON 输出、禁止 L4。 |
| `DIAGNOSIS_PROMPT_TEMPLATE` | 单次诊断。 |
| specialist prompts | metrics/logs/traces 视角诊断。 |
| `SYNTHESIZER_PROMPT_TEMPLATE` | multi-perspective 综合诊断。 |
| `RANK_PROMPT_TEMPLATE` | 假设排序。 |
| `PLAN_ACTIONS_PROMPT_TEMPLATE` | 动作规划，内嵌 allowed action table。 |
| `REPORT_PROMPT_TEMPLATE` | 报告生成模板。 |
| `SUMMARIZATION_PROMPT` | 摘要模板；`packages/memory` 本身不直接调用 LLM。 |

M9 runbook generation prompt 单独在 `packages/rag/runbook_prompt_builder.py`，带 `prompt_template_id`、`prompt_template_version` 和 `redaction_version`。incident diff prompt 在 `packages/rag/incident_diff.py`，metadata 中记录 `prompt_template_version=m9-9.3-1` 和 generated output hash。

修改 prompt 时必须考虑：

- 输出 schema 是否仍可解析。
- evidence ID、runbook chunk ID、action type、target 是否保持可追踪。
- FakeLLM 是否仍能识别 alert name、perspective tag 和 evidence/chunk ID。
- eval report 中的 `prompt_version` 是否需要更新。
- 真实 provider 的 JSON 修复和 fallback 是否覆盖失败场景。

`diagnose` 当前使用 compact internal schema 降低输出 token：`CompactDiagnosisOutput` 的字段为 `h`、`rc`、`e`、`r`、`m`，内部会立即通过 `diagnosis_output_from_compact()` 映射回 public `DiagnosisOutput`。state、API、报告和排序节点仍只看到完整字段名；compact schema 不改变外部 contract。

## JSON 解析与修复

`packages/agent/llm/base.py` 提供两个共享 helper：

| Helper | 行为 |
|--------|------|
| `extract_json()` | 容忍 Markdown fence 和 JSON 前后文本；失败时尝试抽取第一个对象或数组 span。 |
| `parse_into_schema()` | 将解析结果转成 Pydantic model 或 `list[Model]`。 |

`diagnose` 和 `diagnose_synthesize` 有显式 repair prompt：要求 provider 返回匹配 compact diagnosis schema 的 JSON，并保留 evidence ID。repair 失败才进入 deterministic fallback。

`generate_report` 直接对 `invoke()` 输出做 `extract_json()`；失败时走 fallback report。

JSON repair 和 fallback 都有低基数指标：`agentp_llm_json_repair_attempts_total{node}` 和 `agentp_llm_fallback_total{node,reason}`。`reason` 只能是固定 code，例如 `json_repair_failed`、`llm_generate_failed`、`report_generation_failed` 或 `unknown`，不能放异常文本、prompt、URL、query、customer ID 或 secret。

不要把“模型通常能返回 JSON”当成稳定性保证。新增 LLM 调用点时必须有 schema、解析失败路径和 deterministic 降级策略。

## Reasoning 与 Multi-Perspective

Reasoning 是节点级请求参数，不是持久化原始思考内容。

| 配置 | 默认 | 语义 |
|------|------|------|
| `LLM_REASONING_ENABLED` | `false` | 全局关闭时所有调用都是普通推理。 |
| `LLM_REASONING_NODES` | `diagnose,diagnose_synthesize` | 只有列出的节点会传 `thinking=true`。 |
| `LLM_REASONING_EFFORT` | `medium` | 传给支持 reasoning 的 provider。 |
| `LLM_MULTI_PERSPECTIVE_ENABLED` | `false` | 开启 metrics/logs/traces specialist + synthesizer。 |
| `LLM_MULTI_PERSPECTIVE_PARALLEL_ENABLED` | `false` | 开启后前三个 specialist 并发执行；需要 provider 支持 call-local metadata。 |

`record_llm_call()` 使用显式安全 allowlist 记录 LLM metadata，只允许 provider、model、finish reason、token usage、provider cache 三态、duration、service tier 和 redaction 计数/类型等低风险字段进入 `state["llm_calls"]`。`provider_cache_status` 是权威字段；在 worker 聚合迁移完成前，明确 hit/miss 也会保留 legacy `cache_hit` 布尔值，但 `unknown` 不会被折叠成 miss。raw prompt、raw completion、raw query、raw response、`reasoning_summary`、`reasoning_content`、`thinking` 和未知字段会被丢弃，避免 raw reasoning 或 provider 响应内容进入 state、DB 或 audit。node trace 里只保留 provider/model/token/redaction 等短摘要。

Multi-perspective 失败边界：

- specialist 失败不会中止主流程。
- 并发 specialist 线程只接收 state 摘要，不写共享 state，不使用 DB session，不读取或更新共享 `last_metadata`。
- specialist 超时后主流程继续；已运行线程可能稍后返回，但只能消耗自身 provider 调用资源，不能污染 state/DB/metadata。
- synthesizer 失败会尝试 repair。
- repair 失败后回退 single-call diagnosis，并携带已成功的 specialist 输出。
- 正常 multi-perspective 路径只记录真实 LLM calls：metrics、logs、traces 和 synthesizer；不会额外写入 synthetic top-level `diagnose` 调用。

## Redaction 与 Secret 边界

外部云 provider（`openai`、`deepseek`、`anthropic`）会被 `RedactingLLMAdapter` 包装。wrapper 会递归处理：

- `invoke(messages)` 中的 message 字符串。
- `generate_json(prompt)` 的 prompt 字符串。

metadata 中记录：

| 字段 | 说明 |
|------|------|
| `redaction_applied` | 是否发生替换。 |
| `redaction_count` | 替换片段数量。 |
| `redaction_types` | 替换类型集合。 |

注意：`redaction_count` 只说明最后出进程前替换了多少敏感片段，不表示上游可以保存 raw secret。raw API key、token、Authorization header、password、private key、DSN secret 和 backend auth value 仍不得进入 DB、audit、state、prompt 或文档样例。

自托管 `vllm` 当前不套 cloud redaction wrapper。它仍是显式 operator 配置的外部调用点，因此 prompt 构造处仍要做好输入筛选和脱敏。

## LLM Metadata 与 Cache 指标

普通 LLM 调用后，节点会读取 adapter 的 `last_metadata`，再调用 `record_llm_call(state, node_name, meta)`。并发 specialist 直接使用 `generate_json_with_metadata()` 的返回 metadata，由主线程统一写入 `llm_calls`，避免共享 `last_metadata` race。`state["llm_calls"]` 最终用于 worker 汇总：

```text
state["llm_calls"]
  -> apps/worker/tasks.py::_populate_run_metrics()
       -> AgentRun.total_prompt_tokens
       -> AgentRun.total_completion_tokens
       -> AgentRun.provider_cache_hit_count
       -> AgentRun.provider_cache_miss_count
       -> AgentRun.state.llm_metrics_summary
            (cached prompt tokens, LLM duration, provider cache unknown)

RequestLocalToolCache
  -> AgentRun.app_cache_hit_count
  -> AgentRun.app_cache_miss_count
```

重要区分：

| 指标 | 来源 | 不应混淆为 |
|------|------|------------|
| provider prompt/cache metadata | LLM provider 返回的 usage/finish reason | tool cache 或 Redis app cache。 |
| app/tool cache counters | `RequestLocalToolCache` | provider prompt cache。 |
| prompt segment cache key | `ContextBuilder.segment_cache_keys` | 已经命中的 provider cache。 |

当前 OpenAI-compatible adapter 只在 provider 明确返回 `prompt_tokens_details.cached_tokens` 时记录 provider cache hit/miss；没有 provider 明确信号时，provider cache status 保持 `unknown`，不要用 `finish_reason` 或应用层 cache 命中率推断 provider prompt cache。

Profile 只改变 adapter 选项，不改变 provider 选择或云 provider allow 规则。`plan_actions` 可使用 `fast_json`，`diagnose` / `diagnose_synthesize` 在 conditional reasoning 触发时可使用 `diagnose_reasoning`，`generate_report` 可使用 `report`。`LLM_NODE_MODEL_OVERRIDES` 和 `LLM_NODE_MAX_TOKENS` 可用 profile 名或具体节点名；具体节点名优先。

## M9 Draft-Only 边界

M9 LLM 能力默认关闭，并受全局 gate 控制。

draft/amendment 持久化、review/publish/apply 的完整生命周期见 [Runbook 草稿、版本与 Amendment 生命周期技术深挖](runbook-draft-version-amendment-lifecycle-deep-dive.md)。

| 功能 | 必要开关 | 输出 |
|------|----------|------|
| runbook LLM draft | `M9_EXTENSIONS_ENABLED=true` + `RUNBOOK_LLM_GENERATION_ENABLED=true` | `RunbookDraft(status=pending_review, draft_type=llm_generated)` |
| incident diff | `M9_EXTENSIONS_ENABLED=true` + `LLM_INCIDENT_DIFF_ENABLED=true` | `AmendmentDraft(status=pending_review)` |
| 外部 cloud provider | 还需要 `LLM_EXTERNAL_PROVIDER_ALLOWED=true` | 未开启时 blocked。 |

LLM runbook generation：

- prompt 只允许 approved runbook context、incident evidence summary、deterministic template draft、capability gaps 和 redacted EffectiveConfig。
- service 层持久化 draft，并强制 status 为 `pending_review`。
- runbook action classifier 会标注 read-only、diagnostic-only、approval-required、forbidden、unknown。
- LLM 不会 auto-approve、auto-publish 或 ingest chunk。

Incident diff：

- 在调用 LLM 前要求足够证据：diagnosis report、operator feedback、action results、linked approved runbook version，或至少 `MIN_INCIDENT_DIFF_EVIDENCE_REFS` 个 evidence refs。
- prompt 对 service、fault type、approved runbook 和 incident context 做 redaction。
- 解析出的 evidence refs 必须属于请求提供的 available evidence；高置信但无 evidence 会降级为 low。
- service 层只创建 `AmendmentDraft(status=pending_review)`。
- amendment approve/apply 还要走 review API；apply 必须有 evidence refs、`proposal_kind=proposed_patch`，并且只能指定一个目标 draft 或 runbook version。

## Eval 与 CI 边界

CI smoke eval 必须保持 FakeLLM：

| 路径 | Provider | 是否允许出网 | CI 稳定门禁 |
|------|----------|--------------|-------------|
| unit/integration 默认 | FakeLLM / DisabledLLM | 否 | 是 |
| smoke eval | FakeLLM | 否 | 是 |
| full eval 默认 | FakeLLM | 否 | 否，手动/PR 附加 |
| full eval real provider | 显式真实 provider | 是 | 否 |
| replay | 当前 settings | 可能 | 否 |
| shadow | stub | 否 | 否 |

`packages/evals/datasets/harness.py` 默认：

- `LLM_PROVIDER` 环境变量未设置时使用 `fake`。
- 非 fake provider 必须显式提供 `LLM_MODEL`。
- embedding provider 固定 fake。
- reranker provider 固定 fake。
- trace/git 使用 fixture。
- SQLite 环境下 hybrid search 走 deterministic lexical fallback。

真实 provider eval 可以用于人工比较 prompt/model 质量，但不能作为稳定 CI gate。报告必须记录 provider、model、prompt version、timeout、token 设置和运行时间。

## 常见误区

| 误区 | 正确口径 |
|------|----------|
| 生产默认会调用真实 LLM | 不会。未显式配置时 `APP_ENV=production` 默认 `LLM_PROVIDER=disabled`。 |
| `disabled` 表示 Agent 无法诊断 | 不对。`DisabledLLMAdapter` 委托 deterministic FakeLLM fallback，不出网。 |
| `LLM_EXTERNAL_PROVIDER_ALLOWED=true` 就会启用 M9 LLM 功能 | 不会。M9 runbook/diff 还需要 global gate 和子开关。 |
| 应用层 cache hit 等于 provider prompt cache hit | 不等价。两者来源不同。 |
| reasoning summary 会写入 DB | 不会。`record_llm_call()` 会剥离 `reasoning_summary`。 |
| multi-perspective 会记录 5 次诊断 LLM call | 不会。正常路径只记录 3 个 specialist 和 1 个 synthesizer 的真实调用；fallback single-call 才记录 `diagnose`。 |
| LLM 建议的动作可以直接执行 | 不可以。guardrail、approval、executor、verify 才决定执行路径。 |
| LLM runbook draft 会自动发布 | 不会。它只能进入 `pending_review`。 |
| 新 eval case 可以只靠真实 provider | 不可以。CI/smoke 要有 FakeLLM deterministic 覆盖。 |

## 修改 Checklist

修改 LLM、prompt、FakeLLM、provider 或 M9 draft 能力时，按这个清单收口：

- 保持 `LLMProvider` 协议不泄漏 provider-specific 细节到节点。
- 并发 LLM 调用必须使用 call-local metadata 返回值，不读写共享 `last_metadata`。
- 新 provider 需要 timeout、错误降级、safe metadata 和 secret handling。
- 云 provider 必须走 explicit allow 和 redaction wrapper。
- 新 LLM 调用点必须有 schema、JSON parse/repair 或 deterministic fallback。
- prompt 不得包含 raw logs、secret、token、auth header、private key、raw DSN。
- 诊断 prompt 输出必须保留 evidence ID 和 runbook chunk ID。
- action prompt 不得要求模型输出执行许可；执行权限仍由 guardrail 决定。
- 修改 allowed action 时同步 `prompts.py`、guardrail policy、executor capability 和测试。
- 修改 alert/fault coverage 时同步 `rules_fallback.py`、FakeLLM 和 eval case。
- M9 LLM 只能生成 `pending_review` draft/amendment，不能 auto-approve/publish/apply/execute。
- 真实 provider 只用于手动 demo/full eval，不进入 CI stable gate。
- 更新 `llm-and-prompts.md`、`configuration.md`、`evaluation.md`、`runbook-rag.md` 和本文。

## 测试定位

当前相关测试入口：

| 行为 | 测试入口 |
|------|----------|
| provider factory、cloud allow、redaction wrapper | `tests/unit/test_llm_providers.py` |
| disabled provider 和 production default | `tests/unit/test_disabled_llm.py`、`tests/unit/test_settings_production_defaults.py` |
| reasoning metadata 剥离 | `tests/unit/test_reasoning_layering.py` |
| Agent node fallback | `tests/unit/test_agent_nodes.py`、`tests/integration/test_graph_flow.py` |
| FakeLLM smoke eval | `tests/integration/test_eval_runner.py`、`packages/evals/datasets/harness.py` |
| M9 runbook generation | `tests/unit/test_llm_runbook_generation.py` |
| M9 incident diff | `tests/unit/test_incident_diff_analysis.py`、`tests/integration/test_amendment_draft_review.py` |
| runbook action classifier | `tests/unit/test_runbook_action_classifier.py` |
| production safety | `tests/unit/test_production_safety.py` |

本仓库当前约束是由用户本地运行测试；Codex 更新文档时只做静态检查，并提供建议命令。
