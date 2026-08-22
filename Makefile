.PHONY: help install dev run test lint format migrate clean docker-up docker-down

help:
	@echo "EDGE-Execution - Development Commands"
	@echo ""
	@echo "Usage: make [target]"
	@echo ""
	@echo "Targets:"
	@echo "  install       Install development dependencies"
	@echo "  dev          Run in development mode with hot reload"
	@echo "  run          Run production server"
	@echo "  test         Run test suite"
	@echo "  test-cov     Run tests with coverage report"
	@echo "  lint         Run linters (ruff, mypy)"
	@echo "  format       Auto-format code (ruff, black)"
	@echo "  clean        Remove build artifacts and cache"
	@echo "  docker-up    Start services with Docker Compose"
	@echo "  docker-down  Stop Docker Compose services"
	@echo ""

install:
	pip install -e ".[dev]"

dev:
	python -m uvicorn src.api.server:app --host 0.0.0.0 --port 8000 --reload

run:
	python -m uvicorn src.api.server:app --host 0.0.0.0 --port 8000

test:
	pytest tests/ -v

test-cov:
	pytest tests/ -v --cov=src --cov-report=html --cov-report=term-missing

lint:
	@echo "Running ruff..."
	ruff check src/ tests/
	@echo "Running mypy..."
	mypy src/

format:
	@echo "Formatting with ruff..."
	ruff check --fix src/ tests/
	@echo "Formatting with black..."
	black src/ tests/

clean:
	find . -type f -name '*.pyc' -delete
	find . -type d -name '__pycache__' -delete
	find . -type d -name '.pytest_cache' -delete
	find . -type d -name '.mypy_cache' -delete
	find . -type d -name '.ruff_cache' -delete
	find . -type d -name '*.egg-info' -delete
	find . -type d -name 'htmlcov' -delete
	rm -f execution_engine.db

docker-up:
	docker-compose up -d

docker-down:
	docker-compose down

docker-logs:
	docker-compose logs -f app

# Development workflow shortcuts
setup: install lint test
	@echo "✓ Setup complete! Run 'make dev' to start development server."

ci: lint test
	@echo "✓ CI checks passed!"

# Database commands
migrate:
	alembic upgrade head

migrate-new:
	alembic revision --autogenerate

migrate-downgrade:
	alembic downgrade -1
