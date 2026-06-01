---
service: checkout
incident_type: high_5xx
severity: P2
owner: payment-team
updated_at: 2026-05-31
---
# High 5xx Downstream Timeout Path

## Symptoms
Checkout-api may return 5xx when downstream payment, pricing, or inventory calls exceed their deadline. Logs usually contain timeout, context deadline exceeded, connection reset, or circuit breaker open.

## Triage Steps
Compare checkout-api latency with downstream span duration. If checkout-api qps is steady but downstream duration spikes, the likely cause is dependency latency rather than a checkout deployment regression.

## Mitigation Options
Prefer read-only confirmation first. Low-risk mitigations include increasing alert detail or opening a ticket. Restarting pods, scaling deployments, or rollback proposals must pass guardrail and approval policy.
