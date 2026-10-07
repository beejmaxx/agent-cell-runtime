# Agent Cell Runtime

A hands-on exploration of infrastructure for securely executing AI agents: Python control services, Kubernetes workloads, deterministic authorization, AWS identity, and distributed failure recovery.

**Status:** design and learning notes only. No runtime has been implemented or deployed, and no security guarantees have been demonstrated yet.

The goal is to build and understand this path:

```text
submit execution
      |
      v
execution controller -> durable execution state
      |
      v
isolated workload -> trusted mediation -> data / tools / models
      |                     |
      +------ telemetry and audit ------+
      |
      v
reconcile completion and clean up resources
```

An **Agent Cell** is our logical execution boundary. Its implementation is an open design question. A Pod with a maincar and sidecar is one experiment, not proof of isolation.

## What we want to learn

- Write clear Python and reason about async work, cancellation, bounded concurrency, and shared state.
- Understand Kubernetes by creating, observing, and deleting workloads through its API, and by building a small kubeadm cluster.
- Make authorization unavoidable, bind requests to authenticated execution identity, and isolate tenant data.
- Recover from partial failure without blindly repeating consequential operations.
- Use AWS workload identity, temporary credentials, cross-account access, and controlled networking.
- Reconstruct execution behavior through metrics, traces, and durable audit records.

The model proposes actions. Trusted software decides which actions may occur, and the execution environment must prevent bypassing that software.

## Start here

1. [Architecture and open decisions](docs/architecture.md)
2. [Learning roadmap](docs/roadmap.md)
3. [Experiment log](docs/experiments.md)
4. [Contributing and learning approach](CONTRIBUTING.md)

The first implementation milestone will be a Python controller with durable execution records and a local fake agent. It will exercise lifecycle and retry semantics; it will not establish a sandbox for hostile code.

This is an independent educational project. It does not describe or reproduce any company's proprietary architecture. Use synthetic data for experiments.

## License

[MIT](LICENSE)
