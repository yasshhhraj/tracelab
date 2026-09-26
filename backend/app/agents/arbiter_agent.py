"""Turn verification evidence into a diagnosis for human review."""

import json
import logging
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.db.models import (
    Diagnosis,
    Experiment,
    Hypothesis,
    HypothesisStatus,
    Investigation,
    Patch,
    TestEvidence,
)

logger = logging.getLogger(__name__)


class RejectedHypothesisInfo(BaseModel):
    hypothesis_id: str
    agent_type: str
    summary: str
    rejection_reason: str


class DiagnosisData(BaseModel):
    selected_hypothesis_id: str | None
    summary: str
    verified_cause: str
    evidence: list[str]
    rejected_hypotheses: list[RejectedHypothesisInfo]
    changed_files: list[str]
    risk: Literal["low", "medium", "high", "unknown"]
    recommended_action: str


_EMIT_DIAGNOSIS_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "emit_diagnosis",
        "description": "Emit a structured diagnosis based on the recorded evidence.",
        "parameters": {
            "type": "object",
            "required": [
                "selected_hypothesis_id",
                "summary",
                "verified_cause",
                "evidence",
                "rejected_hypotheses",
                "changed_files",
                "risk",
                "recommended_action",
            ],
            "properties": {
                "selected_hypothesis_id": {"type": ["string", "null"]},
                "summary": {"type": "string"},
                "verified_cause": {"type": "string"},
                "evidence": {"type": "array", "items": {"type": "string"}},
                "rejected_hypotheses": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["hypothesis_id", "agent_type", "summary", "rejection_reason"],
                        "properties": {
                            "hypothesis_id": {"type": "string"},
                            "agent_type": {"type": "string"},
                            "summary": {"type": "string"},
                            "rejection_reason": {"type": "string"},
                        },
                    },
                },
                "changed_files": {"type": "array", "items": {"type": "string"}},
                "risk": {"type": "string", "enum": ["low", "medium", "high", "unknown"]},
                "recommended_action": {"type": "string"},
            },
        },
    },
}

_SYSTEM_PROMPT = """You are TraceLab's evidence arbiter. Read the recorded tests and experiments.
Only a VERIFIED hypothesis with reproduction, FAIL before the patch, PASS after it, and
passing existing tests may be selected. If none qualifies, select null. Compare multiple
qualifying hypotheses using causal explanation, actual test results, patch size and risk.
Never invent test results or changed files. Call emit_diagnosis with your assessment.
"""


class ArbiterAgent:
    """Build a report from stored evidence, then persist it once per investigation."""

    async def run(
        self, investigation_id: str, session_factory: async_sessionmaker
    ) -> DiagnosisData:
        prompt, hypotheses = await self._build_evidence_summary(investigation_id, session_factory)
        eligible = {h["id"]: h for h in hypotheses if h["eligible"]}
        if not eligible:
            diagnosis = self._no_winner_diagnosis(hypotheses)
        else:
            diagnosis = await self._call_llm(prompt)
            if diagnosis.selected_hypothesis_id not in eligible:
                logger.warning("Arbiter selected a hypothesis without complete proof")
                fallback = self._no_winner_diagnosis(hypotheses)
                if diagnosis.summary == "Arbiter could not produce a structured diagnosis.":
                    fallback.summary = diagnosis.summary
                    fallback.recommended_action = diagnosis.recommended_action
                diagnosis = fallback
            else:
                winner = eligible[diagnosis.selected_hypothesis_id]
                # These fields are factual; never trust model supplied values for them.
                diagnosis.changed_files = winner["changed_files"]
                rejected_by_id = {r.hypothesis_id: r for r in diagnosis.rejected_hypotheses}
                diagnosis.rejected_hypotheses = [
                    RejectedHypothesisInfo(
                        hypothesis_id=h["id"],
                        agent_type=h["agent_type"],
                        summary=h["summary"],
                        rejection_reason=(
                            rejected_by_id[h["id"]].rejection_reason
                            if h["id"] in rejected_by_id
                            else f"Verification status: {h['status']}"
                        ),
                    )
                    for h in hypotheses
                    if h["id"] != winner["id"]
                ]
        await self._persist_diagnosis(investigation_id, diagnosis, session_factory)
        return diagnosis

    async def _build_evidence_summary(
        self, investigation_id: str, session_factory: async_sessionmaker
    ) -> tuple[str, list[dict[str, Any]]]:
        async with session_factory() as session:
            investigation = await session.get(Investigation, investigation_id)
            if investigation is None:
                raise ValueError(f"Investigation {investigation_id} not found")
            rows = (
                (
                    await session.execute(
                        select(Hypothesis)
                        .where(Hypothesis.investigation_id == investigation_id)
                        .order_by(Hypothesis.created_at, Hypothesis.id)
                    )
                )
                .scalars()
                .all()
            )
            hypotheses: list[dict[str, Any]] = []
            for hyp in rows:
                evidence = (
                    (
                        await session.execute(
                            select(TestEvidence)
                            .where(TestEvidence.hypothesis_id == hyp.id)
                            .order_by(TestEvidence.created_at.desc())
                        )
                    )
                    .scalars()
                    .all()
                )
                experiments = (
                    (
                        await session.execute(
                            select(Experiment)
                            .where(Experiment.hypothesis_id == hyp.id)
                            .order_by(Experiment.created_at)
                        )
                    )
                    .scalars()
                    .all()
                )
                patches = (
                    (
                        await session.execute(
                            select(Patch)
                            .where(Patch.hypothesis_id == hyp.id)
                            .order_by(Patch.created_at.desc())
                        )
                    )
                    .scalars()
                    .all()
                )
                proof = any(
                    ev.pre_fix_result == "FAIL"
                    and ev.post_fix_result == "PASS"
                    and ev.existing_suite_result == "PASS"
                    for ev in evidence
                )
                reproduction = any(
                    exp.command.startswith("reproduce:") or exp.command == "reproduce_full_suite"
                    for exp in experiments
                    if exp.exit_code != 0 and not exp.timed_out
                )
                # CP-07 status already incorporates reproduction. Preserve compatibility
                # with seeded rows that only carry status and test evidence.
                eligible = hyp.status == HypothesisStatus.VERIFIED and proof
                changed_files = patches[0].files_changed if patches else hyp.suspected_files
                hypotheses.append(
                    {
                        "id": hyp.id,
                        "agent_type": hyp.agent_type,
                        "summary": hyp.summary,
                        "reasoning": hyp.reasoning_summary,
                        "candidate_fix": hyp.candidate_fix,
                        "status": hyp.status,
                        "suspected_files": hyp.suspected_files,
                        "changed_files": changed_files,
                        "evidence": [
                            {
                                "pre_fix_result": ev.pre_fix_result,
                                "post_fix_result": ev.post_fix_result,
                                "existing_suite_result": ev.existing_suite_result,
                                "runs": ev.runs,
                                "failures_before": ev.failures_before,
                                "failures_after": ev.failures_after,
                            }
                            for ev in evidence
                        ],
                        "experiment_count": len(experiments),
                        "reproduction_observed": reproduction,
                        "eligible": eligible,
                    }
                )
        bug = investigation.bug_context or {}
        lines = [
            f"Investigation: {investigation_id}",
            f"Issue: {bug.get('issue_id', investigation.external_issue_id)}",
            f"Symptom: {bug.get('symptom', '')}",
            f"Expected: {bug.get('expected', '')}",
            f"Actual: {bug.get('actual', '')}",
            "Hypotheses:",
        ]
        for i, hyp in enumerate(hypotheses, 1):
            lines.extend(
                [
                    f"[H{i}] id={hyp['id']} agent_type={hyp['agent_type']} status={hyp['status']}",
                    f"Summary: {hyp['summary']}",
                    f"Reasoning: {hyp['reasoning']}",
                    f"Candidate fix: {hyp['candidate_fix']}",
                    f"Suspected files: {', '.join(hyp['suspected_files'])}",
                    f"Changed files: {', '.join(hyp['changed_files'])}",
                    f"Test evidence: {json.dumps(hyp['evidence'])}",
                    f"Experiments run: {hyp['experiment_count']}",
                    f"Reproduction observed: {hyp['reproduction_observed']}",
                ]
            )
        return "\n".join(lines), hypotheses

    async def _call_llm(self, prompt: str) -> DiagnosisData:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        for attempt in range(2):
            response = await self._chat(messages, [_EMIT_DIAGNOSIS_SCHEMA])
            message = response["choices"][0]["message"]
            for tool_call in message.get("tool_calls") or []:
                if tool_call.get("function", {}).get("name") != "emit_diagnosis":
                    continue
                try:
                    return DiagnosisData.model_validate_json(
                        tool_call["function"].get("arguments", "{}")
                    )
                except (ValidationError, ValueError):
                    logger.warning("Arbiter returned an invalid diagnosis payload")
            messages.append({"role": "assistant", "content": message.get("content") or ""})
            if attempt == 0:
                messages.append(
                    {
                        "role": "user",
                        "content": "Please call emit_diagnosis with your assessment now.",
                    }
                )
        return DiagnosisData(
            selected_hypothesis_id=None,
            summary="Arbiter could not produce a structured diagnosis.",
            verified_cause="",
            evidence=[],
            rejected_hypotheses=[],
            changed_files=[],
            risk="unknown",
            recommended_action="Review the investigation evidence manually.",
        )

    def _no_winner_diagnosis(self, hypotheses: list[dict[str, Any]]) -> DiagnosisData:
        return DiagnosisData(
            selected_hypothesis_id=None,
            summary="No hypothesis was sufficiently supported by evidence.",
            verified_cause="",
            evidence=[],
            rejected_hypotheses=[
                RejectedHypothesisInfo(
                    hypothesis_id=h["id"],
                    agent_type=h["agent_type"],
                    summary=h["summary"],
                    rejection_reason=f"Verification status: {h['status']}",
                )
                for h in hypotheses
            ],
            changed_files=[],
            risk="unknown",
            recommended_action="Review the investigation logs and supply more context if needed.",
        )

    async def _persist_diagnosis(
        self, investigation_id: str, data: DiagnosisData, session_factory: async_sessionmaker
    ) -> Diagnosis:
        async with session_factory() as session:
            existing = (
                await session.execute(
                    select(Diagnosis).where(Diagnosis.investigation_id == investigation_id)
                )
            ).scalar_one_or_none()
            diagnosis = existing or Diagnosis(investigation_id=investigation_id)
            for field, value in data.model_dump().items():
                setattr(diagnosis, field, value)
            session.add(diagnosis)
            await session.commit()
            await session.refresh(diagnosis)
            return diagnosis

    async def _chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
        payload = {
            "model": settings.llm_model,
            "messages": messages,
            "tools": tools,
            "tool_choice": {"type": "function", "function": {"name": "emit_diagnosis"}},
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{settings.llm_base_url}/chat/completions", headers=headers, json=payload
            )
            response.raise_for_status()
            return response.json()
