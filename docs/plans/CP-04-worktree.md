# CP-04 — Git Worktree Manager

> **Goal:** Create and destroy isolated `git worktree` environments per hypothesis, so each agent patch attempt has its own independent file tree.  
> **Exit state:** Three worktrees for the same repo can coexist with independent file modifications; `destroy()` removes both the directory and the git worktree reference; all tests pass; `ruff` is clean.  
> **Builds on:** CP-03 (`run_command` from `app.tools.executor` — all git operations go through the allowlisted tool layer).

---

## 1. Why Worktrees

The verification engine (CP-07) needs to:
1. Apply hypothesis A's patch to its own isolated tree.
2. Apply hypothesis B's patch to a different tree.
3. Run tests independently in each — without any cross-contamination.

`git worktree add` creates a second working tree linked to the same object database. The main repo checkout is never modified. Each worktree gets its own branch.

```
main repo (.git/)
 ├── worktrees/inv-abc-h1/   ← Hypothesis 1 patch applied here
 ├── worktrees/inv-abc-h2/   ← Hypothesis 2 patch applied here
 └── worktrees/inv-abc-h3/   ← Hypothesis 3 patch applied here
```

Branch naming (PRD §21):

```
ai-debug/{ISSUE_ID}-h{N}    e.g.   ai-debug/PVS-421-h2
```

---

## 2. Files to Create

```
backend/
├── app/
│   └── worktree/
│       ├── __init__.py
│       └── manager.py       # WorktreeManager class
└── tests/
    └── test_worktree.py     # all worktree tests
```

---

## 3. File-by-File Specification

### 3.1 `backend/app/worktree/__init__.py`

```python
# empty — marks worktree as a package
```

---

### 3.2 `backend/app/worktree/manager.py`

```python
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from app.tools.executor import run_command


@dataclass
class WorktreeRecord:
    hypothesis_id: str
    repo_path: Path
    worktree_path: Path
    branch: str


class WorktreeError(Exception):
    """Raised when a worktree operation fails."""


class WorktreeNotFoundError(WorktreeError):
    """Raised when a hypothesis_id has no registered worktree."""

    def __init__(self, hypothesis_id: str) -> None:
        super().__init__(f"No worktree registered for hypothesis '{hypothesis_id}'")
        self.hypothesis_id = hypothesis_id


class WorktreeManager:
    """
    Manages isolated git worktrees, one per hypothesis.

    Usage:
        manager = WorktreeManager(base_dir=Path("/tmp/tracelab/worktrees"))
        path = await manager.create(
            repo_path=Path("/repos/myapp"),
            branch="ai-debug/PVS-421-h2",
            hypothesis_id="h2",
        )
        # ... agents work inside `path` ...
        await manager.destroy("h2")

    All git operations go through `run_command` (allowlisted tool layer).
    """

    def __init__(self, base_dir: Path | None = None) -> None:
        # Default location: a sibling directory of the repo, avoids nesting
        # worktrees inside the repo itself (confuses some git tooling).
        self._base_dir = base_dir
        self._registry: dict[str, WorktreeRecord] = {}

    # ── public API ────────────────────────────────────────────────────────────

    async def create(
        self,
        repo_path: Path,
        branch: str,
        hypothesis_id: str,
    ) -> Path:
        """
        Create a new git worktree for `hypothesis_id` on `branch`.

        If `branch` does not exist yet in the repo it is created from HEAD.
        The worktree directory is placed under `base_dir / hypothesis_id`.

        Args:
            repo_path:     absolute path to the git repository root
            branch:        branch name to create for this worktree
                           (convention: "ai-debug/{ISSUE_ID}-h{N}")
            hypothesis_id: unique identifier (used as directory name)

        Returns:
            Path to the new worktree directory

        Raises:
            WorktreeError:          if the git command fails
            FileExistsError:        if a worktree already exists for this id
        """
        if hypothesis_id in self._registry:
            raise FileExistsError(
                f"Worktree for hypothesis '{hypothesis_id}' already exists at "
                f"{self._registry[hypothesis_id].worktree_path}"
            )

        worktree_path = self._worktree_dir(repo_path, hypothesis_id)
        worktree_path.parent.mkdir(parents=True, exist_ok=True)

        # Create the branch from HEAD if it doesn't exist, then add worktree
        result = await run_command(
            ["git", "worktree", "add", "-b", branch, str(worktree_path)],
            cwd=repo_path,
        )

        if result.exit_code != 0:
            # Branch may already exist — retry without -b
            result = await run_command(
                ["git", "worktree", "add", str(worktree_path), branch],
                cwd=repo_path,
            )
            if result.exit_code != 0:
                raise WorktreeError(
                    f"Failed to create worktree for '{hypothesis_id}': {result.stderr}"
                )

        record = WorktreeRecord(
            hypothesis_id=hypothesis_id,
            repo_path=repo_path,
            worktree_path=worktree_path,
            branch=branch,
        )
        self._registry[hypothesis_id] = record
        return worktree_path

    async def destroy(self, hypothesis_id: str) -> None:
        """
        Remove the worktree for `hypothesis_id`.

        Runs `git worktree remove --force` then deletes the directory if any
        files remain.

        Raises:
            WorktreeNotFoundError: if no worktree is registered for this id
        """
        if hypothesis_id not in self._registry:
            raise WorktreeNotFoundError(hypothesis_id)

        record = self._registry.pop(hypothesis_id)

        # Ask git to unregister the worktree
        await run_command(
            ["git", "worktree", "remove", "--force", str(record.worktree_path)],
            cwd=record.repo_path,
        )

        # Remove any leftover files (dirty worktrees after --force may leave some)
        if record.worktree_path.exists():
            shutil.rmtree(record.worktree_path, ignore_errors=True)

    def get_path(self, hypothesis_id: str) -> Path:
        """
        Return the worktree path for `hypothesis_id`.

        Raises:
            WorktreeNotFoundError: if no worktree is registered for this id
        """
        if hypothesis_id not in self._registry:
            raise WorktreeNotFoundError(hypothesis_id)
        return self._registry[hypothesis_id].worktree_path

    def get_branch(self, hypothesis_id: str) -> str:
        """Return the branch name for `hypothesis_id`."""
        if hypothesis_id not in self._registry:
            raise WorktreeNotFoundError(hypothesis_id)
        return self._registry[hypothesis_id].branch

    def list_ids(self) -> list[str]:
        """Return all currently registered hypothesis IDs."""
        return list(self._registry.keys())

    async def destroy_all(self) -> None:
        """Destroy every registered worktree. Safe to call on teardown."""
        for hid in list(self._registry.keys()):
            try:
                await self.destroy(hid)
            except Exception:  # noqa: BLE001
                pass  # Best-effort cleanup; log in production

    # ── helpers ───────────────────────────────────────────────────────────────

    def _worktree_dir(self, repo_path: Path, hypothesis_id: str) -> Path:
        if self._base_dir is not None:
            return self._base_dir / hypothesis_id
        # Default: place worktrees next to the repo directory
        return repo_path.parent / f"{repo_path.name}-worktrees" / hypothesis_id


# ── module-level helpers ─────────────────────────────────────────────────────


def make_branch_name(issue_id: str, hypothesis_index: int) -> str:
    """
    Produce the canonical branch name for a hypothesis worktree.

    PRD §21: ai-debug/{ISSUE_ID}-h{N}

    Args:
        issue_id:          Jira issue ID, e.g. "PVS-421"
        hypothesis_index:  1-based index (h1, h2, h3)

    Returns:
        e.g. "ai-debug/PVS-421-h2"
    """
    return f"ai-debug/{issue_id}-h{hypothesis_index}"
```

---

## 4. Tests

### 4.1 `backend/tests/test_worktree.py`

All tests use a real git repo created in `tmp_path`. No mocking.

```python
import pytest
import pytest_asyncio
from pathlib import Path

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
    """Core acceptance criterion: three worktrees for one repo, independently modifiable."""
    paths = []
    for i in range(1, 4):
        p = await manager.create(git_repo, make_branch_name("TST-1", i), f"h{i}")
        paths.append(p)

    # Verify all three directories exist
    for p in paths:
        assert p.exists()

    # Modify each worktree independently
    (paths[0] / "service.py").write_text("# patch A\n")
    (paths[1] / "service.py").write_text("# patch B\n")
    (paths[2] / "service.py").write_text("# patch C\n")

    # Confirm they are independent
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


@pytest.mark.asyncio
async def test_list_ids_empty_initially(manager):
    assert manager.list_ids() == []


@pytest.mark.asyncio
async def test_list_ids_returns_all(git_repo, manager):
    await manager.create(git_repo, make_branch_name("TST-1", 1), "h1")
    await manager.create(git_repo, make_branch_name("TST-1", 2), "h2")
    ids = manager.list_ids()
    assert set(ids) == {"h1", "h2"}


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
    """After destroy, `git worktree list` should not show the removed worktree."""
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


# ── default base_dir ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_default_base_dir_is_sibling_of_repo(git_repo):
    """When no base_dir given, worktrees are placed next to the repo."""
    manager = WorktreeManager()  # no base_dir
    path = await manager.create(git_repo, make_branch_name("TST-1", 1), "h1")
    try:
        # Should be a sibling: <repo_parent>/<repo_name>-worktrees/h1
        expected_parent = git_repo.parent / f"{git_repo.name}-worktrees"
        assert path.parent == expected_parent
    finally:
        await manager.destroy_all()
```

---

## 5. Implementation Order

| Step | Action | Verify |
|------|--------|--------|
| 1 | Create `app/worktree/__init__.py` | — |
| 2 | Create `app/worktree/manager.py` | `python -c "from app.worktree.manager import WorktreeManager"` |
| 3 | Create `tests/test_worktree.py` | — |
| 4 | `pytest tests/test_worktree.py -v` | All pass |
| 5 | `pytest` (full suite) | All 60 + new tests pass |
| 6 | `ruff check . && ruff format --check .` | Clean |

---

## 6. Key Design Decisions

### Why `git worktree` instead of `git clone`?

`git clone` copies the entire object store. `git worktree add` shares the object database — it is instantaneous regardless of repository size and uses only the working-tree disk space for the new files. Three worktrees = three working-tree copies + zero extra object copies.

### Why `--force` on `git worktree remove`?

During verification, patches may be applied and files may be modified. Without `--force`, git refuses to remove a "dirty" worktree. We want cleanup to always succeed even if the hypothesis left modified files.

### Why `shutil.rmtree` after `git worktree remove`?

In edge cases (crash mid-operation, OS signals), `git worktree remove` can partially fail but leave the directory. The `shutil.rmtree` call is a belt-and-suspenders cleanup.

### Why in-memory registry (`dict`)?

For the prototype, a single `WorktreeManager` instance lives for the lifetime of one investigation. Persistence into the DB is deferred to post-MVP. The manager is instantiated per-investigation in the orchestrator (CP-06).

### Why `destroy_all()` is best-effort?

If a worktree is already broken (e.g. disk was full), forcing an exception on cleanup would prevent other worktrees from being cleaned. Swallowing individual errors in `destroy_all` is intentional.

### Branch naming guardrails

`make_branch_name` is a pure function with no side effects. Tests assert the exact format from PRD §21. This function is the single source of truth — the orchestrator (CP-06) always calls it, never builds branch strings ad-hoc.

---

## 7. Acceptance Criteria (Definition of Done)

```
[ ] Three worktrees created from one repo coexist simultaneously
[ ] Each worktree has its own independent file state (modification to h1 does not affect h2 or h3)
[ ] Main repo is never modified by worktree operations
[ ] destroy() removes the directory from disk
[ ] destroy() removes the worktree from git worktree list
[ ] destroy() removes the entry from the registry (get_path raises WorktreeNotFoundError)
[ ] destroy_all() cleans up all registered worktrees
[ ] Duplicate create() raises FileExistsError
[ ] get_path/get_branch with unknown id raises WorktreeNotFoundError
[ ] make_branch_name("PVS-421", 2) == "ai-debug/PVS-421-h2"
[ ] pytest (full suite) → all tests pass, 0 failures
[ ] ruff check . && ruff format --check . → clean
```

---

## 8. What CP-04 Deliberately Does NOT Include

| Excluded | Added in |
|----------|----------|
| Worktree lifecycle triggered by investigation | CP-06 (orchestrator) |
| Applying agent patches inside a worktree | CP-07 (verification engine) |
| Persisting worktree paths to DB | Post-MVP |
| Max-worktrees limit enforcement | CP-06 (orchestrator applies `max_hypotheses=3`) |
| Remote repo cloning / SSH auth | CP-11 (Jira/GitHub intake) |
