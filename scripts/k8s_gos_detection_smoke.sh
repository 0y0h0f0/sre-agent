#!/usr/bin/env bash
set -euo pipefail

# K8s smoke harness for validating sre-agent against the target-namespace project
# in ~/target-backend. It does not run pytest/vitest/playwright. It drives the deployed
# systems through Kubernetes, business traffic, /api/alerts, discovery APIs, and
# agent-run tool_call checks.

AGENT_NS="${AGENT_NS:-sre-agent}"
GOS_NS="${GOS_NS:-target-namespace}"
GOS_REPO="${GOS_REPO:-${HOME}/target-backend}"
OUTPUT_ROOT="${OUTPUT_ROOT:-reports/k8s-gos-detection}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
OUT_DIR="${OUT_DIR:-${OUTPUT_ROOT}/${RUN_ID}}"

AGENT_PORT="${AGENT_PORT:-18000}"
GOS_PORT="${GOS_PORT:-18080}"
SRE_AGENT_URL="${SRE_AGENT_URL:-http://127.0.0.1:${AGENT_PORT}}"
GOS_BASE_URL="${GOS_BASE_URL:-http://127.0.0.1:${GOS_PORT}}"

SCENARIO_SECONDS="${SCENARIO_SECONDS:-90}"
WARMUP_SECONDS="${WARMUP_SECONDS:-20}"
RUN_TIMEOUT_SECONDS="${RUN_TIMEOUT_SECONDS:-420}"
DISCOVERY_TIMEOUT_SECONDS="${DISCOVERY_TIMEOUT_SECONDS:-360}"
STRICT_TOOL_SUCCESS="${STRICT_TOOL_SUCCESS:-false}"

SCENARIOS="${SCENARIOS:-latency_spike,error_burst,db_dependency,redis_failure,user_service_down,task_service_down,pod_restart,metrics_unavailable,tracing_disabled,prometheus_down,catalog_alerts}"

DO_SETUP=false
SETUP_ONLY=false
SKIP_SCENARIOS=false
SKIP_DISCOVERY=false
NO_PORT_FORWARD=false

PF_PIDS=()

usage() {
  cat <<'EOF'
Usage:
  scripts/k8s_gos_detection_smoke.sh [options]

Options:
  --setup                 Apply read-only RBAC, patch Agent live-read config,
                          enable gos SRE fault gate, and restart affected pods.
  --setup-only            Run setup/preflight only, then exit.
  --no-port-forward       Use SRE_AGENT_URL and GOS_BASE_URL as provided.
  --skip-discovery        Skip /api/discovery/rerun and discovery read checks.
  --skip-scenarios        Skip fault scenarios; useful with --setup.
  --scenarios <csv>       Override scenario list.
  --scenario-seconds <n>  Per-scenario traffic duration. Default: 90.
  --warmup-seconds <n>    Seconds to wait after fault injection before alert.
  --out <dir>             Output directory.
  -h, --help              Show this help.

Environment:
  SRE_AGENT_API_KEY             Existing bearer key for protected Agent APIs.
  SRE_AGENT_BOOTSTRAP_SEED      Bootstrap seed to create a short-lived key.
  SRE_AGENT_DB_DIAGNOSTICS_URL  Optional target DB read-only DSN. When set with
                                --setup, DB_DIAGNOSTICS_BACKEND becomes live.

Default scenarios:
  latency_spike,error_burst,db_dependency,redis_failure,user_service_down,
  task_service_down,pod_restart,metrics_unavailable,tracing_disabled,
  prometheus_down,catalog_alerts
EOF
}

require_option_value() {
  local option="$1"
  local value="${2:-}"
  if [[ -z "${value}" || "${value}" == --* ]]; then
    echo "missing value for ${option}" >&2
    usage >&2
    exit 2
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --setup) DO_SETUP=true; shift ;;
    --setup-only) DO_SETUP=true; SETUP_ONLY=true; shift ;;
    --no-port-forward) NO_PORT_FORWARD=true; shift ;;
    --skip-discovery) SKIP_DISCOVERY=true; shift ;;
    --skip-scenarios) SKIP_SCENARIOS=true; shift ;;
    --scenarios) require_option_value "$1" "${2:-}"; SCENARIOS="$2"; shift 2 ;;
    --scenario-seconds) require_option_value "$1" "${2:-}"; SCENARIO_SECONDS="$2"; shift 2 ;;
    --warmup-seconds) require_option_value "$1" "${2:-}"; WARMUP_SECONDS="$2"; shift 2 ;;
    --out) require_option_value "$1" "${2:-}"; OUT_DIR="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

mkdir -p "${OUT_DIR}"
SUMMARY_FILE="${OUT_DIR}/summary.jsonl"
: > "${SUMMARY_FILE}"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { printf '[%s] %s\n' "$(ts)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

run() {
  log "+ $*"
  "$@"
}

api_get() {
  local path="$1"
  local outfile="$2"
  local -a auth=()
  if [[ -n "${SRE_AGENT_API_KEY:-}" ]]; then
    auth=(-H "Authorization: Bearer ${SRE_AGENT_API_KEY}")
  fi
  curl -fsS "${auth[@]}" "${SRE_AGENT_URL}${path}" -o "${outfile}"
}

api_post_json() {
  local path="$1"
  local infile="$2"
  local outfile="$3"
  local -a auth=()
  if [[ -n "${SRE_AGENT_API_KEY:-}" ]]; then
    auth=(-H "Authorization: Bearer ${SRE_AGENT_API_KEY}")
  fi
  curl -fsS "${auth[@]}" -H "Content-Type: application/json" \
    -d @"${infile}" "${SRE_AGENT_URL}${path}" -o "${outfile}"
}

wait_url() {
  local url="$1"
  local label="$2"
  local deadline=$((SECONDS + 90))
  until curl -fsS "${url}" >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then
      die "timeout waiting for ${label}: ${url}"
    fi
    sleep 2
  done
}

start_port_forward() {
  local namespace="$1"
  local service="$2"
  local local_port="$3"
  local remote_port="$4"
  local log_file="${OUT_DIR}/port-forward-${namespace}-${service}.log"
  log "starting port-forward ${namespace}/svc/${service} ${local_port}:${remote_port}"
  kubectl -n "${namespace}" port-forward "svc/${service}" \
    "${local_port}:${remote_port}" >"${log_file}" 2>&1 &
  PF_PIDS+=("$!")
}

cleanup_port_forwards() {
  for pid in "${PF_PIDS[@]}"; do
    kill "${pid}" >/dev/null 2>&1 || true
  done
}

fault_env_keys() {
  cat <<'EOF'
ENABLE_FAULT
FAULT_TYPE
FAULT_TARGET
FAULT_RATE
FAULT_DELAY_MS
FAULT_DURATION_SECONDS
FAULT_ENDPOINT
FAULT_METHOD
FAULT_GRPC_METHOD
FAULT_HTTP_STATUS
FAULT_GRPC_CODE
FAULT_OBSERVABILITY_MODE
FAULT_STARTED_AT
FAULT_RAMP_SECONDS
EOF
}

clear_app_faults() {
  local targets=("$@")
  if [[ ${#targets[@]} -eq 0 ]]; then
    targets=(api-gateway user-service task-service)
  fi
  local removals=()
  while IFS= read -r key; do
    removals+=("${key}-")
  done < <(fault_env_keys)
  for target in "${targets[@]}"; do
    if kubectl -n "${GOS_NS}" set env "deploy/${target}" "${removals[@]}" >/dev/null 2>&1; then
      kubectl -n "${GOS_NS}" rollout status "deploy/${target}" --timeout=180s >/dev/null 2>&1 || true
    fi
  done
}

restore_recorded_replicas() {
  shopt -s nullglob
  local file kind name replicas
  for file in "${OUT_DIR}"/original_replicas_*; do
    kind="$(cut -d_ -f3 <<<"$(basename "${file}")")"
    name="$(cut -d_ -f4- <<<"$(basename "${file}")")"
    replicas="$(cat "${file}")"
    [[ -n "${replicas}" ]] || continue
    kubectl -n "${GOS_NS}" scale "${kind}/${name}" --replicas="${replicas}" >/dev/null 2>&1 || true
  done
}

cleanup() {
  log "cleanup: clearing app faults and restoring recorded replicas"
  clear_app_faults || true
  restore_recorded_replicas || true
  cleanup_port_forwards
}
trap cleanup EXIT

record_replicas() {
  local kind="$1"
  local name="$2"
  local safe_kind="${kind//\//-}"
  local file="${OUT_DIR}/original_replicas_${safe_kind}_${name}"
  if [[ ! -f "${file}" ]]; then
    kubectl -n "${GOS_NS}" get "${kind}/${name}" -o jsonpath='{.spec.replicas}' >"${file}"
  fi
}

scale_workload() {
  local kind="$1"
  local name="$2"
  local replicas="$3"
  record_replicas "${kind}" "${name}" || return $?
  run kubectl -n "${GOS_NS}" scale "${kind}/${name}" --replicas="${replicas}" || return $?
  if [[ "${kind}" == "deploy" || "${kind}" == "deployment" ]]; then
    run kubectl -n "${GOS_NS}" rollout status "deploy/${name}" --timeout=180s || return $?
  elif [[ "${kind}" == "sts" || "${kind}" == "statefulset" ]]; then
    run kubectl -n "${GOS_NS}" rollout status "sts/${name}" --timeout=180s || true
  fi
}

restore_workload() {
  local kind="$1"
  local name="$2"
  local safe_kind="${kind//\//-}"
  local file="${OUT_DIR}/original_replicas_${safe_kind}_${name}"
  [[ -f "${file}" ]] || return 0
  local replicas
  replicas="$(cat "${file}")"
  run kubectl -n "${GOS_NS}" scale "${kind}/${name}" --replicas="${replicas}" || return $?
  if [[ "${kind}" == "deploy" || "${kind}" == "deployment" ]]; then
    run kubectl -n "${GOS_NS}" rollout status "deploy/${name}" --timeout=180s || return $?
  elif [[ "${kind}" == "sts" || "${kind}" == "statefulset" ]]; then
    run kubectl -n "${GOS_NS}" rollout status "sts/${name}" --timeout=180s || true
  fi
}

set_app_fault() {
  local target="$1"
  shift
  local started fault_duration
  started="$(date +%s)"
  fault_duration=$((SCENARIO_SECONDS + WARMUP_SECONDS + 300))
  run kubectl -n "${GOS_NS}" set env "deploy/${target}" \
    ENABLE_SRE_FAULTS=true \
    ENABLE_FAULT=true \
    FAULT_STARTED_AT="${started}" \
    FAULT_DURATION_SECONDS="${fault_duration}" \
    "$@" || return $?
  run kubectl -n "${GOS_NS}" rollout status "deploy/${target}" --timeout=180s || return $?
}

set_observability_fault_all() {
  local mode="$1"
  for target in api-gateway user-service task-service; do
    set_app_fault "${target}" \
      FAULT_TYPE=observability \
      FAULT_TARGET=all \
      FAULT_RATE=1 \
      FAULT_OBSERVABILITY_MODE="${mode}" \
      FAULT_ENDPOINT= \
      FAULT_METHOD=
  done
}

apply_agent_readonly_rbac() {
  log "applying read-only Role/RoleBinding in namespace ${GOS_NS}"
  kubectl apply -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: sre-agent-gos-readonly
  namespace: ${GOS_NS}
rules:
  - apiGroups: [""]
    resources: ["pods", "pods/log", "services", "events", "endpoints", "configmaps"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["events.k8s.io"]
    resources: ["events"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["apps"]
    resources: ["deployments", "statefulsets", "daemonsets", "replicasets"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["networking.k8s.io"]
    resources: ["ingresses"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["batch"]
    resources: ["jobs", "cronjobs"]
    verbs: ["get", "list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: sre-agent-gos-readonly
  namespace: ${GOS_NS}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: sre-agent-gos-readonly
subjects:
  - kind: ServiceAccount
    name: sre-agent
    namespace: ${AGENT_NS}
EOF
}

patch_agent_config() {
  log "patching Agent ConfigMap for gos live-read diagnostics"
  local patch_file="${OUT_DIR}/agent-config-patch.json"
  local DB_BACKEND="fixture"
  if [[ -n "${SRE_AGENT_DB_DIAGNOSTICS_URL:-}" ]]; then
    DB_BACKEND="live"
    local encoded
    encoded="$(printf '%s' "${SRE_AGENT_DB_DIAGNOSTICS_URL}" | base64 | tr -d '\n')"
    kubectl -n "${AGENT_NS}" patch secret sre-agent-secret \
      --type merge \
      -p "{\"data\":{\"DB_DIAGNOSTICS_URL\":\"${encoded}\"}}" >/dev/null
  fi
  GOS_NS="${GOS_NS}" DB_BACKEND="${DB_BACKEND}" python3 - <<'PY' >"${patch_file}"
import json
import os

db_backend = os.environ["DB_BACKEND"]
gos_ns = os.environ["GOS_NS"]
data = {
    "PROMETHEUS_URL": f"http://prometheus.{gos_ns}.svc.cluster.local:9090",
    "LOKI_URL": f"http://loki.{gos_ns}.svc.cluster.local:3100",
    "JAEGER_URL": f"http://jaeger.{gos_ns}.svc.cluster.local:16686",
    "ALERTMANAGER_URL": f"http://alertmanager.{gos_ns}.svc.cluster.local:9093",
    "TRACE_ENABLED": "true",
    "TRACE_BACKEND": "jaeger",
    "K8S_BACKEND": "live",
    "K8S_NAMESPACE": gos_ns,
    "EXECUTOR_BACKEND": "fixture",
    "EXECUTOR_K8S_NAMESPACE": gos_ns,
    "DB_DIAGNOSTICS_BACKEND": db_backend,
    "DEPLOYMENT_BACKEND": "fixture",
    "METRICS_SERVICE_LABEL": "job",
    "LOGS_SERVICE_LABEL": "service",
    "LLM_PROVIDER": "fake",
    "M9_EXTENSIONS_ENABLED": "false",
    "DISCOVERY_ENABLED": "true",
    "DISCOVERY_MANUAL_RERUN_ENABLED": "true",
    "BACKEND_URL_ALLOWLIST": "*.svc.cluster.local,*.svc,kubernetes.default.svc",
    "ALERT_SOURCE": "webhook",
}
print(json.dumps({"data": data}))
PY
  run kubectl -n "${AGENT_NS}" patch configmap sre-agent-config --type merge -p "$(cat "${patch_file}")"
  run kubectl -n "${AGENT_NS}" rollout restart deploy/api deploy/worker
  run kubectl -n "${AGENT_NS}" rollout status deploy/api --timeout=240s
  run kubectl -n "${AGENT_NS}" rollout status deploy/worker --timeout=240s
}

enable_gos_fault_gate() {
  log "enabling gos SRE fault gate on application deployments"
  for target in api-gateway user-service task-service; do
    run kubectl -n "${GOS_NS}" set env "deploy/${target}" ENABLE_SRE_FAULTS=true
    run kubectl -n "${GOS_NS}" rollout status "deploy/${target}" --timeout=180s
  done
}

preflight() {
  require_cmd kubectl
  require_cmd curl
  require_cmd jq
  require_cmd python3
  require_cmd base64

  [[ -d "${GOS_REPO}" ]] || die "GOS_REPO not found: ${GOS_REPO}"
  [[ -x "${GOS_REPO}/tests/sre/traffic/run_baseline_traffic.sh" || -f "${GOS_REPO}/tests/sre/traffic/run_baseline_traffic.sh" ]] \
    || die "gos traffic scripts not found under ${GOS_REPO}/tests/sre/traffic"

  run kubectl get namespace "${AGENT_NS}" >/dev/null
  run kubectl get namespace "${GOS_NS}" >/dev/null
  for d in api worker; do
    run kubectl -n "${AGENT_NS}" get deploy "${d}" >/dev/null
  done
  for d in api-gateway user-service task-service; do
    run kubectl -n "${GOS_NS}" get deploy "${d}" >/dev/null
  done
  for s in prometheus loki jaeger alertmanager api-gateway; do
    run kubectl -n "${GOS_NS}" get svc "${s}" >/dev/null
  done
}

create_api_key_from_bootstrap() {
  if [[ -n "${SRE_AGENT_API_KEY:-}" || -z "${SRE_AGENT_BOOTSTRAP_SEED:-}" ]]; then
    return 0
  fi
  log "creating temporary Agent API key from bootstrap seed"
  local body="${OUT_DIR}/api-key-request.json"
  local resp="${OUT_DIR}/api-key-response.json"
  cat >"${body}" <<EOF
{
  "description": "gos-k8s-detection-${RUN_ID}",
  "expires_in_days": 1,
  "scopes": ["api_key:admin", "discovery:read", "discovery:write", "config:read", "runbook:read"],
  "roles": ["operator"]
}
EOF
  curl -fsS -H "Authorization: Bearer ${SRE_AGENT_BOOTSTRAP_SEED}" \
    -H "Content-Type: application/json" \
    -d @"${body}" "${SRE_AGENT_URL}/api/api-keys" -o "${resp}"
  SRE_AGENT_API_KEY="$(jq -r '.raw_key // empty' "${resp}")"
  [[ -n "${SRE_AGENT_API_KEY}" ]] || die "bootstrap key creation did not return raw_key"
  export SRE_AGENT_API_KEY
}

start_connectivity() {
  if [[ "${NO_PORT_FORWARD}" == "false" ]]; then
    start_port_forward "${AGENT_NS}" api "${AGENT_PORT}" 8000
    start_port_forward "${GOS_NS}" api-gateway "${GOS_PORT}" 80
  fi
  wait_url "${SRE_AGENT_URL}/healthz" "sre-agent healthz"
  wait_url "${SRE_AGENT_URL}/readyz" "sre-agent readyz"
  wait_url "${GOS_BASE_URL}/healthz" "gos api-gateway healthz"
  create_api_key_from_bootstrap
}

submit_alert() {
  local scenario="$1"
  local alert_name="$2"
  local service="$3"
  local severity="$4"
  local payload_file="${OUT_DIR}/${scenario}/alert.json"
  local resp_file="${OUT_DIR}/${scenario}/alert-response.json"
  local starts_at
  starts_at="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  mkdir -p "${OUT_DIR}/${scenario}"
  python3 - "$scenario" "$alert_name" "$service" "$severity" "$starts_at" "$RUN_ID" "$GOS_NS" >"${payload_file}" <<'PY'
import json
import sys

scenario, alert_name, service, severity, starts_at, run_id, namespace = sys.argv[1:]
payload = {
    "source": "mock",
    "fingerprint": f"gos-k8s:{run_id}:{scenario}:{alert_name}:{service}",
    "service": service,
    "severity": severity,
    "alert_name": alert_name,
    "starts_at": starts_at,
    "labels": {
        "namespace": namespace,
        "service": service,
        "job": service,
        "scenario": scenario,
    },
    "annotations": {
        "summary": f"{alert_name} during gos k8s smoke scenario {scenario}",
        "description": "Synthetic validation alert; evidence is read from the live K8s gos environment.",
    },
    "raw_payload": {
        "scenario": scenario,
        "run_id": run_id,
    },
}
print(json.dumps(payload))
PY
  api_post_json "/api/alerts" "${payload_file}" "${resp_file}" || return $?
  jq -r '.agent_run_id' "${resp_file}"
}

tool_report() {
  local run_file="$1"
  jq '[.tool_calls[] | {node_name, tool_name, status, error_message, output_summary}]' "${run_file}"
}

wait_agent_run() {
  local scenario="$1"
  local run_id="$2"
  local out_file="${OUT_DIR}/${scenario}/agent-run.json"
  local deadline=$((SECONDS + RUN_TIMEOUT_SECONDS))
  local status="queued"
  while true; do
    api_get "/api/agent-runs/${run_id}" "${out_file}" || true
    status="$(jq -r '.status // "unknown"' "${out_file}" 2>/dev/null || printf 'unknown')"
    case "${status}" in
      succeeded|waiting_approval|failed|cancelled) break ;;
    esac
    if (( SECONDS >= deadline )); then
      die "timeout waiting for agent run ${run_id} in scenario ${scenario}; last status=${status}"
    fi
    sleep 5
  done
  printf '%s\n' "${status}"
}

summarize_run() {
  local scenario="$1"
  local run_id="$2"
  local expected_csv="$3"
  local run_status="$4"
  local run_file="${OUT_DIR}/${scenario}/agent-run.json"
  local expected_json missing_json failed_json degraded_json line
  local missing_count failed_count degraded_count status
  expected_json="$(python3 - "$expected_csv" <<'PY'
import json, sys
items = [x.strip() for x in sys.argv[1].split(",") if x.strip()]
print(json.dumps(items))
PY
)"
  missing_json="$(jq --argjson expected "${expected_json}" '
    [.tool_calls[].tool_name] as $actual
    | [$expected[] | select(. as $tool | ($actual | index($tool)) | not)]
  ' "${run_file}")"
  failed_json="$(jq '
    [.tool_calls[]
      | select(.status == "failed" or .status == "timeout")
      | {tool_name, node_name, status, error_message}]
  ' "${run_file}")"
  degraded_json="$(jq '
    [.tool_calls[]
      | select(.status == "degraded")
      | {tool_name, node_name, status, error_message}]
  ' "${run_file}")"
  missing_count="$(jq 'length' <<<"${missing_json}")"
  failed_count="$(jq 'length' <<<"${failed_json}")"
  degraded_count="$(jq 'length' <<<"${degraded_json}")"
  status="PASS"
  if [[ "${run_status}" == "failed" || "${run_status}" == "cancelled" ]]; then
    status="FAIL"
  fi
  if [[ "${missing_count}" != "0" || "${failed_count}" != "0" ]]; then
    status="FAIL"
  fi
  if [[ "${STRICT_TOOL_SUCCESS}" == "true" && "${degraded_count}" != "0" ]]; then
    status="FAIL"
  fi
  line="$(jq -nc \
    --arg scenario "${scenario}" \
    --arg status "${status}" \
    --arg run_id "${run_id}" \
    --arg run_status "${run_status}" \
    --argjson expected "${expected_json}" \
    --argjson missing "${missing_json}" \
    --argjson failed "${failed_json}" \
    --argjson degraded "${degraded_json}" \
    --argjson tools "$(tool_report "${run_file}")" \
    '{scenario:$scenario, status:$status, agent_run_id:$run_id, run_status:$run_status,
      expected_tools:$expected, missing_tools:$missing, failed_tools:$failed,
      degraded_tools:$degraded, tool_calls:$tools}')"
  printf '%s\n' "${line}" >> "${SUMMARY_FILE}"

  [[ "${status}" == "PASS" ]]
}

run_traffic() {
  local profile="$1"
  local scenario="$2"
  case "${profile}" in
    none) return 0 ;;
    auth_flow)
      SRE_OUTPUT_DIR="${OUT_DIR}/gos-sre-output" \
        bash "${GOS_REPO}/tests/sre/traffic/run_auth_flow.sh" \
        --base-url "${GOS_BASE_URL}" --duration "${SCENARIO_SECONDS}s" --label "${scenario}"
      ;;
    project_task_flow)
      SRE_OUTPUT_DIR="${OUT_DIR}/gos-sre-output" \
        bash "${GOS_REPO}/tests/sre/traffic/run_project_task_flow.sh" \
        --base-url "${GOS_BASE_URL}" --duration "${SCENARIO_SECONDS}s" --label "${scenario}"
      ;;
    baseline|*)
      SRE_OUTPUT_DIR="${OUT_DIR}/gos-sre-output" \
        bash "${GOS_REPO}/tests/sre/traffic/run_baseline_traffic.sh" \
        --base-url "${GOS_BASE_URL}" --duration "${SCENARIO_SECONDS}s" --label "${scenario}"
      ;;
  esac
}

run_agent_scenario() {
  local scenario="$1"
  local profile="$2"
  local alert_name="$3"
  local service="$4"
  local severity="$5"
  local expected_tools="$6"
  mkdir -p "${OUT_DIR}/${scenario}"

  local traffic_pid=""
  if [[ "${profile}" != "none" ]]; then
    log "starting traffic profile ${profile} for scenario ${scenario}"
    run_traffic "${profile}" "${scenario}" >"${OUT_DIR}/${scenario}/traffic.log" 2>&1 &
    traffic_pid="$!"
  fi

  log "warming up ${WARMUP_SECONDS}s before alert ${alert_name}"
  sleep "${WARMUP_SECONDS}"

  local run_id run_status
  if ! run_id="$(submit_alert "${scenario}" "${alert_name}" "${service}" "${severity}")"; then
    log "scenario ${scenario}: alert submission failed"
    return 1
  fi
  log "scenario ${scenario}: submitted alert ${alert_name}; agent_run=${run_id}"
  if ! run_status="$(wait_agent_run "${scenario}" "${run_id}")"; then
    log "scenario ${scenario}: waiting for agent run failed"
    return 1
  fi

  if [[ -n "${traffic_pid}" ]]; then
    wait "${traffic_pid}" || true
  fi

  if summarize_run "${scenario}" "${run_id}" "${expected_tools}" "${run_status}"; then
    log "scenario ${scenario}: PASS (${run_status})"
  else
    log "scenario ${scenario}: FAIL (${run_status}); see ${OUT_DIR}/${scenario}/agent-run.json"
    return 1
  fi
}

run_discovery_check() {
  [[ "${SKIP_DISCOVERY}" == "false" ]] || return 0
  local dir="${OUT_DIR}/discovery"
  mkdir -p "${dir}"
  log "triggering discovery rerun"
  printf '{"triggered_by":"gos-k8s-detection"}\n' >"${dir}/rerun-request.json"
  api_post_json "/api/discovery/rerun" "${dir}/rerun-request.json" "${dir}/rerun-response.json" || return $?
  local discovery_run_id
  discovery_run_id="$(jq -r '.discovery_run_id // empty' "${dir}/rerun-response.json")"
  [[ -n "${discovery_run_id}" ]] || die "discovery rerun response did not include discovery_run_id"

  local deadline=$((SECONDS + DISCOVERY_TIMEOUT_SECONDS))
  local latest status
  while true; do
    api_get "/api/discovery/status" "${dir}/status.json" || return $?
    latest="$(jq -r '.latest_run.discovery_run_id // empty' "${dir}/status.json")"
    status="$(jq -r '.latest_run.status // "unknown"' "${dir}/status.json")"
    if [[ "${latest}" == "${discovery_run_id}" && "${status}" != "running" ]]; then
      break
    fi
    if (( SECONDS >= deadline )); then
      die "timeout waiting for discovery run ${discovery_run_id}; latest=${latest} status=${status}"
    fi
    sleep 5
  done

  api_get "/api/discovery/services" "${dir}/services.json" || return $?
  api_get "/api/discovery/metrics" "${dir}/metrics.json" || return $?
  api_get "/api/discovery/topology" "${dir}/topology.json" || return $?
  api_get "/api/discovery/capabilities" "${dir}/capabilities.json" || return $?

  jq -nc \
    --arg scenario "discovery" \
    --arg discovery_run_id "${discovery_run_id}" \
    --arg status "${status}" \
    --argjson services "$(jq '.total // 0' "${dir}/services.json")" \
    --argjson capabilities "$(jq '.total_services // 0' "${dir}/capabilities.json")" \
    '{scenario:$scenario, discovery_run_id:$discovery_run_id, status:$status,
      services_total:$services, capabilities_total:$capabilities}' >> "${SUMMARY_FILE}"

  log "discovery run ${discovery_run_id}: ${status}"
}

run_named_scenario() {
  local name="$1"
  local rc=0
  case "${name}" in
    latency_spike)
      set_app_fault api-gateway FAULT_TYPE=latency FAULT_TARGET=api-gateway \
        FAULT_RATE=1 FAULT_DELAY_MS=800 FAULT_ENDPOINT=/api/v1/projects FAULT_METHOD=GET \
        FAULT_HTTP_STATUS=503 FAULT_GRPC_CODE=Unavailable FAULT_RAMP_SECONDS=10 || return $?
      run_agent_scenario "${name}" project_task_flow SlowAPI api-gateway P2 \
        metrics,logs,traces,git_changes,runbook_search,db_diagnostics || rc=$?
      clear_app_faults api-gateway
      return "${rc}"
      ;;
    error_burst)
      set_app_fault api-gateway FAULT_TYPE=error FAULT_TARGET=api-gateway \
        FAULT_RATE=1 FAULT_DELAY_MS=0 FAULT_ENDPOINT=/api/v1/projects FAULT_METHOD=POST \
        FAULT_HTTP_STATUS=503 FAULT_GRPC_CODE=Unavailable FAULT_RAMP_SECONDS=0 || return $?
      run_agent_scenario "${name}" project_task_flow High5xxAfterDeploy api-gateway P2 \
        metrics,logs,traces,git_changes,runbook_search || rc=$?
      clear_app_faults api-gateway
      return "${rc}"
      ;;
    db_dependency)
      scale_workload sts postgres 0 || return $?
      run_agent_scenario "${name}" project_task_flow DatabaseConnectionExhaustion task-service P1 \
        metrics,logs,traces,git_changes,runbook_search,db_diagnostics || rc=$?
      restore_workload sts postgres || rc=$?
      return "${rc}"
      ;;
    redis_failure)
      scale_workload sts redis 0 || return $?
      run_agent_scenario "${name}" baseline RedisCacheAvalanche api-gateway P2 \
        metrics,logs,traces,git_changes,runbook_search || rc=$?
      restore_workload sts redis || rc=$?
      return "${rc}"
      ;;
    user_service_down)
      scale_workload deploy user-service 0 || return $?
      run_agent_scenario "${name}" auth_flow DownstreamTimeout api-gateway P1 \
        metrics,logs,traces,git_changes,runbook_search || rc=$?
      restore_workload deploy user-service || rc=$?
      return "${rc}"
      ;;
    task_service_down)
      scale_workload deploy task-service 0 || return $?
      run_agent_scenario "${name}" project_task_flow DownstreamTimeout api-gateway P1 \
        metrics,logs,traces,git_changes,runbook_search || rc=$?
      restore_workload deploy task-service || rc=$?
      return "${rc}"
      ;;
    pod_restart)
      run kubectl -n "${GOS_NS}" rollout restart deploy/task-service || return $?
      run kubectl -n "${GOS_NS}" rollout status deploy/task-service --timeout=180s || return $?
      run_agent_scenario "${name}" baseline PodRestartLoop task-service P2 \
        metrics,logs,traces,git_changes,runbook_search,k8s
      ;;
    metrics_unavailable)
      set_observability_fault_all block_metrics || return $?
      run_agent_scenario "${name}" baseline MetricsEndpointBlindSpot api-gateway P2 \
        metrics,logs,traces,git_changes,runbook_search || rc=$?
      clear_app_faults
      return "${rc}"
      ;;
    tracing_disabled)
      set_observability_fault_all disable_tracing || return $?
      run_agent_scenario "${name}" baseline TracingDisabled api-gateway P2 \
        metrics,logs,traces,git_changes,runbook_search || rc=$?
      clear_app_faults
      return "${rc}"
      ;;
    prometheus_down)
      scale_workload deploy prometheus 0 || return $?
      run_agent_scenario "${name}" baseline P0SiteOutage api-gateway P1 \
        metrics,logs,traces,git_changes,runbook_search,k8s || rc=$?
      restore_workload deploy prometheus || rc=$?
      return "${rc}"
      ;;
    catalog_alerts)
      local alerts=(
        CPUThrottling:api-gateway:metrics,logs,traces,git_changes,runbook_search,k8s
        MemoryLeak:task-service:metrics,logs,traces,git_changes,runbook_search,k8s
        DiskFull:api-gateway:metrics,logs,traces,git_changes,runbook_search
        CertificateExpiry:api-gateway:metrics,logs,traces,git_changes,runbook_search
        DNSFailure:api-gateway:metrics,logs,traces,git_changes,runbook_search,k8s
        MessageQueueLag:task-service:metrics,logs,traces,git_changes,runbook_search
        RateLimitTriggered:api-gateway:metrics,logs,traces,git_changes,runbook_search
        ErrorBudgetBurn:api-gateway:metrics,logs,traces,git_changes,runbook_search
      )
      local item alert service expected child
      for item in "${alerts[@]}"; do
        IFS=: read -r alert service expected <<<"${item}"
        child="catalog_${alert}"
        run_agent_scenario "${child}" none "${alert}" "${service}" P2 "${expected}" || rc=$?
      done
      return "${rc}"
      ;;
    "")
      ;;
    *)
      die "unknown scenario: ${name}"
      ;;
  esac
}

main() {
  preflight
  if [[ "${DO_SETUP}" == "true" ]]; then
    apply_agent_readonly_rbac
    patch_agent_config
    enable_gos_fault_gate
  fi
  start_connectivity

  log "checking Agent runtime config and target RBAC"
  kubectl -n "${AGENT_NS}" exec deploy/worker -- sh -lc \
    'printenv | sort | egrep "PROMETHEUS_URL|LOKI_URL|JAEGER_URL|K8S_BACKEND|K8S_NAMESPACE|TRACE_BACKEND|DB_DIAGNOSTICS_BACKEND|EXECUTOR_BACKEND"' \
    >"${OUT_DIR}/agent-worker-env.txt" || true
  kubectl -n "${AGENT_NS}" exec deploy/worker -- sh -lc \
    'printf "KUBERNETES_SERVICE_HOST=%s\n" "${KUBERNETES_SERVICE_HOST:-}";
     printf "KUBERNETES_SERVICE_PORT=%s\n" "${KUBERNETES_SERVICE_PORT:-}";
     if [ -r /var/run/secrets/kubernetes.io/serviceaccount/token ]; then echo "serviceaccount_token=present"; else echo "serviceaccount_token=missing"; fi;
     if [ -r /var/run/secrets/kubernetes.io/serviceaccount/ca.crt ]; then echo "serviceaccount_ca=present"; else echo "serviceaccount_ca=missing"; fi' \
    >"${OUT_DIR}/agent-worker-k8s-incluster.txt" || true
  kubectl auth can-i list pods -n "${GOS_NS}" \
    --as="system:serviceaccount:${AGENT_NS}:sre-agent" >"${OUT_DIR}/can-i-list-pods.txt" || true
  kubectl auth can-i get pods/log -n "${GOS_NS}" \
    --as="system:serviceaccount:${AGENT_NS}:sre-agent" >"${OUT_DIR}/can-i-get-pod-logs.txt" || true
  kubectl auth can-i list events -n "${GOS_NS}" \
    --as="system:serviceaccount:${AGENT_NS}:sre-agent" >"${OUT_DIR}/can-i-list-events.txt" || true
  kubectl auth can-i list events.events.k8s.io -n "${GOS_NS}" \
    --as="system:serviceaccount:${AGENT_NS}:sre-agent" >"${OUT_DIR}/can-i-list-events-events-k8s-io.txt" || true
  kubectl auth can-i get deployments.apps -n "${GOS_NS}" \
    --as="system:serviceaccount:${AGENT_NS}:sre-agent" >"${OUT_DIR}/can-i-get-deployments-apps.txt" || true

  if [[ "${SETUP_ONLY}" == "true" ]]; then
    log "setup-only complete; output: ${OUT_DIR}"
    return 0
  fi

  run_discovery_check || log "discovery check failed; continuing to scenarios"

  if [[ "${SKIP_SCENARIOS}" == "true" ]]; then
    log "scenario execution skipped; output: ${OUT_DIR}"
    return 0
  fi

  local failed=0
  IFS=',' read -r -a scenario_list <<<"${SCENARIOS}"
  for scenario in "${scenario_list[@]}"; do
    scenario="$(xargs <<<"${scenario}")"
    [[ -n "${scenario}" ]] || continue
    log "=== scenario: ${scenario} ==="
    if ! run_named_scenario "${scenario}"; then
      failed=$((failed + 1))
    fi
  done

  log "summary written to ${SUMMARY_FILE}"
  if (( failed > 0 )); then
    die "${failed} scenario(s) failed"
  fi
}

main "$@"
