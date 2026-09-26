"""
Regression test for the idempotency race condition in ReviewService.

This test uses controlled interleaving to deterministically reproduce the
check-then-act race condition WITHOUT relying on OS thread scheduling.

FAILS on the original buggy code (no lock).
PASSES after the fix (atomic check-and-insert with a threading.Lock).
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from unittest.mock import patch

from review_repository import ReviewRepository
from review_service import ReviewService


def test_submit_review_concurrent_idempotency():
    """
    Simulates the race condition by forcing interleaved execution:
      Thread-1 calls find_by_key() → gets None
      Thread-2 calls find_by_key() → gets None
      Thread-1 calls insert()      → inserts row #1
      Thread-2 calls insert()      → inserts row #2  (duplicate!)

    With the fix (threading.Lock wrapping check-and-insert), the second
    insert returns the existing row ID instead of creating a duplicate.
    """
    repo = ReviewRepository()
    svc = ReviewService(repo=repo)

    # Intercept find_by_key to simulate "both threads see empty before either inserts"
    original_find = repo.find_by_key
    call_count = {"n": 0}

    def patched_find(key):
        call_count["n"] += 1
        # Both the first AND second calls return None (simulating the race window)
        if call_count["n"] <= 2:
            return None
        return original_find(key)

    with patch.object(repo, "find_by_key", side_effect=patched_find):
        id1 = svc.submit_review("race-key", {"score": 5})
        id2 = svc.submit_review("race-key", {"score": 5})

    # Both calls must return the same ID — no duplicate allowed
    assert id1 == id2, (
        f"Got different IDs ({id1}, {id2}) — duplicate insert occurred. "
        "The repository needs an atomic check-and-insert."
    )
    # Repository must contain exactly one row
    assert repo.count() == 1, (
        f"Repository has {repo.count()} rows — duplicate insert occurred."
    )
