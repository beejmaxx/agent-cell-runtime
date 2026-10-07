# Experiment log

Use this file to preserve observed behavior, failed hypotheses, recovery steps, and the evidence supporting design decisions. Never mark a planned experiment as completed.

## 2026-10-07 — Project initialized

**Completed:** documented scope, architectural questions, and the staged learning roadmap.

**Observed:** this repository contains documentation only. No runtime, Kubernetes cluster, AWS infrastructure, or failure experiment has been implemented here.

**Initial design questions:** whether a sidecar can provide an unavoidable enforcement path; how trusted identity reaches policy; how operation idempotency differs from execution idempotency; how cleanup preserves terminal outcome; how audit survives partial failure.

**Next:** establish the threat model, invariants, identity model, state ownership, state machine, and trust boundaries together before implementing runtime code. Include explicit control-plane/data-plane responsibilities and distinguish execution, operation, and attempt identities.

## Template for future entries

```text
Date / title:
Status: planned | running | completed | inconclusive
Question or hypothesis:
Environment and versions:
Commit:
Setup and reproduction commands:
Expected invariant:
Failure or adversarial input:
Observed behavior:
Evidence (sanitized logs, traces, commands, artifact paths):
Interpretation (separate from observations):
Recovery and teardown:
Limitations:
Decision and next experiment:
```
