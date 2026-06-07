import {
  Activity,
  AlertTriangle,
  Bell,
  BellOff,
  Check,
  CheckCircle2,
  ChevronLeft,
  ClipboardCheck,
  Clock,
  Copy,
  Edit3,
  EyeOff,
  FileText,
  Gauge,
  GitBranch,
  History,
  KeyRound,
  ListChecks,
  Loader2,
  LogOut,
  MessageSquare,
  Network,
  Radio,
  RefreshCw,
  RotateCw,
  Search,
  ShieldAlert,
  Workflow,
  X,
  XCircle
} from 'lucide-react';
import { Component, useCallback, useEffect, useRef } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type { FormEvent, ReactNode } from 'react';
import { useMemo, useState } from 'react';
import { Link, Navigate, NavLink, Route, Routes, useParams, useSearchParams } from 'react-router-dom';

import {
  ApiError,
  approveApproval,
  clearStoredApiKey,
  correctIncidentRootCause,
  createApiKey,
  getAction,
  getAgentRun,
  getApproval,
  getCorrelatedIncidents,
  getIncident,
  getIncidentReport,
  getStoredApiKey,
  listApprovals,
  listIncidentApprovals,
  listIncidentAudit,
  listIncidentComments,
  listIncidentRuns,
  listIncidents,
  markIncidentNFA,
  regenerateIncidentReport,
  rejectApproval,
  setStoredApiKey,
  batchDecideApprovals,
  createComment,
  deleteComment,
  type ActionDetail,
  type ActionSummary,
  type AgentRunDetail,
  type ApiKeyCreateResponse,
  type ApprovalDecisionPayload,
  type ApprovalItem,
  type BatchApprovalPayload,
  type AuditLogItem,
  type CommentItem,
  type CorrelatedIncident,
  type EvidenceItem,
  type IncidentDetail,
  type IncidentListItem,
  type IncidentReport,
  type PaginatedResponse
} from './api';

const LIVE_STATUSES = new Set(['open', 'diagnosing', 'waiting_approval', 'queued', 'running', 'executing']);

// ---------------------------------------------------------------------------
// Phase 8: WebSocket hook for real-time updates
// ---------------------------------------------------------------------------

type WsEvent = { type: string; payload: Record<string, unknown>; timestamp?: string };
type WsConnectionState = 'disabled' | 'connecting' | 'open' | 'closed' | 'error';

function buildIncidentWebSocketUrl(incidentId: string): string {
  const apiBase = import.meta.env.VITE_API_BASE_URL as string | undefined;
  const baseUrl = apiBase ? new URL(apiBase, window.location.href) : new URL(window.location.href);
  const protocol = baseUrl.protocol === 'https:' ? 'wss:' : 'ws:';
  const url = new URL(`/api/ws/incidents/${incidentId}`, `${protocol}//${baseUrl.host}`);
  const token = getStoredApiKey();
  if (token) {
    url.searchParams.set('token', token);
  }
  return url.toString();
}

function useWebSocket(incidentId: string | null, enabled: boolean) {
  const wsRef = useRef<WebSocket | null>(null);
  const mountedRef = useRef(true);
  const handlersRef = useRef<Set<(event: WsEvent) => void>>(new Set());
  const reconnectTimerRef = useRef<number | null>(null);
  const [connectionState, setConnectionState] = useState<WsConnectionState>('disabled');
  const [events, setEvents] = useState<WsEvent[]>([]);

  const onEvent = useCallback((handler: (event: WsEvent) => void) => {
    handlersRef.current.add(handler);
    return () => { handlersRef.current.delete(handler); };
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    if (!enabled || !incidentId) {
      setConnectionState('disabled');
      setEvents([]);
      return undefined;
    }

    const wsUrl = buildIncidentWebSocketUrl(incidentId);

    function connect() {
      if (!mountedRef.current) return;
      setConnectionState('connecting');
      let ws: WebSocket;
      try {
        ws = new WebSocket(wsUrl);
      } catch {
        setConnectionState('error');
        return;
      }

      wsRef.current = ws;
      ws.onopen = () => { if (mountedRef.current) setConnectionState('open'); };
      ws.onmessage = (msg) => {
        if (!mountedRef.current) return;
        try {
          const event = JSON.parse(String(msg.data)) as WsEvent;
          setEvents((current) => [event, ...current].slice(0, 40));
          handlersRef.current.forEach((h) => h(event));
        } catch { /* ignore parse errors */ }
      };

      ws.onerror = () => { if (mountedRef.current) setConnectionState('error'); };
      ws.onclose = () => {
        if (!mountedRef.current) return;
        if (wsRef.current === ws) {
          wsRef.current = null;
        }
        setConnectionState((state) => (state === 'disabled' ? state : 'closed'));
        reconnectTimerRef.current = window.setTimeout(() => {
          if (!mountedRef.current) return;
          connect();
        }, 5000);
      };
    }

    connect();

    return () => {
      mountedRef.current = false;
      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      const ws = wsRef.current;
      if (ws) {
        wsRef.current = null;
        ws.close();
      }
    };
  }, [incidentId, enabled]);

  return { onEvent, connectionState, events };
}

class ErrorBoundary extends Component<{ children: ReactNode }, { hasError: boolean }> {
  constructor(props: { children: ReactNode }) {
    super(props);
    this.state = { hasError: false };
  }
  static getDerivedStateFromError() {
    return { hasError: true };
  }
  render() {
    if (this.state.hasError) {
      return <NotFoundPage />;
    }
    return this.props.children;
  }
}

export default function App() {
  return (
    <div className="appShell">
      <aside className="sidebar" aria-label="Primary navigation">
        <div className="brand">
          <Activity size={22} />
          <div>
            <h1>SRE Incident Console</h1>
            <span>Local response workspace</span>
          </div>
        </div>
        <nav className="navLinks">
          <NavItem to="/incidents" icon={<AlertTriangle size={18} />} label="Incidents" />
          <NavItem to="/approvals" icon={<ClipboardCheck size={18} />} label="Approvals" />
        </nav>
        <AuthPanel />
      </aside>

      <main className="contentPane">
        <ErrorBoundary>
        <Routes>
          <Route path="/" element={<Navigate to="/incidents" replace />} />
          <Route path="/incidents" element={<IncidentsPage />} />
          <Route path="/incidents/:incidentId" element={<IncidentDetailPage />} />
          <Route path="/agent-runs/:agentRunId" element={<AgentRunPage />} />
          <Route path="/approvals" element={<ApprovalsPage />} />
          <Route path="/approvals/:approvalId" element={<ApprovalsPage />} />
          <Route path="/incidents/:incidentId/report" element={<ReportPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Routes>
        </ErrorBoundary>
      </main>
    </div>
  );
}

function NavItem({ to, icon, label }: { to: string; icon: ReactNode; label: string }) {
  return (
    <NavLink className={({ isActive }) => (isActive ? 'navItem active' : 'navItem')} to={to}>
      {icon}
      <span>{label}</span>
    </NavLink>
  );
}

function AuthPanel() {
  const queryClient = useQueryClient();
  const [savedKey, setSavedKey] = useState(() => getStoredApiKey() ?? '');
  const [manualKey, setManualKey] = useState('');
  const [bootstrapToken, setBootstrapToken] = useState('');
  const [description, setDescription] = useState('local web key');
  const [expiresInDays, setExpiresInDays] = useState('90');
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [generatedKey, setGeneratedKey] = useState<string | null>(null);
  const [copyStatus, setCopyStatus] = useState<string | null>(null);

  const refreshActiveQueries = useCallback(() => {
    void queryClient.invalidateQueries();
    void queryClient.refetchQueries({ type: 'active' });
  }, [queryClient]);

  const generateMutation = useMutation<ApiKeyCreateResponse, ApiError | Error>({
    mutationFn: () => {
      const authToken = bootstrapToken.trim();
      if (!authToken) {
        throw new Error('Enter a bootstrap seed or an existing admin API key.');
      }
      const trimmedDescription = description.trim() || 'local web key';
      const expiryText = expiresInDays.trim();
      const expiry = expiryText ? Number(expiryText) : null;
      if (expiryText && (!Number.isInteger(expiry) || Number(expiry) <= 0)) {
        throw new Error('Expires in days must be a positive whole number.');
      }
      return createApiKey({ description: trimmedDescription, expires_in_days: expiry }, authToken);
    },
    onSuccess: (created) => {
      setStoredApiKey(created.raw_key);
      setSavedKey(created.raw_key);
      setManualKey('');
      setBootstrapToken('');
      setGeneratedKey(created.raw_key);
      setStatusMessage(`Created ${created.key_id} and saved it for this browser.`);
      setCopyStatus(null);
      refreshActiveQueries();
    }
  });

  function handleSaveExisting(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = manualKey.trim();
    if (!trimmed) {
      setStatusMessage('Enter an API key before saving.');
      return;
    }
    setStoredApiKey(trimmed);
    setSavedKey(trimmed);
    setManualKey('');
    setGeneratedKey(null);
    setCopyStatus(null);
    setStatusMessage('API key saved for this browser.');
    refreshActiveQueries();
  }

  function handleClearKey() {
    clearStoredApiKey();
    setSavedKey('');
    setManualKey('');
    setGeneratedKey(null);
    setCopyStatus(null);
    setStatusMessage('Saved API key cleared.');
    refreshActiveQueries();
  }

  async function copyGeneratedKey() {
    if (!generatedKey || !navigator.clipboard) {
      setCopyStatus('Copy unavailable');
      return;
    }
    try {
      await navigator.clipboard.writeText(generatedKey);
      setCopyStatus('Copied');
    } catch {
      setCopyStatus('Copy failed');
    }
  }

  const hasSavedKey = Boolean(savedKey);

  return (
    <section className="authPanel" aria-label="API authentication">
      <div className="authHeader">
        <KeyRound size={18} />
        <div>
          <strong>Authentication</strong>
          <span>{hasSavedKey ? `Saved ${maskApiKey(savedKey)}` : 'No API key saved'}</span>
        </div>
      </div>

      {statusMessage ? <div className="authNotice">{statusMessage}</div> : null}

      {generatedKey ? (
        <div className="generatedKeyBox">
          <span>Generated key</span>
          <code>{generatedKey}</code>
          <button className="iconTextButton" type="button" onClick={() => void copyGeneratedKey()}>
            <Copy size={15} />
            Copy
          </button>
          {copyStatus ? <small>{copyStatus}</small> : null}
        </div>
      ) : null}

      <form className="authForm" onSubmit={handleSaveExisting}>
        <label>
          <span>API key</span>
          <input
            aria-label="API key"
            autoComplete="off"
            placeholder={hasSavedKey ? 'Replace saved key' : 'Paste raw key'}
            type="password"
            value={manualKey}
            onChange={(event) => setManualKey(event.target.value)}
          />
        </label>
        <button className="iconTextButton fullWidth" type="submit">
          <Check size={16} />
          Save key
        </button>
      </form>

      <form className="authForm" onSubmit={(event) => { event.preventDefault(); generateMutation.mutate(); }}>
        <label>
          <span>Bootstrap seed or admin key</span>
          <input
            aria-label="Bootstrap seed or admin key"
            autoComplete="off"
            placeholder="dev-bootstrap-secret"
            type="password"
            value={bootstrapToken}
            onChange={(event) => setBootstrapToken(event.target.value)}
          />
        </label>
        <label>
          <span>Description</span>
          <input
            aria-label="API key description"
            value={description}
            onChange={(event) => setDescription(event.target.value)}
          />
        </label>
        <label>
          <span>Expires in days</span>
          <input
            aria-label="API key expiry days"
            min="1"
            inputMode="numeric"
            value={expiresInDays}
            onChange={(event) => setExpiresInDays(event.target.value)}
          />
        </label>
        {generateMutation.isError ? <div className="formError">{generateMutation.error.message}</div> : null}
        <button className="iconTextButton primary fullWidth" type="submit" disabled={generateMutation.isPending}>
          {generateMutation.isPending ? <Loader2 className="spin" size={16} /> : <KeyRound size={16} />}
          Generate key
        </button>
      </form>

      {hasSavedKey ? (
        <button className="iconTextButton fullWidth" type="button" onClick={handleClearKey}>
          <LogOut size={16} />
          Clear key
        </button>
      ) : null}
    </section>
  );
}

function maskApiKey(value: string): string {
  if (value.length <= 10) {
    return 'key set';
  }
  return `${value.slice(0, 4)}...${value.slice(-4)}`;
}

function IncidentsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const filters = useMemo(
    () => ({
      status: searchParams.get('status') ?? undefined,
      service: searchParams.get('service') ?? undefined,
      severity: searchParams.get('severity') ?? undefined,
      page_size: 50
    }),
    [searchParams]
  );
  const query = useQuery<PaginatedResponse<IncidentListItem>, ApiError>({
    queryKey: ['incidents', filters],
    queryFn: () => listIncidents(filters),
    refetchInterval: (request) => {
      const data = request.state.data as PaginatedResponse<IncidentListItem> | undefined;
      return data?.items.some((incident) => LIVE_STATUSES.has(incident.status)) ? 5000 : false;
    }
  });

  function onFilter(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const next = new URLSearchParams();
    ['status', 'service', 'severity'].forEach((key) => {
      const value = String(data.get(key) ?? '').trim();
      if (value) {
        next.set(key, value);
      }
    });
    setSearchParams(next);
  }

  return (
    <>
      <PageHeader
        eyebrow="Incidents"
        title="Active diagnosis queue"
        actions={
          <button className="iconTextButton" type="button" onClick={() => void query.refetch()}>
            <RefreshCw size={17} />
            Refresh
          </button>
        }
      />

      <form className="filterBar" key={searchParams.toString()} onSubmit={onFilter}>
        <label>
          <span>Service</span>
          <input name="service" defaultValue={filters.service ?? ''} placeholder="checkout-api" />
        </label>
        <label>
          <span>Status</span>
          <select name="status" defaultValue={filters.status ?? ''}>
            <option value="">Any</option>
            <option value="open">Open</option>
            <option value="diagnosing">Diagnosing</option>
            <option value="waiting_approval">Waiting approval</option>
            <option value="mitigated">Mitigated</option>
            <option value="resolved">Resolved</option>
            <option value="failed">Failed</option>
          </select>
        </label>
        <label>
          <span>Severity</span>
          <select name="severity" defaultValue={filters.severity ?? ''}>
            <option value="">Any</option>
            <option value="P1">P1</option>
            <option value="P2">P2</option>
            <option value="P3">P3</option>
            <option value="P4">P4</option>
          </select>
        </label>
        <button className="iconTextButton primary" type="submit">
          <Search size={17} />
          Filter
        </button>
        <button className="iconTextButton" type="button" onClick={() => setSearchParams(new URLSearchParams())}>
          <X size={17} />
          Clear
        </button>
      </form>

      <section className="dataSurface" aria-label="Incident list">
        <div className="dataToolbar">
          <strong>{query.data?.total ?? 0} incidents</strong>
          <span>Updated {query.data ? formatDate(new Date().toISOString()) : 'after load'}</span>
        </div>
        <div className="dataTable incidentGrid">
          <div className="tableHeader" role="row">
            <span>Service</span>
            <span>Alert</span>
            <span>Severity</span>
            <span>Status</span>
            <span>Root cause</span>
            <span>Updated</span>
          </div>
          {query.isLoading ? <LoadingRows label="Loading incidents" count={4} /> : null}
          {query.isError ? <ErrorState title="Unable to load incidents" error={query.error} onRetry={() => void query.refetch()} /> : null}
          {!query.isLoading && !query.isError && query.data?.items.length === 0 ? (
            <EmptyState title="No incidents" detail="No incidents match the current filters." />
          ) : null}
          {query.data?.items.map((incident) => (
            <Link className="tableRow linkedRow" to={`/incidents/${incident.incident_id}`} key={incident.incident_id}>
              <span className="strongCell">{incident.service}</span>
              <span>{incident.alert_name}</span>
              <span><SeverityBadge value={incident.severity} /></span>
              <span><StatusBadge value={incident.status} /></span>
              <span className="mutedCell">{incident.root_cause_summary ?? 'Pending diagnosis'}</span>
              <span className="mutedCell">{formatDate(incident.updated_at)}</span>
            </Link>
          ))}
        </div>
      </section>
    </>
  );
}

function IncidentDetailPage() {
  const incidentId = useRequiredParam('incidentId');
  const queryClient = useQueryClient();
  const incidentQuery = useQuery<IncidentDetail, ApiError>({
    queryKey: ['incident', incidentId],
    queryFn: () => getIncident(incidentId),
    refetchInterval: (request) => {
      const data = request.state.data as IncidentDetail | undefined;
      return data && LIVE_STATUSES.has(data.status) ? 5000 : false;
    }
  });
  const runsQuery = useQuery({ queryKey: ['incident-runs', incidentId], queryFn: () => listIncidentRuns(incidentId) });
  const approvalsQuery = useQuery({ queryKey: ['incident-approvals', incidentId], queryFn: () => listIncidentApprovals(incidentId) });
  const correlatedQuery = useQuery<CorrelatedIncident[], ApiError>({
    queryKey: ['correlated-incidents', incidentId],
    queryFn: () => getCorrelatedIncidents(incidentId),
    staleTime: 60000
  });

  // NFA mutation
  const nfaMutation = useMutation({
    mutationFn: (reason?: string) => markIncidentNFA(incidentId, { reason }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['incident', incidentId] });
    }
  });

  // Root cause correction
  const [editingRootCause, setEditingRootCause] = useState(false);
  const rootCauseMutation = useMutation({
    mutationFn: (corrected_summary: string) => correctIncidentRootCause(incidentId, { corrected_summary }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['incident', incidentId] });
      setEditingRootCause(false);
    }
  });

  if (incidentQuery.isLoading) {
    return <LoadingPage title="Loading incident" />;
  }
  if (incidentQuery.isError) {
    return <ErrorState title="Unable to load incident" error={incidentQuery.error} onRetry={() => void incidentQuery.refetch()} />;
  }

  const incident = incidentQuery.data;
  if (!incident) {
    return <EmptyState title="Incident unavailable" detail="The incident response was empty." />;
  }
  const latestRun = runsQuery.data?.[0];
  const approvals = approvalsQuery.data ?? [];
  const correlated = correlatedQuery.data ?? [];

  function handleRootCauseSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const corrected = String(data.get('corrected_summary') ?? '').trim();
    if (corrected) {
      rootCauseMutation.mutate(corrected);
    }
  }

  return (
    <>
      <BackLink to="/incidents">Incidents</BackLink>
      <PageHeader
        eyebrow={incident.service}
        title={stringValue(incident.alert.alert_name) || incident.incident_id}
        meta={<><SeverityBadge value={incident.severity} /><StatusBadge value={incident.status} /></>}
        actions={
          <>
            {latestRun ? (
              <Link className="iconTextButton" to={`/agent-runs/${latestRun.agent_run_id}`}>
                <History size={17} />
                Agent run
              </Link>
            ) : null}
            <Link className="iconTextButton" to={`/incidents/${incident.incident_id}/report`}>
              <FileText size={17} />
              Report
            </Link>
            <button
              className="iconTextButton"
              type="button"
              onClick={() => {
                if (confirm('Mark this incident as Not Actionable?')) {
                  nfaMutation.mutate(undefined);
                }
              }}
              disabled={nfaMutation.isPending}
            >
              <EyeOff size={17} />
              {nfaMutation.isPending ? 'Marking...' : 'NFA'}
            </button>
          </>
        }
      />

      {nfaMutation.data ? (
        <div className={`callout ${nfaMutation.data.status === 'suppressed' ? 'warning' : 'info'}`}>
          <MessageSquare size={18} />
          {nfaMutation.data.message}
        </div>
      ) : null}
      {nfaMutation.isError ? (
        <div className="callout danger"><XCircle size={18} />{(nfaMutation.error as ApiError).message}</div>
      ) : null}

      <div className="detailGrid">
        <section className="sectionBlock wide">
          <SectionTitle icon={<Gauge size={18} />} title="Diagnosis" />
          {incident.root_cause ? (
            editingRootCause ? (
              <form className="inlineForm" onSubmit={handleRootCauseSubmit}>
                <textarea name="corrected_summary" rows={3} className="fullWidth" defaultValue={incident.root_cause.summary} />
                <div className="inlineFormActions">
                  <button className="iconTextButton primary" type="submit" disabled={rootCauseMutation.isPending}>
                    <Check size={17} />
                    Save correction
                  </button>
                  <button className="iconTextButton" type="button" onClick={() => setEditingRootCause(false)}>
                    <X size={17} />
                    Cancel
                  </button>
                </div>
              </form>
            ) : (
              <div className="diagnosisBox">
                <p>{incident.root_cause.summary}</p>
                <div className="inlineMeta">
                  <span>Confidence {formatPercent(incident.root_cause.confidence)}</span>
                  <span>Evidence {incident.root_cause.evidence_ids.length || incident.evidence.length}</span>
                </div>
                <button className="iconTextButton" type="button" onClick={() => setEditingRootCause(true)}>
                  <Edit3 size={16} />
                  Correct root cause
                </button>
              </div>
            )
          ) : (
            <EmptyState title="Diagnosis pending" detail="The agent has not written a root cause yet." />
          )}
          {rootCauseMutation.isError ? (
            <div className="formError">{(rootCauseMutation.error as ApiError).message}</div>
          ) : null}
        </section>

        <section className="sectionBlock">
          <SectionTitle icon={<AlertTriangle size={18} />} title="Alert" />
          <AlertSummary alert={incident.alert} />
        </section>

        <section className="sectionBlock wide">
          <SectionTitle icon={<ListChecks size={18} />} title="Evidence" />
          <EvidenceList items={incident.evidence} />
        </section>

        {correlated.length > 0 ? (
          <section className="sectionBlock wide">
            <SectionTitle icon={<GitBranch size={18} />} title="Related incidents" />
            <div className="compactList">
              {correlated.map((ci) => (
                <article className="actionItem" key={ci.incident_id}>
                  <div className="itemHeader">
                    <Link to={`/incidents/${ci.incident_id}`}>
                      <strong>{ci.service}</strong>
                    </Link>
                    <span className="chip">{ci.correlation_type === 'same_fingerprint' ? 'Same fingerprint' : 'Same service'}</span>
                    <SeverityBadge value={ci.severity} />
                  </div>
                  <p>{ci.root_cause_summary ?? 'No root cause recorded'}</p>
                  <div className="inlineMeta">
                    <span>{ci.alert_name}</span>
                    <span>{formatDate(ci.created_at)}</span>
                  </div>
                </article>
              ))}
            </div>
          </section>
        ) : null}

        <section className="sectionBlock wide">
          <SectionTitle icon={<ShieldAlert size={18} />} title="Recommended actions" />
          <ActionList actions={incident.recommended_actions} />
        </section>

        <section className="sectionBlock wide">
          <SectionTitle icon={<ClipboardCheck size={18} />} title="Approvals" />
          <ApprovalSummary approvals={approvals} loading={approvalsQuery.isLoading} />
        </section>

        <section className="sectionBlock wide">
          <SectionTitle icon={<MessageSquare size={18} />} title="Comments" />
          <CommentSection incidentId={incident.incident_id} />
        </section>

        <section className="sectionBlock wide">
          <SectionTitle icon={<History size={18} />} title="Audit trail" />
          <AuditSection incidentId={incident.incident_id} />
        </section>
      </div>
    </>
  );
}

function AgentRunPage() {
  const agentRunId = useRequiredParam('agentRunId');
  const queryClient = useQueryClient();
  const query = useQuery<AgentRunDetail, ApiError>({
    queryKey: ['agent-run', agentRunId],
    queryFn: () => getAgentRun(agentRunId),
    refetchInterval: (request) => {
      const data = request.state.data as AgentRunDetail | undefined;
      return data && LIVE_STATUSES.has(data.status) ? 5000 : false;
    }
  });
  const live = useWebSocket(
    query.data?.incident_id ?? null,
    Boolean(query.data && LIVE_STATUSES.has(query.data.status))
  );

  useEffect(() => {
    const latest = live.events[0];
    if (!latest) return;
    const eventRunId = stringValue(latest.payload.agent_run_id);
    if (eventRunId && eventRunId !== agentRunId) return;
    if (['node_update', 'approval_update', 'incident_update'].includes(latest.type)) {
      void queryClient.invalidateQueries({ queryKey: ['agent-run', agentRunId] });
      if (query.data?.incident_id) {
        void queryClient.invalidateQueries({ queryKey: ['incident', query.data.incident_id] });
      }
    }
  }, [agentRunId, live.events, query.data?.incident_id, queryClient]);

  if (query.isLoading) {
    return <LoadingPage title="Loading agent run" />;
  }
  if (query.isError) {
    return <ErrorState title="Unable to load agent run" error={query.error} onRetry={() => void query.refetch()} />;
  }

  const run = query.data;
  if (!run) {
    return <EmptyState title="Run unavailable" detail="The agent run response was empty." />;
  }
  const compressionEvents = asRecordArray(run.state.compression_events);
  const progress = getRunProgress(run);

  return (
    <>
      <BackLink to={`/incidents/${run.incident_id}`}>Incident</BackLink>
      <PageHeader
        eyebrow={run.agent_run_id}
        title="Agent run trace"
        meta={<StatusBadge value={run.status} />}
        actions={
          <button className="iconTextButton" type="button" onClick={() => void query.refetch()}>
            <RefreshCw size={17} />
            Refresh
          </button>
        }
      />

      <div className="metricStrip">
        <Metric label="Progress" value={`${progress.completed}/${progress.total}`} />
        <Metric label="Current node" value={progress.currentNode ?? 'idle'} />
        <Metric label="Checkpoint" value={run.latest_checkpoint_id ?? run.checkpoint_thread_id ?? 'none'} />
        <Metric label="Tool calls" value={String(run.tool_calls.length)} />
        <Metric label="Compression" value={String(compressionEvents.length)} />
      </div>

      <RunProgress progress={progress} connectionState={live.connectionState} />

      {run.error_message ? (
        <div className="callout danger"><XCircle size={18} />{run.error_code}: {run.error_message}</div>
      ) : null}

      {run.status === 'waiting_approval' ? (
        <PendingApprovalsSection incidentId={run.incident_id} agentRunId={run.agent_run_id} />
      ) : null}

      <section className="sectionBlock wide">
        <SectionTitle icon={<History size={18} />} title="Timeline" />
        {progress.entries.length === 0 ? <EmptyState title="No nodes recorded" detail="The run has not emitted node trace events." /> : <RunTimeline run={run} progress={progress} />}
      </section>

      <section className="sectionBlock wide">
        <SectionTitle icon={<Radio size={18} />} title="Live node log" />
        <LiveNodeLog events={live.events} run={run} />
      </section>

      <section className="sectionBlock wide">
        <SectionTitle icon={<Workflow size={18} />} title="Diagnosis visualizations" />
        <DiagnosisVisualizations run={run} />
      </section>

      <section className="sectionBlock wide">
        <SectionTitle icon={<Activity size={18} />} title="Tool calls" />
        {run.tool_calls.length === 0 ? <EmptyState title="No tool calls" detail="No tool calls have been audited for this run." /> : <ToolCallList run={run} />}
      </section>

      <section className="sectionBlock wide">
        <SectionTitle icon={<Gauge size={18} />} title="Token and context" />
        <ContextSummary state={run.state} compressionEvents={compressionEvents} />
      </section>
    </>
  );
}

function PendingApprovalsSection({ incidentId, agentRunId }: { incidentId: string; agentRunId: string }) {
  const approvalsQuery = useQuery({
    queryKey: ['incident-approvals', incidentId],
    queryFn: () => listIncidentApprovals(incidentId),
    refetchInterval: 5000
  });

  const pending = approvalsQuery.data?.filter((a) => a.approval_status === 'waiting') ?? [];

  return (
    <section className="sectionBlock wide">
      <SectionTitle icon={<ClipboardCheck size={18} />} title="Pending approvals" />
      {approvalsQuery.isLoading ? <LoadingRows label="Loading pending approvals" count={1} /> : null}
      {!approvalsQuery.isLoading && pending.length === 0 ? (
        <EmptyState title="No pending approvals" detail="All approvals for this run have been decided — the run should resume shortly." />
      ) : null}
      {pending.length > 0 ? (
        <div className="callout warning">
          <Bell size={18} />
          <div>
            <strong>Approval required</strong>
            <p>{pending.length} action{pending.length > 1 ? 's' : ''} waiting for approval. An email with approve/reject links has been sent to the SRE team. You can also review and decide in the <Link to="/approvals">Approvals</Link> page.</p>
          </div>
        </div>
      ) : null}
      {pending.map((approval) => (
        <article className="approvalItem" key={approval.approval_id}>
          <div className="approvalBody">
            <div className="approvalTitle">
              <strong>{approval.action_type}</strong>
              <RiskBadge value={approval.risk_level} />
              <StatusBadge value={approval.approval_status} />
            </div>
            <p>{approval.reason}</p>
            <div className="inlineMeta">
              <span>{approval.service}</span>
              <span>{formatDate(approval.requested_at)}</span>
            </div>
          </div>
          <div className="approvalActions">
            <Link className="iconTextButton" to={`/approvals/${approval.approval_id}`}>
              <ClipboardCheck size={16} />
              Review
            </Link>
          </div>
        </article>
      ))}
    </section>
  );
}

function ApprovalsPage() {
  const { approvalId } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const [selected, setSelected] = useState<ApprovalItem | null>(null);
  const [checkedIds, setCheckedIds] = useState<Set<string>>(new Set());
  const status = searchParams.get('status') ?? 'waiting';
  const directApprovalId = approvalId ?? searchParams.get('approval_id') ?? undefined;
  const queryClient = useQueryClient();
  const query = useQuery<PaginatedResponse<ApprovalItem>, ApiError>({
    queryKey: ['approvals', status],
    queryFn: () => listApprovals({ status, page_size: 50 }),
    refetchInterval: status === 'waiting' ? 5000 : false
  });
  const directQuery = useQuery<ApprovalItem, ApiError>({
    queryKey: ['approval', directApprovalId],
    queryFn: () => getApproval(directApprovalId ?? ''),
    enabled: Boolean(directApprovalId)
  });

  const batchMutation = useMutation({
    mutationFn: (payload: BatchApprovalPayload) => batchDecideApprovals(payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['approvals'] });
      setCheckedIds(new Set());
    }
  });

  useEffect(() => {
    if (directQuery.data) {
      setSelected(directQuery.data);
    }
  }, [directQuery.data]);

  function setStatus(value: string) {
    const next = new URLSearchParams();
    next.set('status', value);
    setSearchParams(next);
    setCheckedIds(new Set());
  }

  function toggleCheck(approvalId: string) {
    setCheckedIds((prev) => {
      const next = new Set(prev);
      if (next.has(approvalId)) next.delete(approvalId);
      else next.add(approvalId);
      return next;
    });
  }

  function batchDecide(decision: 'approve' | 'reject') {
    if (checkedIds.size === 0) return;
    batchMutation.mutate({
      decision,
      approver: 'sre-batch',
      comment: decision === 'approve' ? 'Batch approved' : 'Batch rejected',
      approval_ids: Array.from(checkedIds)
    });
  }

  const waitingItems = query.data?.items.filter((a) => a.approval_status === 'waiting') ?? [];

  return (
    <>
      <PageHeader
        eyebrow="Approvals"
        title="Action approval queue"
        actions={
          <>
            <button className="iconTextButton" type="button" onClick={() => void query.refetch()}>
              <RefreshCw size={17} />
              Refresh
            </button>
            <ApprovalNotificationControl approvals={waitingItems} />
          </>
        }
      />

      <div className="segmented" role="tablist" aria-label="Approval status">
        {['waiting', 'approved', 'rejected', 'expired'].map((item) => (
          <button className={item === status ? 'segment active' : 'segment'} key={item} type="button" onClick={() => setStatus(item)}>
            {humanize(item)}
          </button>
        ))}
      </div>

      {directQuery.isLoading ? <LoadingPage title="Loading approval" /> : null}
      {directQuery.isError ? <ErrorState title="Unable to load linked approval" error={directQuery.error} onRetry={() => void directQuery.refetch()} /> : null}

      {status === 'waiting' && waitingItems.length > 0 && checkedIds.size > 0 ? (
        <div className="batchBar">
          <span>{checkedIds.size} selected</span>
          <button className="iconTextButton success" type="button" onClick={() => batchDecide('approve')} disabled={batchMutation.isPending}>
            <Check size={16} />
            Batch approve
          </button>
          <button className="iconTextButton danger" type="button" onClick={() => batchDecide('reject')} disabled={batchMutation.isPending}>
            <X size={16} />
            Batch reject
          </button>
        </div>
      ) : null}

      <section className="approvalList" aria-label="Approvals">
        {query.isLoading ? <LoadingRows label="Loading approvals" count={3} /> : null}
        {query.isError ? <ErrorState title="Unable to load approvals" error={query.error} onRetry={() => void query.refetch()} /> : null}
        {!query.isLoading && !query.isError && query.data?.items.length === 0 ? (
          <EmptyState title="No approvals" detail="No approval records match this status." />
        ) : null}
        {query.data?.items.map((approval) => (
          <article className={selected?.approval_id === approval.approval_id ? 'approvalItem selected' : 'approvalItem'} key={approval.approval_id}>
            {approval.approval_status === 'waiting' ? (
              <input type="checkbox" className="batchCheck" checked={checkedIds.has(approval.approval_id)} onChange={() => toggleCheck(approval.approval_id)} />
            ) : null}
            <div>
              <div className="approvalTitle">
                <strong>{approval.action_type}</strong>
                <RiskBadge value={approval.risk_level} />
                <StatusBadge value={approval.approval_status} />
              </div>
              <p>{approval.reason}</p>
              <div className="inlineMeta">
                <Link to={`/incidents/${approval.incident_id}`}>{approval.service}</Link>
                <span>{formatDate(approval.requested_at)}</span>
                <span>{approval.rollback_plan ?? 'No rollback plan'}</span>
              </div>
            </div>
            {approval.approval_status === 'waiting' ? (
              <button className="iconTextButton primary" type="button" onClick={() => setSelected(approval)}>
                <ClipboardCheck size={17} />
                Review
              </button>
            ) : null}
          </article>
        ))}
      </section>

      {selected ? <ApprovalDialog approval={selected} onClose={() => setSelected(null)} /> : null}
    </>
  );
}

function ApprovalNotificationControl({ approvals }: { approvals: ApprovalItem[] }) {
  const notificationSupported = typeof window !== 'undefined' && 'Notification' in window;
  const [permission, setPermission] = useState<NotificationPermission | 'unsupported'>(
    notificationSupported ? Notification.permission : 'unsupported'
  );
  const initializedRef = useRef(false);
  const notifiedRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    const ids = approvals.map((approval) => approval.approval_id);
    if (!initializedRef.current) {
      ids.forEach((id) => notifiedRef.current.add(id));
      initializedRef.current = true;
      return;
    }
    if (permission !== 'granted') {
      ids.forEach((id) => notifiedRef.current.add(id));
      return;
    }
    approvals.forEach((approval) => {
      if (notifiedRef.current.has(approval.approval_id)) return;
      notifiedRef.current.add(approval.approval_id);
      void showApprovalNotification(approval);
    });
  }, [approvals, permission]);

  async function enableNotifications() {
    if (!notificationSupported) return;
    const next = await Notification.requestPermission();
    setPermission(next);
    if (next === 'granted' && 'serviceWorker' in navigator) {
      await navigator.serviceWorker.register('/sw.js').catch(() => undefined);
    }
  }

  if (permission === 'unsupported') {
    return (
      <button className="iconTextButton" type="button" disabled>
        <BellOff size={17} />
        Notifications unavailable
      </button>
    );
  }

  if (permission === 'granted') {
    return (
      <button className="iconTextButton success" type="button" disabled>
        <Bell size={17} />
        Notifications on
      </button>
    );
  }

  if (permission === 'denied') {
    return (
      <button className="iconTextButton" type="button" disabled>
        <BellOff size={17} />
        Notifications blocked
      </button>
    );
  }

  return (
    <button className="iconTextButton" type="button" onClick={() => void enableNotifications()}>
      <Bell size={17} />
      Enable notifications
    </button>
  );
}

async function showApprovalNotification(approval: ApprovalItem): Promise<void> {
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  const title = `${approval.risk_level} approval requested`;
  const options: NotificationOptions = {
    body: `${approval.service}: ${approval.action_type}`,
    data: { url: `/approvals/${approval.approval_id}` },
    icon: '/icon.svg',
    tag: approval.approval_id
  };
  if ('serviceWorker' in navigator) {
    const registration = await navigator.serviceWorker.getRegistration().catch(() => undefined);
    if (registration) {
      await registration.showNotification(title, options);
      return;
    }
  }
  new Notification(title, options);
}


function ApprovalDialog({ approval, onClose }: { approval: ApprovalItem; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [decision, setDecision] = useState<'approve' | 'reject'>('approve');
  const [formError, setFormError] = useState<string | null>(null);
  const actionQuery = useQuery<ActionDetail, ApiError>({
    queryKey: ['action', approval.action_id],
    queryFn: () => getAction(approval.action_id)
  });
  const mutation = useMutation({
    mutationFn: ({ payload, mode }: { payload: ApprovalDecisionPayload; mode: 'approve' | 'reject' }) => (
      mode === 'approve' ? approveApproval(approval.approval_id, payload) : rejectApproval(approval.approval_id, payload)
    ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['approvals'] });
      void queryClient.invalidateQueries({ queryKey: ['incident', approval.incident_id] });
      void queryClient.invalidateQueries({ queryKey: ['incident-approvals', approval.incident_id] });
      void queryClient.invalidateQueries({ queryKey: ['agent-run', approval.agent_run_id] });
      onClose();
    }
  });

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError(null);
    const data = new FormData(event.currentTarget);
    const approver = String(data.get('approver') ?? '').trim();
    const comment = String(data.get('comment') ?? '').trim();
    if (!approver) {
      setFormError('Approver is required');
      return;
    }
    if (decision === 'reject' && !comment) {
      setFormError('Rejection reason is required');
      return;
    }

    const payload: ApprovalDecisionPayload = { approver, comment: comment || null };
    if (decision === 'approve' && approval.risk_level === 'L3') {
      payload.risk_ack = data.get('risk_ack') === 'on';
      payload.confirm_action_type = String(data.get('confirm_action_type') ?? '').trim();
      payload.confirm_target = String(data.get('confirm_target') ?? '').trim();
    }
    mutation.mutate({ payload, mode: decision });
  }

  const action = actionQuery.data;

  return (
    <div className="dialogBackdrop" role="presentation">
      <div className="dialog" role="dialog" aria-modal="true" aria-labelledby="approval-title">
        <div className="dialogHeader">
          <div>
            <h2 id="approval-title">Review action</h2>
            <p>{approval.approval_id}</p>
          </div>
          <button className="iconButton" type="button" aria-label="Close approval dialog" onClick={onClose}>
            <X size={18} />
          </button>
        </div>

        <div className="approvalFacts">
          <Metric label="Risk" value={approval.risk_level} />
          <Metric label="Action" value={approval.action_type} />
          <Metric label="Target" value={action?.target ?? 'loading'} />
        </div>
        <p className="dialogReason">{approval.reason}</p>
        {approval.rollback_plan ? <p className="rollbackText">Rollback: {approval.rollback_plan}</p> : null}
        {actionQuery.isError ? <ErrorState title="Unable to load action" error={actionQuery.error} /> : null}

        <div className="segmented compact" role="tablist" aria-label="Approval decision">
          <button className={decision === 'approve' ? 'segment active' : 'segment'} type="button" onClick={() => setDecision('approve')}>
            Approve
          </button>
          <button className={decision === 'reject' ? 'segment active' : 'segment'} type="button" onClick={() => setDecision('reject')}>
            Reject
          </button>
        </div>

        <form className="decisionForm" onSubmit={onSubmit}>
          <label>
            <span>Approver</span>
            <input name="approver" autoFocus placeholder="sre-oncall" />
          </label>
          <label>
            <span>{decision === 'reject' ? 'Rejection reason' : 'Comment'}</span>
            <textarea name="comment" rows={3} />
          </label>

          {decision === 'approve' && approval.risk_level === 'L3' ? (
            <div className="l3Confirm">
              <label className="checkboxRow">
                <input name="risk_ack" type="checkbox" />
                <span>Risk acknowledged</span>
              </label>
              <label>
                <span>Confirm action type</span>
                <input name="confirm_action_type" placeholder={action?.type ?? approval.action_type} />
              </label>
              <label>
                <span>Confirm target</span>
                <input name="confirm_target" placeholder={action?.target ?? ''} />
              </label>
            </div>
          ) : null}

          {formError ? <div className="formError">{formError}</div> : null}
          {mutation.isError ? <ErrorState title="Decision failed" error={mutation.error as ApiError} /> : null}

          <div className="dialogActions">
            <button className="iconTextButton" type="button" onClick={onClose}>Cancel</button>
            <button className={decision === 'approve' ? 'iconTextButton success' : 'iconTextButton danger'} type="submit" disabled={mutation.isPending}>
              {decision === 'approve' ? <Check size={17} /> : <X size={17} />}
              {decision === 'approve' ? 'Approve' : 'Reject'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function ReportPage() {
  const incidentId = useRequiredParam('incidentId');
  const queryClient = useQueryClient();
  const query = useQuery<IncidentReport, ApiError>({
    queryKey: ['incident-report', incidentId],
    queryFn: () => getIncidentReport(incidentId),
    retry: false
  });
  const regenerate = useMutation<IncidentReport, ApiError>({
    mutationFn: () => regenerateIncidentReport(incidentId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['incident-report', incidentId] });
    }
  });

  return (
    <>
      <BackLink to={`/incidents/${incidentId}`}>Incident</BackLink>
      <PageHeader
        eyebrow="Report"
        title="Post-incident report"
        actions={
          <button className="iconTextButton primary" type="button" onClick={() => regenerate.mutate()} disabled={regenerate.isPending}>
            <RotateCw size={17} />
            {query.data ? 'Regenerate' : 'Generate'}
          </button>
        }
      />

      {query.isLoading ? <LoadingPage title="Loading report" /> : null}
      {query.isError && query.error.status !== 404 ? <ErrorState title="Unable to load report" error={query.error} onRetry={() => void query.refetch()} /> : null}
      {query.isError && query.error.status === 404 ? <EmptyState title="No report available" detail="No report version exists for this incident yet." /> : null}
      {regenerate.isError ? <ErrorState title="Unable to regenerate report" error={regenerate.error} /> : null}
      {query.data ? <ReportView report={query.data} /> : null}
    </>
  );
}

function ReportView({ report }: { report: IncidentReport }) {
  return (
    <div className="reportLayout">
      <div className="metricStrip">
        <Metric label="Version" value={`v${report.version}`} />
        <Metric label="Run" value={report.agent_run_id} />
        <Metric label="Evidence refs" value={String(report.evidence_ids.length)} />
        <Metric label="Created" value={formatDate(report.created_at)} />
      </div>

      <section className="sectionBlock wide">
        <SectionTitle icon={<Gauge size={18} />} title="Root cause" />
        <p className="reportLead">{report.root_cause}</p>
      </section>
      <section className="sectionBlock wide">
        <SectionTitle icon={<AlertTriangle size={18} />} title="Impact" />
        <p>{report.impact}</p>
      </section>
      <section className="sectionBlock wide">
        <SectionTitle icon={<Clock size={18} />} title="Timeline" />
        {report.timeline.length === 0 ? <EmptyState title="No timeline" detail="The report does not include timeline entries." /> : (
          <ol className="timelineList">
            {report.timeline.map((item, index) => <li key={index}>{formatTimelineItem(item)}</li>)}
          </ol>
        )}
      </section>
      <section className="sectionBlock wide">
        <SectionTitle icon={<ShieldAlert size={18} />} title="Actions" />
        {report.actions.length === 0 ? <EmptyState title="No actions" detail="The report does not include action entries." /> : (
          <div className="compactList">
            {report.actions.map((item, index) => <KeyValueRecord record={item} key={index} />)}
          </div>
        )}
      </section>
      <section className="sectionBlock wide">
        <SectionTitle icon={<ListChecks size={18} />} title="Follow-ups" />
        {report.follow_ups.length === 0 ? <EmptyState title="No follow-ups" detail="The report does not include follow-up items." /> : (
          <ul className="plainList">
            {report.follow_ups.map((item, index) => <li key={index}>{typeof item === 'string' ? item : formatRecord(item)}</li>)}
          </ul>
        )}
      </section>
      <section className="sectionBlock wide">
        <SectionTitle icon={<FileText size={18} />} title="Evidence references" />
        {report.evidence_ids.length === 0 ? <EmptyState title="No evidence references" detail="No evidence IDs were attached to this report." /> : (
          <div className="chipRow">{report.evidence_ids.map((id) => <span className="chip" key={id}>{id}</span>)}</div>
        )}
      </section>
    </div>
  );
}

function AlertSummary({ alert }: { alert: Record<string, unknown> }) {
  const labels = recordEntries(alert.labels);
  const annotations = recordEntries(alert.annotations);
  return (
    <div className="alertSummary">
      <KeyValue label="Fingerprint" value={stringValue(alert.fingerprint)} />
      <KeyValue label="Source" value={stringValue(alert.source)} />
      <KeyValue label="Started" value={formatDate(stringValue(alert.starts_at))} />
      <KeyValue label="Labels" value={labels.length ? labels.map(([key, value]) => `${key}=${value}`).join(', ') : 'none'} />
      <KeyValue label="Annotations" value={annotations.length ? annotations.map(([key, value]) => `${key}=${value}`).join(', ') : 'none'} />
      <details>
        <summary>Raw alert</summary>
        <pre>{JSON.stringify(alert, null, 2)}</pre>
      </details>
    </div>
  );
}

function EvidenceList({ items }: { items: EvidenceItem[] }) {
  if (items.length === 0) {
    return <EmptyState title="No evidence" detail="No evidence has been persisted for this incident." />;
  }
  return (
    <div className="evidenceList">
      {items.map((item) => (
        <article className="evidenceItem" key={item.evidence_id}>
          <div className="itemHeader">
            <strong>{item.title}</strong>
            <span className="chip">{item.evidence_id}</span>
          </div>
          <p>{item.excerpt}</p>
          <div className="inlineMeta">
            <span>{item.type}</span>
            <span>{item.source}</span>
            <span>{formatPercent(item.confidence)}</span>
            <span>{formatDate(item.timestamp)}</span>
          </div>
        </article>
      ))}
    </div>
  );
}

function ActionList({ actions }: { actions: ActionSummary[] }) {
  if (actions.length === 0) {
    return <EmptyState title="No actions" detail="No remediation actions have been proposed." />;
  }
  return (
    <div className="compactList">
      {actions.map((action) => (
        <article className="actionItem" key={action.action_id}>
          <div className="itemHeader">
            <strong>{action.type}</strong>
            <RiskBadge value={action.risk_level} />
            <StatusBadge value={action.status} />
          </div>
          <p>{action.reason}</p>
          {action.rollback_plan ? <p className="rollbackText">Rollback: {action.rollback_plan}</p> : null}
        </article>
      ))}
    </div>
  );
}

function ApprovalSummary({ approvals, loading }: { approvals: ApprovalItem[]; loading: boolean }) {
  if (loading) {
    return <LoadingRows label="Loading incident approvals" count={2} />;
  }
  if (approvals.length === 0) {
    return <EmptyState title="No approvals" detail="No approvals are attached to this incident." />;
  }
  return (
    <div className="compactList">
      {approvals.map((approval) => (
        <article className="actionItem" key={approval.approval_id}>
          <div className="itemHeader">
            <strong>{approval.action_type}</strong>
            <RiskBadge value={approval.risk_level} />
            <StatusBadge value={approval.approval_status} />
          </div>
          <p>{approval.reason}</p>
          <div className="inlineMeta">
            <span>{approval.approver ?? 'unassigned'}</span>
            <Link to={`/approvals/${approval.approval_id}`}>Review</Link>
          </div>
        </article>
      ))}
    </div>
  );
}

type RunNodeTrace = AgentRunDetail['nodes'][number];
type RunTimelineEntry = RunNodeTrace & { synthetic?: boolean };
type RunProgressModel = {
  entries: RunTimelineEntry[];
  total: number;
  completed: number;
  percent: number;
  currentNode: string | null;
};

function getRunProgress(run: AgentRunDetail): RunProgressModel {
  const expectedNames = getExpectedNodeNames(run);
  const latestByName = new Map<string, RunNodeTrace>();
  run.nodes.forEach((node) => latestByName.set(node.name, node));

  const orderedNames = expectedNames.length > 0 ? expectedNames : Array.from(latestByName.keys());
  const seenNames = new Set<string>();
  const entries: RunTimelineEntry[] = orderedNames.map((name) => {
    seenNames.add(name);
    return latestByName.get(name) ?? {
      name,
      status: 'pending',
      started_at: null,
      finished_at: null,
      duration_ms: null,
      input_summary: null,
      output_summary: null,
      tool_calls: [],
      synthetic: true
    };
  });

  run.nodes.forEach((node) => {
    if (!seenNames.has(node.name)) {
      entries.push(node);
    }
  });

  const completed = entries.filter((node) => isTerminalNodeStatus(node.status)).length;
  const current = entries.find((node) => isRunningNodeStatus(node.status))
    ?? entries.find((node) => node.status === 'pending')
    ?? entries[entries.length - 1]
    ?? null;
  const total = entries.length;
  const percent = total > 0 ? Math.round((completed / total) * 100) : 0;
  return { entries, total, completed, percent, currentNode: current?.name ?? null };
}

function getExpectedNodeNames(run: AgentRunDetail): string[] {
  const state = run.state;
  const graph = asRecord(state.graph);
  const workflow = asRecord(state.workflow);
  const candidates = [
    state.graph_node_order,
    state.graph_nodes,
    state.expected_nodes,
    state.expected_graph_nodes,
    state.node_order,
    graph?.node_order,
    graph?.nodes,
    workflow?.node_order,
    workflow?.nodes
  ];
  for (const candidate of candidates) {
    const names = readNodeNameArray(candidate);
    if (names.length > 0) {
      return names;
    }
  }
  return [];
}

function readNodeNameArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => {
      if (typeof item === 'string') return item;
      const record = asRecord(item);
      return record ? stringValue(record.name) || stringValue(record.node_name) || stringValue(record.id) : '';
    })
    .filter((item) => item.length > 0);
}

function isTerminalNodeStatus(status: string): boolean {
  return ['succeeded', 'success', 'completed', 'failed', 'skipped', 'blocked'].includes(status.toLowerCase());
}

function isRunningNodeStatus(status: string): boolean {
  return ['running', 'executing', 'in_progress', 'started'].includes(status.toLowerCase());
}

function RunProgress({ progress, connectionState }: { progress: RunProgressModel; connectionState: WsConnectionState }) {
  return (
    <section className="runProgressPanel" aria-label="Run progress">
      <div className="progressHeader">
        <div>
          <strong>Run progress</strong>
          <span>{progress.completed} of {progress.total} nodes complete</span>
        </div>
        <span className={`connectionPill ${connectionState}`}>
          {connectionState === 'open' ? <Radio size={14} /> : <Clock size={14} />}
          {humanize(connectionState)}
        </span>
      </div>
      <div className="progressTrack" aria-label={`${progress.percent}% complete`}>
        <div className="progressFill" style={{ width: `${progress.percent}%` }} />
      </div>
      <div className="nodeRail" aria-label="Graph nodes">
        {progress.entries.map((node) => (
          <span className={`nodeDot ${nodeStatusClass(node.status)}`} title={`${node.name}: ${humanize(node.status)}`} key={node.name} />
        ))}
      </div>
    </section>
  );
}

function nodeStatusClass(status: string): string {
  const lowered = status.toLowerCase();
  if (isTerminalNodeStatus(lowered) && lowered !== 'failed') return 'done';
  if (lowered === 'failed' || lowered === 'blocked') return 'failed';
  if (isRunningNodeStatus(lowered)) return 'active';
  return 'pending';
}


function RunTimeline({ run, progress }: { run: AgentRunDetail; progress: RunProgressModel }) {
  const entries = progress.entries.length > 0 ? progress.entries : run.nodes;
  return (
    <ol className="runTimeline">
      {entries.map((node) => (
        <li key={`${node.name}-${node.started_at ?? node.finished_at ?? 'pending'}`}>
          <div className={`timelineMarker ${nodeStatusClass(node.status)}`}><TimelineMarkerIcon status={node.status} /></div>
          <div className="timelineContent">
            <div className="itemHeader">
              <strong>{node.name}</strong>
              <StatusBadge value={node.status} />
            </div>
            <div className="inlineMeta">
              <span>{formatDuration(node.duration_ms)}</span>
              <span>{formatDate(node.started_at)}</span>
            </div>
            {node.input_summary ? <p>Input: {node.input_summary}</p> : null}
            {node.output_summary ? <p>Output: {node.output_summary}</p> : null}
          </div>
        </li>
      ))}
    </ol>
  );
}

function TimelineMarkerIcon({ status }: { status: string }) {
  if (status.toLowerCase() === 'failed' || status.toLowerCase() === 'blocked') {
    return <XCircle size={16} />;
  }
  if (isRunningNodeStatus(status)) {
    return <Loader2 className="spin" size={16} />;
  }
  if (isTerminalNodeStatus(status)) {
    return <CheckCircle2 size={16} />;
  }
  return <Clock size={16} />;
}

function LiveNodeLog({ events, run }: { events: WsEvent[]; run: AgentRunDetail }) {
  const nodeEvents = events
    .filter((event) => event.type === 'node_update')
    .filter((event) => {
      const eventRunId = stringValue(event.payload.agent_run_id);
      return !eventRunId || eventRunId === run.agent_run_id;
    })
    .slice(0, 8);

  if (nodeEvents.length === 0) {
    const currentNode = getRunProgress(run).currentNode;
    const latestNode = currentNode ? run.nodes.find((node) => node.name === currentNode) : null;
    if (!latestNode) {
      return <EmptyState title="Waiting for node events" detail="No live node updates have been received for this run." />;
    }
    return (
      <div className="liveLogList">
        <article className="liveLogItem">
          <div className="itemHeader">
            <strong>{latestNode.name}</strong>
            <StatusBadge value={latestNode.status} />
          </div>
          <p>{latestNode.output_summary ?? latestNode.input_summary ?? 'Node trace is recorded without an intermediate summary.'}</p>
        </article>
      </div>
    );
  }

  return (
    <div className="liveLogList">
      {nodeEvents.map((event, index) => (
        <article className="liveLogItem" key={`${event.timestamp ?? 'event'}-${index}`}>
          <div className="itemHeader">
            <strong>{stringValue(event.payload.node_name) || 'node'}</strong>
            <StatusBadge value={stringValue(event.payload.status) || 'unknown'} />
          </div>
          <p>{stringValue(event.payload.output_summary) || stringValue(event.payload.input_summary) || formatRecord(event.payload)}</p>
          <div className="inlineMeta">
            <span>{formatDate(event.timestamp)}</span>
            <span>{humanize(event.type)}</span>
          </div>
        </article>
      ))}
    </div>
  );
}

function DiagnosisVisualizations({ run }: { run: AgentRunDetail }) {
  return (
    <div className="visualGrid">
      <SignalSwimlanes run={run} />
      <DependencyGraph run={run} />
      <EvidenceNetwork run={run} />
    </div>
  );
}

function SignalSwimlanes({ run }: { run: AgentRunDetail }) {
  const rows = buildSwimlaneRows(run);
  if (rows.length === 0) {
    return <VisualPanel title="Signal swimlanes" icon={<Activity size={17} />}><EmptyState title="No signal events" detail="No tool calls are available to align by data source." /></VisualPanel>;
  }

  return (
    <VisualPanel title="Signal swimlanes" icon={<Activity size={17} />}>
      <div className="swimlaneChart">
        {rows.map((row) => (
          <div className="swimlaneRow" key={row.source}>
            <span className="swimlaneLabel">{row.source}</span>
            <div className="swimlaneTrack">
              {row.events.map((event) => (
                <span
                  className={`swimlanePoint ${event.cacheHit ? 'cacheHit' : ''}`}
                  style={{ left: `${event.position}%` }}
                  title={`${event.label} - ${formatDate(event.timestamp)}`}
                  key={event.id}
                />
              ))}
            </div>
          </div>
        ))}
      </div>
    </VisualPanel>
  );
}

function DependencyGraph({ run }: { run: AgentRunDetail }) {
  const graph = buildDependencyGraph(run);
  return (
    <VisualPanel title="Dependency graph" icon={<Network size={17} />}>
      <svg className="dependencyGraph" viewBox="0 0 360 240" role="img" aria-label="Service dependency graph">
        {graph.edges.map((edge) => {
          const from = graph.nodes.find((node) => node.id === edge.from);
          const to = graph.nodes.find((node) => node.id === edge.to);
          if (!from || !to) return null;
          return <line className="graphEdge" x1={from.x} y1={from.y} x2={to.x} y2={to.y} key={`${edge.from}-${edge.to}`} />;
        })}
        {graph.nodes.map((node) => (
          <g className={node.anomaly ? 'graphNode anomaly' : 'graphNode'} key={node.id}>
            <circle cx={node.x} cy={node.y} r={node.primary ? 24 : 18} />
            <text x={node.x} y={node.y + 36}>{node.label}</text>
          </g>
        ))}
      </svg>
    </VisualPanel>
  );
}

function EvidenceNetwork({ run }: { run: AgentRunDetail }) {
  const network = buildEvidenceNetwork(run);
  return (
    <VisualPanel title="Evidence network" icon={<GitBranch size={17} />}>
      <div className="evidenceNetwork">
        <div className="networkColumn">
          <strong>Hypotheses</strong>
          {network.hypotheses.map((item) => (
            <span className="networkNode hypothesis" key={item.id}>{item.summary}</span>
          ))}
        </div>
        <div className="networkFlows" aria-hidden="true">
          {network.hypotheses.map((item) => (
            <span className="flowBar" style={{ width: `${Math.max(18, Math.round(item.confidence * 100))}%` }} key={item.id} />
          ))}
        </div>
        <div className="networkColumn">
          <strong>Evidence</strong>
          {network.evidence.map((item) => (
            <span className="networkNode evidence" key={item.id}>{item.label}</span>
          ))}
        </div>
      </div>
    </VisualPanel>
  );
}

function VisualPanel({ title, icon, children }: { title: string; icon: ReactNode; children: ReactNode }) {
  return (
    <article className="visualPanel">
      <h4>{icon}{title}</h4>
      {children}
    </article>
  );
}

type SwimlaneRow = { source: string; events: Array<{ id: string; label: string; timestamp: string; position: number; cacheHit: boolean }> };

function buildSwimlaneRows(run: AgentRunDetail): SwimlaneRow[] {
  if (run.tool_calls.length === 0) return [];
  const timestamps = run.tool_calls
    .map((call) => new Date(call.created_at).getTime())
    .filter((value) => Number.isFinite(value));
  const min = Math.min(...timestamps);
  const max = Math.max(...timestamps);
  const span = Math.max(1, max - min);
  const groups = new Map<string, SwimlaneRow>();

  run.tool_calls.forEach((call) => {
    const source = classifySignalSource(call.tool_name, call.node_name);
    const timestamp = new Date(call.created_at).getTime();
    const position = Number.isFinite(timestamp) ? Math.round(((timestamp - min) / span) * 100) : 0;
    const row = groups.get(source) ?? { source, events: [] };
    row.events.push({ id: call.tool_call_id, label: call.tool_name, timestamp: call.created_at, position, cacheHit: call.cache_hit });
    groups.set(source, row);
  });

  return Array.from(groups.values()).sort((a, b) => signalSourceWeight(a.source) - signalSourceWeight(b.source));
}

function classifySignalSource(toolName: string, nodeName: string): string {
  const value = `${toolName} ${nodeName}`.toLowerCase();
  if (value.includes('metric') || value.includes('prometheus')) return 'metrics';
  if (value.includes('log') || value.includes('loki')) return 'logs';
  if (value.includes('trace') || value.includes('otel')) return 'traces';
  if (value.includes('git') || value.includes('deploy')) return 'git';
  if (value.includes('runbook') || value.includes('rag')) return 'runbook';
  return 'agent';
}

function signalSourceWeight(source: string): number {
  return ['metrics', 'logs', 'traces', 'git', 'runbook', 'agent'].indexOf(source);
}

type DependencyNode = { id: string; label: string; x: number; y: number; anomaly: boolean; primary: boolean };
type DependencyEdge = { from: string; to: string };

function buildDependencyGraph(run: AgentRunDetail): { nodes: DependencyNode[]; edges: DependencyEdge[] } {
  const stateTopology = extractStateTopology(run.state);
  if (stateTopology.nodes.length > 0) return stateTopology;

  const service = extractRunService(run.state) || 'incident service';
  const sources = Array.from(new Set(buildSwimlaneRows(run).map((row) => row.source)));
  const labels = [service, ...sources.map((source) => humanize(source))];
  const center = { x: 180, y: 116 };
  const nodes = labels.map((label, index) => {
    if (index === 0) {
      return { id: slugId(label), label, x: center.x, y: center.y, anomaly: true, primary: true };
    }
    const angle = ((index - 1) / Math.max(1, labels.length - 1)) * Math.PI * 2 - Math.PI / 2;
    return {
      id: slugId(label),
      label,
      x: Math.round(center.x + Math.cos(angle) * 112),
      y: Math.round(center.y + Math.sin(angle) * 78),
      anomaly: false,
      primary: false
    };
  });
  const primary = nodes[0];
  return {
    nodes,
    edges: nodes.slice(1).map((node) => ({ from: node.id, to: primary.id }))
  };
}

function extractStateTopology(state: Record<string, unknown>): { nodes: DependencyNode[]; edges: DependencyEdge[] } {
  const topology = asRecord(state.service_topology) ?? asRecord(state.topology);
  const rawNodes = Array.isArray(topology?.nodes) ? topology.nodes : [];
  const rawEdges = Array.isArray(topology?.edges) ? topology.edges : [];
  const nodes = rawNodes
    .map((item, index) => {
      const record = asRecord(item);
      if (!record) return null;
      const id = stringValue(record.id) || stringValue(record.name) || `node_${index}`;
      const label = stringValue(record.label) || stringValue(record.name) || id;
      return {
        id,
        label,
        x: numberValue(record.x) ?? 70 + (index % 4) * 96,
        y: numberValue(record.y) ?? 64 + Math.floor(index / 4) * 84,
        anomaly: Boolean(record.anomaly || record.degraded || record.highlighted),
        primary: Boolean(record.primary)
      } satisfies DependencyNode;
    })
    .filter((node): node is DependencyNode => node !== null);
  const edges = rawEdges
    .map((item) => {
      const record = asRecord(item);
      if (!record) return null;
      const from = stringValue(record.from) || stringValue(record.source);
      const to = stringValue(record.to) || stringValue(record.target);
      return from && to ? { from, to } : null;
    })
    .filter((edge): edge is DependencyEdge => edge !== null);
  return { nodes, edges };
}

type EvidenceNetworkModel = {
  hypotheses: Array<{ id: string; summary: string; confidence: number }>;
  evidence: Array<{ id: string; label: string }>;
};

function buildEvidenceNetwork(run: AgentRunDetail): EvidenceNetworkModel {
  const diagnosis = asRecord(run.state.diagnosis) ?? asRecord(run.state.root_cause);
  const rawHypotheses = asRecordArray(run.state.ranked_hypotheses).length > 0
    ? asRecordArray(run.state.ranked_hypotheses)
    : asRecordArray(run.state.hypotheses);
  const hypotheses = rawHypotheses.map((item, index) => ({
    id: stringValue(item.id) || stringValue(item.hypothesis_id) || `hyp_${index + 1}`,
    summary: stringValue(item.summary) || stringValue(item.root_cause) || stringValue(item.title) || `Hypothesis ${index + 1}`,
    confidence: boundedConfidence(numberValue(item.confidence))
  }));

  if (hypotheses.length === 0 && diagnosis) {
    hypotheses.push({
      id: 'root_cause',
      summary: stringValue(diagnosis.summary) || stringValue(diagnosis.root_cause) || 'Root cause pending',
      confidence: boundedConfidence(numberValue(diagnosis.confidence))
    });
  }
  if (hypotheses.length === 0) {
    hypotheses.push({ id: 'agent_hypothesis', summary: 'Diagnosis pending', confidence: 0.2 });
  }

  const evidenceIds = readStringArray(run.state.evidence_ids)
    .concat(readStringArray(diagnosis?.evidence_ids))
    .concat(run.tool_calls.map((call) => call.tool_call_id));
  const uniqueEvidence = Array.from(new Set(evidenceIds)).slice(0, 8);
  const evidence = uniqueEvidence.length > 0
    ? uniqueEvidence.map((id) => ({ id, label: id }))
    : [{ id: 'evidence_pending', label: 'Evidence pending' }];
  return { hypotheses: hypotheses.slice(0, 5), evidence };
}

function readStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => stringValue(item)).filter((item) => item.length > 0);
}

function extractRunService(state: Record<string, unknown>): string {
  const alert = asRecord(state.alert);
  const labels = asRecord(alert?.labels) ?? asRecord(state.labels);
  return stringValue(state.service)
    || stringValue(alert?.service)
    || stringValue(labels?.service)
    || stringValue(labels?.job);
}

function slugId(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '') || 'node';
}

function numberValue(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function boundedConfidence(value: number | null): number {
  if (value === null) return 0.5;
  return Math.max(0.05, Math.min(1, value));
}


function ToolCallList({ run }: { run: AgentRunDetail }) {
  return (
    <div className="toolCallList">
      {run.tool_calls.map((call) => (
        <article className="toolCall" key={call.tool_call_id}>
          <div className="itemHeader">
            <strong>{call.tool_name}</strong>
            <StatusBadge value={call.status} />
            {call.cache_hit ? <span className="chip successChip">cache hit</span> : <span className="chip">cache miss</span>}
          </div>
          <p>{call.output_summary ?? call.input_summary}</p>
          <div className="inlineMeta">
            <span>{call.node_name}</span>
            <span>{formatDuration(call.duration_ms)}</span>
            <span>{formatDate(call.created_at)}</span>
          </div>
          {call.error_message ? <div className="formError">{call.error_message}</div> : null}
        </article>
      ))}
    </div>
  );
}

function ContextSummary({ state, compressionEvents }: { state: Record<string, unknown>; compressionEvents: Array<Record<string, unknown>> }) {
  const tokenUsage = asRecord(state.token_usage);
  const contextBudget = asRecord(state.context_budget);
  return (
    <div className="contextGrid">
      <Metric label="Prompt tokens" value={stringValue(tokenUsage?.prompt_tokens) || stringValue(contextBudget?.prompt_tokens) || 'unknown'} />
      <Metric label="Completion tokens" value={stringValue(tokenUsage?.completion_tokens) || 'unknown'} />
      <Metric label="Budget" value={stringValue(contextBudget?.max_tokens) || 'unknown'} />
      <Metric label="Compression events" value={String(compressionEvents.length)} />
      {compressionEvents.length > 0 ? (
        <div className="compactList spanAll">
          {compressionEvents.map((event, index) => <KeyValueRecord record={event} key={index} />)}
        </div>
      ) : null}
    </div>
  );
}

function PageHeader({ eyebrow, title, meta, actions }: { eyebrow: string; title: string; meta?: ReactNode; actions?: ReactNode }) {
  return (
    <header className="pageHeader">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h2>{title}</h2>
        {meta ? <div className="headerMeta">{meta}</div> : null}
      </div>
      {actions ? <div className="headerActions">{actions}</div> : null}
    </header>
  );
}

function SectionTitle({ icon, title }: { icon: ReactNode; title: string }) {
  return <h3 className="sectionTitle">{icon}{title}</h3>;
}

function BackLink({ to, children }: { to: string; children: ReactNode }) {
  return <Link className="backLink" to={to}><ChevronLeft size={17} />{children}</Link>;
}

function LoadingPage({ title }: { title: string }) {
  return (
    <div className="pageLoading">
      <Loader2 className="spin" size={22} />
      <span>{title}</span>
    </div>
  );
}

function LoadingRows({ label, count }: { label: string; count: number }) {
  return (
    <div className="loadingRows" aria-label={label}>
      {Array.from({ length: count }).map((_, index) => <div className="skeletonRow" key={index} />)}
    </div>
  );
}

function ErrorState({ title, error, onRetry }: { title: string; error?: Error | ApiError | null; onRetry?: () => void }) {
  const apiError = error instanceof ApiError ? error : null;
  return (
    <div className="stateBlock errorState">
      <CircleAlertIcon />
      <div>
        <strong>{title}</strong>
        <p>{error?.message ?? 'Unknown error'}</p>
        {apiError?.code ? <small>Code {apiError.code}</small> : null}
        {apiError?.status === 401 ? <small>Set or generate an API key in the sidebar Authentication panel.</small> : null}
        {apiError?.requestId ? <small>Request {apiError.requestId}</small> : null}
      </div>
      {onRetry ? <button className="iconTextButton" type="button" onClick={onRetry}><RefreshCw size={16} />Retry</button> : null}
    </div>
  );
}

function EmptyState({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="stateBlock emptyState">
      <FileText size={19} />
      <div>
        <strong>{title}</strong>
        <p>{detail}</p>
      </div>
    </div>
  );
}

function CircleAlertIcon() {
  return <AlertTriangle size={19} />;
}

function StatusBadge({ value }: { value: string }) {
  return <span className={`badge status ${badgeTone(value)}`}>{humanize(value)}</span>;
}

function SeverityBadge({ value }: { value: string }) {
  return <span className={`badge severity ${value.toLowerCase()}`}>{value}</span>;
}

function RiskBadge({ value }: { value: string }) {
  return <span className={`badge risk ${value.toLowerCase()}`}>{value}</span>;
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function KeyValue({ label, value }: { label: string; value: string }) {
  return (
    <div className="keyValue">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function KeyValueRecord({ record }: { record: Record<string, unknown> }) {
  return (
    <dl className="recordBox">
      {Object.entries(record).map(([key, value]) => (
        <div className="keyValue" key={key}>
          <dt>{humanize(key)}</dt>
          <dd>{displayValue(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

// ---------------------------------------------------------------------------
// Phase 6: Comment Section
// ---------------------------------------------------------------------------

function CommentSection({ incidentId }: { incidentId: string }) {
  const queryClient = useQueryClient();
  const commentsQuery = useQuery({
    queryKey: ['incident-comments', incidentId],
    queryFn: () => listIncidentComments(incidentId),
    refetchInterval: 15000
  });
  const createMutation = useMutation({
    mutationFn: (payload: { author: string; content: string }) => createComment(incidentId, payload),
    onSuccess: () => { void queryClient.invalidateQueries({ queryKey: ['incident-comments', incidentId] }); }
  });

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const author = String(data.get('author') ?? '').trim();
    const content = String(data.get('content') ?? '').trim();
    if (!author || !content) return;
    createMutation.mutate({ author, content });
    event.currentTarget.reset();
  }

  const comments = commentsQuery.data?.items ?? [];
  const isLoading = commentsQuery.isLoading;
  const error = commentsQuery.error;

  return (
    <div className="commentSection">
      {isLoading ? <LoadingRows label="Loading comments" count={1} /> : null}
      {error ? <ErrorState title="Unable to load comments" error={error} onRetry={() => void commentsQuery.refetch()} /> : null}
      {!isLoading && comments.length === 0 ? <EmptyState title="No comments" detail="No one has commented on this incident yet." /> : null}
      {comments.map((comment) => (
        <article className="commentItem" key={comment.comment_id}>
          <div className="itemHeader">
            <strong>{comment.author}</strong>
            <span className="mutedCell">{formatDate(comment.created_at)}</span>
          </div>
          <p>{comment.content}</p>
          {comment.mentioned_users.length > 0 ? (
            <div className="inlineMeta">
              <small>Mentioned: {comment.mentioned_users.join(', ')}</small>
            </div>
          ) : null}
        </article>
      ))}

      <form className="commentForm" onSubmit={onSubmit}>
        <label>
          <span>Name</span>
          <input name="author" placeholder="Your name" required />
        </label>
        <label>
          <span>Comment</span>
          <textarea name="content" rows={2} placeholder="Add a comment... use @handle to mention" required />
        </label>
        <button className="iconTextButton primary" type="submit" disabled={createMutation.isPending}>
          <MessageSquare size={16} />
          {createMutation.isPending ? 'Posting...' : 'Post comment'}
        </button>
      </form>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Phase 6: Audit Section
// ---------------------------------------------------------------------------

function AuditSection({ incidentId }: { incidentId: string }) {
  const query = useQuery({
    queryKey: ['incident-audit', incidentId],
    queryFn: () => listIncidentAudit(incidentId),
    staleTime: 30000
  });

  if (query.isLoading) {
    return <LoadingRows label="Loading audit trail" count={2} />;
  }
  if (query.isError) {
    return <ErrorState title="Unable to load audit trail" error={query.error} onRetry={() => void query.refetch()} />;
  }

  const items = query.data?.items ?? [];
  if (items.length === 0) {
    return <EmptyState title="No audit entries" detail="No operations have been recorded for this incident." />;
  }

  return (
    <div className="compactList">
      {items.map((entry) => (
        <article className="actionItem" key={entry.audit_id}>
          <div className="itemHeader">
            <strong>{entry.actor}</strong>
            <span className="chip">{humanize(entry.action)}</span>
            <span className="mutedCell">{formatDate(entry.created_at)}</span>
          </div>
          <div className="inlineMeta">
            <span>{entry.resource_type}: {entry.resource_id.slice(0, 20)}...</span>
          </div>
        </article>
      ))}
    </div>
  );
}

function NotFoundPage() {
  return (
    <>
      <PageHeader eyebrow="404" title="Page not found" />
      <EmptyState title="Unknown route" detail="The requested console page does not exist." />
    </>
  );
}

function useRequiredParam(name: string): string {
  const params = useParams();
  const value = params[name];
  if (!value) {
    throw new Error(`Missing route param ${name}`);
  }
  return value;
}

function formatDate(value: string | null | undefined): string {
  if (!value) {
    return 'n/a';
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat('en-US', { dateStyle: 'medium', timeStyle: 'short' }).format(date);
}

function formatDuration(value: number | null): string {
  if (value === null) {
    return 'pending';
  }
  if (value < 1000) {
    return `${value} ms`;
  }
  return `${(value / 1000).toFixed(1)} s`;
}

function formatPercent(value: number | null): string {
  if (value === null) {
    return 'unknown';
  }
  return `${Math.round(value * 100)}%`;
}

function humanize(value: string): string {
  return value.replace(/_/g, ' ').replace(/\b\w/g, (match) => match.toUpperCase());
}

function badgeTone(value: string): string {
  if (['failed', 'rejected', 'blocked', 'expired'].includes(value)) {
    return 'dangerTone';
  }
  if (['waiting', 'waiting_approval', 'diagnosing', 'running', 'executing', 'queued'].includes(value)) {
    return 'warningTone';
  }
  if (['succeeded', 'approved', 'resolved', 'mitigated'].includes(value)) {
    return 'successTone';
  }
  return 'neutralTone';
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function asRecordArray(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value) ? value.filter((item): item is Record<string, unknown> => asRecord(item) !== null) : [];
}

function recordEntries(value: unknown): Array<[string, string]> {
  const record = asRecord(value);
  if (!record) {
    return [];
  }
  return Object.entries(record).map(([key, item]) => [key, displayValue(item)]);
}

function stringValue(value: unknown): string {
  if (typeof value === 'string') {
    return value;
  }
  if (typeof value === 'number' || typeof value === 'boolean') {
    return String(value);
  }
  return '';
}

function displayValue(value: unknown): string {
  if (value === null || value === undefined) {
    return 'n/a';
  }
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    return String(value);
  }
  return JSON.stringify(value);
}

function formatRecord(record: Record<string, unknown>): string {
  return Object.entries(record).map(([key, value]) => `${humanize(key)}: ${displayValue(value)}`).join(', ');
}

function formatTimelineItem(item: Record<string, unknown>): string {
  const time = stringValue(item.time) || stringValue(item.timestamp);
  const event = stringValue(item.event) || stringValue(item.summary) || formatRecord(item);
  return time ? `${formatDate(time)} - ${event}` : event;
}
