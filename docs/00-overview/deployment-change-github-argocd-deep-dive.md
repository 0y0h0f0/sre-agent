# Deployment Change、GitHub、Argo CD 与发布变更证据技术深挖

**最后更新：** 2026-06-18

本文解释发布变更证据如何从 fixture、GitHub 或 Argo CD 进入 Agent 诊断链路。它补充 [工具层](../03-tools/tool-layer.md)、[工具与证据技术深挖](tool-evidence-deep-dive.md) 和 [Observability 与后端适配器技术深挖](observability-backend-adapters-deep-dive.md)：那些文档定义工具协议、证据持久化和后端接入，本文聚焦 deployment change 本身。

核心边界：`GitChangeTool` 是只读证据工具。它不会发布、取消、回滚或修改部署。真正的 Kubernetes rollback / scale / restart 只能走 `guardrail_check -> human_approval -> take_snapshot -> execute_action -> verify`，且默认 executor 仍是 fixture。

## 阅读目标

读完本文应能回答：

- `collect_deployment` 如何为一次 incident 查询发布变更证据。
- `DEPLOYMENT_BACKEND=fixture|github|argocd` 分别读取什么数据。
- GitHub deployments、GitHub commits fallback 和 Argo CD sync history 如何归一成同一种 evidence。
- deployment evidence 如何影响交叉验证、假设排序、报告和 runbook 引用。
- 为什么“发现最近部署”不是“自动回滚”，以及 rollback action 的审批边界在哪里。
- 后端降级时应该看哪些日志、`tool_calls` 和测试入口。

## 代码入口

| 主题 | 入口 |
|------|------|
| Agent 节点 | `packages/agent/nodes/collect_deployment.py` |
| Gap-fill 重新采集 | `packages/agent/nodes/collect_gap.py` 的 `_safe_query_deployment()` |
| 工具 schema 和输出 | `packages/tools/git_changes.py` |
| 后端适配器 | `packages/tools/deployment_backends.py` |
| worker 依赖构造 | `apps/worker/tasks.py` 的 `_build_deps()` |
| 证据交叉验证 | `packages/agent/evidence_validation.py` |
| 假设排序 | `packages/agent/nodes/rank_hypotheses.py` |
| fallback 诊断与动作 | `packages/agent/rules_fallback.py`、`packages/agent/fake_llm.py` |
| 执行和 rollback 边界 | `packages/agent/nodes/execute_action.py`、`packages/tools/executor_backends.py` |
| 配置 | `packages/common/settings.py`、`docs/11-reference/configuration.md` |
| 测试 | `tests/unit/test_tools.py`、`tests/unit/test_tools_phase2.py` |

## 端到端路径

```text
_build_deps()
  -> build_deployment_backend(settings)
  -> GitChangeTool(backend=fixture|github|argocd, cache=RequestLocalToolCache)

collect_deployment
  -> build GitChangeQuery(service, start-30m, end+30m)
  -> deps.git_change_tool.run(query)
  -> record tool_calls
  -> return deployment_evidence

collect_all_evidence
  -> persist deployment evidence to evidence_items
  -> write evidence_id back into state evidence

diagnose / cross_validate / rank_hypotheses / report
  -> use deployment evidence as correlation signal
  -> cite evidence_id or include deployment summary

plan_actions / guardrail_check / human_approval / execute_action
  -> any rollback action still requires deterministic guardrail and approval
```

## 1. Runtime 依赖构造

`apps/worker/tasks.py` 在 `_build_deps()` 中创建 deployment change 工具：

```python
git_change_tool = GitChangeTool(
    backend=build_deployment_backend(settings),
    timeout_seconds=timeout,
    cache=cache,
)
```

这个依赖随后注入 `AgentDeps.git_change_tool`。Agent 节点只调用 `deps.git_change_tool.run(query)`，不直接创建 GitHub client、Argo CD client 或读取 fixture 文件。

`build_deployment_backend(settings)` 只读取 process settings，不读取未发布 discovery proposal：

| 配置 | 行为 |
|------|------|
| `DEPLOYMENT_BACKEND=fixture` | 使用 `GIT_CHANGES_FIXTURE_PATH`，默认 `demo/faults/git_changes.json`。 |
| `DEPLOYMENT_BACKEND=github` | 需要 `GITHUB_REPO`；使用 `GITHUB_API_URL` 和可选 `GITHUB_TOKEN`。 |
| `DEPLOYMENT_BACKEND=argocd` | 使用 `ARGOCD_URL` 和可选 `ARGOCD_TOKEN`。 |
| 其它值 | 构造失败，worker run 失败关闭。 |

GitHub/Argo token 只在 backend client 构造和请求 header 中使用。工具输出、evidence、state、prompt、audit summary 和文档示例都不应包含原始 token。

## 2. Query Window

`collect_deployment` 从 incident state 读取：

- `service_name`
- `time_window.start`
- `time_window.end`

然后把查询窗口前后各扩展 30 分钟：

```text
query.start = incident_start - 30m
query.end   = incident_end + 30m
```

原因是发布和告警之间通常存在延迟：部署可能先完成，错误率随后上升；也可能在告警窗口内出现二次同步。这个扩展只影响只读查询窗口，不会扩大任何执行权限。

`GitChangeQuery` 会把时间统一为 UTC aware datetime，并拒绝 `end <= start`。`service` 会 trim，随后进入 redaction 和 cache key 构造。

## 3. Backend 输出归一化

所有 backend 都归一成同一字段：

```json
{
  "service": "checkout",
  "deployed_at": "2026-06-08T07:46:40Z",
  "commit_sha": "a1b2c3d",
  "author": "demo",
  "summary": "Raise checkout payment client timeout and switch retry policy",
  "files": ["services/checkout/payment_client.py", "deploy/checkout.yaml"]
}
```

`GitChangeTool` 只保留最多 10 条匹配变更，并在公共输出层执行 redaction：

- `service`、`deployed_at`、`author`、`summary`、`files` 走文本脱敏。
- `commit_sha` 作为排障关联 ID 保留；如果字段异常携带 secret 或内部 endpoint，会被脱敏。
- backend 异常消息也会先脱敏，再进入 `ToolResult.error_message`。

### Fixture backend

`FixtureDeploymentBackend` 读取 JSON 文件，默认是 `demo/faults/git_changes.json`。它是本地 demo、测试和 CI 的默认路径。

fixture 文件中的 `changes` 是列表；非列表会抛出 `ValueError`，由 `GitChangeTool` 转成 `degraded`。工具层会在查询阶段再按 service 和时间窗口过滤，因此 fixture 可以包含多个服务或多条变更。

### GitHub backend

`GitHubDeploymentBackend` 是只读 GitHub API client：

1. 先调用 `/repos/{owner}/{repo}/deployments`，带 `environment=service`。
2. 如果部署记录非空，映射 deployment record：`created_at`、`sha`、`creator.login`、`description/ref`。
3. 如果没有 deployment records，回退到 commits API：
   - 用 incident 查询窗口作为 `since` / `until`。
   - 最多读取 `_MAX_COMMIT_DETAIL_LOOKUPS=30` 个 commit detail。
   - 用 changed files 判断 commit 是否和 service 相关。

commit service 匹配规则是保守启发式：服务名、下划线变体、去掉 `api` / `service` 这类泛词后的片段，会和文件路径片段比较。它只用于证据相关性，不是发布权限判定。

### Argo CD backend

`ArgoCDDeploymentBackend` 读取 `/api/v1/applications/{service}`，从 `status.history` 提取 sync history：

- `deployedAt` -> `deployed_at`
- `revision` 前 7 位 -> `commit_sha`
- `summary` -> `Argo CD sync to <revision>`

Argo CD history 通常是 oldest-first，backend 会 reverse 成 newest-first，再交给工具层按窗口过滤。

当前 backend 只读取单个 application 名称等于 service 的 sync history；它不做 app discovery、不执行 sync、不回滚 application。

## 4. ToolResult、Cache 和 Evidence

`GitChangeTool.run()` 的结果形状：

| 字段 | 行为 |
|------|------|
| `status` | 找到匹配变更为 `succeeded`；没有匹配为 `degraded`；timeout 为 `timeout`；HTTP/解析/数据错误为 `degraded`。 |
| `data.change_count` | 时间窗口内匹配变更总数。 |
| `data.changes` | 最多 10 条公共变更记录。 |
| `summary` | 有变更时包含 service、changes、latest commit；无变更时是 `no deployment changes for <service>`。 |
| `evidence` | 有变更时写一条 type=`git`、source=`backend.name` 的 evidence。 |
| `cache_key` | 600 秒 UTC bucket，包含 backend name 作为 datasource。 |

注意 evidence type 当前来自工具实现为 `git`，而 state bucket 名称是 `deployment_evidence`。文档和 UI 讨论时通常称为 deployment evidence，因为它表达的是发布变更信号。

当没有匹配变更时，工具不会生成 `ToolResult.evidence`。`collect_deployment` 会构造一条轻量 fallback evidence，保留 status 和 summary，供后续 state/debug 使用；是否落到 `evidence_items` 取决于批量持久化逻辑对该 evidence 的处理。

## 5. 诊断如何消费 Deployment Evidence

deployment evidence 有三个主要作用。

### 交叉验证

`packages/agent/evidence_validation.py` 把 deployment 信号权重设为 `0.4`，低于 traces、metrics、K8s、logs 和 DB：

```text
Trace > Metrics > K8s > Logs > DB > Git/deployment
```

如果 payload 中有 `change_count > 0` 或 `changes`，deployment 方向是 `anomaly`，表示“最近部署与 incident 时间相关”。如果没有部署，方向是 `None`，也就是中性信号，不会当作健康反证。

这个不对称规则很重要：没有部署不代表系统健康，只说明“该 incident 不太像 deployment-correlated regression”。

### 假设排序

`rank_hypotheses` 会在存在 deployment evidence 且 hypothesis 文本包含 `deploy`、`release` 或 `rollback` 时给 `deployment_correlation=0.8`。它只是排序加分，不会单独决定 root cause。

### 报告和上下文

`build_context`、`diagnose`、`generate_report` 都会把 `deployment_evidence` 纳入 evidence set。持久化后，诊断输出和报告应引用 `evidence_id`，而不是只写“最近有发布”这种不可追溯结论。

## 6. 与 Rollback 执行的边界

deployment evidence 可能让 FakeLLM、rules fallback 或真实 provider 提出 rollback 类动作，但提出动作不等于执行动作。

安全链路固定是：

```text
plan_actions
  -> guardrail_check
  -> human_approval
  -> take_snapshot
  -> execute_action
  -> verify
```

关键边界：

- `rollback_release` / `rollback_deployment` 是 L3，需要人工审批和二次确认。
- L3 approve 必须验证 `risk_ack=true`、`confirm_action_type`、`confirm_target`。
- 默认 `EXECUTOR_BACKEND=fixture`，本地 demo 和 CI 不执行真实 rollback。
- `EXECUTOR_BACKEND=live` 只允许当前受控 Kubernetes mutation；GitHub 和 Argo CD backend 仍然只是只读证据来源。
- `/api/actions/{action_id}/execute` 当前固定 fixture executor，不是 live rollback 入口。
- Argo CD backend 读取 sync history，但 live executor 不调用 Argo CD rollback/sync API。

也就是说，GitHub/Argo CD 变更证据最多支持“判断是否与发布相关”和“解释为什么建议回滚”，不能绕过 guardrail、approval、snapshot 或 verify。

## 7. 常见失败路径

| 现象 | 可能原因 | 排查入口 |
|------|----------|----------|
| `deployment backend unavailable` | GitHub/Argo URL、token、repo、网络或 JSON 解析失败 | `tool_calls.error_message`、worker 日志、`tests/unit/test_tools_phase2.py` |
| `no deployment changes` | 窗口内没有匹配变更，或 service 与 GitHub environment / Argo app 名不一致 | `tool_calls.query`、fixture/后端原始数据、service naming |
| GitHub deployments 为空但 commits 也为空 | repo 没有 deployment records，且 commit 文件路径无法匹配 service | commit files、`_commit_matches_service()` 规则 |
| Argo CD 有 history 但未命中 | `deployedAt` 不在扩展窗口内，或 application 名称不是 service | incident time window、service/app 映射 |
| 诊断说 deployment 相关但 evidence 不清楚 | evidence 没有持久化 ID 或 report 没引用 ID | `evidence_items`、`agent_runs.state.deployment_evidence`、report payload |
| 出现 rollback 建议 | planner 基于证据提出动作 | 看 `guardrail_check`、approval 状态、executor backend，确认没有绕过 L3 |

## 8. 测试入口

| 测试 | 覆盖 |
|------|------|
| `tests/unit/test_tools.py::test_git_change_tool_reads_fixture_and_filters_window` | fixture 读取、service/window 过滤。 |
| `tests/unit/test_tools_phase2.py::test_github_deployment_backend_maps_changes` | GitHub deployments 映射。 |
| `tests/unit/test_tools_phase2.py::test_github_deployment_backend_falls_back_to_service_commits` | GitHub commits fallback 和 service 文件匹配。 |
| `tests/unit/test_tools_phase2.py::test_argocd_deployment_backend_reverses_history` | Argo CD history reverse 和字段映射。 |
| `tests/unit/test_tools_phase2.py::test_deployment_backend_http_error_degrades` | HTTP 错误降级。 |
| `tests/unit/test_tools_phase2.py::test_git_change_tool_redacts_change_values_from_backend` | backend 返回值脱敏。 |
| `tests/unit/test_tools_phase2.py::test_git_change_tool_redacts_sensitive_service_before_backend_and_output` | query service 脱敏和 cache/output 边界。 |
| `tests/unit/test_tools_phase2.py::test_build_deployment_backend_selects_by_setting` | settings 到 backend 的选择逻辑。 |

新增 deployment backend 时，至少补齐：

1. settings 构造测试。
2. 成功映射测试。
3. 空结果测试。
4. timeout/HTTP/解析错误降级测试。
5. secret redaction 测试。
6. cache datasource 隔离测试。
7. 说明该 backend 只读，不新增真实写路径。

## 9. 变更 Checklist

修改 deployment change 链路时按这个顺序处理：

1. 更新 `packages/tools/deployment_backends.py` 的 backend 或映射逻辑。
2. 确认 `GitChangeTool` 输出仍是统一字段，不泄漏 secret。
3. 如果新增配置，更新 `packages/common/settings.py`、`.env.example` 和 [配置参考](../11-reference/configuration.md)。
4. 如果 evidence 语义变化，更新 [工具层](../03-tools/tool-layer.md)、[工具与证据技术深挖](tool-evidence-deep-dive.md) 和本文。
5. 如果 planner 会新增 action 类型，必须同步 guardrail、approval、executor capability、测试和安全文档；不要只改 prompt。
6. 如果涉及真实写入，先确认是否违反当前项目边界。GitHub/Argo CD deployment backend 当前只能保持只读。
