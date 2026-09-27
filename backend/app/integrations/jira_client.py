"""
Jira REST API v3 client — CP-11

Credentials are read from settings at construction time and NEVER placed in
any prompt, log line, or AgentContext (AGENTS.md).

Usage::

    client = JiraClient()
    issue  = await client.get_issue("PVS-421")
    await  client.add_comment("PVS-421", "AI analysis complete.")
"""

import base64
import logging
from typing import Any

import httpx
from pydantic import BaseModel

from app.config import settings

_LOGGER = logging.getLogger(__name__)

# ── Jira issue fields query ───────────────────────────────────────────────────

_FIELDS = (
    "summary,description,issuetype,priority,status,"
    "components,labels,reporter,assignee,comment,attachment,"
    "created,updated"
)

# ── Exceptions ────────────────────────────────────────────────────────────────


class JiraClientError(Exception):
    """Base class for all Jira client errors."""


class JiraNotFoundError(JiraClientError):
    """Raised when the Jira issue does not exist (HTTP 404)."""


class JiraAuthError(JiraClientError):
    """Raised when credentials are rejected (HTTP 401 / 403)."""


class JiraTransitionNotFoundError(JiraClientError):
    """Raised when no transition matches the requested status name."""


# ── Data model ────────────────────────────────────────────────────────────────


class JiraIssue(BaseModel):
    """
    Normalised view of a Jira issue.

    No credentials appear in this model (AGENTS.md).
    ADF descriptions and comments are pre-converted to plain text.
    """

    issue_id: str  # e.g. "PVS-421"
    summary: str
    description: str  # ADF → plain text
    issue_type: str  # "Bug", "Task", …
    priority: str  # "High", "Medium", …
    status: str  # Jira workflow status label
    components: list[str]
    labels: list[str]
    reporter: str  # display name
    assignee: str | None
    comments: list[str]  # ADF bodies → plain text
    attachments: list[str]  # filenames only
    created_at: str  # ISO-8601
    updated_at: str  # ISO-8601


# ── ADF helpers ───────────────────────────────────────────────────────────────


def _adf_to_text(node: Any) -> str:  # noqa: ANN401
    """
    Recursively extract plain text from an Atlassian Document Format node.

    Handles: doc, paragraph, text, codeBlock, heading, bulletList,
             orderedList, listItem, blockquote, hardBreak.
    Unknown node types are traversed for nested content.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node

    node_type = node.get("type", "")
    parts: list[str] = []

    if node_type == "text":
        return node.get("text", "")

    if node_type == "hardBreak":
        return "\n"

    content = node.get("content") or []
    for child in content:
        parts.append(_adf_to_text(child))

    joined = "".join(parts)

    # Add newlines after block-level nodes to preserve paragraph structure.
    if node_type in {"paragraph", "heading", "blockquote", "listItem"}:
        return joined.rstrip() + "\n"
    if node_type in {"bulletList", "orderedList"}:
        return joined
    if node_type == "codeBlock":
        return joined.rstrip() + "\n"

    return joined


# ── ADF builder ───────────────────────────────────────────────────────────────


def _plain_text_to_adf(text: str) -> dict[str, Any]:
    """Wrap plain text in the minimal Atlassian Document Format body object."""
    return {
        "body": {
            "version": 1,
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": text}],
                }
            ],
        }
    }


# ── Client ────────────────────────────────────────────────────────────────────


class JiraClient:
    """
    Async Jira REST API v3 client.

    Credentials are injected from settings at construction time and NEVER
    placed in any prompt, log line, or AgentContext (AGENTS.md).
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_token: str | None = None,
        user_email: str | None = None,
    ) -> None:
        self._base_url = (base_url or settings.jira_base_url).rstrip("/")
        _token = api_token or settings.jira_api_token
        _email = user_email or settings.jira_user_email

        # Build the Basic auth header once; store privately — never logged.
        raw = f"{_email}:{_token}".encode()
        self._auth_header: str = f"Basic {base64.b64encode(raw).decode()}"

    # ── helpers ───────────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": self._auth_header,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self._base_url}/rest/api/3/{path.lstrip('/')}"

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code == 404:
            raise JiraNotFoundError(f"Issue not found (HTTP 404): {response.url}")
        if response.status_code in (401, 403):
            raise JiraAuthError(
                f"Authentication failed (HTTP {response.status_code}): {response.url}"
            )
        if response.status_code >= 400:
            raise JiraClientError(
                f"Jira API error (HTTP {response.status_code}): {response.text[:200]}"
            )

    # ── public API ────────────────────────────────────────────────────────────

    async def get_issue(self, issue_id: str) -> JiraIssue:
        """
        Fetch a Jira issue and map its fields to JiraIssue.

        Raises:
            JiraNotFoundError  — HTTP 404
            JiraAuthError      — HTTP 401 / 403
            JiraClientError    — any other HTTP error
        """
        _LOGGER.info("Fetching Jira issue %s", issue_id)
        url = self._url(f"issue/{issue_id}")
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=self._headers(), params={"fields": _FIELDS})
        self._raise_for_status(response)
        data = response.json()
        fields = data.get("fields", {})

        description_adf = fields.get("description") or {}
        description = _adf_to_text(description_adf).strip()

        comments_raw = (fields.get("comment") or {}).get("comments", [])
        comments = [
            _adf_to_text(c.get("body") or {}).strip() for c in comments_raw if c.get("body")
        ]

        attachments = [a.get("filename", "") for a in (fields.get("attachment") or [])]

        return JiraIssue(
            issue_id=data.get("key", issue_id),
            summary=fields.get("summary") or "",
            description=description,
            issue_type=(fields.get("issuetype") or {}).get("name", "Unknown"),
            priority=(fields.get("priority") or {}).get("name", "Unknown"),
            status=(fields.get("status") or {}).get("name", "Unknown"),
            components=[c.get("name", "") for c in (fields.get("components") or [])],
            labels=list(fields.get("labels") or []),
            reporter=((fields.get("reporter") or {}).get("displayName") or "Unknown"),
            assignee=((fields.get("assignee") or {}) or {}).get("displayName"),
            comments=comments,
            attachments=attachments,
            created_at=fields.get("created", ""),
            updated_at=fields.get("updated", ""),
        )

    async def add_comment(self, issue_id: str, body: str) -> None:
        """
        Add a plain-text comment to a Jira issue.

        The body is wrapped in Atlassian Document Format.
        SECURITY: never include credentials in `body`.
        """
        _LOGGER.info("Adding comment to Jira issue %s", issue_id)
        url = self._url(f"issue/{issue_id}/comment")
        payload = _plain_text_to_adf(body)
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, headers=self._headers(), json=payload)
        self._raise_for_status(response)

    async def transition_status(self, issue_id: str, status_name: str) -> None:
        """
        Transition the Jira issue to a named workflow status.

        Raises JiraTransitionNotFoundError if no matching transition exists.
        No-ops silently if the issue is already in the target status.
        """
        _LOGGER.info("Transitioning Jira issue %s to status '%s'", issue_id, status_name)
        transitions_url = self._url(f"issue/{issue_id}/transitions")

        async with httpx.AsyncClient(timeout=30.0) as client:
            tr_response = await client.get(transitions_url, headers=self._headers())
            self._raise_for_status(tr_response)
            transitions: list[dict[str, Any]] = tr_response.json().get("transitions", [])

            target = next(
                (t for t in transitions if t.get("name", "").lower() == status_name.lower()),
                None,
            )
            if target is None:
                raise JiraTransitionNotFoundError(
                    f"No transition named '{status_name}' found for issue {issue_id}"
                )

            post_response = await client.post(
                transitions_url,
                headers=self._headers(),
                json={"transition": {"id": target["id"]}},
            )
            self._raise_for_status(post_response)
