"""Seed the CP-13 review-race repository in a disposable directory."""

import difflib
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "demo_repo"
FIXTURES = ROOT / "backend" / "tests" / "fixtures" / "cp13"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def unified_patch(path: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def hero_patches() -> dict[str, str]:
    buggy_repository = (SOURCE / "review_repository.py").read_text()
    fixed_repository = (FIXTURES / "review_repository_fixed.py.txt").read_text()
    service = (SOURCE / "review_service.py").read_text()
    return {
        "code_path": unified_patch(
            "review_repository.py", buggy_repository, fixed_repository
        ),
        "git_history": unified_patch(
            "review_service.py",
            service,
            service.replace(
                "        existing = self.repository.find_by_hash(input_hash)",
                "        payload = payload.strip()\n"
                "        existing = self.repository.find_by_hash(input_hash)",
            ),
        ),
        "test_behavior": unified_patch(
            "review_service.py",
            service,
            service.replace(
                "        existing = self.repository.find_by_hash(input_hash)",
                "        input_hash = input_hash.strip()\n"
                "        existing = self.repository.find_by_hash(input_hash)",
            ),
        ),
    }


def seed(destination: Path) -> Path:
    """Return a new Git repo whose HEAD contains the intentionally buggy code."""
    repo = destination / "review-race"
    shutil.copytree(
        SOURCE,
        repo,
        ignore=shutil.ignore_patterns(
            "__pycache__", ".pytest_cache", "*.pyc", "*.sqlite"
        ),
    )
    repository_file = repo / "review_repository.py"
    buggy = repository_file.read_text()
    repository_file.write_text(
        (FIXTURES / "review_repository_fixed.py.txt").read_text()
    )
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "TraceLab Demo")
    _git(repo, "config", "user.email", "demo@tracelab.invalid")
    _git(repo, "remote", "add", "origin", "https://github.com/demo/review-race.git")
    original_author_date = os.environ.get("GIT_AUTHOR_DATE")
    original_committer_date = os.environ.get("GIT_COMMITTER_DATE")
    os.environ["GIT_AUTHOR_DATE"] = "2024-01-01T12:00:00+0000"
    os.environ["GIT_COMMITTER_DATE"] = "2024-01-01T12:00:00+0000"
    try:
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "Add atomic review-run persistence")
        repository_file.write_text(buggy)
        os.environ["GIT_AUTHOR_DATE"] = "2024-01-02T12:00:00+0000"
        os.environ["GIT_COMMITTER_DATE"] = "2024-01-02T12:00:00+0000"
        _git(repo, "add", "review_repository.py")
        _git(repo, "commit", "-m", "Simplify review-run insertion")
    finally:
        if original_author_date is None:
            os.environ.pop("GIT_AUTHOR_DATE", None)
        else:
            os.environ["GIT_AUTHOR_DATE"] = original_author_date
        if original_committer_date is None:
            os.environ.pop("GIT_COMMITTER_DATE", None)
        else:
            os.environ["GIT_COMMITTER_DATE"] = original_committer_date
    return repo
