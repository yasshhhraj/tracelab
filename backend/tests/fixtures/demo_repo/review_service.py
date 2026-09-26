"""
ReviewService — business logic layer for submitting reviews.

The submit_review method is supposed to be idempotent: given the same
idempotency_key, it should always return the same row ID without inserting
a duplicate.  Under concurrent load the check-then-act pattern is racy.
"""

from review_repository import ReviewRepository


class ReviewService:
    def __init__(self, repo: ReviewRepository | None = None) -> None:
        self.repo = repo or ReviewRepository()

    def submit_review(self, idempotency_key: str, data: dict) -> int:
        """
        Submit a review, returning the row ID.

        Idempotent for sequential callers: two calls with the same key
        return the same ID.  BUGGY under concurrent callers: both may pass
        the find_by_key check and both insert.
        """
        existing = self.repo.find_by_key(idempotency_key)
        if existing:
            return existing["id"]
        # BUG: no lock around this check-then-insert sequence
        return self.repo.insert(idempotency_key, data)
