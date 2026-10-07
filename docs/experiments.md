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

## 2026-10-08 — S1 LC-8 commit comparison (inconclusive bisect)

The reviewer requested LC-8 alone at three commits, rebuilding the agent image
at each commit, with an approximately 30-minute diagnostic cap. Each detached
worktree used its own locked Python 3.12 environment and pinned kubectl. The
same Colima VM (2 CPUs, 2 GiB, shared workloads) stayed running throughout.
Neither the 30-second Pod deadline nor the 60-second assertion wait changed.

| Commit | Isolated LC-8 result | Pytest duration |
|---|---|---|
| `c21bc07` | 1 failed | 155.00 s |
| `c4dfd6f` | 1 failed | 102.25 s |
| `024b024` | 1 passed | 83.54 s |
| `bb4f05b`, additional baseline with read-only request/state tracing | 1 failed | 125.72 s |
| `c21bc07`, repeat with the same tracing | 1 passed | 75.52 s |

All three failures were at `tests/test_k8s.py:204`, waiting for phase `Failed`:
`Condition did not converge within 60 seconds`. They did not reach the
`DeadlineExceeded` reason assertion. The first `c21bc07` run saved no Pod
snapshot and had no new Pod events; its database state was not captured, so
the launch outcome is unknown. The added tracing only observes requests and
records selected database fields before teardown; it does not retry requests,
change reconciliation, or include credentials.

Verified Pod evidence (UTC):

- `c4dfd6f`, Pod `exec-081865bf-d839-404b-8872-5ea25b27be14`: start
  18:02:53, first deadline event 18:03:23, Killing 18:03:38. The last saved
  phase was `Pending`, with no top-level reason, although its container state
  was running and its readiness conditions said `PodFailed`. Deadline
  detection occurred at 30 seconds; terminal-phase observation did not
  converge before the assertion expired.
- `024b024`, Pod `exec-4dfa19e1-9dd1-4508-bc72-58e9d594b534`: start
  18:05:28, first deadline event 18:06:03, Killing 18:06:06, container exit
  18:06:11. The saved phase/reason was `Failed/DeadlineExceeded`; LC-8 passed.
- Baseline `bb4f05b`, Pod `exec-eb33cba6-32fa-4136-97ed-9306fc152f84`:
  start 18:08:35, first deadline event 18:09:42 (67 seconds later), Killing
  18:09:53. The last saved phase was `Running`, with no reason. Request tracing
  recorded a successful list (200), create (201), one create call, zero delete
  calls, and database state `RUNNING` at assertion failure. During cleanup,
  `FailedSync` reported a Docker container-status lookup for a missing container.
- The unchanged `c21bc07` repeat observed `Failed/DeadlineExceeded`, then
  transitioned the database to `TIMED_OUT` after restarting reconciliation.

The first failing commit in the requested order was `c21bc07`, but **it is not
an established first bad commit**: it also passed unchanged, the latest code
passed, and the pre-checkpoint baseline reproduced the failure. Kubelet logs
showed slow housekeeping and unrelated health-probe timeouts before testing.
At 18:08:30 UTC, the guest's one-minute load average was 11.44 and its CPU
pressure `some avg60` was 90.27%. These verify cluster delays and contention;
they do not isolate the cause of every failure or prove all checkpoint-2 code
free of regressions. No speculative runtime fix or timeout increase was made.

Local evidence: `.local/s1/bisect-summary.json`, `bisect-<commit>.log`,
`bisect-<commit>-events.json`, `bisect-evidence/`, `trace-summary.json`,
`trace-<commit>.log`, `trace-<commit>.json`, `trace-bb4f05b-events.json`,
`diagnostics/lc8_trace.py`, `bisect-kubelet-*.log`, and `bisect-pressure.txt`.

A final current-checkout image rebuild succeeded. `make test-k8s` then failed
in preflight after 45.61 seconds: `httpx.ReadTimeout` on the Pod-create POST at
`scripts/k8s_preflight.py:96`. **Zero suite tests ran; 18/18 has not been
re-established.** Its logs are `bisect-current-full.log` and
`bisect-current-full-summary.json`. Investigation stopped within the requested
cap without a root-cause fix. Checkpoint 2 remains incomplete; the approved
two-namespace harness-policy and pre-created control-namespace changes remain
pending the regression gate. There is no new spec ambiguity.

Cleanup verified an empty `agent-exec` namespace. The four temporary
worktrees were removed after exporting their sanitized evidence; the current
agent image was restored. No AWS resources were created or changed.

## 2026-10-08 — S1 checkpoint 2 code complete; EKS validation deferred to E1

The reviewer accepted the local Kubernetes failures as environmental and
instructed that Colima remain stopped. LC-8 and the other Kubernetes tests will
run on EKS in E1; a failure there remains a finding, not an assertion to relax.

Implemented the test-only `s1-harness` trust and EKS policy association, scoped
exactly to `agent-exec` and `agent-exec-psa-control`. The EKS harness uses that
role only for admin kubectl calls and separately refreshes controller tokens.
Operator setup pre-creates the unlabeled PSA control namespace and its service
account. ISO-5 never creates/deletes that namespace on EKS; LC-7 restores only
its namespaced RoleBinding. Added credential-path probes that retain no secret
values, profile guards, and unit coverage of the approved boundaries. The spec
and implementation record the same test-only rationale.

Validation: `make test` **169 passed, 22 deselected** (18 Kubernetes and four
HTTP integration tests), one dependency deprecation warning. Ruff check,
formatting and `git diff --check` passed. Mock Workday was unavailable, so its
four integration tests retain the earlier passing evidence, not a fresh run.
Colima was not started. Live EKS role assumption, namespace authorization and
Fargate lifecycle behavior remain unverified until E1. No AWS resources changed.
