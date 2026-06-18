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
  source_id?: string | null;
  source_path?: string | null;
  title: string;
  excerpt: string;
  metadata?: Record<string, unknown>;
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

export type ApiKeyCreatePayload = {
  description: string;
  expires_in_days?: number | null;
  scopes?: string[];
  roles?: string[];
};

export type ApiKeyCreateResponse = {
  key_id: string;
  description: string;
  raw_key: string;
  created_by: string;
  scopes: string[];
  roles: string[];
  expires_at: string | null;
  created_at: string;
};

export type WebSocketTicketResponse = {
  ticket: string;
  expires_at: string;
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
const API_KEY_STORAGE_KEY = 'sre_api_key';

export function getStoredApiKey(): string | null {
  if (typeof window === 'undefined') {
    return null;
  }
  return window.localStorage.getItem(API_KEY_STORAGE_KEY);
}

export function setStoredApiKey(apiKey: string): void {
  if (typeof window === 'undefined') {
    return;
  }
  const trimmed = apiKey.trim();
  if (trimmed) {
    window.localStorage.setItem(API_KEY_STORAGE_KEY, trimmed);
  }
}

export function clearStoredApiKey(): void {
  if (typeof window === 'undefined') {
    return;
  }
  window.localStorage.removeItem(API_KEY_STORAGE_KEY);
}

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
  authToken?: string | null;
} = {}): Promise<T> {
  const headers = new Headers({ 'X-Request-Id': requestId() });
  if (options.body !== undefined) {
    headers.set('Content-Type', 'application/json');
  }
  const apiKey = options.authToken !== undefined ? options.authToken : getStoredApiKey();
  if (apiKey) {
    headers.set('Authorization', `Bearer ${apiKey}`);
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

export function createApiKey(payload: ApiKeyCreatePayload, authToken: string): Promise<ApiKeyCreateResponse> {
  return apiRequest<ApiKeyCreateResponse>('/api/api-keys', {
    method: 'POST',
    body: payload,
    authToken
  });
}

export function createWebSocketTicket(incidentId: string): Promise<WebSocketTicketResponse> {
  return apiRequest<WebSocketTicketResponse>(`/api/ws/incidents/${incidentId}/ticket`, { method: 'POST', body: {} });
}

export function getIncidentReport(incidentId: string): Promise<IncidentReport> {
  return apiRequest<IncidentReport>(`/api/incidents/${incidentId}/report`);
}

export function regenerateIncidentReport(incidentId: string): Promise<IncidentReport> {
  return apiRequest<IncidentReport>(`/api/incidents/${incidentId}/report/regenerate`, { method: 'POST', body: {} });
}

// ---------------------------------------------------------------------------
// Phase 5: Memory & Continuous Learning
// ---------------------------------------------------------------------------

export type NFAMarkRequest = { reason?: string | null };
export type NFAMarkResponse = { pattern_id: string; fingerprint: string; nfa_count: number; status: string; message: string };
export type RootCauseCorrectionRequest = { corrected_summary: string; reason?: string | null };
export type ActionCorrectionRequest = { action_type: 'add' | 'remove'; action?: Record<string, unknown> | null; action_id?: string | null; reason?: string | null };
export type CorrelatedIncident = { incident_id: string; service: string; severity: string; alert_name: string; root_cause_summary: string | null; correlation_type: string; similarity_score: number | null; created_at: string };
export type FeedbackItem = { feedback_id: string; incident_id: string; feedback_type: string; original_value: Record<string, unknown> | null; corrected_value: Record<string, unknown> | null; delta: Record<string, unknown> | null; submitted_by: string; submitted_at: string };
export type FeedbackListResponse = { items: FeedbackItem[]; total: number };

export function markIncidentNFA(incidentId: string, payload: NFAMarkRequest): Promise<NFAMarkResponse> {
  return apiRequest<NFAMarkResponse>(`/api/incidents/${incidentId}/nfa`, { method: 'POST', body: payload });
}

export function correctIncidentRootCause(incidentId: string, payload: RootCauseCorrectionRequest): Promise<FeedbackItem> {
  return apiRequest<FeedbackItem>(`/api/incidents/${incidentId}/root-cause`, { method: 'PATCH', body: payload });
}

export function correctIncidentAction(incidentId: string, actionId: string, payload: ActionCorrectionRequest): Promise<FeedbackItem> {
  return apiRequest<FeedbackItem>(`/api/incidents/${incidentId}/actions/${actionId}/feedback`, { method: 'POST', body: payload });
}

export function getCorrelatedIncidents(incidentId: string): Promise<CorrelatedIncident[]> {
  return apiRequest<CorrelatedIncident[]>(`/api/incidents/${incidentId}/correlated`);
}

export function listIncidentFeedback(incidentId: string): Promise<FeedbackListResponse> {
  return apiRequest<FeedbackListResponse>(`/api/incidents/${incidentId}/feedback`);
}

// ---------------------------------------------------------------------------
// Phase 6: Collaboration & Approval Enhancement
// ---------------------------------------------------------------------------

export type CommentItem = {
  comment_id: string;
  incident_id: string;
  author: string;
  content: string;
  parent_comment_id: string | null;
  mentioned_users: string[];
  created_at: string;
};
export type CommentListResponse = { items: CommentItem[]; total: number };
export type CommentCreatePayload = { author: string; content: string; parent_comment_id?: string | null; mentioned_users?: string[] };

export type AnnotationItem = {
  annotation_id: string;
  evidence_id: string;
  incident_id: string;
  author: string;
  content: string;
  created_at: string;
};
export type AnnotationListResponse = { items: AnnotationItem[]; total: number };
export type AnnotationCreatePayload = { author: string; content: string };

export type AuditLogItem = {
  audit_id: string;
  incident_id: string | null;
  actor: string;
  action: string;
  resource_type: string;
  resource_id: string;
  details: Record<string, unknown>;
  created_at: string;
};
export type AuditLogListResponse = { items: AuditLogItem[]; total: number };

export type BatchApprovalPayload = {
  decision: 'approve' | 'reject';
  approver: string;
  comment?: string | null;
  approval_ids: string[];
  risk_ack?: boolean;
  confirm_action_type?: string | null;
  confirm_target?: string | null;
};

export function listIncidentComments(incidentId: string): Promise<CommentListResponse> {
  return apiRequest<CommentListResponse>(`/api/incidents/${incidentId}/comments`);
}

export function createComment(incidentId: string, payload: CommentCreatePayload): Promise<CommentItem> {
  return apiRequest<CommentItem>(`/api/incidents/${incidentId}/comments`, { method: 'POST', body: payload });
}

export function deleteComment(commentId: string): Promise<void> {
  return apiRequest<void>(`/api/comments/${commentId}`, { method: 'DELETE' });
}

export function listEvidenceAnnotations(evidenceId: string): Promise<AnnotationListResponse> {
  return apiRequest<AnnotationListResponse>(`/api/evidence/${evidenceId}/annotations`);
}

export function createEvidenceAnnotation(evidenceId: string, payload: AnnotationCreatePayload): Promise<AnnotationItem> {
  return apiRequest<AnnotationItem>(`/api/evidence/${evidenceId}/annotations`, { method: 'POST', body: payload });
}

export function listIncidentAudit(incidentId: string): Promise<AuditLogListResponse> {
  return apiRequest<AuditLogListResponse>(`/api/incidents/${incidentId}/audit`);
}

export function batchDecideApprovals(payload: BatchApprovalPayload): Promise<ApprovalDecision[]> {
  return apiRequest<ApprovalDecision[]>('/api/approvals/batch', { method: 'POST', body: payload });
}
