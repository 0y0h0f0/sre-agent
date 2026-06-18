#!/usr/bin/env bash
set -euo pipefail

# Controlled smoke helper for EXECUTOR_BACKEND=live.
#
# Default MODE=preflight performs read-only cluster/API checks.
# MODE=agent-scale submits a CPUThrottling alert and approves the resulting
# L2 scale_deployment action through the API/worker graph. It mutates only the
# configured smoke Deployment and requires an exact confirmation string:
#
#   LIVE_EXECUTOR_SMOKE_CONFIRM="agent-scale:${TARGET_NS}:${DEPLOYMENT}"
#
# MODE=agent-scale-transaction and MODE=agent-rollback-transaction additionally
# create a temporary checkout Deployment, flip the Agent runtime to live/fake,
# create a temporary API key, run the selected smoke, then restore the previous
# runtime state on exit.

AGENT_NS="${AGENT_NS:-sre-agent}"
TARGET_NS="${TARGET_NS:-sre-agent-smoke}"
DEPLOYMENT="${DEPLOYMENT:-checkout}"
STATEFULSET="${STATEFULSET:-}"
MODE="${MODE:-preflight}"
AGENT_PORT="${AGENT_PORT:-18000}"
SRE_AGENT_URL="${SRE_AGENT_URL:-http://127.0.0.1:${AGENT_PORT}}"
OUTPUT_ROOT="${OUTPUT_ROOT:-reports/k8s-live-executor}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
OUT_DIR="${OUT_DIR:-${OUTPUT_ROOT}/${RUN_ID}}"
RUN_TIMEOUT_SECONDS="${RUN_TIMEOUT_SECONDS:-360}"
RESTORE_SCALE="${RESTORE_SCALE:-true}"

PF_PID=""
ORIGINAL_REPLICAS=""
ORIGINAL_EXECUTOR_BACKEND=""
ORIGINAL_K8S_BACKEND=""
ORIGINAL_EXECUTOR_K8S_NAMESPACE=""
ORIGINAL_K8S_NAMESPACE=""
ORIGINAL_CM_LLM_PROVIDER=""
ORIGINAL_SECRET_LLM_PROVIDER_B64=""
TRANSACTION_STARTED="false"
TEMP_CHECKOUT_CREATED="false"
TEMP_API_KEY_FILE=""
TEMP_API_KEY_CREATED="false"

mkdir -p "${OUT_DIR}"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { printf '[%s] %s\n' "$(ts)" "$*"; }
fail() { log "FAIL: $*"; exit 1; }

usage() {
  cat <<EOF
Usage:
  MODE=preflight scripts/k8s_live_executor_smoke.sh
  MODE=agent-scale LIVE_EXECUTOR_SMOKE_CONFIRM="agent-scale:${TARGET_NS}:${DEPLOYMENT}" scripts/k8s_live_executor_smoke.sh
  MODE=agent-scale-transaction LIVE_EXECUTOR_SMOKE_CONFIRM="agent-scale-transaction:${TARGET_NS}:${DEPLOYMENT}" scripts/k8s_live_executor_smoke.sh
  MODE=agent-rollback-transaction LIVE_EXECUTOR_SMOKE_CONFIRM="agent-rollback-transaction:${TARGET_NS}:${DEPLOYMENT}" scripts/k8s_live_executor_smoke.sh

Environment:
  AGENT_NS                     Agent namespace. Default: sre-agent
  TARGET_NS                    Controlled smoke namespace. Default: sre-agent-smoke
  DEPLOYMENT                   Smoke Deployment target. Default: checkout
  STATEFULSET                  Optional smoke StatefulSet target for preflight reads
  MODE                         preflight | agent-scale | agent-scale-transaction | agent-rollback-transaction
  SRE_AGENT_API_KEY            Bearer key when API auth is enabled
  SRE_AGENT_URL                API URL. Default uses local port-forward
  RESTORE_SCALE                Restore original replicas after agent-scale. Default: true
  LIVE_EXECUTOR_SMOKE_CONFIRM  Required for mutating modes
EOF
}

cleanup() {
  if [[ -n "${PF_PID}" ]]; then
    kill "${PF_PID}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

cleanup_all() {
  local exit_code=$?
  set +e
  restore_scale
  restore_transaction
  cleanup
  exit "${exit_code}"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "missing command: $1"
}

require_cmd curl
require_cmd jq
require_cmd kubectl

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

if [[ -z "${SRE_AGENT_API_KEY:-}" && -f /tmp/sre-agent-k8s-smoke-key.json ]]; then
  SRE_AGENT_API_KEY="$(jq -r '.raw_key // empty' /tmp/sre-agent-k8s-smoke-key.json)"
  export SRE_AGENT_API_KEY
fi

api_get() {
  local path="$1"
  local outfile="$2"
  if [[ -n "${SRE_AGENT_API_KEY:-}" ]]; then
    curl -fsS -H "Authorization: Bearer ${SRE_AGENT_API_KEY}" \
      "${SRE_AGENT_URL}${path}" -o "${outfile}"
  else
    curl -fsS "${SRE_AGENT_URL}${path}" -o "${outfile}"
  fi
}

api_post_status() {
  local path="$1"
  local infile="$2"
  local outfile="$3"
  if [[ -n "${SRE_AGENT_API_KEY:-}" ]]; then
    curl -sS -w '%{http_code}' \
      -H "Authorization: Bearer ${SRE_AGENT_API_KEY}" \
      -H "Content-Type: application/json" \
      -d @"${infile}" \
      "${SRE_AGENT_URL}${path}" -o "${outfile}"
  else
    curl -sS -w '%{http_code}' \
      -H "Content-Type: application/json" \
      -d @"${infile}" \
      "${SRE_AGENT_URL}${path}" -o "${outfile}"
  fi
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

start_port_forward() {
  if curl -fsS "${SRE_AGENT_URL}/healthz" >/dev/null 2>&1; then
    return 0
  fi
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

read_runtime_env() {
  local workload="$1"
  local outfile="$2"
  kubectl -n "${AGENT_NS}" exec "deploy/${workload}" -- sh -lc \
    'printenv | sort | egrep "^(EXECUTOR_BACKEND|EXECUTOR_K8S_NAMESPACE|K8S_BACKEND|K8S_NAMESPACE|LLM_PROVIDER|API_KEY_AUTH_ENABLED)=" || true' \
    >"${outfile}"
}

can_i() {
  local verb="$1"
  local resource="$2"
  local subresource="${3:-}"
  if [[ -n "${subresource}" ]]; then
    kubectl auth can-i "${verb}" "${resource}" --subresource="${subresource}" \
      -n "${TARGET_NS}" --as="system:serviceaccount:${AGENT_NS}:sre-agent"
  else
    kubectl auth can-i "${verb}" "${resource}" -n "${TARGET_NS}" \
      --as="system:serviceaccount:${AGENT_NS}:sre-agent"
  fi
}

record_can_i() {
  local verb="$1"
  local resource="$2"
  local subresource="${3:-}"
  if [[ -n "${subresource}" ]]; then
    printf '%s %s/%s: ' "${verb}" "${resource}" "${subresource}"
    can_i "${verb}" "${resource}" "${subresource}" || true
  else
    printf '%s %s: ' "${verb}" "${resource}"
    can_i "${verb}" "${resource}" || true
  fi
}

run_preflight() {
  log "preflight: context and target resources"
  kubectl config current-context >"${OUT_DIR}/kubectl-context.txt"
  kubectl get ns "${TARGET_NS}" -o json >"${OUT_DIR}/target-namespace.json"
  kubectl -n "${TARGET_NS}" get deployment "${DEPLOYMENT}" -o json \
    >"${OUT_DIR}/deployment-before.json"
  if [[ -n "${STATEFULSET}" ]]; then
    kubectl -n "${TARGET_NS}" get statefulset "${STATEFULSET}" -o json \
      >"${OUT_DIR}/statefulset-before.json"
  fi

  log "preflight: agent workloads and runtime env"
  kubectl -n "${AGENT_NS}" get deploy api worker -o json >"${OUT_DIR}/agent-deployments.json"
  read_runtime_env api "${OUT_DIR}/api-env.txt"
  read_runtime_env worker "${OUT_DIR}/worker-env.txt"

  log "preflight: API health"
  start_port_forward
  curl -fsS "${SRE_AGENT_URL}/healthz" -o "${OUT_DIR}/healthz.json"
  curl -fsS "${SRE_AGENT_URL}/readyz" -o "${OUT_DIR}/readyz.json"

  log "preflight: live executor RBAC"
  {
    record_can_i patch deployments.apps
    record_can_i patch deployments.apps scale
    record_can_i create deployments.apps rollback
    record_can_i patch statefulsets.apps
    record_can_i get pods
    record_can_i get pods/log
  } >"${OUT_DIR}/rbac.txt"
  kubectl get --raw /apis/apps/v1 | jq -r \
    '.resources[] | select(.name | test("^(deployments|statefulsets)(/|$)")) | .name + " " + (.verbs | join(","))' \
    >"${OUT_DIR}/apps-v1-resources.txt"

  local worker_executor worker_k8s worker_namespace worker_llm rollback_api_available
  worker_executor="$(grep -E '^EXECUTOR_BACKEND=' "${OUT_DIR}/worker-env.txt" | cut -d= -f2- || true)"
  worker_k8s="$(grep -E '^K8S_BACKEND=' "${OUT_DIR}/worker-env.txt" | cut -d= -f2- || true)"
  worker_namespace="$(grep -E '^EXECUTOR_K8S_NAMESPACE=' "${OUT_DIR}/worker-env.txt" | cut -d= -f2- || true)"
  worker_llm="$(grep -E '^LLM_PROVIDER=' "${OUT_DIR}/worker-env.txt" | cut -d= -f2- || true)"
  rollback_api_available="$(
    awk '$1 == "deployments/rollback" { found = "yes" } END { print found ? found : "no" }' \
      "${OUT_DIR}/apps-v1-resources.txt"
  )"

  jq -n \
    --arg mode "${MODE}" \
    --arg context "$(cat "${OUT_DIR}/kubectl-context.txt")" \
    --arg agent_ns "${AGENT_NS}" \
    --arg target_ns "${TARGET_NS}" \
    --arg deployment "${DEPLOYMENT}" \
    --arg worker_executor "${worker_executor}" \
    --arg worker_k8s "${worker_k8s}" \
    --arg worker_namespace "${worker_namespace}" \
    --arg worker_llm "${worker_llm}" \
    --arg rollback_api_available "${rollback_api_available}" \
    --rawfile rbac "${OUT_DIR}/rbac.txt" \
    --rawfile apps_v1_resources "${OUT_DIR}/apps-v1-resources.txt" \
    '{
      status: "PREFLIGHT_COMPLETE",
      mode: $mode,
      context: $context,
      agent_namespace: $agent_ns,
      target_namespace: $target_ns,
      deployment: $deployment,
      worker: {
        executor_backend: $worker_executor,
        k8s_backend: $worker_k8s,
        executor_k8s_namespace: $worker_namespace,
        llm_provider: $worker_llm
      },
      rbac: $rbac,
      api_resources: {
        deployments_rollback_available: $rollback_api_available,
        apps_v1: $apps_v1_resources
      }
    }' >"${OUT_DIR}/summary.json"

  [[ "${worker_executor}" == "live" ]] || fail "worker EXECUTOR_BACKEND is not live"
  [[ "${worker_k8s}" == "live" ]] || fail "worker K8S_BACKEND is not live"
  [[ "${worker_namespace}" == "${TARGET_NS}" ]] || fail "worker EXECUTOR_K8S_NAMESPACE does not match TARGET_NS"
  [[ "${worker_llm}" == "fake" || "${worker_llm}" == "disabled" ]] || fail "worker LLM_PROVIDER is not fake/disabled"
  grep -q 'patch deployments.apps: yes' "${OUT_DIR}/rbac.txt" || fail "missing patch deployments RBAC"
  grep -q 'patch deployments.apps/scale: yes' "${OUT_DIR}/rbac.txt" || fail "missing patch deployments/scale RBAC"
  if [[ "${MODE}" == "agent-rollback-transaction" && "${rollback_api_available}" != "yes" ]]; then
    fail "apps/v1 deployments/rollback API resource is not available in this cluster"
  fi

  log "preflight PASS: ${OUT_DIR}/summary.json"
}

submit_scale_alert() {
  local dir="${OUT_DIR}/agent-scale"
  mkdir -p "${dir}"
  jq -n \
    --arg starts_at "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" \
    --arg fp "k8s-live-executor:${RUN_ID}:agent-scale" \
    --arg ns "${TARGET_NS}" \
    '{
      source:"mock",
      fingerprint:$fp,
      service:"checkout",
      severity:"P2",
      alert_name:"CPUThrottling",
      starts_at:$starts_at,
      labels:{service:"checkout",job:"checkout",namespace:$ns},
      annotations:{summary:"CPUThrottling live executor smoke",description:"Synthetic scale_deployment smoke"},
      raw_payload:{scenario:"agent-scale"}
    }' >"${dir}/alert.json"
  api_post_json "/api/alerts" "${dir}/alert.json" "${dir}/alert-response.json"
  jq -r '.agent_run_id' "${dir}/alert-response.json"
}

submit_rollback_alert() {
  local dir="${OUT_DIR}/agent-rollback"
  mkdir -p "${dir}"
  jq -n \
    --arg starts_at "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" \
    --arg fp "k8s-live-executor:${RUN_ID}:agent-rollback" \
    --arg ns "${TARGET_NS}" \
    '{
      source:"mock",
      fingerprint:$fp,
      service:"checkout",
      severity:"P1",
      alert_name:"High5xxAfterDeploy",
      starts_at:$starts_at,
      labels:{service:"checkout",job:"checkout",namespace:$ns},
      annotations:{summary:"High5xxAfterDeploy live rollback smoke",description:"Synthetic rollback_deployment smoke"},
      raw_payload:{scenario:"agent-rollback"}
    }' >"${dir}/alert.json"
  api_post_json "/api/alerts" "${dir}/alert.json" "${dir}/alert-response.json"
  jq -r '.agent_run_id' "${dir}/alert-response.json"
}

wait_run_status() {
  local run_id="$1"
  local outfile="$2"
  local deadline=$((SECONDS + RUN_TIMEOUT_SECONDS))
  local status
  while true; do
    api_get "/api/agent-runs/${run_id}" "${outfile}" || true
    status="$(jq -r '.status // "unknown"' "${outfile}" 2>/dev/null || printf unknown)"
    case "${status}" in
      waiting_approval|succeeded|failed|cancelled) printf '%s\n' "${status}"; return 0 ;;
    esac
    (( SECONDS < deadline )) || fail "timeout waiting for run ${run_id}; last=${status}"
    sleep 5
  done
}

first_waiting_approval() {
  local run_id="$1"
  local outfile="$2"
  api_get "/api/approvals?status=waiting&page_size=50" "${outfile}"
  jq --arg run "${run_id}" '[.items[]? | select(.agent_run_id == $run)][0]' "${outfile}"
}

approve_l2() {
  local approval_id="$1"
  local body="$2"
  jq -n '{approver:"codex-live-smoke",comment:"controlled live executor scale smoke"}' \
    >"${body}"
  api_post_json "/api/approvals/${approval_id}/approve" "${body}" "${body}.response"
}

approve_l3() {
  local approval_id="$1"
  local action_type="$2"
  local target="$3"
  local body="$4"
  jq -n \
    --arg action_type "${action_type}" \
    --arg target "${target}" \
    '{
      approver:"codex-live-smoke",
      comment:"controlled live executor rollback smoke",
      risk_ack:true,
      confirm_action_type:$action_type,
      confirm_target:$target
    }' >"${body}"
  api_post_json "/api/approvals/${approval_id}/approve" "${body}" "${body}.response"
}

action_target() {
  local action_id="$1"
  local outfile="$2"
  api_get "/api/actions/${action_id}" "${outfile}"
  jq -r '.target // ""' "${outfile}"
}

wait_scale_execution() {
  local run_id="$1"
  local action_id="$2"
  local outfile="$3"
  local deadline=$((SECONDS + RUN_TIMEOUT_SECONDS))
  while true; do
    api_get "/api/agent-runs/${run_id}" "${outfile}" || true
    if jq -e --arg action "${action_id}" '
      (.state.execution_results // [])
      | any(.action_id == $action and (.execution_result.status // "") == "succeeded")
    ' "${outfile}" >/dev/null; then
      return 0
    fi
    (( SECONDS < deadline )) || fail "timeout waiting for scale execution result"
    sleep 5
  done
}

wait_action_execution() {
  local run_id="$1"
  local action_id="$2"
  local outfile="$3"
  local deadline=$((SECONDS + RUN_TIMEOUT_SECONDS))
  while true; do
    api_get "/api/agent-runs/${run_id}" "${outfile}" || true
    if jq -e --arg action "${action_id}" '
      (.state.execution_results // [])
      | any(.action_id == $action and ((.execution_result.status // "") != ""))
    ' "${outfile}" >/dev/null; then
      return 0
    fi
    (( SECONDS < deadline )) || fail "timeout waiting for action ${action_id} execution result"
    sleep 5
  done
}

restore_scale() {
  if [[ "${RESTORE_SCALE}" != "true" || -z "${ORIGINAL_REPLICAS}" ]]; then
    return 0
  fi
  log "cleanup: restoring ${DEPLOYMENT} replicas to ${ORIGINAL_REPLICAS}"
  kubectl -n "${TARGET_NS}" scale deployment "${DEPLOYMENT}" \
    --replicas="${ORIGINAL_REPLICAS}" >/dev/null
}

restore_transaction() {
  if [[ "${TRANSACTION_STARTED}" != "true" ]]; then
    return 0
  fi

  log "cleanup: restoring Agent runtime config"
  kubectl -n "${AGENT_NS}" patch cm sre-agent-config --type merge -p "$(
    jq -n \
      --arg executor "${ORIGINAL_EXECUTOR_BACKEND}" \
      --arg k8s_backend "${ORIGINAL_K8S_BACKEND}" \
      --arg executor_ns "${ORIGINAL_EXECUTOR_K8S_NAMESPACE}" \
      --arg k8s_ns "${ORIGINAL_K8S_NAMESPACE}" \
      --arg llm "${ORIGINAL_CM_LLM_PROVIDER}" \
      '{
        data: {
          EXECUTOR_BACKEND: $executor,
          K8S_BACKEND: $k8s_backend,
          EXECUTOR_K8S_NAMESPACE: $executor_ns,
          K8S_NAMESPACE: $k8s_ns,
          LLM_PROVIDER: $llm
        }
      }'
  )" >/dev/null

  if [[ -n "${ORIGINAL_SECRET_LLM_PROVIDER_B64}" ]]; then
    kubectl -n "${AGENT_NS}" patch secret sre-agent-secret --type json -p "$(
      jq -n --arg value "${ORIGINAL_SECRET_LLM_PROVIDER_B64}" \
        '[{"op":"replace","path":"/data/LLM_PROVIDER","value":$value}]'
    )" >/dev/null
  else
    kubectl -n "${AGENT_NS}" patch secret sre-agent-secret --type json -p \
      '[{"op":"remove","path":"/data/LLM_PROVIDER"}]' >/dev/null 2>&1 || true
  fi

  kubectl -n "${AGENT_NS}" rollout restart deploy/api deploy/worker >/dev/null
  kubectl -n "${AGENT_NS}" rollout status deploy/api --timeout=180s >/dev/null
  kubectl -n "${AGENT_NS}" rollout status deploy/worker --timeout=180s >/dev/null

  if [[ "${TEMP_CHECKOUT_CREATED}" == "true" ]]; then
    log "cleanup: deleting temporary deployment/${DEPLOYMENT}"
    kubectl -n "${TARGET_NS}" delete deploy "${DEPLOYMENT}" --ignore-not-found=true >/dev/null
  fi

  if [[ "${TEMP_API_KEY_CREATED}" == "true" && -n "${TEMP_API_KEY_FILE}" && -f "${TEMP_API_KEY_FILE}" ]]; then
    local key_id
    key_id="$(jq -r '.key_id // empty' "${TEMP_API_KEY_FILE}")"
    if [[ -n "${key_id}" ]]; then
      log "cleanup: revoking temporary API key ${key_id}"
      kubectl -n "${AGENT_NS}" exec deploy/api -- env KEY_ID="${key_id}" python -c '
import os
from apps.api.services.api_key_service import ApiKeyService
from packages.db.session import SessionLocal

db = SessionLocal()
try:
    ApiKeyService(db).revoke(os.environ["KEY_ID"])
finally:
    db.close()
' >/dev/null 2>&1 || true
    fi
    rm -f "${TEMP_API_KEY_FILE}"
  fi
}

create_temp_api_key() {
  TEMP_API_KEY_FILE="/tmp/sre-agent-k8s-live-smoke-key-${RUN_ID}.json"
  kubectl -n "${AGENT_NS}" exec deploy/api -- python -c '
from apps.api.schemas.api_keys import ApiKeyCreateRequest
from apps.api.services.api_key_service import ApiKeyService
from packages.db.session import SessionLocal

db = SessionLocal()
try:
    response = ApiKeyService(db).create(
        ApiKeyCreateRequest(
            description="codex live executor smoke temporary key",
            expires_in_days=1,
            scopes=[],
            roles=["operator"],
        ),
        created_by="codex-live-smoke",
    )
    print(response.model_dump_json())
finally:
    db.close()
' >"${TEMP_API_KEY_FILE}"
  TEMP_API_KEY_CREATED="true"
  SRE_AGENT_API_KEY="$(jq -r '.raw_key' "${TEMP_API_KEY_FILE}")"
  export SRE_AGENT_API_KEY
}

create_temp_checkout() {
  if kubectl -n "${TARGET_NS}" get deploy "${DEPLOYMENT}" >/dev/null 2>&1; then
    fail "deployment/${DEPLOYMENT} already exists; refusing to mutate an existing target"
  fi
  kubectl -n "${TARGET_NS}" get deploy api-gateway -o json | jq \
    --arg deployment "${DEPLOYMENT}" \
    'del(
      .metadata.uid,
      .metadata.resourceVersion,
      .metadata.generation,
      .metadata.creationTimestamp,
      .metadata.managedFields,
      .metadata.annotations,
      .status
    )
    | .metadata.name = $deployment
    | .metadata.labels.app = $deployment
    | .spec.replicas = 2
    | .spec.selector.matchLabels.app = $deployment
    | .spec.template.metadata.labels.app = $deployment
    | .spec.template.metadata.annotations["codex.openai.com/live-smoke"] = "true"
    | .spec.template.spec.containers[0].name = $deployment' \
    | kubectl apply -f - >/dev/null
  TEMP_CHECKOUT_CREATED="true"
  kubectl -n "${TARGET_NS}" rollout status "deploy/${DEPLOYMENT}" --timeout=180s >/dev/null
}

prepare_temp_checkout_rollback_revision() {
  local value="rollback-smoke-${RUN_ID}"
  kubectl -n "${TARGET_NS}" patch deploy "${DEPLOYMENT}" --type merge -p "$(
    jq -n --arg value "${value}" \
      '{
        spec: {
          template: {
            metadata: {
              annotations: {
                "codex.openai.com/rollback-smoke-revision": $value
              }
            }
          }
        }
      }'
  )" >/dev/null
  kubectl -n "${TARGET_NS}" rollout status "deploy/${DEPLOYMENT}" --timeout=180s >/dev/null
  kubectl -n "${TARGET_NS}" get deploy "${DEPLOYMENT}" -o json \
    >"${OUT_DIR}/deployment-before-rollback-alert.json"
}

save_runtime_config() {
  ORIGINAL_EXECUTOR_BACKEND="$(
    kubectl -n "${AGENT_NS}" get cm sre-agent-config \
      -o jsonpath='{.data.EXECUTOR_BACKEND}'
  )"
  ORIGINAL_K8S_BACKEND="$(
    kubectl -n "${AGENT_NS}" get cm sre-agent-config \
      -o jsonpath='{.data.K8S_BACKEND}'
  )"
  ORIGINAL_EXECUTOR_K8S_NAMESPACE="$(
    kubectl -n "${AGENT_NS}" get cm sre-agent-config \
      -o jsonpath='{.data.EXECUTOR_K8S_NAMESPACE}'
  )"
  ORIGINAL_K8S_NAMESPACE="$(
    kubectl -n "${AGENT_NS}" get cm sre-agent-config \
      -o jsonpath='{.data.K8S_NAMESPACE}'
  )"
  ORIGINAL_CM_LLM_PROVIDER="$(
    kubectl -n "${AGENT_NS}" get cm sre-agent-config \
      -o jsonpath='{.data.LLM_PROVIDER}'
  )"
  ORIGINAL_SECRET_LLM_PROVIDER_B64="$(
    kubectl -n "${AGENT_NS}" get secret sre-agent-secret \
      -o jsonpath='{.data.LLM_PROVIDER}' 2>/dev/null || true
  )"
}

set_live_runtime_config() {
  kubectl -n "${AGENT_NS}" patch cm sre-agent-config --type merge -p "$(
    jq -n \
      --arg target_ns "${TARGET_NS}" \
      '{
        data: {
          EXECUTOR_BACKEND: "live",
          K8S_BACKEND: "live",
          EXECUTOR_K8S_NAMESPACE: $target_ns,
          K8S_NAMESPACE: $target_ns,
          LLM_PROVIDER: "fake"
        }
      }'
  )" >/dev/null
  kubectl -n "${AGENT_NS}" patch secret sre-agent-secret --type json -p \
    '[{"op":"replace","path":"/data/LLM_PROVIDER","value":"ZmFrZQ=="}]' >/dev/null
  kubectl -n "${AGENT_NS}" rollout restart deploy/api deploy/worker >/dev/null
  kubectl -n "${AGENT_NS}" rollout status deploy/api --timeout=180s >/dev/null
  kubectl -n "${AGENT_NS}" rollout status deploy/worker --timeout=180s >/dev/null
}

run_agent_scale() {
  local expected_confirm="agent-scale:${TARGET_NS}:${DEPLOYMENT}"
  [[ "${LIVE_EXECUTOR_SMOKE_CONFIRM:-}" == "${expected_confirm}" ]] || {
    fail "set LIVE_EXECUTOR_SMOKE_CONFIRM='${expected_confirm}' for mutating agent-scale mode"
  }
  [[ "${DEPLOYMENT}" == "checkout" ]] || fail "FakeLLM scale smoke requires DEPLOYMENT=checkout"

  run_preflight
  ORIGINAL_REPLICAS="$(jq -r '.spec.replicas // 1' "${OUT_DIR}/deployment-before.json")"
  trap cleanup_all EXIT

  log "agent-scale: submitting CPUThrottling alert"
  local run_id status approval action_id action_type risk replicas_after
  run_id="$(submit_scale_alert)"
  status="$(wait_run_status "${run_id}" "${OUT_DIR}/agent-scale/agent-run.json")"
  [[ "${status}" == "waiting_approval" ]] || fail "run did not wait for approval: ${status}"

  approval="$(first_waiting_approval "${run_id}" "${OUT_DIR}/agent-scale/approvals.json")"
  [[ "${approval}" != "null" ]] || fail "missing waiting approval for run ${run_id}"
  action_id="$(jq -r '.action_id' <<<"${approval}")"
  action_type="$(jq -r '.action_type' <<<"${approval}")"
  risk="$(jq -r '.risk_level' <<<"${approval}")"
  [[ "${action_type}" == "scale_deployment" ]] || fail "expected scale_deployment, got ${action_type}"
  [[ "${risk}" == "L2" ]] || fail "expected L2, got ${risk}"

  log "agent-scale: approving ${action_id}"
  approve_l2 "$(jq -r '.approval_id' <<<"${approval}")" "${OUT_DIR}/agent-scale/approve.json"
  wait_scale_execution "${run_id}" "${action_id}" "${OUT_DIR}/agent-scale/agent-run-after.json"

  kubectl -n "${TARGET_NS}" get deployment "${DEPLOYMENT}" -o json \
    >"${OUT_DIR}/agent-scale/deployment-after-scale.json"
  replicas_after="$(jq -r '.spec.replicas // empty' "${OUT_DIR}/agent-scale/deployment-after-scale.json")"
  [[ "${replicas_after}" == "4" ]] || fail "expected ${DEPLOYMENT} replicas=4, got ${replicas_after}"

  jq -n \
    --arg run_id "${run_id}" \
    --arg action_id "${action_id}" \
    --arg original_replicas "${ORIGINAL_REPLICAS}" \
    --arg replicas_after "${replicas_after}" \
    '{
      status: "PASS",
      mode: "agent-scale",
      agent_run_id: $run_id,
      action_id: $action_id,
      original_replicas: ($original_replicas | tonumber),
      replicas_after: ($replicas_after | tonumber)
    }' >"${OUT_DIR}/agent-scale/result.json"
  jq -s '{status:"PASS", results:.}' "${OUT_DIR}/summary.json" "${OUT_DIR}/agent-scale/result.json" \
    >"${OUT_DIR}/summary-agent-scale.json"

  log "agent-scale PASS: ${OUT_DIR}/summary-agent-scale.json"
}

run_agent_scale_transaction() {
  local expected_confirm="agent-scale-transaction:${TARGET_NS}:${DEPLOYMENT}"
  [[ "${LIVE_EXECUTOR_SMOKE_CONFIRM:-}" == "${expected_confirm}" ]] || {
    fail "set LIVE_EXECUTOR_SMOKE_CONFIRM='${expected_confirm}' for mutating transaction mode"
  }
  [[ "${DEPLOYMENT}" == "checkout" ]] || fail "transaction mode requires DEPLOYMENT=checkout"

  trap cleanup_all EXIT
  save_runtime_config
  TRANSACTION_STARTED="true"

  log "transaction: creating temporary API key"
  create_temp_api_key
  log "transaction: creating temporary deployment/${DEPLOYMENT}"
  create_temp_checkout
  log "transaction: switching Agent runtime to EXECUTOR_BACKEND=live and LLM_PROVIDER=fake"
  set_live_runtime_config

  LIVE_EXECUTOR_SMOKE_CONFIRM="agent-scale:${TARGET_NS}:${DEPLOYMENT}"
  run_agent_scale
}

run_agent_rollback_transaction() {
  local expected_confirm="agent-rollback-transaction:${TARGET_NS}:${DEPLOYMENT}"
  [[ "${LIVE_EXECUTOR_SMOKE_CONFIRM:-}" == "${expected_confirm}" ]] || {
    fail "set LIVE_EXECUTOR_SMOKE_CONFIRM='${expected_confirm}' for mutating transaction mode"
  }
  [[ "${DEPLOYMENT}" == "checkout" ]] || fail "transaction mode requires DEPLOYMENT=checkout"

  trap cleanup_all EXIT
  save_runtime_config
  TRANSACTION_STARTED="true"

  log "transaction: creating temporary API key"
  create_temp_api_key
  log "transaction: creating temporary deployment/${DEPLOYMENT}"
  create_temp_checkout
  log "transaction: preparing a second deployment revision"
  prepare_temp_checkout_rollback_revision
  log "transaction: switching Agent runtime to EXECUTOR_BACKEND=live and LLM_PROVIDER=fake"
  set_live_runtime_config

  MODE="agent-rollback-transaction"
  run_preflight

  log "agent-rollback: submitting High5xxAfterDeploy alert"
  local run_id status approval action_id action_type risk target execution_status
  run_id="$(submit_rollback_alert)"
  status="$(wait_run_status "${run_id}" "${OUT_DIR}/agent-rollback/agent-run.json")"
  [[ "${status}" == "waiting_approval" ]] || fail "rollback run did not wait for approval: ${status}"

  approval="$(first_waiting_approval "${run_id}" "${OUT_DIR}/agent-rollback/approvals.json")"
  [[ "${approval}" != "null" ]] || fail "missing waiting approval for rollback run ${run_id}"
  action_id="$(jq -r '.action_id' <<<"${approval}")"
  action_type="$(jq -r '.action_type' <<<"${approval}")"
  risk="$(jq -r '.risk_level' <<<"${approval}")"
  [[ "${action_type}" == "rollback_deployment" || "${action_type}" == "rollback_release" ]] || {
    fail "expected rollback action, got ${action_type}"
  }
  [[ "${risk}" == "L3" ]] || fail "expected L3, got ${risk}"
  target="$(action_target "${action_id}" "${OUT_DIR}/agent-rollback/action.json")"

  log "agent-rollback: approving ${action_id}"
  approve_l3 \
    "$(jq -r '.approval_id' <<<"${approval}")" \
    "${action_type}" \
    "${target}" \
    "${OUT_DIR}/agent-rollback/approve.json"
  wait_action_execution "${run_id}" "${action_id}" "${OUT_DIR}/agent-rollback/agent-run-after.json"
  execution_status="$(
    jq -r --arg action "${action_id}" '
      (.state.execution_results // [])
      | map(select(.action_id == $action))[0].execution_result.status // "missing"
    ' "${OUT_DIR}/agent-rollback/agent-run-after.json"
  )"
  jq -n \
    --arg run_id "${run_id}" \
    --arg action_id "${action_id}" \
    --arg action_type "${action_type}" \
    --arg execution_status "${execution_status}" \
    '{
      status: (if $execution_status == "succeeded" then "PASS" else "FAIL" end),
      mode: "agent-rollback",
      agent_run_id: $run_id,
      action_id: $action_id,
      action_type: $action_type,
      execution_status: $execution_status
    }' >"${OUT_DIR}/agent-rollback/result.json"
  [[ "${execution_status}" == "succeeded" ]] || {
    jq -s '{status:"FAIL", results:.}' "${OUT_DIR}/summary.json" "${OUT_DIR}/agent-rollback/result.json" \
      >"${OUT_DIR}/summary-agent-rollback.json"
    fail "rollback execution status was ${execution_status}; see ${OUT_DIR}/summary-agent-rollback.json"
  }
  jq -s '{status:"PASS", results:.}' "${OUT_DIR}/summary.json" "${OUT_DIR}/agent-rollback/result.json" \
    >"${OUT_DIR}/summary-agent-rollback.json"

  log "agent-rollback PASS: ${OUT_DIR}/summary-agent-rollback.json"
}

case "${MODE}" in
  preflight)
    run_preflight
    ;;
  agent-scale)
    run_agent_scale
    ;;
  agent-scale-transaction)
    run_agent_scale_transaction
    ;;
  agent-rollback-transaction)
    run_agent_rollback_transaction
    ;;
  *)
    usage
    fail "unknown MODE '${MODE}'"
    ;;
esac
