"""Run CP-13's isolated, end-to-end TraceLab demo without external services."""

import asyncio
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT / "backend")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
os.environ["PATH"] = f"{ROOT / 'backend' / '.venv' / 'bin'}:{os.environ['PATH']}"

from app.agents.arbiter_agent import ArbiterAgent
from app.agents.llm.openai_model import OpenAIAgentModel
from app.config import settings
from app.db.base import Base
from app.db.models import (
    Hypothesis,
    HypothesisStatus,
    Patch,
    TestEvidence,
)
from app.db.session import get_session
from app.main import app
from app.verification.regression_test_generator import (
    RegressionTestGenerator,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from scripts.seed_demo import FIXTURES, hero_patches, seed

MOCK_PR_URL = "https://github.test/demo/review-race/pull/1"


def _tool_call(name: str, arguments: dict) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"demo-{name}",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                }
            }
        ]
    }


class FakeGitHubClient:
    def __init__(self) -> None:
        self.push_calls: list[dict] = []
        self.pr_calls: list[dict] = []

    async def clone_apply_and_push(self, **kwargs) -> str:
        self.push_calls.append(kwargs)
        return "demo-commit-sha"

    async def create_draft_pr(self, **kwargs) -> str:
        self.pr_calls.append(kwargs)
        return MOCK_PR_URL


async def run_demo() -> dict:
    """Run the real API/orchestrator/verifier with scripted model and GitHub boundaries."""
    with tempfile.TemporaryDirectory(prefix="tracelab-cp13-") as temp_dir:
        temporary = Path(temp_dir)
        repository = seed(temporary)
        patches = hero_patches()
        scripted_hypotheses = json.loads(
            (FIXTURES / "hero_hypotheses.json").read_text()
        )
        regression_content = (FIXTURES / "hero_regression_test.py.txt").read_text()
        engine = create_async_engine(f"sqlite+aiosqlite:///{temporary / 'demo.sqlite'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async def demo_session():
            async with session_factory() as session:
                yield session

        async def agent_chat(self, messages, tools):
            agent_type = self._agent_type
            if not any(message.get("role") == "tool" for message in messages):
                if agent_type == "code_path":
                    return _tool_call(
                        "search_code",
                        {"pattern": "input_hash", "path": str(repository)},
                    )
                if agent_type == "git_history":
                    return _tool_call("git_log", {"repo_path": str(repository), "n": 3})
                worktree = re.search(r"Worktree path: (.+)", messages[1]["content"])
                assert worktree is not None
                return _tool_call("run_test", {"repo_path": worktree.group(1)})
            hypothesis = scripted_hypotheses[agent_type]
            summary = hypothesis["summary"]
            return _tool_call(
                "emit_hypothesis",
                {
                    "summary": summary,
                    "suspected_files": [hypothesis["suspected_file"]],
                    "reasoning_summary": summary,
                    "reproduction_plan": ["repro/test_concurrent_review.py"],
                    "candidate_fix": patches[agent_type],
                    "confidence": hypothesis["confidence"],
                },
            )

        async def test_chat(self, messages, tools):
            return _tool_call(
                "emit_test",
                {
                    "test_file_path": "tests/test_generated_race.py",
                    "test_content": regression_content,
                },
            )

        async def arbiter_chat(self, messages, tools):
            evidence = messages[1]["content"]
            winner = re.search(
                r"id=([a-f0-9-]+) agent_type=code_path status=VERIFIED", evidence
            )
            assert winner is not None, "The verified code-path hypothesis is missing"
            return _tool_call(
                "emit_diagnosis",
                {
                    "selected_hypothesis_id": winner.group(1),
                    "summary": "Concurrent lookup and insert can create duplicate review runs.",
                    "verified_cause": "The input hash had no database uniqueness constraint.",
                    "evidence": [
                        "Concurrent reproduction failed on the original code",
                        "Regression test failed before and passed after the patch",
                        "Existing tests passed after the patch",
                    ],
                    "rejected_hypotheses": [],
                    "changed_files": [],
                    "risk": "low",
                    "recommended_action": "Review the tested patch and create a draft PR.",
                },
            )

        github = FakeGitHubClient()
        app.dependency_overrides[get_session] = demo_session
        started = time.monotonic()
        try:
            with (
                patch("app.routers.investigations.AsyncSessionLocal", session_factory),
                patch.object(OpenAIAgentModel, "_chat", agent_chat),
                patch.object(RegressionTestGenerator, "_chat", test_chat),
                patch.object(ArbiterAgent, "_chat", arbiter_chat),
                patch("app.routers.investigations.GitHubClient", return_value=github),
                patch(
                    "app.integrations.jira_client.JiraClient.add_comment",
                    new_callable=AsyncMock,
                ),
                patch.object(settings, "github_token", "demo-token-not-a-credential"),
                patch.object(settings, "jira_base_url", ""),
                patch.object(settings, "jira_api_token", ""),
            ):
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://tracelab.demo"
                ) as client:
                    created = await client.post(
                        "/api/investigations",
                        json={
                            "jira_issue_id": "DEMO-421",
                            "repository": str(repository),
                            "base_branch": "main",
                            "symptom": "Duplicate ReviewRun rows appear for concurrent requests",
                            "expected": "One run per input hash",
                            "actual": "Two rows may be inserted",
                            "affected_area": "review persistence",
                            "error_type": "deterministic",
                            "known_evidence": [
                                "Two requests can pass the lookup before insertion"
                            ],
                        },
                    )
                    assert created.status_code == 201, created.text
                    investigation_id = created.json()["id"]
                    detail = await client.get(f"/api/investigations/{investigation_id}")
                    assert detail.status_code == 200, detail.text
                    diagnosis = detail.json()["diagnosis"]
                    assert detail.json()["status"] == "WAITING_FOR_REVIEW", detail.text
                    assert diagnosis and diagnosis["selected_hypothesis_id"]
                    hypotheses_response = await client.get(
                        f"/api/investigations/{investigation_id}/hypotheses"
                    )
                    hypotheses = hypotheses_response.json()
                    assert {h["agent_type"]: h["status"] for h in hypotheses} == {
                        name: data["expected_status"]
                        for name, data in scripted_hypotheses.items()
                    }, hypotheses
                    blocked = await client.post(
                        f"/api/investigations/{investigation_id}/pull-request"
                    )
                    assert blocked.status_code == 409
                    assert not github.pr_calls
                    approved = await client.post(
                        f"/api/investigations/{investigation_id}/approve"
                    )
                    assert approved.status_code == 200, approved.text
                    created_pr = await client.post(
                        f"/api/investigations/{investigation_id}/pull-request"
                    )
                    assert created_pr.status_code == 200, created_pr.text
                    assert created_pr.json()["pr_url"] == MOCK_PR_URL
                    repeated = await client.post(
                        f"/api/investigations/{investigation_id}/pull-request"
                    )
                    assert repeated.json()["pr_url"] == MOCK_PR_URL
                    assert len(github.push_calls) == len(github.pr_calls) == 1

            async with session_factory() as session:
                verified = (
                    await session.execute(
                        select(Hypothesis).where(
                            Hypothesis.investigation_id == investigation_id,
                            Hypothesis.status == HypothesisStatus.VERIFIED,
                        )
                    )
                ).scalar_one()
                patch_row = (
                    await session.execute(
                        select(Patch).where(Patch.hypothesis_id == verified.id)
                    )
                ).scalar_one()
                evidence = (
                    await session.execute(
                        select(TestEvidence).where(
                            TestEvidence.hypothesis_id == verified.id
                        )
                    )
                ).scalar_one()
                assert evidence.pre_fix_result == "FAIL"
                assert evidence.post_fix_result == "PASS"
                assert evidence.existing_suite_result == "PASS"
                assert "review_repository.py" in patch_row.files_changed
                assert "tests/test_generated_race.py" in patch_row.files_changed
                assert patch_row.pr_url == MOCK_PR_URL
                assert "tests/test_generated_race.py" in github.push_calls[0]["diff"]
                assert github.push_calls[0]["base_branch"] == "main"
                assert github.pr_calls[0]["base"] == "main"
                assert "DEMO-421" in github.pr_calls[0]["title"]
                assert "Regression test failed before" in github.pr_calls[0]["body"]
            return {
                "issue_id": "DEMO-421",
                "investigation_id": investigation_id,
                "status": "APPROVED",
                "hypotheses": {h["agent_type"]: h["status"] for h in hypotheses},
                "verified_hypothesis_id": verified.id,
                "patch_files": patch_row.files_changed,
                "pre_fix": evidence.pre_fix_result,
                "post_fix": evidence.post_fix_result,
                "existing_suite": evidence.existing_suite_result,
                "mock_pr_url": MOCK_PR_URL,
                "duration_seconds": round(time.monotonic() - started, 2),
            }
        finally:
            app.dependency_overrides.pop(get_session, None)
            await engine.dispose()


def main() -> None:
    report = asyncio.run(run_demo())
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
