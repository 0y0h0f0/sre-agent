---
service: checkout
incident_type: pod_restart_loop
severity: P2
owner: payment-team
updated_at: 2026-05-31
---
# Pod Restart Loop OOMKilled Path

## Symptoms
OOMKilled restarts usually show rising memory, abrupt process exit, and Kubernetes events referencing memory limit. The service may return intermittent 5xx while pods recycle.

## Checks
Compare process memory with restart timestamps. Pull logs before the restart, not only after the new pod starts. Look for large payload handling, cache growth, or leak signatures after a release.

## Response
Scaling or restart proposals require approval depending on risk level. Do not perform real Kubernetes write operations in MVP; the executor must remain mock-only.
