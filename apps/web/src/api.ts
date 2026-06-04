/// <reference types="vite/client" />
export type Severity = 'P1' | 'P2' | 'P3' | 'P4';
export type IncidentStatus = 'open' | 'diagnosing' | 'waiting_approval' | 'mitigated' | 'resolved' | 'failed';
export type AgentRunStatus = 'queued' | 'running' | 'waiting_approval' | 'succeeded' | 'failed' | 'cancelled';
export type RiskLevel = 'L0' | 'L1' | 'L2' | 'L3' | 'L4';
export type ActionStatus = 'proposed' | 'blocked' | 'waiting_approval' | 'approved' | 'rejected' | 'executing' | 'succeeded' | 'failed';
export type ApprovalStatus = 'waiting' | 'approved' | 'rejected' | 'expired';

export type PaginatedResponse<T> = {
  items: T[];
  total: number;
  page: number;
  page_size: number;
};

export type IncidentListItem = {
  incident_id: string;
  service: string;
  severity: Severity;
  status: IncidentStatus;
  alert_name: string;
  root_cause_summary: string | null;
  created_at: string;
  updated_at: string;
};

export type RootCause = {
  summary: string;
  confidence: number | null;
  evidence_ids: string[];
};

export type EvidenceItem = {
  evidence_id: string;
  type: string;
  source: string;
  title: string;
  excerpt: string;
  confidence: number | null;
  timestamp: string | null;
};

export type ActionSummary = {
  action_id: string;
  type: string;
  risk_level: RiskLevel;
  status: ActionStatus;
  reason: string;
  rollback_plan: string | null;
};

export type IncidentDetail = {
  incident_id: string;
  service: string;
  severity: Severity;
  status: IncidentStatus;
  alert: Record<string, unknown>;
  root_cause: RootCause | null;
  evidence: EvidenceItem[];
  recommended_actions: ActionSummary[];
};

export type AgentRunSummary = {
  agent_run_id: string;
  incident_id: string;
  status: AgentRunStatus;
  celery_task_id: string | null;
  created_at: string;
  updated_at: string;
};

export type AgentRunNode = {
  name: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  input_summary: string | null;
  output_summary: string | null;
  tool_calls: string[];
};

export type ToolCallSummary = {
  tool_call_id: string;
  node_name: string;
  tool_name: string;
  status: string;
  input_summary: string;
  output_summary: string | null;
  duration_ms: number | null;
  cache_key: string | null;
  cache_hit: boolean;
  error_message: string | null;
  created_at: string;
};

export type AgentRunDetail = {
  agent_run_id: string;
  incident_id: string;
  status: AgentRunStatus;
  celery_task_id: string | null;
  error_code: string | null;
  error_message: string | null;
  state: Record<string, unknown>;
  checkpoint_thread_id: string | null;
  checkpoint_ns: string;
  latest_checkpoint_id: string | null;
  nodes: AgentRunNode[];
  tool_calls: ToolCallSummary[];
  created_at: string;
  updated_at: string;
};

export type ApprovalItem = {
  approval_id: string;
  action_id: string;
  incident_id: string;
  agent_run_id: string;
  service: string;
  action_type: string;
  risk_level: RiskLevel;
  approval_status: ApprovalStatus;
  action_status: ActionStatus;
  reason: string;
  rollback_plan: string | null;
  requested_at: string;
  decided_at: string | null;
  approver: string | null;
  comment: string | null;
};

export type ActionDetail = {
  action_id: string;
  incident_id: string;
  agent_run_id: string;
  type: string;
  risk_level: RiskLevel;
  status: ActionStatus;
  executor: string;
  target: string | null;
  params: Record<string, unknown>;
  reason: string;
  rollback_plan: string | null;
  execution_result: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
};

export type ApprovalDecision = {
  approval_id: string;
  action_id: string;
  status: ApprovalStatus;
  agent_run_id: string;
};

export type ApprovalDecisionPayload = {
  approver: string;
  comment?: string | null;
  risk_ack?: boolean;
  confirm_action_type?: string | null;
  confirm_target?: string | null;
};

export type IncidentReport = {
  report_id: string;
  incident_id: string;
  agent_run_id: string;
  version: number;
  root_cause: string;
  impact: string;
  timeline: Array<Record<string, unknown>>;
  actions: Array<Record<string, unknown>>;
  follow_ups: Array<Record<string, unknown> | string>;
  evidence_ids: string[];
  body_markdown: string;
  created_at: string;
};

export type ListIncidentsFilters = {
  status?: string;
  service?: string;
  severity?: string;
  page?: number;
  page_size?: number;
};

export type ListApprovalsFilters = {
  status?: string;
  incident_id?: string;
  service?: string;
  risk_level?: string;
  page?: number;
  page_size?: number;
};

type ApiErrorBody = {
  code?: string;
  message?: string;
  request_id?: string;
  details?: Record<string, unknown>;
};

export class ApiError extends Error {
  status: number;
  code: string;
  requestId: string | null;
  details: Record<string, unknown>;

  constructor(message: string, status: number, code = 'HTTP_ERROR', requestId: string | null = null, details: Record<string, unknown> = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.requestId = requestId;
    this.details = details;
  }
}

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? '';

function requestId(): string {
  return `req_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;
}

function buildUrl(path: string, query?: Record<string, string | number | boolean | null | undefined>): string {
  const base = API_BASE_URL || window.location.origin;
  const url = new URL(path, base);
  Object.entries(query ?? {}).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') {
      url.searchParams.set(key, String(value));
    }
  });
  return API_BASE_URL ? url.toString() : `${url.pathname}${url.search}`;
}

async function parseBody(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) {
    return undefined;
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

async function apiRequest<T>(path: string, options: {
  method?: string;
  query?: Record<string, string | number | boolean | null | undefined>;
  body?: unknown;
} = {}): Promise<T> {
  const headers = new Headers({ 'X-Request-Id': requestId() });
  if (options.body !== undefined) {
    headers.set('Content-Type', 'application/json');
  }

  const response = await fetch(buildUrl(path, options.query), {
    method: options.method ?? 'GET',
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body)
  });
  const data = await parseBody(response);

  if (!response.ok) {
    const envelope = isRecord(data) && isRecord(data.error) ? (data.error as ApiErrorBody) : {};
    throw new ApiError(
      envelope.message ?? `Request failed with status ${response.status}`,
      response.status,
      envelope.code ?? 'HTTP_ERROR',
      envelope.request_id ?? response.headers.get('X-Request-Id'),
      envelope.details ?? {}
    );
  }

  return data as T;
}

function normalizePaginated<T>(value: unknown): PaginatedResponse<T> {
  if (Array.isArray(value)) {
    return { items: value as T[], total: value.length, page: 1, page_size: value.length };
  }
  if (isRecord(value) && Array.isArray(value.items)) {
    return value as PaginatedResponse<T>;
  }
  return { items: [], total: 0, page: 1, page_size: 20 };
}

export async function listIncidents(filters: ListIncidentsFilters = {}): Promise<PaginatedResponse<IncidentListItem>> {
  const data = await apiRequest<unknown>('/api/incidents', { query: filters });
  return normalizePaginated<IncidentListItem>(data);
}

export function getIncident(incidentId: string): Promise<IncidentDetail> {
  return apiRequest<IncidentDetail>(`/api/incidents/${incidentId}`);
}

export function triggerDiagnosis(incidentId: string, payload: { force: boolean; reason?: string | null }): Promise<AgentRunSummary> {
  return apiRequest<AgentRunSummary>(`/api/incidents/${incidentId}/diagnose`, { method: 'POST', body: payload });
}

export function listIncidentRuns(incidentId: string): Promise<AgentRunSummary[]> {
  return apiRequest<AgentRunSummary[]>(`/api/incidents/${incidentId}/runs`);
}

export function getAgentRun(agentRunId: string): Promise<AgentRunDetail> {
  return apiRequest<AgentRunDetail>(`/api/agent-runs/${agentRunId}`);
}

export async function listApprovals(filters: ListApprovalsFilters = {}): Promise<PaginatedResponse<ApprovalItem>> {
  const data = await apiRequest<unknown>('/api/approvals', { query: filters });
  return normalizePaginated<ApprovalItem>(data);
}

export function listIncidentApprovals(incidentId: string): Promise<ApprovalItem[]> {
  return apiRequest<ApprovalItem[]>(`/api/incidents/${incidentId}/approvals`);
}

export function getApproval(approvalId: string): Promise<ApprovalItem> {
  return apiRequest<ApprovalItem>(`/api/approvals/${approvalId}`);
}

export function getAction(actionId: string): Promise<ActionDetail> {
  return apiRequest<ActionDetail>(`/api/actions/${actionId}`);
}

export function approveApproval(approvalId: string, payload: ApprovalDecisionPayload): Promise<ApprovalDecision> {
  return apiRequest<ApprovalDecision>(`/api/approvals/${approvalId}/approve`, { method: 'POST', body: payload });
}

export function rejectApproval(approvalId: string, payload: ApprovalDecisionPayload): Promise<ApprovalDecision> {
  return apiRequest<ApprovalDecision>(`/api/approvals/${approvalId}/reject`, { method: 'POST', body: payload });
}

export function getIncidentReport(incidentId: string): Promise<IncidentReport> {
  return apiRequest<IncidentReport>(`/api/incidents/${incidentId}/report`);
}

export function regenerateIncidentReport(incidentId: string): Promise<IncidentReport> {
  return apiRequest<IncidentReport>(`/api/incidents/${incidentId}/report/regenerate`, { method: 'POST', body: {} });
}
