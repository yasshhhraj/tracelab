"""
Tests for app/integrations/pr_body.py — CP-12

Pure unit tests, no mocking needed.
"""

import pytest

from app.integrations.pr_body import build_pr_body, build_pr_title


# ── build_pr_title ─────────────────────────────────────────────────────────────


def test_build_pr_title_basic():
    """Title contains the issue ID and symptom."""
    title = build_pr_title("PVS-421", "Duplicate rows on concurrent requests")
    assert "PVS-421" in title
    assert "Duplicate rows" in title


def test_build_pr_title_truncation():
    """A very long symptom is truncated to ≤255 characters."""
    long_symptom = "x" * 300
    title = build_pr_title("PVS-001", long_symptom)
    assert len(title) <= 255


def test_build_pr_title_format():
    """Title follows 'Fix {issue_id}: {symptom}' pattern."""
    title = build_pr_title("BUG-42", "Something broke")
    assert title.startswith("Fix BUG-42:")
    assert "Something broke" in title


def test_build_pr_title_exact_255():
    """A symptom that would make the title exactly 255 chars is not truncated."""
    # "Fix X: " is 7 chars; 255 - 7 = 248 chars for symptom
    symptom = "s" * 248
    title = build_pr_title("X", symptom)
    assert len(title) == 255
    assert title == title[:255]


# ── build_pr_body ──────────────────────────────────────────────────────────────


def _make_body(**overrides) -> str:
    defaults = dict(
        issue_id="PVS-421",
        symptom="Duplicate rows on concurrent requests",
        verified_cause="Race condition between idempotency check and insert",
        evidence=["Regression test fails before patch", "Regression test passes after patch"],
        rejected_hypotheses=[
            {"summary": "Stale cache", "rejection_reason": "Could not reproduce via cache"},
            {"summary": "Hash collision", "rejection_reason": "Hashes matched correctly"},
        ],
        changed_files=["review_repository.py", "test_review_idempotency.py"],
        risk="low",
    )
    defaults.update(overrides)
    return build_pr_body(**defaults)


def test_build_pr_body_contains_verified_cause():
    body = _make_body()
    assert "Race condition between idempotency check and insert" in body


def test_build_pr_body_evidence_list():
    body = _make_body()
    assert "Regression test fails before patch" in body
    assert "Regression test passes after patch" in body


def test_build_pr_body_rejected_hypotheses():
    body = _make_body()
    assert "Stale cache" in body
    assert "Could not reproduce via cache" in body
    assert "Hash collision" in body
    assert "Hashes matched correctly" in body


def test_build_pr_body_no_rejected_hypotheses():
    """When there are no rejected hypotheses, 'None' or equivalent appears."""
    body = _make_body(rejected_hypotheses=[])
    assert "None" in body or "none" in body.lower()


def test_build_pr_body_changed_files():
    body = _make_body()
    assert "review_repository.py" in body
    assert "test_review_idempotency.py" in body


def test_build_pr_body_human_review_notice():
    body = _make_body()
    assert "Human review required" in body


def test_build_pr_body_issue_id_present():
    body = _make_body(issue_id="MYPROJECT-99")
    assert "MYPROJECT-99" in body


def test_build_pr_body_risk_present():
    body = _make_body(risk="medium")
    assert "medium" in body


def test_build_pr_body_all_fields_present():
    """Ensure no unfilled template placeholders remain."""
    body = _make_body()
    # Check that none of the curly-brace placeholder patterns remain
    assert "{" not in body
    assert "}" not in body


def test_build_pr_body_returns_string():
    assert isinstance(_make_body(), str)


def test_build_pr_body_empty_evidence():
    body = _make_body(evidence=[])
    # Should contain the evidence section header, not crash
    assert "Verification" in body


def test_build_pr_body_empty_changed_files():
    body = _make_body(changed_files=[])
    assert "Changed files" in body
