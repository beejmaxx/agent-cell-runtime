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

## 2026-10-07 — R2 Colima preflight

**Completed preflight only; R2 validation is still in progress.** Context `colima`,
node k3s 1.35, checksum-verified kubectl v1.35.0 in `.local/bin`; global kubectl
unchanged. `make k8s-up` created only marked `agent-runtime` and `agent-exec`.
`make agent-image` built `agent-runtime/fake-agent:r2` in Colima's Docker daemon.

Reproduce: `make k8s-tools k8s-up agent-image k8s-preflight` (serial Make).
The preflight receiver listens on Mac loopback and collects sanitized completion
facts; Pod status supplies failure evidence. No container logs are used.

Observed from the fixed execution template:

- `/sys/fs/cgroup/cpu.max` is `25000 100000`; `cpu.stat` is readable.
- The projected token carries audience `agent-cell-gateway`, an `exp`, subject
  `system:serviceaccount:agent-exec:agent-exec`, and the observed Pod UID.
  Issued lifetime was 600 s (recorded, not assumed to be an issuer guarantee).
- The default token directory is absent; the Kubernetes API returns 401 when
  presented with the projected token over verified TLS.
- A confirmed, fsynced 48 MiB write into the 32 MiB emptyDir was evicted:
  `Usage of EmptyDir volume "tmp" exceeds the limit "32Mi".`
- A separate bounded 80 MiB write, with only the preflight emptyDir size limit
  raised to 96 MiB to isolate container storage enforcement, was evicted:
  `Pod ephemeral local storage usage exceeds the total limit of containers 64Mi.`
- Node `Ready=True`, `DiskPressure=False`. Probe Pods were deleted with UID
  preconditions. Namespaces and local image remain for the subsequent tests.

Evidence: `.local/k8s/preflight/*-status.json`, `*-result.json`, and
`node-conditions.json` (ignored locally; contain no bearer credentials).
The first ephemeral-storage assertion expected the API field spelling with a
hyphen; the actual eviction message uses "ephemeral local storage". Correcting
that assertion and rerunning both storage probes passed; neither test was skipped.

These observations establish that the preflight mechanisms work on this cluster;
they make no network isolation or hostile-code sandbox claim.
