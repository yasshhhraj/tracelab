"""
TraceLab CLI

Entry point::

    python -m app.cli investigate --bug "..." --repo ./path/to/repo

Prints the resulting Hypothesis as formatted JSON to stdout.

This is a manual E2E check tool — it is NOT run in CI (requires a real LLM
API key and a real repository).
"""

import asyncio
import json
from pathlib import Path

import click

from app.agents.base import AgentContext
from app.agents.code_path_agent import run_code_path_agent
from app.schemas.bug_context import BugContext


@click.group()
def cli() -> None:
    """TraceLab — multi-agent differential debugging."""


@cli.command()
@click.option("--issue-id", default="CLI-1", show_default=True, help="Issue identifier.")
@click.option("--bug", required=True, help="One-sentence symptom description.")
@click.option("--expected", default="", help="Expected behaviour.")
@click.option("--actual", default="", help="Actual behaviour.")
@click.option("--area", default="unknown", help="Affected system area.")
@click.option(
    "--error-type",
    default="deterministic",
    type=click.Choice(["deterministic", "intermittent", "regression"]),
    help="Error classification.",
)
@click.option("--repo", required=True, type=click.Path(exists=True), help="Repository path.")
@click.option("--branch", default="main", show_default=True, help="Base branch.")
@click.option(
    "--investigation-id",
    default="cli-investigation-1",
    help="Investigation ID (for event records).",
)
def investigate(
    issue_id: str,
    bug: str,
    expected: str,
    actual: str,
    area: str,
    error_type: str,
    repo: str,
    branch: str,
    investigation_id: str,
) -> None:
    """Run a single CodePathAgent investigation and print the hypothesis JSON."""
    repo_path = Path(repo).resolve()

    bug_context = BugContext(
        issue_id=issue_id,
        symptom=bug,
        expected=expected or "No errors or unexpected behaviour.",
        actual=actual or bug,
        affected_area=area,
        error_type=error_type,
        known_evidence=[],
        repository=str(repo_path),
        base_branch=branch,
    )

    context = AgentContext(
        bug_context=bug_context,
        repo_path=repo_path,
        # CLI uses the main repo directly — no isolated worktree needed for manual runs.
        worktree_path=repo_path,
        investigation_id=investigation_id,
    )

    result = asyncio.run(run_code_path_agent(context))
    click.echo(json.dumps(result.hypothesis.model_dump(), indent=2, default=str))


if __name__ == "__main__":
    cli()
