.PHONY: test test-integration run init-db

test:
	uv run pytest -m 'not integration and not k8s'

test-integration:
	uv run pytest -m integration

init-db:
	uv run python -m agent_runtime.seed

run:
	uv run uvicorn agent_runtime.app:create_app --factory

.PHONY: k8s-tools k8s-up k8s-token k8s-down agent-image

k8s-tools:
	uv run python -m scripts.k8s tools

k8s-up:
	uv run python -m scripts.k8s up

k8s-token:
	uv run python -m scripts.k8s token

k8s-down:
	uv run python -m scripts.k8s down

agent-image:
	uv run python -m scripts.k8s image

.PHONY: k8s-preflight
k8s-preflight:
	uv run python -m scripts.k8s_preflight

.PHONY: test-k8s
test-k8s: k8s-preflight
	uv run pytest -m k8s -x

# Checkpoint 3: prepare and review the saved plan. Mutations require checkpoint 4 approval.
.PHONY: s1-plan s1-up s1-test s1-down s1-leftovers
s1-plan:
	uv run python -m scripts.s1 plan

s1-up:
	uv run python -m scripts.s1 up

s1-test:
	uv run python -m scripts.s1 test

s1-down:
	uv run python -m scripts.s1 down

s1-leftovers:
	uv run python -m scripts.s1 leftovers

.PHONY: aws-cost
aws-cost:
	uv run python scripts/aws_cost.py
