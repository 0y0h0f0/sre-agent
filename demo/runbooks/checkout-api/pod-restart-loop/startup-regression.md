---
service: checkout
incident_type: pod_restart_loop
severity: P2
owner: payment-team
updated_at: 2026-05-31
---
# Pod Startup Regression Checks

## Common Cause
Startup regressions happen when a new image cannot load configuration, connect to required dependencies, or complete migrations. Pods may restart before serving traffic.

## Verification
Check deploy timing, first failing pod event, readiness probe messages, and startup logs. If failures start only after a checkout-api image change, cite git evidence and this runbook chunk.

## Mitigation Guidance
Rollback may be appropriate when startup failure is clearly tied to a deploy. It is L3 in this project and requires explicit risk acknowledgement and target confirmation.
