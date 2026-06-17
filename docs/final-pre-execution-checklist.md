# 最终执行前发布门禁

**最后更新：** 2026-06-14
**适用范围：** 生产环境启用、计划发现、Alertmanager 轮询、live backend、M9 受控增强

这是执行前最后一道阻塞门禁。所有 P0 必须通过；P1 如未通过必须有明确缓解措施和回滚开关。真实 LLM、live executor、M9 外部调用不能作为 CI 稳定门禁。

<p>
  <img src="assets/production-release-gate-flow.png" alt="生产发布门禁与回滚" width="900" />
</p>

## 0. Baseline

| # | 检查 | 通过标准 |
|---|------|----------|
| B0-1 | Git commit / image tag 已冻结 | 发布记录中有 commit/tag |
| B0-2 | DB migration 版本已确认 | `alembic current` 与发布记录一致 |
| B0-3 | 当前配置已记录 | `APP_ENV`、`LLM_PROVIDER`、`EXECUTOR_BACKEND`、`M9_EXTENSIONS_ENABLED` 已写入发布记录 |
| B0-4 | 回滚人和回滚窗口已确认 | 有明确 owner、命令和观察指标 |

## 1. CI 与确定性验证

| # | 检查 | 命令/证据 | 通过标准 |
|---|------|-----------|----------|
| G1 | Backend lint | `ruff check apps packages tests` | 0 error |
| G2 | Backend type check | `mypy apps packages` | 0 error 或有已记录的既有豁免 |
| G3 | Backend unit/integration coverage | `pytest tests/unit tests/integration --cov=apps --cov=packages --cov-report=term-missing --cov-fail-under=80` | 通过，后端总体覆盖率 >= 80% |
| G4 | Smoke eval | `python -m packages.evals.runner --suite smoke --output reports/eval-smoke.json` | 4 cases，top1/top3/evidence/high-risk/json/report 指标为 1.0 |
| G5 | Frontend coverage | `cd apps/web && npm run test:coverage` | statements/branches/functions/lines >= 80% |
| G6 | Frontend build | `cd apps/web && npm run build` | 成功 |
| G7 | Playwright smoke | `cd apps/web && npm run test:e2e` | 成功 |

Python `tests/e2e/`、contract、manual full eval 按变更风险追加；不是默认 CI 替代品。

## 2. 安全 P0

| # | 检查 | 通过标准 |
|---|------|----------|
| S1 | `APP_ENV=production` | 已显式设置 |
| S2 | LLM 稳定路径 | `LLM_PROVIDER=disabled`，或有手动演练批准且不作为 CI gate |
| S3 | Executor | `EXECUTOR_BACKEND=fixture`，除非本次明确是受控 live 演练 |
| S4 | API auth | `API_KEY_AUTH_ENABLED=true`，非开放路径无 token 返回 401 |
| S5 | Bootstrap key | `API_KEY_INITIAL_SEED` 已移除、轮换或限制为一次性引导 |
| S6 | Backend URL safety | localhost、metadata IP、file scheme、未 allowlist 私网地址被拒绝 |
| S7 | Secret leakage | DB/audit/log/report/prompt/state 抽样无 raw token/password/private key/auth header |
| S8 | L2/L3/L4 | L2/L3 阻断执行；L3 二次确认；L4 直接拒绝 |
| S9 | Checkpointer | PostgreSQL checkpointer 可用；不可用时不 fail open |
| S10 | Redis/Celery | `/readyz` 中 postgres/redis/celery_broker 均为 `ok` |
| S11 | Beat 单例 | 只有一个 Beat 实例 |

## 3. 生产功能门禁

| # | 检查 | 通过标准 |
|---|------|----------|
| F1 | Alertmanager poll scope | receiver/matcher/namespace/service allowlist 已验证 |
| F2 | Discovery | production 默认 `DISCOVERY_ENABLED=false`；启用时只读发现，生产不自动发布不安全后端 |
| F3 | Override | TTL 生效，secret/auth/executor/live 字段无法通过通用 override 设置 |
| F4 | API scopes | `api_key:admin` 不隐含 `config:write` 或 `discovery:write` |
| F5 | Runbook ingest | embedding 不可用时仍能入库并关键词检索 |
| F6 | Report regeneration | 生成新版本，不覆盖旧报告 |
| F7 | Email token | L3 不能通过 email token 审批 |
| F8 | Live diagnostics | K8s/DB live diagnostics 仍只读 |
| F9 | Live executor | 仅显式 opt-in，且只允许 restart/pause/resume/scale/rollback 受控 K8s mutation |

## 4. M9 门禁

先验证 baseline：

| # | 检查 | 通过标准 |
|---|------|----------|
| M9-1 | `M9_EXTENSIONS_ENABLED=false` | M8 smoke 通过，M9 子功能均 disabled |
| M9-2 | 子功能 conflict | 子功能 true + 全局 false 时记录 warning/metric，功能不生效 |
| M9-3 | Jaeger 独立性 | M9 false 不禁用已验证的 Jaeger trace backend |
| M9-4 | 回滚变量 | `PRE_M9_TRACE_BACKEND`、`PRE_M9_TRACE_ENABLED` 已记录 |

启用任一 M9 子能力前额外验证：

| 功能 | 必须通过 |
|------|----------|
| LLM runbook draft | 只生成 `RunbookDraft(status=pending_review)`，不发布、不审批、不执行 |
| LLM incident diff | 证据不足跳过；证据充分只生成 `AmendmentDraft(status=pending_review)` |
| Web search | HTTPS/allowlist/blocked domains、redaction、timeout、audit、metric、degraded result |
| Tempo backend | 显式 `TRACE_BACKEND=tempo`，不可达时 degraded；不影响 Jaeger rollback |
| Tempo discovery | 生产最多 `requires_review`，绝不 auto-publish |
| Grafana ingest | 默认 disabled；启用时 HMAC、payload size、fingerprint dedup 通过 |
| Semantic search | embedding 失败回退关键词/混合检索 |
| External embedding | 需要 semantic search + external embedding gate + safe URL + scope |
| External LLM | 需要 M9 gate + 子功能 gate + `LLM_EXTERNAL_PROVIDER_ALLOWED=true`，只可手动演示 |

## 5. 回滚命令已演练

基础安全回滚：

```bash
export EXECUTOR_BACKEND=fixture
export LLM_PROVIDER=disabled
export DISCOVERY_ENABLED=false
export ALERT_SOURCE=webhook
```

M9 完全回滚：

```bash
export M9_EXTENSIONS_ENABLED=false
export TRACE_BACKEND=${PRE_M9_TRACE_BACKEND}
export TRACE_ENABLED=${PRE_M9_TRACE_ENABLED}
```

子功能回滚：

```bash
export RUNBOOK_LLM_GENERATION_ENABLED=false
export LLM_INCIDENT_DIFF_ENABLED=false
export RUNBOOK_WEB_SEARCH_ENABLED=false
export TEMPO_DISCOVERY_ENABLED=false
export GRAFANA_ALERT_INGEST_ENABLED=false
export SEMANTIC_RUNBOOK_SEARCH_ENABLED=false
export EXTERNAL_EMBEDDING_PROVIDER_ENABLED=false
export LLM_EXTERNAL_PROVIDER_ALLOWED=false
```

回滚后必须复验：`/readyz`、smoke eval、审批流程、report generation、关键 `agentp_*` 指标。

## 6. 前 24 小时观察

| 指标/信号 | 目标 |
|-----------|------|
| `/readyz` | 持续 ready |
| `agentp_diagnosis_total` | 无异常失败尖峰 |
| `agentp_active_diagnoses` | 无长期卡住 |
| `agentp_tool_call_total` | degraded/failed 比例可解释 |
| `agentp_approval_total` | L2/L3 审批流转正常 |
| `agentp_llm_call_errors_total` | 默认 disabled/fake 路径不应有真实 provider 错误 |
| `agentp_m9_feature_enabled` | 未启用 M9 时全部 0 |
| `agentp_m9_feature_flag_conflict_total` | 应为 0；非 0 要检查环境变量冲突 |
| Audit log | 无 raw secret，无异常写操作 |
| Worker logs | 无 repeated checkpoint/build deps failure |

## 相关文档

- [生产环境检查清单](production-checklist.md)
- [运维 Runbook](10-operations/runbook.md)
- [测试策略](07-testing/testing-strategy.md)
- [评测体系](09-evals/evaluation.md)
- [M9 发布计划](m9-rollout.md)
- [M9 威胁模型](m9-threat-model.md)
- [Day-2 运维操作手册](operator-runbook.md)
