import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

# ── helpers ───────────────────────────────────────────────────────────────────


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


# ── status / type constants ───────────────────────────────────────────────────


class InvestigationStatus:
    CREATED = "CREATED"
    CONTEXT_LOADING = "CONTEXT_LOADING"
    INVESTIGATING = "INVESTIGATING"
    VERIFYING = "VERIFYING"
    ARBITRATING = "ARBITRATING"
    WAITING_FOR_REVIEW = "WAITING_FOR_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class HypothesisStatus:
    PROPOSED = "PROPOSED"
    REPRODUCING = "REPRODUCING"
    PATCHING = "PATCHING"
    VERIFYING = "VERIFYING"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    INCONCLUSIVE = "INCONCLUSIVE"
    BLOCKED = "BLOCKED"


class AgentType:
    CODE_PATH = "code_path"
    GIT_HISTORY = "git_history"
    TEST_BEHAVIOR = "test_behavior"
    INTAKE = "intake"
    ARBITER = "arbiter"


# ── models ────────────────────────────────────────────────────────────────────


class Investigation(Base):
    __tablename__ = "investigations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    external_issue_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    repository: Mapped[str] = mapped_column(String(512), nullable=False)
    base_branch: Mapped[str] = mapped_column(String(256), nullable=False, default="main")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=InvestigationStatus.CREATED
    )
    # Full BugContext stored as JSON blob
    bug_context: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # relationships
    hypotheses: Mapped[list["Hypothesis"]] = relationship(back_populates="investigation")
    diagnosis: Mapped["Diagnosis | None"] = relationship(
        back_populates="investigation", uselist=False
    )
    agent_events: Mapped[list["AgentEvent"]] = relationship(back_populates="investigation")


class Hypothesis(Base):
    __tablename__ = "hypotheses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    investigation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("investigations.id"), nullable=False, index=True
    )
    agent_type: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reasoning_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    candidate_fix: Mapped[str] = mapped_column(Text, nullable=False, default="")
    suspected_files: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    reproduction_plan: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=HypothesisStatus.PROPOSED
    )
    # informational only — must NOT be used for selection logic (AGENTS.md)
    confidence: Mapped[str] = mapped_column(String(16), nullable=False, default="medium")
    patch_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    # relationships
    investigation: Mapped["Investigation"] = relationship(back_populates="hypotheses")
    experiments: Mapped[list["Experiment"]] = relationship(back_populates="hypothesis")
    test_evidence: Mapped[list["TestEvidence"]] = relationship(back_populates="hypothesis")
    patches: Mapped[list["Patch"]] = relationship(back_populates="hypothesis")


class Experiment(Base):
    __tablename__ = "experiments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    hypothesis_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("hypotheses.id"), nullable=False, index=True
    )
    command: Mapped[str] = mapped_column(Text, nullable=False)
    working_directory: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stdout: Mapped[str] = mapped_column(Text, nullable=False, default="")
    stderr: Mapped[str] = mapped_column(Text, nullable=False, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    timed_out: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    hypothesis: Mapped["Hypothesis"] = relationship(back_populates="experiments")


class TestEvidence(Base):
    __tablename__ = "test_evidence"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    hypothesis_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("hypotheses.id"), nullable=False, index=True
    )
    test_path: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    # "PASS" | "FAIL" | "ERROR" | "SKIP" | None
    pre_fix_result: Mapped[str | None] = mapped_column(String(16), nullable=True)
    post_fix_result: Mapped[str | None] = mapped_column(String(16), nullable=True)
    existing_suite_result: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # for flaky bug tracking (AGENTS.md: min 20 runs)
    runs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failures_before: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failures_after: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    hypothesis: Mapped["Hypothesis"] = relationship(back_populates="test_evidence")


class Patch(Base):
    __tablename__ = "patches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    hypothesis_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("hypotheses.id"), nullable=False, index=True
    )
    branch: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    diff: Mapped[str] = mapped_column(Text, nullable=False, default="")
    files_changed: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    pr_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    hypothesis: Mapped["Hypothesis"] = relationship(back_populates="patches")


class Diagnosis(Base):
    __tablename__ = "diagnoses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    investigation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("investigations.id"), nullable=False, unique=True
    )
    # None when no hypothesis was verified
    selected_hypothesis_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("hypotheses.id"), nullable=True
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    verified_cause: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # List of evidence bullet strings
    evidence: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    # List of {"hypothesis_id": ..., "summary": ..., "rejection_reason": ...}
    rejected_hypotheses: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    changed_files: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    risk: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    recommended_action: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    investigation: Mapped["Investigation"] = relationship(back_populates="diagnosis")


class AgentEvent(Base):
    __tablename__ = "agent_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    investigation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("investigations.id"), nullable=False, index=True
    )
    agent: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    target: Mapped[str | None] = mapped_column(String(512), nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)

    investigation: Mapped["Investigation"] = relationship(back_populates="agent_events")
