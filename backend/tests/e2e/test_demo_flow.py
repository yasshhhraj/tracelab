"""Exercise the whole seeded API journey in an isolated child process."""

import json
import subprocess
import sys
from pathlib import Path


def test_hero_demo_flow():
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "run_demo.py")],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    report = json.loads(result.stdout)
    assert report["hypotheses"] == {
        "code_path": "VERIFIED",
        "git_history": "REJECTED",
        "test_behavior": "REJECTED",
    }
    assert report["pre_fix"] == "FAIL"
    assert report["post_fix"] == report["existing_suite"] == "PASS"
    assert report["mock_pr_url"].startswith("https://github.test/")
    assert "tests/test_generated_race.py" in report["patch_files"]
