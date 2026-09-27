"""
FlakyRunner — CP-07

Handles the repeated-run loop for intermittent (flaky) bugs.

AGENTS.md constraint: minimum 20 runs before drawing conclusions.
Config: flaky_min_runs=20, flaky_default_runs=50, flaky_max_runs=100
"""

import logging
from pathlib import Path

from pydantic import BaseModel

from app.config import settings
from app.tools.test_runner import run_test

logger = logging.getLogger(__name__)


class FlakyRunResult(BaseModel):
    """Result of a repeated-run flaky test campaign."""

    runs: int
    failures: int
    failure_rate: float  # failures / runs
    output_samples: list[str]  # up to 3 stdout+stderr snippets from failing runs


async def run_flaky(
    worktree_path: Path,
    test_path: str,
    n_runs: int,
) -> FlakyRunResult:
    """
    Run `test_path` exactly `n_runs` times (sequential), counting failures.

    AGENTS.md constraint: n_runs is clamped to
        [settings.flaky_min_runs, settings.flaky_max_runs]
    Never fewer than flaky_min_runs (20) — do not draw conclusions on fewer runs.

    Runs are sequential (not concurrent) to avoid masking timing-dependent bugs
    and to prevent inter-run interference on shared resources.

    Args:
        worktree_path: absolute path to the project root (git worktree)
        test_path:     pytest node ID or file path for the regression test
        n_runs:        requested number of runs (clamped to allowed range)

    Returns:
        FlakyRunResult with failure count, rate, and output samples
    """
    min_runs = settings.flaky_min_runs
    max_runs = settings.flaky_max_runs

    effective_runs = max(min_runs, min(n_runs, max_runs))
    if effective_runs != n_runs:
        logger.info(
            "run_flaky: requested %d runs clamped to %d (allowed range [%d, %d])",
            n_runs,
            effective_runs,
            min_runs,
            max_runs,
        )

    failures = 0
    output_samples: list[str] = []

    for i in range(effective_runs):
        result = await run_test(
            repo_path=worktree_path,
            test_path=test_path,
        )
        if result.exit_code != 0:
            failures += 1
            if len(output_samples) < 3:
                # Capture a compact sample for evidence
                sample_lines = []
                if result.stdout:
                    sample_lines.append(result.stdout[-500:])  # last 500 chars
                if result.stderr:
                    sample_lines.append(result.stderr[-200:])
                output_samples.append("\n".join(sample_lines).strip())
        logger.debug("run_flaky run %d/%d: exit_code=%d", i + 1, effective_runs, result.exit_code)

    failure_rate = failures / effective_runs if effective_runs > 0 else 0.0
    logger.info(
        "run_flaky completed: %d/%d failures (%.1f%%)",
        failures,
        effective_runs,
        failure_rate * 100,
    )
    return FlakyRunResult(
        runs=effective_runs,
        failures=failures,
        failure_rate=failure_rate,
        output_samples=output_samples,
    )
