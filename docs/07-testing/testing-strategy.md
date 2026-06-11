# 测试策略

## 目标

测试覆盖应验证功能正确性、安全边界和回归风险。尤其必须覆盖：

- fingerprint 去重。
- Celery 幂等性。
- LangGraph checkpoint resume。
- post-action snapshot、verify 和 degraded 回滚/replan。
- fixture executor 默认、live executor opt-in 与 namespace 传递。
- L2/L3 审批阻断执行。
- L3 缺少二次确认。
- L4 直接拒绝。
- FakeEmbedding determinism。
- context compression trigger。
- compression 后 evidence ID 保留。
- provider/app cache metrics 分离。
- Runbook search 返回 source 和 chunk ID。

## 后端命令

```bash
pytest tests/unit tests/integration --cov=apps --cov=packages --cov-report=term-missing --cov-report=xml --cov-fail-under=80
ruff check apps packages tests
mypy apps packages
```

项目 `pyproject.toml` 中 coverage source 为 `apps` 和 `packages`，并忽略 migrations、tests 和 packages/evals。

## 前端命令

```bash
cd apps/web
npm run test
npm run test:coverage
npm run test:e2e
npm run build
```

前端覆盖率门禁：

- statements >= 80。
- branches >= 80。
- functions >= 80。
- lines >= 80。

## 测试目录

```text
tests/
  unit/
  integration/
  contract/
  manual/
apps/web/src/
  *.test.tsx
  *.test.ts
  e2e/
```

## 当前后端测试分布

Unit：

- agent core、agent nodes、guardrails、LLM providers。
- common helpers、settings、session。
- schemas、repositories、tool calls。
- tools、phase2 tools。
- RAG、memory、topology。
- email notifications、feedback、cross incident、API key service。

Integration：

- alert API。
- approval API。
- health API。
- report API。
- runbook API。
- graph flow。
- worker task。
- worker tool audit。
- eval runner。
- feedback API。
- phase6 collaboration。
- email SMTP delivery。

Contract：

- Runbook API contract。

Manual：

- SMTP connectivity。
- real email delivery。

## FakeLLM 规则

CI、unit tests、integration smoke 和 smoke eval 使用 FakeLLM。不得把真实 LLM key、网络 LLM 服务或 provider 额度作为稳定测试依赖。

## 外部服务隔离

测试应优先使用：

- fixture tools。
- FakeEmbedding。
- FakeLLM。
- in-memory 或 transactional DB。
- mocked HTTP client。
- injected task enqueue function。

测试不应：

- 写真实 Kubernetes。
- 写真实 cloud。
- flush 真实 Redis cache。
- 修改真实数据库数据。
- 发送真实邮件，除非是 `tests/manual` 且显式设置 `RUN_REAL_EMAIL_TEST=true`。

## 真实邮件手动测试

```bash
RUN_REAL_EMAIL_TEST=true pytest tests/manual/test_smtp_connectivity.py -q
RUN_REAL_EMAIL_TEST=true pytest tests/manual/test_real_email_delivery.py -q
```

真实发送测试只应发送一封 smoke email 到 `SRE_EMAIL_LIST`。

## E2E

Playwright smoke test 位于：

```text
apps/web/src/e2e/smoke.spec.ts
```

E2E 需要前端 dev server。若测试依赖 API mock 或真实 API，应在 spec 中明确 setup，避免使用未准备好的外部状态。

## 增加测试的准则

- 新 schema：加 schema validation 单元测试。
- 新 repository：加 repository 单元测试或 integration 测试。
- 新 service：加业务分支和错误分支测试。
- 新 API：加 integration 或 contract 测试。
- 新 Agent 节点：加节点纯函数测试和 graph flow 覆盖。
- 新工具：mock HTTP/data source，测试 timeout/degraded/cache/audit summary。
- 新审批逻辑：测试冲突、L2/L3/L4、resume 幂等。
- 新前端页面：测试 loading/empty/error/success 和关键交互。
