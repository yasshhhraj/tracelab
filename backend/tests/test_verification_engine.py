"""
Tests for CP-07: VerificationEngine.

Uses the seeded demo repo in tests/fixtures/demo_repo/ — copied fresh into
tmp_path per test so each test has an isolated, clean git repo.

All LLM calls (RegressionTestGenerator) are mocked with pre-written test
content from the fixture, so tests are deterministic and fast.
"""

import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.db.models import (
    Experiment,
    Hypothesis,
    HypothesisStatus,
    Investigation,
    InvestigationStatus,
    TestEvidence,
)
from app.schemas.bug_context import BugContext
from app.verification.engine import VerificationEngine

# ── fixture path ──────────────────────────────────────────────────────────────

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "demo_repo"


# ── helpers ───────────────────────────────────────────────────────────────────


def _init_git_repo(path: Path) -> None:
    """Initialise a git repo with one commit so git apply works."""
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@tracelab.test"],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "TraceLab Test"],
        capture_output=True,
        check=True,
    )
    subprocess.run(["git", "-C", str(path), "add", "."], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "initial"],
        capture_output=True,
        check=True,
    )


@pytest.fixture
def demo_repo(tmp_path: Path) -> Path:
    """
    Copy the demo repo fixture into tmp_path and initialise a git repo.
    Returns the path to the fresh isolated repo.
    """
    repo_path = tmp_path / "demo_repo"
    shutil.copytree(_FIXTURE_DIR, repo_path)
    _init_git_repo(repo_path)
    return repo_path


def _read_fixture_constant(name: str) -> str:
    """Import a string constant from fixture_constants.py."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "fixture_constants", _FIXTURE_DIR / "fixture_constants.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return getattr(mod, name)


CORRECT_PATCH: str = _read_fixture_constant("CORRECT_PATCH")
WRONG_PATCH: str = _read_fixture_constant("WRONG_PATCH")
INVALID_PATCH: str = _read_fixture_constant("INVALID_PATCH")
REGRESSION_TEST_PATH: str = _read_fixture_constant("REGRESSION_TEST_PATH")


async def _seed_hypothesis(
    session_factory,
    investigation_id: str,
    *,
    candidate_fix: str,
    reproduction_plan: list[str] | None = None,
    agent_type: str = "code_path",
    suspected_files: list[str] | None = None,
) -> str:
    """Insert a PROPOSED Hypothesis row, return its id."""
    async with session_factory() as session:
        hyp = Hypothesis(
            investigation_id=investigation_id,
            agent_type=agent_type,
            summary="Test hypothesis",
            reasoning_summary="For testing",
            candidate_fix=candidate_fix,
            suspected_files=suspected_files or ["review_service.py"],
            reproduction_plan=reproduction_plan or [REGRESSION_TEST_PATH],
            confidence="medium",
            status=HypothesisStatus.PROPOSED,
        )
        session.add(hyp)
        await session.commit()
        await session.refresh(hyp)
        return hyp.id


async def _seed_investigation(session_factory) -> str:
    """Insert a minimal Investigation row, return its id."""
    async with session_factory() as session:
        inv = Investigation(
            external_issue_id="TEST-007",
            repository="/tmp/demo",
            base_branch="main",
            status=InvestigationStatus.VERIFYING,
            bug_context={},
        )
        session.add(inv)
        await session.commit()
        await session.refresh(inv)
        return inv.id


def _mock_gen_with_concurrent_test(demo_repo: Path):
    """
    Return a mock RegressionTestGenerator whose .generate() writes the
    pre-built concurrent regression test from the fixture.
    """
    regression_content = (demo_repo / REGRESSION_TEST_PATH).read_text()

    async def _fake_generate(**kwargs):
        # Write the pre-built test into the worktree
        dest = demo_repo / REGRESSION_TEST_PATH
        dest.write_text(regression_content)
        return REGRESSION_TEST_PATH, regression_content

    mock = AsyncMock()
    mock.generate = AsyncMock(side_effect=_fake_generate)
    return mock


def _make_bug_context(error_type: str = "deterministic") -> BugContext:
    return BugContext(
        issue_id="TEST-007",
        symptom="Duplicate rows on concurrent insert",
        expected="One row per key",
        actual="Two rows on concurrent insert",
        affected_area="review_service",
        error_type=error_type,
        known_evidence=[],
        repository="/tmp/demo",
        base_branch="main",
    )


# ── tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_correct_hypothesis_is_verified(
    db_engine, session_factory, demo_repo: Path
):
    """AC-1: correct hypothesis on seeded bug repo → VERIFIED."""
    inv_id = await _seed_investigation(session_factory)
    hyp_id = await _seed_hypothesis(
        session_factory,
        inv_id,
        candidate_fix=CORRECT_PATCH,
        reproduction_plan=[REGRESSION_TEST_PATH],
    )

    engine = VerificationEngine(session_factory)
    mock_gen = _mock_gen_with_concurrent_test(demo_repo)

    engine._gen = mock_gen
    status = await engine.verify(
        hypothesis_id=hyp_id,
        worktree_path=demo_repo,
        bug_context=_make_bug_context(),
    )

    assert status == HypothesisStatus.VERIFIED

    # Verify DB state
    async with session_factory() as session:
        hyp = await session.get(Hypothesis, hyp_id)
        assert hyp.status == HypothesisStatus.VERIFIED

        ev_result = await session.execute(
            select(TestEvidence).where(TestEvidence.hypothesis_id == hyp_id)
        )
        evidence = ev_result.scalars().all()

    assert len(evidence) >= 1
    # Most recent evidence row has all three results
    latest = sorted(evidence, key=lambda e: e.created_at)[-1]
    assert latest.pre_fix_result == "FAIL"
    assert latest.post_fix_result == "PASS"
    assert latest.existing_suite_result == "PASS"


@pytest.mark.asyncio
async def test_verify_wrong_hypothesis_is_rejected(
    db_engine, session_factory, demo_repo: Path
):
    """AC-2: wrong-file patch doesn't fix the bug → post-fix run still FAIL → REJECTED."""
    inv_id = await _seed_investigation(session_factory)
    hyp_id = await _seed_hypothesis(
        session_factory,
        inv_id,
        candidate_fix=WRONG_PATCH,
        reproduction_plan=[REGRESSION_TEST_PATH],
    )

    engine = VerificationEngine(session_factory)
    mock_gen = _mock_gen_with_concurrent_test(demo_repo)

    engine._gen = mock_gen
    status = await engine.verify(
        hypothesis_id=hyp_id,
        worktree_path=demo_repo,
        bug_context=_make_bug_context(),
    )

    assert status == HypothesisStatus.REJECTED

    async with session_factory() as session:
        hyp = await session.get(Hypothesis, hyp_id)
        assert hyp.status == HypothesisStatus.REJECTED


@pytest.mark.asyncio
async def test_verify_inconclusive_when_pre_fix_passes(
    db_engine, session_factory, demo_repo: Path
):
    """AC-3: pre-fix test already passes → INCONCLUSIVE (AGENTS.md core rule)."""
    inv_id = await _seed_investigation(session_factory)
    # reproduction_plan uses the concurrent test (FAILS before fix) so step 1 passes.
    # The generator returns the always-passing sequential test → step 3 gets PASS → INCONCLUSIVE.
    hyp_id = await _seed_hypothesis(
        session_factory,
        inv_id,
        candidate_fix=CORRECT_PATCH,
        reproduction_plan=[REGRESSION_TEST_PATH],  # fails on buggy code → reproduction succeeds
    )

    engine = VerificationEngine(session_factory)

    # Generator returns a test that always passes (the existing sequential test)
    always_pass_content = (demo_repo / "tests/test_review.py").read_text()

    async def _fake_generate(**kwargs):
        dest = demo_repo / "tests/test_regression_always_pass.py"
        dest.write_text(always_pass_content)
        return "tests/test_regression_always_pass.py", always_pass_content

    mock_gen = AsyncMock()
    mock_gen.generate = AsyncMock(side_effect=_fake_generate)
    engine._gen = mock_gen
    status = await engine.verify(
        hypothesis_id=hyp_id,
        worktree_path=demo_repo,
        bug_context=_make_bug_context(),
    )

    assert status == HypothesisStatus.INCONCLUSIVE

    async with session_factory() as session:
        hyp = await session.get(Hypothesis, hyp_id)
        assert hyp.status == HypothesisStatus.INCONCLUSIVE
        # Patch must NOT have been applied (patch_attempts stays 0)
        assert hyp.patch_attempts == 0


@pytest.mark.asyncio
async def test_verify_blocked_after_max_patch_attempts(
    db_engine, session_factory, demo_repo: Path
):
    """AC-4: invalid patch fails, exhausts max_patch_attempts → BLOCKED."""
    from app.config import settings

    inv_id = await _seed_investigation(session_factory)
    hyp_id = await _seed_hypothesis(
        session_factory,
        inv_id,
        candidate_fix=INVALID_PATCH,
        reproduction_plan=[REGRESSION_TEST_PATH],
    )

    engine = VerificationEngine(session_factory)
    mock_gen = _mock_gen_with_concurrent_test(demo_repo)

    # Run verify enough times to exhaust patch attempts.
    # Each call to verify() increments patch_attempts on failure.
    engine._gen = mock_gen
    for _ in range(settings.max_patch_attempts):
        status = await engine.verify(
            hypothesis_id=hyp_id,
            worktree_path=demo_repo,
            bug_context=_make_bug_context(),
        )

    assert status == HypothesisStatus.BLOCKED

    async with session_factory() as session:
        hyp = await session.get(Hypothesis, hyp_id)
        assert hyp.status == HypothesisStatus.BLOCKED
        assert hyp.patch_attempts >= settings.max_patch_attempts


@pytest.mark.asyncio
async def test_verify_persists_test_evidence(
    db_engine, session_factory, demo_repo: Path
):
    """AC-6: after VERIFIED, TestEvidence row has correct result fields."""
    inv_id = await _seed_investigation(session_factory)
    hyp_id = await _seed_hypothesis(
        session_factory,
        inv_id,
        candidate_fix=CORRECT_PATCH,
        reproduction_plan=[REGRESSION_TEST_PATH],
    )

    engine = VerificationEngine(session_factory)
    mock_gen = _mock_gen_with_concurrent_test(demo_repo)

    engine._gen = mock_gen
    await engine.verify(
        hypothesis_id=hyp_id,
        worktree_path=demo_repo,
        bug_context=_make_bug_context(),
    )

    async with session_factory() as session:
        result = await session.execute(
            select(TestEvidence).where(TestEvidence.hypothesis_id == hyp_id)
        )
        rows = result.scalars().all()

    assert len(rows) >= 1
    latest = sorted(rows, key=lambda e: e.created_at)[-1]
    assert latest.pre_fix_result == "FAIL"
    assert latest.post_fix_result == "PASS"
    assert latest.existing_suite_result == "PASS"


@pytest.mark.asyncio
async def test_verify_persists_experiments(
    db_engine, session_factory, demo_repo: Path
):
    """AC-7: Experiment rows are persisted for each verification step."""
    inv_id = await _seed_investigation(session_factory)
    hyp_id = await _seed_hypothesis(
        session_factory,
        inv_id,
        candidate_fix=CORRECT_PATCH,
        reproduction_plan=[REGRESSION_TEST_PATH],
    )

    engine = VerificationEngine(session_factory)
    mock_gen = _mock_gen_with_concurrent_test(demo_repo)

    engine._gen = mock_gen
    await engine.verify(
        hypothesis_id=hyp_id,
        worktree_path=demo_repo,
        bug_context=_make_bug_context(),
    )

    async with session_factory() as session:
        result = await session.execute(
            select(Experiment).where(Experiment.hypothesis_id == hyp_id)
        )
        experiments = result.scalars().all()

    # Should have at least: reproduce, pre_fix_run, git_apply_patch, post_fix_run, existing_suite
    assert len(experiments) >= 3
    labels = {e.command for e in experiments}
    assert "pre_fix_run" in labels
    assert "post_fix_run" in labels
    assert "git_apply_patch" in labels


@pytest.mark.asyncio
async def test_verify_nonexistent_hypothesis_returns_blocked(
    db_engine, session_factory, demo_repo: Path
):
    """Engine returns BLOCKED gracefully when hypothesis ID doesn't exist."""
    engine = VerificationEngine(session_factory)
    status = await engine.verify(
        hypothesis_id="nonexistent-id",
        worktree_path=demo_repo,
        bug_context=_make_bug_context(),
    )
    assert status == HypothesisStatus.BLOCKED
