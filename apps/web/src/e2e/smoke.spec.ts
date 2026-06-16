import { expect, test } from '@playwright/test';

test('reviews an incident and approval in the console', async ({ page }) => {
  let alertCreated = false;
  await page.route('/api/alerts', async (route) => {
    alertCreated = true;
    await route.fulfill({
      status: 201,
      json: { incident_id: 'inc_1', agent_run_id: 'run_1', deduplicated: false }
    });
  });
  await page.route('/api/incidents?**', async (route) => {
    await route.fulfill({
      json: {
        items: [
          {
            incident_id: 'inc_1',
            service: 'checkout-api',
            severity: 'P2',
            status: 'waiting_approval',
            alert_name: 'High5xxAfterDeploy',
            root_cause_summary: 'New release caused downstream timeouts',
            created_at: '2026-06-01T00:00:00Z',
            updated_at: '2026-06-01T00:05:00Z'
          }
        ],
        total: 1,
        page: 1,
        page_size: 50
      }
    });
  });
  await page.route('/api/incidents/inc_1', async (route) => {
    await route.fulfill({
      json: {
        incident_id: 'inc_1',
        service: 'checkout-api',
        severity: 'P2',
        status: 'waiting_approval',
        alert: { fingerprint: 'fp-1', source: 'mock', starts_at: '2026-06-01T00:00:00Z', labels: {}, annotations: {} },
        root_cause: { summary: 'New release caused downstream timeouts', confidence: 0.9, evidence_ids: ['evd_1'] },
        evidence: [{ evidence_id: 'evd_1', type: 'logs', source: 'loki', title: 'Timeout errors', excerpt: 'payment-api timeout', confidence: 0.9, timestamp: '2026-06-01T00:03:00Z' }],
        recommended_actions: [{ action_id: 'act_1', type: 'rollback_release', risk_level: 'L3', status: 'waiting_approval', reason: 'release correlated with errors', rollback_plan: 'redeploy previous version' }]
      }
    });
  });
  await page.route('/api/incidents/inc_1/runs', async (route) => {
    await route.fulfill({ json: [{ agent_run_id: 'run_1', incident_id: 'inc_1', status: 'waiting_approval', celery_task_id: 'task-1', created_at: '2026-06-01T00:00:00Z', updated_at: '2026-06-01T00:04:00Z' }] });
  });
  await page.route('/api/incidents/inc_1/approvals', async (route) => {
    await route.fulfill({ json: [{ approval_id: 'apv_1', action_id: 'act_1', incident_id: 'inc_1', agent_run_id: 'run_1', service: 'checkout-api', action_type: 'rollback_release', risk_level: 'L3', approval_status: 'waiting', action_status: 'waiting_approval', reason: 'rollback needs confirmation', rollback_plan: 'redeploy previous version', requested_at: '2026-06-01T00:04:00Z', decided_at: null, approver: null, comment: null }] });
  });
  await page.route('/api/incidents/inc_1/correlated', async (route) => {
    await route.fulfill({ json: [] });
  });
  await page.route('/api/incidents/inc_1/comments', async (route) => {
    await route.fulfill({ json: { items: [], total: 0 } });
  });
  await page.route('/api/incidents/inc_1/audit', async (route) => {
    await route.fulfill({ json: { items: [], total: 0 } });
  });
  await page.route('/api/approvals?**', async (route) => {
    await route.fulfill({ json: { items: [{ approval_id: 'apv_1', action_id: 'act_1', incident_id: 'inc_1', agent_run_id: 'run_1', service: 'checkout-api', action_type: 'rollback_release', risk_level: 'L3', approval_status: 'waiting', action_status: 'waiting_approval', reason: 'rollback needs confirmation', rollback_plan: 'redeploy previous version', requested_at: '2026-06-01T00:04:00Z', decided_at: null, approver: null, comment: null }], total: 1, page: 1, page_size: 50 } });
  });
  await page.route('/api/actions/act_1', async (route) => {
    await route.fulfill({ json: { action_id: 'act_1', incident_id: 'inc_1', agent_run_id: 'run_1', type: 'rollback_release', risk_level: 'L3', status: 'waiting_approval', executor: 'mock', target: 'checkout-api', params: {}, reason: 'release correlated with errors', rollback_plan: 'redeploy previous version', execution_result: null, created_at: '2026-06-01T00:04:00Z', updated_at: '2026-06-01T00:04:00Z' } });
  });
  await page.route('/api/approvals/apv_1/approve', async (route) => {
    await route.fulfill({ json: { approval_id: 'apv_1', action_id: 'act_1', status: 'approved', agent_run_id: 'run_1' } });
  });
  await page.route('/api/incidents/inc_1/report', async (route) => {
    await route.fulfill({
      json: {
        report_id: 'rpt_1',
        incident_id: 'inc_1',
        agent_run_id: 'run_1',
        version: 1,
        root_cause: 'New release caused downstream timeouts',
        impact: 'Checkout failures affected a subset of requests',
        timeline: [{ time: '2026-06-01T00:00:00Z', event: 'Alert fired' }],
        actions: [{ type: 'rollback_release', status: 'waiting_approval' }],
        follow_ups: ['Add canary checks'],
        evidence_ids: ['evd_1'],
        body_markdown: '# report',
        created_at: '2026-06-01T00:08:00Z'
      }
    });
  });

  await page.goto('/');
  await page.evaluate(async () => {
    await fetch('/api/alerts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source: 'mock',
        fingerprint: 'fp-1',
        service: 'checkout-api',
        severity: 'P2',
        alert_name: 'High5xxAfterDeploy',
        starts_at: '2026-06-01T00:00:00Z'
      })
    });
  });
  expect(alertCreated).toBe(true);
  await expect(page.getByRole('heading', { name: 'SRE 事件控制台' })).toBeVisible();
  await expect(page.getByText('High5xxAfterDeploy')).toBeVisible();

  await page.getByText('High5xxAfterDeploy').click();
  await expect(page.getByText('New release caused downstream timeouts')).toBeVisible();

  await page.getByRole('link', { name: '报告' }).click();
  await expect(page.getByText('Checkout failures affected a subset of requests')).toBeVisible();
  await expect(page.getByText('evd_1')).toBeVisible();

  await page.getByRole('link', { name: '审批' }).click();
  await page.getByRole('button', { name: '审核' }).click();
  await page.getByLabel('审批人').fill('sre-oncall');
  await page.getByLabel('备注').fill('approved');
  await page.getByLabel('已确认风险').check();
  await page.getByLabel('确认操作类型').fill('rollback_release');
  await page.getByLabel('确认目标').fill('checkout-api');
  await page.getByRole('dialog').getByRole('button', { name: '批准' }).last().click();
  await expect(page.getByRole('dialog')).toBeHidden();
});
