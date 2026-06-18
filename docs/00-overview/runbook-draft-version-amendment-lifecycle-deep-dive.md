# Runbook 草稿、版本与 Amendment 生命周期技术深挖

最后更新：2026-06-18

本文说明当前项目里 Runbook 从静态 Markdown、incident cluster、模板、M9 LLM draft 和 incident diff proposal 进入系统后的真实生命周期。

它补充：

- [Runbook RAG](../04-rag/runbook-rag.md)
- [反馈、NFA、关联事件与持续学习技术深挖](feedback-nfa-correlation-continuous-learning-deep-dive.md)
- [LLM、Prompt、FakeLLM 与 Provider 边界技术深挖](llm-prompt-fakellm-provider-boundaries-deep-dive.md)

本文重点不是“如何写一个好 runbook”，而是解释当前代码如何保证：

- searchable runbook chunks 有稳定来源和 chunk ID。
- draft 不会绕过人工 review 直接发布。
- M9 LLM 只能生成待审草稿或待审 amendment。
- amendment 的 `applied` 目前只记录生命周期元数据，不会自动改写 runbook 内容。

## 一句话模型

```text
Markdown source
  -> ingest
  -> runbook_chunks
  -> search/retrieve_runbook evidence

incident cluster / template / M9 LLM
  -> RunbookDraft
  -> review published
  -> RunbookVersion
  -> ingest draft chunks

incident + approved runbook + feedback/evidence
  -> IncidentDiffAnalyzer
  -> AmendmentDraft(pending_review)
  -> review lifecycle metadata
```

安全边界：

- `RunbookDraft` 是草稿对象，不等于已发布知识。
- `RunbookVersion` 是发布历史，不等于运行时自动执行策略。
- `RunbookChunk` 是 RAG 检索单元，诊断引用它时仍要保留 evidence ID。
- `AmendmentDraft` 是修改建议，不会自动合并、发布或重新 ingest。

## 代码入口

| 能力 | 主要入口 | 说明 |
|------|----------|------|
| Markdown ingest | `apps/api/services/runbook_service.py` `ingest()` | 从目录摄取 Markdown runbooks。 |
| Runbook search | `RunbookService.search()` | 调用 `RunbookRetriever`，返回 chunk ID、source path、title、excerpt、score、metadata。 |
| incident-cluster draft | `RunbookService.generate_drafts()` | 从 incident clusters 生成 draft，使用注入的 LLM adapter；本地/测试应走 FakeLLM。 |
| template draft | `RunbookService.generate_template_draft()` | 纯模板引擎生成 draft。 |
| M9 LLM draft | `RunbookService.llm_generate_draft()` | feature-gated，只保存 `pending_review` draft。 |
| draft review | `RunbookService.review_draft()` | `published` 会创建 version 并 ingest draft chunks；`rejected` 只更新审核状态。 |
| draft regenerate | `RunbookService.regenerate_draft()` | 创建一个新的 child draft，不覆盖原 draft。 |
| incident diff | `RunbookService.llm_incident_diff()` | feature-gated，生成并持久化 amendment drafts。 |
| amendment review | `RunbookService.review_amendment()` | 控制 `pending_review -> approved/rejected/applied/superseded` 等状态转换。 |
| API router | `apps/api/routers/runbooks.py` | 暴露 `/api/runbooks/*` endpoints。 |

## 数据对象边界

### `RunbookChunk`

`RunbookChunk` 是检索和 Agent 证据引用的核心对象。

关键字段：

- `chunk_id`：`chk_` 前缀公开 ID。
- `document_id`：runbook document 标识。
- `source_path`：原始 Markdown 路径或 `drafts/{draft_id}.md`。
- `title`、`content`、`metadata`：检索和 prompt context 的主体。
- `content_hash`：用于跳过重复内容。
- `embedding`：当前 schema 为 512 维向量。
- `embedding_model`：默认 fake 或外部 provider 名称；降级时可为 `none`。
- `language`：解析自 front matter，默认 `en`。

Agent 的 `retrieve_runbook` 节点不会只把 chunk 文本塞进状态；它会把命中作为 `evidence_items(type=runbook)` 持久化，并保留 `chunk_id`、`source_path` 和生成的 `evidence_id`。

### `RunbookDraft`

`RunbookDraft` 是待审内容容器。

常见状态：

- `draft`：普通生成草稿或模板草稿默认状态。
- `pending_review`：M9 LLM draft 默认状态。
- `published`：人工 review 后发布。
- `rejected`：人工拒绝。

关键字段：

- `draft_id`：`drf_` 前缀公开 ID。
- `fingerprint`：用于去重相同 incident cluster 或模板来源。
- `incident_ids`：来源 incidents。
- `service`、`incident_type`：检索、列表和模板上下文。
- `draft_type`：如 `incident_cluster`、`template`、`llm_generated`。
- `source`：如 `llm`、`template_engine`、`regenerated`。
- `parent_draft_id`：regenerate 生成的新草稿会指向原草稿。
- `source_chunk_ids`：可记录引用过的已有 runbook chunks。
- `llm_model`：LLM draft 或 cluster draft 可记录模型来源。

### `RunbookVersion`

`RunbookVersion` 是发布历史。

当前 publish 行为：

- `document_id = draft.draft_id`
- `source_path = drafts/{draft_id}.md`
- `content_hash = sha256(draft.content)`
- `change_reason = published_from_draft`
- `related_draft_id = draft.draft_id`
- `created_by = reviewer`

`RunbookVersionRepository.create()` 会在同一个 `document_id` 下读取最新版本并加锁，然后分配递增 `version_number`。数据库还有 `(document_id, version_number)` 唯一约束。

### `AmendmentDraft`

`AmendmentDraft` 是 runbook 修改建议。

它可以来自：

- M9 `IncidentDiffAnalyzer`。
- 确定性 feedback analyzer 的未来编排。

当前 API 中，M9 incident diff 会持久化：

- `status = pending_review`
- `source = llm_incident_diff`
- `related_incident_id`
- `runbook_version_id`
- `amendment_type`
- `section_path`
- `original_content`
- `proposed_content`
- `rationale`
- `evidence_incident_ids`
- `confidence`
- `proposal_kind`

`AmendmentDraft` 不是 patch apply engine。即使状态变成 `applied`，当前实现也只是记录 `applied_to_draft_id` 或 `applied_to_runbook_version_id` 等元数据。

## 静态 Markdown Ingest

API：

```text
POST /api/runbooks/ingest
```

service 行为：

1. 创建 `RunbookIngestor(self.repository)`。
2. 调用 `ingest_path(request.path, reingest=request.reingest)`。
3. 成功后 commit。
4. 路径不存在或 metadata 解析错误时抛 `ValidationAppError`。

摄取后的 chunks 会被 runbook search 和 Agent `RunbookSearchTool` 使用。

设计含义：

- 静态 Markdown ingest 是知识库的基础路径。
- 默认 fake embedding provider 保证本地和测试可重复。
- 不能在测试中使用随机 embedding。
- 新增 provider 时必须保持 512 维或同步迁移、代码和测试。

## Search 与 Evidence

API：

```text
GET /api/runbooks/search?q=...&top_k=...
```

响应项保留：

- `chunk_id`
- `source_path`
- `title`
- `excerpt`
- `score`
- `metadata`

Agent 侧关键点：

- runbook 命中会变成 `evidence_items(type=runbook)`。
- `source_id` 指向 `chunk_id`。
- `payload.source_path` 保留 Markdown 或 draft source path。
- `runbook_context` 回写 evidence ID。
- 诊断输出引用 runbook 时应保留 `evidence_id` 和 `runbook_chunk_ids`。

这使后续报告、审计和错误排查可以追溯到具体 chunk，而不是只留下自然语言摘要。

## Draft 来源

### Incident-cluster draft

API：

```text
POST /api/runbooks/drafts/generate
```

service 行为：

1. 构造 `TemplateExtractor(IncidentRepository(self.db))`。
2. 构造 `RunbookGenerator(llm, draft_repo, extractor)`。
3. 从 incident clusters 提取候选。
4. 对已有 `draft` 或 `published` fingerprint 的候选跳过。
5. 通过注入的 LLM adapter 生成 Markdown content。
6. 创建 `RunbookDraft(status=draft)`。
7. commit。

注意：

- 该路径使用注入的 LLM adapter，不应理解为总是纯字符串模板。
- 本地、测试和 CI 应使用 FakeLLM 来保持确定性。
- 生成 draft 不会发布，不会写入 `runbook_chunks`。

### Template draft

API：

```text
POST /api/runbooks/template
```

service 行为：

1. 使用 `RunbookTemplateEngine` 生成标准 Markdown。
2. 创建 `RunbookDraft(status=draft, draft_type=template, source=template_engine)`。
3. fingerprint 来自 `template:{service_name}:{incident_type}` 的 hash 前缀。
4. commit。

该路径适合手动补齐 runbook 结构，不需要外部 LLM。

### Regenerated draft

API：

```text
POST /api/runbooks/drafts/{draft_id}/regenerate
```

service 行为：

- 读取原 draft。
- 创建新的 draft。
- 复制原 draft 的 service、incident type、content、front matter、source chunks 等字段。
- 设置 `source=regenerated`。
- 设置 `parent_draft_id=original.draft_id`。
- 标题追加 `(Regenerated)`。
- commit。

重要边界：

- regenerate 不覆盖原 draft。
- regenerate 不自动发布。
- regenerate 不创建 `RunbookVersion`。

### M9 LLM draft

API：

```text
POST /api/runbooks/llm-generate
```

feature gates：

- `M9_EXTENSIONS_ENABLED=true`
- `RUNBOOK_LLM_GENERATION_ENABLED=true`

外部 provider 还需要：

- `LLM_EXTERNAL_PROVIDER_ALLOWED=true`
- API scope 满足 runbook LLM 入口要求。

service 行为：

1. 构造 `LLMRunbookGenerator(settings, llm, RunbookActionClassifier(), RunbookPromptBuilder())`。
2. generator 校验 M9 子能力开关。
3. 若 provider 是外部 LLM，校验 external provider allow。
4. prompt builder 做 redaction，并记录 prompt metadata。
5. LLM 失败时返回 `degraded`，不创建 DB draft。
6. 内容太短时返回 `degraded`，不创建 DB draft。
7. action classifier 对生成内容做 runbook 内容安全分类。
8. service 持久化 `RunbookDraft`。
9. 显式设置 `draft.status = "pending_review"`。
10. commit。

M9 LLM draft 的硬边界：

- 不自动发布。
- 不修改 approved runbook。
- 不创建 `RunbookVersion`。
- 不 ingest chunks。
- 不执行 remediation。
- 只能等待人审后走普通 draft review。

## Draft Review 与 Publish

API：

```text
POST /api/runbooks/drafts/{draft_id}/review
```

请求只允许：

- `status = published`
- `status = rejected`

### Rejected

拒绝 draft 时：

- 更新 draft status。
- 写入 reviewer 和 review comment。
- commit。

不会创建 version，也不会 ingest chunks。

### Published

发布 draft 时：

1. 更新 draft status。
2. 创建 `RunbookVersion`。
3. 调用 `_ingest_draft_chunks(draft)`。
4. commit。

`RunbookVersion` 使用 `draft_id` 作为 document id。发布后的内容才会以 `drafts/{draft_id}.md` 为 `source_path` 进入 RAG 检索。

## Draft Chunk Ingest 降级

`_ingest_draft_chunks(draft)` 的行为：

1. 用 `parse_runbook_markdown()` 解析 draft content。
2. 解析失败时记录 warning 并返回，不中断整个 API。
3. 调用 `build_embedding_provider(get_settings())`。
4. provider 不可用时记录 warning，继续走降级 embedding。
5. 用 `split_markdown_document()` 分块。
6. 已存在相同 `content_hash` 的 chunk 会跳过。
7. 默认 embedding 为 `degraded_runbook_embedding()`。
8. 单个 chunk embedding 失败时记录 warning，继续保存降级 embedding。
9. 写入 `RunbookChunk`，并设置 `language`。

这里的取舍是：发布流程不因为单个 embedding provider 故障整体失败，关键词检索仍然可以工作；但需要通过日志和后续检查发现 embedding 降级。

## Version 查询

API：

```text
GET /api/runbooks/versions/{document_id}
```

返回 `RunbookVersionItem` 列表，包含：

- `version_id`
- `document_id`
- `version_number`
- `source_path`
- `content_hash`
- `change_reason`
- `related_incident_id`
- `related_draft_id`
- `diff_from_previous`
- `created_by`
- `created_at`

当前 draft publish 的 `document_id` 是 `draft_id`。这点在排查时很重要：如果你拿原始 Markdown 文件路径去查 draft 发布版本，通常查不到对应 version；要用发布时创建的 document id。

## M9 Web Search Context

API：

```text
POST /api/runbooks/web-search
```

web search 是 runbook enrichment context，不是发布路径。

它的职责是：

- 在 feature gate 开启后取外部上下文。
- 做 query 和内容脱敏。
- 做 URL safety 校验。
- 返回可审查的 context results。

它不会：

- 自动创建 `RunbookDraft`。
- 自动创建 `RunbookVersion`。
- 自动修改 approved runbook。
- 自动写入 RAG chunks。

生产环境必须配置 allowed domains，否则 web search provider 不应被调用。

## Incident Diff 与 Amendment

API：

```text
POST /api/runbooks/incident-diff
```

feature gates：

- `M9_EXTENSIONS_ENABLED=true`
- `INCIDENT_DIFF_LLM_ENABLED=true`

外部 provider 还需要：

- `LLM_EXTERNAL_PROVIDER_ALLOWED=true`
- API key scope 包含外部 LLM 允许 scope。

分析器边界：

- `IncidentDiffAnalyzer` 只生成 proposal。
- evidence 不足时返回 `skipped_insufficient_evidence`。
- prompt 会脱敏。
- LLM 输出必须解析为受控 JSON 或降级为低置信 note。
- proposal 只保留可信 evidence refs。
- 高置信但没有 refs 的 proposal 会降级。
- 有可信 refs 的 proposal 才是 `proposed_patch`。
- 没有可信 refs 的 proposal 是 `low_confidence_note`。

service 持久化：

1. 对每个 proposal 创建 `AmendmentDraftModel`。
2. `status = pending_review`。
3. `source = llm_incident_diff`。
4. 写入 `related_incident_id`、`runbook_version_id`、proposal content、rationale、evidence refs、confidence、proposal kind。
5. 写 audit log：`runbook.amendment_draft.created`。
6. commit。

## Amendment Review 状态机

API：

```text
POST /api/runbooks/amendments/{amendment_id}/review
```

允许目标状态：

- `approved`
- `rejected`
- `applied`
- `superseded`

状态转换约束：

| 目标状态 | 前置状态 | 额外条件 | 结果 |
|----------|----------|----------|------|
| `approved` | `pending_review` | 必须有 evidence refs | 写 `approved_by`、`approved_at`，状态变为 approved。 |
| `rejected` | `pending_review` | 无额外条件 | 状态变为 rejected。 |
| `applied` | `approved` | 必须有 evidence refs；`proposal_kind=proposed_patch`；且 `applied_to_draft_id` 与 `applied_to_runbook_version_id` 必须二选一 | 写 applied target 和 `applied_at`，状态变为 applied。 |
| `superseded` | 非 terminal | 必须提供 `superseded_by_amendment_id` | 状态变为 superseded。 |

terminal 状态：

- `rejected`
- `applied`
- `superseded`

terminal 状态不能继续转换。

每次 review 会写 audit log：

```text
runbook.amendment.<status>
```

audit payload 包含 previous/status、incident、runbook version、applied target 和 superseded target 等信息。

## Applied 的精确含义

当前 `applied` 的含义是：

```text
operator reviewed and recorded that this amendment was applied to a target draft/version reference
```

当前 `applied` 不做这些事：

- 不修改 `RunbookDraft.content`。
- 不修改 `RunbookVersion` 内容。
- 不创建新 `RunbookVersion`。
- 不重新 ingest chunks。
- 不更新已有 `RunbookChunk`。
- 不把 patch 自动合并进 Markdown。

因此，如果未来要实现真正的 amendment apply engine，需要新增独立设计：

1. patch 格式和冲突处理。
2. draft/version target 的写入规则。
3. 新 version 创建规则。
4. chunk reingest 策略。
5. audit diff。
6. rollback。
7. 测试覆盖冲突、重复应用、证据丢失和并发 review。

在当前实现里，不应把 `applied` 文案写成“系统已自动合并 runbook”。

## API Scope 与 Feature Gate

Runbook 基础接口当前依赖全局 API auth。

M9 接口有更细的 scope 和 feature gates：

| Endpoint | Scope 口径 | Feature gate |
|----------|------------|--------------|
| `/api/runbooks/llm-generate` | `runbook:review` 或 `runbook:llm_generate` | `M9_EXTENSIONS_ENABLED` + `RUNBOOK_LLM_GENERATION_ENABLED` |
| `/api/runbooks/web-search` | `runbook:review` 且 `runbook:web_search` | `M9_EXTENSIONS_ENABLED` + `RUNBOOK_WEB_SEARCH_ENABLED` |
| `/api/runbooks/incident-diff` | `runbook:review` 且 `incident:llm_diff`；外部 LLM 还需 `llm:invoke` 或 `ai:external` | `M9_EXTENSIONS_ENABLED` + `INCIDENT_DIFF_LLM_ENABLED` |

关闭 feature gate 时，接口应返回结构化状态：

- `disabled`
- `blocked`
- `degraded`
- `skipped_insufficient_evidence`

而不是绕过 gate 调用外部系统。

## 与 Frontend 的关系

当前 React console 会在 incident/run 视图中展示 runbook evidence、RAG source path 和 chunk IDs。

当前没有专门的 runbook admin 页面来完成 draft review、amendment review 或 version 管理。相关操作主要通过 API 暴露。

如果后续新增 UI，应至少覆盖：

- draft list/detail。
- draft publish/reject。
- LLM draft `pending_review` 标记。
- amendment list/detail。
- amendment review 状态约束和错误展示。
- version history。
- action classifier summary。
- feature gate disabled/degraded 状态。

## 排查清单

### Draft 发布后搜不到

优先检查：

1. `review_draft` 请求 status 是否为 `published`。
2. 是否创建了 `RunbookVersion`。
3. `_ingest_draft_chunks` 是否解析失败并 warning 返回。
4. chunk 是否因为相同 `content_hash` 被跳过。
5. `source_path` 是否为 `drafts/{draft_id}.md`。
6. search query 是否匹配 chunk 内容或 metadata。

### M9 LLM draft 没有创建 DB draft

优先检查：

1. `M9_EXTENSIONS_ENABLED`。
2. `RUNBOOK_LLM_GENERATION_ENABLED`。
3. 外部 provider 是否设置 `LLM_EXTERNAL_PROVIDER_ALLOWED=true`。
4. API key scope 是否足够。
5. LLM 调用是否返回错误。
6. 生成内容长度是否小于最小要求。
7. action classification 是否提示 forbidden 内容，需要人工重写。

### Incident diff 返回 skipped

优先检查：

1. diagnosis report 是否足够。
2. operator feedback 是否足够。
3. action execution results 是否存在。
4. linked approved runbook version 是否存在。
5. evidence refs 是否达到阈值。

### Amendment 无法 approved

优先检查：

1. 当前状态是否是 `pending_review`。
2. amendment 是否有 evidence refs。
3. 是否已经处于 terminal 状态。

### Amendment 无法 applied

优先检查：

1. 当前状态是否是 `approved`。
2. proposal kind 是否是 `proposed_patch`。
3. 是否有 evidence refs。
4. 是否只提供了一个 target：`applied_to_draft_id` 或 `applied_to_runbook_version_id`。
5. 是否误以为 `applied` 会自动修改内容。

## 测试入口

根据项目测试策略，Codex 不直接运行测试。修改 Runbook 生命周期相关代码后，建议由用户在本地运行：

```bash
pytest tests/unit/test_rag.py tests/unit/test_runbook_draft_ingest.py tests/unit/test_runbook_template_engine.py tests/unit/test_llm_runbook_generation.py -v
pytest tests/unit/test_incident_diff_analysis.py tests/unit/test_runbook_feedback.py tests/unit/test_web_search_safety.py -v
pytest tests/integration/test_runbook_api.py tests/integration/test_runbook_review_api.py tests/integration/test_amendment_draft_review.py tests/integration/test_runbook_web_context_draft.py -v
pytest tests/contract/test_runbook_api_contract.py -v
```

若改动影响 Agent RAG evidence 引用，还应补充 Agent workflow 相关 targeted tests，确认 `evidence_id`、`runbook_chunk_ids` 和 report 引用没有丢失。

## 文档维护规则

Runbook 生命周期行为变化时，至少同步：

- 本文。
- [Runbook RAG](../04-rag/runbook-rag.md)。
- [API Reference](../01-backend/api-reference.md)。
- [Data Model](../01-backend/data-model.md)。
- [LLM、Prompt、FakeLLM 与 Provider 边界技术深挖](llm-prompt-fakellm-provider-boundaries-deep-dive.md)，如果变更 M9 LLM draft 或 diff 行为。
- [反馈、NFA、关联事件与持续学习技术深挖](feedback-nfa-correlation-continuous-learning-deep-dive.md)，如果变更 amendment 或 feedback 行为。

尤其要避免把未来计划写成当前能力：

- 不要说 LLM draft 会自动发布。
- 不要说 amendment applied 会自动合并 patch。
- 不要说 web search 会自动写入 runbook。
- 不要说 external LLM 能作为 CI 稳定门禁。
