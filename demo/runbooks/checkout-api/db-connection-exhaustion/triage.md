---
service: checkout
incident_type: db_connection_exhaustion
severity: P1
owner: payment-team
updated_at: 2026-05-31
---
# Database Connection Exhaustion Triage

## Detection
Use this runbook when checkout-api logs mention connection pool exhausted, too many connections, timeout acquiring connection, or database unavailable. Prometheus db_connections should be checked against the configured pool and database limit.

## Evidence To Collect
Collect active connection count, request latency, error rate, and logs for pool exhaustion. Add trace evidence showing slow database spans if available. Include recent deploy evidence when pool settings or database access code changed.

## Initial Decision
If active connections remain high while qps is normal, suspect leaked connections or stuck transactions. If qps surges at the same time, suspect traffic-driven saturation and consider rate limiting only after approval.
