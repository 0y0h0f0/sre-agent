---
service: checkout
incident_type: pod_restart_loop
severity: P1
owner: payment-team
updated_at: 2026-05-31
---
# Pod Restart Loop Triage

## Detection
Use this runbook when checkout-api pods repeatedly restart, readiness fails, or logs mention OOMKilled, CrashLoopBackOff, panic, or failed startup checks.

## Evidence To Collect
Collect restart count, recent logs, Kubernetes event fixture data, metrics for memory and cpu, and recent git changes. Check whether the restart loop starts immediately after deploy or after sustained traffic.

## Initial Decision
If restarts begin after a deploy and logs show startup panic, suspect regression. If memory rises before OOMKilled, suspect resource pressure or a leak.
