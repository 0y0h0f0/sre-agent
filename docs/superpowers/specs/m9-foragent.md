# sre-agent M9 Agent 执行计划

## 1. 阶段定位

M9 是 `sre-agent` 在 M8 Testing & Docs release gate 之后的受控增强阶段。

M9 不替换 M0–M8 已完成的确定性诊断、安全发布、配置合并、审计、回滚和 Runbook 审核能力，只在现有安全边界内增加 AI、Web 上下文、Tempo、Grafana 和语义搜索能力。

## 2. 总目标

M9 需要完成以下能力：

1. 增加 M9 全局 feature gate；
2. 支持 LLM 生成 Runbook 草稿；
3. 支持 LLM 分析 incident 与 Runbook 的差异；
4. 支持受控的 Runbook web_search；
5. 支持 Tempo trace backend；
6. 支持 Tempo endpoint discovery；
7. 增强 Grafana webhook ingest；
8. 支持 semantic runbook search；
9. 支持 external embedding provider；
10. 补齐 runtime metrics、E2E、threat model、data flow、rollout 和 rollback 文档。

## 3. 执行原则

### 3.1 默认关闭

production 中所有 M9 能力默认关闭。

```env
M9_EXTENSIONS_ENABLED=false
RUNBOOK_LLM_GENERATION_ENABLED=false
LLM_INCIDENT_DIFF_ENABLED=false
RUNBOOK_WEB_SEARCH_ENABLED=false
TEMPO_DISCOVERY_ENABLED=false
GRAFANA_ALERT_INGEST_ENABLED=false
SEMANTIC_RUNBOOK_SEARCH_ENABLED=false
EXTERNAL_EMBEDDING_PROVIDER_ENABLED=false
```

### 3.2 只增强，不接管

M9 不允许改变以下 M0–M8 不变量：

* worker 只读取 `published EffectiveConfigVersion`；
* production backend URL discovery 永不 auto_publish；
* raw backend secret 不进入 DB、audit、log、prompt、state；
* `raw_labels` 是 fingerprint 的原始输入；
* `LLM_PROVIDER=disabled` 路径必须可运行；
* embedding provider 失败不得阻塞 approved runbook ingest；
* Discovery 失败不阻塞 agent 启动；
* 已发布配置 stale 只 warning，不自动 hard fail。

### 3.3 LLM 只能生成草稿

LLM 相关能力只能生成：

```text
RunbookDraft(status=pending_review)
AmendmentDraft(status=pending_review)
```

LLM 不允许：

* 自动 approve；
* 自动 publish；
* 自动 apply amendment；
* 自动执行 remediation；
* 直接修改 approved runbook。

### 3.4 外部调用必须受控

所有外部调用必须具备：

* feature flag；
* timeout；
* redaction；
* audit 或 metric；
* error degraded；
* secret leakage test。

### 3.5 回滚必须可独立执行

每个 M9 子能力必须有独立回滚开关。

总回滚不得硬编码 `TRACE_BACKEND=jaeger` 或 `TRACE_BACKEND=fixture`，必须读取：

```env
PRE_M9_TRACE_BACKEND
PRE_M9_TRACE_ENABLED
```

---

## 4. Agent 执行约束

agent 执行时必须遵守：

1. 先搜索当前代码库已有模块，再修改；
2. 如果文档建议路径不存在，必须定位等价模块；
3. 不得擅自新建重复模块；
4. 不得替换 M0–M8 已有核心实现；
5. 每个 PR 必须单独提交、单独测试、单独回滚；
6. 每个 PR 必须包含测试；
7. 每个 PR 不得提前实现后续 PR 的范围；
8. 每个 PR 完成后必须输出：

   * 修改文件；
   * 新增测试；
   * 已运行测试；
   * 剩余风险；
   * 回滚方式。

---

## 5. 推荐执行顺序

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

PR 9.10 Runtime Metrics / Threat Model / E2E / Docs 最后执行
```

分批上线建议：

| 批次  | 内容              | 目标                                              |
| --- | --------------- | ----------------------------------------------- |
| M9A | PR 9.1、9.2、9.3  | Feature gate + LLM draft + LLM diff             |
| M9B | PR 9.4          | web_search 安全封装                                 |
| M9C | PR 9.5、9.6、9.7  | Tempo + Grafana                                 |
| M9D | PR 9.8、9.9、9.10 | Semantic search + external embedding + E2E docs |

---

# 6. PR 9.1：M9 Feature Gate 与基础不变量

## 6.1 目标

建立 M9 总开关、子开关、trace backend 语义、权限 scope 和冲突处理。

## 6.2 建议修改文件

```text
packages/common/settings.py
packages/common/feature_flags.py
apps/api/dependencies.py
docs/m9-rollout.md
.env.example
```

## 6.3 建议测试文件

```text
tests/unit/test_m9_feature_flags.py
tests/unit/test_trace_backend_settings.py
```

## 6.4 实现内容

新增配置：

```env
M9_EXTENSIONS_ENABLED=false

RUNBOOK_LLM_GENERATION_ENABLED=false
LLM_INCIDENT_DIFF_ENABLED=false
RUNBOOK_WEB_SEARCH_ENABLED=false

TRACE_ENABLED=false
TRACE_BACKEND=disabled

TEMPO_DISCOVERY_ENABLED=false
GRAFANA_ALERT_INGEST_ENABLED=false

SEMANTIC_RUNBOOK_SEARCH_ENABLED=false
EMBEDDING_PROVIDER=disabled
EXTERNAL_EMBEDDING_PROVIDER_ENABLED=false
```

新增 `TRACE_BACKEND` 枚举：

```text
disabled
fixture
jaeger
tempo
```

新增 `EMBEDDING_PROVIDER` 枚举：

```text
disabled
bge_zh
external
```

新增权限 scope：

```text
runbook:read
runbook:web_search
runbook:llm_generate
incident:llm_diff
llm:invoke
ai:external
embedding:external
```

实现 feature flag 优先级：

```text
M9_EXTENSIONS_ENABLED=false
  -> 强制关闭所有 M9 子能力
  -> 不调用 LLM
  -> 不调用 web_search
  -> 不启用 Tempo
  -> 不启用 Tempo discovery
  -> 不接收 Grafana ingest
  -> 不启用 semantic search
  -> 不调用 external embedding provider
```

特殊规则：

```text
M9_EXTENSIONS_ENABLED=false + TRACE_BACKEND=jaeger
  -> 保持 M8 已验证 Jaeger 行为
```

冲突处理：

```text
global disabled + 子开关 true
  -> 不启用子功能
  -> 服务继续启动
  -> 记录 startup warning
  -> 记录 metric
  -> 不抛 fatal error
```

metric：

```text
m9_feature_flag_conflict_total{feature="..."}
```

## 6.5 必测项

```text
test_m9_extensions_default_disabled
test_m9_global_disabled_forces_subfeatures_disabled
test_m9_subfeature_true_with_global_false_records_warning
test_m9_subfeature_true_with_global_false_records_metric
test_trace_backend_accepts_disabled_fixture_jaeger_tempo
test_fixture_trace_backend_rejected_as_normal_production_backend
test_trace_backend_disabled_means_trace_tool_degraded
test_m9_global_disabled_does_not_disable_existing_jaeger
test_m9_global_disabled_forces_tempo_degraded
test_tempo_trace_conflict_metric_recorded
```

## 6.6 验收标准

* [ ] M9 子能力 production 默认全部关闭；
* [ ] global gate 关闭时所有 M9 子能力强制关闭；
* [ ] 子开关冲突不导致服务启动失败；
* [ ] 冲突有 warning 和 metric；
* [ ] `TRACE_BACKEND=fixture` 不作为 production 正常 trace backend；
* [ ] `TRACE_BACKEND=disabled` 或 `TRACE_ENABLED=false` 时 TraceTool degraded；
* [ ] M9 global gate 不关闭 M8 已验证 Jaeger；
* [ ] `.env.example` 使用 `TRACE_ENABLED=false`、`TRACE_BACKEND=disabled`。

## 6.7 回滚

```env
M9_EXTENSIONS_ENABLED=false
```

---

# 7. PR 9.2：LLM Runbook Draft Generation

## 7.1 目标

支持 LLM 生成 Runbook 草稿，但只能生成 `pending_review` 草稿，不能直接发布。

## 7.2 建议修改文件

```text
packages/rag/runbook_llm_generator.py
packages/rag/runbook_prompt_builder.py
packages/rag/runbook_action_classifier.py
apps/api/routers/runbooks.py
apps/api/schemas/runbooks.py
packages/db/models.py
```

## 7.3 建议测试文件

```text
tests/unit/test_llm_runbook_generation.py
tests/unit/test_runbook_action_classifier.py
tests/integration/test_llm_runbook_draft_lifecycle.py
```

## 7.4 启用条件

```env
M9_EXTENSIONS_ENABLED=true
RUNBOOK_LLM_GENERATION_ENABLED=true
LLM_PROVIDER != disabled
```

如果使用外部云 LLM，还必须满足：

```env
LLM_EXTERNAL_PROVIDER_ALLOWED=true
```

API scope：

```text
runbook:review + runbook:llm_generate
```

外部云 LLM 额外 scope：

```text
llm:invoke 或 ai:external
```

## 7.5 实现内容

新增：

```text
LLMRunbookGenerator
RunbookPromptBuilder
RunbookActionClassifier
```

prompt 输入只允许使用 redacted context：

* approved runbook chunks；
* incident evidence summary；
* deterministic template draft；
* capability gaps；
* redacted EffectiveConfig。

输出：

```text
RunbookDraft
  draft_type=llm_generated
  status=pending_review
  parent_draft_id
  evidence_refs
  redacted_prompt_metadata
  action_classification_summary
```

禁止保存：

```text
full raw prompt
raw token
password
private key
auth header
backend secret
```

允许保存 metadata：

```text
prompt_template_id
prompt_template_version
redaction_version
input_object_hash
evidence_ids
generated_output_hash
model/provider redacted metadata
```

如保存 prompt preview：

```text
必须 redacted
长度 <= 4096 chars
```

action step 分类：

```text
read_only
diagnostic_only
approval_required
forbidden
unknown
```

approve 规则：

```text
forbidden / unknown -> 不允许 approve
approval_required -> approve 时必须二次确认
```

## 7.6 必测项

```text
test_llm_runbook_generation_default_disabled
test_llm_runbook_generation_requires_m9_enabled
test_llm_runbook_prompt_uses_redacted_effective_config
test_llm_runbook_prompt_excludes_bearer_token
test_llm_runbook_prompt_excludes_password
test_llm_prompt_full_text_not_persisted
test_llm_prompt_preview_max_length
test_llm_prompt_metadata_contains_hash_only
test_llm_runbook_creates_pending_review_draft
test_llm_runbook_does_not_publish_directly
test_llm_draft_action_steps_are_classified
test_llm_draft_forbidden_action_blocks_approval
test_llm_draft_unknown_action_requires_manual_edit
test_llm_external_provider_requires_llm_invoke_scope
test_llm_external_provider_timeout_degraded
test_llm_failure_keeps_deterministic_template
test_llm_draft_audit_log_created
```

## 7.7 验收标准

* [ ] disabled 时不构造 LLM prompt；
* [ ] LLM draft 只能是 `pending_review`；
* [ ] LLM draft 不直接生成 RunbookVersion；
* [ ] 不保存 full raw prompt；
* [ ] prompt preview 脱敏且限长；
* [ ] action step 完成安全分级；
* [ ] forbidden / unknown action 不可 approve；
* [ ] 外部云 LLM 必须双重 opt-in；
* [ ] audit、log、state、prompt 中无 raw secret。

## 7.8 回滚

```env
RUNBOOK_LLM_GENERATION_ENABLED=false
```

---

# 8. PR 9.3：LLM Incident Diff Analysis

## 8.1 目标

支持 LLM 分析 incident 与 approved runbook 的差异，只生成 `AmendmentDraft`。

## 8.2 建议修改文件

```text
packages/rag/incident_diff.py
packages/rag/amendment_draft.py
apps/api/routers/runbooks.py
apps/api/schemas/runbooks.py
packages/db/models.py
migrations/versions/XXXX_amendment_drafts.py
```

## 8.3 建议测试文件

```text
tests/unit/test_incident_diff_analysis.py
tests/integration/test_amendment_draft_review.py
```

## 8.4 启用条件

```env
M9_EXTENSIONS_ENABLED=true
LLM_INCIDENT_DIFF_ENABLED=true
```

API scope：

```text
runbook:review + incident:llm_diff
```

外部云 LLM 额外要求：

```text
LLM_EXTERNAL_PROVIDER_ALLOWED=true
llm:invoke 或 ai:external
```

## 8.5 最低证据门槛

满足以下任一条件才可运行：

* incident 有 diagnosis report；
* incident 有 operator feedback；
* incident 有 action execution result；
* incident 有 linked approved runbook version；
* incident 有至少 `MIN_INCIDENT_DIFF_EVIDENCE_REFS` 条 evidence refs。

否则：

```text
return status=skipped_insufficient_evidence
do not call LLM
do not create AmendmentDraft
record metric llm_incident_diff_total{status="skipped_insufficient_evidence"}
```

## 8.6 实现内容

新增：

```text
IncidentDiffAnalyzer
AmendmentDraft
```

AmendmentDraft 状态：

```text
pending_review
approved
rejected
applied
superseded
```

amendment 类型：

```text
missing_step
outdated_metric
wrong_label_mapping
missing_rollback
unsafe_action_wording
insufficient_evidence
```

强制规则：

* 只创建 `AmendmentDraft(status=pending_review)`；
* 不修改 approved RunbookVersion；
* 可 apply 的 item 必须有 evidence refs；
* 无 evidence 的建议只能作为 reviewer note 或 low confidence note；
* `approved` 不等于 `applied`。

## 8.7 必测项

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
test_amendment_status_pending_to_approved
test_amendment_status_approved_to_applied
test_amendment_approved_does_not_mean_applied
test_incident_diff_audit_log_created
```

## 8.8 回滚

```env
LLM_INCIDENT_DIFF_ENABLED=false
```

---

# 9. PR 9.4：Runbook web_search Safety Wrapper

## 9.1 目标

支持安全 web_search，作为 Runbook draft evidence，不直接进入 approved runbook。

## 9.2 建议修改文件

```text
packages/rag/web_search_provider.py
packages/rag/runbook_web_context.py
packages/common/redaction.py
packages/common/backend_url_safety.py
apps/api/routers/runbooks.py
```

## 9.3 建议测试文件

```text
tests/unit/test_web_search_redaction.py
tests/unit/test_web_search_safety.py
tests/unit/test_web_search_source_traceability.py
tests/integration/test_runbook_web_context_draft.py
```

## 9.4 启用条件

```env
M9_EXTENSIONS_ENABLED=true
RUNBOOK_WEB_SEARCH_ENABLED=true
RUNBOOK_WEB_SEARCH_PROVIDER != disabled
```

API scope：

```text
runbook:review + runbook:web_search
```

production 额外要求：

```text
RUNBOOK_WEB_SEARCH_ALLOWED_DOMAINS 非空
```

## 9.5 配置项

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

## 9.6 实现内容

新增：

```text
WebSearchProvider
RunbookWebContextBuilder
```

查询前必须 redaction：

* token；
* password；
* private key；
* auth header；
* internal URL；
* IP；
* namespace；
* service name。

必须阻止：

* localhost；
* 127.0.0.0/8；
* ::1；
* link-local；
* metadata endpoint；
* cluster internal domain；
* private IP；
* non-http/https scheme。

安全规则：

* 默认只允许 HTTPS；
* DNS resolution 后再次校验 IP；
* redirect 后重新执行 URL safety；
* 限制 redirect 次数；
* 限制响应大小；
* 不执行 JS；
* 不提交 cookie、header、token；
* blocked domains 优先于 allowed domains；
* provider disabled 时返回 config_error / degraded，不 fallback。

结果保存字段：

```text
title
original_url
final_url
retrieved_at
snippet
content_hash
provider
redaction_version
```

## 9.7 必测项

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

## 9.8 回滚

```env
RUNBOOK_WEB_SEARCH_ENABLED=false
```

---

# 10. PR 9.5：TempoTraceBackend

## 10.1 目标

支持 `TRACE_BACKEND=tempo`，用于 Grafana Tempo。Tempo 必须显式 opt-in，失败只能 degraded。

## 10.2 建议修改文件

```text
packages/tools/trace_backends.py
packages/tools/traces.py
```

## 10.3 建议测试文件

```text
tests/unit/test_tempo_trace_backend.py
tests/integration/test_trace_tool_tempo_backend.py
```

## 10.4 启用条件

```env
M9_EXTENSIONS_ENABLED=true
TRACE_ENABLED=true
TRACE_BACKEND=tempo
```

## 10.5 实现内容

新增：

```text
TempoTraceBackend
```

能力：

* 按 trace ID 查询；
* 按 service / time range 查询；
* 集成 `RuntimeBackendAuthConfig`；
* 输出 `TraceEvidence`；
* 不可达时 degraded；
* auth 失败时 degraded；
* raw secret 不进入 evidence、log、audit、prompt、state。

capability detection：

```text
supports_trace_by_id
supports_search
supports_service_filter
supports_traceql
```

降级规则：

* 只支持 trace by ID 时，service/time range 查询 degraded；
* search 不可用时，不让整个 TraceTool failed；
* TraceQL 不可用时，不影响 trace by ID。

## 10.6 必测项

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

## 10.7 回滚

```env
TRACE_BACKEND=${PRE_M9_TRACE_BACKEND}
TRACE_ENABLED=${PRE_M9_TRACE_ENABLED}
```

回滚前必须校验：

```text
PRE_M9_TRACE_BACKEND 非空
PRE_M9_TRACE_BACKEND in disabled|jaeger|tempo
PRE_M9_TRACE_ENABLED 是合法 boolean
```

禁止 fallback 到 fixture。

---

# 11. PR 9.6：Tempo Auto-discovery Enablement

## 11.1 目标

支持 Discovery 识别 Tempo endpoint，但 production 中永不 auto_publish。

## 11.2 建议修改文件

```text
packages/discovery/backend_endpoints.py
```

## 11.3 建议测试文件

```text
tests/unit/test_tempo_endpoint_detection.py
tests/integration/test_tempo_discovery_proposal.py
```

## 11.4 启用条件

```env
M9_EXTENSIONS_ENABLED=true
TEMPO_DISCOVERY_ENABLED=true
```

## 11.5 状态机

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

## 11.6 强制规则

* production 中 Tempo backend URL 永不 auto_publish；
* detected_only 不进入 EffectiveConfig；
* requires_review 不进入 worker；
* 只有 published 才可被 worker 使用；
* manual env / profile / active override 永远优先于 discovery；
* 不自动切换 `TRACE_BACKEND=tempo`。

## 11.7 必测项

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

## 11.8 回滚

```env
TEMPO_DISCOVERY_ENABLED=false
```

---

# 12. PR 9.7：Grafana Webhook Parser Enhancement

## 12.1 目标

增强 Grafana unified alerting webhook ingest，并保持 fingerprint / dedup 稳定。

## 12.2 建议修改文件

```text
apps/api/schemas/alerts.py
apps/api/services/alert_service.py
```

## 12.3 建议测试文件

```text
tests/unit/test_grafana_alert_parser.py
tests/integration/test_grafana_webhook_ingest.py
```

## 12.4 disabled 行为

当：

```env
GRAFANA_ALERT_INGEST_ENABLED=false
```

Grafana dedicated endpoint 必须：

```text
return 204 No Content
do not create incident
do not enqueue diagnosis
do not log full payload
record metric grafana_webhook_ignored_total{reason="disabled"}
```

## 12.5 enabled 鉴权规则

当：

```env
GRAFANA_ALERT_INGEST_ENABLED=true
```

必须满足：

* 默认使用 HMAC signature + `GRAFANA_WEBHOOK_SECRET_REF`；
* API key / shared token 只能作为兼容路径；
* 未配置任何鉴权方式时返回 `503` / `config_error`；
* 未授权请求返回 `401` 或 `403`；
* malformed payload 返回 `400`，不得 panic；
* payload size 受 `GRAFANA_WEBHOOK_MAX_BYTES` 限制；
* endpoint 接入 rate limit；
* 不记录完整 payload；
* raw secret 不进入 DB、audit、log。

## 12.6 parser 字段

支持：

```text
status
alerts
labels
annotations
startsAt
endsAt
generatorURL
dashboardURL
panelURL
silenceURL
ruleUID
```

fingerprint 禁止包含：

```text
dashboardURL
panelURL
ruleUID
generatorURL
alert_format
internal marker
```

cross-source dedup 只允许基于：

```text
normalized raw_labels + existing ignore rules
```

## 12.7 必测项

```text
test_grafana_ingest_default_disabled
test_grafana_disabled_returns_204
test_grafana_disabled_does_not_create_incident
test_grafana_disabled_does_not_log_full_payload
test_grafana_disabled_records_ignored_metric
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
test_grafana_fingerprint_stable
test_grafana_dedup_with_alertmanager_when_normalized_labels_equivalent
test_grafana_rule_uid_not_used_as_cross_source_fingerprint_key
test_grafana_internal_marker_excluded_from_fingerprint
```

## 12.8 回滚

```env
GRAFANA_ALERT_INGEST_ENABLED=false
```

---

# 13. PR 9.8：Semantic Runbook Search

## 13.1 目标

支持 keyword / semantic / hybrid runbook search。embedding 失败不得影响 approved runbook ingest。

## 13.2 建议修改文件

```text
packages/rag/embedding_provider.py
packages/rag/runbook_ingest.py
packages/rag/embedding_jobs.py
packages/tools/runbook_search.py
packages/db/models.py
migrations/versions/XXXX_runbook_chunk_embeddings.py
```

## 13.3 建议测试文件

```text
tests/unit/test_semantic_runbook_search.py
tests/integration/test_embedding_fallback.py
```

## 13.4 启用条件

```env
M9_EXTENSIONS_ENABLED=true
SEMANTIC_RUNBOOK_SEARCH_ENABLED=true
```

API scope：

```text
runbook:read 或 runbook:review
```

禁止复用：

```text
config:read
```

## 13.5 数据模型

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

唯一约束：

```text
runbook_chunk_id + provider + model + dimension + text_hash
```

## 13.6 实现内容

新增：

```text
EmbeddingProvider
RunbookChunkEmbedding
embedding job
```

provider：

```text
disabled
bge_zh
external
```

approve 流程：

```text
1. 写 RunbookVersion
2. 写 RunbookChunk
3. enqueue embedding job
4. embedding 成功后 semantic search available
5. embedding 失败时 semantic search degraded
6. keyword search 始终可用
```

强制规则：

* approve API 不同步等待 embedding provider；
* embedding input 必须 redaction；
* raw secret 不进入 embedding input；
* embedding provider 不可用时 runbook approve 仍成功；
* 未启用 pgvector 时降级 keyword-only；
* 不得用 JSONB 假装支持向量检索；
* dimension mismatch 标记 failed / degraded。

## 13.7 必测项

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

## 13.8 回滚

```env
SEMANTIC_RUNBOOK_SEARCH_ENABLED=false
EMBEDDING_PROVIDER=disabled
```

---

# 14. PR 9.9：External Embedding Provider

## 14.1 目标

支持外部 embedding provider。该能力属于数据外发，必须默认关闭、单独授权、可审计。

## 14.2 建议修改文件

```text
packages/rag/external_embedding_provider.py
packages/common/backend_auth.py
packages/common/backend_url_safety.py
apps/api/routers/config.py
```

## 14.3 建议测试文件

```text
tests/unit/test_external_embedding_provider.py
tests/integration/test_external_embedding_degraded.py
```

## 14.4 启用条件

```env
M9_EXTENSIONS_ENABLED=true
SEMANTIC_RUNBOOK_SEARCH_ENABLED=true
EXTERNAL_EMBEDDING_PROVIDER_ENABLED=true
EMBEDDING_PROVIDER=external
```

配置 scope：

```text
config:write + embedding:external
```

## 14.5 实现内容

新增：

```text
ExternalEmbeddingProvider
```

要求：

* 支持 external endpoint；
* 支持 auth secret reference；
* endpoint 必须经过 `BackendUrlSafetyValidator`；
* 输入文本必须 redaction；
* 实现 timeout、retry、circuit breaker；
* provider failure 时 semantic search degraded；
* audit 只记录 redacted provider metadata；
* 不保存 raw provider token；
* 不发起 unsafe URL 试连；
* 配置保存时不测试真实认证有效性。

启用时 audit event：

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

## 14.6 必测项

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

## 14.7 回滚

```env
EXTERNAL_EMBEDDING_PROVIDER_ENABLED=false
EMBEDDING_PROVIDER=disabled
```

---

# 15. PR 9.10：Runtime Metrics / Threat Model / E2E / Docs

## 15.1 目标

补齐 M9 整体指标、E2E、threat model、data flow、rollout、rollback 和 operator docs。

## 15.2 建议修改文件

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

## 15.3 Runtime metrics

每个 PR 必须实现自身 safety metric，PR 9.10 只汇总。

必须覆盖：

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

## 15.4 文档必须覆盖

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

## 15.5 E2E smoke sequence

```text
1. production + 所有 M9 flags disabled，确认 M8 smoke 通过。
2. M9_EXTENSIONS_ENABLED=true，但所有子功能 false，确认无行为变化。
3. 开启 RUNBOOK_LLM_GENERATION_ENABLED，生成 pending_review draft。
4. 检查 LLM draft 不保存 full raw prompt，action 已分类。
5. 开启 LLM_INCIDENT_DIFF_ENABLED，证据不足时 skipped，不调用 LLM。
6. 提供足够 evidence 后创建 AmendmentDraft，不修改 approved runbook。
7. 开启 RUNBOOK_WEB_SEARCH_ENABLED，确认脱敏、来源追溯、SSRF 拒绝。
8. 设置 TRACE_BACKEND=tempo，确认正常或 degraded。
9. 关闭 M9 global gate，确认既有 Jaeger 不被关闭。
10. 开启 TEMPO_DISCOVERY_ENABLED，确认 production 不 auto_publish。
11. Grafana disabled 时发送 payload，确认 204 ignored 且不创建 incident。
12. Grafana enabled 后，未授权请求被拒绝，已鉴权 firing/resolved 正常。
13. 开启 SEMANTIC_RUNBOOK_SEARCH_ENABLED，embedding 不可用时 keyword search 可用。
14. 开启 EXTERNAL_EMBEDDING_PROVIDER_ENABLED，确认 unsafe endpoint 被拒绝。
15. 检查 audit/log/state/prompt 无 token/password/private key/internal auth header。
16. 逐个关闭 feature flag，确认回到 M8 行为。
17. 执行总回滚，确认恢复 PRE_M9_TRACE_BACKEND 与 PRE_M9_TRACE_ENABLED。
```

## 15.6 失败注入测试

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

## 15.7 验收标准

* [ ] 所有 M9 能力都有 E2E；
* [ ] 所有 M9 能力都有 rollback switch；
* [ ] 所有 M9 写操作有 audit；
* [ ] 所有 M9 外部请求有 timeout；
* [ ] 所有 M9 外部请求有 redaction / safety 校验；
* [ ] 任一 M9 能力失败不会阻塞基础 diagnosis；
* [ ] runtime metrics 能判断启用、降级、阻断、冲突；
* [ ] threat model 明确禁止字段和数据边界。

---

# 16. 总回滚计划

## 16.1 回滚配置

```env
M9_EXTENSIONS_ENABLED=false

RUNBOOK_LLM_GENERATION_ENABLED=false
LLM_INCIDENT_DIFF_ENABLED=false
RUNBOOK_WEB_SEARCH_ENABLED=false

TEMPO_DISCOVERY_ENABLED=false
GRAFANA_ALERT_INGEST_ENABLED=false

SEMANTIC_RUNBOOK_SEARCH_ENABLED=false
EMBEDDING_PROVIDER=disabled
EXTERNAL_EMBEDDING_PROVIDER_ENABLED=false

TRACE_BACKEND=${PRE_M9_TRACE_BACKEND}
TRACE_ENABLED=${PRE_M9_TRACE_ENABLED}
```

## 16.2 回滚前校验

```text
PRE_M9_TRACE_BACKEND 非空
PRE_M9_TRACE_BACKEND in disabled|jaeger|tempo
PRE_M9_TRACE_ENABLED 是合法 boolean
```

校验失败时：

```text
fail closed
do not write empty TRACE_BACKEND
do not fallback to fixture
do not hardcode jaeger
```

如果某环境在 M9 前没有真实 trace backend：

```env
TRACE_ENABLED=false
TRACE_BACKEND=disabled
```

---

# 17. Agent 执行循环

每个 PR 按以下循环执行：

```text
1. Read
   - 阅读本 PR 目标、禁止事项、测试清单、回滚方案。
   - 查找当前代码库已有等价模块。
   - 不确定路径时先 grep，不新建重复模块。

2. Plan
   - 列出将修改的文件。
   - 列出将新增/修改的测试。
   - 确认不会触碰本 PR 不做事项。

3. Test First
   - 先写或更新失败测试。
   - 测试名优先使用本文给出的测试名。

4. Implement
   - 只实现本 PR 范围。
   - 不提前实现后续 PR。
   - 不改变 M0–M8 不变量。

5. Verify
   - 运行本 PR unit tests。
   - 运行相关 integration tests。
   - 运行 secret leakage tests。
   - 如涉及配置，运行 settings tests。

6. Document
   - 更新 env example、rollout、operator docs 或 threat model 中与本 PR 相关的部分。
   - 记录 rollback switch。

7. Stop
   - 输出变更摘要。
   - 输出测试结果。
   - 输出未解决风险。
   - 不自动进入下一个 PR，除非明确要求连续执行。
```

---

# 18. Agent 停止条件

出现以下情况时必须停止并报告：

* M8 release gate 未通过；
* 找不到当前代码库中对应核心模块，且无法判断等价路径；
* 现有实现与本文不变量冲突；
* 需要保存 raw secret 才能继续；
* 需要绕过 BackendUrlSafetyValidator 才能继续；
* 需要让 LLM 自动 approve、publish 或 apply；
* 需要让 production discovery auto_publish backend URL；
* 需要把 fixture 当 production 正常 trace backend；
* 回滚变量 `PRE_M9_TRACE_*` 缺失且当前环境不是明确的新环境；
* 测试发现 secret 泄露到 prompt、audit、log、state 或 DB。

---

# 19. 最终验收清单

* [ ] production 默认安全关闭；
* [ ] `M9_EXTENSIONS_ENABLED=false` 时行为等同 M8；
* [ ] 子开关冲突不启用功能、不导致启动失败、有 warning/metric；
* [ ] 外部云 LLM 默认不允许；
* [ ] 外部云 LLM 必须 `LLM_EXTERNAL_PROVIDER_ALLOWED=true` 才可调用；
* [ ] LLM Runbook 只能生成 pending_review draft；
* [ ] LLM Runbook 不保存 full raw prompt；
* [ ] LLM Runbook action step 已分级；
* [ ] forbidden / unknown action 不可 approve；
* [ ] LLM incident diff 证据不足时不调用 LLM；
* [ ] LLM incident diff 只能生成 AmendmentDraft；
* [ ] AmendmentDraft 的 approved 与 applied 状态分离；
* [ ] web_search 需要 `runbook:review + runbook:web_search`；
* [ ] web_search 结果只能作为 draft evidence；
* [ ] web_search production allowlist 为空时不能启用；
* [ ] web_search provider disabled 时不得 fallback；
* [ ] TempoTraceBackend opt-in 才启用；
* [ ] Tempo capability detection 可部分降级；
* [ ] Tempo discovery 不 auto_publish；
* [ ] Grafana ingest feature flag 控制；
* [ ] Grafana disabled 时固定 204 ignored，不创建 incident；
* [ ] Grafana enabled 时默认 HMAC 鉴权；
* [ ] Grafana enabled 但未配置鉴权时返回 503/config_error；
* [ ] Grafana / Alertmanager cross-source dedup 只基于 normalized raw_labels；
* [ ] semantic search 可降级为 keyword-only；
* [ ] approved runbook ingest 不等待 embedding provider；
* [ ] external embedding provider 默认关闭；
* [ ] external embedding provider 需要 `config:write + embedding:external`；
* [ ] 所有 M9 写操作有 audit；
* [ ] 所有 M9 外部请求有 timeout；
* [ ] 所有 M9 路径通过 secret leakage tests；
* [ ] 所有 M9 功能可独立回滚；
* [ ] 总回滚恢复 `PRE_M9_TRACE_BACKEND` / `PRE_M9_TRACE_ENABLED`；
* [ ] 总回滚不硬编码 jaeger；
* [ ] 总回滚不硬编码 fixture。
