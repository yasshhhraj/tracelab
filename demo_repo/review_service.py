"""Create a review run, reusing an existing run for sequential repeats."""

from collections.abc import Callable

from review_repository import ReviewRepository


class ReviewService:
    def __init__(
        self, repository: ReviewRepository, before_insert: Callable[[], None] | None = None
    ) -> None:
        self.repository = repository
        self.before_insert = before_insert

    def create_run(self, input_hash: str, payload: str) -> int:
        existing = self.repository.find_by_hash(input_hash)
        if existing is not None:
            return existing
        if self.before_insert:
            self.before_insert()
        return self.repository.insert(input_hash, payload)
