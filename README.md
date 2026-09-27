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

## Seeded end-to-end demo

```bash
make demo
```

The command creates a temporary Python repository with a repeatable review-run
race and a temporary SQLite database. It runs the three agents with scripted
model responses, proves a fail-before/pass-after fix, exercises approval, and
returns a **mock** draft-PR URL. It does not use Jira, GitHub, Loreforge, or
live credentials. The printed report includes the selected diagnosis, patch
files, test evidence, and duration. A nonzero exit means an assertion failed.

`make benchmark` runs ten isolated seeded bugs through the real verification
engine and arbiter with scripted model responses. It writes a JSON report to
`backend/output/cp13-benchmark.json`, including failures and the 50-run flaky
proof. This measures pipeline correctness, not live-model diagnosis quality.

## Architecture

See `docs/plans/IMPLEMENTATION_PLAN.md`.

For the AWS CLI deployment sequence and external service setup, see
[`docs/plans/AWS-CLI-MVP-DEPLOYMENT.md`](docs/plans/AWS-CLI-MVP-DEPLOYMENT.md).
