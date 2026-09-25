# TraceLab

Multi-agent differential debugging platform.

## Quickstart

```bash
cp .env.example .env
# fill in LLM_API_KEY at minimum

docker compose up
# → API:  http://localhost:8000
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

## Test

```bash
make test
```

## Architecture

See `docs/plans/IMPLEMENTATION_PLAN.md`.
