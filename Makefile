.PHONY: install fmt lint type test test-unit test-int eval-fixtures eval-dataset mcp up down logs api worker migrate revision seed demo ci

install:
	uv sync --all-groups

fmt:
	uv run ruff format .
	uv run ruff check --fix .

lint:
	uv run ruff check .
	uv run ruff format --check .

type:
	uv run mypy

test-unit:
	uv run pytest -m unit

test-int:
	uv run pytest -m integration

test:
	uv run pytest -m "unit or integration"

eval-fixtures:
	uv run python -m evals.run --mode fixture --config naive --dataset v1 --check
	uv run python -m evals.run --mode fixture --config schema_retry --dataset v1 --check
	uv run python -m evals.run --mode fixture --config validation --dataset v1 --check

eval-dataset:
	uv run python -m evals.generator --dataset v1
	uv run python -m evals.bootstrap_fixtures --dataset v1

mcp:
	uv run python -m app.mcp_server

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f --tail=100

api:
	uv run uvicorn app.main:app --reload --port 8000

worker:
	uv run arq app.worker.WorkerSettings --watch app

migrate:
	uv run alembic upgrade head

revision:
	uv run alembic revision --autogenerate -m "$(m)"

seed:
	uv run python -m seed.seed

demo:
	bash scripts/demo.sh

ci: lint type test
