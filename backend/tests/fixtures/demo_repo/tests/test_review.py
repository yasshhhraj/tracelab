"""
Existing sequential tests for ReviewService.

These tests all pass on both the buggy and the fixed code.
They do NOT test concurrent behaviour — that is the gap.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from review_service import ReviewService


def test_submit_review_returns_id():
    svc = ReviewService()
    row_id = svc.submit_review("key-001", {"score": 5})
    assert isinstance(row_id, int)
    assert row_id > 0


def test_submit_review_idempotent_sequential():
    """Same key submitted twice sequentially returns the same ID."""
    svc = ReviewService()
    id1 = svc.submit_review("key-002", {"score": 4})
    id2 = svc.submit_review("key-002", {"score": 4})
    assert id1 == id2


def test_submit_review_different_keys_different_ids():
    svc = ReviewService()
    id1 = svc.submit_review("key-003", {"score": 3})
    id2 = svc.submit_review("key-004", {"score": 3})
    assert id1 != id2


def test_repository_count_after_two_unique_reviews():
    svc = ReviewService()
    svc.submit_review("key-005", {"score": 5})
    svc.submit_review("key-006", {"score": 4})
    assert svc.repo.count() == 2
