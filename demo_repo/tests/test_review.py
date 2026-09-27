from review_repository import ReviewRepository
from review_service import ReviewService


def test_sequential_duplicate_returns_same_run(tmp_path):
    service = ReviewService(ReviewRepository(tmp_path / "review.sqlite"))
    first = service.create_run("hash-1", "payload")
    second = service.create_run("hash-1", "payload")
    assert first == second
    assert service.repository.count_by_hash("hash-1") == 1


def test_different_hashes_create_different_runs(tmp_path):
    service = ReviewService(ReviewRepository(tmp_path / "review.sqlite"))
    assert service.create_run("hash-1", "first") != service.create_run("hash-2", "second")
