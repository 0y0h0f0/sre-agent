import { afterEach, expect, test, vi } from 'vitest';

import {
  ApiError,
  approveApproval,
  clearStoredApiKey,
  batchDecideApprovals,
  correctIncidentAction,
  correctIncidentRootCause,
  createApiKey,
  createComment,
  createEvidenceAnnotation,
  deleteComment,
  getAgentRun,
  getApproval,
  getCorrelatedIncidents,
  getStoredApiKey,
  getIncident,
  getIncidentReport,
  listApprovals,
  listEvidenceAnnotations,
  listIncidentApprovals,
  listIncidentAudit,
  listIncidentComments,
  listIncidentFeedback,
  listIncidentRuns,
  listIncidents,
  markIncidentNFA,
  regenerateIncidentReport,
  rejectApproval,
  setStoredApiKey,
  triggerDiagnosis
} from './api';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json', 'X-Request-Id': 'req-test' } });
}

afterEach(() => {
  window.localStorage.removeItem('sre_api_key');
  vi.restoreAllMocks();
});

test('api client sends stored API key as bearer token', async () => {
  window.localStorage.setItem('sre_api_key', 'demo-secret');
  const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    jsonResponse({ items: [], total: 0, page: 1, page_size: 20 })
  );

  await expect(listIncidents()).resolves.toEqual({ items: [], total: 0, page: 1, page_size: 20 });

  const [, init] = fetchMock.mock.calls[0];
  expect((init?.headers as Headers).get('Authorization')).toBe('Bearer demo-secret');
});

test('api key storage helpers trim and clear keys', () => {
  setStoredApiKey('  raw-secret  ');
  expect(getStoredApiKey()).toBe('raw-secret');

  clearStoredApiKey();
  expect(getStoredApiKey()).toBeNull();
});

test('createApiKey posts with the bootstrap token instead of the stored key', async () => {
  window.localStorage.setItem('sre_api_key', 'old-browser-key');
  const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    jsonResponse({
      key_id: 'apik_1',
      description: 'local web key',
      raw_key: 'new-browser-key',
      created_by: 'system',
      scopes: [],
      roles: [],
      expires_at: null,
      created_at: '2026-06-01T00:00:00Z'
    }, 201)
  );

  await expect(createApiKey({ description: 'local web key', expires_in_days: 90 }, 'bootstrap-secret')).resolves.toMatchObject({
    raw_key: 'new-browser-key'
  });

  const [url, init] = fetchMock.mock.calls[0];
  expect(String(url)).toBe('/api/api-keys');
  expect(init?.method).toBe('POST');
  expect((init?.headers as Headers).get('Authorization')).toBe('Bearer bootstrap-secret');
  expect(JSON.parse(String(init?.body))).toEqual({ description: 'local web key', expires_in_days: 90 });
});

test('listIncidents returns paginated incidents and sends a request id', async () => {
  const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    jsonResponse({
      items: [
        {
          incident_id: 'inc_1',
          service: 'checkout-api',
          severity: 'P2',
          status: 'open',
          alert_name: 'High5xxAfterDeploy',
          root_cause_summary: null,
          created_at: '2026-06-01T00:00:00Z',
          updated_at: '2026-06-01T00:00:00Z'
        }
      ],
      total: 1,
      page: 1,
      page_size: 20
    })
  );

  await expect(listIncidents({ service: 'checkout-api' })).resolves.toEqual(
    expect.objectContaining({ total: 1, items: [expect.objectContaining({ incident_id: 'inc_1' })] })
  );
  const [url, init] = fetchMock.mock.calls[0];
  expect(String(url)).toContain('service=checkout-api');
  expect((init?.headers as Headers).get('X-Request-Id')).toMatch(/^req_/);
});

test('listIncidents normalizes legacy array responses', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    jsonResponse([
      {
        incident_id: 'inc_legacy',
        service: 'checkout-api',
        severity: 'P3',
        status: 'resolved',
        alert_name: 'Recovered',
        root_cause_summary: 'cache recovered',
        created_at: '2026-06-01T00:00:00Z',
        updated_at: '2026-06-01T00:00:00Z'
      }
    ])
  );

  await expect(listIncidents()).resolves.toEqual(
    expect.objectContaining({ total: 1, items: [expect.objectContaining({ incident_id: 'inc_legacy' })] })
  );
});

test('api client surfaces standard error envelopes', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    jsonResponse({ error: { code: 'NOT_FOUND', message: 'incident not found', request_id: 'req-404', details: { id: 'inc_missing' } } }, 404)
  );

  await expect(getIncidentReport('inc_missing')).rejects.toMatchObject({
    name: 'ApiError',
    code: 'NOT_FOUND',
    requestId: 'req-404',
    status: 404
  } satisfies Partial<ApiError>);
});


test('getApproval calls the direct approval endpoint', async () => {
  const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    jsonResponse({
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
      rollback_plan: null,
      requested_at: '2026-06-01T00:04:00Z',
      decided_at: null,
      approver: null,
      comment: null
    })
  );

  await expect(getApproval('apv_1')).resolves.toMatchObject({ approval_id: 'apv_1' });
  expect(String(fetchMock.mock.calls[0][0])).toBe('/api/approvals/apv_1');
});

test('approveApproval posts L3 confirmation fields', async () => {
  const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    jsonResponse({ approval_id: 'apv_1', action_id: 'act_1', status: 'approved', agent_run_id: 'run_1' })
  );

  await approveApproval('apv_1', {
    approver: 'sre-oncall',
    comment: 'approved',
    risk_ack: true,
    confirm_action_type: 'rollback_release',
    confirm_target: 'checkout-api'
  });

  const [url, init] = fetchMock.mock.calls[0];
  expect(String(url)).toBe('/api/approvals/apv_1/approve');
  expect(init?.method).toBe('POST');
  expect(JSON.parse(String(init?.body))).toEqual(expect.objectContaining({ risk_ack: true, confirm_target: 'checkout-api' }));
});


test('api client handles empty and non-json responses', async () => {
  const fetchMock = vi.spyOn(globalThis, 'fetch')
    .mockResolvedValueOnce(new Response('', { status: 200 }))
    .mockResolvedValueOnce(new Response('not-json', { status: 500, headers: { 'X-Request-Id': 'req-plain' } }));

  await expect(listApprovals()).resolves.toEqual({ items: [], total: 0, page: 1, page_size: 20 });
  await expect(getIncident('inc_1')).rejects.toMatchObject({ message: 'Request failed with status 500', requestId: 'req-plain' });
  expect(fetchMock).toHaveBeenCalledTimes(2);
});

test('api helpers call expected endpoints', async () => {
  const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init = {}) => {
    const url = new URL(String(input), 'http://localhost');
    if (url.pathname.endsWith('/diagnose')) {
      return jsonResponse({ incident_id: 'inc_1', agent_run_id: 'run_2', celery_task_id: 'task-2', status: 'queued' }, 202);
    }
    if (url.pathname.endsWith('/runs')) {
      return jsonResponse([{ agent_run_id: 'run_1', incident_id: 'inc_1', status: 'succeeded', celery_task_id: null, created_at: '2026-06-01T00:00:00Z', updated_at: '2026-06-01T00:00:00Z' }]);
    }
    if (url.pathname === '/api/agent-runs/run_1') {
      return jsonResponse({ agent_run_id: 'run_1', incident_id: 'inc_1', status: 'succeeded', celery_task_id: null, error_code: null, error_message: null, state: {}, checkpoint_thread_id: 'run_1', checkpoint_ns: '', latest_checkpoint_id: null, nodes: [], tool_calls: [], created_at: '2026-06-01T00:00:00Z', updated_at: '2026-06-01T00:00:00Z' });
    }
    if (url.pathname.endsWith('/approvals')) {
      return jsonResponse([]);
    }
    if (url.pathname.endsWith('/reject')) {
      return jsonResponse({ approval_id: 'apv_1', action_id: 'act_1', status: 'rejected', agent_run_id: 'run_1' });
    }
    if (url.pathname.endsWith('/report/regenerate')) {
      return jsonResponse({ report_id: 'rpt_1', incident_id: 'inc_1', agent_run_id: 'run_1', version: 1, root_cause: 'rc', impact: 'impact', timeline: [], actions: [], follow_ups: [], evidence_ids: [], body_markdown: '', created_at: '2026-06-01T00:00:00Z' }, 201);
    }
    return jsonResponse({ incident_id: 'inc_1', service: 'checkout-api', severity: 'P2', status: 'open', alert: {}, root_cause: null, evidence: [], recommended_actions: [] });
  });

  await expect(triggerDiagnosis('inc_1', { force: true, reason: 'manual' })).resolves.toMatchObject({ agent_run_id: 'run_2' });
  await expect(listIncidentRuns('inc_1')).resolves.toHaveLength(1);
  await expect(getAgentRun('run_1')).resolves.toMatchObject({ agent_run_id: 'run_1' });
  await expect(listIncidentApprovals('inc_1')).resolves.toEqual([]);
  await expect(rejectApproval('apv_1', { approver: 'sre-oncall', comment: 'too risky' })).resolves.toMatchObject({ status: 'rejected' });
  await expect(regenerateIncidentReport('inc_1')).resolves.toMatchObject({ version: 1 });
  expect(fetchMock.mock.calls.some(([url, init]) => String(url).endsWith('/diagnose') && init?.method === 'POST')).toBe(true);
});


test('phase feedback and collaboration helpers call expected endpoints', async () => {
  const calls: Array<{ path: string; method: string; body: unknown }> = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init = {}) => {
    const url = new URL(String(input), 'http://localhost');
    const method = init.method ?? 'GET';
    calls.push({ path: url.pathname, method, body: init.body ? JSON.parse(String(init.body)) : null });
    if (method === 'DELETE') {
      return new Response(null, { status: 204 });
    }
    if (url.pathname.endsWith('/correlated')) {
      return jsonResponse([]);
    }
    if (url.pathname.endsWith('/feedback') && method === 'GET') {
      return jsonResponse({ items: [], total: 0 });
    }
    if (url.pathname.endsWith('/comments') && method === 'GET') {
      return jsonResponse({ items: [], total: 0 });
    }
    if (url.pathname.endsWith('/annotations') && method === 'GET') {
      return jsonResponse({ items: [], total: 0 });
    }
    if (url.pathname.endsWith('/audit')) {
      return jsonResponse({ items: [], total: 0 });
    }
    if (url.pathname === '/api/approvals/batch') {
      return jsonResponse([{ approval_id: 'apv_1', action_id: 'act_1', status: 'approved', agent_run_id: 'run_1' }]);
    }
    if (url.pathname.endsWith('/nfa')) {
      return jsonResponse({ pattern_id: 'mem_1', fingerprint: 'fp_1', nfa_count: 1, status: 'recorded', message: 'recorded' });
    }
    if (url.pathname.endsWith('/comments')) {
      return jsonResponse({ comment_id: 'cmt_1', incident_id: 'inc_1', author: 'sre', content: 'ack', parent_comment_id: null, mentioned_users: [], created_at: '2026-06-01T00:00:00Z' }, 201);
    }
    if (url.pathname.endsWith('/annotations')) {
      return jsonResponse({ annotation_id: 'ann_1', evidence_id: 'evd_1', incident_id: 'inc_1', author: 'sre', content: 'note', created_at: '2026-06-01T00:00:00Z' }, 201);
    }
    return jsonResponse({ feedback_id: 'fb_1', incident_id: 'inc_1', feedback_type: 'correction', original_value: null, corrected_value: null, delta: null, submitted_by: 'sre', submitted_at: '2026-06-01T00:00:00Z' });
  });

  await expect(markIncidentNFA('inc_1', { reason: 'noise' })).resolves.toMatchObject({ status: 'recorded' });
  await expect(correctIncidentRootCause('inc_1', { corrected_summary: 'actual root cause' })).resolves.toMatchObject({ feedback_id: 'fb_1' });
  await expect(correctIncidentAction('inc_1', 'act_1', { action_type: 'remove', action_id: 'act_1' })).resolves.toMatchObject({ feedback_id: 'fb_1' });
  await expect(getCorrelatedIncidents('inc_1')).resolves.toEqual([]);
  await expect(listIncidentFeedback('inc_1')).resolves.toEqual({ items: [], total: 0 });
  await expect(listIncidentComments('inc_1')).resolves.toEqual({ items: [], total: 0 });
  await expect(createComment('inc_1', { author: 'sre', content: 'ack' })).resolves.toMatchObject({ comment_id: 'cmt_1' });
  await expect(deleteComment('cmt_1')).resolves.toBeUndefined();
  await expect(listEvidenceAnnotations('evd_1')).resolves.toEqual({ items: [], total: 0 });
  await expect(createEvidenceAnnotation('evd_1', { author: 'sre', content: 'note' })).resolves.toMatchObject({ annotation_id: 'ann_1' });
  await expect(listIncidentAudit('inc_1')).resolves.toEqual({ items: [], total: 0 });
  await expect(batchDecideApprovals({ decision: 'approve', approver: 'sre', approval_ids: ['apv_1'] })).resolves.toHaveLength(1);

  expect(calls).toEqual(expect.arrayContaining([
    expect.objectContaining({ path: '/api/incidents/inc_1/nfa', method: 'POST' }),
    expect.objectContaining({ path: '/api/incidents/inc_1/root-cause', method: 'PATCH' }),
    expect.objectContaining({ path: '/api/incidents/inc_1/actions/act_1/feedback', method: 'POST' }),
    expect.objectContaining({ path: '/api/comments/cmt_1', method: 'DELETE' }),
    expect.objectContaining({ path: '/api/evidence/evd_1/annotations', method: 'POST' }),
    expect.objectContaining({ path: '/api/approvals/batch', method: 'POST' })
  ]));
});
