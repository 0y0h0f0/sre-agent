# 里程碑拆分

## M1：项目骨架、数据库与基础 API

生成内容：FastAPI、React + Vite、PostgreSQL、Redis、Celery、SQLAlchemy、Alembic、基础 incident API。

验收：可以创建 incident，重复 fingerprint 去重，后端覆盖率 > 80%。

## M2：工具层、Loki 日志与模拟故障

生成内容：MetricsTool、LogsTool、TraceTool、GitChangeTool、demo-service、Prometheus、Loki、OTel Collector 配置。

验收：故障注入后能查到指标和日志，工具失败可降级，`tool_calls` 有审计。

## M3：Runbook RAG 与证据引用

生成内容：Runbook 文档、splitter、embedding、pgvector 入库、RunbookSearchTool、检索 API。

验收：4 类故障都能检索到 Runbook，结果包含来源和片段。

## M4：LangGraph 诊断工作流

生成内容：Agent state、节点、Celery diagnosis task、run 轨迹持久化、诊断 API。

验收：4 类 mock alert 都能生成根因、证据、处置建议和报告。

## M5：审批、Guardrail 与 Mock Executor

生成内容：风险分级、审批 API、mock executor、LangGraph interrupt/resume。

验收：L2/L3 无审批不能执行，L4 直接拒绝，高风险拦截率 100%。

## M6：React 前端控制台

生成内容：Incident 列表、详情、Agent run、审批、报告页面。

验收：页面能完成查看诊断和审批动作，前端覆盖率 > 80%。

## M7：评测、工程指标与简历包装

生成内容：20 到 50 个样本、eval runner、指标报告、README、演示材料。

验收：一条命令运行 smoke eval，输出根因命中率和性能指标。
