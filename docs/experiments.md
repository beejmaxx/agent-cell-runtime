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


## 2026-10-07 — R2 scope narrowed

The CPU and storage observations above are historical results of the original
preflight, not isolation evidence. After threat-model revision 1, the approved
R2 scope removed CPU, OOM, storage eviction, filesystem freshness, and containment
probes. Their fake-agent behaviors and preflight code have been removed. Current
`make k8s-preflight` checks projected-token issuance only, alongside setup guards.
The fixed resource template, controller capacity limit, and API quota remain.

## 2026-10-08 — Narrowed R2 validation completed

**Completed:** `make test` (128), `make test-integration` (4), and
`make test-k8s` (18, plus the token preflight). The final cluster suite took
288 seconds. Ruff and whitespace checks passed. This is controller and API
behavior evidence only; it establishes no execution isolation guarantee.

Observed on Colima:

- Happy-path input survived kubelet environment processing byte for byte.
  Lost create responses and controller restarts retained completion authority;
  no execution issued a second create. Outages preserved queued work while
  cancellation and deadlines still applied.
- The kubelet produced `DeadlineExceeded` without reconciliation. Cleanup,
  orphan checks, and same-name UID replacement behaved as specified.
- The projected token carried the intended audience, subject, expiry, and Pod
  binding; its observed lifetime was 600 seconds. The Kubernetes API rejected
  it with 401 over verified TLS. Environment names matched the exact approved
  allowlist, including runtime-provided HOME and HOSTNAME.
- All eight admission negative cases were rejected by PodSecurity, and all
  eight positive controls passed in the temporary unlabeled namespace. No
  positive control was deferred. The writable-root control wrote and read
  back its probe, while the fixed template returned EROFS.
- Global capacity and the quota backstop held. Real controller-token calls
  could read Pods in agent-exec, could not read Secrets there, and could not
  dry-run a Pod create in rook-dev. Access reviews matched the specified role.

Corrections during validation: the permissions assertion initially omitted
built-in OpenID discovery URLs. The UID-conflict parser initially assumed a
storage-layer error shape; this API returns `details.kind: Pod` and a message
that the UID in the precondition does not match the UID in the record. The
parser and unit fixture now recognize that response; the real test records
409 and verifies the replacement remains. One integration attempt received a
500 at Mock Workday login; subsequent complete runs passed using only its HTTP
API, without changing that service.

Sanitized local evidence: `.local/k8s/evidence/ISO-1-ID-8.json`,
`ISO-1-permissions.txt`, `ISO-5-admission.json`, `LC-11-uid-conflict.json`, and
per-Pod status JSON. No bearer credentials or Pod env values are saved there.
Each cluster test ended with no Pods in agent-exec; temporary control namespaces
were deleted. The two marked lab namespaces, service accounts, Role,
RoleBinding, quota, and local image remain available. No push was performed.

## 2026-10-08 — S1 checkpoint-2 local regression comparison (unresolved)

This is local test evidence, not an S1 AWS experiment. The reviewer requested
one baseline run with an approximately 20-minute investigation cap.

- **Baseline `bb4f05b`:** an unmodified detached worktree, a separate locked
  Python environment, and its original rebuilt agent image. `make test-k8s`
  passed its preflight and **18 tests** in **304.18 seconds**.
- **Checkpoint-2 `024b024`:** immediately afterwards, rebuilt its agent image
  and ran the same target. Preflight passed; **9 tests passed, 1 failed** in
  **285.20 seconds**, stopping at `test_LC_8_kubelet_deadline_without_reconciler`.
  The Pod was not observed in `Failed/DeadlineExceeded` within 60 seconds.
- **Unmodified isolated LC-8 retry:** **1 failed, 1 teardown error** in
  **170.78 seconds**. Teardown's namespace read received a server timeout.

Both checkouts used Colima default (aarch64, 2 CPUs, 2 GiB, k3s 1.35), pinned
kubectl 1.35, and local Mock Workday running alongside the existing shared
workloads. No test timeout or VM resource setting was changed. Host load was
not controlled: its one-minute average was about 11 during the baseline and
rose to about 109 later in the comparison.

For the failed full-run LC-8 Pod, saved events show start at 17:40:59 UTC,
`DeadlineExceeded` at 17:41:53, and `Killing` at 17:42:29; its last captured
phase was still Running. This establishes delayed deadline/termination
observation, not its cause. The earlier happy-path and LC-6 cleanup failures
did not recur in either full run. **The baseline passed and the current
checkout failed: a checkpoint-2 regression has not been ruled out or fixed.**
Resource pressure is a hypothesis, not a verified explanation. Investigation
stopped within the requested cap; assertions were not relaxed.

Evidence is retained locally under `.local/s1/`: `baseline-bb4f05b.log`,
`baseline-bb4f05b-summary.json`, `baseline-environment.txt`, `baseline-evidence/`,
`current-comparison.log`, `current-comparison-summary.json`,
`current-lc8-isolated.log`, and `current-lc8-events.json`. The temporary worktree
was removed after exporting its sanitized evidence. No AWS resources changed.
