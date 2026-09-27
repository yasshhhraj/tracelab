"""Independent, intentionally buggy Python fixtures for CP-13's pipeline benchmark."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Case:
    buggy: str
    fixed: str
    existing: str
    regression: str
    repro: str | None = None


CASES = {
    "logic-pages": Case(
        "def pages(items, size):\n    return items // size\n",
        "def pages(items, size):\n    return (items + size - 1) // size\n",
        "from subject import pages\ndef test_full_pages():\n    assert pages(10, 5) == 2\n",
        "from subject import pages\ndef test_partial_page():\n    assert pages(11, 5) == 3\n",
    ),
    "logic-guest": Case(
        "def can_view(role, public):\n    return bool(role) or public\n",
        "def can_view(role, public):\n    return role in ('admin', 'member') or public\n",
        "from subject import can_view\ndef test_admin():\n    assert can_view('admin', False)\n",
        "from subject import can_view\ndef test_private_guest():\n    assert not can_view('guest', False)\n",
    ),
    "logic-range": Case(
        "def total_through(n):\n    return sum(range(n))\n",
        "def total_through(n):\n    return sum(range(n + 1))\n",
        "from subject import total_through\ndef test_zero():\n    assert total_through(0) == 0\n",
        "from subject import total_through\ndef test_three():\n    assert total_through(3) == 6\n",
    ),
    "persistence-once": Case(
        "import sqlite3\ndef save_once(db, key):\n    db.execute('CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY, key TEXT)')\n    db.execute('INSERT INTO jobs (key) VALUES (?)', (key,))\n    db.commit()\n    return db.execute('SELECT count(*) FROM jobs WHERE key=?', (key,)).fetchone()[0]\n",
        "import sqlite3\ndef save_once(db, key):\n    db.execute('CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY, key TEXT UNIQUE)')\n    db.execute('INSERT OR IGNORE INTO jobs (key) VALUES (?)', (key,))\n    db.commit()\n    return db.execute('SELECT count(*) FROM jobs WHERE key=?', (key,)).fetchone()[0]\n",
        "import sqlite3\nfrom subject import save_once\ndef test_first_save():\n    assert save_once(sqlite3.connect(':memory:'), 'a') == 1\n",
        "import sqlite3\nfrom subject import save_once\ndef test_retry_is_idempotent():\n    db = sqlite3.connect(':memory:')\n    save_once(db, 'a')\n    assert save_once(db, 'a') == 1\n",
    ),
    "persistence-overdraft": Case(
        "def withdraw(db, amount):\n    balance = db.execute('SELECT balance FROM account').fetchone()[0]\n    db.execute('UPDATE account SET balance=?', (balance - amount,))\n    db.commit()\n    return balance - amount\n",
        "def withdraw(db, amount):\n    balance = db.execute('SELECT balance FROM account').fetchone()[0]\n    if amount > balance:\n        raise ValueError('insufficient funds')\n    db.execute('UPDATE account SET balance=?', (balance - amount,))\n    db.commit()\n    return balance - amount\n",
        "import sqlite3\nfrom subject import withdraw\ndef test_valid_withdrawal():\n    db=sqlite3.connect(':memory:')\n    db.execute('CREATE TABLE account (balance INTEGER)')\n    db.execute('INSERT INTO account VALUES (10)')\n    assert withdraw(db, 3) == 7\n",
        "import sqlite3\nimport pytest\nfrom subject import withdraw\ndef test_overdraft_preserves_balance():\n    db=sqlite3.connect(':memory:')\n    db.execute('CREATE TABLE account (balance INTEGER)')\n    db.execute('INSERT INTO account VALUES (10)')\n    with pytest.raises(ValueError):\n        withdraw(db, 11)\n    assert db.execute('SELECT balance FROM account').fetchone()[0] == 10\n",
    ),
    "concurrency-counter": Case(
        "def increment(counter, barrier=None):\n    current = counter['value']\n    if barrier is not None:\n        barrier.wait()\n    counter['value'] = current + 1\n",
        "def increment(counter, barrier=None):\n    if barrier is not None:\n        barrier.wait()\n    with counter['lock']:\n        counter['value'] += 1\n",
        "import threading\nfrom subject import increment\ndef test_one_increment():\n    counter={'value': 0, 'lock': threading.Lock()}\n    increment(counter)\n    assert counter['value'] == 1\n",
        "import threading\nfrom subject import increment\ndef test_two_concurrent_increments():\n    counter={'value': 0, 'lock': threading.Lock()}\n    barrier=threading.Barrier(2)\n    threads=[threading.Thread(target=increment, args=(counter, barrier)) for _ in range(2)]\n    for thread in threads: thread.start()\n    for thread in threads: thread.join(timeout=5)\n    assert counter['value'] == 2\n",
    ),
    "regression-trim": Case(
        "def normalize_label(label):\n    return label.strip()\n",
        "def normalize_label(label):\n    return label.lstrip()\n",
        "from subject import normalize_label\ndef test_leading_space():\n    assert normalize_label('  abc') == 'abc'\n",
        "from subject import normalize_label\ndef test_trailing_space_is_meaningful():\n    assert normalize_label('abc  ') == 'abc  '\n",
    ),
    "regression-sort": Case(
        "def rank(scores):\n    return sorted(scores)\n",
        "def rank(scores):\n    return sorted(scores, reverse=True)\n",
        "from subject import rank\ndef test_one_score():\n    assert rank([8]) == [8]\n",
        "from subject import rank\ndef test_highest_first():\n    assert rank([2, 9, 5]) == [9, 5, 2]\n",
    ),
    "flaky-delivery": Case(
        "from pathlib import Path\ndef deliver(value, state_file='.delivery-count'):\n    path=Path(state_file)\n    count=int(path.read_text()) + 1 if path.exists() else 1\n    path.write_text(str(count))\n    return None if count % 5 == 0 else value\n",
        "from pathlib import Path\ndef deliver(value, state_file='.delivery-count'):\n    path=Path(state_file)\n    count=int(path.read_text()) + 1 if path.exists() else 1\n    path.write_text(str(count))\n    return value\n",
        "from subject import deliver\ndef test_first_delivery(tmp_path):\n    assert deliver('ok', tmp_path / 'count') == 'ok'\n",
        "from subject import deliver\ndef test_repeated_delivery():\n    assert deliver('ok') == 'ok'\n",
        "from subject import deliver\ndef test_fifth_delivery():\n    assert [deliver('ok') for _ in range(5)] == ['ok'] * 5\n",
    ),
}
