---
service: checkout
incident_type: cache_avalanche
severity: P2
owner: payment-team
updated_at: 2026-05-31
---
# Redis Hot Key Protection

## Symptoms
Hot key pressure appears as high qps to a small set of cache keys, increased Redis latency, and checkout-api timeout logs. It can look similar to cache avalanche but does not require many keys expiring together.

## Triage Steps
Compare cache hit rate with Redis latency. If hit rate remains high but Redis latency rises, hot key pressure is more likely than avalanche. If hit rate falls sharply, return to avalanche checks.

## Evidence Requirement
The diagnosis must cite chunk ids and tool evidence. A runbook title alone is not sufficient evidence for root cause or mitigation.
