# sre-agent M9 阶段执行计划：Controlled AI & Observability Extensions

**Date:** 2026-06-12  
**Status:** final reference draft after applicability review  
**Depends on:** M8 Testing & Docs release gate  
**Target:** 在不破坏 M0–M8 生产安全边界的前提下，为 `sre-agent` 增加受控的 AI 增强、Web 上下文、Tempo、Grafana 和语义 Runbook 搜索能力。

**Usage Contract:** 本文件是 M9 实现参照与 agent 执行约束文档，必须与 M0–M8 主施工文档共同使用。M9 不得绕过 M8 release gate；如果当前代码库与本文建议文件路径不一致，agent 必须先定位现有等价模块，不得擅自新建重复模块或替换 M0–M8 已有核心实现。

---

## 0. 背景与定位

当前主施工文档中，M9+ 属于 M8 之后的 Future Extensions，不进入 Phase 0–M8 的当前交付范围。M9 的定位是“受控增强”，不是替换 M0–M8 已完成的确定性诊断、安全发布、配置合并、审计和回滚能力。

M9 包含以下能力：

| Capability | 默认状态 | 强制约束 |
|---|---:|---|
| LLM Runbook Generation | `RUNBOOK_LLM_GENERATION_ENABLED=false` | 允许外部云 LLM，但只能生成 `RunbookDraft(status=pending_review)`，不能直接发布 |
| LLM Incident Diff Analysis | `LLM_INCIDENT_DIFF_ENABLED=false` | 允许外部云 LLM，但只能生成 `AmendmentDraft(status=pending_review)`，不能直接修改 approved runbook |
| Runbook web_search | `RUNBOOK_WEB_SEARCH_ENABLED=false` | 必须脱敏、SSRF 防护、来源追溯，只能进入 draft evidence |
| TempoTraceBackend | `TRACE_BACKEND=tempo` 显式 opt-in | 默认不启用；失败只能 degraded |
| Tempo auto-discovery | `TEMPO_DISCOVERY_ENABLED=false` | production 中永不 auto_publish backend URL |
| Grafana webhook ingest | `GRAFANA_ALERT_INGEST_ENABLED=false` | 默认关闭；关闭时行为固定，不创建 incident |
| Semantic runbook search | `SEMANTIC_RUNBOOK_SEARCH_ENABLED=false` | embedding 失败只降级 semantic search，不影响 approved ingest |
| External embedding provider | `EXTERNAL_EMBEDDING_PROVIDER_ENABLED=false` | 外部数据流出能力，必须单独授权和审计 |

---

## 0.1 本次审查后已确认的 M9 执行决策

本次 M9 文档按以下已确认决策执行，不再作为开放问题留给实现 agent 自行判断：

- **允许外部云 LLM 服务，但默认不允许**：LLM Runbook Draft 与 LLM Incident Diff 可以调用外部云 LLM provider，但 `LLM_EXTERNAL_PROVIDER_ALLOWED` 默认必须为 `false`；只有显式开启、单独授权、完整脱敏、可审计时才允许调用，并且不能保存 full raw prompt。
- **外部云 LLM 属于数据外发能力**：除 `runbook:review` 外，还必须引入并校验 `runbook:llm_generate`、`incident:llm_diff`、`llm:invoke` / `ai:external` 等 scope。
- **外部云 LLM 必须双重 opt-in**：除对应功能开关外，外部云 LLM provider 还必须满足 `LLM_EXTERNAL_PROVIDER_ALLOWED=true` 与显式 `LLM_PROVIDER=<external_provider>`，不得因 M9 总开关开启而自动启用。
- **M8 已有稳定 Jaeger**：本次 M9 上线前 trace 基线为 `TRACE_BACKEND=jaeger`、`TRACE_ENABLED=true`。M9 回滚时必须恢复该基线；这不是通用脚本硬编码，而是本次 rollout 的 `PRE_M9_TRACE_*` 记录值。
- **web_search production 必须配置 allowlist**：`APP_ENV=production` 且 `RUNBOOK_WEB_SEARCH_ALLOWED_DOMAINS` 为空时，不允许启用 web_search。
- **web_search provider disabled 行为固定**：`RUNBOOK_WEB_SEARCH_ENABLED=true` 但 `RUNBOOK_WEB_SEARCH_PROVIDER=disabled` 时，必须返回 `config_error` / `degraded`，不得 fallback 到任何默认搜索 provider。
- **Grafana webhook enabled 时默认使用 HMAC 鉴权**：启用 Grafana ingest 后，默认实现 `HMAC signature + GRAFANA_WEBHOOK_SECRET_REF`；API key / shared token 只能作为兼容路径。未配置任何鉴权方式时，endpoint 必须返回 `503` / `config_error`，不创建 incident。
- **Semantic search 权限不复用 config read**：Runbook 搜索应使用 `runbook:read` 或 `runbook:review`，不使用 `config:read` 作为替代权限。
- **`.env.example` 与本次 rollout 配置必须区分**：`.env.example` 面向新环境应展示 `TRACE_ENABLED=false`、`TRACE_BACKEND=disabled`；本次 production rollout 才使用 `TRACE_ENABLED=true`、`TRACE_BACKEND=jaeger`、`PRE_M9_TRACE_BACKEND=jaeger`、`PRE_M9_TRACE_ENABLED=true`。

---

## 1. M9 总目标

M9 的目标是在 M0–M8 稳定运行后，增加以下可控增强能力：

1. 支持 LLM 生成 Runbook 草稿；
2. 支持 LLM 对 incident 与 Runbook 的差异进行分析；
3. 支持 Runbook 生成时使用受控的外部 Web 上下文；
4. 支持 Tempo trace backend；
5. 支持 Tempo backend endpoint discovery；
6. 增强 Grafana webhook ingest；
7. 支持 semantic runbook search；
8. 支持 external embedding provider；
9. 建立 M9 feature gate、权限、审计、E2E、运行时指标和回滚文档。

M9 不改变以下 M0–M8 不变量：

- worker 只读取 `published EffectiveConfigVersion`；
- production backend URL discovery 永不 auto_publish；
- raw backend secret 不进入 DB / audit / log / prompt / state；
- `raw_labels` 是 fingerprint 的原始输入，内部 marker 不得覆盖；
- `LLM_PROVIDER=disabled` 路径必须可运行；
- embedding provider 失败不得阻塞 approved runbook ingest；
- `EXECUTOR_BACKEND=fixture` 仍是 production 安全默认执行后端；
- Discovery 失败不阻塞 agent 启动；
- 已发布配置 stale 只 warning，不自动硬失效。

---

## 2. M9 核心原则

1. **默认关闭**  
   production 中所有 M9 能力默认 disabled。除非 operator 显式开启，否则不调用 LLM、web_search、Tempo、Grafana ingest 或 external embedding provider。

2. **只增强，不接管**  
   M9 不替代 deterministic diagnosis、template runbook、published EffectiveConfig、Alertmanager poll、BackendAuth redaction 等核心路径。

3. **全局开关强制优先**  
   `M9_EXTENSIONS_ENABLED=false` 时，所有 M9 子能力强制 disabled，即使某个子开关被设置为 `true`。

4. **LLM 只能生成草稿**  
   LLM Runbook 和 LLM incident diff 只能生成待审核草稿，不能直接发布、approve、apply 或执行。

5. **web_search 只能作为引用上下文**  
   web_search 结果必须保留来源、检索时间、内容 hash，只能作为 draft evidence，不能直接进入 approved runbook。

6. **人工审核仍是边界**  
   Runbook approve、publish、regenerate、amendment merge 仍必须经过人工 review。

7. **secret 不进入 prompt / audit / state / log**  
   runtime secret 只能用于 backend client construction，不允许进入 AgentDeps、LLM prompt、debug log、audit details、Runbook draft 或 embedding input。

8. **backend URL 仍需安全校验**  
   Tempo、web_search、external embedding provider 等所有外部 URL 都必须经过 BackendUrlSafetyValidator。

9. **失败即降级**  
   LLM、web_search、Tempo、Grafana ingest、embedding provider 任一失败时，只能让对应增强能力 degraded，不得阻塞基础 diagnosis。

10. **可独立回滚**  
    每个 M9 能力必须有独立开关。总回滚必须恢复到 M9 上线前的已验证配置，而不是硬编码某个 backend。

---

## 3. M9 Feature Flag 优先级

### 3.1 总规则

```text
M9_EXTENSIONS_ENABLED=false
  -> 强制关闭所有 M9 子功能
  -> 不调用 LLM
  -> 不调用 web_search
  -> 不启用 TempoTraceBackend
  -> 不启用 Tempo discovery
  -> 不接收 Grafana webhook ingest
  -> 不启用 semantic search
  -> 不调用 external embedding provider
```

### 3.2 子开关冲突处理

如果检测到以下冲突：

```env
M9_EXTENSIONS_ENABLED=false
RUNBOOK_LLM_GENERATION_ENABLED=true
```

系统行为必须是：

- 不启用该子功能；
- 服务继续启动；
- 记录 startup warning；
- 暴露 metric：`m9_feature_flag_conflict_total{feature="runbook_llm"}`；
- 不抛出 fatal error；
- 不发起任何外部调用。

### 3.3 production opt-in 规则

production 中启用任一 M9 子功能必须同时满足：

```env
APP_ENV=production
M9_EXTENSIONS_ENABLED=true
<对应子开关>=true
```

其中 external embedding provider 还必须满足额外权限和配置要求，详见 PR 9.9。

外部云 LLM provider 还必须额外满足：

```env
LLM_EXTERNAL_PROVIDER_ALLOWED=true
LLM_PROVIDER=<explicit_external_provider>
```

并且调用方必须具备 `llm:invoke` 或 `ai:external` scope。

---

## 4. M9 前置条件

开始 M9 前，必须确认：

- [ ] M8 的 P0 release gate 全部通过；
- [ ] M8 的 P1 release gate 无阻塞项；
- [ ] `APP_ENV=production` 下，`LLM_PROVIDER=disabled` 可完成完整 diagnosis；
- [ ] `EXECUTOR_BACKEND=fixture` 是 production 默认执行后端；
- [ ] worker 只读取 `published EffectiveConfigVersion`；
- [ ] 未发布 proposal / detected_only config 不会进入 worker；
- [ ] BackendUrlSafetyValidator 已接入 publish / override / profile / EffectiveConfig merge；
- [ ] BackendAuth redaction 已覆盖 AgentDeps / audit / log / prompt；
- [ ] AuditLog 对 publish / rollback / revoke / override / rerun / poll / bootstrap 均完整记录；
- [ ] Runbook approved ingest 在 embedding provider 不可用时可降级为 keyword-only / chunk-only；
- [ ] Beat singleton observability 和 Redis lock 测试已通过；
- [ ] 已记录 M9 上线前 trace backend 稳态值：`PRE_M9_TRACE_BACKEND=jaeger`；
- [ ] 已记录 M9 上线前 trace enabled 稳态值：`PRE_M9_TRACE_ENABLED=true`；
- [ ] 已确认 M8 Jaeger 为稳定真实 trace backend；M9 回滚恢复该基线。

---

## 5. M9 不做事项

M9 明确不做：

- [ ] 不允许 LLM 自动 approve runbook；
- [ ] 不允许 LLM 自动 publish runbook；
- [ ] 不允许 LLM 自动 apply amendment；
- [ ] 不允许 LLM 自动执行 remediation action；
- [ ] 不允许 web_search 结果绕过人工 review；
- [ ] 不允许 external embedding provider 接收 raw secret；
- [ ] 不允许 Tempo auto-discovery 自动发布 production backend URL；
- [ ] 不允许 Grafana webhook ingest 默认启用；
- [ ] 不改变 Alertmanager poll 的 fingerprint 和 raw_labels 规则；
- [ ] 不改变 EffectiveConfig 优先级；
- [ ] 不放宽 Backend URL 安全校验；
- [ ] 不把 production 默认 LLM provider 从 disabled 改为真实 provider；
- [ ] 不把 fixture 当作 production 正常 observability backend；
- [ ] 不在总回滚脚本里硬编码 `TRACE_BACKEND=jaeger` 或 `TRACE_BACKEND=fixture`。

---

## 6. M9 PR 总览

| PR | 名称 | 目标 | 依赖 | 默认状态 |
|---|---|---|---|---|
| PR 9.1 | M9 Feature Gate 与基础不变量 | 增加 M9 总开关、子开关、disabled trace 语义、运行时冲突处理 | M8 | disabled |
| PR 9.2 | LLM Runbook Draft Generation | LLM 生成 pending_review RunbookDraft | PR 9.1 | disabled |
| PR 9.3 | LLM Incident Diff Analysis | LLM 生成 AmendmentDraft | PR 9.1 | disabled |
| PR 9.4 | Runbook web_search Safety Wrapper | 安全 Web 上下文检索 | PR 9.1 | disabled |
| PR 9.5 | TempoTraceBackend | 增加 Tempo trace backend | PR 9.1 | opt-in |
| PR 9.6 | Tempo Auto-discovery Enablement | 自动发现 Tempo endpoint | PR 9.5 | disabled / detected_only |
| PR 9.7 | Grafana Webhook Parser Enhancement | 增强 Grafana alert ingest | PR 9.1 | disabled |
| PR 9.8 | Semantic Runbook Search | 增加语义 Runbook 搜索 | PR 9.1 | disabled |
| PR 9.9 | External Embedding Provider | 支持外部 embedding provider | PR 9.8 | disabled |
| PR 9.10 | M9 Runtime Metrics / Threat Model / E2E / Docs | 端到端测试、运行时指标、威胁模型和上线文档 | PR 9.1–9.9 | n/a |

---

## 7. 推荐执行顺序

```text
PR 9.1 M9 Feature Gate 与基础不变量
  ├── PR 9.2 LLM Runbook Draft Generation
  ├── PR 9.3 LLM Incident Diff Analysis
  ├── PR 9.4 Runbook web_search Safety Wrapper
  ├── PR 9.5 TempoTraceBackend
  │     └── PR 9.6 Tempo Auto-discovery Enablement
  ├── PR 9.7 Grafana Webhook Parser Enhancement
  └── PR 9.8 Semantic Runbook Search
        └── PR 9.9 External Embedding Provider

PR 9.10 M9 Runtime Metrics / Threat Model / E2E / Docs 最后执行
```

建议分四批上线：

```text
M9A: Feature gate + LLM draft + LLM diff
M9B: web_search 安全封装
M9C: Tempo + Grafana
M9D: Semantic search + external embedding + E2E docs
```

---

# 8. Detailed PR Plan

---

## PR 9.1: M9 Feature Gate 与基础不变量

### 背景

M9 引入 LLM、web_search、Tempo、Grafana ingest 和 external embedding provider。它们都涉及外部依赖、外部数据流出或生产观测链路变化，因此必须先建立统一 feature gate、回滚语义和运行时冲突处理。

### 范围

- [ ] 新增 `M9_EXTENSIONS_ENABLED`，默认 `false`；
- [ ] 新增 `RUNBOOK_LLM_GENERATION_ENABLED`，默认 `false`；
- [ ] 新增 `RUNBOOK_WEB_SEARCH_ENABLED`，默认 `false`；
- [ ] 新增 `LLM_INCIDENT_DIFF_ENABLED`，默认 `false`；
- [ ] 新增 `TRACE_ENABLED`，默认 production 中根据已有配置决定；无真实 backend 时为 `false`；
- [ ] 新增 `TRACE_BACKEND` 支持值：`disabled | fixture | jaeger | tempo`；
- [ ] 明确：`fixture` 只允许用于 local / CI / demo / 显式 emergency fail-closed，不作为 production 正常 observability backend；
- [ ] 新增 `TEMPO_DISCOVERY_ENABLED`，默认 `false`；
- [ ] 新增 `GRAFANA_ALERT_INGEST_ENABLED`，默认 `false`；
- [ ] 新增 `SEMANTIC_RUNBOOK_SEARCH_ENABLED`，默认 `false`；
- [ ] 新增 `EMBEDDING_PROVIDER` 支持值：`disabled | bge_zh | external`；
- [ ] 新增 `EXTERNAL_EMBEDDING_PROVIDER_ENABLED`，默认 `false`；
- [ ] 新增 scope：`runbook:web_search`；
- [ ] 新增 scope：`runbook:read`；
- [ ] 新增 scope：`runbook:llm_generate`；
- [ ] 新增 scope：`incident:llm_diff`；
- [ ] 新增 scope：`llm:invoke` 或 `ai:external`，用于外部云 LLM 调用授权；
- [ ] 新增 scope：`embedding:external`；
- [ ] 实现 M9 全局开关优先级；
- [ ] 实现子开关冲突 warning 和 metric；
- [ ] 记录 `PRE_M9_TRACE_BACKEND=jaeger`、`PRE_M9_TRACE_ENABLED=true` 的本次 rollout 文档要求；
- [ ] 明确 M9 global gate 与既有 trace backend 的关系：`M9_EXTENSIONS_ENABLED=false` 只能禁止 M9 新增的 Tempo 能力，不得关闭 M8 已验证的 Jaeger；
- [ ] 当 `M9_EXTENSIONS_ENABLED=false` 且 `TRACE_BACKEND=tempo` 时，Tempo trace 强制 degraded/disabled，并记录 `m9_feature_flag_conflict_total{feature="tempo_trace"}`；
- [ ] 当 `M9_EXTENSIONS_ENABLED=false` 且 `TRACE_BACKEND=jaeger` 时，保持 M8 Jaeger 行为不变。

### 不做

- 不实现 LLM draft；
- 不实现 web_search；
- 不实现 Tempo；
- 不实现 Grafana parser；
- 不实现 embedding provider。

### 建议文件

```text
packages/common/settings.py
apps/api/dependencies.py
packages/common/feature_flags.py
tests/unit/test_m9_feature_flags.py
tests/unit/test_trace_backend_settings.py
docs/m9-rollout.md
```

### 测试清单

```text
test_m9_extensions_default_disabled
test_m9_global_disabled_forces_subfeatures_disabled
test_m9_subfeature_true_with_global_false_records_warning
test_m9_subfeature_true_with_global_false_records_metric
test_production_runbook_llm_default_false
test_production_web_search_default_false
test_production_llm_incident_diff_default_false
test_production_tempo_discovery_default_false
test_production_grafana_ingest_default_false
test_production_semantic_search_default_false
test_trace_backend_accepts_disabled_fixture_jaeger_tempo
test_fixture_trace_backend_rejected_as_normal_production_backend
test_trace_backend_disabled_means_trace_tool_degraded
test_embedding_provider_accepts_disabled_bge_zh_external
test_m9_global_disabled_does_not_disable_existing_jaeger
test_m9_global_disabled_forces_tempo_degraded
test_tempo_trace_conflict_metric_recorded
```

### 验收标准

- [ ] `M9_EXTENSIONS_ENABLED=false` 时，所有 M9 子路径强制关闭；
- [ ] 子开关残留为 true 不导致启动失败；
- [ ] 子开关冲突有 warning 和 metric；
- [ ] production 默认不会调用任何 M9 外部依赖；
- [ ] `TRACE_BACKEND=fixture` 不被文档或代码当作 production 正常 trace backend；
- [ ] `TRACE_BACKEND=disabled` 或 `TRACE_ENABLED=false` 能让 TraceTool 明确 degraded；
- [ ] M9 global gate 关闭时不影响 M8 已验证 Jaeger；
- [ ] 新 scope 可用于后续 API 权限控制。

### 风险点

- 只有子开关没有总开关，会导致回滚复杂；
- 总开关若设计为 fatal，会导致生产配置残留时无法启动；
- trace 回滚若在通用脚本中硬编码 `jaeger` 或 `fixture`，都会导致环境歧义；
- 本次 rollout 已确认 `PRE_M9_TRACE_BACKEND=jaeger`，但实现仍必须读取 `PRE_M9_TRACE_*`，不能把 Jaeger 写死在通用逻辑中。

### 回滚方案

```env
M9_EXTENSIONS_ENABLED=false
```

---

## PR 9.2: LLM Runbook Draft Generation

### 背景

M6 已完成 deterministic runbook template generation。M9 可以添加 LLM 草稿生成能力，但 LLM 只能作为草稿辅助，不能直接发布或执行。

### 范围

- [ ] 新增 `LLMRunbookGenerator`；
- [ ] 新增 `RunbookPromptBuilder`；
- [ ] 输入只允许使用 prompt-safe redacted context；
- [ ] 支持从以下来源构造 prompt：
  - approved runbook chunks；
  - incident evidence summary；
  - deterministic template draft；
  - capability gaps；
  - redacted EffectiveConfig；
- [ ] 输出 `RunbookDraft`；
- [ ] `draft_type=llm_generated`；
- [ ] `status=pending_review`；
- [ ] 保存 `parent_draft_id`；
- [ ] 不保存 full raw prompt；
- [ ] 只允许保存 prompt metadata：
  - `prompt_template_id`
  - `prompt_template_version`
  - `redaction_version`
  - `input_object_hash`
  - `evidence_ids`
  - `generated_output_hash`
  - `model/provider redacted metadata`
- [ ] 如保存 redacted prompt preview，长度必须 `<= 4096 chars`；
- [ ] LLM 输出必须通过 schema validation；
- [ ] LLM draft 中每个 action step 必须被安全分级；
- [ ] LLM 失败时保留 deterministic template draft，不影响原流程。

### 外部云 LLM 授权与数据边界

本阶段允许调用外部云 LLM provider，但必须满足以下条件：

```text
M9_EXTENSIONS_ENABLED=true
RUNBOOK_LLM_GENERATION_ENABLED=true
LLM_PROVIDER != disabled
# 如果 LLM_PROVIDER 是外部云服务，还必须：
LLM_EXTERNAL_PROVIDER_ALLOWED=true
```

API 调用必须具备：

```text
runbook:review + runbook:llm_generate
```

如果 `LLM_PROVIDER` 是外部云服务，还必须具备：

```text
llm:invoke 或 ai:external
```

外部云 LLM 默认不允许：

```env
LLM_EXTERNAL_PROVIDER_ALLOWED=false
LLM_PROVIDER=disabled
```

强制规则：

- 调用前必须完成 redaction；
- 不保存 full raw prompt；
- prompt preview 如保存，必须是 redacted preview 且长度受限；
- audit 只记录 provider redacted metadata、prompt hash、input object hash、evidence IDs；
- LLM provider timeout / auth error / 5xx 只能让 LLM draft degraded，不影响 deterministic template draft。

### Action Step 分级

LLM draft 中所有 action step 必须被分类：

```text
read_only
diagnostic_only
approval_required
forbidden
unknown
```

规则：

- `read_only` / `diagnostic_only` 可以保留在 draft；
- `approval_required` 可以保留在 draft，但 approve 时必须二次确认，并要求 reviewer 具备 `runbook:review`；如果 action 涉及执行或变更，还必须具备对应 executor/action scope；
- `forbidden` / `unknown` 不允许进入 approved runbook，除非人工修改为 `read_only` / `diagnostic_only` / 合法的 `approval_required` 步骤；
- action classification 必须写入 draft metadata；
- approval API 必须拒绝包含 `forbidden` / `unknown` action 的 draft。

### 不做

- 不 approve；
- 不 publish；
- 不覆盖 deterministic draft；
- 不直接写入 RunbookVersion；
- 不触发 executor action；
- 不保存 full raw prompt。

### 建议文件

```text
packages/rag/runbook_llm_generator.py
packages/rag/runbook_prompt_builder.py
packages/rag/runbook_action_classifier.py
apps/api/routers/runbooks.py
apps/api/schemas/runbooks.py
packages/db/models.py
tests/unit/test_llm_runbook_generation.py
tests/unit/test_runbook_action_classifier.py
tests/integration/test_llm_runbook_draft_lifecycle.py
```

### 测试清单

```text
test_llm_runbook_generation_default_disabled
test_llm_runbook_generation_requires_m9_enabled
test_llm_runbook_prompt_uses_redacted_effective_config
test_llm_runbook_prompt_excludes_bearer_token
test_llm_runbook_prompt_excludes_password
test_llm_prompt_full_text_not_persisted
test_llm_prompt_preview_max_length
test_llm_prompt_metadata_contains_hash_only
test_llm_runbook_output_schema_validation
test_llm_runbook_creates_pending_review_draft
test_llm_runbook_does_not_publish_directly
test_llm_runbook_preserves_parent_draft_id
test_llm_draft_action_steps_are_classified
test_llm_draft_forbidden_action_blocks_approval
test_llm_draft_unknown_action_requires_manual_edit
test_llm_draft_approval_required_needs_second_confirmation
test_llm_external_provider_requires_llm_invoke_scope
test_llm_external_provider_timeout_degraded
test_llm_failure_keeps_deterministic_template
test_llm_draft_audit_log_created
```

### 验收标准

- [ ] `RUNBOOK_LLM_GENERATION_ENABLED=false` 时不会构造 LLM prompt；
- [ ] LLM draft 只能是 `pending_review`；
- [ ] LLM draft 必须有 evidence references；
- [ ] LLM draft 不允许直接成为 approved version；
- [ ] 不保存 full raw prompt；
- [ ] prompt preview 如存在必须脱敏且长度受限；
- [ ] draft action step 必须完成安全分级；
- [ ] 包含 `forbidden` / `unknown` action 的 draft 不可 approve；
- [ ] 包含 `approval_required` action 的 draft approve 时必须二次确认；
- [ ] 外部云 LLM 调用必须具备 `llm:invoke` 或 `ai:external` scope；
- [ ] audit log 记录 draft 生成动作；
- [ ] prompt、audit、state、debug log 中无 raw secret。

### 风险点

- LLM 可能生成不可执行或危险步骤；
- LLM 可能幻觉不存在的 metric、service、namespace；
- prompt 若未脱敏，可能泄露 backend token；
- 保存完整 prompt 会形成长期敏感数据风险。

### 回滚方案

```env
RUNBOOK_LLM_GENERATION_ENABLED=false
```

---

## PR 9.3: LLM Incident Diff Analysis

### 背景

M7 已有 deterministic runbook feedback。M9 可以加入 LLM diff analysis，用于从 incident 诊断结果、人工反馈、执行记录和现有 runbook 中发现差异，但输出只能是 `AmendmentDraft`。

### 范围

- [ ] 新增 `IncidentDiffAnalyzer`；
- [ ] 支持比较：
  - incident diagnosis report；
  - evidence IDs；
  - approved runbook version；
  - operator feedback；
  - action execution result；
  - deterministic feedback summary；
- [ ] 输出 `AmendmentDraft`；
- [ ] `status=pending_review`；
- [ ] amendment 类型至少包含：
  - `missing_step`
  - `outdated_metric`
  - `wrong_label_mapping`
  - `missing_rollback`
  - `unsafe_action_wording`
  - `insufficient_evidence`
- [ ] 可 approve/apply 的 amendment item 必须带 evidence references；
- [ ] 无 evidence 的建议只能作为 `reviewer_note` / `low_confidence_note`，不得进入 proposed patch；
- [ ] review 后才能 merge 到 runbook draft。

### 外部云 LLM 授权

Incident Diff 允许调用外部云 LLM provider，但 API 调用必须具备：

```text
runbook:review + incident:llm_diff
```

如果 `LLM_PROVIDER` 是外部云服务，还必须具备：

```text
llm:invoke 或 ai:external
```

外部云 LLM 调用还必须满足：

```env
LLM_EXTERNAL_PROVIDER_ALLOWED=true
```

证据不足时不得调用 LLM。

### 最低证据门槛

`IncidentDiffAnalyzer` 必须满足以下至少一项才可运行：

- incident 有 diagnosis report；
- incident 有至少 1 条 operator feedback；
- incident 有 action execution result；
- incident 有 linked approved runbook version；
- incident 有至少 `MIN_INCIDENT_DIFF_EVIDENCE_REFS` 条 evidence refs。

否则：

```text
return status=skipped_insufficient_evidence
do not call LLM
do not create AmendmentDraft
record metric llm_incident_diff_total{status="skipped_insufficient_evidence"}
```

### AmendmentDraft 状态机

`AmendmentDraft.status` 必须使用以下状态：

```text
pending_review
approved
rejected
applied
superseded
```

字段建议：

```text
approved_by
approved_at
applied_to_draft_id
applied_to_runbook_version_id
applied_at
```

语义：

- `pending_review`：等待人工 review；
- `approved`：reviewer 认可该 amendment，但尚未合并；
- `applied`：已合并进新的 RunbookDraft 或 RunbookVersion；
- `rejected`：拒绝；
- `superseded`：被新的 amendment 替代。

### 不做

- 不直接修改 approved runbook；
- 不直接生成 RunbookVersion；
- 不直接执行 remediation；
- 不作为 incident diagnosis 的必需步骤。

### 建议文件

```text
packages/rag/incident_diff.py
packages/rag/amendment_draft.py
apps/api/routers/runbooks.py
apps/api/schemas/runbooks.py
packages/db/models.py
migrations/versions/XXXX_amendment_drafts.py
tests/unit/test_incident_diff_analysis.py
tests/integration/test_amendment_draft_review.py
```

### 测试清单

```text
test_incident_diff_default_disabled
test_incident_diff_requires_m9_enabled
test_incident_diff_skips_without_minimum_evidence
test_incident_diff_does_not_call_llm_when_skipped
test_incident_diff_creates_amendment_draft
test_incident_diff_does_not_modify_approved_runbook
test_incident_diff_requires_evidence_refs
test_incident_diff_low_confidence_note_without_evidence
test_incident_diff_rejects_apply_item_without_evidence_refs
test_incident_diff_external_provider_requires_llm_invoke_scope
test_incident_diff_detects_missing_step
test_incident_diff_detects_outdated_metric
test_incident_diff_detects_missing_rollback
test_amendment_status_pending_to_approved
test_amendment_status_approved_to_applied
test_amendment_approved_does_not_mean_applied
test_incident_diff_audit_log_created
```

### 验收标准

- [ ] 关闭时无 LLM 调用；
- [ ] 证据不足时不调用 LLM；
- [ ] 开启且证据满足时只创建 `AmendmentDraft`；
- [ ] 不修改 existing RunbookVersion；
- [ ] `approved` 与 `applied` 语义分离；
- [ ] 可 approve/apply 的 amendment item 必须可追溯；
- [ ] 无 evidence 的 low confidence note 不得进入 applied patch；
- [ ] audit log 包含 actor、source、incident_id、runbook_version_id。

### 回滚方案

```env
LLM_INCIDENT_DIFF_ENABLED=false
```

---

## PR 9.4: Runbook web_search 安全封装

### 背景

Runbook web_search 可以补充外部文档、官方最佳实践和错误信息解释，但它会带来 SSRF、敏感信息外泄、来源不可控、内容不可复现和数据外发风险。因此必须先封装安全边界。

### 权限要求

web_search 是外部数据流出能力，不能只依赖 `runbook:review`。调用 web_search API 必须满足：

```text
runbook:review + runbook:web_search
```

### 范围

- [ ] 新增 `WebSearchProvider` 抽象；
- [ ] 新增 `RunbookWebContextBuilder`；
- [ ] 查询前执行 redaction：
  - token；
  - password；
  - private key；
  - auth header；
  - internal URL；
  - IP；
  - namespace；
  - service name，可按配置泛化；
- [ ] 禁止访问：
  - localhost；
  - 127.0.0.0/8；
  - ::1；
  - link-local；
  - metadata endpoint；
  - cluster internal domain；
  - private IP 直连；
  - non-http/https scheme；
- [ ] 默认只允许 HTTPS；
- [ ] `APP_ENV=production` 时 `RUNBOOK_WEB_SEARCH_ALLOWED_DOMAINS` 不能为空，否则 web_search 启用失败并返回 `config_error`；
- [ ] `RUNBOOK_WEB_SEARCH_ENABLED=true` 但 `RUNBOOK_WEB_SEARCH_PROVIDER=disabled` 时，必须返回 `config_error` / `degraded`，不得 fallback 到任何默认搜索 provider；
- [ ] `RUNBOOK_WEB_SEARCH_BLOCKED_DOMAINS` 优先级高于 allowlist；
- [ ] 每次 DNS resolution 后必须拒绝 private/link-local/metadata IP；
- [ ] 每次 redirect 后必须重新执行 URL safety + DNS resolution；
- [ ] 限制 redirect 次数；
- [ ] 保存 final_url；
- [ ] 限制响应大小；
- [ ] 不执行 JS；
- [ ] 不提交 cookie/header/token；
- [ ] 每条 web result 保存：
  - title；
  - original_url；
  - final_url；
  - retrieved_at；
  - snippet；
  - content_hash；
  - provider；
  - redaction_version；
- [ ] web result 只能进入 draft evidence；
- [ ] web result 不能直接进入 approved runbook；
- [ ] 支持 provider mock，保证测试可重复。

### 新增配置

```env
RUNBOOK_WEB_SEARCH_ENABLED=false
RUNBOOK_WEB_SEARCH_PROVIDER=disabled
RUNBOOK_WEB_SEARCH_TIMEOUT_SECONDS=10
RUNBOOK_WEB_SEARCH_MAX_RESULTS=5
RUNBOOK_WEB_SEARCH_REQUIRE_HTTPS=true
RUNBOOK_WEB_SEARCH_ALLOWED_DOMAINS=
RUNBOOK_WEB_SEARCH_BLOCKED_DOMAINS=
RUNBOOK_WEB_SEARCH_MAX_CONTENT_BYTES=1048576
RUNBOOK_WEB_SEARCH_CACHE_TTL_SECONDS=86400
RUNBOOK_WEB_SEARCH_MAX_REDIRECTS=3
```

### 不做

- 不实现通用浏览器；
- 不抓取需要登录的内部页面；
- 不把 web result 作为事实直接发布；
- 不替代 operator review；
- 不提交用户 cookie 或内部 token；
- 不抓取内网地址。

### 建议文件

```text
packages/rag/web_search_provider.py
packages/rag/runbook_web_context.py
packages/common/redaction.py
packages/common/backend_url_safety.py
apps/api/routers/runbooks.py
tests/unit/test_web_search_redaction.py
tests/unit/test_web_search_safety.py
tests/unit/test_web_search_source_traceability.py
tests/integration/test_runbook_web_context_draft.py
```

### 测试清单

```text
test_web_search_default_disabled
test_web_search_requires_m9_enabled
test_web_search_requires_runbook_web_search_scope
test_web_search_query_redacts_token
test_web_search_query_redacts_password
test_web_search_query_redacts_private_key
test_web_search_blocks_localhost
test_web_search_blocks_metadata_endpoint
test_web_search_blocks_cluster_internal_domain
test_web_search_blocks_private_ip
test_web_search_requires_https_by_default
test_web_search_production_requires_allowed_domains
test_web_search_enabled_with_provider_disabled_returns_config_error
test_web_search_does_not_fallback_to_default_provider
test_web_search_blocked_domains_override_allowed_domains
test_web_search_dns_resolution_private_ip_blocked
test_web_search_redirect_to_metadata_blocked
test_web_search_redirect_revalidates_final_url
test_web_search_result_has_source_url
test_web_search_result_has_final_url
test_web_search_result_has_retrieved_at
test_web_search_result_has_content_hash
test_web_search_response_size_limited
test_web_search_only_attaches_to_draft
test_web_search_does_not_publish_runbook
```

### 验收标准

- [ ] 默认关闭；
- [ ] 调用 API 需要 `runbook:review + runbook:web_search`；
- [ ] 查询前必须脱敏；
- [ ] SSRF 风险 URL 被拒绝；
- [ ] production allowlist 为空时不能启用 web_search；
- [ ] provider disabled 时不得 fallback 到默认 provider；
- [ ] redirect 和 DNS 解析后的最终地址仍必须通过安全校验；
- [ ] 来源可追溯；
- [ ] web result 只作为 draft evidence；
- [ ] web_search 失败不影响 deterministic runbook。

### 回滚方案

```env
RUNBOOK_WEB_SEARCH_ENABLED=false
```

---

## PR 9.5: TempoTraceBackend

### 背景

M0–M8 中 trace backend 主要面向 fixture / Jaeger。M9 增加 TempoTraceBackend，用于支持使用 Grafana Tempo 的生产环境。Tempo 必须显式 opt-in，且失败只能 degraded。

### 范围

- [ ] 新增 `TempoTraceBackend`；
- [ ] 对齐现有 trace backend protocol；
- [ ] 支持按 trace ID 查询；
- [ ] 支持按 service / time range 的基础查询；
- [ ] 集成 `RuntimeBackendAuthConfig`；
- [ ] 输出 `TraceEvidence`；
- [ ] Tempo 不可达时返回 degraded；
- [ ] auth 失败时返回 degraded，不泄露 secret；
- [ ] `TRACE_BACKEND=tempo` 且 `TRACE_ENABLED=true` 时才启用。

### Tempo Capability Detection

TempoTraceBackend 初始化时或首次查询时必须检测能力：

```text
supports_trace_by_id
supports_search
supports_service_filter
supports_traceql
```

规则：

- 如果只支持 trace by ID，则 service/time range 查询 degraded；
- 如果 search 不可用，不让整个 TraceTool failed；
- 如果 TraceQL 不可用，不影响 trace by ID；
- capability detection 结果写入 redacted backend metadata；
- capability detection 不得泄露 auth secret。

### 不做

- 不实现复杂 call graph 聚合；
- 不实现 Tempo metrics-generator 集成；
- 不改变 Jaeger 默认行为；
- 不自动发布 Tempo URL；
- 不把 fixture 当作 production trace backend。

### 建议文件

```text
packages/tools/trace_backends.py
packages/tools/traces.py
tests/unit/test_tempo_trace_backend.py
tests/integration/test_trace_tool_tempo_backend.py
```

### 测试清单

```text
test_tempo_backend_default_disabled
test_trace_backend_tempo_opt_in
test_tempo_query_trace_by_id_success
test_tempo_query_service_time_range_success
test_tempo_capability_detection_trace_by_id_only
test_tempo_search_unavailable_degrades_service_query
test_tempo_traceql_unavailable_does_not_fail_backend
test_tempo_unavailable_degraded
test_tempo_auth_error_degraded
test_tempo_no_raw_secret_in_evidence
test_trace_tool_uses_tempo_backend_when_configured
test_trace_tool_falls_back_to_degraded_when_tempo_down
```

### 验收标准

- [ ] `TRACE_BACKEND=tempo` 且 `TRACE_ENABLED=true` 才启用；
- [ ] Tempo trace evidence 与现有 TraceTool schema 兼容；
- [ ] Tempo 失败不阻塞 diagnosis；
- [ ] 部分能力不可用只降级对应查询模式；
- [ ] raw auth secret 不进入 AgentDeps、state、audit、log、prompt。

### 回滚方案

必须恢复 M9 上线前已验证 trace backend。本次已确认 M8 稳定 Jaeger，因此 rollout 文档必须记录：

```env
PRE_M9_TRACE_BACKEND=jaeger
PRE_M9_TRACE_ENABLED=true
```

回滚脚本必须先校验 `PRE_M9_TRACE_BACKEND` 非空且属于 `disabled | jaeger | tempo`，并校验 `PRE_M9_TRACE_ENABLED` 为合法 boolean，然后恢复：

```env
# validated non-empty PRE_M9_TRACE_BACKEND and boolean PRE_M9_TRACE_ENABLED
TRACE_BACKEND=${PRE_M9_TRACE_BACKEND}
TRACE_ENABLED=${PRE_M9_TRACE_ENABLED}
```

如果未来某环境在 M9 上线前没有已验证真实 trace backend：

```env
TRACE_ENABLED=false
TRACE_BACKEND=disabled
```

不得在通用 production 回滚脚本中硬编码：

```env
TRACE_BACKEND=jaeger
TRACE_BACKEND=fixture
```

---

## PR 9.6: Tempo Auto-discovery Enablement

### 背景

TempoTraceBackend 实现后，可以让 Discovery 自动识别 Tempo endpoint。但 production 中 backend URL 自动发现仍然必须 review-first，不能 auto_publish。

### 范围

- [ ] 扩展 `BackendEndpointDetector`；
- [ ] 支持识别 Tempo service / endpoint / ingress；
- [ ] 输出 `BackendEndpointCandidate(type=tempo)`；
- [ ] 不覆盖 `.env` / profile / active override；
- [ ] 不 auto_publish；
- [ ] 集成 BackendUrlSafetyValidator；
- [ ] 记录 evidence。

### Tempo Endpoint 状态机

Tempo endpoint candidate 的状态必须按以下规则确定：

```text
unsafe_url or invalid_url
  -> rejected

low_confidence or auth_unknown or missing_endpoint_evidence
  -> detected_only

url_safe and confidence >= threshold and evidence sufficient
  -> requires_review

operator explicitly publishes after review
  -> published
```

强制规则：

- production 中 Tempo backend URL 永不 auto_publish；
- detected_only 不进入 EffectiveConfig；
- requires_review 不进入 worker；
- 只有 published 才可被 worker 使用；
- manual env / profile / active override 永远优先于 discovery。

### 不做

- 不启用 TempoTraceBackend；
- 不自动切换 `TRACE_BACKEND=tempo`；
- 不在 production 启动时自动扫描，除非已有 Discovery schedule 明确开启。

### 建议文件

```text
packages/discovery/backend_endpoints.py
tests/unit/test_tempo_endpoint_detection.py
tests/integration/test_tempo_discovery_proposal.py
```

### 测试清单

```text
test_detect_tempo_service_endpoint
test_detect_tempo_ingress
test_tempo_discovery_default_disabled
test_tempo_discovery_requires_m9_enabled
test_tempo_endpoint_unsafe_url_rejected
test_tempo_endpoint_low_confidence_detected_only
test_tempo_endpoint_auth_unknown_detected_only
test_tempo_endpoint_safe_high_confidence_requires_review
test_tempo_endpoint_production_never_auto_publish
test_tempo_endpoint_does_not_override_env
test_tempo_endpoint_does_not_enter_worker_until_published
test_tempo_endpoint_has_evidence
```

### 验收标准

- [ ] Tempo discovery 默认关闭；
- [ ] production 中 Tempo URL 不 auto_publish；
- [ ] unsafe URL 被 rejected；
- [ ] 低置信或 auth unknown 只能 detected_only；
- [ ] 高置信安全 URL 只能 requires_review；
- [ ] 手动配置优先；
- [ ] discovery result 有 evidence 和 confidence。

### 回滚方案

```env
TEMPO_DISCOVERY_ENABLED=false
```

---

## PR 9.7: Grafana Webhook Parser Enhancement

### 背景

Grafana alerting payload 与 Alertmanager payload 不完全一致。当前 `_from_grafana_alert()` 已存在但需要增强。M9 需要支持 Grafana unified alerting 的常见字段，并保持 fingerprint / dedup 语义稳定。

### Grafana disabled 行为

当：

```env
GRAFANA_ALERT_INGEST_ENABLED=false
```

Grafana dedicated webhook endpoint 必须采用以下固定行为：

- 返回 `204 No Content`；
- 不创建 incident；
- 不 enqueue diagnosis；
- 不记录完整 payload；
- 记录 metric：`grafana_webhook_ignored_total{reason="disabled"}`；
- 可选记录轻量 audit/security event：`grafana.webhook.disabled`，但 details 中不得包含完整 payload。

选择 `204` 的原因：避免 Grafana 重试造成噪音，同时明确该 payload 被忽略。

### Grafana enabled 鉴权与入口保护

当：

```env
GRAFANA_ALERT_INGEST_ENABLED=true
```

Grafana dedicated webhook endpoint 必须满足：

- 默认实现 `HMAC signature + GRAFANA_WEBHOOK_SECRET_REF`；
- API key / shared token 仅作为兼容路径，不作为默认实现；
- `GRAFANA_WEBHOOK_SECRET_REF` 只能保存 secret reference，禁止把 raw secret 写入 DB / audit / log；
- 如果 `GRAFANA_ALERT_INGEST_ENABLED=true` 但 HMAC / API key / shared token 均未配置，则服务启动记录 warning，endpoint 返回 `503` 或 `config_error`，不创建 incident，不 enqueue diagnosis；
- 未授权请求返回 `401` 或 `403`，不创建 incident，不 enqueue diagnosis；
- malformed payload 返回 `400`，不 panic，不记录完整 payload；
- payload size 必须受 `GRAFANA_WEBHOOK_MAX_BYTES` 限制；
- endpoint 必须接入 rate limit；
- 鉴权失败、payload 过大、rate limit 命中均必须记录 metric，但不得记录完整 payload。

### 范围

- [ ] 增强 `_from_grafana_alert()`；
- [ ] 支持 Grafana unified alerting 字段：
  - `status`
  - `alerts`
  - `labels`
  - `annotations`
  - `startsAt`
  - `endsAt`
  - `generatorURL`
  - `dashboardURL`
  - `panelURL`
  - `silenceURL`
  - `ruleUID`
- [ ] 保留 `raw_labels`；
- [ ] 内部 ingestion marker 不参与 fingerprint；
- [ ] `dashboardURL`、`panelURL`、`ruleUID` 不参与 fingerprint；
- [ ] firing / resolved 映射到现有 incident lifecycle；
- [ ] 与 Alertmanager webhook / poll dedup 兼容；
- [ ] 通过 `GRAFANA_ALERT_INGEST_ENABLED` 控制；
- [ ] enabled 时必须鉴权；
- [ ] 限制 payload size；
- [ ] 接入 rate limit。

### Source 枚举策略

如果现有 `AlertSource` 已包含 `grafana`：

```text
source=grafana
ingestion_metadata.alert_format=grafana
```

如果现有 `AlertSource` 不包含 `grafana`：

```text
source=webhook
ingestion_metadata.alert_format=grafana
```

强制规则：

- 不新增 `alertmanager_poll` 这类兼容性破坏枚举；
- `raw_labels` 保持原样；
- fingerprint 不包含 `alert_format`；
- fingerprint 不包含 dashboardURL / panelURL / ruleUID / internal marker。

### Cross-source dedup 规则

Grafana / Alertmanager cross-source dedup 只允许基于：

```text
normalized raw_labels + existing ignore rules
```

不得使用以下字段作为跨 source fingerprint 输入：

```text
dashboardURL
panelURL
ruleUID
generatorURL
ingestion format
internal marker
```

### 不做

- 不默认启用 Grafana ingest；
- 不改变 Alertmanager parser；
- 不把 Grafana URL 作为 backend URL 使用；
- 不记录完整 disabled payload。

### 建议文件

```text
apps/api/schemas/alerts.py
apps/api/services/alert_service.py
tests/unit/test_grafana_alert_parser.py
tests/integration/test_grafana_webhook_ingest.py
```

### 测试清单

```text
test_grafana_ingest_default_disabled
test_grafana_disabled_returns_204
test_grafana_disabled_does_not_create_incident
test_grafana_disabled_does_not_log_full_payload
test_grafana_disabled_records_ignored_metric
test_grafana_webhook_requires_feature_flag
test_grafana_webhook_requires_auth_when_enabled
test_grafana_webhook_default_hmac_signature_required
test_grafana_webhook_enabled_without_auth_returns_503_config_error
test_grafana_webhook_rejects_invalid_signature
test_grafana_webhook_payload_size_limited
test_grafana_webhook_rate_limited
test_grafana_malformed_payload_returns_400_without_panic
test_grafana_unified_alert_firing_parsed
test_grafana_unified_alert_resolved_parsed
test_grafana_raw_labels_preserved
test_grafana_annotations_preserved
test_grafana_dashboard_url_preserved_as_metadata
test_grafana_panel_url_preserved_as_metadata
test_grafana_fingerprint_stable
test_grafana_dedup_with_alertmanager_when_normalized_labels_equivalent
test_grafana_does_not_dedup_when_normalized_labels_differ
test_grafana_rule_uid_not_used_as_cross_source_fingerprint_key
test_grafana_internal_metadata_excluded_from_fingerprint
test_grafana_internal_marker_excluded_from_fingerprint
```

### 验收标准

- [ ] Grafana alert 在鉴权通过后可创建 incident；
- [ ] 未授权 Grafana webhook 不创建 incident；
- [ ] enabled 但未配置鉴权时返回 503/config_error 且不创建 incident；
- [ ] payload 过大或 malformed 不创建 incident；
- [ ] resolved payload 可更新 incident；
- [ ] disabled 时固定返回 204 且不创建 incident；
- [ ] raw labels 不被覆盖；
- [ ] fingerprint 稳定；
- [ ] cross-source dedup 只基于 normalized raw_labels；
- [ ] 不影响 Alertmanager webhook/poll；
- [ ] feature flag 关闭时不记录完整 payload。

### 回滚方案

```env
GRAFANA_ALERT_INGEST_ENABLED=false
```

---

## PR 9.8: Semantic Runbook Search

### 背景

M6/M7 已要求 embedding provider 不可用时 approved runbook ingest 不能失败。M9 可以增强 runbook search，使其支持 semantic search，但 semantic search 只能作为增强能力，不能取代 keyword-only/chunk-only fallback。

### 范围

- [ ] 新增 `EmbeddingProvider` 抽象；
- [ ] 支持 `disabled` provider；
- [ ] 支持本地 `bge_zh` provider；
- [ ] 新增 `RunbookChunkEmbedding` 模型；
- [ ] Runbook ingest 支持 embedding optional；
- [ ] embedding 失败时仍写入 runbook chunk；
- [ ] embedding 生成异步化；
- [ ] search 支持：
  - keyword-only；
  - semantic-only；
  - hybrid；
- [ ] 搜索结果包含：
  - runbook_version_id；
  - chunk_id；
  - source_path；
  - score；
  - search_mode；
  - embedding_provider；
  - degraded warning；
- [ ] semantic search 由 `SEMANTIC_RUNBOOK_SEARCH_ENABLED` 控制。

### RunbookChunkEmbedding 模型

```text
RunbookChunkEmbedding
  id
  runbook_chunk_id
  provider
  model
  dimension
  embedding_vector
  vector_backend: pgvector
  text_hash
  redaction_version
  status: available | degraded | failed
  error_code
  created_at
```

规则：

- 一个 chunk 可以有多份 embedding；
- 默认存储后端为 PostgreSQL `pgvector`；如果项目尚未启用 pgvector，必须先降级为 keyword-only，不得用 JSONB 假装支持向量检索；
- `embedding_vector` 维度必须等于 `dimension`，不一致时该 embedding 标记为 `failed/degraded`；
- 必须建立幂等唯一约束：`runbook_chunk_id + provider + model + dimension + text_hash`；
- 不同 provider / model / dimension 不互相覆盖；
- `text_hash` 不一致时必须重新生成；
- embedding 失败不影响 RunbookChunk 写入；
- embedding input 必须经过 redaction；
- raw secret 不得进入 embedding input。

### Async Embedding Flow

Runbook approve 流程必须是：

```text
1. 写 RunbookVersion。
2. 写 RunbookChunk。
3. enqueue embedding job。
4. embedding 成功后 semantic search available。
5. embedding 失败时 semantic search degraded，keyword search 继续可用。
```

禁止 approve API 同步等待 external embedding provider。

### 不做

- 不要求 embedding provider 必须可用；
- 不把 embedding 失败作为 approve 失败；
- 不默认启用 external provider。

### 建议文件

```text
packages/rag/embedding_provider.py
packages/rag/runbook_ingest.py
packages/rag/embedding_jobs.py
packages/tools/runbook_search.py
packages/db/models.py
migrations/versions/XXXX_runbook_chunk_embeddings.py
tests/unit/test_semantic_runbook_search.py
tests/integration/test_embedding_fallback.py
```

### 测试清单

```text
test_semantic_search_default_disabled
test_embedding_provider_disabled_keyword_search_works
test_runbook_approve_succeeds_without_embedding
test_runbook_approve_does_not_wait_for_embedding_provider
test_embedding_job_failure_marks_semantic_search_degraded
test_embedding_failure_degrades_semantic_search
test_semantic_search_returns_chunk_id
test_semantic_search_returns_runbook_version_id
test_semantic_search_returns_source_path
test_hybrid_search_combines_keyword_and_vector
test_semantic_search_no_secret_in_embedding_input
test_embedding_dimension_mismatch_degraded
test_chunk_can_have_multiple_provider_embeddings
test_embedding_unique_key_prevents_duplicate_jobs
test_pgvector_dimension_mismatch_degraded
test_text_hash_change_triggers_reembedding
```

### 验收标准

- [ ] embedding disabled 时 keyword search 可用；
- [ ] embedding provider 不可用时 approved ingest 不失败；
- [ ] approve API 不同步等待 embedding provider；
- [ ] semantic search result 可追溯；
- [ ] embedding 输入不包含 secret；
- [ ] semantic search degraded 有明确 warning；
- [ ] migration 可 upgrade/downgrade；
- [ ] embedding job 幂等，不重复生成相同 provider/model/dimension/text_hash 的向量；
- [ ] vector 存储使用 pgvector，dimension mismatch 明确 degraded。

### 回滚方案

```env
SEMANTIC_RUNBOOK_SEARCH_ENABLED=false
EMBEDDING_PROVIDER=disabled
```

---

## PR 9.9: External Embedding Provider

### 背景

BGE-ZH 可以作为本地 embedding provider，但某些部署可能希望使用外部 embedding provider。该能力属于外部数据流出，必须可选、默认关闭、单独授权，并且不能接收 raw secret。

### 启用条件

external embedding provider 必须同时满足：

```env
M9_EXTENSIONS_ENABLED=true
SEMANTIC_RUNBOOK_SEARCH_ENABLED=true
EXTERNAL_EMBEDDING_PROVIDER_ENABLED=true
EMBEDDING_PROVIDER=external
```

配置写入或启用 external provider 需要 scope：

```text
config:write + embedding:external
```

### 范围

- [ ] 新增 `ExternalEmbeddingProvider`；
- [ ] 支持 provider endpoint 配置；
- [ ] 支持 auth secret reference；
- [ ] 集成 BackendUrlSafetyValidator；
- [ ] 输入文本先经过 redaction；
- [ ] timeout / retry / circuit breaker；
- [ ] provider failure 时 semantic search degraded；
- [ ] audit 中只记录 redacted provider metadata；
- [ ] 启用 external provider 时记录单独 audit event。

### Audit Event

启用 external provider 时必须记录：

```json
{
  "action": "embedding.external_provider.enabled",
  "resource_type": "embedding_provider",
  "details": {
    "provider_url_redacted": "...",
    "secret_ref": "...",
    "data_redaction_enabled": true,
    "approved_by_key_id": "..."
  }
}
```

不得记录 raw token、password、private key 或完整 embedding input。

### 不做

- 不默认启用；
- 不把 raw provider token 存入 DB；
- 不把 external provider 作为 approved ingest 的硬依赖；
- 不实现 provider-specific 管理 UI；
- 不绕过 redaction；
- 不绕过 BackendUrlSafetyValidator。

### 建议文件

```text
packages/rag/external_embedding_provider.py
packages/common/backend_auth.py
packages/common/backend_url_safety.py
apps/api/routers/config.py
tests/unit/test_external_embedding_provider.py
tests/integration/test_external_embedding_degraded.py
```

### 测试清单

```text
test_external_embedding_default_disabled
test_external_embedding_requires_m9_enabled
test_external_embedding_requires_semantic_search_enabled
test_external_embedding_requires_external_embedding_scope
test_external_embedding_rejects_unsafe_url
test_external_embedding_uses_secret_reference
test_external_embedding_redacts_input
test_external_embedding_timeout_degraded
test_external_embedding_auth_error_degraded
test_external_embedding_no_raw_secret_in_audit
test_external_embedding_enable_audit_event
test_external_embedding_provider_5xx_degraded
```

### 验收标准

- [ ] 默认关闭；
- [ ] 启用需要 `config:write + embedding:external`；
- [ ] unsafe endpoint 被拒绝；
- [ ] raw token 不进入 DB / audit / prompt / log；
- [ ] provider 失败不影响 keyword search；
- [ ] provider 失败不影响 runbook approve；
- [ ] 启用 external provider 有独立 audit event。

### 回滚方案

```env
EXTERNAL_EMBEDDING_PROVIDER_ENABLED=false
EMBEDDING_PROVIDER=disabled
```

---

## PR 9.10: M9 Runtime Metrics / Threat Model / E2E / Docs

### 背景

M9 涉及多个增强能力和多个外部依赖，必须有完整运行时指标、威胁模型、E2E 测试、上线文档和回滚文档。所有 M9 功能都必须能单独关闭，关闭后系统行为应回到 M8 完成态。

### 范围

- [ ] 汇总各 PR 已实现的 M9 runtime metrics；
- [ ] 补齐 dashboard / alert / docs，不把前序 PR 的安全指标全部后置到 PR 9.10；
- [ ] 新增 M9 threat model；
- [ ] 新增 M9 data flow 文档；
- [ ] 新增 M9 E2E 测试；
- [ ] 新增 M9 staging smoke sequence；
- [ ] 新增 M9 production rollout checklist；
- [ ] 新增 M9 rollback matrix；
- [ ] 新增 M9 security checklist；
- [ ] 更新 production checklist；
- [ ] 更新 operator runbook；
- [ ] 更新 `.env.example`；
- [ ] 更新 Helm / compose 示例配置，如果项目已有。

### Runtime Metrics 分工

每个 PR 必须实现自身 safety metric，不能等到 PR 9.10 才补：

```text
PR 9.1: m9_feature_flag_conflict_total
PR 9.2: llm_runbook_draft_total
PR 9.3: llm_incident_diff_total
PR 9.4: web_search_requests_total / web_search_blocked_total
PR 9.5: tempo_trace_queries_total / tempo_capability_detected
PR 9.7: grafana_webhook_ingest_total / grafana_webhook_ignored_total
PR 9.8/9.9: embedding_jobs_total / semantic_search_queries_total
```

PR 9.10 只负责汇总、dashboard、E2E、threat model 和文档。

### Runtime Metrics

建议指标：

```text
m9_feature_enabled{feature=...}
m9_feature_flag_conflict_total{feature=...}

llm_runbook_draft_total{status=...}
llm_incident_diff_total{status=...}

web_search_requests_total{status=...,reason=...}
web_search_blocked_total{reason=...}

tempo_trace_queries_total{status=...,mode=...}
tempo_capability_detected{capability=...,supported=...}

grafana_webhook_ingest_total{status=...}
grafana_webhook_ignored_total{reason=...}

semantic_search_queries_total{mode=...,status=...}
embedding_jobs_total{provider=...,status=...}

m9_secret_redaction_failures_total
```

### Threat Model / Data Flow 文档必须覆盖

```text
LLM prompt data flow
web_search query data flow
external embedding data flow
Tempo backend access data flow
Grafana webhook ingest data flow
secret redaction boundary
audit/log/state/prompt forbidden fields
rollback data flow
```

### 建议文件

```text
tests/e2e/test_m9_ai_extensions.py
tests/e2e/test_m9_tempo_grafana.py
tests/e2e/test_m9_semantic_search.py
docs/m9-rollout.md
docs/m9-threat-model.md
docs/m9-data-flow.md
docs/production-checklist.md
docs/operator-runbook.md
.env.example
```

### M9 Smoke Sequence

```text
1. APP_ENV=production，关闭所有 M9 feature flags，确认 M0–M8 全部 smoke 通过。
2. 开启 M9_EXTENSIONS_ENABLED=true，但所有子功能仍 false，确认无行为变化。
3. 开启 RUNBOOK_LLM_GENERATION_ENABLED=true，生成 LLM RunbookDraft，确认 status=pending_review。
4. 检查 LLM draft：不保存 full raw prompt；action step 已分级。
5. 开启 LLM_INCIDENT_DIFF_ENABLED=true，证据不足时确认 skipped，不调用 LLM。
6. 提供足够 evidence 后生成 AmendmentDraft，确认不修改 approved runbook。
7. 开启 RUNBOOK_WEB_SEARCH_ENABLED=true，确认查询脱敏、来源追溯、SSRF 拒绝。
8. 设置 TRACE_BACKEND=tempo，确认 TempoTraceBackend 正常或 degraded；再关闭 M9 global gate，确认既有 Jaeger 行为不被关闭。
9. 开启 TEMPO_DISCOVERY_ENABLED=true，确认 production Tempo endpoint 只进入 detected_only/requires_review，不 auto_publish。
10. 开启 GRAFANA_ALERT_INGEST_ENABLED=false，发送 Grafana payload，确认 204 ignored 且不创建 incident。
11. 开启 GRAFANA_ALERT_INGEST_ENABLED=true，先验证未授权请求被拒绝，再发送已鉴权 Grafana firing/resolved webhook，确认 fingerprint/dedup。
12. 开启 SEMANTIC_RUNBOOK_SEARCH_ENABLED=true，确认 embedding provider unavailable 时 keyword search 可用。
13. 开启 EXTERNAL_EMBEDDING_PROVIDER_ENABLED=true，确认 unsafe endpoint 被拒绝、不会试连 unsafe URL、token 泄露测试通过。
14. 检查 audit log：所有 M9 draft/search/discovery/parser 操作都有 source/request_id/actor/details。
15. 检查 logs/state/prompt/audit：无 token/password/private key/internal auth header 泄露。
16. 逐个关闭 feature flag，确认系统回到 M8 行为。
17. 执行总回滚，确认本次环境 `TRACE_BACKEND=jaeger`、`TRACE_ENABLED=true` 被恢复；通用脚本仍必须读取 `PRE_M9_TRACE_*`，不能硬编码 jaeger/fixture。
```

### 失败注入测试

```text
test_llm_provider_timeout_degraded
test_web_search_provider_timeout_degraded
test_web_search_redirect_to_metadata_blocked
test_tempo_partial_capability_degraded
test_grafana_malformed_payload_rejected_without_panic
test_embedding_provider_dimension_mismatch_degraded
test_external_embedding_provider_5xx_degraded
test_m9_secret_redaction_failure_blocks_external_call
```

### 验收标准

- [ ] 关闭所有 M9 flags 后，M8 行为不变；
- [ ] 所有 M9 能力都有 E2E；
- [ ] 所有 M9 能力都有 rollback switch；
- [ ] 所有 M9 写操作都有 audit；
- [ ] 所有 M9 外部请求都有 timeout；
- [ ] 所有 M9 外部请求都有 redaction / safety 校验；
- [ ] 任一 M9 能力失败不会阻塞基础 diagnosis；
- [ ] runtime metrics 可用于判断 M9 是否启用、降级或被阻断；
- [ ] threat model 明确禁止字段和数据流边界。

---

# 9. 数据模型建议

## 9.1 RunbookDraft 扩展

```text
RunbookDraft
  id
  draft_type: template | llm_generated | regenerated | amendment_merge
  status: pending_review | approved | rejected | superseded
  parent_draft_id
  supersedes_draft_id
  source_runbook_version_id
  created_by_key_id
  created_by_scopes
  evidence_refs
  source_refs
  action_classification_summary
  redacted_prompt_metadata
  redacted_prompt_preview
  created_at
  updated_at
```

约束：

- 不保存 full raw prompt；
- `redacted_prompt_preview` 如存在必须 `<= 4096 chars`；
- `action_classification_summary` 中存在 `forbidden` 或 `unknown` 时不可 approve。

## 9.2 AmendmentDraft

```text
AmendmentDraft
  id
  incident_id
  runbook_version_id
  status: pending_review | approved | rejected | applied | superseded
  amendment_type: missing_step | outdated_metric | wrong_label_mapping | missing_rollback | unsafe_action_wording | insufficient_evidence
  proposed_patch
  confidence
  evidence_refs
  created_by
  approved_by
  approved_at
  applied_to_draft_id
  applied_to_runbook_version_id
  applied_at
  created_at
```

## 9.3 WebSearchResultRef

```text
WebSearchResultRef
  id
  draft_id
  provider
  title
  original_url
  final_url
  retrieved_at
  snippet_hash
  content_hash
  redaction_applied
  redaction_version
  source_status
```

## 9.4 RunbookChunkEmbedding

```text
RunbookChunkEmbedding
  id
  runbook_chunk_id
  provider
  model
  dimension
  embedding_vector
  vector_backend: pgvector
  text_hash
  redaction_version
  status: available | degraded | failed
  error_code
  created_at
```

---

# 10. API 建议

## 10.1 LLM Runbook Draft

```http
POST /api/runbooks/{runbook_id}/drafts/llm
```

Required scope：

```text
runbook:review + runbook:llm_generate
```

如果 `LLM_PROVIDER` 是外部云服务，还必须具备：

```text
llm:invoke 或 ai:external
```

行为：

- feature flag disabled → 403；
- 创建 `RunbookDraft(status=pending_review, draft_type=llm_generated)`；
- 不保存 full raw prompt；
- 返回 `draft_id`。

## 10.2 Incident Diff

```http
POST /api/incidents/{incident_id}/runbook-diff
```

Required scope：

```text
runbook:review + incident:llm_diff
```

如果 `LLM_PROVIDER` 是外部云服务，还必须具备：

```text
llm:invoke 或 ai:external
```

行为：

- 证据不足 → 200 with `status=skipped_insufficient_evidence`；
- 证据足够 → 创建 `AmendmentDraft(status=pending_review)`；
- 不修改 RunbookVersion。

## 10.3 Web Context Preview

```http
POST /api/runbooks/web-context/preview
```

Required scope：

```text
runbook:review + runbook:web_search
```

行为：

- 返回 redacted query；
- 返回候选 sources；
- 不写 approved runbook；
- 不抓取 unsafe URL；
- production 中 `RUNBOOK_WEB_SEARCH_ALLOWED_DOMAINS` 为空时返回 `config_error`，不发起外部请求。

## 10.4 Semantic Search

```http
GET /api/runbooks/search?q=...&mode=keyword|semantic|hybrid
```

Required scope：

```text
runbook:read 或 runbook:review
```

行为：

- provider unavailable → fallback keyword；
- response 中包含 search_mode 和 degraded warning。

## 10.5 External Embedding Provider Config

```http
POST /api/config/embedding-provider/external
```

Required scope：

```text
config:write + embedding:external
```

行为：

- 写入 redacted provider metadata；
- 只保存 secret reference；
- 记录 `embedding.external_provider.enabled` audit；
- 写入前必须执行 `BackendUrlSafetyValidator`；
- unsafe URL 必须拒绝；
- 不发起真实 provider 试连，不测试认证有效性；
- 只保存 redacted provider metadata 和 secret reference。

---

# 11. 配置项建议

## 11.1 新环境 `.env.example` 安全示例

`.env.example` 面向新环境和默认安全启动，应展示 disabled trace 语义，不应把本次生产 rollout 的 Jaeger 基线当作所有环境默认值。

```env
APP_ENV=production
M9_EXTENSIONS_ENABLED=false
LLM_PROVIDER=disabled
LLM_EXTERNAL_PROVIDER_ALLOWED=false
TRACE_ENABLED=false
TRACE_BACKEND=disabled
RUNBOOK_WEB_SEARCH_ENABLED=false
RUNBOOK_WEB_SEARCH_PROVIDER=disabled
GRAFANA_ALERT_INGEST_ENABLED=false
SEMANTIC_RUNBOOK_SEARCH_ENABLED=false
EMBEDDING_PROVIDER=disabled
EXTERNAL_EMBEDDING_PROVIDER_ENABLED=false
```

## 11.2 本次 production rollout 配置建议

本次 production rollout 已确认 M8 有稳定 Jaeger，因此 rollout 记录和生产部署可使用以下配置。该配置不得复制为通用 `.env.example` 默认值。

```env
# M9 global
M9_EXTENSIONS_ENABLED=false

# LLM runbook generation
RUNBOOK_LLM_GENERATION_ENABLED=false
LLM_INCIDENT_DIFF_ENABLED=false
RUNBOOK_LLM_MAX_TOKENS=2048
RUNBOOK_LLM_TIMEOUT_SECONDS=30
MIN_INCIDENT_DIFF_EVIDENCE_REFS=1
# External cloud LLM is data egress and must be explicitly enabled.
LLM_PROVIDER=disabled
LLM_EXTERNAL_PROVIDER_ALLOWED=false

# Web search
RUNBOOK_WEB_SEARCH_ENABLED=false
RUNBOOK_WEB_SEARCH_PROVIDER=disabled
RUNBOOK_WEB_SEARCH_TIMEOUT_SECONDS=10
RUNBOOK_WEB_SEARCH_MAX_RESULTS=5
RUNBOOK_WEB_SEARCH_REQUIRE_HTTPS=true
RUNBOOK_WEB_SEARCH_ALLOWED_DOMAINS=
RUNBOOK_WEB_SEARCH_BLOCKED_DOMAINS=
RUNBOOK_WEB_SEARCH_MAX_CONTENT_BYTES=1048576
RUNBOOK_WEB_SEARCH_CACHE_TTL_SECONDS=86400
RUNBOOK_WEB_SEARCH_MAX_REDIRECTS=3

# Trace
# This rollout has a stable M8 Jaeger baseline. Do not overwrite it unless testing Tempo opt-in.
TRACE_ENABLED=true
TRACE_BACKEND=jaeger
PRE_M9_TRACE_BACKEND=jaeger
PRE_M9_TRACE_ENABLED=true
TEMPO_DISCOVERY_ENABLED=false
TEMPO_URL=
TEMPO_AUTH_SECRET_REF=

# Grafana alert ingest
GRAFANA_ALERT_INGEST_ENABLED=false
# Default enabled auth mode is HMAC + secret reference; API key/shared token are compatibility paths.
GRAFANA_WEBHOOK_AUTH_MODE=hmac
GRAFANA_WEBHOOK_SECRET_REF=
GRAFANA_WEBHOOK_MAX_BYTES=1048576

# Semantic search
SEMANTIC_RUNBOOK_SEARCH_ENABLED=false
EMBEDDING_PROVIDER=disabled
EXTERNAL_EMBEDDING_PROVIDER_ENABLED=false
RUNBOOK_EMBEDDING_VECTOR_BACKEND=pgvector
EXTERNAL_EMBEDDING_URL=
EXTERNAL_EMBEDDING_SECRET_REF=
EMBEDDING_TIMEOUT_SECONDS=10
```

说明：

- `TRACE_BACKEND=fixture` 仅用于 local / CI / demo / 显式 emergency fail-closed，不作为 production 正常 observability backend。
- 本次 M9 rollout 已确认 M8 稳定 Jaeger，因此 `PRE_M9_TRACE_BACKEND=jaeger`、`PRE_M9_TRACE_ENABLED=true` 必须写入 rollout 记录。
- production 中如没有真实 trace backend，应使用 `TRACE_ENABLED=false` 或 `TRACE_BACKEND=disabled`，而不是 fixture。
- `.env.example` 必须展示 `TRACE_ENABLED=false`、`TRACE_BACKEND=disabled` 作为新环境安全示例；本次 production rollout 才记录并使用 `TRACE_ENABLED=true`、`TRACE_BACKEND=jaeger`、`PRE_M9_TRACE_BACKEND=jaeger`、`PRE_M9_TRACE_ENABLED=true`。

---

# 12. M9 风险清单

| 风险 | 等级 | 影响 | 缓解 |
|---|---|---|---|
| LLM 生成错误 Runbook | HIGH | 误导 operator | 只能 pending_review，必须 evidence refs |
| LLM 生成危险 action | HIGH | 误操作风险 | action step classification，forbidden/unknown 阻止 approve |
| LLM 泄露 secret | CRITICAL | 凭据泄露 | prompt redaction，不保存 full raw prompt |
| 外部云 LLM 数据外发 | HIGH | incident/runbook/context 外发 | `llm:invoke`/`ai:external` scope、redaction、audit、timeout |
| web_search SSRF | CRITICAL | 内网探测 | URL safety、denylist、redirect 检查 |
| web_search 数据外发 | HIGH | 内部信息泄露 | `runbook:web_search` scope、redaction、audit |
| web_search 结果不可靠 | MEDIUM | Runbook 质量下降 | source trace + review required |
| Tempo endpoint 误发布 | HIGH | worker 访问错误后端 | production requires_review/detected_only，不 auto_publish |
| Tempo API 能力差异 | MEDIUM | trace 查询失败 | capability detection，部分降级 |
| Grafana dedup 错误 | HIGH | incident 重复或漏恢复 | normalized raw_labels-only fingerprint regression tests |
| Grafana disabled payload 泄露 | MEDIUM | 日志泄露 | disabled 时 204 ignored，不记录完整 payload |
| Grafana webhook 伪造 | HIGH | 伪造 incident / 队列噪音 | enabled 时鉴权、payload size limit、rate limit |
| embedding provider 不可用 | MEDIUM | semantic search degraded | keyword-only fallback |
| external embedding 数据泄露 | HIGH | 内部文本外发 | `embedding:external` scope、redaction、audit |
| M9 开关遗漏 | HIGH | production 意外启用 | M9 global gate + conflict metrics |
| M9 失败阻塞基础诊断 | HIGH | 可用性下降 | degraded-only policy |
| trace rollback 硬编码错误 backend | HIGH | 回滚后访问错误后端 | 本次恢复 `PRE_M9_TRACE_BACKEND=jaeger`，通用脚本读取 `PRE_M9_TRACE_*` 且校验非空合法 |

---

# 13. 总回滚策略

M9 总回滚不得在通用脚本中硬编码 `TRACE_BACKEND=jaeger` 或 `TRACE_BACKEND=fixture`。必须恢复 M9 上线前已验证值。

本次已确认 M8 稳定 trace backend 为 Jaeger，因此 rollout 记录必须包含：

```env
PRE_M9_TRACE_BACKEND=jaeger
PRE_M9_TRACE_ENABLED=true
```

回滚脚本必须校验 `PRE_M9_TRACE_BACKEND` 非空且属于合法枚举，`PRE_M9_TRACE_ENABLED` 为合法 boolean；校验失败时 fail closed，不得写入空 backend。

```env
M9_EXTENSIONS_ENABLED=false

RUNBOOK_LLM_GENERATION_ENABLED=false
RUNBOOK_WEB_SEARCH_ENABLED=false
LLM_INCIDENT_DIFF_ENABLED=false

TRACE_BACKEND=${PRE_M9_TRACE_BACKEND}
TRACE_ENABLED=${PRE_M9_TRACE_ENABLED}
TEMPO_DISCOVERY_ENABLED=false
GRAFANA_ALERT_INGEST_ENABLED=false

SEMANTIC_RUNBOOK_SEARCH_ENABLED=false
EMBEDDING_PROVIDER=disabled
EXTERNAL_EMBEDDING_PROVIDER_ENABLED=false
```

如果 M9 上线前没有已验证真实 trace backend：

```env
TRACE_ENABLED=false
TRACE_BACKEND=disabled
```

fixture 只能用于：

```text
local
CI
demo
explicit emergency fail-closed mode
```

fixture 不能作为 production 正常观测 backend。

---

# 14. 最终验收标准

M9 完成时必须满足：

- [ ] production 默认仍是安全关闭状态；
- [ ] `M9_EXTENSIONS_ENABLED=false` 时系统行为等同 M8；
- [ ] 子开关冲突不启用功能、不导致启动失败、有 warning/metric；
- [ ] 外部云 LLM 默认不允许，必须显式 `LLM_EXTERNAL_PROVIDER_ALLOWED=true` 才可调用；
- [ ] LLM Runbook 只能生成 pending draft；
- [ ] LLM Runbook 不保存 full raw prompt；
- [ ] LLM Runbook action step 已分级；
- [ ] forbidden / unknown action 不可 approve；
- [ ] LLM incident diff 证据不足时不调用 LLM；
- [ ] LLM incident diff 只能生成 AmendmentDraft；
- [ ] AmendmentDraft 的 approved 与 applied 状态分离；
- [ ] web_search 需要 `runbook:review + runbook:web_search`；
- [ ] web_search 结果只能作为 draft evidence；
- [ ] TempoTraceBackend opt-in 才启用；
- [ ] Tempo capability detection 可部分降级；
- [ ] Tempo discovery 不 auto_publish；
- [ ] Grafana ingest feature flag 控制；
- [ ] Grafana disabled 时固定 204 ignored，不创建 incident；
- [ ] Grafana enabled 时默认 HMAC 鉴权，未配置鉴权返回 503/config_error；
- [ ] Grafana / Alertmanager cross-source dedup 只基于 normalized raw_labels；
- [ ] semantic search 可降级为 keyword-only；
- [ ] approved runbook ingest 不等待 embedding provider；
- [ ] external embedding provider 默认关闭；
- [ ] external embedding provider 需要 `config:write + embedding:external`；
- [ ] 所有 M9 写操作有 audit；
- [ ] 所有 M9 外部请求有 timeout；
- [ ] 所有 M9 路径通过 secret leakage tests；
- [ ] 所有 M9 功能可独立回滚；
- [ ] 总回滚恢复 `PRE_M9_TRACE_BACKEND` 或 disabled，不硬编码 jaeger/fixture。

---

# 15. 给 Agent 的执行提示词

```text
你现在要基于 sre-agent 当前代码库实现 M9: Controlled AI & Observability Extensions。

执行要求：
1. 严格按照 docs/m9-execution-plan.md 的 PR 顺序执行。
2. 先实现 PR 9.1 feature gate，不得直接实现 LLM/web_search/Tempo/Grafana。
3. 所有 M9 能力 production 默认 disabled。
4. M9_EXTENSIONS_ENABLED=false 时，所有 M9 子能力强制 disabled；子开关残留 true 只记录 warning/metric，不启动功能。
5. 不得在通用生产总回滚逻辑中硬编码 TRACE_BACKEND=jaeger 或 TRACE_BACKEND=fixture；本次 rollout 已确认 PRE_M9_TRACE_BACKEND=jaeger、PRE_M9_TRACE_ENABLED=true，必须从 PRE_M9_TRACE_* 读取并校验后恢复。
6. TRACE_BACKEND=fixture 只能用于 local/CI/demo/显式 emergency fail-closed，不作为 production 正常 observability backend。
7. 本文件是 M9 实现参照，必须与 M0–M8 主施工文档共同使用；不得绕过 M8 release gate；如文件路径与当前代码库不一致，先定位等价模块，不得擅自新建重复模块。
8. 外部云 LLM 默认不允许；只有 LLM_EXTERNAL_PROVIDER_ALLOWED=true、显式 LLM_PROVIDER、对应 feature flag 和 llm:invoke/ai:external scope 同时满足时才能调用。
9. 所有 LLM/web_search/external provider 输入必须先经过 redaction。
8. LLM Runbook 只能创建 RunbookDraft(status=pending_review)，不得 publish。
9. 不得保存 full raw prompt，只能保存 redacted metadata/hash；redacted prompt preview 如存在必须限制长度。
10. LLM draft 中所有 action step 必须分类为 read_only/diagnostic_only/approval_required/forbidden/unknown；forbidden/unknown 不可 approve。
11. LLM incident diff 证据不足时必须 skipped，不得调用 LLM。
12. LLM incident diff 只能创建 AmendmentDraft，不得修改 approved RunbookVersion。
13. AmendmentDraft 必须区分 approved 和 applied。
14. web_search 需要 runbook:review + runbook:web_search scope。
15. RUNBOOK_WEB_SEARCH_ENABLED=true 但 RUNBOOK_WEB_SEARCH_PROVIDER=disabled 时，必须 config_error/degraded，不得 fallback 到默认 provider。
16. web_search 结果只能作为 draft evidence，必须保存 source URL、final URL、retrieved_at、hash。
16. Tempo discovery 在 production 中只能 detected_only/requires_review/rejected，不得 auto_publish。
17. TempoTraceBackend 必须实现 capability detection，部分能力不可用只能降级对应查询模式。
18. Grafana webhook disabled 时固定返回 204 ignored，不创建 incident，不记录完整 payload。
19. Grafana webhook enabled 时默认实现 HMAC + GRAFANA_WEBHOOK_SECRET_REF；未配置任何鉴权方式时 endpoint 返回 503/config_error，不创建 incident；同时必须限制 payload size、接入 rate limit。
20. Grafana webhook parser 必须保持 raw_labels，不得让 internal marker/dashboardURL/panelURL/ruleUID 参与 fingerprint.
20. embedding provider 失败只能让 semantic search degraded，不得影响 approved runbook ingest。
21. Runbook approve 不得同步等待 embedding provider，必须先写 chunk 后 enqueue embedding job。
22. external embedding provider 需要 config:write + embedding:external scope，且必须记录独立 audit event。
23. 每个 PR 必须包含 unit tests、必要的 integration tests、audit tests、secret leakage tests、failure injection tests。
25. 每个 PR 必须有 rollback switch。
25. 修改完成后运行 ruff、mypy、pytest，并列出新增测试、风险点和回滚方式。
```

---

# 16. 推荐落地方式

建议不要一次性实现完整 M9，而是分四批上线：

## M9A: Feature Gate + AI Draft

包含：

- PR 9.1；
- PR 9.2；
- PR 9.3。

目标：建立开关和 LLM draft 边界，让 LLM 只参与 draft，不接触 publish/apply。

## M9B: Web Context

包含：

- PR 9.4。

目标：建立 web_search 安全边界，不急着和 LLM 深度耦合。

## M9C: Trace / Alert Ingest

包含：

- PR 9.5；
- PR 9.6；
- PR 9.7。

目标：支持 Tempo 和 Grafana，但默认关闭，production review-first。

## M9D: Runbook Search / External Embedding / E2E

包含：

- PR 9.8；
- PR 9.9；
- PR 9.10。

目标：支持 semantic search 和 external embedding，同时保证 fallback、审计、指标、E2E 和回滚。
