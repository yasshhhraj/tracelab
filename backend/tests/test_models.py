import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models import (
    AgentEvent,  # noqa: F401
    Diagnosis,  # noqa: F401
    Experiment,  # noqa: F401
    Hypothesis,
    HypothesisStatus,
    Investigation,
    InvestigationStatus,
    Patch,  # noqa: F401
)

# Aliased to avoid pytest treating it as a test class (name starts with "Test")
from app.db.models import TestEvidence as EvidenceModel

# ── in-memory test engine ─────────────────────────────────────────────────────

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


# ── helpers ───────────────────────────────────────────────────────────────────


async def make_investigation(session: AsyncSession, **kwargs) -> Investigation:
    inv = Investigation(
        external_issue_id=kwargs.get("external_issue_id", "PVS-421"),
        repository=kwargs.get("repository", "github.com/org/repo"),
        base_branch=kwargs.get("base_branch", "main"),
        status=kwargs.get("status", InvestigationStatus.CREATED),
        bug_context=kwargs.get("bug_context", {"symptom": "duplicate rows"}),
    )
    session.add(inv)
    await session.commit()
    await session.refresh(inv)
    return inv


async def make_hypothesis(session: AsyncSession, investigation_id: str, **kwargs) -> Hypothesis:
    h = Hypothesis(
        investigation_id=investigation_id,
        agent_type=kwargs.get("agent_type", "code_path"),
        summary=kwargs.get("summary", "Race condition in persistence layer"),
        reasoning_summary="Two requests pass the idempotency check concurrently.",
        candidate_fix="Add unique constraint and handle conflict.",
        suspected_files=["review_service.py"],
        reproduction_plan=["Send two concurrent requests"],
        status=HypothesisStatus.PROPOSED,
        confidence="medium",
    )
    session.add(h)
    await session.commit()
    await session.refresh(h)
    return h


# ── tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_investigation_create_and_retrieve(session):
    inv = await make_investigation(session)
    assert inv.id is not None
    assert inv.status == InvestigationStatus.CREATED
    assert inv.bug_context["symptom"] == "duplicate rows"
    assert inv.created_at is not None
    assert inv.completed_at is None


@pytest.mark.asyncio
async def test_hypothesis_links_to_investigation(session):
    inv = await make_investigation(session)
    h = await make_hypothesis(session, inv.id)
    assert h.investigation_id == inv.id
    assert h.status == HypothesisStatus.PROPOSED
    assert h.confidence == "medium"
    assert h.patch_attempts == 0


@pytest.mark.asyncio
async def test_experiment_links_to_hypothesis(session):
    inv = await make_investigation(session)
    h = await make_hypothesis(session, inv.id)
    exp = Experiment(
        hypothesis_id=h.id,
        command="pytest tests/ -x",
        working_directory="/worktrees/h1",
        exit_code=1,
        stdout="FAILED test_duplicate_run",
        stderr="",
        duration_ms=3200,
    )
    session.add(exp)
    await session.commit()
    await session.refresh(exp)
    assert exp.hypothesis_id == h.id
    assert exp.exit_code == 1
    assert exp.timed_out is False


@pytest.mark.asyncio
async def test_test_evidence_flaky_fields(session):
    inv = await make_investigation(session)
    h = await make_hypothesis(session, inv.id)
    ev = EvidenceModel(
        hypothesis_id=h.id,
        test_path="tests/test_idempotency.py",
        pre_fix_result="FAIL",
        post_fix_result="PASS",
        existing_suite_result="PASS",
        runs=50,
        failures_before=11,
        failures_after=0,
    )
    session.add(ev)
    await session.commit()
    await session.refresh(ev)
    assert ev.pre_fix_result == "FAIL"
    assert ev.post_fix_result == "PASS"
    assert ev.runs == 50
    assert ev.failures_before == 11
    assert ev.failures_after == 0


@pytest.mark.asyncio
async def test_patch_stores_diff(session):
    inv = await make_investigation(session)
    h = await make_hypothesis(session, inv.id)
    patch = Patch(
        hypothesis_id=h.id,
        branch="ai-debug/PVS-421-h2",
        commit_sha="abc123",
        diff="--- a/review_service.py\n+++ b/review_service.py\n...",
        files_changed=["review_service.py"],
    )
    session.add(patch)
    await session.commit()
    await session.refresh(patch)
    assert patch.branch == "ai-debug/PVS-421-h2"
    assert "review_service.py" in patch.files_changed
    assert patch.pr_url is None


@pytest.mark.asyncio
async def test_diagnosis_links_to_investigation(session):
    inv = await make_investigation(session)
    h = await make_hypothesis(session, inv.id)
    diag = Diagnosis(
        investigation_id=inv.id,
        selected_hypothesis_id=h.id,
        summary="Race condition confirmed.",
        verified_cause="Idempotency check runs outside transaction.",
        evidence=["Bug reproduced", "Regression test fails before patch"],
        rejected_hypotheses=[{"hypothesis_id": "other", "rejection_reason": "Could not reproduce"}],
        changed_files=["review_service.py"],
        risk="low",
        recommended_action="Review candidate patch and create PR.",
    )
    session.add(diag)
    await session.commit()
    await session.refresh(diag)
    assert diag.selected_hypothesis_id == h.id
    assert diag.risk == "low"
    assert len(diag.evidence) == 2


@pytest.mark.asyncio
async def test_agent_event_stores_payload(session):
    inv = await make_investigation(session)
    ev = AgentEvent(
        investigation_id=inv.id,
        agent="git-history",
        action="git_log",
        target="review_service.py",
        payload={"commits_found": 3},
    )
    session.add(ev)
    await session.commit()
    await session.refresh(ev)
    assert ev.agent == "git-history"
    assert ev.payload["commits_found"] == 3
    assert ev.timestamp is not None


@pytest.mark.asyncio
async def test_all_seven_tables_exist(session):
    """Confirm all tables are present in the schema."""
    from sqlalchemy import text

    result = await session.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    )
    tables = {row[0] for row in result.fetchall()}
    expected = {
        "investigations",
        "hypotheses",
        "experiments",
        "test_evidence",
        "patches",
        "diagnoses",
        "agent_events",
    }
    assert expected.issubset(tables), f"Missing tables: {expected - tables}"
