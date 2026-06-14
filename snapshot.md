# Live K8s Reversible and Bounded-Irreversible Actions Plan

Last updated: 2026-06-14

## Objective

Implement a stricter but practical live remediation safety model:

> Live mutating actions must be either reversible with rollback guarantees, or explicitly classified as bounded irreversible actions with stricter approval, preflight, snapshot, verify, and audit. Bounded irreversible actions do not claim restore guarantees.

Scope is intentionally narrow:

- K8s live mutations only, through the existing `LiveK8sExecutorBackend`.
- Two live K8s classes are allowed:
  - reversible actions with rollback contracts;
  - bounded irreversible actions such as `restart_pod` and `restart_service`.
- Database participation is read-only snapshot and verify only, through `DbDiagnosticsTool`.
- No database writes, no cache flushes, no cloud writes, no new Kubernetes mutation classes.
- Fixture executor remains the default path for tests, local demo, and CI.

This plan does not claim full system restore. It provides action-level rollback for reversible actions and bounded-risk execution for restart actions.

## Current Baseline

Relevant implementation points:

- Workflow routes executable actions through `take_snapshot -> execute_action -> verify`.
- `take_snapshot` writes `pre_action_snapshot` into LangGraph state.
- Snapshot currently records time, action types, evidence counts, and optional K8s deployment payload.
- `execute_action` delegates to `ExecutorBackend.execute()` or `ExecutorBackend.rollback()`.
- `LiveK8sExecutorBackend` currently supports `restart_pod`, `restart_service`, `scale_deployment`, `scale_back`, and `rollback_release`.
- `verify` currently re-queries metrics and logs for L2/L3 action verification.
- `DbDiagnosticsTool` is read-only and supports fixed `SELECT` operations only: `connection_pool`, `locks`, `slow_queries`.

Important gap:

- The current snapshot is advisory. Execution is not blocked when required rollback or preflight data is missing.
- There is no explicit action capability registry tying action type to reversibility, bounded irreversibility, required snapshot fields, rollback handler, and verify strategy.
- `restart_pod` and `restart_service` are operationally useful live mutations but are not truly reversible. They should be allowed only as bounded irreversible actions with explicit safety controls and no restore guarantee.

## Target Semantics

### Action Categories

Classify actions into these execution categories:

| Category | Examples | Execution rule |
| --- | --- | --- |
| `read_only` | `query_metrics`, `query_logs`, `query_traces`, `query_git` | Always allowed by existing risk policy; no reversibility requirement. |
| `record_only` | `generate_report`, `create_ticket` | Allowed by existing L1 policy; no live remediation guarantee. |
| `local_or_fixture_only` | `warmup_cache`, `adjust_connection_pool` | Fixture/local only unless a future explicit safe backend exists. |
| `live_mutating_reversible` | `scale_deployment`, `rollback_release`, `scale_back` | May execute live only if snapshot, rollback handler, and verify gate pass. |
| `live_mutating_bounded_irreversible` | `restart_pod`, `restart_service` | May execute live only with L2 approval, preflight, compact snapshot, rollout verify, and audit. No restore guarantee. |
| `forbidden` | `modify_database`, `delete_data`, `truncate_table`, `flush_cache` | Existing L4 direct reject. |

### Live K8s Execution Contracts

For any `live_mutating_reversible` action:

1. Action must be listed in a deterministic capability registry.
2. Registry entry must define:
   - `action_type`
   - `category`
   - `live_backend`
   - `reversible`
   - `rollback_action_type`
   - `required_snapshot_paths`
   - `verify_gates`
   - `risk_level_expectation`
3. `take_snapshot` must capture all required snapshot fields before execution.
4. `execute_action` must refuse the action if required snapshot fields are absent.
5. `execute_action` must refuse the action if no rollback handler exists for the declared rollback action.
6. `verify` must run the declared verify gate after execution.
7. If verify returns `degraded`, only rollback actions may execute in the next cycle.

For any `live_mutating_bounded_irreversible` action:

1. Action must be listed in the deterministic capability registry.
2. Registry entry must define:
   - `action_type`
   - `category`
   - `live_backend`
   - `reversible=false`
   - `bounded_irreversible=true`
   - `required_snapshot_paths`
   - `preflight_checks`
   - `verify_gates`
   - `risk_level_expectation`
3. Action must still follow existing approval rules. `restart_pod` and `restart_service` remain L2 and require human approval in normal worker mode.
4. Live execution must be limited to Deployment rolling restart through pod template annotation patch. It must not delete Pods or mutate unrelated resources.
5. Snapshot must capture enough context for audit and degraded recovery planning, even though exact restoration is not possible.
6. Preflight must confirm the Deployment exists, has a valid target/namespace, has replicas greater than zero, and is not already in a failed rollout state when that signal is available.
7. Verify must include K8s rollout/readiness checks and existing metrics/logs checks.
8. If verify returns `degraded`, the next cycle may only propose reversible rollback actions or report escalation; it must not repeat restart as the first fallback.

### Database Contract

Database remains read-only:

1. DB diagnostics may enrich `pre_action_snapshot` with a redacted baseline.
2. DB diagnostics may enrich `verify_evidence` after execution.
3. DB diagnostics must never execute DDL, DML, cache flush, session kill, `VACUUM`, `REINDEX`, or other write-like operations.
4. Existing L4 actions such as `modify_database`, `truncate_table`, and forbidden SQL-like terms remain blocked.
5. DB verify failures degrade or fail the verify gate, but they never trigger DB writes.

Recommended DB snapshot fields:

- `db.connection_pool`: state counts from `pg_stat_activity`.
- `db.locks`: lock mode counts from `pg_locks`.
- `db.slow_queries`: redacted query fingerprints or compact summaries only, not raw full SQL.
- `db.captured_operations`: list of diagnostics operations captured.
- `db.error`: optional degraded reason if read-only diagnostics fail.

## Planned Design

### 1. Capability Registry

Add a small deterministic registry, likely under:

- `packages/agent/actions/capabilities.py`

Candidate schema:

```python
class ActionCapability(BaseModel):
    action_type: str
    category: Literal[
        "read_only",
        "record_only",
        "local_or_fixture_only",
        "live_mutating_reversible",
        "live_mutating_bounded_irreversible",
        "forbidden",
    ]
    live_backend: Literal["none", "k8s"]
    reversible: bool
    bounded_irreversible: bool = False
    rollback_action_type: str | None = None
    required_snapshot_paths: tuple[str, ...] = ()
    preflight_checks: tuple[str, ...] = ()
    verify_gates: tuple[str, ...] = ()
    risk_level_expectation: str | None = None
```

Initial live K8s entries:

| Action | Category | Required snapshot | Rollback | Verify |
| --- | --- | --- | --- | --- |
| `scale_deployment` | `live_mutating_reversible` | `k8s.replicas`, `k8s.name`, `k8s.namespace` | `scale_back` | `k8s_rollout`, `metrics_logs`, optional `db_readonly` |
| `scale_back` | `live_mutating_reversible` rollback action | `k8s.replicas`, `k8s.name`, `k8s.namespace` | `scale_deployment` or no chained rollback after max cycle | `k8s_rollout`, `metrics_logs` |
| `rollback_release` | `live_mutating_reversible` | `k8s.revision`, `k8s.image`, `k8s.name`, `k8s.namespace` | `rollback_release` to original revision | `k8s_rollout`, `metrics_logs`, optional `db_readonly` |
| `restart_pod` | `live_mutating_bounded_irreversible` | `k8s.name`, `k8s.namespace`, `k8s.replicas`, `k8s.ready_replicas`, `k8s.available_replicas`, `k8s.image`, optional `k8s.revision` | none | `k8s_rollout`, `metrics_logs` |
| `restart_service` | `live_mutating_bounded_irreversible` | `k8s.name`, `k8s.namespace`, `k8s.replicas`, `k8s.ready_replicas`, `k8s.available_replicas`, `k8s.image`, optional `k8s.revision` | none | `k8s_rollout`, `metrics_logs` |

Rationale for treating restart as bounded irreversible:

- A rolling restart changes pod identity and rollout history.
- It cannot restore in-flight request state.
- It may be operationally safe and commonly useful, but it is not reversible in the same sense as replica restoration or deployment revision rollback.

Therefore restart is allowed in live mode only as a bounded irreversible action with explicit approval, preflight, snapshot, verify, and audit.

### 2. Snapshot Contract Enforcement

Extend `take_snapshot` from advisory capture to contract-aware capture:

- Read action capabilities for pending executable actions.
- Build `snapshot_requirements` by action type.
- Capture K8s deployment snapshot only when required.
- Capture DB read-only baseline when a verify gate requires `db_readonly` or when DB evidence/root cause indicates a database-related incident.
- Record per-action snapshot status:

```json
{
  "snapshot_status": {
    "act_123": {
      "status": "ready",
      "missing_paths": []
    },
    "act_456": {
      "status": "blocked",
      "missing_paths": ["k8s.revision"]
    }
  }
}
```

Execution must not proceed for actions whose snapshot status is `blocked`.

Snapshot failure policy:

- For fixture/local actions, preserve current degraded behavior where appropriate.
- For live reversible K8s actions, missing required snapshot fields is fatal for that action and must block execution.
- For bounded irreversible restart actions, missing required audit/preflight snapshot fields must block execution because verify and audit would be blind.
- Capture errors should be included in state and node trace with no raw secret or raw large logs.

### 3. Execution Preflight and Journal

Before calling a live backend:

1. Resolve action capability.
2. Confirm the active executor is live K8s only for `live_backend="k8s"`.
3. Confirm the action is either `reversible=True` or `bounded_irreversible=True`.
4. Confirm all `required_snapshot_paths` exist.
5. For reversible actions, confirm the declared rollback action has a registered live rollback handler.
6. For bounded irreversible actions, confirm all declared preflight checks pass.
7. Confirm at least one verify gate exists.
8. Persist or update action status before execution to support idempotency.

Preflight checks for restart actions:

- target and namespace pass existing Kubernetes DNS-1123 validation;
- Deployment exists;
- replicas are greater than zero;
- available replicas are not already zero unless the incident class explicitly indicates outage recovery;
- rollout status is not already failed when `rollout_status` is available;
- action type maps only to the existing rolling restart patch path.

Recommended execution status flow:

```text
waiting_approval -> approved -> executing -> succeeded/failed/blocked
```

Idempotency behavior:

- If an action is already `succeeded`, do not execute it again on retry/resume.
- If an action is `executing` and stale, mark it `unknown` or `failed_retriable` only after a configured timeout.
- If status update fails before execution, do not execute live mutation.
- If status update fails after execution, record a structured error and rely on verify, but do not re-execute blindly.

This is the highest-value hardening for Celery retry and worker crash scenarios.

### 4. Verify Gates

Keep existing metrics/logs verification, then add gate-specific checks:

#### `k8s_rollout`

Read-only checks through `K8sDiagnosticsTool`:

- `rollout_status`
- `get_deployment`

Expected signals:

- Deployment exists.
- Observed generation catches up when available.
- Ready/available replicas are not worse than snapshot for scale/rollback actions after the stabilization window.
- If rollout status reports failure or timeout, verdict cannot be `resolved`.

#### `db_readonly`

Read-only checks through `DbDiagnosticsTool`:

- `connection_pool`
- `locks`
- `slow_queries`

Recommended deterministic comparison:

- If DB diagnostics unavailable: mark DB gate `unknown` or `degraded` depending on whether DB gate was required.
- Connection exhaustion incident: resolved only if active/waiting connections decrease or fall below configured threshold.
- Lock contention incident: resolved only if blocking/held lock counts do not worsen.
- Slow query incident: improving only if mean execution time or top slow-query count improves.

Keep thresholds conservative and documented. DB gate should never invent writes as a remediation.

### 5. Planner and Prompt Alignment

Update allowed action guidance so the planner uses the correct live action category:

- Prefer `scale_deployment`, `scale_back`, and `rollback_release` only when evidence supports reversible remediation.
- Allow `restart_pod` and `restart_service` only when evidence supports restart/reset behavior, and label them as bounded irreversible actions with no restore guarantee.
- Do not propose repeated restart as the first fallback after a degraded verify result; prefer reversible rollback or escalation/reporting.
- Tell the model that database actions are diagnostic/read-only only.
- Keep deterministic guardrails as source of truth; prompts only reduce bad proposals.

### 6. Documentation and Operator UX

Update docs to state:

- Snapshot is still not a full-system backup.
- Live policy distinguishes reversible actions from bounded irreversible restart actions.
- Restart actions may execute live under stricter controls, but they do not claim restore guarantees.
- DB integration is read-only baseline and verify only.
- L2/L3 approval still applies; reversibility does not remove approval.
- L3 rollback confirmation remains unchanged.

API/frontend changes are optional for the first implementation, but useful fields in run state include:

- `pre_action_snapshot.snapshot_status`
- per-action `blocked_reason`
- verify gate verdicts

## Deep Plan Review

### Safety Review

Strengths:

- Does not add new real write paths.
- Makes live execution more explicit and controlled than today.
- Keeps L4 data/cache/database actions rejected.
- Removes implicit trust in LLM for reversibility.
- Converts missing snapshot data from best-effort degradation into execution block for live mutations.

Residual risks:

- K8s rollback does not restore external side effects such as dropped requests, consumer lag, or downstream retries.
- Deployment rollback may not restore all dependent resources if ConfigMap, Secret, or HPA changed concurrently.
- `scale_back` can restore replica count but not exact pod placement or in-flight workload.
- Restart actions cannot restore exact pod identity, in-flight requests, or all side effects.
- Argo CD or another controller may race with live patches.

Mitigations:

- State explicitly that this is action-level reversible execution for reversible actions and bounded-risk execution for restart actions, not full restore.
- Use verify gates after rollback as well as after primary and restart actions.
- Preserve original snapshot during degraded rollback cycles.
- Add action status/idempotency protections before live execution.

### Correctness Review

Potential failure modes:

- Snapshot captured stale deployment state because another controller changed it after snapshot.
- Required fields exist but are semantically wrong due backend adapter differences.
- DB baseline is noisy and causes false `unchanged` or `degraded` verdicts.
- Planner overuses bounded irreversible restart actions when reversible remediation or escalation is more appropriate.

Mitigations:

- Include `resource_version` or generation in K8s snapshot if available.
- Re-read deployment immediately before execution and compare identity/generation when feasible.
- Treat DB verify as supporting evidence unless explicitly required by incident class.
- Update planner prompt and add fallback report path when actions fail preflight or all proposed actions are blocked.

### Compatibility Review

Expected behavior changes:

- Live `restart_pod` and `restart_service` remain allowed, but only as bounded irreversible actions with approval, preflight, snapshot, verify, and audit.
- Live restart may fail closed if preflight or required snapshot capture fails.
- Fixture executor can still support deterministic restart demos.
- Existing default local/demo path remains safe because `EXECUTOR_BACKEND=fixture`.

Compatibility risks:

- Manual live demos that expected restart to run unconditionally may now see preflight/snapshot blocks.
- Some existing tests may need to distinguish reversible rollback guarantees from bounded irreversible restart execution.

Mitigations:

- Gate the new strict checks on live executor only, not fixture.
- Add clear blocked reasons to action results.
- Update tests to assert live restart is allowed when preflight passes and blocked when preflight fails.

### Database Review

Database writes remain out of scope.

Rejected ideas:

- Transaction-wrapped arbitrary SQL remediation.
- DDL rollback.
- Automatic `kill session`.
- `VACUUM`, `REINDEX`, `ALTER SYSTEM`, connection limit edits.
- Cache flush or table truncation.

Reason:

- These violate current safety boundaries and cannot be generally reversed by an Agent workflow.

Accepted DB role:

- Read-only baseline.
- Read-only post-action verification.
- Evidence for incident report and future planning.

### Idempotency and Checkpoint Review

Checkpoint protects graph progress, but it is not enough for external side effects.

Required hardening:

- Action-level execution journal must be checked before live mutation.
- Live mutation must not happen if the action cannot be marked `executing`.
- Retry/resume must skip already succeeded actions.
- Rollback actions must also be journaled.

This reduces duplicate live mutations after Celery retry, worker crash, or resume ambiguity.

### Testing Review

Required test dimensions:

- Unit tests for capability registry.
- Guardrail/preflight tests for bounded irreversible restart actions.
- Snapshot tests for required/missing K8s fields.
- DB diagnostics snapshot and verify tests with fixture backend.
- Live executor rollback parameter fill tests.
- Execute action idempotency tests.
- Verify gate verdict tests.
- Integration tests around approval -> snapshot -> execute -> verify -> degraded rollback.
- Regression tests that DB writes remain L4 blocked.

No stable CI test may depend on real Kubernetes, real DB mutation, or real LLM.

## PR Breakdown

### PR 1: Add Action Capability Registry

Implementation:

- Add `packages/agent/actions/capabilities.py`.
- Define action categories, capability schema, and lookup helpers.
- Register all current action types.
- Mark live K8s reversible actions explicitly.
- Mark `restart_pod` and `restart_service` as `live_mutating_bounded_irreversible`.
- Mark DB destructive actions as forbidden and DB diagnostics as read-only only.

Boundaries:

- No behavior change yet, unless tests call the registry directly.
- Do not remove existing guardrail risk classification.
- Do not add new live executor mutations.

Tests:

- `tests/unit/test_action_capabilities.py`
- Assert every `_RISK_TABLE` action has a capability entry.
- Assert every live reversible entry has rollback action, required snapshot paths, and verify gates.
- Assert restart actions are bounded irreversible, not reversible, and have required preflight checks and verify gates.
- Assert DB write-like actions are forbidden.

Acceptance:

- Registry is deterministic and importable without external dependencies.
- Existing tests still pass.

### PR 2: Enforce Live Capability Preflight

Implementation:

- Update `guardrail_check` or `execute_action` to attach capability metadata and blocked reasons.
- In `execute_action`, before live execution:
  - reject unknown live mutation capability;
  - reject live mutations that are neither reversible nor bounded irreversible;
  - reject reversible action without verify gates;
  - reject reversible action whose rollback action is not registered;
  - reject bounded irreversible action without preflight checks and verify gates.
- Keep fixture behavior compatible where possible.

Boundaries:

- Applies to live executor path only.
- Does not relax L2/L3 approval.
- L4 direct reject remains unchanged.
- No DB writes.

Tests:

- `tests/unit/test_agent_nodes.py`
- `tests/unit/test_action_execution.py`
- Live `restart_pod` and `restart_service` execute only when bounded irreversible preflight passes.
- Live restart actions are blocked with explicit reason when preflight or snapshot requirements fail.
- Fixture restart actions still behave deterministically if existing demo tests require it.
- Unknown live mutating action fails closed.
- L0/L1 read-only or record-only actions are not forced through live capability checks.

Acceptance:

- No live mutation can reach `LiveK8sExecutorBackend.execute()` unless registered as reversible or bounded irreversible.
- Blocked action result is visible in `execution_results` and action DB status when applicable.

### PR 3: Snapshot Contract Enforcement

Implementation:

- Extend `take_snapshot` to read required snapshot paths from capabilities.
- Capture K8s deployment details for actions requiring K8s snapshot.
- Add optional DB read-only baseline capture through `DbDiagnosticsTool`.
- Add `pre_action_snapshot.snapshot_status` per action.
- Preserve original snapshot for degraded rollback cycles, as current code already intends.
- Add helper to validate nested snapshot paths.

Boundaries:

- Missing required snapshot blocks only the affected live action.
- DB snapshot is read-only and uses existing fixed diagnostics operations.
- Do not store raw secrets or large raw logs.

Tests:

- Required K8s fields present -> snapshot status `ready`.
- Missing `k8s.replicas` blocks `scale_deployment`.
- Missing `k8s.revision` blocks `rollback_release`.
- K8s tool failure blocks live reversible or bounded irreversible action, but does not crash the graph.
- DB baseline capture uses only `DbDiagnosticsTool`.
- DB diagnostics failure records `db.error` and does not create writes.

Acceptance:

- Live reversible and bounded irreversible actions cannot execute without required snapshot paths.
- Snapshot remains compact and serializable in LangGraph state.

### PR 4: Action Journal and Idempotent Live Execution

Implementation:

- Harden action status transitions around live execution.
- Before live mutation, atomically mark action `executing`.
- If status cannot be updated, block execution.
- Skip actions already `succeeded`.
- Record blocked preflight failures as action status `blocked` or `failed` with structured reason.
- Ensure rollback actions follow the same journal path.

Boundaries:

- Do not introduce destructive DB writes to application databases; this only updates the agent control-plane DB.
- Do not use broad locks beyond the action/run rows needed for idempotency.
- Keep fixture execution simple where existing tests need it, but live path must enforce capability contracts.

Tests:

- Already succeeded action is not executed again.
- Action status update failure prevents live mutation.
- Blocked action records structured reason.
- Rollback action is journaled.
- Celery retry simulation does not duplicate a live action.

Acceptance:

- Live mutation has a durable control-plane status before external side effect.
- Retry/resume cannot blindly re-run succeeded live actions.

### PR 5: Add K8s and DB Verify Gates

Implementation:

- Add verify gate dispatch based on capability metadata.
- Keep existing metrics/logs verification.
- Add `k8s_rollout` gate using read-only K8s diagnostics.
- Add `db_readonly` gate using `DbDiagnosticsTool`.
- Include gate verdicts in state, for example:

```json
{
  "verify_gates": [
    {"gate": "metrics_logs", "verdict": "improving"},
    {"gate": "k8s_rollout", "verdict": "resolved"},
    {"gate": "db_readonly", "verdict": "unknown"}
  ]
}
```

Boundaries:

- Verify gates only read.
- DB gate does not trigger DB write remediation.
- Keep `MAX_VERIFY_CYCLES` bounded.

Tests:

- K8s rollout success contributes to `resolved`.
- K8s rollout failure prevents `resolved`.
- Bounded restart actions execute verify gates and expose bounded irreversible status in state.
- DB connection pool improvement contributes to `improving` or `resolved`.
- DB diagnostics unavailable gives deterministic `unknown` or `degraded` depending on required/optional gate.
- Degraded verdict routes back to `plan_actions`.

Acceptance:

- Every live reversible and bounded irreversible capability has at least one executed verify gate.
- Gate verdicts are visible in state and reportable.

### PR 6: Planner, Docs, and Regression Coverage

Implementation:

- Update `packages/agent/prompts.py` allowed action guidance.
- Update docs:
  - `docs/02-agent/guardrails-and-approval.md`
  - `docs/02-agent/workflow.md`
  - `docs/03-tools/tool-layer.md`
  - possibly `docs/00-overview/scope-and-boundaries.md`
- Add regression tests for live capability policy and DB read-only boundary.
- Add or update integration smoke for approval -> snapshot -> execute -> verify -> rollback or escalation.

Boundaries:

- Documentation must not claim full restore.
- Documentation must state restart actions are bounded irreversible, allowed only under explicit controls, and do not provide restore guarantees.
- No new roadmap item that relaxes safety boundaries.

Tests:

- Existing backend unit coverage.
- Integration test with fixture or mocked live backend.
- Eval smoke remains FakeLLM.

Acceptance:

- Docs and implementation agree.
- CI smoke remains deterministic.
- No real external service is required for tests.

## Suggested Implementation Order

1. PR 1 capability registry.
2. PR 2 live capability preflight.
3. PR 3 snapshot contract enforcement.
4. PR 4 action journal/idempotency hardening.
5. PR 5 verify gates.
6. PR 6 prompt/docs/integration cleanup.

Reason for this order:

- The registry gives a stable contract first.
- Preflight can then fail closed even before richer snapshot work lands.
- Snapshot enforcement provides the concrete rollback data.
- Journal hardening closes the side-effect retry gap.
- Verify gates complete the execution loop.
- Prompt/docs updates come after behavior is concrete.

## Validation Commands

Run targeted tests after each PR:

```bash
pytest tests/unit/test_action_capabilities.py
pytest tests/unit/test_agent_nodes.py
pytest tests/unit/test_action_execution.py
pytest tests/unit/test_executor_backends.py
```

Run broader backend gate before merge:

```bash
pytest tests/unit tests/integration --cov=apps --cov=packages --cov-report=term-missing --cov-report=xml --cov-fail-under=80
```

Frontend tests are only needed if UI surfaces new state fields:

```bash
npm run test:coverage
```

E2E is needed only if the console or full demo flow changes:

```bash
npm run test:e2e
```

## Stop Conditions

Stop and report instead of continuing if any of these occur:

- A proposed implementation requires database writes outside the agent control-plane tables.
- A proposed implementation requires new live Kubernetes mutation types.
- Snapshot requires storing raw secrets, raw auth headers, or large raw logs.
- A test requires real Kubernetes, real DB mutation, or real LLM as a stable CI gate.
- `restart_pod` or `restart_service` is being described as reversible without an explicit product decision to weaken the guarantee.
- Checkpoint or action journal failure would fail open and allow live execution.

## Final Acceptance Criteria

The work is complete when:

1. Live K8s mutating execution is blocked unless the action is registered as reversible or bounded irreversible.
2. Every live reversible action has required snapshot paths, rollback action, rollback handler, and verify gates.
3. Every bounded irreversible restart action has required snapshot paths, preflight checks, verify gates, audit visibility, and no rollback guarantee claim.
4. Missing required snapshot fields or failed preflight checks block live execution.
5. DB integration is read-only baseline and verify only.
6. Degraded verification can only lead to reversible rollback actions or escalation/reporting, not arbitrary new mutations or repeated restart.
7. Live action retry/resume cannot blindly duplicate an already succeeded action.
8. Tests cover registry, preflight, snapshot, rollback, bounded restart, verify, DB read-only boundaries, and idempotency.
9. Docs state action-level reversibility and bounded irreversibility clearly and do not promise full-system restore.
