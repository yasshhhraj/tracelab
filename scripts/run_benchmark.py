"""Run ten isolated scripted CP-13 cases through the real verifier and arbiter."""

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT / "backend")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
os.environ["PATH"] = f"{ROOT / 'backend' / '.venv' / 'bin'}:{os.environ['PATH']}"

from app.agents.arbiter_agent import ArbiterAgent
from app.db.base import Base
from app.db.models import (
    Experiment,
    Hypothesis,
    Investigation,
    InvestigationStatus,
    Patch,
    TestEvidence,
)
from app.schemas.bug_context import BugContext
from app.verification.engine import VerificationEngine
from app.verification.regression_test_generator import (
    RegressionTestGenerator,
)
from app.worktree.manager import WorktreeManager, make_branch_name
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from scripts.benchmark_cases import CASES
from scripts.seed_demo import FIXTURES, hero_patches, seed, unified_patch

MANIFEST = FIXTURES / "benchmark_manifest.json"
AGENTS = ("code_path", "git_history", "test_behavior")


def _git(repo: Path, *args: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": "2024-01-01T12:00:00+0000",
        "GIT_COMMITTER_DATE": "2024-01-01T12:00:00+0000",
    }
    subprocess.run(
        ["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True
    )


def _tool_call(name: str, arguments: dict) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "benchmark-tool",
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


def _seed_generic(case_id: str, parent: Path) -> tuple[Path, dict[str, str], str]:
    case = CASES[case_id]
    repo = parent / case_id
    (repo / "tests").mkdir(parents=True)
    (repo / "repro").mkdir()
    (repo / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\npythonpath = ["."]\n'
    )
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n.delivery-count\n")
    (repo / "subject.py").write_text(case.fixed)
    (repo / "tests" / "test_existing.py").write_text(case.existing)
    (repo / "repro" / "test_bug.py").write_text(case.repro or case.regression)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "TraceLab Benchmark")
    _git(repo, "config", "user.email", "benchmark@tracelab.invalid")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Baseline behavior")
    (repo / "subject.py").write_text(case.buggy)
    _git(repo, "add", "subject.py")
    _git(repo, "commit", "-m", f"Introduce {case_id} regression")
    patches = {
        "code_path": unified_patch("subject.py", case.buggy, case.fixed),
        "git_history": unified_patch(
            "subject.py",
            case.buggy,
            case.buggy + "\ndef invalidate_cache():\n    return None\n",
        ),
        "test_behavior": unified_patch(
            "subject.py",
            case.buggy,
            case.buggy + "\ndef normalize_input(value):\n    return value.strip()\n",
        ),
    }
    return repo, patches, case.regression


def _validate_manifest(manifest: list[dict]) -> None:
    expected = {
        "logic": 3,
        "data/persistence": 2,
        "concurrency": 2,
        "regression": 2,
        "flaky": 1,
    }
    assert len(manifest) == 10
    assert len({case["id"] for case in manifest}) == 10
    assert {
        category: sum(c["category"] == category for c in manifest)
        for category in expected
    } == expected
    assert {case["id"] for case in manifest} == set(CASES) | {"concurrency-review"}
    for case in manifest:
        assert case["cause"] and case["expected_source"]


async def _run_case(spec: dict, parent: Path) -> dict:
    case_id = spec["id"]
    case_dir = parent / case_id
    case_dir.mkdir()
    if case_id == "concurrency-review":
        repo = seed(case_dir)
        patches = hero_patches()
        generated = (FIXTURES / "hero_regression_test.py.txt").read_text()
        repro = "repro/test_concurrent_review.py"
        test_path = "tests/test_generated_race.py"
    else:
        repo, patches, generated = _seed_generic(case_id, case_dir)
        repro = "repro/test_bug.py"
        test_path = "tests/test_generated.py"
    engine = create_async_engine(f"sqlite+aiosqlite:///{case_dir / 'case.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    issue = "BENCH-" + case_id.upper()
    context = BugContext(
        issue_id=issue,
        symptom=spec["cause"],
        expected="Regression test passes",
        actual="Regression test fails",
        affected_area=spec["category"],
        error_type="intermittent" if spec["category"] == "flaky" else "deterministic",
        known_evidence=[repro],
        repository=str(repo),
        base_branch="main",
    )
    async with sf() as session:
        investigation = Investigation(
            external_issue_id=issue,
            repository=str(repo),
            base_branch="main",
            status=InvestigationStatus.VERIFYING,
            bug_context=context.model_dump(),
        )
        session.add(investigation)
        await session.flush()
        investigation_id = investigation.id
        hypotheses = []
        for agent in AGENTS:
            hyp = Hypothesis(
                investigation_id=investigation_id,
                agent_type=agent,
                summary=spec["cause"]
                if agent == "code_path"
                else (
                    "Stale cache" if agent == "git_history" else "Input normalization"
                ),
                reasoning_summary="Scripted benchmark hypothesis",
                candidate_fix=patches[agent],
                suspected_files=[spec["expected_source"]],
                reproduction_plan=[
                    "tests/test_existing.py"
                    if spec["category"] == "flaky" and agent != "code_path"
                    else repro
                ],
                confidence="low" if agent == "code_path" else "high",
            )
            session.add(hyp)
            hypotheses.append(hyp)
        await session.commit()

    async def generated_chat(self, messages, tools):
        return _tool_call(
            "emit_test",
            {
                "test_file_path": test_path,
                "test_content": generated,
            },
        )

    async def arbiter_chat(self, messages, tools):
        winner = re.search(
            r"id=([a-f0-9-]+) agent_type=code_path status=VERIFIED",
            messages[1]["content"],
        )
        if winner is None:
            raise AssertionError(
                "Verified code-path hypothesis missing from arbiter evidence"
            )
        return _tool_call(
            "emit_diagnosis",
            {
                "selected_hypothesis_id": winner.group(1),
                "summary": spec["cause"],
                "verified_cause": spec["cause"],
                "evidence": ["Fail before, pass after, existing suite pass"],
                "rejected_hypotheses": [],
                "changed_files": [],
                "risk": "low",
                "recommended_action": "Review the verified patch",
            },
        )

    manager = WorktreeManager(base_dir=case_dir / "worktrees")
    started = time.monotonic()
    try:
        with (
            patch.object(RegressionTestGenerator, "_chat", generated_chat),
            patch.object(ArbiterAgent, "_chat", arbiter_chat),
        ):
            verifier = VerificationEngine(sf)
            statuses = {}
            for number, hyp in enumerate(hypotheses, 1):
                worktree = await manager.create(
                    repo, make_branch_name(issue, number), hyp.id
                )
                statuses[hyp.agent_type] = await verifier.verify(
                    hyp.id, worktree, context
                )
                print(f"  {hyp.agent_type}: {statuses[hyp.agent_type]}", flush=True)
                await manager.destroy(hyp.id)
            diagnosis = await ArbiterAgent().run(investigation_id, sf)
        async with sf() as session:
            investigation = await session.get(Investigation, investigation_id)
            investigation.status = InvestigationStatus.WAITING_FOR_REVIEW
            await session.commit()
            rows = (
                (
                    await session.execute(
                        select(Hypothesis).where(
                            Hypothesis.investigation_id == investigation_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            winner = next((h for h in rows if h.status == "VERIFIED"), None)
            evidence = None
            patch_row = None
            reproduced = False
            if winner:
                evidence = (
                    await session.execute(
                        select(TestEvidence).where(
                            TestEvidence.hypothesis_id == winner.id
                        )
                    )
                ).scalar_one_or_none()
                patch_row = (
                    await session.execute(
                        select(Patch).where(Patch.hypothesis_id == winner.id)
                    )
                ).scalar_one_or_none()
                experiments = (
                    (
                        await session.execute(
                            select(Experiment).where(
                                Experiment.hypothesis_id == winner.id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                reproduced = any(
                    e.command.startswith("reproduce:")
                    and e.exit_code != 0
                    and not e.timed_out
                    for e in experiments
                )
        proof = bool(
            evidence
            and reproduced
            and evidence.pre_fix_result == "FAIL"
            and evidence.post_fix_result == "PASS"
            and evidence.existing_suite_result == "PASS"
            and patch_row
        )
        correct = bool(
            proof
            and winner
            and winner.agent_type == "code_path"
            and diagnosis.selected_hypothesis_id == winner.id
            and patch_row
            and spec["expected_source"] in patch_row.files_changed
            and test_path in patch_row.files_changed
            and statuses
            == {
                "code_path": "VERIFIED",
                "git_history": "REJECTED",
                "test_behavior": "REJECTED",
            }
        )
        return {
            "id": case_id,
            "category": spec["category"],
            "status": "PASS" if correct else "FAIL",
            "hypothesis_coverage": len(rows),
            "hypothesis_statuses": statuses,
            "selected_agent": winner.agent_type if winner else None,
            "proof_complete": proof,
            "reproduced": reproduced,
            "pre_fix": evidence.pre_fix_result if evidence else None,
            "post_fix": evidence.post_fix_result if evidence else None,
            "existing_suite": evidence.existing_suite_result if evidence else None,
            "flaky_runs": evidence.runs if evidence else 0,
            "flaky_failures_before": evidence.failures_before if evidence else 0,
            "flaky_failures_after": evidence.failures_after if evidence else 0,
            "duration_seconds": round(time.monotonic() - started, 2),
        }
    finally:
        await manager.destroy_all()
        await engine.dispose()


async def run_benchmark() -> dict:
    manifest = json.loads(MANIFEST.read_text())
    _validate_manifest(manifest)
    cases = []
    with tempfile.TemporaryDirectory(prefix="tracelab-benchmark-") as temp:
        for spec in manifest:
            print(f"Running {spec['id']}...", flush=True)
            try:
                cases.append(await _run_case(spec, Path(temp)))
            except Exception as error:  # noqa: BLE001 - include infrastructure failures in denominator
                cases.append(
                    {
                        "id": spec["id"],
                        "category": spec["category"],
                        "status": "FAIL",
                        "error": str(error)[:300],
                    }
                )
    passed = sum(case["status"] == "PASS" for case in cases)
    verified = [case for case in cases if case.get("selected_agent")]
    durations = [
        case["duration_seconds"] for case in cases if "duration_seconds" in case
    ]
    coverage_count = sum(c.get("hypothesis_coverage") == 3 for c in cases)
    proof_count = sum(c.get("proof_complete") is True for c in verified)
    timing_unavailable = [c["id"] for c in cases if "duration_seconds" not in c]
    over_three_minutes = [c["id"] for c in cases if c.get("duration_seconds", 0) > 180]
    report = {
        "benchmark_type": "scripted pipeline correctness; not live LLM diagnostic accuracy",
        "cases": cases,
        "verified_resolution_rate": f"{passed}/10",
        "target_met": (
            passed >= 8
            and coverage_count == 10
            and proof_count == len(verified)
            and not timing_unavailable
            and not over_three_minutes
        ),
        "hypothesis_coverage": f"{coverage_count}/10",
        "regression_proof_rate": f"{proof_count}/{len(verified)}",
        "human_acceptance_rate": "not measured",
        "mean_seconds_to_diagnosis": (
            round(sum(durations) / len(durations), 2) if durations else None
        ),
        "timing_unavailable": timing_unavailable,
        "over_three_minutes": over_three_minutes,
    }
    return report


def main() -> None:
    report = asyncio.run(run_benchmark())
    output = ROOT / "backend" / "output" / "cp13-benchmark.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    for case in report["cases"]:
        print(
            f"{case['id']:<24} {case['status']:<5} {case.get('duration_seconds', 0):>6}s"
            + (f"  {case['error']}" if case.get("error") else "")
        )
    print(
        f"Verified resolution: {report['verified_resolution_rate']}; report: {output}"
    )
    if any(case["status"] != "PASS" for case in report["cases"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
