"""
ReviewRepository — in-memory store for reviews.

BUG: insert() has no locking — concurrent callers can both pass the
find_by_key() check and both insert, producing duplicates.
"""



class ReviewRepository:
    def __init__(self) -> None:
        self._store: dict[str, dict] = {}
        self._next_id = 1
        # The fix: use a lock around the check-and-insert sequence.
        # self._lock = threading.Lock()  # <-- MISSING in buggy version

    def find_by_key(self, idempotency_key: str) -> dict | None:
        return self._store.get(idempotency_key)

    def insert(self, idempotency_key: str, data: dict) -> int:
        # BUG: no atomicity — two concurrent callers can both reach here
        row_id = self._next_id
        self._next_id += 1
        self._store[idempotency_key] = {"id": row_id, **data}
        return row_id

    def count(self) -> int:
        return len(self._store)
