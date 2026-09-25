import contextlib
import shutil
from dataclasses import dataclass
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

    All git operations go through run_command (allowlisted tool layer).
    """

    def __init__(self, base_dir: Path | None = None) -> None:
        # Default: place worktrees next to the repo directory.
        # Avoids nesting worktrees inside the repo itself (confuses some git tooling).
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
            WorktreeError:   if the git command fails
            FileExistsError: if a worktree already exists for this id
        """
        if hypothesis_id in self._registry:
            raise FileExistsError(
                f"Worktree for hypothesis '{hypothesis_id}' already exists at "
                f"{self._registry[hypothesis_id].worktree_path}"
            )

        worktree_path = self._worktree_dir(repo_path, hypothesis_id)
        worktree_path.parent.mkdir(parents=True, exist_ok=True)

        # Try to create a new branch from HEAD and add the worktree
        result = await run_command(
            ["git", "worktree", "add", "-b", branch, str(worktree_path)],
            cwd=repo_path,
        )

        if result.exit_code != 0:
            # Branch may already exist — retry without -b (checkout existing branch)
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
        files remain (belt-and-suspenders for crashed/partial removals).

        Raises:
            WorktreeNotFoundError: if no worktree is registered for this id
        """
        if hypothesis_id not in self._registry:
            raise WorktreeNotFoundError(hypothesis_id)

        record = self._registry.pop(hypothesis_id)

        # Ask git to unregister the worktree (--force handles dirty worktrees)
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
            with contextlib.suppress(Exception):
                await self.destroy(hid)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _worktree_dir(self, repo_path: Path, hypothesis_id: str) -> Path:
        if self._base_dir is not None:
            return self._base_dir / hypothesis_id
        # Default: sibling of repo directory
        return repo_path.parent / f"{repo_path.name}-worktrees" / hypothesis_id


# ── module-level helpers ──────────────────────────────────────────────────────


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
