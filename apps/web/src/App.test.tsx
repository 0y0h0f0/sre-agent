import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen, waitFor, within } from '@testing-library/react';
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
  params: { to_revision: 42 },
  reason: 'new release correlated with 5xx spike',
  rollback_plan: 'redeploy previous stable version',
  execution_result: null,
  created_at: '2026-06-01T00:04:00Z',
  updated_at: '2026-06-01T00:04:00Z'
};

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

test('renders incident rows, filters, and empty state', async () => {
  const fetchMock = mockFetch({
    'GET /api/incidents': () => jsonResponse({ items: [incidentListItem], total: 1, page: 1, page_size: 50 })
  });

  renderApp('/incidents');

  expect(await screen.findByText('checkout-api')).toBeInTheDocument();
  expect(screen.getByText('High5xxAfterDeploy')).toBeInTheDocument();

  await userEvent.type(screen.getByLabelText('服务'), 'checkout-api');
  await userEvent.selectOptions(screen.getByLabelText('状态'), 'open');
  await userEvent.click(screen.getByRole('button', { name: '筛选' }));

  await waitFor(() => {
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('service=checkout-api') && String(url).includes('status=open'))).toBe(true);
  });
});

test('renders the incidents error state', async () => {
  mockFetch({
    'GET /api/incidents': () => jsonResponse({ error: { code: 'SERVER_ERROR', message: 'boom', request_id: 'req-1', details: {} } }, 500)
  });

  renderApp('/incidents');

  expect(await screen.findByText('无法加载事件')).toBeInTheDocument();
  expect(screen.getByText('boom')).toBeInTheDocument();
  expect(screen.getByText('错误码 SERVER_ERROR')).toBeInTheDocument();
});

test('shows API key guidance when incident loading is unauthorized', async () => {
  mockFetch({
    'GET /api/incidents': () => jsonResponse({ error: { code: 'UNAUTHORIZED', message: 'unauthorized', request_id: 'req-auth', details: {} } }, 401)
  });

  renderApp('/incidents');

  expect(await screen.findByText('无法加载事件')).toBeInTheDocument();
  expect(screen.getByText('请在侧边栏身份认证面板中设置或生成 API 密钥。')).toBeInTheDocument();
  expect(screen.getByRole('region', { name: 'API 认证' })).toBeInTheDocument();
});

test('generates and saves an API key from the authentication panel', async () => {
  const fetchMock = mockFetch({
    'GET /api/incidents': () => jsonResponse({ items: [], total: 0, page: 1, page_size: 50 }),
    'POST /api/api-keys': () => jsonResponse({
      key_id: 'apik_1',
      description: '本地 Web 密钥',
      raw_key: 'web-raw-key',
      created_by: 'system',
      scopes: ['api_key:admin'],
      roles: ['operator'],
      expires_at: null,
      created_at: '2026-06-01T00:00:00Z'
    }, 201)
  });

  renderApp('/incidents');

  await screen.findByText('无事件');
  await userEvent.type(screen.getByLabelText('引导种子或管理员密钥'), 'bootstrap-secret');
  await userEvent.click(screen.getByRole('button', { name: '生成密钥' }));

  expect(await screen.findByText('已创建 apik_1 并保存至浏览器。')).toBeInTheDocument();
  expect(screen.getByText('web-raw-key')).toBeInTheDocument();
  expect(window.localStorage.getItem('sre_api_key')).toBe('web-raw-key');

  await waitFor(() => {
    const createCall = fetchMock.mock.calls.find(([url, init]) => String(url) === '/api/api-keys' && init?.method === 'POST');
    expect(createCall).toBeTruthy();
    expect((createCall?.[1]?.headers as Headers).get('Authorization')).toBe('Bearer bootstrap-secret');
    expect(JSON.parse(String(createCall?.[1]?.body))).toEqual({
      description: '本地 Web 密钥',
      expires_in_days: 90,
      scopes: ['api_key:admin'],
      roles: ['operator']
    });
  });
});

test('renders incident detail with diagnosis, actions, approvals, run link, and rerun button', async () => {
  const fetchMock = mockFetch({
    'GET /api/incidents/inc_1': () => jsonResponse(incidentDetail),
    'GET /api/incidents/inc_1/runs': () => jsonResponse([{ agent_run_id: 'run_1', incident_id: 'inc_1', status: 'waiting_approval', celery_task_id: 'task-1', created_at: '2026-06-01T00:00:00Z', updated_at: '2026-06-01T00:04:00Z' }]),
    'GET /api/incidents/inc_1/approvals': () => jsonResponse([approval]),
    'POST /api/incidents/inc_1/diagnose': (_url, init) => {
      expect(JSON.parse(String(init.body))).toEqual({ force: true, reason: 'manual rerun from UI' });
      return jsonResponse({ incident_id: 'inc_1', agent_run_id: 'run_2', celery_task_id: 'task-2', status: 'queued' }, 202);
    }
  });

  const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
  const user = userEvent.setup();

  renderApp('/incidents/inc_1');

  expect(await screen.findByText('New checkout release introduced downstream timeouts')).toBeInTheDocument();
  expect(screen.getByText('Timeout errors')).toBeInTheDocument();
  expect(screen.getAllByText('rollback_release').length).toBeGreaterThan(0);
  expect(screen.getByRole('link', { name: 'Agent 运行' })).toHaveAttribute('href', '/agent-runs/run_1');
  expect(screen.getByRole('link', { name: '报告' })).toHaveAttribute('href', '/incidents/inc_1/report');

  await user.click(screen.getByRole('button', { name: '重新诊断' }));

  expect(confirmSpy).toHaveBeenCalled();
  await waitFor(() => {
    expect(fetchMock.mock.calls.some(([url, init]) => String(url) === '/api/incidents/inc_1/diagnose' && init?.method === 'POST')).toBe(true);
  });
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

  expect((await screen.findAllByText('parse_alert')).length).toBeGreaterThan(0);
  expect(screen.getByText('MetricsTool')).toBeInTheDocument();
  expect(screen.getByText('缓存命中')).toBeInTheDocument();
  expect(screen.getByText('123')).toBeInTheDocument();
  expect(screen.getByText('运行进度')).toBeInTheDocument();
  expect(screen.getByText('1 / 1 个节点已完成')).toBeInTheDocument();
  expect(screen.getByText('信号泳道')).toBeInTheDocument();
  expect(screen.getByText('依赖关系图')).toBeInTheDocument();
  expect(screen.getByText('证据网络')).toBeInTheDocument();
});


test('shows dynamic run progress and live websocket node updates', async () => {
  class MockWebSocket {
    static instances: MockWebSocket[] = [];
    onopen: ((event: Event) => void) | null = null;
    onmessage: ((event: MessageEvent) => void) | null = null;
    onerror: ((event: Event) => void) | null = null;
    onclose: ((event: CloseEvent) => void) | null = null;
    url: string;

    constructor(url: string) {
      this.url = url;
      MockWebSocket.instances.push(this);
    }

    close() {}
    send() {}

    emit(data: unknown) {
      this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent);
    }
  }
  vi.stubGlobal('WebSocket', MockWebSocket);
  window.localStorage.setItem('sre_api_key', 'web-raw-key');

  mockFetch({
    'POST /api/ws/incidents/inc_1/ticket': () => jsonResponse({
      ticket: 'ws_ticket_123',
      expires_at: '2026-06-01T00:01:00Z'
    }),
    'GET /api/agent-runs/run_live': () => jsonResponse({
      agent_run_id: 'run_live',
      incident_id: 'inc_1',
      status: 'running',
      celery_task_id: 'task-live',
      error_code: null,
      error_message: null,
      state: {
        service: 'checkout-api',
        graph_node_order: ['parse_alert', 'collect_metrics', 'collect_logs'],
        hypotheses: [{ id: 'hyp_1', summary: 'Deploy regression', confidence: 0.82 }],
        evidence_ids: ['evd_1']
      },
      checkpoint_thread_id: 'run_live',
      checkpoint_ns: '',
      latest_checkpoint_id: null,
      nodes: [
        { name: 'parse_alert', status: 'succeeded', started_at: '2026-06-01T00:00:00Z', finished_at: '2026-06-01T00:00:01Z', duration_ms: 1000, input_summary: 'alert', output_summary: 'service=checkout-api', tool_calls: [] },
        { name: 'collect_metrics', status: 'running', started_at: '2026-06-01T00:00:02Z', finished_at: null, duration_ms: null, input_summary: 'query prometheus', output_summary: null, tool_calls: [] }
      ],
      tool_calls: [
        { tool_call_id: 'tool_live_1', node_name: 'collect_metrics', tool_name: 'MetricsTool', status: 'succeeded', input_summary: '5xx', output_summary: '5xx elevated', duration_ms: 30, cache_key: 'metrics', cache_hit: false, error_message: null, created_at: '2026-06-01T00:00:03Z' }
      ],
      created_at: '2026-06-01T00:00:00Z',
      updated_at: '2026-06-01T00:00:03Z'
    })
  });

  renderApp('/agent-runs/run_live');

  expect(await screen.findByText('1 / 3 个节点已完成')).toBeInTheDocument();
  expect(screen.getByText('collect_logs')).toBeInTheDocument();
  await waitFor(() => expect(MockWebSocket.instances).toHaveLength(1));
  expect(MockWebSocket.instances[0].url).toContain('/api/ws/incidents/inc_1');
  expect(MockWebSocket.instances[0].url).toContain('ticket=ws_ticket_123');
  expect(MockWebSocket.instances[0].url).not.toContain('token=');
  expect(MockWebSocket.instances[0].url).not.toContain('web-raw-key');

  act(() => {
    MockWebSocket.instances[0].onopen?.(new Event('open'));
    MockWebSocket.instances[0].emit({
      type: 'node_update',
      payload: {
        agent_run_id: 'run_live',
        node_name: 'collect_logs',
        status: 'running',
        output_summary: 'tailing checkout logs'
      },
      timestamp: '2026-06-01T00:00:04Z'
    });
  });

  expect(await screen.findByText('tailing checkout logs')).toBeInTheDocument();
  expect(screen.getByText('已连接')).toBeInTheDocument();
});


test('renders the approval notification control', async () => {
  mockFetch({
    'GET /api/approvals': () => jsonResponse({ items: [approval], total: 1, page: 1, page_size: 50 })
  });

  renderApp('/approvals');

  expect(await screen.findByRole('button', { name: /通知不可用|启用通知|通知已开启|通知已阻止/ })).toBeInTheDocument();
});


test('disables batch approval when an L3 approval is selected', async () => {
  mockFetch({
    'GET /api/approvals': () => jsonResponse({ items: [approval], total: 1, page: 1, page_size: 50 })
  });

  renderApp('/approvals');

  await userEvent.click(await screen.findByRole('checkbox'));

  expect(await screen.findByText('L3 需单独确认')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '批量批准' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '批量拒绝' })).not.toBeDisabled();
});


test('enables approval notifications and notifies for newly arrived approvals', async () => {
  const requestPermission = vi.fn(async () => {
    MockNotification.permission = 'granted';
    return 'granted' as NotificationPermission;
  });
  class MockNotification {
    static permission: NotificationPermission = 'default';
    static requestPermission = requestPermission;
    constructor(_title: string, _options?: NotificationOptions) {}
  }
  const showNotification = vi.fn(async () => undefined);
  const register = vi.fn(async () => undefined);
  const getRegistration = vi.fn(async () => ({ showNotification }));
  vi.stubGlobal('Notification', MockNotification);
  Object.defineProperty(navigator, 'serviceWorker', {
    configurable: true,
    value: { register, getRegistration }
  });

  let approvalFetches = 0;
  mockFetch({
    'GET /api/approvals': () => {
      approvalFetches += 1;
      const items = approvalFetches < 2 ? [approval] : [approval, { ...approval, approval_id: 'apv_2', action_id: 'act_2' }];
      return jsonResponse({ items, total: items.length, page: 1, page_size: 50 });
    }
  });

  renderApp('/approvals');

  await userEvent.click(await screen.findByRole('button', { name: '启用通知' }));
  await waitFor(() => expect(requestPermission).toHaveBeenCalled());
  expect(await screen.findByRole('button', { name: '通知已开启' })).toBeInTheDocument();
  expect(register).toHaveBeenCalledWith('/sw.js');

  await userEvent.click(screen.getByRole('button', { name: '刷新' }));
  await waitFor(() => expect(showNotification).toHaveBeenCalledWith(
    'L3 审批请求',
    expect.objectContaining({ tag: 'apv_2' })
  ));
});

test('opens a direct linked approval route in the review dialog', async () => {
  mockFetch({
    'GET /api/approvals': () => jsonResponse({ items: [approval], total: 1, page: 1, page_size: 50 }),
    'GET /api/approvals/apv_1': () => jsonResponse(approval),
    'GET /api/actions/act_1': () => jsonResponse(actionDetail)
  });

  renderApp('/approvals/apv_1');

  const dialog = await screen.findByRole('dialog', { name: '审核操作' });
  expect(within(dialog).getByText('apv_1')).toBeInTheDocument();
  expect(await within(dialog).findByText('checkout-api')).toBeInTheDocument();
  expect(within(dialog).getByRole('region', { name: '执行参数' })).toHaveTextContent('To Revision');
  expect(within(dialog).getByRole('region', { name: '执行参数' })).toHaveTextContent('42');
  expect(within(dialog).getByLabelText('确认操作类型')).toBeInTheDocument();
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

  await userEvent.click(await screen.findByRole('button', { name: '审核' }));
  const dialog = await screen.findByRole('dialog', { name: '审核操作' });
  expect(await within(dialog).findByText('checkout-api')).toBeInTheDocument();

  await userEvent.type(within(dialog).getByLabelText('审批人'), 'sre-oncall');
  await userEvent.type(within(dialog).getByLabelText('备注'), 'approved with confirmation');
  await userEvent.click(within(dialog).getByLabelText('已确认风险'));
  await userEvent.type(within(dialog).getByLabelText('确认操作类型'), 'rollback_release');
  await userEvent.type(within(dialog).getByLabelText('确认目标'), 'checkout-api');
  const approveButtons = within(dialog).getAllByRole('button', { name: '批准' });
  await userEvent.click(approveButtons[approveButtons.length - 1]);

  await waitFor(() => expect(approvePayload).toEqual(expect.objectContaining({ risk_ack: true, confirm_action_type: 'rollback_release', confirm_target: 'checkout-api' })));
});

test('reject flow requires a rejection reason', async () => {
  mockFetch({
    'GET /api/approvals': () => jsonResponse({ items: [{ ...approval, risk_level: 'L2' }], total: 1, page: 1, page_size: 50 }),
    'GET /api/actions/act_1': () => jsonResponse({ ...actionDetail, risk_level: 'L2' })
  });

  renderApp('/approvals');

  await userEvent.click(await screen.findByRole('button', { name: '审核' }));
  const dialog = await screen.findByRole('dialog', { name: '审核操作' });
  await userEvent.click(within(dialog).getAllByRole('button', { name: '拒绝' })[0]);
  await userEvent.type(within(dialog).getByLabelText('审批人'), 'sre-oncall');
  await userEvent.click(within(dialog).getAllByRole('button', { name: '拒绝' })[1]);

  expect(await within(dialog).findByText('请填写拒绝原因')).toBeInTheDocument();
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
  await userEvent.click(screen.getByRole('button', { name: '重新生成' }));
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

  expect(await screen.findByText('诊断等待中')).toBeInTheDocument();
  expect(screen.getByText('无证据')).toBeInTheDocument();
  expect(screen.getByText('无操作')).toBeInTheDocument();
  expect(screen.getByText('无审批')).toBeInTheDocument();
  expect(screen.getByText(/missing=暂无/)).toBeInTheDocument();
  expect(screen.getByText(/nested=/)).toBeInTheDocument();
});


test('handles incident detail feedback, comments, and audit trail interactions', async () => {
  let nfaCalled = false;
  let correctionPayload: Record<string, unknown> | null = null;
  let commentPayload: Record<string, unknown> | null = null;
  vi.stubGlobal('confirm', vi.fn(() => true));
  mockFetch({
    'GET /api/incidents/inc_1': () => jsonResponse(incidentDetail),
    'GET /api/incidents/inc_1/runs': () => jsonResponse([]),
    'GET /api/incidents/inc_1/approvals': () => jsonResponse([]),
    'GET /api/incidents/inc_1/correlated': () => jsonResponse([{ ...incidentListItem, correlation_type: 'same_service', similarity_score: 0.7 }]),
    'GET /api/incidents/inc_1/comments': () => jsonResponse({ items: [{ comment_id: 'cmt_1', incident_id: 'inc_1', author: 'alice', content: 'checking deploy logs', parent_comment_id: null, mentioned_users: ['bob'], created_at: '2026-06-01T00:06:00Z' }], total: 1 }),
    'POST /api/incidents/inc_1/comments': (_url, init) => {
      commentPayload = JSON.parse(String(init.body));
      return jsonResponse({ comment_id: 'cmt_2', incident_id: 'inc_1', author: 'bob', content: 'added note', parent_comment_id: null, mentioned_users: [], created_at: '2026-06-01T00:07:00Z' }, 201);
    },
    'GET /api/incidents/inc_1/audit': () => jsonResponse({ items: [{ audit_id: 'aud_1', incident_id: 'inc_1', actor: 'sre-system', action: 'root_cause_updated', resource_type: 'incident', resource_id: 'inc_1', details: {}, created_at: '2026-06-01T00:08:00Z' }], total: 1 }),
    'POST /api/incidents/inc_1/nfa': () => {
      nfaCalled = true;
      return jsonResponse({ pattern_id: 'mem_1', fingerprint: 'fp-checkout-5xx', nfa_count: 1, status: 'recorded', message: 'NFA recorded' });
    },
    'PATCH /api/incidents/inc_1/root-cause': (_url, init) => {
      correctionPayload = JSON.parse(String(init.body));
      return jsonResponse({ feedback_id: 'fb_1', incident_id: 'inc_1', feedback_type: 'root_cause', original_value: null, corrected_value: correctionPayload, delta: null, submitted_by: 'sre', submitted_at: '2026-06-01T00:09:00Z' });
    }
  });

  renderApp('/incidents/inc_1');

  expect(await screen.findByText('相关事件')).toBeInTheDocument();
  expect(await screen.findByText('alice')).toBeInTheDocument();
  expect(await screen.findByText('sre-system')).toBeInTheDocument();

  await userEvent.click(screen.getByRole('button', { name: '修正根因' }));
  const rootCauseBox = screen.getByDisplayValue('New checkout release introduced downstream timeouts');
  await userEvent.clear(rootCauseBox);
  await userEvent.type(rootCauseBox, 'Corrected downstream timeout root cause');
  await userEvent.click(screen.getByRole('button', { name: '保存修正' }));
  await waitFor(() => expect(correctionPayload).toEqual({ corrected_summary: 'Corrected downstream timeout root cause' }));

  await userEvent.click(screen.getByRole('button', { name: '标记无效' }));
  await waitFor(() => expect(nfaCalled).toBe(true));

  await userEvent.type(screen.getByLabelText('名称'), 'bob');
  await userEvent.type(screen.getByLabelText('评论'), 'added note');
  await userEvent.click(screen.getByRole('button', { name: '发表评论' }));
  await waitFor(() => expect(commentPayload).toEqual({ author: 'bob', content: 'added note' }));
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
  expect(screen.getByText('无节点记录')).toBeInTheDocument();
  expect(screen.getByText('无工具调用')).toBeInTheDocument();
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

  await userEvent.click(await screen.findByRole('button', { name: '审核' }));
  const dialog = await screen.findByRole('dialog', { name: '审核操作' });
  await userEvent.click(within(dialog).getAllByRole('button', { name: '拒绝' })[0]);
  await userEvent.type(within(dialog).getByLabelText('审批人'), 'sre-oncall');
  await userEvent.type(within(dialog).getByLabelText('拒绝原因'), 'too risky');
  await userEvent.click(within(dialog).getAllByRole('button', { name: '拒绝' })[1]);

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

  expect(await screen.findByText('无可用报告')).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: '生成' }));
  await waitFor(() => expect(generated).toBe(true));
});

test('renders the not found route', () => {
  renderApp('/missing-route');

  expect(screen.getByText('页面未找到')).toBeInTheDocument();
  expect(screen.getByText('未知路由')).toBeInTheDocument();
});
