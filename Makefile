.PHONY: test test-integration run init-db

test:
	uv run pytest -m 'not integration'

test-integration:
	uv run pytest -m integration

init-db:
	uv run python -m agent_runtime.seed

run:
	uv run uvicorn agent_runtime.app:create_app --factory
