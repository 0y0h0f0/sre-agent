# 反馈、NFA、关联事件与持续学习技术深挖

**最后更新：** 2026-06-18

本文沿当前代码路径说明操作员反馈、NFA 标记、跨 incident 关联、runbook feedback analyzer、amendment draft、memory 和 eval 数据回流的真实边界。它补充 [API 参考](../01-backend/api-reference.md)、[数据模型](../01-backend/data-model.md)、[Runbook RAG](../04-rag/runbook-rag.md)、[记忆、缓存与上下文压缩](../05-memory/memory-cache-compression.md) 和 [Eval](../09-evals/evaluation.md)。Runbook draft/version/amendment 的完整发布与审核生命周期见 [Runbook 草稿、版本与 Amendment 生命周期技术深挖](runbook-draft-version-amendment-lifecycle-deep-dive.md)。

最重要的边界：

- NFA 当前会让相同 fingerprint 的后续告警降级为 `P4`，不会丢弃告警，也不会跳过 incident/agent run 创建。
- 根因修正和 action 反馈是审计化的人类反馈记录，不会直接重跑 Agent、执行 action、训练模型或自动修改 runbook。
- 跨 incident 关联 API 当前按相同 fingerprint 和相同 service 查询 incident；`incident_correlations` 表和 repository 存在，但当前 `FeedbackService.get_correlated_incidents()` 不依赖已持久化的 pair 记录。
- `RunbookFeedbackAnalyzer` 是 deterministic library 能力；当前没有 worker/Beat/API 自动把它的结果落成 `RunbookFeedbackSummary` 或自动生成可应用 amendment。
- M9 incident diff 可以创建 `AmendmentDraft(status=pending_review)`；审核状态变化只更新 amendment lifecycle metadata，不会自动合并内容、发布 runbook 或重新 ingest chunks。
- API router 当前构造 `FeedbackService(db, settings)`，没有注入 `MemoryStore`；因此 API 反馈默认不会写入 memory。Agent 自身的 `persist_memory`/`compress_context` 仍是当前稳定 memory 写回路径。
- `FeedbackItemRepository.list_for_eval()` 提供 eval 数据读取形状，但当前 eval runner/API 不会自动把 operator feedback 变成 eval case，也不会做 fine-tuning。

## 阅读目标

读完本文应能回答：

- `POST /api/incidents/{incident_id}/nfa` 如何更新 `false_positive_patterns`、`feedback_items`、audit 和 incident severity。
- alert ingestion 如何读取 NFA pattern，并为什么只降级 severity 而不是忽略告警。
- 根因修正、action add/remove 反馈写入哪些表，哪些业务对象不会被直接修改。
- “相关事件”页面为什么只展示 same fingerprint/same service，而不是 vector similarity。
- `RunbookFeedbackAnalyzer` 做了哪些 deterministic 分析，当前缺少哪层自动编排。
- `AmendmentDraft` 从 M9 incident diff 生成到 review/apply/supersede 的状态机是什么。
- feedback、memory、eval 之间当前哪些是实际数据回流，哪些只是预留接口。

## 代码入口

| 主题 | 当前入口 |
|------|----------|
| Feedback API routes | `apps/api/routers/incidents.py` |
| Feedback service | `apps/api/services/feedback_service.py` |
| Feedback schemas | `apps/api/schemas/feedback.py` |
| NFA repository | `packages/db/repositories/false_positive_patterns.py` |
| Feedback repository | `packages/db/repositories/feedback.py` |
| Correlation repository | `packages/db/repositories/incident_correlations.py` |
| Alert ingestion suppression check | `apps/api/services/alert_service.py` |
| Runbook feedback analyzer | `packages/discovery/runbook_feedback.py` |
| Runbook amendment service | `apps/api/services/runbook_service.py` |
| Runbook amendment routes | `apps/api/routers/runbooks.py` |
| Frontend incident interactions | `apps/web/src/App.tsx` |
| Frontend feedback API helpers | `apps/web/src/api.ts` |
| Phase 5 feedback migration | `migrations/versions/0006_phase5_feedback.py` |
| Runbook feedback migration | `migrations/versions/3f7e8d9c0a1b_runbook_feedback_models.py` |
| M9 amendment lifecycle migration | `migrations/versions/d4e5f6a7b8c9_m9_amendment_draft_lifecycle.py` |
| Unit tests | `tests/unit/test_feedback.py`、`tests/unit/test_runbook_feedback.py` |
| API/integration tests | `tests/integration/test_feedback_api.py`、`tests/integration/test_amendment_draft_review.py` |

## 数据对象

| 对象 | 表 | Public ID | 当前用途 |
|------|----|-----------|----------|
| `FalsePositivePattern` | `false_positive_patterns` | `nfp_` | 记录 fingerprint 的 NFA 次数、active/suppressed 状态和自动抑制时间 |
| `FeedbackItem` | `feedback_items` | `fbk_` | 记录根因修正、action add/remove、NFA mark 等操作员反馈 |
| `IncidentCorrelation` | `incident_correlations` | `cor_` | pairwise incident 关联表；repository 可创建/查询，但当前 correlated API 不从该表读取 |
| `RunbookFeedbackSummary` | `runbook_feedback_summaries` | `summary_id` | deterministic runbook feedback 聚合模型；当前没有自动生产编排 |
| `AmendmentDraft` | `amendment_drafts` | `amd_` | runbook amendment 提案；来自 M9 incident diff 或未来 runbook feedback 编排 |
| `MemoryItem` | `memory_items` | `mem_` | Agent memory；API feedback 默认不写入 |
| `EvalRun` / `EvalCase` | `eval_runs` / `eval_cases` | `eval_` / `eval_case_id` | eval 结果持久化；不会自动从 feedback 生成 case |

`FeedbackItem.feedback_type` 的当前 API service 输出值是：

| feedback_type | 来源 |
|---------------|------|
| `nfa_mark` | `FeedbackService.mark_nfa()` |
| `root_cause_correction` | `FeedbackService.correct_root_cause()` |
| `action_add` | `FeedbackService.correct_action(action_type="add")` |
| `action_remove` | `FeedbackService.correct_action(action_type="remove")` |

旧模型注释和部分历史文档可能出现 `action_addition` / `action_removal`；按当前 API/service/test 行为应使用 `action_add` / `action_remove`。

## 总链路

```text
Incident detail page
  -> mark NFA / edit root cause / view correlated incidents
  -> apps/web/src/api.ts
  -> apps/api/routers/incidents.py
  -> FeedbackService
       -> IncidentRepository
       -> FalsePositivePatternRepository / FeedbackItemRepository
       -> IncidentCorrelationRepository
       -> AuditLogRepository
  -> commit
  -> frontend query invalidation
```

另一条 runbook 学习链路当前是“可被调用的库能力 + M9 diff API”，不是自动后台训练：

```text
RunbookFeedbackAnalyzer
  -> aggregate closed/resolved incidents
  -> compute action statistics
  -> detect gaps vs existing runbook drafts
  -> build deterministic amendment content
  -> FeedbackResult

M9 incident diff API
  -> IncidentDiffAnalyzer
  -> AmendmentDraft(status=pending_review, source=llm_incident_diff)
  -> operator review
  -> approved/rejected/applied/superseded metadata
```

## 1. NFA 标记

API 入口：

```text
POST /api/incidents/{incident_id}/nfa
```

请求体是 `NfaMarkRequest`：

```json
{
  "reason": "Noise alert"
}
```

服务路径：

```text
router mark_incident_nfa()
  -> FeedbackService.mark_nfa()
  -> _require_incident()
  -> FalsePositivePatternRepository.increment_nfa()
  -> FeedbackItemRepository.create(feedback_type="nfa_mark")
  -> AuditLogRepository.create(action="nfa_mark")
  -> if pattern.status == "suppressed": incident.severity = "P4"
  -> commit
```

`FalsePositivePatternRepository.increment_nfa()` 使用 `SELECT ... FOR UPDATE` 读取已有 fingerprint，避免并发 NFA mark 丢计数。第一次标记创建 `nfp_` pattern；后续标记递增 `nfa_count`。当计数达到 `NFA_AUTO_SUPPRESS_THRESHOLD`，默认 `3`，且 pattern 仍是 `active` 时，会把 pattern 状态改为 `suppressed`，写 `suppressed_at` 和 `suppressed_by="auto"`。

返回体示例：

```json
{
  "pattern_id": "nfp_xxx",
  "fingerprint": "fp-checkout",
  "nfa_count": 3,
  "status": "suppressed",
  "message": "Auto-suppressed after 3 NFA marks"
}
```

安全边界：

- NFA 是人类反馈和降级信号，不是删除 incident。
- 达到阈值时当前 incident severity 会改为 `P4`。
- 后续相同 fingerprint 的 alert ingestion 会读 pattern 并把新 incident severity 设为 `P4`。
- 当前没有 API 暴露 `restore_pattern()` 或 `expire_stale_patterns()`；这些 repository helper 只在 unit tests 中覆盖。
- `NFA_RESET_DAYS` 配置存在，但当前没有 Beat task 自动调用 `expire_stale_patterns()`。

## 2. Alert Ingestion 与 NFA Pattern

`AlertService.create_alert()` 开头先调用：

```text
FalsePositivePatternRepository.should_suppress(payload.fingerprint)
```

`should_suppress()` 的语义：

1. 没有 pattern：返回 `False`。
2. pattern 已是 `suppressed`：返回 `True`。
3. pattern 是 `active` 但计数已达到阈值：把状态推进为 `suppressed` 并返回 `True`。

随后 alert ingestion 仍会继续：

```text
existing = get_open_by_fingerprint()
if existing: return deduplicated
create Incident
if suppressed: incident.severity = "P4"
create AgentRun
commit
enqueue Celery diagnosis
```

因此当前“suppressed”更准确地说是 severity 降级，不是 drop/ignore。这样保留了审计、报告、后续诊断和人工回溯能力。

## 3. 根因修正

API 入口：

```text
PATCH /api/incidents/{incident_id}/root-cause
```

请求体：

```json
{
  "corrected_summary": "Memory leak in checkout pod",
  "reason": "OOM evidence was stronger than CPU saturation"
}
```

服务路径：

```text
FeedbackService.correct_root_cause()
  -> _require_incident()
  -> original = incident.root_cause_summary or "(not set)"
  -> incident.root_cause_summary = corrected
  -> FeedbackItem(feedback_type="root_cause_correction")
  -> AuditLog(action="root_cause_correct")
  -> commit
```

当前行为：

- 修改 `incidents.root_cause_summary`，让 incident detail 后续读取新摘要。
- 写入 `feedback_items`，保留 original/corrected/delta。
- 写入 audit。
- 不自动重新生成报告；报告再生成仍通过 report regenerate API。
- 不自动重跑 Agent，也不把修正同步到 LangGraph checkpoint。
- API route 当前没有注入 `MemoryStore`，所以不会通过 feedback API 写 service memory。

`FeedbackService` 内部有 `_write_memory()` hook，但它只在构造 service 时传入 `memory_store` 才会执行。当前 `apps/api/routers/incidents.py` 没有传入该依赖。

## 4. Action 反馈

API 入口：

```text
POST /api/incidents/{incident_id}/actions/{action_id}/feedback
```

`action_type="add"` 时：

```json
{
  "action_type": "add",
  "action": {
    "type": "restart_pod",
    "target": "checkout-abc"
  },
  "reason": "Missing remediation option"
}
```

`action_type="remove"` 时：

```json
{
  "action_type": "remove",
  "action_id": "act_123",
  "reason": "Unsafe action for this service"
}
```

服务路径：

```text
FeedbackService.correct_action()
  -> validate action_type
  -> FeedbackItem(feedback_type="action_add" | "action_remove")
  -> AuditLog(action="action_add" | "action_remove")
  -> commit
```

当前行为：

- `add` 要求 payload 中带 `action`。
- `remove` 要求 payload 中带 `action_id`。
- 无效 `action_type` 返回 `VALIDATION_ERROR`。
- 该 API 不会插入新的 `Action` 记录，不会删除已有 action，也不会触发执行。
- action feedback 是供审计、人工复盘、未来 eval 或未来学习编排使用的数据。
- API 当前没有把 action add 写入 procedural memory；只有 service 被外部构造并显式注入 `MemoryStore` 时，内部 hook 才可能写 memory。

## 5. 反馈列表

API 入口：

```text
GET /api/incidents/{incident_id}/feedback
```

服务路径：

```text
FeedbackService.list_feedback()
  -> _require_incident()
  -> FeedbackItemRepository.list_for_incident()
  -> order by submitted_at desc
```

返回 `FeedbackListResponse(items, total)`。当前前端 `api.ts` 已提供 `listIncidentFeedback()` helper，但 `IncidentDetailPage` 还没有把反馈列表渲染成独立 UI 区块。

## 6. 跨 Incident 关联

API 入口：

```text
GET /api/incidents/{incident_id}/correlated
```

当前查询策略在 `FeedbackService.get_correlated_incidents()` 中：

1. 先按相同 `fingerprint` 查最近 incident，`correlation_type="same_fingerprint"`。
2. 如果结果数量还小于 `CROSS_INCIDENT_MAX_RESULTS`，再按相同 `service` 查最近 incident，`correlation_type="similar_service"`。
3. 去重后返回。

重要细节：

- `CROSS_INCIDENT_MAX_RESULTS` 默认是 `5`。
- 当前返回的 `similarity_score` 为 `None`。
- 当前不会执行 embedding/vector similarity。
- `IncidentCorrelationRepository.create()` 和 `incident_correlations` 表可保存 pairwise 关联，但 `get_correlated_incidents()` 当前没有读取 `get_for_incident()` 的持久化 pair。
- 前端 incident detail 页面会读取该 API，并把相同 fingerprint 显示为“相同指纹”，相同 service 显示为“相同服务”。

这意味着 `similar_embedding` 和 `manual` 是数据模型支持的类型，不是当前关联 API 的主动计算路径。

## 7. 前端交互边界

`apps/web/src/App.tsx` 当前在 incident detail 页面暴露：

- “标记无效”：调用 `markIncidentNFA()`。
- “修正根因”：调用 `correctIncidentRootCause()`。
- “相关事件”：读取 `getCorrelatedIncidents()`，`staleTime=60000`。

`apps/web/src/api.ts` 还提供：

- `correctIncidentAction()`
- `listIncidentFeedback()`

但当前页面没有独立 action feedback 表单，也没有 feedback timeline。协作可见性主要通过 comments、audit section、root cause display 和 correlated incidents 体现。

## 8. Runbook Feedback Analyzer

`packages/discovery/runbook_feedback.py` 是 deterministic analyzer。它不调用 LLM，不做 web search，也不写数据库。

核心步骤：

```text
aggregate_incidents()
  -> group closed/resolved incidents by (service, fault_type)
compute_action_statistics()
  -> count success/failed/skipped/rejected actions above confidence threshold
detect_gaps()
  -> compare cluster fault_type and expected diagnostic steps with existing drafts
analyze_and_propose()
  -> enforce cooldown
  -> build deterministic amendment section/rationale/content
```

默认参数：

| 参数 | 默认值 | 来源 |
|------|--------|------|
| `min_incidents` | `5` | `RUNBOOK_AMENDMENT_MIN_INCIDENTS` 配置默认值 |
| `cooldown_days` | `7` | `RUNBOOK_AMENDMENT_COOLDOWN_DAYS` 配置默认值 |
| `confidence_threshold` | `0.7` | analyzer constructor 默认值 |

fault type 派生规则：

| alert name hint | fault_type |
|-----------------|------------|
| latency、slow、p99、p95 | `high_latency` |
| error、5xx、500、failure | `high_error_rate` |
| cpu、memory、disk、saturation、oom | `resource_saturation` |
| dependency、downstream、upstream | `dependency_failure` |
| 其它 | `generic_incident` |

Gap 检测当前只基于内置诊断步骤表、existing runbook draft content 和 evidence summary 中的关键词/工具频次。它不会读取 live observability backend，也不会自行创建 draft。

当前未接线的部分：

- 没有 API endpoint 触发 `RunbookFeedbackAnalyzer`。
- 没有 Celery Beat 定期扫描 closed/resolved incidents。
- 没有 repository/service 自动把 `FeedbackResult` 写入 `runbook_feedback_summaries`。
- 没有自动从 `FeedbackResult` 创建 `AmendmentDraft(source="runbook_feedback")` 的生产路径。

因此它是已实现并测试的分析能力，不是已启用的自动学习闭环。

## 9. Runbook Feedback Summary 模型

`RunbookFeedbackSummary` 和迁移 `3f7e8d9c0a1b_runbook_feedback_models.py` 提供了持久化形状：

| 字段 | 含义 |
|------|------|
| `summary_id` | summary public ID |
| `service` / `fault_type` | 聚合维度 |
| `incident_count` / `incident_ids` | 聚合样本 |
| `total_actions` / `successful_actions` / `failed_actions` / `skipped_actions` / `rejected_actions` | action outcome 统计 |
| `top_action_types` | action type 频次 |
| `missing_fault_types` | runbook coverage 缺口 |
| `missing_diagnostic_steps` | 缺失诊断步骤 |
| `recurring_evidence_patterns` | 重复证据模式 |
| `cooldown_until` | 防重复 proposal 的冷却时间 |
| `generated_by` | 默认 `runbook_feedback` |

当前仓库没有专门的 `RunbookFeedbackSummaryRepository`，也没有 API 列表/详情端点。开发这条链路时需要补 repository、service、事务、audit、测试和文档，不能只调用 analyzer。

## 10. Amendment Draft 生命周期

`AmendmentDraft` 可来自两类来源：

| source | 当前状态 |
|--------|----------|
| `runbook_feedback` | 数据模型默认值；当前缺少自动创建编排 |
| `llm_incident_diff` | M9 incident diff API 已实现 |

M9 incident diff API：

```text
POST /api/runbooks/incident-diff
```

启用条件：

- `M9_EXTENSIONS_ENABLED=true`
- `LLM_INCIDENT_DIFF_ENABLED=true`
- provider 配置满足 M9 LLM 规则

服务路径：

```text
RunbookService.llm_incident_diff()
  -> IncidentDiffAnalyzer.analyze()
  -> if skipped/disabled/blocked/degraded: return status, no DB write
  -> create AmendmentDraft(status="pending_review", source="llm_incident_diff")
  -> AuditLog(action="runbook.amendment_draft.created")
  -> commit
```

证据不足时返回 `skipped_insufficient_evidence`，不会调用 LLM，也不会创建 amendment。

## 11. Amendment Review 状态机

API 入口：

```text
GET  /api/runbooks/amendments
POST /api/runbooks/amendments/{amendment_id}/review
```

允许的 review status：

| status | 前置条件 | 当前效果 |
|--------|----------|----------|
| `approved` | amendment 必须是 `pending_review`，且有 `evidence_incident_ids` | 写 reviewer、approved_by、approved_at，状态为 approved |
| `rejected` | amendment 必须是 `pending_review` | 写 reviewer/review_comment，状态为 rejected |
| `applied` | amendment 必须是 `approved`，有 evidence refs，`proposal_kind="proposed_patch"`，且正好一个 target | 写 applied target 和 applied_at，状态为 applied |
| `superseded` | 请求必须带 `superseded_by_amendment_id` | 写 superseded_by_amendment_id，状态为 superseded |

终态约束：

- `applied`、`rejected`、`superseded` 是 terminal；再次 review 会返回 validation error。
- `approved` 不是 applied；它只是审核通过。
- `applied` 当前只记录 amendment lifecycle metadata，不会自动修改 `runbook_drafts.content`、不会创建 `RunbookVersion`、不会把新内容 ingest 到 `runbook_chunks`。
- 如果需要真正更新 runbook，仍要走 draft/version 发布链路，并保留人工 review。

## 12. Memory 回流边界

当前稳定 memory 写回路径在 Agent workflow 内：

- `compress_context` 把 compression summary 写入 L2 service memory。
- `persist_memory` 在报告后写 L1 incident、L2 service、L3 procedural 和 L0 run memory。

Feedback API 的 memory 情况：

- `FeedbackService` 支持可选 `memory_store` 构造参数。
- `_write_memory()` 是 best-effort，并吞掉异常。
- 当前 `apps/api/routers/incidents.py` 没有传入 `MemoryStore`。
- 因此通过 HTTP API 提交的 NFA、root cause correction、action feedback 当前默认不会进入 `memory_items`。

这点很关键：不要在诊断质量评估中假设“操作员刚修正了根因，下一次 Agent 会自动从 memory 读到这条修正”。当前要做到这一点，需要新增明确的 service wiring、事务提交策略和测试。

## 13. Eval 回流边界

`FeedbackItemRepository.list_for_eval()` 提供了后续构造 eval 数据集的读取接口：

- 不传 `feedback_type` 时读取 `root_cause_correction`、`action_add`、`action_remove`。
- 默认排除 `nfa_mark`。
- 可按具体 feedback type 过滤。

当前 eval 实现不会自动消费该接口：

- `run_suite()` 使用固定 smoke/full dataset。
- Eval API/Celery task 运行 suite 并写 `EvalRun.metrics`。
- `replay_incident()` 离线重放历史 incident，但不是 CI 门禁。
- `run_shadow_diagnosis()` 当前是 safe stub，不写真实 incident/action/approval。

因此 feedback 到 eval 的自动数据回流尚未接线。新增时至少需要：

1. 明确 feedback 到 eval case 的筛选规则。
2. 保留原 incident/evidence/action 引用。
3. 避免把 NFA/noise 直接当成根因准确率样本。
4. 使用 FakeLLM/fixture backend 做 CI smoke。
5. 不把真实 LLM full eval 结果作为稳定 CI gate。

## 14. 审计与安全边界

Feedback 写路径都会写 audit：

| 行为 | audit action | resource_type |
|------|--------------|---------------|
| NFA mark | `nfa_mark` | `incident` |
| 根因修正 | `root_cause_correct` | `incident` |
| Action add feedback | `action_add` | `action` |
| Action remove feedback | `action_remove` | `action` |
| M9 amendment 创建 | `runbook.amendment_draft.created` | `amendment_draft` |
| Amendment review | `runbook.amendment.<status>` | `amendment_draft` |

安全不变量：

- Feedback 不绕过 guardrail。
- Action feedback 不执行 remediation。
- NFA 不删除 incident，不修改外部系统。
- Amendment 不自动发布，不自动应用到 runbook content。
- LLM incident diff 只产出 pending review amendment，不会 auto-approve、auto-apply 或 auto-execute。
- Feedback/eval/memory 数据不得包含 raw secret、Authorization header、provider token 或未脱敏后端密钥。

## 15. 常见排障

| 现象 | 先查 | 可能原因 |
|------|------|----------|
| NFA 后新告警仍是 P2/P1 | `false_positive_patterns`、fingerprint、`NFA_AUTO_SUPPRESS_THRESHOLD` | fingerprint 不一致、计数未达阈值、pattern 未变 suppressed |
| NFA 后没有“消失”告警 | `AlertService.create_alert()` | 当前设计是降级为 P4，不是 drop alert |
| 根因修正后报告没变 | report version、report regenerate API | 根因修正不自动重写历史报告 |
| action feedback 没有新增/删除 action | `feedback_items`、`actions` 表 | action feedback 只记录反馈，不改 action 表 |
| 相关事件没有 vector 相似结果 | `get_correlated_incidents()` | 当前只查 same fingerprint/same service |
| runbook feedback analyzer 没有产生 DB 记录 | `packages/discovery/runbook_feedback.py` 调用点 | analyzer 是纯函数能力，没有自动落库编排 |
| amendment approved 后 runbook 未更新 | `runbook_drafts`、`runbook_versions`、`runbook_chunks` | approved/applied 只改 amendment metadata，不合并内容 |
| 反馈没有影响下一次诊断 | `memory_items`、router 构造方式 | API route 未注入 `MemoryStore`，feedback 不自动进 memory |

## 16. 当前测试入口

| 测试 | 覆盖 |
|------|------|
| `tests/unit/test_feedback.py` | NFA repository/service、feedback item、correlation repository、root cause/action correction |
| `tests/integration/test_feedback_api.py` | NFA/root cause/action/correlation/feedback API |
| `tests/integration/test_phase6_collaboration.py` | root cause correction audit、comments/annotations 等协作审计 |
| `tests/unit/test_runbook_feedback.py` | deterministic runbook feedback analyzer 聚合、action stats、gap、cooldown/amendment content |
| `tests/integration/test_amendment_draft_review.py` | M9 incident diff 创建 amendment、review transition、apply/supersede validation |

按项目测试策略，Codex 不直接运行测试。需要验证该主题时由开发者本地运行：

```bash
pytest tests/unit/test_feedback.py tests/integration/test_feedback_api.py
pytest tests/unit/test_runbook_feedback.py tests/integration/test_amendment_draft_review.py
```

## 17. 新增能力 Checklist

### 新增 feedback 类型

- 更新 `apps/api/schemas/feedback.py`、`FeedbackService`、`FeedbackItemRepository`。
- 明确是否修改业务对象，还是只写 feedback/audit。
- 更新 `status-and-ids.md` 的 feedback type。
- 加 API integration test 和 audit assertion。

### 接线 feedback -> memory

- 不在 router 中隐式创建全局 session 或跨事务对象。
- 明确 memory 写入和 feedback commit 的事务关系。
- 保证 memory 写入失败不破坏主反馈写入，或显式返回结构化降级。
- 增加 `memory_items` 断言和下一次 `retrieve_memory` 行为测试。

### 接线 feedback -> eval

- 使用 deterministic dataset 生成规则。
- 保留 incident/action/evidence 引用。
- 排除或单独标注 NFA/noise 样本。
- CI 仍使用 FakeLLM 和 fixture executor。
- 不引入真实 LLM/fine-tuning 稳定门禁。

### 接线 RunbookFeedbackAnalyzer -> DB

- 新增 repository/service，而不是在 analyzer 里直接写 DB。
- analyzer 保持纯函数和无外部调用。
- 写 `RunbookFeedbackSummary` 时记录 incident/action/evidence provenance。
- 创建 `AmendmentDraft` 时状态必须是 `pending_review`。
- 不自动 apply，不自动 publish，不自动 ingest chunks。
- 加 cooldown、幂等和 audit 测试。
