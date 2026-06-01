---
service: checkout
incident_type: db_connection_exhaustion
severity: P2
owner: payment-team
updated_at: 2026-05-31
---
# Database Pool Saturation Response

## Symptoms
Pool saturation differs from a leak because connections eventually return to the pool. Requests wait for a connection and then succeed or fail after a timeout. Error rate and latency usually move together.

## Checks
Check qps, active connection count, p95 latency, and database span duration in the same time window. If all rise together, the diagnosis should state saturation rather than a leak.

## Response
Prepare conservative mitigations first: reduce noisy callers, raise follow-up tickets, or propose controlled scaling. Any rate-limit change is L3 and requires second confirmation.
