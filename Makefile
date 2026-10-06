# Receipt AI - Makefile

.PHONY: install dev test lint format run clean

# Install dependencies
install:
	python -m pip install -r requirements.txt

# Install dev dependencies
dev:
	python -m pip install -r requirements.txt
	python -m pip install black ruff pytest pytest-asyncio httpx

# Run tests
test:
	python -m pytest tests/ -v

# Lint code
lint:
	python -m ruff check app/ tests/
	python -m ruff format --check app/ tests/

# Format code
format:
	python -m ruff format app/ tests/
	python -m ruff check --fix app/ tests/

# Run development server
run:
	python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Run production server
run-prod:
	python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4

# CLI commands
classify:
	python cli.py classify $(FILE)

extract:
	python cli.py extract $(FILE)

extract-json:
	python cli.py extract $(FILE) --json

# Docker
docker-build:
	docker build -t receipt-ai .

docker-run:
	docker run -p 8000:8000 --env-file .env receipt-ai

# Clean
clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	rm -rf .pytest_cache .ruff_cache

# Help
help:
	@echo "Receipt AI - Available commands:"
	@echo ""
	@echo "  make install      Install dependencies"
	@echo "  make dev          Install with dev dependencies"
	@echo "  make test         Run tests"
	@echo "  make lint         Check code style"
	@echo "  make format       Auto-format code"
	@echo "  make run          Run dev server"
	@echo "  make run-prod     Run production server"
	@echo ""
	@echo "  make classify FILE=receipt.jpg    Classify a document"
	@echo "  make extract FILE=receipt.jpg     Extract data from document"
	@echo "  make extract-json FILE=doc.pdf    Extract as JSON"
	@echo ""
	@echo "  make docker-build Build Docker image"
	@echo "  make docker-run   Run in Docker"