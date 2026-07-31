# Bug — <Short, specific title>

| Field | Value |
| --- | --- |
| **Severity** | S1 Critical \| S2 Major \| S3 Minor \| S4 Trivial |
| **Priority** | P0 \| P1 \| P2 \| P3 |
| **Status** | New \| Triaged \| In Progress \| Fixed \| Won't Fix \| Cannot Reproduce |
| **Reported by** | <name> |
| **Reported on** | YYYY-MM-DD |
| **Affected area** | `apps/api` \| `apps/web` \| `apps/admin` \| `packages/<name>` |
| **Affected version** | <commit, tag, or build> |

### Severity Guide

| Level | Definition |
| --- | --- |
| S1 | Outage, data loss, or security exposure — no workaround |
| S2 | Core feature broken or match integrity affected — workaround is painful |
| S3 | Non-core feature degraded — acceptable workaround exists |
| S4 | Cosmetic or copy issue — no functional impact |

---

## Summary

One or two sentences describing the incorrect behaviour.

## Environment

| | |
| --- | --- |
| **Environment** | local \| dev \| staging \| production |
| **Client** | browser + version, or API client |
| **OS / Device** | |
| **Account / Role** | |
| **Timestamp (UTC)** | |
| **Request / Trace ID** | |

## Steps to Reproduce

1. <step>
2. <step>
3. <step>

**Reproducibility:** Always \| Intermittent (<n> in <m> attempts) \| Once only

## Expected Behaviour

What should have happened, and the source that defines it (spec, ADR, or documented rule).

## Actual Behaviour

What happened instead. Be precise and observable — avoid interpretation.

## Evidence

Logs, stack traces, screenshots, recordings, or network captures.

```text
<paste log or stack trace here>
```

## Impact

Who is affected, how many, and what they cannot do. Include any data integrity concern.

## Workaround

A temporary mitigation for affected users, or "None known".

## Regression

- [ ] Known to have worked previously — last known good version: <version>
- [ ] Never worked
- [ ] Unknown

Suspected cause (commit, deploy, or config change), if identified.

## Root Cause

*Completed during investigation.* The underlying defect, not the symptom.

## Fix

*Completed during resolution.* What was changed and the pull request link.

## Regression Test

*Required for S1 and S2.* The test that fails before the fix and passes after.

## Follow-Up Actions

- [ ] <preventive action to stop this class of bug recurring>