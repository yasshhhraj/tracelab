.PHONY: dev test lint fmt build up down migrate clean demo benchmark

BACKEND_PYTHON := $(if $(wildcard backend/.venv/bin/python),.venv/bin/python,python)
BACKEND_PYTEST := $(if $(wildcard backend/.venv/bin/pytest),.venv/bin/pytest,pytest)
BACKEND_RUFF := $(if $(wildcard backend/.venv/bin/ruff),.venv/bin/ruff,ruff)

demo:
	cd backend && $(BACKEND_PYTHON) ../scripts/run_demo.py

benchmark:
	cd backend && $(BACKEND_PYTHON) ../scripts/run_benchmark.py

# ── Local development (SQLite, hot-reload) ────────────────────────────────────
dev:
	cd backend && uvicorn app.main:app --reload --port 8000

# ── Tests ─────────────────────────────────────────────────────────────────────
test:
	cd backend && $(BACKEND_PYTEST) --cov=app --cov-report=term-missing -q

# ── Lint ──────────────────────────────────────────────────────────────────────
lint:
	cd backend && $(BACKEND_RUFF) check . && $(BACKEND_RUFF) format --check .

fmt:
	cd backend && $(BACKEND_RUFF) format .

# ── Docker ────────────────────────────────────────────────────────────────────
build:
	docker compose build

up:
	docker compose up

down:
	docker compose down

# ── Database (added in CP-02) ─────────────────────────────────────────────────
migrate:
	cd backend && alembic upgrade head

# ── Clean ─────────────────────────────────────────────────────────────────────
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete
	rm -f backend/tracelab_dev.db
