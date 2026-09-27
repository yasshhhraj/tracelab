"""Validate the independent CP-13 benchmark's manifest and result contract."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_benchmark_cases() -> None:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_benchmark.py")],
        cwd=ROOT / "backend",
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout[-4000:] + completed.stderr[-4000:]
    report = json.loads((ROOT / "backend" / "output" / "cp13-benchmark.json").read_text())
    assert len(report["cases"]) == 10
    assert report["verified_resolution_rate"] == "10/10"
    assert report["hypothesis_coverage"] == "10/10"
    assert report["human_acceptance_rate"] == "not measured"
    flaky = next(case for case in report["cases"] if case["category"] == "flaky")
    assert flaky["flaky_runs"] == 50
    assert flaky["flaky_failures_before"] > 0
    assert flaky["flaky_failures_after"] == 0
