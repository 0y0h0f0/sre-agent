# LLM、Prompt 与 FakeLLM

## 默认策略

默认 `LLM_PROVIDER=fake`。单元测试、CI smoke flow 和 smoke eval 必须使用 FakeLLM。

真实 LLM 只适合手动 demo 或 manual full eval，不应作为稳定 CI gate。

## Provider 配置

配置项：

- `LLM_PROVIDER`：`fake`、`openai`、`deepseek`、`anthropic`、`vllm` 等。
- `LLM_MODEL`：模型名。
- `LLM_BASE_URL`：兼容 OpenAI API 的 base URL。
- `LLM_API_KEY`：secret。
- `LLM_TIMEOUT_SECONDS`。
- `LLM_MAX_TOKENS`。
- `LLM_TEMPERATURE`。
- `LLM_REASONING_ENABLED`：是否启用深度推理（默认 `false`）。
- `LLM_REASONING_EFFORT`：推理 effort 级别（provider 特定：`low`、`medium`、`high`）。
- `LLM_REASONING_NODES`：启用推理的节点列表（默认仅 `diagnose`）。

## FakeLLM 行为

FakeLLM 根据 alert name 或上下文中的事故类型返回确定性输出。MVP 四类事故都有固定根因和动作：

- DatabaseConnectionExhaustion。
- High5xxAfterDeploy。
- RedisCacheAvalanche。
- PodRestartLoop。

FakeLLM 还会返回 token 使用估算和结构化对象，用于测试：

- JSON output validity。
- 高风险动作阻断。
- evidence ID 保留。
- prompt/cache metrics。

## LLM Reasoning

`packages/agent/llm/reasoning.py` 管理深度推理行为：

- 通过 `LLM_REASONING_NODES` 配置需要推理的节点（默认仅 `diagnose`）。
- 仅当 `LLM_REASONING_ENABLED=true` 且当前节点在配置列表中时启用。
- `diagnose` 节点启用推理时输出 `diagnosis_rationale` 摘要。
- LLM 调用元数据（model、tokens、reasoning 状态）写入 `state["llm_calls"]`。
- 原始 chain-of-thought 不持久化到数据库，仅用于本次调用。

当前支持的 reasoning effort 级别与 provider 相关：
- Anthropic：`low` / `medium` / `high`
- OpenAI/DeepSeek：通过 `reasoning_effort` 参数传递
- FakeLLM/vLLM：reasoning 为 no-op，返回空 rationale

## Prompt 构建

`ContextBuilder` 负责构造 LLM messages，不直接调用 LLM。

messages 结构：

- system message：稳定 system prompt + output schema，用于 provider prefix cache。
- user message：alert、evidence、Runbook、memory、related incidents。

ContextBuilder 返回：

- `messages`
- `token_usage_estimate`
- `segment_cache_keys`
- `compressed_context`

## 缓存区分

必须区分两种缓存：

- provider prompt cache：由 LLM provider 的 prefix caching 行为决定。
- app prompt segment cache：系统自己的 Redis/application cache 概念。

Redis/tool cache hit rate 不能当作 provider prompt cache hit rate。provider cache 不可得时应标记 unknown 或按 adapter 返回信息统计。

## 输出要求

诊断输出应包含：

- hypotheses。
- root_cause。
- evidence_ids。
- missing_evidence。
- recommended_actions。

根因、假设和动作原因必须可追溯到 evidence ID 或 Runbook chunk ID。不能只写模型自由推断。

## 真实 LLM 使用注意

启用真实 LLM 时仍必须：

- 保持 guardrail 确定性规则。
- 保持 mock executor。
- 对 prompt 做 token 预算。
- 压缩大日志。
- 记录 LLM 调用和 token 使用。
- 不允许 LLM 直接授权 L2/L3/L4。
