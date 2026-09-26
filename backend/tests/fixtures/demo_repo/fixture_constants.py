"""
Fixture constants for verification engine tests.

CORRECT_PATCH — unified diff that fixes the race condition by adding a
                threading lock to ReviewRepository.
WRONG_PATCH   — valid diff that touches review_service.py but doesn't fix the race.
INVALID_PATCH — malformed diff that git apply will reject.
"""

# Single-hunk patch that rewrites the entire review_repository.py with the fix.
CORRECT_PATCH = """\
--- a/review_repository.py
+++ b/review_repository.py
@@ -1,29 +1,32 @@
 \"\"\"
 ReviewRepository — in-memory store for reviews.
 
 BUG: insert() has no locking — concurrent callers can both pass the
 find_by_key() check and both insert, producing duplicates.
 \"\"\"
 
+import threading
+
 
 class ReviewRepository:
     def __init__(self) -> None:
         self._store: dict[str, dict] = {}
         self._next_id = 1
-        # The fix: use a lock around the check-and-insert sequence.
-        # self._lock = threading.Lock()  # <-- MISSING in buggy version
+        self._lock = threading.Lock()
 
     def find_by_key(self, idempotency_key: str) -> dict | None:
         return self._store.get(idempotency_key)
 
     def insert(self, idempotency_key: str, data: dict) -> int:
-        # BUG: no atomicity — two concurrent callers can both reach here
-        row_id = self._next_id
-        self._next_id += 1
-        self._store[idempotency_key] = {"id": row_id, **data}
-        return row_id
+        with self._lock:
+            existing = self._store.get(idempotency_key)
+            if existing:
+                return existing["id"]
+            row_id = self._next_id
+            self._next_id += 1
+            self._store[idempotency_key] = {"id": row_id, **data}
+            return row_id
 
     def count(self) -> int:
         return len(self._store)
"""

# Touches review_service.py with a cosmetic no-op — doesn't fix the race.
WRONG_PATCH = """\
--- a/review_service.py
+++ b/review_service.py
@@ -24,6 +24,7 @@ class ReviewService:
         existing = self.repo.find_by_key(idempotency_key)
         if existing:
             return existing["id"]
+        # NOTE: still no lock here — this patch does not fix the race
         return self.repo.insert(idempotency_key, data)
"""

# Completely malformed — git apply will refuse it.
INVALID_PATCH = "this is not a valid unified diff\n"

# Path of the regression test (relative to demo repo root)
REGRESSION_TEST_PATH = "tests/test_concurrent_review.py"

# Reproduction plan commands (relative paths executed from repo root)
REPRODUCTION_PLAN = [
    "tests/test_concurrent_review.py",
]
