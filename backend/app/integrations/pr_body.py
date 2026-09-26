"""
PR body template renderer — CP-12

Pure functions: no I/O, no side effects, no app.* imports.
"""

_MAX_TITLE_LEN = 255  # GitHub PR title character limit


def build_pr_title(issue_id: str, symptom: str) -> str:
    """
    Render the pull request title.

    Format: "Fix {issue_id}: {symptom}"
    Truncated to 255 characters (GitHub API limit).
    """
    title = f"Fix {issue_id}: {symptom}"
    return title[:_MAX_TITLE_LEN]


def build_pr_body(
    issue_id: str,
    symptom: str,
    verified_cause: str,
    evidence: list[str],
    rejected_hypotheses: list[dict],
    changed_files: list[str],
    risk: str,
) -> str:
    """
    Render the pull request description from diagnosis data.

    Template (PRD §22):

        Fix {issue_id}: {symptom}

        AI-assisted diagnosis (TraceLab)

        **Verified cause:**
        {verified_cause}

        **Verification:**
        - ✓ {evidence item}
        ...

        **Alternative hypotheses investigated:**
        - {summary} — {rejection_reason}
        ...

        **Changed files:**
        - `{file}`
        ...

        **Risk:** {risk}

        > Human review required before merge.

    Args:
        issue_id:             Jira/external issue key, e.g. "PVS-421"
        symptom:              One-sentence bug description
        verified_cause:       Root cause identified by the winning hypothesis
        evidence:             List of evidence bullet strings from Diagnosis.evidence
        rejected_hypotheses:  List of dicts with keys "summary" and "rejection_reason"
        changed_files:        List of file paths from Diagnosis.changed_files
        risk:                 Risk level string, e.g. "low", "medium", "high"
    """
    lines: list[str] = []

    # Header
    lines.append(f"Fix {issue_id}: {symptom}")
    lines.append("")
    lines.append("AI-assisted diagnosis (TraceLab)")
    lines.append("")

    # Verified cause
    lines.append("**Verified cause:**")
    lines.append(verified_cause or "(no cause identified)")
    lines.append("")

    # Verification evidence
    lines.append("**Verification:**")
    if evidence:
        for item in evidence:
            lines.append(f"- ✓ {item}")
    else:
        lines.append("- (no evidence recorded)")
    lines.append("")

    # Rejected hypotheses
    lines.append("**Alternative hypotheses investigated:**")
    if rejected_hypotheses:
        for rh in rejected_hypotheses:
            summary = rh.get("summary", "Unknown hypothesis")
            reason = rh.get("rejection_reason", "rejected")
            lines.append(f"- {summary} — {reason}")
    else:
        lines.append("- None")
    lines.append("")

    # Changed files
    lines.append("**Changed files:**")
    if changed_files:
        for f in changed_files:
            lines.append(f"- `{f}`")
    else:
        lines.append("- (no files recorded)")
    lines.append("")

    # Risk
    lines.append(f"**Risk:** {risk}")
    lines.append("")

    # Human-in-the-loop notice (PRD §17)
    lines.append("> Human review required before merge.")

    return "\n".join(lines)
