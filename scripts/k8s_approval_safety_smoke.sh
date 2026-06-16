#!/usr/bin/env bash
set -euo pipefail

# Targeted K8s/API smoke checks for approval, guardrail, and executor safety.
# This does not run pytest/vitest/playwright and does not enable live remediation.

AGENT_NS="${AGENT_NS:-sre-agent}"
AGENT_PORT="${AGENT_PORT:-18000}"
SRE_AGENT_URL="${SRE_AGENT_URL:-http://127.0.0.1:${AGENT_PORT}}"
OUTPUT_ROOT="${OUTPUT_ROOT:-reports/k8s-approval-safety}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
OUT_DIR="${OUT_DIR:-${OUTPUT_ROOT}/${RUN_ID}}"
RUN_TIMEOUT_SECONDS="${RUN_TIMEOUT_SECONDS:-360}"

mkdir -p "${OUT_DIR}"
SUMMARY_FILE="${OUT_DIR}/summary.json"

PF_PID=""

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { printf '[%s] %s\n' "$(ts)" "$*"; }
fail() { log "FAIL: $*"; exit 1; }

cleanup() {
  if [[ -n "${PF_PID}" ]]; then
    kill "${PF_PID}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "missing command: $1"
}

require_cmd curl
require_cmd jq
require_cmd kubectl

if [[ -z "${SRE_AGENT_API_KEY:-}" && -f /tmp/sre-agent-k8s-smoke-key.json ]]; then
  SRE_AGENT_API_KEY="$(jq -r '.raw_key // empty' /tmp/sre-agent-k8s-smoke-key.json)"
  export SRE_AGENT_API_KEY
fi
[[ -n "${SRE_AGENT_API_KEY:-}" ]] || fail "SRE_AGENT_API_KEY is required"

start_port_forward() {
  kubectl -n "${AGENT_NS}" port-forward svc/api "${AGENT_PORT}:8000" \
    >"${OUT_DIR}/port-forward-api.log" 2>&1 &
  PF_PID="$!"
  for _ in $(seq 1 60); do
    if curl -fsS "${SRE_AGENT_URL}/healthz" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  fail "timeout waiting for ${SRE_AGENT_URL}/healthz"
}

api_get() {
  local path="$1"
  local outfile="$2"
  curl -fsS -H "Authorization: Bearer ${SRE_AGENT_API_KEY}" \
    "${SRE_AGENT_URL}${path}" -o "${outfile}"
}

api_post_status() {
  local path="$1"
  local infile="$2"
  local outfile="$3"
  curl -sS -w '%{http_code}' \
    -H "Authorization: Bearer ${SRE_AGENT_API_KEY}" \
    -H "Content-Type: application/json" \
    -d @"${infile}" \
    "${SRE_AGENT_URL}${path}" -o "${outfile}"
}

api_post_json() {
  local path="$1"
  local infile="$2"
  local outfile="$3"
  local status
  status="$(api_post_status "${path}" "${infile}" "${outfile}")"
  [[ "${status}" =~ ^2 ]] || {
    cat "${outfile}" >&2 || true
    fail "POST ${path} returned HTTP ${status}"
  }
}

json_result() {
  local name="$1"
  local status="$2"
  local detail_file="$3"
  jq -nc \
    --arg name "${name}" \
    --arg status "${status}" \
    --slurpfile detail "${detail_file}" \
    '{name:$name,status:$status,detail:$detail[0]}' >> "${SUMMARY_FILE}.tmp"
}

submit_alert() {
  local scenario="$1"
  local alert_name="$2"
  local service="$3"
  local severity="$4"
  local dir="${OUT_DIR}/${scenario}"
  mkdir -p "${dir}"
  jq -n \
    --arg scenario "${scenario}" \
    --arg alert_name "${alert_name}" \
    --arg service "${service}" \
    --arg severity "${severity}" \
    --arg starts_at "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" \
    --arg fp "k8s-approval-safety:${RUN_ID}:${scenario}" \
    --arg run_id "${RUN_ID}" \
    '{
      source:"mock",
      fingerprint:$fp,
      service:$service,
      severity:$severity,
      alert_name:$alert_name,
      starts_at:$starts_at,
      labels:{service:$service,job:$service,scenario:$scenario,namespace:"task-platform"},
      annotations:{summary:($alert_name + " safety smoke"),description:"Synthetic approval safety smoke"},
      raw_payload:{scenario:$scenario,run_id:$run_id}
    }' >"${dir}/alert.json"
  api_post_json "/api/alerts" "${dir}/alert.json" "${dir}/alert-response.json"
  jq -r '.agent_run_id' "${dir}/alert-response.json"
}

wait_run_status() {
  local scenario="$1"
  local run_id="$2"
  local out_file="${OUT_DIR}/${scenario}/agent-run.json"
  local deadline=$((SECONDS + RUN_TIMEOUT_SECONDS))
  local status
  while true; do
    api_get "/api/agent-runs/${run_id}" "${out_file}" || true
    status="$(jq -r '.status // "unknown"' "${out_file}" 2>/dev/null || printf unknown)"
    case "${status}" in
      waiting_approval|succeeded|failed|cancelled) printf '%s\n' "${status}"; return 0 ;;
    esac
    (( SECONDS < deadline )) || fail "timeout waiting for run ${run_id}; last=${status}"
    sleep 5
  done
}

refresh_run() {
  local scenario="$1"
  local run_id="$2"
  api_get "/api/agent-runs/${run_id}" "${OUT_DIR}/${scenario}/agent-run.json"
}

waiting_approvals_for_run() {
  local run_id="$1"
  local outfile="$2"
  api_get "/api/approvals?status=waiting&page_size=50" "${outfile}"
  jq --arg run "${run_id}" '[.items[]? | select(.agent_run_id == $run)]' "${outfile}"
}

first_waiting_approval() {
  local run_id="$1"
  local outfile="$2"
  waiting_approvals_for_run "${run_id}" "${outfile}" | jq '.[0]'
}

approve_approval() {
  local approval_id="$1"
  local action_type="$2"
  local target="$3"
  local risk="$4"
  local comment="$5"
  local body="$6"
  if [[ "${risk}" == "L3" ]]; then
    jq -n \
      --arg comment "${comment}" \
      --arg action_type "${action_type}" \
      --arg target "${target}" \
      '{approver:"codex-k8s-safety",comment:$comment,risk_ack:true,confirm_action_type:$action_type,confirm_target:$target}' \
      >"${body}"
  else
    jq -n --arg comment "${comment}" \
      '{approver:"codex-k8s-safety",comment:$comment}' >"${body}"
  fi
  api_post_json "/api/approvals/${approval_id}/approve" "${body}" "${body}.response"
}

reject_approval() {
  local approval_id="$1"
  local comment="$2"
  local body="$3"
  jq -n --arg comment "${comment}" \
    '{approver:"codex-k8s-safety",comment:$comment}' >"${body}"
  api_post_json "/api/approvals/${approval_id}/reject" "${body}" "${body}.response"
}

action_target() {
  local action_id="$1"
  local outfile="$2"
  api_get "/api/actions/${action_id}" "${outfile}"
  jq -r '.target // ""' "${outfile}"
}

wait_execution_result() {
  local scenario="$1"
  local run_id="$2"
  local action_id="$3"
  local deadline=$((SECONDS + RUN_TIMEOUT_SECONDS))
  while true; do
    refresh_run "${scenario}" "${run_id}" || true
    if jq -e --arg action "${action_id}" '
      (.state.execution_results // [])
      | any(.action_id == $action and (.execution_result.status // "") == "succeeded")
    ' "${OUT_DIR}/${scenario}/agent-run.json" >/dev/null; then
      return 0
    fi
    (( SECONDS < deadline )) || fail "timeout waiting for action ${action_id} execution"
    sleep 5
  done
}

approve_until_done() {
  local scenario="$1"
  local run_id="$2"
  local max_rounds="${3:-4}"
  local approvals_file="${OUT_DIR}/${scenario}/approvals-loop.json"
  local round approval action_id action_type risk target
  for round in $(seq 1 "${max_rounds}"); do
    refresh_run "${scenario}" "${run_id}" || true
    if [[ "$(jq -r '.status // "unknown"' "${OUT_DIR}/${scenario}/agent-run.json")" == "succeeded" ]]; then
      return 0
    fi
    approval="$(first_waiting_approval "${run_id}" "${approvals_file}")"
    [[ "${approval}" != "null" ]] || { sleep 5; continue; }
    action_id="$(jq -r '.action_id' <<<"${approval}")"
    action_type="$(jq -r '.action_type' <<<"${approval}")"
    risk="$(jq -r '.risk_level' <<<"${approval}")"
    target="$(action_target "${action_id}" "${OUT_DIR}/${scenario}/action-${round}.json")"
    approve_approval \
      "$(jq -r '.approval_id' <<<"${approval}")" \
      "${action_type}" "${target}" "${risk}" \
      "approval safety cleanup round ${round}" \
      "${OUT_DIR}/${scenario}/approve-${round}.json"
    sleep 8
  done
  refresh_run "${scenario}" "${run_id}" || true
}

test_l2_approval() {
  local scenario="l2_approval"
  local run_id status approval action_id action_type risk target
  log "test: L2 approval execution"
  run_id="$(submit_alert "${scenario}" CPUThrottling api-gateway P2)"
  status="$(wait_run_status "${scenario}" "${run_id}")"
  [[ "${status}" == "waiting_approval" ]] || fail "L2 run did not wait for approval: ${status}"
  approval="$(first_waiting_approval "${run_id}" "${OUT_DIR}/${scenario}/approvals.json")"
  risk="$(jq -r '.risk_level' <<<"${approval}")"
  action_type="$(jq -r '.action_type' <<<"${approval}")"
  action_id="$(jq -r '.action_id' <<<"${approval}")"
  [[ "${risk}" == "L2" ]] || fail "expected L2 approval, got ${risk}"
  [[ "${action_type}" == "scale_deployment" ]] || fail "expected scale_deployment, got ${action_type}"
  target="$(action_target "${action_id}" "${OUT_DIR}/${scenario}/action.json")"
  approve_approval "$(jq -r '.approval_id' <<<"${approval}")" "${action_type}" "${target}" "${risk}" \
    "L2 approval safety smoke" "${OUT_DIR}/${scenario}/approve.json"
  wait_execution_result "${scenario}" "${run_id}" "${action_id}"
  approve_until_done "${scenario}" "${run_id}" 4
  jq -n \
    --arg run_id "${run_id}" \
    --arg action_id "${action_id}" \
    --arg action_type "${action_type}" \
    --arg final_status "$(jq -r '.status' "${OUT_DIR}/${scenario}/agent-run.json")" \
    '{agent_run_id:$run_id,approved_action_id:$action_id,action_type:$action_type,final_status:$final_status}' \
    >"${OUT_DIR}/${scenario}/result.json"
  json_result "l2_approval_executes" "PASS" "${OUT_DIR}/${scenario}/result.json"
}

test_rejection_bounded() {
  local scenario="reject_bounded"
  local run_id status approvals_file approval round
  log "test: rejection path bounded replan"
  run_id="$(submit_alert "${scenario}" CPUThrottling api-gateway P2)"
  status="$(wait_run_status "${scenario}" "${run_id}")"
  [[ "${status}" == "waiting_approval" ]] || fail "reject run did not wait: ${status}"
  approvals_file="${OUT_DIR}/${scenario}/approvals.json"
  for round in 1 2 3; do
    approval="$(first_waiting_approval "${run_id}" "${approvals_file}")"
    [[ "${approval}" != "null" ]] || fail "missing waiting approval for reject round ${round}"
    reject_approval "$(jq -r '.approval_id' <<<"${approval}")" \
      "bounded rejection smoke round ${round}" \
      "${OUT_DIR}/${scenario}/reject-${round}.json"
    sleep 8
    refresh_run "${scenario}" "${run_id}" || true
  done
  local deadline=$((SECONDS + RUN_TIMEOUT_SECONDS))
  while true; do
    refresh_run "${scenario}" "${run_id}" || true
    status="$(jq -r '.status // "unknown"' "${OUT_DIR}/${scenario}/agent-run.json")"
    [[ "${status}" == "succeeded" || "${status}" == "failed" || "${status}" == "cancelled" ]] && break
    (( SECONDS < deadline )) || fail "reject bounded run did not settle; last=${status}"
    sleep 5
  done
  [[ "${status}" == "succeeded" ]] || fail "reject bounded final status ${status}"
  local waiting_count
  waiting_count="$(waiting_approvals_for_run "${run_id}" "${approvals_file}" | jq 'length')"
  [[ "${waiting_count}" == "0" ]] || fail "reject bounded left ${waiting_count} waiting approvals"
  jq -n --arg run_id "${run_id}" --arg final_status "${status}" \
    '{agent_run_id:$run_id,final_status:$final_status,rejected_rounds:3}' \
    >"${OUT_DIR}/${scenario}/result.json"
  json_result "reject_replan_is_bounded" "PASS" "${OUT_DIR}/${scenario}/result.json"
}

test_l3_negative_confirmation() {
  local scenario="l3_negative"
  local run_id approval action_id action_type risk target status body resp
  log "test: L3 confirmation negative cases"
  run_id="$(submit_alert "${scenario}" High5xxAfterDeploy api-gateway P1)"
  status="$(wait_run_status "${scenario}" "${run_id}")"
  [[ "${status}" == "waiting_approval" ]] || fail "L3 run did not wait: ${status}"
  approval="$(first_waiting_approval "${run_id}" "${OUT_DIR}/${scenario}/approvals.json")"
  risk="$(jq -r '.risk_level' <<<"${approval}")"
  action_type="$(jq -r '.action_type' <<<"${approval}")"
  action_id="$(jq -r '.action_id' <<<"${approval}")"
  [[ "${risk}" == "L3" ]] || fail "expected L3 approval, got ${risk}"
  target="$(action_target "${action_id}" "${OUT_DIR}/${scenario}/action.json")"

  body="${OUT_DIR}/${scenario}/approve-missing-risk-ack.json"
  jq -n '{approver:"codex-k8s-safety",comment:"missing risk ack"}' >"${body}"
  resp="${body}.response"
  status="$(api_post_status "/api/approvals/$(jq -r '.approval_id' <<<"${approval}")/approve" "${body}" "${resp}")"
  [[ ! "${status}" =~ ^2 ]] || fail "L3 missing risk_ack unexpectedly succeeded"

  body="${OUT_DIR}/${scenario}/approve-wrong-type.json"
  jq -n --arg target "${target}" \
    '{approver:"codex-k8s-safety",comment:"wrong type",risk_ack:true,confirm_action_type:"wrong_action",confirm_target:$target}' >"${body}"
  resp="${body}.response"
  status="$(api_post_status "/api/approvals/$(jq -r '.approval_id' <<<"${approval}")/approve" "${body}" "${resp}")"
  [[ ! "${status}" =~ ^2 ]] || fail "L3 wrong confirm_action_type unexpectedly succeeded"

  body="${OUT_DIR}/${scenario}/approve-wrong-target.json"
  jq -n --arg action_type "${action_type}" \
    '{approver:"codex-k8s-safety",comment:"wrong target",risk_ack:true,confirm_action_type:$action_type,confirm_target:"wrong-target"}' >"${body}"
  resp="${body}.response"
  status="$(api_post_status "/api/approvals/$(jq -r '.approval_id' <<<"${approval}")/approve" "${body}" "${resp}")"
  [[ ! "${status}" =~ ^2 ]] || fail "L3 wrong confirm_target unexpectedly succeeded"

  approval="$(first_waiting_approval "${run_id}" "${OUT_DIR}/${scenario}/approvals-after-negative.json")"
  [[ "$(jq -r '.approval_id' <<<"${approval}")" != "null" ]] || fail "L3 approval no longer waiting after negative cases"
  approve_approval "$(jq -r '.approval_id' <<<"${approval}")" "${action_type}" "${target}" "${risk}" \
    "valid L3 after negative confirmation checks" "${OUT_DIR}/${scenario}/approve-valid.json"
  approve_until_done "${scenario}" "${run_id}" 4
  jq -n --arg run_id "${run_id}" --arg action_id "${action_id}" \
    '{agent_run_id:$run_id,action_id:$action_id,negative_cases:["missing_risk_ack","wrong_action_type","wrong_target"]}' \
    >"${OUT_DIR}/${scenario}/result.json"
  json_result "l3_second_confirmation_negative_cases" "PASS" "${OUT_DIR}/${scenario}/result.json"
}

create_l4_action() {
  kubectl -n "${AGENT_NS}" exec deploy/api -- python -c '
import json
from packages.common.ids import new_id
from packages.common.time import utc_now
from packages.db.models import Action, AgentRun, Incident
from packages.db.session import SessionLocal

db = SessionLocal()
inc_id = new_id("inc_")
run_id = new_id("run_")
act_id = new_id("act_")
db.add(Incident(
    incident_id=inc_id,
    fingerprint="k8s-approval-safety:l4:" + inc_id,
    source="mock",
    service="api-gateway",
    severity="P1",
    alert_name="SyntheticL4",
    status="open",
    starts_at=utc_now(),
    labels={},
    annotations={},
    raw_payload={},
))
db.add(AgentRun(agent_run_id=run_id, incident_id=inc_id, status="succeeded", state={}))
db.flush()
db.add(Action(
    action_id=act_id,
    incident_id=inc_id,
    agent_run_id=run_id,
    type="delete_data",
    risk_level="L4",
    status="proposed",
    executor="fixture",
    target="orders",
    params={"table": "orders"},
    reason="synthetic L4 safety test",
))
db.commit()
print(json.dumps({"incident_id": inc_id, "agent_run_id": run_id, "action_id": act_id}))
db.close()
'
}

test_l4_block() {
  local scenario="l4_block"
  local created action_id body status
  log "test: L4 direct block"
  mkdir -p "${OUT_DIR}/${scenario}"
  created="$(create_l4_action)"
  printf '%s\n' "${created}" >"${OUT_DIR}/${scenario}/created.json"
  action_id="$(jq -r '.action_id' "${OUT_DIR}/${scenario}/created.json")"
  jq -n '{operator:"codex-k8s-safety",reason:"verify L4 block"}' >"${OUT_DIR}/${scenario}/execute.json"
  status="$(api_post_status "/api/actions/${action_id}/execute" "${OUT_DIR}/${scenario}/execute.json" "${OUT_DIR}/${scenario}/execute-response.json")"
  [[ ! "${status}" =~ ^2 ]] || fail "L4 execute unexpectedly succeeded"
  api_get "/api/actions/${action_id}" "${OUT_DIR}/${scenario}/action-after.json"
  [[ "$(jq -r '.status' "${OUT_DIR}/${scenario}/action-after.json")" == "blocked" ]] || fail "L4 action not blocked"
  [[ "$(jq -r '.execution_result.status' "${OUT_DIR}/${scenario}/action-after.json")" == "blocked" ]] || fail "L4 execution_result not blocked"
  jq -n --arg action_id "${action_id}" --arg http_status "${status}" \
    '{action_id:$action_id,http_status:$http_status,blocked:true}' \
    >"${OUT_DIR}/${scenario}/result.json"
  json_result "l4_direct_rejection" "PASS" "${OUT_DIR}/${scenario}/result.json"
}

test_live_executor_safety() {
  local scenario="live_executor_safety"
  log "test: live executor safety boundaries"
  mkdir -p "${OUT_DIR}/${scenario}"
  kubectl -n "${AGENT_NS}" exec deploy/worker -- python -c '
import json
from packages.common.settings import get_settings
from packages.tools.executor_backends import _LIVE_HANDLERS, _LIVE_ROLLBACK_HANDLERS
from packages.agent.actions.capabilities import get_action_capability

s = get_settings()
k8s_live = []
for action in sorted(["restart_pod", "restart_service", "scale_deployment", "scale_back", "rollback_release", "rollback_deployment"]):
    cap = get_action_capability(action)
    if cap and cap.live_backend == "k8s":
        k8s_live.append(action)
blocked_examples = {}
for action in ["enable_rate_limit", "adjust_connection_pool", "delete_data"]:
    cap = get_action_capability(action)
    blocked_examples[action] = None if cap is None else {
        "category": cap.category,
        "live_backend": cap.live_backend,
        "risk_level_expectation": cap.risk_level_expectation,
    }
print(json.dumps({
    "executor_backend": s.executor_backend,
    "live_handler_keys": sorted(_LIVE_HANDLERS),
    "live_rollback_handler_keys": sorted(_LIVE_ROLLBACK_HANDLERS),
    "k8s_live_capability_actions": k8s_live,
    "blocked_examples": blocked_examples,
}))
' >"${OUT_DIR}/${scenario}/runtime.json"
  [[ "$(jq -r '.executor_backend' "${OUT_DIR}/${scenario}/runtime.json")" == "fixture" ]] || fail "executor backend is not fixture"
  jq -e '
    (.k8s_live_capability_actions | sort) == (["restart_pod","restart_service","rollback_deployment","rollback_release","scale_back","scale_deployment"] | sort)
    and (.blocked_examples.enable_rate_limit.live_backend == "none")
    and (.blocked_examples.adjust_connection_pool.live_backend == "none")
    and (.blocked_examples.delete_data.category == "forbidden")
  ' "${OUT_DIR}/${scenario}/runtime.json" >/dev/null || fail "live executor safety capability check failed"
  json_result "live_executor_safety_boundaries" "PASS" "${OUT_DIR}/${scenario}/runtime.json"
}

main() {
  : >"${SUMMARY_FILE}.tmp"
  start_port_forward
  test_l2_approval
  test_rejection_bounded
  test_l3_negative_confirmation
  test_l4_block
  test_live_executor_safety
  jq -s --arg generated_at "${RUN_ID}" \
    '{status:"PASS", generated_at:$generated_at, results:.}' \
    "${SUMMARY_FILE}.tmp" >"${SUMMARY_FILE}"
  log "summary written to ${SUMMARY_FILE}"
}

main "$@"
