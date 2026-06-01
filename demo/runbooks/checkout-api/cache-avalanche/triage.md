---
service: checkout
incident_type: cache_avalanche
severity: P1
owner: payment-team
updated_at: 2026-05-31
---
# Redis Cache Avalanche Triage

## Detection
Use this runbook when checkout-api latency and database load increase while Redis cache hit rate drops sharply. Logs may mention cache miss spike, Redis timeout, or fallback query pressure.

## Evidence To Collect
Collect cache_hit_rate, qps, database connection count, latency, and logs with cache keywords. Compare the alert window with any deploy that changed cache keys, TTL values, or serialization format.

## Initial Decision
If cache misses and database pressure rise together, suspect cache avalanche. If Redis itself is unavailable, separate the diagnosis into dependency outage and application fallback behavior.
