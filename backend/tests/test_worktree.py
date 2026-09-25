from pathlib import Path

import pytest
import pytest_asyncio

from app.tools.executor import run_command
from app.worktree.manager import (
    WorktreeManager,
    WorktreeNotFoundError,
    make_branch_name,
)

# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def git_repo(tmp_path: Path) -> Path:
    """Minimal git repo with one commit — used as the source for worktrees."""
    repo = tmp_path / "main_repo"
    repo.mkdir()

    await run_command(["git", "init"], cwd=repo)
    await run_command(["git", "config", "user.email", "test@tracelab.ai"], cwd=repo)
    await run_command(["git", "config", "user.name", "TraceLab Test"], cwd=repo)

    (repo / "service.py").write_text("def process():\n    pass\n")
    await run_command(["git", "add", "."], cwd=repo)
    await run_command(["git", "commit", "-m", "initial"], cwd=repo)

    return repo


@pytest.fixture
def manager(tmp_path: Path) -> WorktreeManager:
    """WorktreeManager with an explicit base_dir inside tmp_path."""
    return WorktreeManager(base_dir=tmp_path / "worktrees")


# ── branch naming ─────────────────────────────────────────────────────────────


def test_make_branch_name_format():
    assert make_branch_name("PVS-421", 2) == "ai-debug/PVS-421-h2"


def test_make_branch_name_different_issues():
    assert make_branch_name("BUG-1", 1) == "ai-debug/BUG-1-h1"
    assert make_branch_name("FEAT-99", 3) == "ai-debug/FEAT-99-h3"


# ── create ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_returns_path(git_repo, manager):
    path = await manager.create(
        repo_path=git_repo,
        branch=make_branch_name("TST-1", 1),
        hypothesis_id="h1",
    )
    assert path.exists()
    assert path.is_dir()


@pytest.mark.asyncio
async def test_created_worktree_contains_repo_files(git_repo, manager):
    path = await manager.create(git_repo, make_branch_name("TST-1", 1), "h1")
    assert (path / "service.py").exists()


@pytest.mark.asyncio
async def test_three_worktrees_coexist(git_repo, manager):
    """Core acceptance criterion: 3 independent worktrees from one repo."""
    paths = []
    for i in range(1, 4):
        p = await manager.create(git_repo, make_branch_name("TST-1", i), f"h{i}")
        paths.append(p)

    # All three directories exist
    for p in paths:
        assert p.exists()

    # Modify each worktree independently
    (paths[0] / "service.py").write_text("# patch A\n")
    (paths[1] / "service.py").write_text("# patch B\n")
    (paths[2] / "service.py").write_text("# patch C\n")

    # Confirm each has its own content
    assert (paths[0] / "service.py").read_text() == "# patch A\n"
    assert (paths[1] / "service.py").read_text() == "# patch B\n"
    assert (paths[2] / "service.py").read_text() == "# patch C\n"

    # Main repo is untouched
    assert "patch" not in (git_repo / "service.py").read_text()


@pytest.mark.asyncio
async def test_create_duplicate_raises(git_repo, manager):
    await manager.create(git_repo, make_branch_name("TST-1", 1), "h1")
    with pytest.raises(FileExistsError):
        await manager.create(git_repo, make_branch_name("TST-1", 1), "h1")


# ── get_path / get_branch ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_path_returns_correct_path(git_repo, manager):
    created = await manager.create(git_repo, make_branch_name("TST-1", 1), "h1")
    assert manager.get_path("h1") == created


@pytest.mark.asyncio
async def test_get_branch_returns_branch_name(git_repo, manager):
    branch = make_branch_name("TST-1", 1)
    await manager.create(git_repo, branch, "h1")
    assert manager.get_branch("h1") == branch


def test_get_path_unknown_id_raises(manager):
    with pytest.raises(WorktreeNotFoundError):
        manager.get_path("nonexistent")


def test_get_branch_unknown_id_raises(manager):
    with pytest.raises(WorktreeNotFoundError):
        manager.get_branch("nonexistent")


# ── list_ids ──────────────────────────────────────────────────────────────────


def test_list_ids_empty_initially(manager):
    assert manager.list_ids() == []


@pytest.mark.asyncio
async def test_list_ids_returns_all(git_repo, manager):
    await manager.create(git_repo, make_branch_name("TST-1", 1), "h1")
    await manager.create(git_repo, make_branch_name("TST-1", 2), "h2")
    assert set(manager.list_ids()) == {"h1", "h2"}


# ── destroy ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_destroy_removes_directory(git_repo, manager):
    path = await manager.create(git_repo, make_branch_name("TST-1", 1), "h1")
    assert path.exists()
    await manager.destroy("h1")
    assert not path.exists()


@pytest.mark.asyncio
async def test_destroy_removes_from_registry(git_repo, manager):
    await manager.create(git_repo, make_branch_name("TST-1", 1), "h1")
    await manager.destroy("h1")
    with pytest.raises(WorktreeNotFoundError):
        manager.get_path("h1")


@pytest.mark.asyncio
async def test_destroy_deregisters_git_worktree(git_repo, manager):
    """After destroy, git worktree list must not mention the removed path."""
    path = await manager.create(git_repo, make_branch_name("TST-1", 1), "h1")
    await manager.destroy("h1")
    result = await run_command(["git", "worktree", "list"], cwd=git_repo)
    assert str(path) not in result.stdout


@pytest.mark.asyncio
async def test_destroy_unknown_id_raises(manager):
    with pytest.raises(WorktreeNotFoundError):
        await manager.destroy("nonexistent")


# ── destroy_all ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_destroy_all_cleans_up_everything(git_repo, manager):
    paths = []
    for i in range(1, 4):
        p = await manager.create(git_repo, make_branch_name("TST-1", i), f"h{i}")
        paths.append(p)

    await manager.destroy_all()

    for p in paths:
        assert not p.exists()
    assert manager.list_ids() == []


@pytest.mark.asyncio
async def test_destroy_all_is_idempotent_on_empty(manager):
    """Calling destroy_all on an empty manager should not raise."""
    await manager.destroy_all()
    assert manager.list_ids() == []


# ── default base_dir ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_default_base_dir_is_sibling_of_repo(git_repo):
    """When no base_dir is given, worktrees are placed next to the repo."""
    mgr = WorktreeManager()  # no base_dir
    path = await mgr.create(git_repo, make_branch_name("TST-1", 1), "h1")
    try:
        expected_parent = git_repo.parent / f"{git_repo.name}-worktrees"
        assert path.parent == expected_parent
    finally:
        await mgr.destroy_all()
