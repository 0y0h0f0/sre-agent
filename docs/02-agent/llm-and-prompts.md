# LLM 与提示词

**最后更新：** 2026-06-23

## 概述

Agent 的 LLM 调用通过同步 `LLMProvider` 协议抽象，入口在 `packages/agent/llm/`。CI、单元测试和默认本地 demo 使用 deterministic `FakeLLMAdapter`；真实 provider 只在显式配置时使用。

LLM 不是安全决策者。它可以输出诊断、排序、动作建议和报告草稿，但动作权限由确定性 guardrail、approval 和 executor backend 决定。

如果需要沿代码路径理解 provider factory、FakeLLM / disabled provider、prompt fallback、usage metadata、reasoning redaction、真实 provider eval 边界和 M9 draft-only 能力，见 [LLM、Prompt、FakeLLM 与 Provider 边界技术深挖](../00-overview/llm-prompt-fakellm-provider-boundaries-deep-dive.md)。

下图概括 provider 工厂、真实 provider 脱敏边界、JSON 解析修复和 deterministic fallback 的关系。

<p>
  <img src="assets/llm-provider-boundary-flow.png" alt="LLM Provider 与提示词安全边界" width="900" />
</p>

## Provider 工厂

`packages/agent/llm/factory.py` 根据 `Settings.llm_provider` 构造 provider：

| `LLM_PROVIDER` | Adapter | 外部调用 | 用途 |
|----------------|---------|----------|------|
| `fake` | `FakeLLMAdapter` | 否 | 本地 demo、测试、CI smoke eval |
| `disabled` | `DisabledLLMAdapter` | 否 | 生产安全默认或显式禁用 |
| `vllm` | `OpenAICompatibleAdapter` | 是，本地/自托管 OpenAI-compatible endpoint | 手动 demo 或 eval |
| `openai` | `RedactingLLMAdapter(OpenAICompatibleAdapter)` | 是 | 手动 full eval 或受控 M9 功能 |
| `deepseek` | `RedactingLLMAdapter(OpenAICompatibleAdapter)` | 是 | 手动 full eval 或受控 M9 功能 |
| `anthropic` | `RedactingLLMAdapter(AnthropicAdapter)` | 是 | 手动 full eval 或受控 M9 功能 |

本地默认值是 `LLM_PROVIDER=fake`。当 `APP_ENV=production` 且用户没有显式设置 `llm_provider` 时，settings validator 会把 provider 改为 `disabled`。

外部云 provider（`openai`、`deepseek`、`anthropic`）还必须显式设置 `LLM_EXTERNAL_PROVIDER_ALLOWED=true`，否则 provider factory 会拒绝构造 adapter。自托管 `vllm` 不受该云 provider 开关限制，但仍必须由 operator 显式配置 endpoint、模型和超时。

外部云 provider 会由 `RedactingLLMAdapter` 包装。该包装层是所有云端 LLM 请求出进程前的最后一道边界，会对 `invoke()` messages 和 `generate_json()` prompt 中的字符串递归执行 `redact_text()`，并在 `last_metadata` / `llm_calls` 中记录 `redaction_applied`、`redaction_count`、`redaction_types`。包装层不保存 raw prompt；provider API key 仍只作为请求 header 使用，不进入 prompt。

## 当前默认配置

| 配置 | 默认值 | 说明 |
|------|--------|------|
| `LLM_PROVIDER` | `fake` | 本地/CI deterministic path |
| `LLM_MODEL` | `fake-diagnosis-model` | run 记录中的模型名 |
| `LLM_BASE_URL` | `http://localhost:8001/v1` | OpenAI-compatible provider 的 base URL |
| `LLM_API_KEY` | 空 | `SecretStr`，只在 adapter 调用点解包 |
| `LLM_TIMEOUT_SECONDS` | `30.0` | provider 请求超时 |
| `LLM_MAX_TOKENS` | `512` | 最大输出 token |
| `LLM_TEMPERATURE` | `0.1` | 真实 provider 温度 |
| `LLM_DETERMINISTIC_REPORT_ENABLED` | `false` | 开启后 `generate_report` 跳过报告 LLM 调用，使用确定性报告生成；report schema 和版本追加不变 |
| `LLM_FAST_JSON_MODEL` | 空 | `fast_json` profile 的可选模型覆盖；空值继承 `LLM_MODEL` |
| `LLM_FAST_JSON_MAX_TOKENS` | `0` | `fast_json` profile 的可选 token 覆盖；`0` 继承 `LLM_MAX_TOKENS` |
| `LLM_DIAGNOSE_REASONING_MODEL` | 空 | `diagnose_reasoning` profile 的可选模型覆盖；空值继承 `LLM_MODEL` |
| `LLM_DIAGNOSE_REASONING_MAX_TOKENS` | `0` | `diagnose_reasoning` profile 的可选 token 覆盖；`0` 继承 `LLM_MAX_TOKENS` |
| `LLM_REPORT_MODEL` | 空 | `report` profile 的可选模型覆盖；空值继承 `LLM_MODEL` |
| `LLM_REPORT_MAX_TOKENS` | `0` | `report` profile 的可选 token 覆盖；`0` 继承 `LLM_MAX_TOKENS` |
| `LLM_NODE_MODEL_OVERRIDES` | 空 | JSON object 或逗号分隔 `node_or_profile=model` 覆盖 |
| `LLM_NODE_MAX_TOKENS` | 空 | JSON object 或逗号分隔 `node_or_profile=tokens` 覆盖 |
| `LLM_REASONING_ENABLED` | `false` | 深度推理总开关 |
| `LLM_REASONING_EFFORT` | `medium` | 传给支持 reasoning 的 provider |
| `LLM_REASONING_NODES` | `diagnose,diagnose_synthesize` | 启用 reasoning 时使用深度推理的节点 |
| `LLM_MULTI_PERSPECTIVE_ENABLED` | `false` | 是否启用 metrics/logs/traces specialist + synthesizer |
| `LLM_MULTI_PERSPECTIVE_PARALLEL_ENABLED` | `false` | multi-perspective 开启时是否并发运行 metrics/logs/traces specialist；仅 provider 支持 call-local metadata 时生效，synthesizer 仍顺序执行 |

LLM profile 当前只改变 adapter 选项：`packages/agent/llm/factory.py` 可以用 `build_llm(settings, profile="fast_json" | "diagnose_reasoning" | "report")` 构造不同模型/token 参数的 adapter；运行时节点也会在 profile 实际配置时把低风险 per-call 覆盖传给已注入的 `deps.llm`。profile 只能改变模型名、max tokens、temperature/reasoning effort 这类 adapter 选项；不会改变 `LLM_PROVIDER`、云 provider 的 `LLM_EXTERNAL_PROVIDER_ALLOWED` 要求，也不会绕过 `RedactingLLMAdapter`。

`LLM_NODE_MODEL_OVERRIDES` / `LLM_NODE_MAX_TOKENS` 可使用 profile 名或具体节点名；具体节点名（例如 `plan_actions`、`generate_report`）优先于 profile 名。

当前节点路由：

- `plan_actions` 在配置了 `fast_json` profile 时使用其模型/token 覆盖，确定性 fallback 不变。
- `generate_report` 在配置了 `report` profile 时使用其模型/token 覆盖；`LLM_DETERMINISTIC_REPORT_ENABLED=true` 时跳过报告 LLM 调用，直接使用确定性报告生成。
- `diagnose` / `diagnose_synthesize` 只有在 deep reasoning 触发时使用 `diagnose_reasoning` profile。

M9 的 LLM runbook 生成和 incident diff 还有独立 feature gate，见下文。

## FakeLLM 覆盖范围

`FakeLLMAdapter` 包装 `packages/agent/fake_llm.py`，读取 `packages/agent/rules_fallback.py` 中的确定性映射。当前覆盖 15 类告警：

- `DatabaseConnectionExhaustion`
- `High5xxAfterDeploy`
- `RedisCacheAvalanche`
- `PodRestartLoop`
- `CPUThrottling`
- `MemoryLeak`
- `DiskFull`
- `CertificateExpiry`
- `DNSFailure`
- `MessageQueueLag`
- `RateLimitTriggered`
- `SlowAPI`
- `ErrorBudgetBurn`
- `P0SiteOutage`
- `DownstreamTimeout`

未知告警按 `High5xxAfterDeploy` 路径回退。FakeLLM 无随机性、无网络调用，并会尽量把 prompt 中出现的 evidence ID 写回诊断结果。

## 调用点

| 节点/组件 | 调用方式 | 输出 |
|-----------|----------|------|
| `diagnose` | `generate_json(prompt, CompactDiagnosisOutput)` then internal mapping | public `hypotheses`、`root_cause`、`missing_evidence` |
| `rank_hypotheses` | LLM 或确定性排序路径 | ranked hypotheses |
| `plan_actions` | `generate_json(prompt, list[PlannedAction])` | recommended actions |
| `generate_report` | `invoke()`/JSON parse；deterministic report mode 下跳过 LLM | incident report |
| `LLMRunbookGenerator` (M9) | `invoke()` | `RunbookDraft(status=pending_review)` 的内容 |
| `IncidentDiffAnalyzer` (M9) | `invoke()` | `AmendmentDraft(status=pending_review)` 的提案 |

`diagnose` 失败时会尝试 JSON repair；repair 仍失败时使用 deterministic rules fallback。

诊断节点使用 compact internal schema 降低 LLM 输出 token：

- `CompactDiagnosisOutput` 短字段：`h`、`rc`、`e`、`r`、`m`。
- `CompactHypothesis` 短字段：`id`、`s`、`e`、`r`、`c`、`why`。
- `CompactRootCause` 短字段：`s`、`c`、`e`、`r`。

节点立刻通过 `diagnosis_output_from_compact()` 映射回现有 `DiagnosisOutput`，所以 state、报告生成、API 和后续 deterministic ranking 仍看到 `hypotheses`、`root_cause`、`evidence_ids`、`runbook_chunk_ids`、`missing_evidence`。映射必须保留 evidence IDs、runbook chunk IDs、confidence、hypothesis statement 和 root cause summary。

## 提示词文件

`packages/agent/prompts.py` 保存 Agent 运行时提示词：

| 模板 | 用途 |
|------|------|
| `SYSTEM_PROMPT` | SRE Agent 总规则，要求引用 evidence ID、输出 JSON、禁止 L4 |
| `DIAGNOSIS_PROMPT_TEMPLATE` | 单次诊断路径 |
| `METRICS_SPECIALIST_SYSTEM_PROMPT` | metrics specialist |
| `LOGS_SPECIALIST_SYSTEM_PROMPT` | logs specialist |
| `TRACES_SPECIALIST_SYSTEM_PROMPT` | traces specialist |
| `SYNTHESIZER_SYSTEM_PROMPT` | multi-perspective synthesizer |
| `RANK_PROMPT_TEMPLATE` | 假设排序 |
| `PLAN_ACTIONS_PROMPT_TEMPLATE` | 动作规划，内嵌 allowed action table |
| `REPORT_PROMPT_TEMPLATE` | 事故报告 |
| `SUMMARIZATION_PROMPT` | 摘要提示词，当前 memory 包本身不直接调用 LLM |

`allowed_actions_table()` 的动作列表必须与 `guardrails/policy.py` 保持一致。新增动作类型时两处都要更新，并补 guardrail 测试。

## Multi-perspective 诊断

当 `LLM_MULTI_PERSPECTIVE_ENABLED=true` 时，`diagnose` 会运行：

1. metrics specialist
2. logs specialist
3. traces specialist，附带 service topology
4. synthesizer，整合 specialist 输出、deployment、K8s、DB、runbook 和 memory

specialist 调用失败不会中止主流程；失败的 perspective 返回空 `DiagnosisOutput`，synthesizer 仍会尝试继续。synthesizer 失败时回退到单次诊断，并携带已成功的 specialist 输出。

`LLM_MULTI_PERSPECTIVE_PARALLEL_ENABLED=true` 时，前三个 specialist 可以并发执行。worker 线程只返回各自 compact output 和 call-local metadata，不写 LangGraph state、不读取或更新共享 `last_metadata`、不使用 DB session；主线程统一聚合 specialist output 并写入 `llm_calls`。synthesizer 仍在 specialist 完成后顺序执行。传统 `invoke()` / `generate_json()` 入口仍会更新 `last_metadata`，但并发 specialist 必须使用 `*_with_metadata()` 返回值。multi-perspective 正常路径只记录真实 LLM 调用：metrics、logs、traces 和 synthesizer；不会额外写入 synthetic top-level `diagnose` 调用。

如果某个并发 specialist 超过 `LLM_TIMEOUT_SECONDS`，主流程会以该 perspective 的空诊断继续。Python 无法强制终止已运行中的 provider 请求；该线程可能稍后返回，但由于 call-local metadata API 不写共享 state/DB/`last_metadata`，迟到结果不会污染后续 synthesizer 或 top-level audit。

## 深度推理与审计

`packages/agent/llm/reasoning.py` 负责节点级 reasoning 开关：

- `LLM_REASONING_ENABLED=false` 时所有调用都是普通推理。
- 开启后，只有 `LLM_REASONING_NODES` 中的候选节点可能把 `thinking=true` 传给 adapter。
- 默认候选节点是 `diagnose` 和 `diagnose_synthesize`；实际 diagnosis thinking 还需要 evidence conflict、P0/SEV0/SEV1/CRITICAL severity、cascade suspicion、missing evidence，或显式 operator override。
- `record_llm_call()` 使用显式 allowlist，只记录 provider、model、usage、provider cache 三态、duration、service tier、finish_reason 和 redaction 等安全元数据。`provider_cache_status` 是权威字段；迁移期仅为当前 worker 聚合保留 explicit hit/miss 对应的 legacy `cache_hit`，且不会把 `unknown` 折叠成 miss。
- `reasoning_summary`、`reasoning_content`、raw prompt、raw completion、raw response、raw query 和未知字段会被丢弃，不写入 state、DB 或审计轨迹，避免保存原始推理内容或 provider 响应内容。

OpenAI-compatible adapter 会把 provider 返回的安全 token usage 写入 Prometheus 指标和 `llm_calls` 元数据，包括 `prompt_tokens`、`completion_tokens`、`total_tokens`、显式 `prompt_tokens_details.cached_tokens`、reasoning token 数、`service_tier`、`finish_reason` 和 `duration_ms`。provider prompt cache 状态是三态：`hit`、`miss`、`unknown`；只有 provider 明确返回 cache token 详情时才记录 hit/miss，没有明确信号时必须保持 `unknown`。Prometheus runtime 指标使用低基数 label：`model`、`provider` 和 cache `status`；`agentp_llm_provider_cache_status_total` 记录三态状态，legacy `agentp_llm_cache_hit_total` / `agentp_llm_cache_miss_total` 只在明确 hit/miss 时递增，`unknown` 不进入 miss。不要把 `finish_reason == "cache_hit"` 或应用层 cache 命中率解释成 provider prompt cache 命中率。

外部云 provider 的 `llm_calls` 还会包含脱敏元数据，例如 `redaction_count`。该数字只表示 wrapper 在最终 prompt/message 中替换的敏感片段数量，不代表上游证据采集允许保存 raw secret。

JSON repair 和 fallback 路径也有独立指标：

- `agentp_llm_json_repair_attempts_total{node}` 记录节点发起 JSON repair 的次数。
- `agentp_llm_fallback_total{node,reason}` 记录进入 fallback 路径的次数。

`node` 和 `reason` 都使用固定低基数 allowlist，未知值归一为 `unknown`。`reason` 只能使用固定 code，例如 `json_repair_failed`、`llm_generate_failed`、`report_generation_failed` 或 `unknown`。不要把异常字符串、prompt、completion、provider raw response、URL、query、customer ID 或 secret 放入 metrics label。

## JSON 解析与回退

`packages/agent/llm/base.py` 提供：

| 函数 | 作用 |
|------|------|
| `extract_json()` | 容忍 Markdown fence 和 JSON 前后的文本，提取对象或数组 |
| `parse_into_schema()` | 把解析后的 JSON 转成 Pydantic model 或 model list |

节点层回退策略：

1. 首次 `generate_json()`。
2. 失败后构造 repair prompt，要求只返回匹配 compact schema 的 JSON。
3. repair 失败时，诊断使用 deterministic rules fallback；其它节点按各自实现降级。

## M9 LLM 边界

M9 LLM 能力默认关闭，并受全局开关控制：

| 功能 | 必要开关 | 输出边界 |
|------|----------|----------|
| LLM runbook draft | `M9_EXTENSIONS_ENABLED=true` + `RUNBOOK_LLM_GENERATION_ENABLED=true` | 只生成 `RunbookDraft(status=pending_review, draft_type=llm_generated)` |
| LLM incident diff | `M9_EXTENSIONS_ENABLED=true` + `LLM_INCIDENT_DIFF_ENABLED=true` | 只生成 `AmendmentDraft(status=pending_review)` |
| 外部/cloud LLM provider | 还需要 `LLM_EXTERNAL_PROVIDER_ALLOWED=true` | 未开启时返回 blocked |

LLM 不会自动批准、发布、应用 amendment，也不会触发 remediation 执行。runbook prompt builder 会脱敏输入，并只允许 approved runbook context、evidence summary、template draft、capability gaps 和 redacted EffectiveConfig 进入 prompt。

## 新增或修改提示词 checklist

1. 保持输出 schema 明确，优先要求 JSON。
2. 保留 evidence ID、chunk ID、action type 和 target 等可追溯字段。
3. 不要求模型输出执行许可；权限仍归 deterministic guardrail。
4. 不把 raw logs、secret、token、private key、auth header 放入 prompt。
5. 更新 FakeLLM 或 rules fallback，保证 CI/smoke eval deterministic。
6. 添加解析失败、无效 JSON、未知动作、L3/L4 边界测试。
7. 如果启用真实 provider，只用于手动 full eval 或手动 demo，不作为稳定 CI gate。

## 相关测试

- `tests/unit/test_llm_providers.py`
- `tests/unit/test_disabled_llm.py`
- `tests/unit/test_agent_nodes.py`
- `tests/unit/test_reasoning_layering.py`
- `tests/unit/test_llm_runbook_generation.py`
- `tests/unit/test_incident_diff_analysis.py`
- `tests/evals/` 和 `packages/evals/` 中的 FakeLLM smoke eval
