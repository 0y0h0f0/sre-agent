# Runbook RAG 设计

## 目录

```text
demo/runbooks/
  checkout-api/
    high-5xx.md
    db-connection-exhaustion.md
    cache-avalanche.md
    pod-restart-loop.md
packages/rag/
  ingest.py
  splitter.py
  metadata.py
  embeddings.py
  retriever.py
  reranker.py
```

## Runbook front matter

每篇 Runbook 必须包含元数据：

```yaml
---
service: checkout-api
incident_type: high_5xx
severity: P1
owner: payment-team
updated_at: 2026-05-31
---
```

## 切分策略

目标：保留标题层级，避免把步骤和前置条件切断。

规则：

- 优先按二级标题切分。
- chunk token 目标 300 到 600。
- chunk 最大 900 token。
- chunk overlap 80 token。
- 每个 chunk 保留文档标题、父标题、source_path。

## 入库流程

1. 扫描 Markdown 文件。
2. 解析 front matter。
3. 计算 document hash。
4. 切分 chunk。
5. 计算 chunk content hash。
6. 已存在 hash 则跳过。
7. 生成 embedding。MVP 固定 384 维，FakeEmbedding 必须 deterministic。
8. 写入 `runbook_chunks`。

## 检索流程

输入：

```python
class RunbookSearchQuery(BaseModel):
    query: str
    service: str | None = None
    incident_type: str | None = None
    top_k: int = 5
```

流程：

1. query normalization。
2. Redis 查 search cache。
3. embedding query。
4. pgvector top 20 recall。
5. metadata filter：service、incident_type、severity。
6. rerank：向量分数、标题匹配、服务匹配、更新时间。
7. 返回 top_k。

## Rerank 分数

```text
score = vector_score * 0.65
      + service_match * 0.15
      + incident_type_match * 0.10
      + title_keyword_match * 0.05
      + freshness_score * 0.05
```

## 诊断引用要求

LLM 看到的 Runbook context 必须包含：

```text
[chunk_id=chk_123 source=demo/runbooks/checkout-api/high-5xx.md title="Rollback checks"]
content excerpt...
```

诊断输出必须引用 `chunk_id`，不能只写来源文件名。

## 缓存策略

- embedding cache：`embedding:{model}:{dimension}:{text_hash}`，长期缓存。
- search cache：`runbook_search:{query_hash}:{service}:{incident_type}:{top_k}`，TTL 30 分钟。
- chunk text cache：`runbook_chunk:{chunk_id}`，TTL 1 小时。

## Embedding 约定

- MVP 数据库列使用 `vector(384)`。
- `embedding_provider=fake` 时，对输入文本做稳定 hash，再生成 deterministic 384 维向量。
- 测试不得依赖随机向量。
- 更换真实 embedding model 时必须同步更新 `embedding_model`、维度和迁移。

## 测试

- splitter 不破坏标题层级。
- front matter 缺失时报校验错误。
- 重复 ingest 不产生重复 chunk。
- service filter 生效。
- 每类故障 query 都能命中目标 Runbook。
- 返回结果必须包含 `chunk_id`、`source_path`、`title`、`excerpt`。
