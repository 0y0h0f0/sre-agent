---
service: checkout
incident_type: db_connection_exhaustion
severity: P2
owner: payment-team
updated_at: 2026-05-31
---
# Database Connection Leak Investigation

## Common Causes
Connection leaks often follow code paths that return early without closing sessions, long transaction scopes, or retry loops that create a new connection for each attempt. A recent checkout-api deploy is important evidence.

## Verification
Look for a rising connection count that does not fall after request volume drops. Sample logs for transaction timeout, session not returned, and connection pool wait. Compare traces for long database spans with the same endpoint name.

## Mitigation Guidance
Avoid direct database writes from the agent. Recommended actions should be mock-only in MVP and cite evidence ids or runbook chunk ids.
