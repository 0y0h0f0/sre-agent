---
service: checkout
incident_type: cache_avalanche
severity: P2
owner: payment-team
updated_at: 2026-05-31
---
# Redis TTL Staggering Checks

## Common Cause
Cache avalanche can happen when many hot keys expire at the same time. Checkout-api then sends a burst of fallback database reads, which can trigger connection pressure and slower responses.

## Verification
Look for synchronized cache misses and repeated reload of the same key group. Confirm whether the most recent deploy changed TTL constants, key prefixes, or warmup behavior.

## Mitigation Guidance
Do not flush Redis in MVP. Propose safer follow-ups such as randomized TTL, cache warming, or mock-only rate limiting with approval when the action is high risk.
