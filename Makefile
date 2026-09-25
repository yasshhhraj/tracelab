.PHONY: dev test lint fmt build up down migrate clean

# ── Local development (SQLite, hot-reload) ────────────────────────────────────
dev:
	cd backend && uvicorn app.main:app --reload --port 8000

# ── Tests ─────────────────────────────────────────────────────────────────────
test:
	cd backend && pytest --cov=app --cov-report=term-missing -q

# ── Lint ──────────────────────────────────────────────────────────────────────
lint:
	cd backend && ruff check . && ruff format --check .

fmt:
	cd backend && ruff format .

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
