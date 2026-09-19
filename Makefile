.PHONY: setup check test migrate up down smoke

setup:
	uv sync --frozen

check:
	uv run ruff format --check .
	uv run ruff check .
	uv run mypy src

test:
	uv run pytest -q

migrate:
	uv run alembic upgrade head

up:
	docker compose up --build -d

down:
	docker compose down

smoke:
	uv run tennis-platform health
