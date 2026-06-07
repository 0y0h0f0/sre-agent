# Phase 3：告警源与通知（闭环打通）

目标：从被动查到主动推。接入多种告警源，并通过邮件覆盖全部通知场景。告警入口对应 `01-backend/api-contract.md` 的 `POST /api/alerts`。

> 完成记录：多来源告警入口、邮件模板、异步发送、审批/报告链接和邮件日志已纳入当前实现。SMTP 真实发送仍作为手动 smoke，默认测试不依赖外部邮件服务。

## 3.1 多告警源接入

| 来源 | 集成方式 |
| --- | --- |
| **Alertmanager** | Webhook → POST `/api/alerts` |
| **PagerDuty** | Webhook v3 → 解析 payload → POST `/api/alerts` |
| **Grafana Alerting** | Webhook → POST `/api/alerts` |
| **Datadog** | Webhook → 解析 metric alert → POST `/api/alerts` |
| **自定义 Webhook** | 通用 JSON 适配器，自动映射字段 |

**统一告警模型**：

```python
class AlertPayload(BaseModel):
    source: str          # alertmanager | pagerduty | grafana | datadog | custom
    fingerprint: str     # 去重 key
    service: str
    severity: Severity   # P1-P4
    alert_name: str
    starts_at: datetime
    labels: dict
    annotations: dict
    raw_payload: dict    # 原始 payload 保留，用于审计回溯
```

> fingerprint 去重沿用 MVP 的 open incident 去重约束（见 `01-backend/data-model.md`）。

## 3.2 邮件通知

目标：通过 SMTP 邮件覆盖所有通知场景，保持简单可控。

| 触发场景 | 邮件类型 | 收件人 |
| --- | --- | --- |
| 新告警接入 | `[P1] New Incident: DatabaseConnectionExhaustion` | 配置的 SRE 邮件列表 |
| 诊断完成 | `[P1] Diagnosis Complete: checkout` + 根因摘要 | 同上 |
| 需要审批 (L2/L3) | `[ACTION REQUIRED] L3 Approval: rollback_release` | 审批人邮件 |
| L3 二次确认 | `[CONFIRM] L3 Action: rollback_release on checkout` | 审批人邮件（包含确认链接） |
| 事后报告 | `[REPORT] Incident Report: inc_xxx` + Markdown 正文 | SRE 邮件列表 |
| 每日摘要 | `Daily Incident Summary` + 当天所有 incident 表格 | SRE 邮件列表 |

**实现方案**：

| 任务 | 细节 |
| --- | --- |
| SMTP 配置 | 扩展 `packages/common/settings.py`：`SMTP_HOST`、`SMTP_PORT`、`SMTP_USER`、`SMTP_PASSWORD`、`SMTP_FROM`、`SRE_EMAIL_LIST`、`WEB_BASE_URL` |
| 依赖与服务 | 在 `pyproject.toml` 增加 `aiosmtplib`、`jinja2`；为每日摘要增加 celery beat 服务和 schedule 配置 |
| 邮件服务 | 新建 `apps/api/services/email_service.py`，用 `aiosmtplib` 异步发送 |
| 邮件模板 | Jinja2 模板：`templates/email/incident_alert.html`、`diagnosis_complete.html`、`approval_request.html`、`daily_summary.html` |
| 邮件中的审批链接 | L2/L3 邮件包含直达审批页面的链接；前端需新增 `/approvals/:approval_id` 或支持 `/approvals?approval_id=apv_xxx` 自动打开对应审批，不能只链接到当前队列页 |
| 每日摘要 | Celery Beat 定时任务，每天早上 9:00 发送；时区由配置控制 |

**关键技术细节**：

- 诊断完成后异步发送邮件（不阻塞主流程）。
- 发送失败重试 3 次，最终失败记录到 `email_log` 表。
- 邮件正文包含关键信息的纯文本版本（应对邮件客户端不支持 HTML 的情况）。

**验收标准**：

- 诊断完成 → 30 秒内收到邮件。
- 邮件包含根因摘要 + 关键证据 + 可打开指定 approval / incident / report 的前端直达链接。
- SMTP 不可用时不影响诊断流程。
