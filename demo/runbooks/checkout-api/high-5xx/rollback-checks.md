---
service: checkout
incident_type: high_5xx
severity: P1
owner: payment-team
updated_at: 2026-05-31
---
# High 5xx Rollback Checks

## Preconditions
Rollback is considered when checkout-api 5xx rate stays above the SLO burn threshold and the last deploy is the strongest evidence. Confirm that the previous image is known, migrations are backward compatible, and no active approval already covers the same action.

## Safety Checks
Review database migration notes, feature flag changes, and cache schema changes. If the deploy changed request validation only, rollback is usually safe. If it changed database writes, require L3 approval and a clear rollback plan.

## Expected Evidence
The diagnosis must cite the runbook chunk id together with metric, log, trace, or git evidence. Do not claim rollback safety based only on a source path.
