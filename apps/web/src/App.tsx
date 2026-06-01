import {
  Activity,
  AlertTriangle,
  ArrowRight,
  Check,
  CheckCircle2,
  ChevronLeft,
  ClipboardCheck,
  Clock,
  FileText,
  Gauge,
  History,
  ListChecks,
  Loader2,
  RefreshCw,
  RotateCw,
  Search,
  ShieldAlert,
  X,
  XCircle
} from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type { FormEvent, ReactNode } from 'react';
import { useMemo, useState } from 'react';
import { Link, Navigate, NavLink, Route, Routes, useParams, useSearchParams } from 'react-router-dom';

import {
  ApiError,
  approveApproval,
  getAction,
  getAgentRun,
  getIncident,
  getIncidentReport,
  listApprovals,
  listIncidentApprovals,
  listIncidentRuns,
  listIncidents,
  regenerateIncidentReport,
  rejectApproval,
  type ActionDetail,
  type ActionSummary,
  type AgentRunDetail,
  type ApprovalDecisionPayload,
  type ApprovalItem,
  type EvidenceItem,
  type IncidentDetail,
  type IncidentListItem,
  type IncidentReport,
  type PaginatedResponse
} from './api';

const LIVE_STATUSES = new Set(['open', 'diagnosing', 'waiting_approval', 'queued', 'running', 'executing']);

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
      </aside>

      <main className="contentPane">
        <Routes>
          <Route path="/" element={<Navigate to="/incidents" replace />} />
          <Route path="/incidents" element={<IncidentsPage />} />
          <Route path="/incidents/:incidentId" element={<IncidentDetailPage />} />
          <Route path="/agent-runs/:agentRunId" element={<AgentRunPage />} />
          <Route path="/approvals" element={<ApprovalsPage />} />
          <Route path="/incidents/:incidentId/report" element={<ReportPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Routes>
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
          </>
        }
      />

      <div className="detailGrid">
        <section className="sectionBlock wide">
          <SectionTitle icon={<Gauge size={18} />} title="Diagnosis" />
          {incident.root_cause ? (
            <div className="diagnosisBox">
              <p>{incident.root_cause.summary}</p>
              <div className="inlineMeta">
                <span>Confidence {formatPercent(incident.root_cause.confidence)}</span>
                <span>Evidence {incident.root_cause.evidence_ids.length || incident.evidence.length}</span>
              </div>
            </div>
          ) : (
            <EmptyState title="Diagnosis pending" detail="The agent has not written a root cause yet." />
          )}
        </section>

        <section className="sectionBlock">
          <SectionTitle icon={<AlertTriangle size={18} />} title="Alert" />
          <AlertSummary alert={incident.alert} />
        </section>

        <section className="sectionBlock wide">
          <SectionTitle icon={<ListChecks size={18} />} title="Evidence" />
          <EvidenceList items={incident.evidence} />
        </section>

        <section className="sectionBlock wide">
          <SectionTitle icon={<ShieldAlert size={18} />} title="Recommended actions" />
          <ActionList actions={incident.recommended_actions} />
        </section>

        <section className="sectionBlock wide">
          <SectionTitle icon={<ClipboardCheck size={18} />} title="Approvals" />
          <ApprovalSummary approvals={approvals} loading={approvalsQuery.isLoading} />
        </section>
      </div>
    </>
  );
}

function AgentRunPage() {
  const agentRunId = useRequiredParam('agentRunId');
  const query = useQuery<AgentRunDetail, ApiError>({
    queryKey: ['agent-run', agentRunId],
    queryFn: () => getAgentRun(agentRunId),
    refetchInterval: (request) => {
      const data = request.state.data as AgentRunDetail | undefined;
      return data && LIVE_STATUSES.has(data.status) ? 5000 : false;
    }
  });

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
        <Metric label="Checkpoint" value={run.latest_checkpoint_id ?? run.checkpoint_thread_id ?? 'none'} />
        <Metric label="Task" value={run.celery_task_id ?? 'not queued'} />
        <Metric label="Tool calls" value={String(run.tool_calls.length)} />
        <Metric label="Compression" value={String(compressionEvents.length)} />
      </div>

      {run.error_message ? (
        <div className="callout danger"><XCircle size={18} />{run.error_code}: {run.error_message}</div>
      ) : null}

      <section className="sectionBlock wide">
        <SectionTitle icon={<History size={18} />} title="Timeline" />
        {run.nodes.length === 0 ? <EmptyState title="No nodes recorded" detail="The run has not emitted node trace events." /> : <RunTimeline run={run} />}
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

function ApprovalsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [selected, setSelected] = useState<ApprovalItem | null>(null);
  const status = searchParams.get('status') ?? 'waiting';
  const query = useQuery<PaginatedResponse<ApprovalItem>, ApiError>({
    queryKey: ['approvals', status],
    queryFn: () => listApprovals({ status, page_size: 50 }),
    refetchInterval: status === 'waiting' ? 5000 : false
  });

  function setStatus(value: string) {
    const next = new URLSearchParams();
    next.set('status', value);
    setSearchParams(next);
  }

  return (
    <>
      <PageHeader
        eyebrow="Approvals"
        title="Action approval queue"
        actions={
          <button className="iconTextButton" type="button" onClick={() => void query.refetch()}>
            <RefreshCw size={17} />
            Refresh
          </button>
        }
      />

      <div className="segmented" role="tablist" aria-label="Approval status">
        {['waiting', 'approved', 'rejected', 'expired'].map((item) => (
          <button className={item === status ? 'segment active' : 'segment'} key={item} type="button" onClick={() => setStatus(item)}>
            {humanize(item)}
          </button>
        ))}
      </div>

      <section className="approvalList" aria-label="Approvals">
        {query.isLoading ? <LoadingRows label="Loading approvals" count={3} /> : null}
        {query.isError ? <ErrorState title="Unable to load approvals" error={query.error} onRetry={() => void query.refetch()} /> : null}
        {!query.isLoading && !query.isError && query.data?.items.length === 0 ? (
          <EmptyState title="No approvals" detail="No approval records match this status." />
        ) : null}
        {query.data?.items.map((approval) => (
          <article className="approvalItem" key={approval.approval_id}>
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
            <Link to="/approvals">Review queue</Link>
          </div>
        </article>
      ))}
    </div>
  );
}

function RunTimeline({ run }: { run: AgentRunDetail }) {
  return (
    <ol className="runTimeline">
      {run.nodes.map((node) => (
        <li key={`${node.name}-${node.started_at ?? node.finished_at ?? 'pending'}`}>
          <div className="timelineMarker"><CheckCircle2 size={16} /></div>
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
