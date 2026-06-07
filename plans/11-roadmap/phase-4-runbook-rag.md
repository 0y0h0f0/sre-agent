# Phase 4：Runbook RAG 增强（知识引擎）

目标：从检索到理解。在现有 pgvector 检索（见 `04-rag/runbook-rag.md`）基础上增加混合检索、reranker、自动生成与多语言能力。

> 完成记录：混合检索、reranker、Runbook 草稿、版本管理和多语言 embedding 配置已纳入当前实现；默认仍可用 FakeEmbedding 保持测试确定性。

## 4.1 混合检索

目标：BM25（关键词）+ 向量（语义）混合，解决 embedding 对专业术语不敏感的问题。

| 任务 | 细节 |
| --- | --- |
| BM25 索引 | 用 PostgreSQL `tsvector` / Elasticsearch / Quickwit 实现全文检索 |
| 混合排序 | `final_score = alpha * bm25_score + (1-alpha) * vector_similarity` |
| 自适应权重 | 告警名命中 runbook 标题 → alpha 调高；自然语言描述 → alpha 调低 |

## 4.2 Reranker 二次排序

目标：粗排（pgvector）→ 精排（Reranker），提升 Top-3 命中率。

| 选项 | 说明 |
| --- | --- |
| Cohere Rerank | 托管服务，效果好，有免费额度 |
| Jina Reranker | 开源，可本地部署（`jinaai/jina-reranker-v2`） |
| BGE-Reranker | 中文场景推荐（`BAAI/bge-reranker-v2-m3`） |

## 4.3 Runbook 自动生成

目标：从历史成功诊断中自动提取 runbook。

| 任务 | 细节 |
| --- | --- |
| 模板提取 | 同一 fingerprint 多次出现 → LLM 总结共性 → 生成 runbook 草稿 |
| 人工审核 | 草稿 → SRE 审核/编辑 → 正式入库 |
| 版本管理 | runbook 关联 incident，记录每次更新原因 |

## 4.4 多语言 Runbook

目标：支持中文 runbook 的嵌入和检索。

| 任务 | 细节 |
| --- | --- |
| 中文 Embedding | `BAAI/bge-small-zh` 或 `text2vec-large-chinese` |
| 混合语言检索 | 英文 query 搜中文文档 → Cross-Lingual Reranker |
| 双语 Runbook | 同一 runbook 同时维护中英文版本 |

**验收标准**：4 类 MVP 故障 Runbook Top-3 命中率 > 80%。
