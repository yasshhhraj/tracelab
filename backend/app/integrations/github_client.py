"""
GitHub REST API v3 client — CP-12

Credentials are read from settings at construction time and NEVER placed in
any prompt, log line, or AgentContext (AGENTS.md).

Push and clone operations use an HTTPS URL without credentials. Git obtains
the token through a process-local askpass helper, never through argv or a
persisted remote URL.

Usage::

    client = GitHubClient()
    pr_url = await client.create_draft_pr(
        owner="org", repo="myapp",
        branch="ai-debug/PVS-421-h2",
        base="main",
        title="Fix PVS-421: duplicate ReviewRuns",
        body="...",
    )
"""

import logging
import tempfile
from pathlib import Path
from typing import Any

import httpx

from app.config import settings
from app.tools.executor import run_command

_LOGGER = logging.getLogger(__name__)

# ── Exceptions ────────────────────────────────────────────────────────────────


class GitHubClientError(Exception):
    """Base class for all GitHub client errors."""


class GitHubNotFoundError(GitHubClientError):
    """Raised when the repository or ref is not found (HTTP 404)."""


class GitHubAuthError(GitHubClientError):
    """Raised when credentials are rejected (HTTP 401 / 403)."""


class GitHubConflictError(GitHubClientError):
    """Raised when a branch or PR already exists (HTTP 409 / 422)."""


# ── Client ────────────────────────────────────────────────────────────────────


class GitHubClient:
    """
    Async GitHub REST API v3 client.

    The token is injected from settings at construction time and NEVER placed
    in any prompt, log line, or AgentContext (AGENTS.md).
    """

    def __init__(self, token: str | None = None) -> None:
        # _token is stored privately; never logged, never serialised.
        self._token: str = token or settings.github_token
        self._auth_header: str = f"Bearer {self._token}"
        self._api_url: str = getattr(settings, "github_api_url", "https://api.github.com")

    # ── helpers ───────────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": self._auth_header,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self._api_url}/{path.lstrip('/')}"

    def _git_auth_env(self) -> dict[str, str]:
        return {
            "GIT_ASKPASS": str(Path(__file__).with_name("git_askpass.sh").resolve()),
            "GIT_TERMINAL_PROMPT": "0",
            "TRACELAB_GITHUB_TOKEN": self._token,
        }

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code in (401, 403):
            raise GitHubAuthError(f"GitHub authentication failed (HTTP {response.status_code})")
        if response.status_code == 404:
            raise GitHubNotFoundError(f"GitHub resource not found (HTTP 404): {response.url}")
        if response.status_code in (409, 422):
            body = response.text.lower()
            if "already exists" in body or "reference already exists" in body:
                raise GitHubConflictError(f"Branch or PR already exists: {response.text[:200]}")
            raise GitHubClientError(
                f"GitHub conflict/validation error (HTTP {response.status_code}): "
                f"{response.text[:200]}"
            )
        if response.status_code >= 400:
            raise GitHubClientError(
                f"GitHub API error (HTTP {response.status_code}): {response.text[:200]}"
            )

    # ── push_branch ───────────────────────────────────────────────────────────

    async def push_branch(
        self,
        owner: str,
        repo: str,
        branch: str,
        local_repo: Path,
        force: bool = False,
    ) -> str:
        """
        Push a local branch to GitHub.

        Git receives the token through GIT_ASKPASS. The remote URL and command
        arguments contain no credential.

        Returns the HEAD commit SHA after a successful push.

        Raises:
            GitHubAuthError      — git reports auth failure in stderr
            GitHubNotFoundError  — repository not found
            GitHubClientError    — any other push failure
        """
        _LOGGER.info("Pushing branch '%s' to %s/%s", branch, owner, repo)
        remote_url = f"https://github.com/{owner}/{repo}.git"

        cmd = ["git", "push", remote_url, f"{branch}:{branch}"]
        if force:
            cmd.insert(2, "--force")

        result = await run_command(cmd, cwd=local_repo, env=self._git_auth_env())

        if result.exit_code != 0 or result.timed_out:
            stderr_lower = result.stderr.lower()
            auth_indicators = (
                "401" in result.stderr
                or "authentication" in stderr_lower
                or "credentials" in stderr_lower
            )
            if auth_indicators:
                raise GitHubAuthError(f"GitHub push authentication failure for {owner}/{repo}")
            not_found = "not found" in stderr_lower or (
                "repository" in stderr_lower and "404" in result.stderr
            )
            if not_found:
                raise GitHubNotFoundError(f"GitHub repository not found: {owner}/{repo}")
            if result.timed_out:
                raise GitHubClientError(
                    f"git push timed out for branch '{branch}' on {owner}/{repo}"
                )
            raise GitHubClientError(
                f"git push failed for branch '{branch}' on {owner}/{repo} (exit {result.exit_code})"
            )

        # Read HEAD SHA
        sha_result = await run_command(["git", "rev-parse", "HEAD"], cwd=local_repo)
        return sha_result.stdout.strip()

    # ── get_or_create_ref ─────────────────────────────────────────────────────

    async def get_or_create_ref(
        self,
        owner: str,
        repo: str,
        branch: str,
        sha: str,
    ) -> str:
        """
        Ensure a branch ref exists on GitHub pointing to `sha`.

        - If the ref doesn't exist (404): creates it via POST.
        - If the ref exists: updates it via PATCH (force).

        Returns the SHA the ref points to after the operation.
        """
        ref_path = f"refs/heads/{branch}"
        get_url = self._url(f"repos/{owner}/{repo}/git/ref/heads/{branch}")

        async with httpx.AsyncClient(timeout=30.0) as client:
            get_resp = await client.get(get_url, headers=self._headers())

        if get_resp.status_code == 404:
            # Create the ref
            _LOGGER.info("Creating ref %s at %s in %s/%s", ref_path, sha[:7], owner, repo)
            async with httpx.AsyncClient(timeout=30.0) as client:
                post_resp = await client.post(
                    self._url(f"repos/{owner}/{repo}/git/refs"),
                    headers=self._headers(),
                    json={"ref": ref_path, "sha": sha},
                )
            self._raise_for_status(post_resp)
            return post_resp.json()["object"]["sha"]

        self._raise_for_status(get_resp)
        # Update the ref
        _LOGGER.info("Updating ref %s to %s in %s/%s", ref_path, sha[:7], owner, repo)
        async with httpx.AsyncClient(timeout=30.0) as client:
            patch_resp = await client.patch(
                self._url(f"repos/{owner}/{repo}/git/refs/heads/{branch}"),
                headers=self._headers(),
                json={"sha": sha, "force": True},
            )
        self._raise_for_status(patch_resp)
        return patch_resp.json()["object"]["sha"]

    # ── create_draft_pr ───────────────────────────────────────────────────────

    async def create_draft_pr(
        self,
        owner: str,
        repo: str,
        branch: str,
        base: str,
        title: str,
        body: str,
    ) -> str:
        """
        Create a draft pull request.

        Returns the HTML URL of the created PR.

        Raises:
            GitHubConflictError  — PR already exists for this branch
            GitHubAuthError      — bad token
            GitHubNotFoundError  — repo not found
            GitHubClientError    — any other API error
        """
        _LOGGER.info("Creating draft PR in %s/%s: '%s' ← '%s'", owner, repo, base, branch)
        url = self._url(f"repos/{owner}/{repo}/pulls")
        payload: dict[str, Any] = {
            "title": title,
            "body": body,
            "head": branch,
            "base": base,
            "draft": True,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, headers=self._headers(), json=payload)

        self._raise_for_status(response)
        pr_url: str = response.json()["html_url"]
        _LOGGER.info("Draft PR created: %s", pr_url)
        return pr_url

    # ── worktree-less push (apply diff on fresh clone) ────────────────────────

    async def clone_apply_and_push(
        self,
        owner: str,
        repo: str,
        branch: str,
        base_branch: str,
        diff: str,
        commit_message: str,
    ) -> str:
        """
        Apply a diff onto a fresh clone of the repository and push a new branch.

        Used when no live worktree is available (the normal case at PR-creation
        time — worktrees are destroyed after verification).

        Flow:
            1. Create TemporaryDirectory.
            2. git clone --branch {base_branch} https://github.com/{owner}/{repo}.git ./repo
            3. git checkout -b {branch}
            4. Write diff to a temp file; git apply <file>
            5. Stage paths from the diff and commit
            6. git push {remote_url} {branch}:{branch}

        Returns the HEAD commit SHA.

        The temp directory is always cleaned up in a finally block.
        The token is supplied to Git only through GIT_ASKPASS.
        """
        _LOGGER.info("clone_apply_and_push: %s/%s → branch '%s'", owner, repo, branch)
        remote_url = f"https://github.com/{owner}/{repo}.git"

        tmp = tempfile.mkdtemp(prefix="tracelab-gh-")
        try:
            repo_dir = Path(tmp) / "repo"
            repo_dir.mkdir()

            # 1. Clone
            clone_result = await run_command(
                ["git", "clone", "--branch", base_branch, remote_url, str(repo_dir)],
                cwd=Path(tmp),
                env=self._git_auth_env(),
            )
            if clone_result.exit_code != 0 or clone_result.timed_out:
                raise GitHubClientError(
                    f"git clone failed for {owner}/{repo} (exit {clone_result.exit_code})"
                )

            # 2. Checkout new branch from base
            checkout_result = await run_command(
                ["git", "checkout", "-b", branch],
                cwd=repo_dir,
            )
            if checkout_result.exit_code != 0:
                raise GitHubClientError(f"git checkout -b failed: {checkout_result.stderr[:200]}")

            # 3. Write diff to temp file and apply
            if diff.strip():
                patch_file = Path(tmp) / "tracelab.patch"
                patch_file.write_text(diff, encoding="utf-8")
                apply_result = await run_command(
                    ["git", "apply", str(patch_file)],
                    cwd=repo_dir,
                )
                if apply_result.exit_code != 0:
                    raise GitHubClientError(f"git apply failed: {apply_result.stderr[:300]}")

            # 4. Commit
            # Configure git identity for the commit (required in CI/containers)
            await run_command(
                ["git", "config", "user.email", "tracelab@noreply.github.com"],
                cwd=repo_dir,
            )
            await run_command(
                ["git", "config", "user.name", "TraceLab"],
                cwd=repo_dir,
            )
            changed_paths = sorted(
                {line[6:] for line in diff.splitlines() if line.startswith(("+++ b/", "--- a/"))}
            )
            if not changed_paths:
                raise GitHubClientError("Patch contains no changed files")
            stage_result = await run_command(["git", "add", "--", *changed_paths], cwd=repo_dir)
            if stage_result.exit_code != 0:
                raise GitHubClientError(f"git add failed: {stage_result.stderr[:200]}")
            commit_result = await run_command(["git", "commit", "-m", commit_message], cwd=repo_dir)
            if commit_result.exit_code != 0:
                raise GitHubClientError(f"git commit failed: {commit_result.stderr[:200]}")

            # 5. Push
            sha = await self.push_branch(
                owner=owner,
                repo=repo,
                branch=branch,
                local_repo=repo_dir,
            )
            return sha

        finally:
            # Always clean up the fresh clone and patch.
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)
