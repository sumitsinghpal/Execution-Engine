.PHONY: run test lint migrate

run:
	uvicorn src.api.server:app --reload --host 0.0.0.0 --port 8000

test:
	python -m pytest -q

lint:
	ruff check src tests && mypy src

migrate:
	alembic upgrade head
