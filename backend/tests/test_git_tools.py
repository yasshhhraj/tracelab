from pathlib import Path

import pytest
import pytest_asyncio

from app.tools.executor import run_command
from app.tools.git_tools import apply_patch, git_blame, git_diff, git_log, git_show


@pytest_asyncio.fixture
async def git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo with two commits."""
    repo = tmp_path / "repo"
    repo.mkdir()

    await run_command(["git", "init"], cwd=repo)
    await run_command(["git", "config", "user.email", "test@tracelab.ai"], cwd=repo)
    await run_command(["git", "config", "user.name", "TraceLab Test"], cwd=repo)

    # First commit
    (repo / "service.py").write_text("def hello():\n    return 'hello'\n")
    await run_command(["git", "add", "."], cwd=repo)
    await run_command(["git", "commit", "-m", "initial commit"], cwd=repo)

    # Second commit — modify the file
    (repo / "service.py").write_text("def hello():\n    return 'hello world'\n")
    await run_command(["git", "add", "."], cwd=repo)
    await run_command(["git", "commit", "-m", "update greeting"], cwd=repo)

    return repo


async def _get_shas(repo: Path) -> list[str]:
    result = await run_command(["git", "log", "--format=%H", "--reverse"], cwd=repo)
    return result.stdout.strip().splitlines()


@pytest.mark.asyncio
async def test_git_log_returns_commits(git_repo):
    result = await git_log(git_repo, n=10)
    assert result.exit_code == 0
    assert "initial commit" in result.stdout
    assert "update greeting" in result.stdout


@pytest.mark.asyncio
async def test_git_log_filters_by_path(git_repo):
    result = await git_log(git_repo, path="service.py", n=10)
    assert result.exit_code == 0
    assert "initial commit" in result.stdout


@pytest.mark.asyncio
async def test_git_log_respects_n_limit(git_repo):
    result = await git_log(git_repo, n=1)
    assert result.exit_code == 0
    # Only one line expected for n=1
    assert len(result.stdout.strip().splitlines()) == 1


@pytest.mark.asyncio
async def test_git_blame_returns_output(git_repo):
    result = await git_blame(git_repo, "service.py")
    assert result.exit_code == 0
    assert "hello" in result.stdout


@pytest.mark.asyncio
async def test_git_diff_between_commits(git_repo):
    shas = await _get_shas(git_repo)
    result = await git_diff(git_repo, base=shas[0], head=shas[1])
    assert result.exit_code == 0
    assert "hello world" in result.stdout


@pytest.mark.asyncio
async def test_git_diff_filters_by_path(git_repo):
    shas = await _get_shas(git_repo)
    result = await git_diff(git_repo, base=shas[0], head=shas[1], path="service.py")
    assert result.exit_code == 0
    assert "service.py" in result.stdout


@pytest.mark.asyncio
async def test_git_show_commit(git_repo):
    shas = await _get_shas(git_repo)
    result = await git_show(git_repo, ref=shas[0])
    assert result.exit_code == 0
    assert "initial commit" in result.stdout


@pytest.mark.asyncio
async def test_apply_patch_check_only(git_repo):
    """apply_patch --check verifies a valid patch without modifying files."""
    valid_diff = (
        "--- a/service.py\n"
        "+++ b/service.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def hello():\n"
        "-    return 'hello world'\n"
        "+    return 'hello universe'\n"
    )
    result = await apply_patch(git_repo, diff=valid_diff, check_only=True)
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_apply_patch_modifies_file(git_repo):
    valid_diff = (
        "--- a/service.py\n"
        "+++ b/service.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def hello():\n"
        "-    return 'hello world'\n"
        "+    return 'hello universe'\n"
    )
    result = await apply_patch(git_repo, diff=valid_diff)
    assert result.exit_code == 0
    assert "hello universe" in (git_repo / "service.py").read_text()


@pytest.mark.asyncio
async def test_apply_patch_invalid_diff_fails(git_repo):
    """A completely wrong diff should fail with non-zero exit."""
    bad_diff = "this is not a valid patch\n"
    result = await apply_patch(git_repo, diff=bad_diff)
    assert result.exit_code != 0
