"""
Investigations router — CP-06

Endpoints:
    POST  /api/investigations                        create + trigger background task
    GET   /api/investigations                        list (paginated)
    GET   /api/investigations/{id}                   detail
    GET   /api/investigations/{id}/hypotheses        all hypotheses

Stubbed for CP-09:
    POST  /api/investigations/{id}/approve
    POST  /api/investigations/{id}/reject
    POST  /api/investigations/{id}/pull-request
    GET   /api/investigations/{id}/events

AGENTS.md constraint enforced here:
    POST /pull-request MUST check status == APPROVED — stub returns 501 now,
    full gate implemented in CP-09.
"""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db.models import Hypothesis as HypothesisORM
from app.db.models import Investigation as InvestigationORM
from app.db.models import InvestigationStatus
from app.db.session import AsyncSessionLocal, get_session
from app.orchestrator import run_investigation
from app.schemas.bug_context import BugContext
from app.schemas.investigation import (
    HypothesisResponse,
    InvestigationCreate,
    InvestigationDetailResponse,
    InvestigationListResponse,
    InvestigationResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/investigations", tags=["investigations"])


# ── POST /api/investigations ──────────────────────────────────────────────────


@router.post("", response_model=InvestigationResponse, status_code=201)
async def create_investigation(
    body: InvestigationCreate,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> InvestigationResponse:
    """
    Start a new investigation.

    Creates the Investigation row immediately and fires the orchestrator as a
    background task.  Returns the investigation in CREATED state without
    waiting for agents to complete.
    """
    repository = body.repository or settings.target_repository
    if not repository:
        raise HTTPException(status_code=422, detail="Repository is required")

    bug_context = BugContext(
        issue_id=body.jira_issue_id,
        symptom=body.symptom,
        expected=body.expected,
        actual=body.actual,
        affected_area=body.affected_area,
        error_type=body.error_type,
        known_evidence=body.known_evidence,
        stack_trace=body.stack_trace,
        repository=repository,
        base_branch=body.base_branch,
    )

    investigation = InvestigationORM(
        external_issue_id=body.jira_issue_id,
        repository=repository,
        base_branch=body.base_branch,
        status=InvestigationStatus.CREATED,
        bug_context=bug_context.model_dump(),
    )
    session.add(investigation)
    await session.commit()
    await session.refresh(investigation)

    # Fire background task — BackgroundTasks runs after response is sent
    background_tasks.add_task(run_investigation, investigation.id, AsyncSessionLocal)
    logger.info("Investigation %s created; background task queued.", investigation.id)

    return InvestigationResponse.model_validate(investigation)


# ── GET /api/investigations ───────────────────────────────────────────────────


@router.get("", response_model=InvestigationListResponse)
async def list_investigations(
    limit: int = 20,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
) -> InvestigationListResponse:
    """Return a paginated list of all investigations."""
    total_result = await session.execute(select(func.count()).select_from(InvestigationORM))
    total: int = total_result.scalar_one()

    rows_result = await session.execute(
        select(InvestigationORM)
        .order_by(InvestigationORM.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    items = [InvestigationResponse.model_validate(r) for r in rows_result.scalars().all()]
    return InvestigationListResponse(items=items, total=total, limit=limit, offset=offset)


# ── GET /api/investigations/{id} ──────────────────────────────────────────────


@router.get("/{investigation_id}", response_model=InvestigationDetailResponse)
async def get_investigation(
    investigation_id: str,
    session: AsyncSession = Depends(get_session),
) -> InvestigationDetailResponse:
    """Return a single investigation by ID."""
    inv = await session.get(
        InvestigationORM,
        investigation_id,
        options=[selectinload(InvestigationORM.diagnosis)],
    )
    if inv is None:
        raise HTTPException(status_code=404, detail="Investigation not found")
    return InvestigationDetailResponse.model_validate(inv)


# ── GET /api/investigations/{id}/hypotheses ───────────────────────────────────


@router.get("/{investigation_id}/hypotheses", response_model=list[HypothesisResponse])
async def list_hypotheses(
    investigation_id: str,
    session: AsyncSession = Depends(get_session),
) -> list[HypothesisResponse]:
    """Return all hypotheses for an investigation."""
    # Verify investigation exists
    inv = await session.get(InvestigationORM, investigation_id)
    if inv is None:
        raise HTTPException(status_code=404, detail="Investigation not found")

    result = await session.execute(
        select(HypothesisORM)
        .where(HypothesisORM.investigation_id == investigation_id)
        .order_by(HypothesisORM.created_at)
    )
    return [HypothesisResponse.model_validate(h) for h in result.scalars().all()]


# ── Stubs for CP-09 ───────────────────────────────────────────────────────────
# These endpoints are fully implemented in CP-09.
# POST /pull-request will enforce status == APPROVED (AGENTS.md constraint).


@router.post("/{investigation_id}/approve", status_code=501)
async def approve_investigation(investigation_id: str) -> dict:
    raise HTTPException(status_code=501, detail="Not implemented — coming in CP-09")


@router.post("/{investigation_id}/reject", status_code=501)
async def reject_investigation(investigation_id: str) -> dict:
    raise HTTPException(status_code=501, detail="Not implemented — coming in CP-09")


@router.post("/{investigation_id}/pull-request", status_code=501)
async def create_pull_request(investigation_id: str) -> dict:
    # AGENTS.md: must verify status == APPROVED before proceeding.
    # Full enforcement implemented in CP-09.
    raise HTTPException(status_code=501, detail="Not implemented — coming in CP-09")


@router.get("/{investigation_id}/events", status_code=501)
async def get_events(investigation_id: str) -> dict:
    raise HTTPException(status_code=501, detail="Not implemented — coming in CP-09")
