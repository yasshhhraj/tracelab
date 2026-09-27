"""
Tests for CP-07: FlakyRunner.
"""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.tools.executor import CommandResult
from app.verification.flaky_runner import FlakyRunResult, run_flaky


def _make_result(exit_code: int) -> CommandResult:
    return CommandResult(
        command=["pytest", "tests/test_foo.py"],
        exit_code=exit_code,
        stdout="output" if exit_code != 0 else "passed",
        stderr="",
        duration_ms=10,
        timed_out=False,
    )


# ── clamping tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_flaky_runner_clamps_below_min_runs(tmp_path: Path):
    """Requesting fewer than flaky_min_runs (20) is clamped to 20."""
    call_count = {"n": 0}

    async def fake_run_test(**kwargs):
        call_count["n"] += 1
        return _make_result(0)

    with patch("app.verification.flaky_runner.run_test", new=AsyncMock(side_effect=fake_run_test)):
        result = await run_flaky(worktree_path=tmp_path, test_path="tests/test_foo.py", n_runs=5)

    assert result.runs == 20  # clamped to min
    assert call_count["n"] == 20


@pytest.mark.asyncio
async def test_flaky_runner_clamps_above_max_runs(tmp_path: Path):
    """Requesting more than flaky_max_runs (100) is clamped to 100."""
    call_count = {"n": 0}

    async def fake_run_test(**kwargs):
        call_count["n"] += 1
        return _make_result(0)

    with patch("app.verification.flaky_runner.run_test", new=AsyncMock(side_effect=fake_run_test)):
        result = await run_flaky(worktree_path=tmp_path, test_path="tests/test_foo.py", n_runs=200)

    assert result.runs == 100  # clamped to max
    assert call_count["n"] == 100


@pytest.mark.asyncio
async def test_flaky_runner_exact_min_runs_not_clamped(tmp_path: Path):
    """Requesting exactly flaky_min_runs (20) is not changed."""
    call_count = {"n": 0}

    async def fake_run_test(**kwargs):
        call_count["n"] += 1
        return _make_result(0)

    with patch("app.verification.flaky_runner.run_test", new=AsyncMock(side_effect=fake_run_test)):
        result = await run_flaky(worktree_path=tmp_path, test_path="tests/test_foo.py", n_runs=20)

    assert result.runs == 20
    assert call_count["n"] == 20


# ── failure counting tests ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_flaky_runner_all_pass_returns_zero_failures(tmp_path: Path):
    """All runs pass → failures == 0, failure_rate == 0.0."""

    async def fake_run_test(**kwargs):
        return _make_result(0)

    with patch("app.verification.flaky_runner.run_test", new=AsyncMock(side_effect=fake_run_test)):
        result = await run_flaky(worktree_path=tmp_path, test_path="tests/test_foo.py", n_runs=20)

    assert result.failures == 0
    assert result.failure_rate == 0.0
    assert result.output_samples == []


@pytest.mark.asyncio
async def test_flaky_runner_all_fail_returns_n_failures(tmp_path: Path):
    """All runs fail → failures == n_runs."""

    async def fake_run_test(**kwargs):
        return _make_result(1)

    with patch("app.verification.flaky_runner.run_test", new=AsyncMock(side_effect=fake_run_test)):
        result = await run_flaky(worktree_path=tmp_path, test_path="tests/test_foo.py", n_runs=20)

    assert result.failures == 20
    assert result.failure_rate == 1.0


@pytest.mark.asyncio
async def test_flaky_runner_counts_failures_correctly(tmp_path: Path):
    """First 5 of 20 runs fail; remaining 15 pass."""
    call_count = {"n": 0}

    async def fake_run_test(**kwargs):
        n = call_count["n"]
        call_count["n"] += 1
        return _make_result(1 if n < 5 else 0)

    with patch("app.verification.flaky_runner.run_test", new=AsyncMock(side_effect=fake_run_test)):
        result = await run_flaky(worktree_path=tmp_path, test_path="tests/test_foo.py", n_runs=20)

    assert result.failures == 5
    assert result.runs == 20
    assert abs(result.failure_rate - 0.25) < 1e-9


@pytest.mark.asyncio
async def test_flaky_runner_collects_output_samples(tmp_path: Path):
    """Output samples are collected for up to 3 failing runs."""
    call_count = {"n": 0}

    async def fake_run_test(**kwargs):
        n = call_count["n"]
        call_count["n"] += 1
        ec = 1 if n < 10 else 0
        return CommandResult(
            command=["pytest"],
            exit_code=ec,
            stdout=f"FAIL run {n}" if ec != 0 else "pass",
            stderr="",
            duration_ms=1,
            timed_out=False,
        )

    with patch("app.verification.flaky_runner.run_test", new=AsyncMock(side_effect=fake_run_test)):
        result = await run_flaky(worktree_path=tmp_path, test_path="tests/test_foo.py", n_runs=20)

    # Should have at most 3 samples
    assert len(result.output_samples) <= 3
    assert result.failures == 10


@pytest.mark.asyncio
async def test_flaky_runner_result_model_fields(tmp_path: Path):
    """FlakyRunResult has the expected fields."""

    async def fake_run_test(**kwargs):
        return _make_result(0)

    with patch("app.verification.flaky_runner.run_test", new=AsyncMock(side_effect=fake_run_test)):
        result = await run_flaky(worktree_path=tmp_path, test_path="t.py", n_runs=20)

    assert isinstance(result, FlakyRunResult)
    assert isinstance(result.runs, int)
    assert isinstance(result.failures, int)
    assert isinstance(result.failure_rate, float)
    assert isinstance(result.output_samples, list)
