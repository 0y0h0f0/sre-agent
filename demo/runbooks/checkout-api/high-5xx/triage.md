---
service: checkout
incident_type: high_5xx
severity: P1
owner: payment-team
updated_at: 2026-05-31
---
# High 5xx After Deploy Triage

## Detection
Use this runbook when checkout-api error rate rises after a deployment. Check the alert window, the deployment timestamp, and whether the first 5xx samples start within ten minutes of the release.

## Evidence To Collect
Collect Prometheus error_rate, latency, and qps for checkout-api. Pull Loki logs with keywords error, exception, timeout, and rollback. Compare traces for downstream payment and inventory spans, then attach the latest git change summary.

## Initial Decision
If 5xx errors line up with a new checkout-api commit and traces show application exceptions, prepare rollback checks. If 5xx errors come from downstream timeouts only, escalate to the downstream service owner before changing checkout-api.
