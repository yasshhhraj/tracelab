"""Explicit reproduction kept outside the default sequential test suite."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from review_repository import ReviewRepository
from review_service import ReviewService


def test_two_requests_create_one_run(tmp_path):
    repository = ReviewRepository(tmp_path / "review.sqlite")
    barrier = Barrier(2)
    service = ReviewService(repository, before_insert=lambda: barrier.wait(timeout=5))
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: service.create_run("same-hash", "payload"), range(2)))
    assert ids[0] == ids[1]
    assert repository.count_by_hash("same-hash") == 1
