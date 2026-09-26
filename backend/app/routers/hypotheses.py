"""
Hypotheses router — CP-09

Endpoints:
    GET  /api/hypotheses/{id}/evidence   experiments + test_evidence + patches
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Hypothesis as HypothesisORM
from app.db.session import get_session
from app.schemas.investigation import (
    ExperimentResponse,
    HypothesisEvidenceResponse,
    HypothesisResponse,
    PatchResponse,
    TestEvidenceResponse,
)

router = APIRouter(prefix="/api/hypotheses", tags=["hypotheses"])


# ── GET /api/hypotheses/{id}/evidence ─────────────────────────────────────────


@router.get("/{hypothesis_id}/evidence", response_model=HypothesisEvidenceResponse)
async def get_hypothesis_evidence(
    hypothesis_id: str,
    session: AsyncSession = Depends(get_session),
) -> HypothesisEvidenceResponse:
    """
    Return experiments, test_evidence, and patches for a single hypothesis.

    Provides all evidence collected during the investigation and verification
    phases for one hypothesis, enabling the frontend Evidence Panel.
    """
    hyp = await session.get(
        HypothesisORM,
        hypothesis_id,
        options=[
            selectinload(HypothesisORM.experiments),
            selectinload(HypothesisORM.test_evidence),
            selectinload(HypothesisORM.patches),
        ],
    )
    if hyp is None:
        raise HTTPException(status_code=404, detail="Hypothesis not found")

    return HypothesisEvidenceResponse(
        hypothesis=HypothesisResponse.model_validate(hyp),
        experiments=[ExperimentResponse.model_validate(e) for e in hyp.experiments],
        test_evidence=[TestEvidenceResponse.model_validate(t) for t in hyp.test_evidence],
        patches=[PatchResponse.model_validate(p) for p in hyp.patches],
    )
