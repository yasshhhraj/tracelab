# CP-01 — Repository Scaffold + CI Skeleton

> **Goal:** An empty-but-runnable project that passes linting and a trivial health-check test.  
> **Exit state:** `docker compose up` starts the FastAPI server; `pytest` reports 0 failures; `ruff check .` is clean.

---

## 1. Final Directory Layout

```
tracelab/
├── backend/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py                  # FastAPI app factory + lifespan
│   │   ├── config.py                # Pydantic Settings (env-driven, secrets never logged)
│   │   └── routers/
│   │       ├── __init__.py
│   │       └── health.py            # GET /health → { "status": "ok", "version": "..." }
│   ├── tests/
│   │   ├── __init__.py
│   │   ├── conftest.py              # shared async client fixture
│   │   └── test_health.py
│   ├── pyproject.toml
│   ├── .python-version              # pins 3.12
│   └── Dockerfile
├── frontend/                        # empty Next.js scaffold (no logic yet)
│   ├── package.json
│   └── app/
│       └── page.tsx                 # placeholder "TraceLab" landing page
├── docker-compose.yml
├── docker-compose.override.yml      # dev overrides (hot-reload, no restart policy)
├── .env.example                     # all required keys with placeholder values
├── .gitignore
├── Makefile                         # make dev / test / lint / build
└── README.md
```

---

## 2. File-by-File Specification

### 2.1 `backend/pyproject.toml`

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "tracelab-backend"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.111",
    "uvicorn[standard]>=0.29",
    "pydantic>=2.7",
    "pydantic-settings>=2.3",
    # CP-02 additions (declared now so later checkpoints don't change pyproject)
    "sqlalchemy[asyncio]>=2.0",
    "aiosqlite>=0.20",
    "alembic>=1.13",
    # CP-11 / CP-12 (declared now, unused until those CPs)
    "httpx>=0.27",
]

[project.optional-dependencies]
dev = [
    "pytest>=8",
    "pytest-asyncio>=0.23",
    "pytest-cov>=5",
    "ruff>=0.4",
    "httpx>=0.27",   # also test client
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]
ignore = []

[tool.coverage.run]
source = ["app"]
omit = ["tests/*"]
```

---

### 2.2 `backend/app/config.py`

```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Application
    app_name: str = "TraceLab"
    version: str = "0.1.0"
    environment: str = "development"   # development | staging | production

    # Database — never log this value
    database_url: str = "sqlite+aiosqlite:///./tracelab.db"

    # LLM — secret, never appears in logs or prompts
    llm_api_key: str = ""
    llm_model: str = "gpt-4o"
    llm_base_url: str = "https://api.openai.com/v1"

    # External integrations — secrets, env-only
    jira_base_url: str = ""
    jira_api_token: str = ""
    github_token: str = ""

    # Resource limits (used by later CPs)
    max_hypotheses: int = 3
    max_patch_attempts: int = 2
    max_command_timeout_seconds: int = 120
    flaky_default_runs: int = 50
    flaky_min_runs: int = 20
    flaky_max_runs: int = 100


# Module-level singleton — import this everywhere
settings = Settings()
```

**Rule:** `settings.llm_api_key`, `settings.jira_api_token`, `settings.github_token` must never be passed to an LLM prompt or written to a log. Any logging of `settings` must exclude secret fields.

---

### 2.3 `backend/app/routers/health.py`

```python
from fastapi import APIRouter
from pydantic import BaseModel

from app.config import settings

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    return HealthResponse(
        status="ok",
        version=settings.version,
        environment=settings.environment,
    )
```

---

### 2.4 `backend/app/main.py`

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers.health import router as health_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    print(f"🔬 {settings.app_name} v{settings.version} [{settings.environment}] starting...")
    yield
    # Shutdown
    print(f"🔬 {settings.app_name} shutting down.")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=settings.version,
        lifespan=lifespan,
        docs_url="/docs" if settings.environment != "production" else None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000"],   # Next.js dev server
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router)

    return app


app = create_app()
```

---

### 2.5 `backend/tests/conftest.py`

```python
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest_asyncio.fixture
async def client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac
```

---

### 2.6 `backend/tests/test_health.py`

```python
import pytest


@pytest.mark.asyncio
async def test_health_returns_ok(client):
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "version" in body
    assert "environment" in body


@pytest.mark.asyncio
async def test_health_version_matches_config(client):
    from app.config import settings
    response = await client.get("/health")
    assert response.json()["version"] == settings.version
```

---

### 2.7 `backend/Dockerfile`

```dockerfile
# ── Stage 1: builder ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir hatchling && \
    pip install --no-cache-dir ".[dev]" 2>/dev/null || \
    pip install --no-cache-dir .

# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# git is needed by later CPs (worktree manager)
RUN apt-get update && apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.12 /usr/local/lib/python3.12
COPY --from=builder /usr/local/bin /usr/local/bin
COPY app/ ./app/

# Never bake secrets into the image
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ENVIRONMENT=production

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

---

### 2.8 `docker-compose.yml`

```yaml
services:
  db:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_DB: tracelab
      POSTGRES_USER: tracelab
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-tracelab_dev}
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U tracelab"]
      interval: 5s
      timeout: 5s
      retries: 5

  api:
    build:
      context: ./backend
      target: runtime
    restart: unless-stopped
    ports:
      - "8000:8000"
    environment:
      DATABASE_URL: postgresql+asyncpg://tracelab:${POSTGRES_PASSWORD:-tracelab_dev}@db:5432/tracelab
      ENVIRONMENT: ${ENVIRONMENT:-development}
      LLM_API_KEY: ${LLM_API_KEY:-}
      LLM_MODEL: ${LLM_MODEL:-gpt-4o}
      JIRA_BASE_URL: ${JIRA_BASE_URL:-}
      JIRA_API_TOKEN: ${JIRA_API_TOKEN:-}
      GITHUB_TOKEN: ${GITHUB_TOKEN:-}
    depends_on:
      db:
        condition: service_healthy

volumes:
  postgres_data:
```

---

### 2.9 `docker-compose.override.yml` (dev only, git-ignored from prod)

```yaml
services:
  api:
    build:
      target: builder   # use builder stage with dev deps
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
    volumes:
      - ./backend/app:/app/app   # hot-reload source mount
    environment:
      ENVIRONMENT: development
      DATABASE_URL: sqlite+aiosqlite:///./tracelab_dev.db
```

---

### 2.10 `.env.example`

```dotenv
# Copy to .env and fill in real values.
# NEVER commit .env to version control.

# Application
ENVIRONMENT=development

# Database (used by docker-compose; ignored in SQLite dev mode)
POSTGRES_PASSWORD=change_me

# LLM provider — secret, never logged
LLM_API_KEY=sk-...
LLM_MODEL=gpt-4o
LLM_BASE_URL=https://api.openai.com/v1

# Jira — secret, never logged
JIRA_BASE_URL=https://your-org.atlassian.net
JIRA_API_TOKEN=...

# GitHub — secret, never logged
GITHUB_TOKEN=ghp_...
```

---

### 2.11 `Makefile`

```makefile
.PHONY: dev test lint build migrate clean

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
```

---

### 2.12 `frontend/` — Placeholder Scaffold

```bash
# Run once to scaffold (not checked in as code here, just the init command)
cd frontend && npx create-next-app@latest . --typescript --tailwind --app --no-src-dir --import-alias "@/*"
```

`frontend/app/page.tsx` — minimal placeholder:

```tsx
export default function Home() {
  return (
    <main className="flex min-h-screen items-center justify-center">
      <h1 className="text-3xl font-bold">TraceLab</h1>
      <p className="ml-4 text-gray-500">Multi-agent differential debugging platform</p>
    </main>
  );
}
```

No logic, no API calls — those come in CP-10.

---

### 2.13 `README.md`

````markdown
# TraceLab

Multi-agent differential debugging platform.

## Quickstart

```bash
cp .env.example .env
# fill in LLM_API_KEY at minimum

docker compose up
# → API: http://localhost:8000
# → Docs: http://localhost:8000/docs
```

## Local dev (no Docker)

```bash
cd backend
pip install -e ".[dev]"
uvicorn app.main:app --reload
pytest
```

## Lint

```bash
make lint
```

## Architecture

See `docs/plans/IMPLEMENTATION_PLAN.md`.
````

---

## 3. Implementation Order

Execute these steps in sequence. Each step leaves the repo in a runnable state.

| Step | Action | Verify with |
|------|--------|-------------|
| 1 | Create `backend/pyproject.toml` | `pip install -e ".[dev]"` succeeds |
| 2 | Create `backend/app/__init__.py`, `config.py`, `routers/__init__.py`, `routers/health.py` | `python -c "from app.routers.health import router"` |
| 3 | Create `backend/app/main.py` | `uvicorn app.main:app` starts without error |
| 4 | Create `backend/tests/conftest.py` + `test_health.py` | `pytest` → 2 tests pass |
| 5 | Create `backend/.python-version` (`3.12`) | `python --version` = 3.12 |
| 6 | Run `ruff check backend/` | Zero errors |
| 7 | Create `backend/Dockerfile` | `docker build ./backend` succeeds |
| 8 | Create `docker-compose.yml` + `docker-compose.override.yml` | `docker compose config` valid |
| 9 | Create `.env.example`, `.gitignore` | `.env` listed in `.gitignore` |
| 10 | Scaffold `frontend/` (Next.js init) | `npm run build` in frontend succeeds |
| 11 | Create `Makefile` | `make test lint` both pass |
| 12 | Create `README.md` | — |

---

## 4. `.gitignore` Entries Required

```gitignore
# Secrets
.env
*.env

# Python
__pycache__/
*.pyc
*.pyo
.venv/
dist/
*.egg-info/
.coverage
htmlcov/

# SQLite dev DB
*.db

# Node
frontend/node_modules/
frontend/.next/

# Docker
docker-compose.override.yml   # optional: keep local overrides out of repo

# OS
.DS_Store
```

---

## 5. Acceptance Criteria (Definition of Done)

All of the following must be true before CP-01 is considered complete:

```
[ ] pytest backend/ → 2 tests, 0 failures, 0 errors
[ ] ruff check backend/ → exit 0 (no lint violations)
[ ] ruff format --check backend/ → exit 0 (formatting clean)
[ ] docker compose build → exit 0
[ ] docker compose up (detached) → GET http://localhost:8000/health → 200 { "status": "ok" }
[ ] GET http://localhost:8000/docs → 200 (Swagger UI, dev mode only)
[ ] .env not tracked by git (in .gitignore, absent from git status)
[ ] LLM_API_KEY, JIRA_API_TOKEN, GITHUB_TOKEN have no default values in code (empty string only from env)
[ ] frontend/ scaffolded → npm run build exits 0
[ ] make test, make lint both exit 0
```

---

## 6. What CP-01 Deliberately Does NOT Include

These are out of scope and will be added in later checkpoints:

| Excluded | Added in |
|----------|----------|
| Database models / Alembic | CP-02 |
| Any tool execution code | CP-03 |
| Git worktree logic | CP-04 |
| Any agent or LLM calls | CP-05 |
| `/api/investigations` route | CP-06 |
| Frontend pages beyond placeholder | CP-10 |
| Jira or GitHub clients | CP-11 / CP-12 |

The `pyproject.toml` declares all future dependencies upfront so later checkpoints only add code, never change the dependency manifest.
