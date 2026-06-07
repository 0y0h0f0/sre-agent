# Runbook RAG

## 目标

Runbook RAG 用于把静态运维知识带入诊断上下文，并让根因和处置建议能引用具体来源。RAG 结果必须包含 chunk ID 和 source path。

## Runbook 来源

默认 Runbook 位于：

```text
demo/runbooks/
```

MVP 四类事故在 `demo/runbooks/checkout-api/` 下有对应目录。

## 入库流程

`RunbookIngestor.ingest_path()`：

1. 扫描 `.md` 文件。
2. 解析 front matter 和正文。
3. 使用 Markdown-aware splitter 切分。
4. 生成 deterministic fake embedding。
5. 按 `content_hash` 去重。
6. 写入 `runbook_chunks`。

API：

```http
POST /api/runbooks/ingest
```

请求：

```json
{
  "path": "demo/runbooks",
  "reingest": true
}
```

## Chunk 规则

Splitter 规则：

- target tokens：约 450。
- max tokens：900。
- overlap tokens：80。
- 优先按 H2 section 切分。
- 超长 section 按段落切分并保留 overlap。
- 每个 chunk 保留 title、parent title、source path、metadata、content hash。

`runbook_chunks.embedding` 为 384 维。

## FakeEmbedding

FakeEmbedding 是 deterministic：

- 对规范化文本做 SHA-256 扩展。
- 生成 384 维向量。
- 向量归一化。
- 同一文本总是得到同一 embedding。

测试不得使用随机向量。

## 检索流程

`RunbookRetriever.search()`：

1. 校验 `RunbookSearchQuery`。
2. 构造 runbook search cache key。
3. 计算 query embedding。
4. 遍历 chunk，按 metadata 过滤 service 和 incident_type。
5. 计算向量余弦分数和 lexical overlap。
6. 可选执行 BM25 recall。
7. 使用 adaptive alpha 做 hybrid score fusion。
8. 召回 top 20。
9. rerank。
10. 返回 top_k。

响应项：

```json
{
  "chunk_id": "chk_xxx",
  "source_path": "demo/runbooks/checkout-api/high-5xx/triage.md",
  "title": "Triage",
  "excerpt": "...",
  "score": 0.92,
  "metadata": {}
}
```

## Hybrid Search

配置项：

- `RUNBOOK_HYBRID_SEARCH_ENABLED`
- `RUNBOOK_HYBRID_ALPHA_KEYWORD`
- `RUNBOOK_HYBRID_ALPHA_NL`

BM25 通过 PostgreSQL text search helper 构造 tsquery。检索结果会与向量/词法分数融合。

## Reranker

配置项：

- `RERANKER_PROVIDER`
- `RERANKER_COHERE_API_KEY`
- `RERANKER_COHERE_MODEL`
- `RERANKER_JINA_BASE_URL`
- `RERANKER_JINA_MODEL`
- `RERANKER_BGE_BASE_URL`
- `RERANKER_BGE_MODEL`

默认 fake reranker，保持本地测试稳定。

## Runbook Search Tool

Agent 通过 `RunbookSearchTool` 使用 RAG，不直接访问 retriever。工具层会返回 `ToolResult`，并记录 cache 和审计摘要。

## 草稿与版本

当前实现包含 Runbook draft/version 能力：

- `GET /api/runbooks/drafts`
- `GET /api/runbooks/drafts/{draft_id}`
- `POST /api/runbooks/drafts/generate`
- `POST /api/runbooks/drafts/{draft_id}/review`
- `GET /api/runbooks/versions/{document_id}`

草稿用于把事故反馈和历史诊断转为待审核 Runbook。版本表记录 document 的内容 hash、版本号、diff 和关联事故。

## 诊断引用要求

诊断输出不能只说“根据 runbook”。应引用：

- `chunk_id`
- `source_path`
- evidence ID

压缩和报告生成也应保留这些 ID。
