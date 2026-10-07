# Contributing

This project prioritizes understanding mechanisms and demonstrating claims. Small, explainable changes with reproducible evidence are welcome.

- Describe the problem, invariant, and tradeoffs before adding infrastructure or abstractions.
- Distinguish implemented behavior, proposals, observations, and hypotheses.
- Link primary documentation for external technical claims.
- Keep examples and experiments reproducible, using synthetic tenant and employee data.
- Include meaningful checks for changed lifecycle, policy, concurrency, and failure semantics.
- Record experiments and recoveries in [the experiment log](docs/experiments.md).
- Keep credentials, Terraform state, kubeconfigs, personal interview details, and private customer information out of the repository.

When using an AI assistant, use it to explain mechanisms, challenge assumptions, review security boundaries, and help implement scoped changes. Preserve the learner's ownership of architectural decisions and understanding of the resulting code.
