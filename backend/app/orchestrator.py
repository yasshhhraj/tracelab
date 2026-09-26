"""
Orchestrator — CP-06 through CP-11

Runs the three investigation agents concurrently, then drives each hypothesis
through the VerificationEngine sequentially, then persists an Arbiter diagnosis.
On completion, posts a structured comment back to the originating Jira issue.

Status flow:
    CREATED → CONTEXT_LOADING → INVESTIGATING → VERIFYING → ARBITRATING
    → WAITING_FOR_REVIEW
    On total failure: → FAILED

AGENTS.md constraints enforced here:
- asyncio.gather() for parallelism (investigation phase only)
- Verification is sequential — concurrent test runs can interfere
- max_hypotheses = 3 (hard cap; exactly 3 agents launched)
- confidence field is never branched on
- Credentials never in prompts — AgentContext carries no tokens
"""

import asyncio
import contextlib
import logging
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agents.arbiter_agent import ArbiterAgent
from app.agents.base import AgentContext, AgentResult
from app.agents.code_path_agent import run_code_path_agent
from app.agents.git_history_agent import run_git_history_agent
from app.agents.test_behavior_agent import run_test_behavior_agent
from app.config import settings
from app.db.models import (
    AgentEvent,
    Diagnosis,
    Experiment,
    Hypothesis,
    HypothesisStatus,
    Investigation,
    InvestigationStatus,
)
from app.schemas.bug_context import BugContext
from app.verification.engine import VerificationEngine
from app.worktree.manager import WorktreeManager, make_branch_name

logger = logging.getLogger(__name__)

# Ordered list of (agent_type, runner_coroutine_factory) — exactly 3, per AGENTS.md
_AGENT_TYPES = ["code_path", "git_history", "test_behavior"]
_AGENT_RUNNERS = [run_code_path_agent, run_git_history_agent, run_test_behavior_agent]


# ── private helpers ───────────────────────────────────────────────────────────


async def _set_status(
    session_factory: async_sessionmaker,
    investigation_id: str,
    status: str,
) -> None:
    """Persist a single status transition immediately (no batching)."""
    async with session_factory() as session:
        inv = await session.get(Investigation, investigation_id)
        if inv is not None:
            inv.status = status
            await session.commit()
    logger.debug("Investigation %s → %s", investigation_id, status)


async def _persist_result(
    session_factory: async_sessionmaker,
    investigation_id: str,
    agent_type: str,
    result: AgentResult | BaseException,
) -> None:
    """
    Persist one agent's outcome (success or exception) as a Hypothesis row,
    plus any Experiment and AgentEvent children.

    If `result` is an exception the hypothesis is marked BLOCKED with the
    error message as reasoning_summary.
    """
    async with session_factory() as session:
        if isinstance(result, BaseException):
            hyp_row = Hypothesis(
                investigation_id=investigation_id,
                agent_type=agent_type,
                summary=f"Agent {agent_type} failed with an unhandled exception.",
                reasoning_summary=str(result),
                candidate_fix="",
                suspected_files=[],
                reproduction_plan=[],
                confidence="low",
                status=HypothesisStatus.BLOCKED,
            )
            session.add(hyp_row)
            await session.flush()
            # Emit a single AgentEvent recording the failure
            session.add(
                AgentEvent(
                    investigation_id=investigation_id,
                    agent=agent_type,
                    action="agent_error",
                    target=None,
                    payload={"error": str(result)},
                )
            )
        else:
            h = result.hypothesis
            hyp_row = Hypothesis(
                investigation_id=investigation_id,
                agent_type=h.agent_type,
                summary=h.summary,
                reasoning_summary=h.reasoning_summary,
                candidate_fix=h.candidate_fix,
                suspected_files=h.suspected_files,
                reproduction_plan=h.reproduction_plan,
                # confidence stored but NEVER used for selection (AGENTS.md)
                confidence=h.confidence,
                status=HypothesisStatus.PROPOSED,
            )
            session.add(hyp_row)
            await session.flush()  # populate hyp_row.id before children

            for exp in result.experiments:
                session.add(
                    Experiment(
                        hypothesis_id=hyp_row.id,
                        command=" ".join(exp.command),
                        working_directory=exp.cwd,
                        exit_code=exp.exit_code,
                        stdout=exp.stdout,
                        stderr=exp.stderr,
                        duration_ms=exp.duration_ms,
                        timed_out=exp.timed_out,
                    )
                )

            for event in result.events:
                session.add(
                    AgentEvent(
                        investigation_id=investigation_id,
                        agent=event.agent,
                        action=event.action,
                        target=event.target,
                        payload=event.payload,
                    )
                )

        await session.commit()


# ── verification phase ────────────────────────────────────────────────────────


async def _run_verification(
    session_factory: async_sessionmaker,
    investigation_id: str,
    hypothesis_worktree_map: dict[str, Path],
    bug_context: BugContext,
) -> None:
    """
    Run VerificationEngine.verify() for each non-BLOCKED hypothesis.

    Sequential — NOT concurrent. Concurrent test-suite runs on the same
    machine can interfere via port conflicts, file locks, etc.

    After all hypotheses are processed, transitions the investigation to
    ARBITRATING for the Arbiter.
    """
    engine = VerificationEngine(session_factory)

    for hypothesis_id, worktree_path in hypothesis_worktree_map.items():
        # Load hypothesis to check if it was already BLOCKED by a failed agent
        async with session_factory() as session:
            hyp = await session.get(Hypothesis, hypothesis_id)
            if hyp is None:
                continue
            if hyp.status == HypothesisStatus.BLOCKED:
                logger.info("Skipping verification for BLOCKED hypothesis %s", hypothesis_id)
                continue
            if not hyp.candidate_fix:
                logger.info(
                    "Skipping verification for hypothesis %s: empty candidate_fix",
                    hypothesis_id,
                )
                continue

        try:
            await engine.verify(
                hypothesis_id=hypothesis_id,
                worktree_path=worktree_path,
                bug_context=bug_context,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("VerificationEngine raised for hypothesis %s: %s", hypothesis_id, exc)
            with contextlib.suppress(Exception):
                async with session_factory() as session:
                    hyp = await session.get(Hypothesis, hypothesis_id)
                    if hyp is not None:
                        hyp.status = HypothesisStatus.BLOCKED
                        await session.commit()

    await _set_status(session_factory, investigation_id, InvestigationStatus.ARBITRATING)


async def _run_arbitration(
    session_factory: async_sessionmaker,
    investigation_id: str,
) -> None:
    """Persist an evidence-based diagnosis and finish automated investigation."""
    try:
        await ArbiterAgent().run(investigation_id, session_factory)
        async with session_factory() as session:
            investigation = await session.get(Investigation, investigation_id)
            if investigation is not None:
                investigation.status = InvestigationStatus.WAITING_FOR_REVIEW
                investigation.completed_at = datetime.now(UTC)
                await session.commit()
    except Exception:
        logger.exception("Arbitration failed for investigation %s", investigation_id)
        await _set_status(session_factory, investigation_id, InvestigationStatus.FAILED)


async def _post_jira_comment(
    session_factory: async_sessionmaker,
    investigation_id: str,
) -> None:
    """
    Fire-and-forget: add a structured comment to the originating Jira issue.

    CP-11 — called after the investigation reaches WAITING_FOR_REVIEW.

    Skipped silently when:
    - settings.jira_base_url or settings.jira_api_token is empty (Jira not configured)
    - The investigation has no diagnosis record yet
    - Any JiraClientError (a Jira outage must never fail an investigation)

    SECURITY: The comment body contains only diagnosis data — no credentials,
    tokens, or env-var values are included (AGENTS.md).
    """
    if not settings.jira_base_url or not settings.jira_api_token:
        logger.debug("Jira not configured — skipping post-diagnosis comment")
        return

    async with session_factory() as session:
        from sqlalchemy.orm import selectinload

        investigation = await session.get(
            Investigation,
            investigation_id,
            options=[selectinload(Investigation.diagnosis)],
        )
        if investigation is None:
            return

        diagnosis: Diagnosis | None = investigation.diagnosis
        if diagnosis is None:
            logger.debug(
                "No diagnosis for investigation %s — skipping Jira comment", investigation_id
            )
            return

        issue_id = investigation.external_issue_id
        n_hypotheses = len(investigation.hypotheses) if investigation.hypotheses else 3
        evidence_lines = "\n".join(f"- {e}" for e in (diagnosis.evidence or []))

        comment_lines = [
            "AI Debugging Analysis Complete",
            "",
            f"{n_hypotheses} root-cause hypotheses investigated.",
        ]
        if diagnosis.verified_cause:
            comment_lines.append(f"Verified: {diagnosis.verified_cause}")
        if evidence_lines:
            comment_lines += ["", "Evidence:", evidence_lines]
        comment_lines += [
            "",
            f"Risk: {diagnosis.risk}",
            f"Recommended action: {diagnosis.recommended_action}",
            "",
            f"TraceLab investigation: {investigation_id}",
        ]
        comment = "\n".join(comment_lines)

    # Import here to avoid circular imports (jira_client → config, orchestrator → jira_client).
    from app.integrations.jira_client import JiraClient, JiraClientError

    try:
        await JiraClient().add_comment(issue_id, comment)
        logger.info(
            "Posted Jira comment for investigation %s on issue %s", investigation_id, issue_id
        )
    except JiraClientError:
        logger.warning(
            "Failed to post Jira comment for investigation %s on issue %s",
            investigation_id,
            issue_id,
            exc_info=True,
        )


# ── public API ────────────────────────────────────────────────────────────────


async def run_investigation(
    investigation_id: str,
    session_factory: async_sessionmaker,
) -> None:
    """
    Full investigation + verification pipeline for one Investigation record.

    Designed to be called as a background task:
        background_tasks.add_task(run_investigation, inv_id, AsyncSessionLocal)

    Status flow:
        CREATED → CONTEXT_LOADING → INVESTIGATING → VERIFYING → ARBITRATING
        → WAITING_FOR_REVIEW
        On total failure: → FAILED

    AGENTS.md constraints:
        - asyncio.gather() for agent phase only — no external workflow library
        - Verification is sequential (test runners can interfere if concurrent)
        - Exactly 3 agents (max_hypotheses = 3)
        - Worktrees destroyed after verification completes (in finally block)
        - AgentContext carries no credentials
    """
    worktree_manager: WorktreeManager | None = None
    tmp_dir: tempfile.TemporaryDirectory | None = None  # type: ignore[type-arg]
    # Maps agent_index (0/1/2) → effective worktree path; built during investigation
    agent_worktree_paths: list[Path] = []

    try:
        # ── CONTEXT_LOADING ───────────────────────────────────────────────────
        await _set_status(session_factory, investigation_id, InvestigationStatus.CONTEXT_LOADING)

        async with session_factory() as session:
            inv: Investigation | None = await session.get(Investigation, investigation_id)
            if inv is None:
                logger.error("Investigation %s not found — aborting", investigation_id)
                return
            raw_bug_context: dict = inv.bug_context or {}
            issue_id: str = inv.external_issue_id
            repository: str = inv.repository

        try:
            bug_context = BugContext(**raw_bug_context)
        except Exception as exc:
            logger.error("Failed to parse BugContext for %s: %s", investigation_id, exc)
            await _set_status(session_factory, investigation_id, InvestigationStatus.FAILED)
            return

        # Resolve the repo path — local paths only (remote clone support in CP-11)
        repo_path = Path(repository)

        # Create a temporary base directory for this investigation's worktrees.
        # TemporaryDirectory ensures cleanup on process exit even if finally doesn't run.
        tmp_dir = tempfile.TemporaryDirectory(prefix=f"tracelab-{investigation_id[:8]}-")
        base_dir = Path(tmp_dir.name)

        worktree_manager = WorktreeManager(base_dir=base_dir)

        # Create one worktree per agent (branches: ai-debug/{issue_id}-h1/h2/h3)
        worktree_paths: list[Path | None] = []
        for i, agent_type in enumerate(_AGENT_TYPES, start=1):
            branch = make_branch_name(issue_id, i)
            try:
                wt_path = await worktree_manager.create(
                    repo_path=repo_path,
                    branch=branch,
                    hypothesis_id=f"h{i}",
                )
                worktree_paths.append(wt_path)
                logger.debug("Worktree created for %s agent: %s", agent_type, wt_path)
            except Exception as exc:
                # Worktree failure must not block sibling agents.
                logger.warning(
                    "Worktree creation failed for %s (h%d): %s — falling back to repo_path",
                    agent_type,
                    i,
                    exc,
                )
                worktree_paths.append(None)

        # Effective worktree path per agent (fallback: repo_path)
        agent_worktree_paths = [wt if wt is not None else repo_path for wt in worktree_paths]

        # Build one AgentContext per agent
        contexts: list[AgentContext] = [
            AgentContext(
                bug_context=bug_context,
                repo_path=repo_path,
                worktree_path=agent_worktree_paths[i],
                investigation_id=investigation_id,
            )
            for i in range(len(_AGENT_TYPES))
        ]

        # ── INVESTIGATING ─────────────────────────────────────────────────────
        await _set_status(session_factory, investigation_id, InvestigationStatus.INVESTIGATING)

        # Run all 3 agents concurrently — AGENTS.md: asyncio.gather(), no workflow framework
        results: list[AgentResult | BaseException] = list(
            await asyncio.gather(
                *[runner(ctx) for runner, ctx in zip(_AGENT_RUNNERS, contexts, strict=True)],
                return_exceptions=True,
            )
        )

        # Persist each result (success or exception) as a Hypothesis row
        for agent_type, result in zip(_AGENT_TYPES, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("Agent %s raised: %s", agent_type, result)
            await _persist_result(session_factory, investigation_id, agent_type, result)

        # ── VERIFYING / FAILED ────────────────────────────────────────────────
        all_failed = all(isinstance(r, BaseException) for r in results)
        if all_failed:
            logger.error(
                "All agents failed for investigation %s — marking FAILED", investigation_id
            )
            await _set_status(session_factory, investigation_id, InvestigationStatus.FAILED)
            return

        await _set_status(session_factory, investigation_id, InvestigationStatus.VERIFYING)

        # Build hypothesis_id → worktree_path map for the verification phase.
        # Hypotheses were just persisted in _AGENT_TYPES order; query them back.
        async with session_factory() as session:
            hyp_result = await session.execute(
                select(Hypothesis)
                .where(Hypothesis.investigation_id == investigation_id)
                .order_by(Hypothesis.created_at)
            )
            ordered_hyps = hyp_result.scalars().all()

        # Match hypothesis rows to worktree paths by insertion order
        hypothesis_worktree_map: dict[str, Path] = {}
        for idx, hyp in enumerate(ordered_hyps):
            if idx < len(agent_worktree_paths):
                hypothesis_worktree_map[hyp.id] = agent_worktree_paths[idx]

        # ── VERIFYING (sequential) → ARBITRATING ─────────────────────────────
        await _run_verification(
            session_factory=session_factory,
            investigation_id=investigation_id,
            hypothesis_worktree_map=hypothesis_worktree_map,
            bug_context=bug_context,
        )
        await _run_arbitration(session_factory, investigation_id)

        # CP-11: post diagnosis summary back to Jira — fire-and-forget,
        # never allowed to raise (AGENTS.md: Jira outage must not fail investigation).
        with contextlib.suppress(Exception):
            await _post_jira_comment(session_factory, investigation_id)

    except Exception as exc:
        logger.exception("Unhandled error in run_investigation(%s): %s", investigation_id, exc)
        with contextlib.suppress(Exception):
            await _set_status(session_factory, investigation_id, InvestigationStatus.FAILED)

    finally:
        # Always clean up worktrees to prevent directory leaks (AGENTS.md).
        # Cleanup runs AFTER verification so worktrees are available to the engine.
        if worktree_manager is not None:
            await worktree_manager.destroy_all()
        if tmp_dir is not None:
            with contextlib.suppress(Exception):
                tmp_dir.cleanup()
