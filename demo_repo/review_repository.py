"""A deliberately racy SQLite repository used by the CP-13 demo."""

import sqlite3
from pathlib import Path


class ReviewRepository:
    def __init__(self, database: Path) -> None:
        self.database = database
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS review_runs ("
                "id INTEGER PRIMARY KEY, input_hash TEXT NOT NULL, payload TEXT NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database, timeout=5)

    def find_by_hash(self, input_hash: str) -> int | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id FROM review_runs WHERE input_hash = ? ORDER BY id LIMIT 1",
                (input_hash,),
            ).fetchone()
        return row[0] if row else None

    def insert(self, input_hash: str, payload: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO review_runs (input_hash, payload) VALUES (?, ?)",
                (input_hash, payload),
            )
            return cursor.lastrowid

    def count_by_hash(self, input_hash: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM review_runs WHERE input_hash = ?",
                (input_hash,),
            ).fetchone()
        return row[0]
