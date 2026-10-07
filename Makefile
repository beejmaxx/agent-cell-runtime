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
	uv run python scripts/k8s.py tools

k8s-up:
	uv run python scripts/k8s.py up

k8s-token:
	uv run python scripts/k8s.py token

k8s-down:
	uv run python scripts/k8s.py down

agent-image:
	uv run python scripts/k8s.py image

.PHONY: k8s-preflight
k8s-preflight:
	uv run python scripts/k8s_preflight.py

.PHONY: test-k8s
test-k8s: k8s-preflight
	uv run pytest -m k8s -x
