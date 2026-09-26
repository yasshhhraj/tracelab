"""
VerificationEngine — CP-07

Runs the 7-step fail-before/pass-after proof for a single hypothesis.

Verification contract (AGENTS.md):
    1. reproduction = successful
    2. regression_test_before_fix = FAIL   ← enforced: INCONCLUSIVE if PASS
    3. regression_test_after_fix  = PASS
    4. relevant_existing_tests    = PASS

Outcomes: VERIFIED | REJECTED | INCONCLUSIVE | BLOCKED

AGENTS.md constraints enforced:
    - Pre-fix regression test MUST fail (abort → INCONCLUSIVE if it passes)
    - Max 2 patch attempts per hypothesis
    - Max 120s per command (via run_test / apply_patch defaults)
    - Credentials never in prompts
    - confidence field never read
"""

import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.db.models import (
    AgentEvent,
    Experiment,
    Hypothesis,
    HypothesisStatus,
    TestEvidence,
)
from app.schemas.bug_context import BugContext
from app.tools.git_tools import apply_patch
from app.tools.test_runner import run_test
from app.verification.flaky_runner import FlakyRunResult, run_flaky
from app.verification.regression_test_generator import RegressionTestGenerator

logger = logging.getLogger(__name__)

# Test result constants
_PASS = "PASS"
_FAIL = "FAIL"
_ERROR = "ERROR"


def _exit_to_result(exit_code: int) -> str:
    return _PASS if exit_code == 0 else _FAIL


class VerificationEngine:
    """
    Runs the full verification proof for a single hypothesis.

    Each DB write uses a short-lived session — the engine never holds an open
    session across long-running test/patch operations.
    """

    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._sf = session_factory
        self._gen = RegressionTestGenerator()

    # ── public API ────────────────────────────────────────────────────────────

    async def verify(
        self,
        hypothesis_id: str,
        worktree_path: Path,
        bug_context: BugContext,
    ) -> str:
        """
        Run the full 7-step verification proof.

        Returns the final HypothesisStatus string.
        Updates hypothesis.status in DB before returning.
        """
        hyp = await self._load_hypothesis(hypothesis_id)
        if hyp is None:
            logger.error("Hypothesis %s not found — cannot verify", hypothesis_id)
            return HypothesisStatus.BLOCKED

        logger.info(
            "Verifying hypothesis %s (%s) in %s", hypothesis_id, hyp.agent_type, worktree_path
        )

        # ── Step 1: Reproduce ─────────────────────────────────────────────────
        reproduced = await self._reproduce(hyp, worktree_path, bug_context)
        if not reproduced:
            logger.info("Hypothesis %s: could not reproduce — REJECTED", hypothesis_id)
            return await self._save_status(hypothesis_id, HypothesisStatus.REJECTED)

        # ── Step 2: Generate regression test ──────────────────────────────────
        test_path, _ = await self._generate_regression_test(hyp, worktree_path, bug_context)

        # ── Step 3: Pre-fix run (MUST FAIL) ───────────────────────────────────
        if bug_context.error_type == "intermittent":
            pre_result = await self._run_flaky_pre(hypothesis_id, worktree_path, test_path)
            pre_outcome = _FAIL if pre_result.failures > 0 else _PASS
            await self._save_test_evidence(
                hypothesis_id,
                test_path=test_path,
                pre_fix_result=pre_outcome,
                runs=pre_result.runs,
                failures_before=pre_result.failures,
            )
        else:
            pre_outcome = await self._run_pre_fix(hypothesis_id, worktree_path, test_path)
            await self._save_test_evidence(
                hypothesis_id,
                test_path=test_path,
                pre_fix_result=pre_outcome,
            )

        # AGENTS.md core constraint: pre-fix MUST fail
        if pre_outcome == _PASS:
            logger.info(
                "Hypothesis %s: pre-fix test passed — INCONCLUSIVE "
                "(bug not observable via regression test)",
                hypothesis_id,
            )
            return await self._save_status(hypothesis_id, HypothesisStatus.INCONCLUSIVE)

        # ── Step 4: Apply patch (with attempt limit) ──────────────────────────
        patch_applied = await self._apply_patch(hypothesis_id, worktree_path, hyp.candidate_fix)
        if not patch_applied:
            # _apply_patch already bumped patch_attempts and checked the cap
            current_attempts = await self._get_patch_attempts(hypothesis_id)
            if current_attempts >= settings.max_patch_attempts:
                logger.info(
                    "Hypothesis %s: patch failed after %d attempts — BLOCKED",
                    hypothesis_id,
                    current_attempts,
                )
                return await self._save_status(hypothesis_id, HypothesisStatus.BLOCKED)
            # Patch failed but under the cap: treat as REJECTED for this run
            logger.info(
                "Hypothesis %s: patch did not apply cleanly — REJECTED", hypothesis_id
            )
            return await self._save_status(hypothesis_id, HypothesisStatus.REJECTED)

        # ── Step 5: Post-fix run (MUST PASS) ──────────────────────────────────
        if bug_context.error_type == "intermittent":
            post_result = await self._run_flaky_post(hypothesis_id, worktree_path, test_path)
            post_outcome = _PASS if post_result.failures == 0 else _FAIL
            await self._update_test_evidence(
                hypothesis_id,
                test_path=test_path,
                post_fix_result=post_outcome,
                failures_after=post_result.failures,
            )
        else:
            post_outcome = await self._run_post_fix(hypothesis_id, worktree_path, test_path)
            await self._update_test_evidence(
                hypothesis_id,
                test_path=test_path,
                post_fix_result=post_outcome,
            )

        if post_outcome != _PASS:
            logger.info("Hypothesis %s: post-fix test failed — REJECTED", hypothesis_id)
            return await self._save_status(hypothesis_id, HypothesisStatus.REJECTED)

        # ── Step 6: Existing test suite (MUST PASS) ───────────────────────────
        existing_outcome = await self._run_existing_tests(hypothesis_id, worktree_path)
        await self._update_test_evidence(
            hypothesis_id,
            test_path=test_path,
            existing_suite_result=existing_outcome,
        )

        if existing_outcome != _PASS:
            logger.info(
                "Hypothesis %s: existing tests failed after patch — REJECTED "
                "(patch introduced regression)",
                hypothesis_id,
            )
            return await self._save_status(hypothesis_id, HypothesisStatus.REJECTED)

        # ── Step 7: All conditions met — VERIFIED ─────────────────────────────
        logger.info("Hypothesis %s: VERIFIED ✓", hypothesis_id)
        return await self._save_status(hypothesis_id, HypothesisStatus.VERIFIED)

    # ── private steps ─────────────────────────────────────────────────────────

    async def _reproduce(
        self,
        hyp: Hypothesis,
        worktree_path: Path,
        bug_context: BugContext,
    ) -> bool:
        """
        Run the reproduction plan to confirm the bug is observable.
        Returns True if at least one command fails (bug observed).
        """
        if not hyp.reproduction_plan:
            # No plan: run the full test suite and hope something fails
            result = await run_test(repo_path=worktree_path)
            await self._append_experiment(
                hyp.id, result, "reproduce_full_suite"
            )
            return result.exit_code != 0

        any_failure = False
        for step in hyp.reproduction_plan:
            # Treat the step as a pytest node ID / path
            result = await run_test(repo_path=worktree_path, test_path=step)
            await self._append_experiment(hyp.id, result, f"reproduce:{step[:60]}")
            if result.exit_code != 0:
                any_failure = True

        return any_failure

    async def _generate_regression_test(
        self,
        hyp: Hypothesis,
        worktree_path: Path,
        bug_context: BugContext,
    ) -> tuple[str, str]:
        """Call RegressionTestGenerator and emit an AgentEvent."""
        test_path, test_content = await self._gen.generate(
            hypothesis_id=hyp.id,
            worktree_path=worktree_path,
            bug_context=bug_context,
            candidate_fix=hyp.candidate_fix,
            suspected_files=hyp.suspected_files,
            reproduction_plan=hyp.reproduction_plan,
        )
        async with self._sf() as session:
            session.add(
                AgentEvent(
                    investigation_id=hyp.investigation_id,
                    agent="verification_engine",
                    action="regression_test_generated",
                    target=test_path,
                    payload={"hypothesis_id": hyp.id, "test_path": test_path},
                )
            )
            await session.commit()
        return test_path, test_content

    async def _run_pre_fix(
        self,
        hypothesis_id: str,
        worktree_path: Path,
        test_path: str,
    ) -> str:
        """Run the regression test BEFORE applying the patch."""
        result = await run_test(repo_path=worktree_path, test_path=test_path)
        await self._append_experiment(hypothesis_id, result, "pre_fix_run")
        return _exit_to_result(result.exit_code)

    async def _apply_patch(
        self,
        hypothesis_id: str,
        worktree_path: Path,
        candidate_fix: str,
    ) -> bool:
        """
        Apply the candidate fix diff.
        Increments patch_attempts on failure.
        Returns True if applied cleanly.
        """
        if not candidate_fix.strip():
            logger.warning(
                "Hypothesis %s: candidate_fix is empty — cannot apply", hypothesis_id
            )
            await self._increment_patch_attempts(hypothesis_id)
            return False

        result = await apply_patch(repo_path=worktree_path, diff=candidate_fix)
        await self._append_experiment(hypothesis_id, result, "git_apply_patch")

        if result.exit_code != 0:
            logger.warning(
                "Hypothesis %s: patch failed to apply (exit %d): %s",
                hypothesis_id,
                result.exit_code,
                result.stderr[:200],
            )
            await self._increment_patch_attempts(hypothesis_id)
            return False
        return True

    async def _run_post_fix(
        self,
        hypothesis_id: str,
        worktree_path: Path,
        test_path: str,
    ) -> str:
        """Run the regression test AFTER applying the patch."""
        result = await run_test(repo_path=worktree_path, test_path=test_path)
        await self._append_experiment(hypothesis_id, result, "post_fix_run")
        return _exit_to_result(result.exit_code)

    async def _run_existing_tests(
        self,
        hypothesis_id: str,
        worktree_path: Path,
    ) -> str:
        """Run the full existing test suite to catch regressions."""
        result = await run_test(repo_path=worktree_path)
        await self._append_experiment(hypothesis_id, result, "existing_suite")
        return _exit_to_result(result.exit_code)

    async def _run_flaky_pre(
        self,
        hypothesis_id: str,
        worktree_path: Path,
        test_path: str,
    ) -> FlakyRunResult:
        result = await run_flaky(
            worktree_path=worktree_path,
            test_path=test_path,
            n_runs=settings.flaky_default_runs,
        )
        return result

    async def _run_flaky_post(
        self,
        hypothesis_id: str,
        worktree_path: Path,
        test_path: str,
    ) -> FlakyRunResult:
        result = await run_flaky(
            worktree_path=worktree_path,
            test_path=test_path,
            n_runs=settings.flaky_default_runs,
        )
        return result

    # ── DB helpers ────────────────────────────────────────────────────────────

    async def _load_hypothesis(self, hypothesis_id: str) -> Hypothesis | None:
        async with self._sf() as session:
            return await session.get(Hypothesis, hypothesis_id)

    async def _save_status(self, hypothesis_id: str, status: str) -> str:
        async with self._sf() as session:
            hyp = await session.get(Hypothesis, hypothesis_id)
            if hyp is not None:
                hyp.status = status
                await session.commit()
        return status

    async def _get_patch_attempts(self, hypothesis_id: str) -> int:
        async with self._sf() as session:
            hyp = await session.get(Hypothesis, hypothesis_id)
            return hyp.patch_attempts if hyp is not None else 0

    async def _increment_patch_attempts(self, hypothesis_id: str) -> int:
        async with self._sf() as session:
            hyp = await session.get(Hypothesis, hypothesis_id)
            if hyp is not None:
                hyp.patch_attempts += 1
                await session.commit()
                return hyp.patch_attempts
        return 0

    async def _save_test_evidence(
        self,
        hypothesis_id: str,
        *,
        test_path: str = "",
        pre_fix_result: str | None = None,
        post_fix_result: str | None = None,
        existing_suite_result: str | None = None,
        runs: int = 0,
        failures_before: int = 0,
        failures_after: int = 0,
    ) -> None:
        """Insert a new TestEvidence row."""
        async with self._sf() as session:
            session.add(
                TestEvidence(
                    hypothesis_id=hypothesis_id,
                    test_path=test_path,
                    pre_fix_result=pre_fix_result,
                    post_fix_result=post_fix_result,
                    existing_suite_result=existing_suite_result,
                    runs=runs,
                    failures_before=failures_before,
                    failures_after=failures_after,
                )
            )
            await session.commit()

    async def _update_test_evidence(
        self,
        hypothesis_id: str,
        *,
        test_path: str = "",
        post_fix_result: str | None = None,
        existing_suite_result: str | None = None,
        failures_after: int = 0,
    ) -> None:
        """Update the most recent TestEvidence row for this hypothesis."""
        async with self._sf() as session:
            result = await session.execute(
                select(TestEvidence)
                .where(TestEvidence.hypothesis_id == hypothesis_id)
                .order_by(TestEvidence.created_at.desc())
                .limit(1)
            )
            evidence = result.scalar_one_or_none()
            if evidence is not None:
                if post_fix_result is not None:
                    evidence.post_fix_result = post_fix_result
                if existing_suite_result is not None:
                    evidence.existing_suite_result = existing_suite_result
                if failures_after:
                    evidence.failures_after = failures_after
                await session.commit()

    async def _append_experiment(
        self,
        hypothesis_id: str,
        result: object,
        step_label: str,
    ) -> None:
        """Persist a CommandResult as an Experiment row."""
        async with self._sf() as session:
            session.add(
                Experiment(
                    hypothesis_id=hypothesis_id,
                    command=step_label,
                    working_directory="",
                    exit_code=getattr(result, "exit_code", None),
                    stdout=getattr(result, "stdout", "") or "",
                    stderr=getattr(result, "stderr", "") or "",
                    duration_ms=getattr(result, "duration_ms", 0) or 0,
                    timed_out=getattr(result, "timed_out", False) or False,
                )
            )
            await session.commit()
