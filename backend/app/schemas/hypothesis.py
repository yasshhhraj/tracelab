import uuid
from typing import Literal

from pydantic import BaseModel, Field

from app.db.models import HypothesisStatus


class Hypothesis(BaseModel):
    """
    Structured output produced by an investigation agent.

    NOTE: `confidence` is INFORMATIONAL ONLY.
    The Arbiter (CP-08) MUST NOT use it for winner selection (AGENTS.md constraint).
    Selection is driven solely by evidence: reproduction + test proof.
    """

    hypothesis_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    agent_type: Literal["code_path", "git_history", "test_behavior"]
    summary: str  # ≤ 2 sentences, human-readable
    suspected_files: list[str]  # relative file paths
    reasoning_summary: str  # multi-sentence causal chain
    reproduction_plan: list[str]  # ordered steps to reproduce
    candidate_fix: str  # unified diff or prose description
    confidence: Literal["low", "medium", "high"]  # informational only — never used for selection
    status: str = HypothesisStatus.PROPOSED
