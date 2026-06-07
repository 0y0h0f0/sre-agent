# 术语表

## Agent run

一次 LangGraph 诊断运行。一个 incident 可以有多个 agent run。

## App cache

应用内部缓存，例如 request-local tool cache 或 prompt segment cache。不能等同 provider prompt cache。

## Approval

人工审批记录。L2/L3 动作需要审批，L3 还需要二次确认。

## Checkpointer

LangGraph checkpoint persistence。真实 PostgreSQL 下使用 `PostgresSaver`，用于 human approval interrupt 和 resume。

## Evidence

诊断证据，来源可以是 metrics、logs、traces、deployment、k8s、db、runbook、memory 等。根因必须引用 evidence ID 或 Runbook chunk ID。

## FakeEmbedding

本地 deterministic embedding 实现，输出 384 维归一化向量。

## FakeLLM

本地 deterministic LLM adapter，用于测试、CI smoke eval 和本地 demo。

## Guardrail

确定性风险策略。负责把动作分类为 L0-L4，并决定是否允许、是否审批、是否直接拒绝。

## Incident

事故记录，由 alert 创建。open fingerprint 去重。

## L4

禁止级风险。包括 delete data、truncate table、flush cache、modify database 等，直接拒绝。

## Mock executor

MVP 动作执行器。只返回固定 mock 结果，不调用真实系统。

## NFA

Not Actionable Alert。用户可把事故标记为 NFA，用于反馈和后续自动降级。

## Provider prompt cache

LLM provider 的 prefix caching 行为。只有 provider 返回或 adapter 可判断时才可统计。

## RAG

Retrieval-Augmented Generation。这里指 Runbook chunk 的 embedding/BM25/rerank 检索，并将结果带入诊断上下文。

## Runbook chunk

Runbook Markdown 被切分后的检索单元，包含 `chunk_id`、source path、title、excerpt/content、metadata 和 embedding。

## Shadow mode

无副作用的平行评测模式。当前完成口径是只写 eval 表、记录 shadow model/prompt 元信息，不修改真实 incident/action/approval，也不执行真实动作。
