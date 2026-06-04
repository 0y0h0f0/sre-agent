import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';

import App from './App';

type RouteHandler = (url: URL, init: RequestInit) => Response | Promise<Response>;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

function mockFetch(routes: Record<string, RouteHandler>) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init = {}) => {
    const url = new URL(String(input), 'http://localhost');
    const method = init.method ?? 'GET';
    const handler = routes[`${method} ${url.pathname}`];
    if (!handler) {
      return jsonResponse({ error: { code: 'NOT_FOUND', message: `No route for ${method} ${url.pathname}`, request_id: 'req-test', details: {} } }, 404);
    }
    return handler(url, init);
  });
}

function renderApp(path = '/incidents') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

const incidentListItem = {
  incident_id: 'inc_1',
  service: 'checkout-api',
  severity: 'P2',
  status: 'open',
  alert_name: 'High5xxAfterDeploy',
  root_cause_summary: null,
  created_at: '2026-06-01T00:00:00Z',
  updated_at: '2026-06-01T00:05:00Z'
};

const incidentDetail = {
  incident_id: 'inc_1',
  service: 'checkout-api',
  severity: 'P2',
  status: 'waiting_approval',
  alert: {
    fingerprint: 'fp-checkout-5xx',
    source: 'mock',
    starts_at: '2026-06-01T00:00:00Z',
    labels: { team: 'payments' },
    annotations: { summary: '5xx increased after deploy' }
  },
  root_cause: { summary: 'New checkout release introduced downstream timeouts', confidence: 0.91, evidence_ids: ['evd_1'] },
  evidence: [
    {
      evidence_id: 'evd_1',
      type: 'logs',
      source: 'loki',
      title: 'Timeout errors',
      excerpt: 'payment-api timeout after deploy',
      confidence: 0.9,
      timestamp: '2026-06-01T00:03:00Z'
    }
  ],
  recommended_actions: [
    {
      action_id: 'act_1',
      type: 'rollback_release',
      risk_level: 'L3',
      status: 'waiting_approval',
      reason: 'new release correlated with 5xx spike',
      rollback_plan: 'redeploy previous stable version'
    }
  ]
};

const approval = {
  approval_id: 'apv_1',
  action_id: 'act_1',
  incident_id: 'inc_1',
  agent_run_id: 'run_1',
  service: 'checkout-api',
  action_type: 'rollback_release',
  risk_level: 'L3',
  approval_status: 'waiting',
  action_status: 'waiting_approval',
  reason: 'rollback needs confirmation',
  rollback_plan: 'redeploy previous stable version',
  requested_at: '2026-06-01T00:04:00Z',
  decided_at: null,
  approver: null,
  comment: null
};

const actionDetail = {
  action_id: 'act_1',
  incident_id: 'inc_1',
  agent_run_id: 'run_1',
  type: 'rollback_release',
  risk_level: 'L3',
  status: 'waiting_approval',
  executor: 'mock',
  target: 'checkout-api',
  params: {},
  reason: 'new release correlated with 5xx spike',
  rollback_plan: 'redeploy previous stable version',
  execution_result: null,
  created_at: '2026-06-01T00:04:00Z',
  updated_at: '2026-06-01T00:04:00Z'
};

afterEach(() => {
  vi.restoreAllMocks();
});

test('renders incident rows, filters, and empty state', async () => {
  const fetchMock = mockFetch({
    'GET /api/incidents': () => jsonResponse({ items: [incidentListItem], total: 1, page: 1, page_size: 50 })
  });

  renderApp('/incidents');

  expect(await screen.findByText('checkout-api')).toBeInTheDocument();
  expect(screen.getByText('High5xxAfterDeploy')).toBeInTheDocument();

  await userEvent.type(screen.getByLabelText('Service'), 'checkout-api');
  await userEvent.selectOptions(screen.getByLabelText('Status'), 'open');
  await userEvent.click(screen.getByRole('button', { name: 'Filter' }));

  await waitFor(() => {
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('service=checkout-api') && String(url).includes('status=open'))).toBe(true);
  });
});

test('renders the incidents error state', async () => {
  mockFetch({
    'GET /api/incidents': () => jsonResponse({ error: { code: 'SERVER_ERROR', message: 'boom', request_id: 'req-1', details: {} } }, 500)
  });

  renderApp('/incidents');

  expect(await screen.findByText('Unable to load incidents')).toBeInTheDocument();
  expect(screen.getByText('boom')).toBeInTheDocument();
  expect(screen.getByText('Code SERVER_ERROR')).toBeInTheDocument();
});

test('renders incident detail with diagnosis, actions, approvals, and run link', async () => {
  mockFetch({
    'GET /api/incidents/inc_1': () => jsonResponse(incidentDetail),
    'GET /api/incidents/inc_1/runs': () => jsonResponse([{ agent_run_id: 'run_1', incident_id: 'inc_1', status: 'waiting_approval', celery_task_id: 'task-1', created_at: '2026-06-01T00:00:00Z', updated_at: '2026-06-01T00:04:00Z' }]),
    'GET /api/incidents/inc_1/approvals': () => jsonResponse([approval])
  });

  renderApp('/incidents/inc_1');

  expect(await screen.findByText('New checkout release introduced downstream timeouts')).toBeInTheDocument();
  expect(screen.getByText('Timeout errors')).toBeInTheDocument();
  expect(screen.getAllByText('rollback_release').length).toBeGreaterThan(0);
  expect(screen.getByRole('link', { name: 'Agent run' })).toHaveAttribute('href', '/agent-runs/run_1');
  expect(screen.getByRole('link', { name: 'Report' })).toHaveAttribute('href', '/incidents/inc_1/report');
});

test('renders agent run timeline, tool calls, and context summary', async () => {
  mockFetch({
    'GET /api/agent-runs/run_1': () => jsonResponse({
      agent_run_id: 'run_1',
      incident_id: 'inc_1',
      status: 'succeeded',
      celery_task_id: 'task-1',
      error_code: null,
      error_message: null,
      state: { token_usage: { prompt_tokens: 123, completion_tokens: 45 }, compression_events: [{ reason: 'budget', saved_tokens: 200 }] },
      checkpoint_thread_id: 'run_1',
      checkpoint_ns: '',
      latest_checkpoint_id: 'chk_1',
      nodes: [
        { name: 'parse_alert', status: 'succeeded', started_at: '2026-06-01T00:00:00Z', finished_at: '2026-06-01T00:00:01Z', duration_ms: 1000, input_summary: 'alert', output_summary: 'service=checkout-api', tool_calls: [] }
      ],
      tool_calls: [
        { tool_call_id: 'tool_1', node_name: 'collect_metrics', tool_name: 'MetricsTool', status: 'succeeded', input_summary: 'error_rate', output_summary: '5xx elevated', duration_ms: 42, cache_key: 'metrics', cache_hit: true, error_message: null, created_at: '2026-06-01T00:00:02Z' }
      ],
      created_at: '2026-06-01T00:00:00Z',
      updated_at: '2026-06-01T00:00:02Z'
    })
  });

  renderApp('/agent-runs/run_1');

  expect(await screen.findByText('parse_alert')).toBeInTheDocument();
  expect(screen.getByText('MetricsTool')).toBeInTheDocument();
  expect(screen.getByText('cache hit')).toBeInTheDocument();
  expect(screen.getByText('123')).toBeInTheDocument();
});


test('opens a direct linked approval route in the review dialog', async () => {
  mockFetch({
    'GET /api/approvals': () => jsonResponse({ items: [approval], total: 1, page: 1, page_size: 50 }),
    'GET /api/approvals/apv_1': () => jsonResponse(approval),
    'GET /api/actions/act_1': () => jsonResponse(actionDetail)
  });

  renderApp('/approvals/apv_1');

  const dialog = await screen.findByRole('dialog', { name: 'Review action' });
  expect(within(dialog).getByText('apv_1')).toBeInTheDocument();
  expect(await within(dialog).findByText('checkout-api')).toBeInTheDocument();
  expect(within(dialog).getByLabelText('Confirm action type')).toBeInTheDocument();
});

test('approves an L3 approval with secondary confirmation', async () => {
  let approvePayload: Record<string, unknown> | null = null;
  mockFetch({
    'GET /api/approvals': () => jsonResponse({ items: [approval], total: 1, page: 1, page_size: 50 }),
    'GET /api/actions/act_1': () => jsonResponse(actionDetail),
    'POST /api/approvals/apv_1/approve': (_url, init) => {
      approvePayload = JSON.parse(String(init.body));
      return jsonResponse({ approval_id: 'apv_1', action_id: 'act_1', status: 'approved', agent_run_id: 'run_1' });
    }
  });

  renderApp('/approvals');

  await userEvent.click(await screen.findByRole('button', { name: 'Review' }));
  const dialog = await screen.findByRole('dialog', { name: 'Review action' });
  expect(await within(dialog).findByText('checkout-api')).toBeInTheDocument();

  await userEvent.type(within(dialog).getByLabelText('Approver'), 'sre-oncall');
  await userEvent.type(within(dialog).getByLabelText('Comment'), 'approved with confirmation');
  await userEvent.click(within(dialog).getByLabelText('Risk acknowledged'));
  await userEvent.type(within(dialog).getByLabelText('Confirm action type'), 'rollback_release');
  await userEvent.type(within(dialog).getByLabelText('Confirm target'), 'checkout-api');
  const approveButtons = within(dialog).getAllByRole('button', { name: 'Approve' });
  await userEvent.click(approveButtons[approveButtons.length - 1]);

  await waitFor(() => expect(approvePayload).toEqual(expect.objectContaining({ risk_ack: true, confirm_action_type: 'rollback_release', confirm_target: 'checkout-api' })));
});

test('reject flow requires a rejection reason', async () => {
  mockFetch({
    'GET /api/approvals': () => jsonResponse({ items: [{ ...approval, risk_level: 'L2' }], total: 1, page: 1, page_size: 50 }),
    'GET /api/actions/act_1': () => jsonResponse({ ...actionDetail, risk_level: 'L2' })
  });

  renderApp('/approvals');

  await userEvent.click(await screen.findByRole('button', { name: 'Review' }));
  const dialog = await screen.findByRole('dialog', { name: 'Review action' });
  await userEvent.click(within(dialog).getAllByRole('button', { name: 'Reject' })[0]);
  await userEvent.type(within(dialog).getByLabelText('Approver'), 'sre-oncall');
  await userEvent.click(within(dialog).getAllByRole('button', { name: 'Reject' })[1]);

  expect(await within(dialog).findByText('Rejection reason is required')).toBeInTheDocument();
});

test('renders report and regenerates it', async () => {
  let regenerated = false;
  mockFetch({
    'GET /api/incidents/inc_1/report': () => jsonResponse({
      report_id: 'rpt_1',
      incident_id: 'inc_1',
      agent_run_id: 'run_1',
      version: 1,
      root_cause: 'Bad release caused elevated 5xx',
      impact: 'Checkout requests failed for a subset of users',
      timeline: [{ time: '2026-06-01T00:00:00Z', event: 'Alert fired' }],
      actions: [{ type: 'rollback_release', status: 'approved' }],
      follow_ups: [{ item: 'Add deploy canary checks', status: 'open' }],
      evidence_ids: ['evd_1'],
      body_markdown: '# report',
      created_at: '2026-06-01T00:10:00Z'
    }),
    'POST /api/incidents/inc_1/report/regenerate': () => {
      regenerated = true;
      return jsonResponse({
        report_id: 'rpt_2',
        incident_id: 'inc_1',
        agent_run_id: 'run_1',
        version: 2,
        root_cause: 'Bad release caused elevated 5xx',
        impact: 'Checkout requests failed for a subset of users',
        timeline: [],
        actions: [],
        follow_ups: [],
        evidence_ids: ['evd_1'],
        body_markdown: '# report',
        created_at: '2026-06-01T00:12:00Z'
      }, 201);
    }
  });

  renderApp('/incidents/inc_1/report');

  expect(await screen.findByText('Bad release caused elevated 5xx')).toBeInTheDocument();
  expect(screen.getByText('evd_1')).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'Regenerate' }));
  await waitFor(() => expect(regenerated).toBe(true));
});


test('renders incident detail empty states and compact alert values', async () => {
  mockFetch({
    'GET /api/incidents/inc_empty': () => jsonResponse({
      ...incidentDetail,
      incident_id: 'inc_empty',
      status: 'open',
      alert: { fingerprint: null, source: 'mock', starts_at: 'bad-date', labels: { missing: null, nested: { zone: 'a' }, healthy: true }, annotations: undefined },
      root_cause: null,
      evidence: [],
      recommended_actions: []
    }),
    'GET /api/incidents/inc_empty/runs': () => jsonResponse([]),
    'GET /api/incidents/inc_empty/approvals': () => jsonResponse([])
  });

  renderApp('/incidents/inc_empty');

  expect(await screen.findByText('Diagnosis pending')).toBeInTheDocument();
  expect(screen.getByText('No evidence')).toBeInTheDocument();
  expect(screen.getByText('No actions')).toBeInTheDocument();
  expect(screen.getByText('No approvals')).toBeInTheDocument();
  expect(screen.getByText(/missing=n\/a/)).toBeInTheDocument();
  expect(screen.getByText(/nested=/)).toBeInTheDocument();
});

test('renders failed agent run empty states', async () => {
  mockFetch({
    'GET /api/agent-runs/run_failed': () => jsonResponse({
      agent_run_id: 'run_failed',
      incident_id: 'inc_1',
      status: 'failed',
      celery_task_id: null,
      error_code: 'NODE_FAILED',
      error_message: 'metrics unavailable',
      state: { context_budget: { max_tokens: 4000 } },
      checkpoint_thread_id: null,
      checkpoint_ns: '',
      latest_checkpoint_id: null,
      nodes: [],
      tool_calls: [],
      created_at: '2026-06-01T00:00:00Z',
      updated_at: '2026-06-01T00:00:02Z'
    })
  });

  renderApp('/agent-runs/run_failed');

  expect(await screen.findByText('NODE_FAILED: metrics unavailable')).toBeInTheDocument();
  expect(screen.getByText('No nodes recorded')).toBeInTheDocument();
  expect(screen.getByText('No tool calls')).toBeInTheDocument();
  expect(screen.getByText('4000')).toBeInTheDocument();
});

test('rejects an approval with a reason', async () => {
  let rejectPayload: Record<string, unknown> | null = null;
  mockFetch({
    'GET /api/approvals': () => jsonResponse({ items: [{ ...approval, risk_level: 'L2' }], total: 1, page: 1, page_size: 50 }),
    'GET /api/actions/act_1': () => jsonResponse({ ...actionDetail, risk_level: 'L2' }),
    'POST /api/approvals/apv_1/reject': (_url, init) => {
      rejectPayload = JSON.parse(String(init.body));
      return jsonResponse({ approval_id: 'apv_1', action_id: 'act_1', status: 'rejected', agent_run_id: 'run_1' });
    }
  });

  renderApp('/approvals');

  await userEvent.click(await screen.findByRole('button', { name: 'Review' }));
  const dialog = await screen.findByRole('dialog', { name: 'Review action' });
  await userEvent.click(within(dialog).getAllByRole('button', { name: 'Reject' })[0]);
  await userEvent.type(within(dialog).getByLabelText('Approver'), 'sre-oncall');
  await userEvent.type(within(dialog).getByLabelText('Rejection reason'), 'too risky');
  await userEvent.click(within(dialog).getAllByRole('button', { name: 'Reject' })[1]);

  await waitFor(() => expect(rejectPayload).toEqual(expect.objectContaining({ approver: 'sre-oncall', comment: 'too risky' })));
});

test('renders missing report empty state and generates a report', async () => {
  let generated = false;
  mockFetch({
    'GET /api/incidents/inc_empty/report': () => jsonResponse({ error: { code: 'NOT_FOUND', message: 'report not found', request_id: 'req-report', details: {} } }, 404),
    'POST /api/incidents/inc_empty/report/regenerate': () => {
      generated = true;
      return jsonResponse({
        report_id: 'rpt_new',
        incident_id: 'inc_empty',
        agent_run_id: 'run_1',
        version: 1,
        root_cause: 'Generated root cause',
        impact: 'Generated impact',
        timeline: [],
        actions: [],
        follow_ups: [],
        evidence_ids: [],
        body_markdown: '# report',
        created_at: '2026-06-01T00:12:00Z'
      }, 201);
    }
  });

  renderApp('/incidents/inc_empty/report');

  expect(await screen.findByText('No report available')).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'Generate' }));
  await waitFor(() => expect(generated).toBe(true));
});

test('renders the not found route', () => {
  renderApp('/missing-route');

  expect(screen.getByText('Page not found')).toBeInTheDocument();
  expect(screen.getByText('Unknown route')).toBeInTheDocument();
});
