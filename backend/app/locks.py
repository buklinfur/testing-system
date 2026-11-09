"""Every PostgreSQL advisory lock of the backend, in one place (D-075).

Transaction-level locks only (pg_advisory_xact_lock): released at commit or rollback, never left behind by a crashed
process. PostgreSQL has two key spaces that never meet: one bigint key, and a pair of int4 keys. Here every lock has
its own constant, and the constants are different numbers whatever space they are used in (tests/test_locks_units.py
checks that, and that no other module of the backend calls pg_advisory_* with a key of its own):

* CLAIM - one bigint key: claims of the queue (submissions and problem checks) one at a time (docs/QUEUE.md);
* COMMENT_RATE - the pair (COMMENT_RATE, user id): the comments of one student one at a time, so the rate limit
  counts every earlier one (docs/CLASSROOM.md §9);
* PROBLEM_IMPORT - the pair (PROBLEM_IMPORT, hashtext(slug)): imports of one problem one at a time
  (docs/PROBLEM_IMPORT.md). Two slugs with the same hash only wait for each other; nothing is wrong then.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

CLAIM = 7_340_001
COMMENT_RATE = 7_340_002
PROBLEM_IMPORT = 7_340_003

ALL = {"CLAIM": CLAIM, "COMMENT_RATE": COMMENT_RATE, "PROBLEM_IMPORT": PROBLEM_IMPORT}


def take(db: Session, key: int) -> None:
    """The lock of one bigint key, until the end of the transaction."""
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})


def take_pair(db: Session, key: int, sub: int) -> None:
    """The lock of the pair (key, sub), until the end of the transaction."""
    db.execute(text("SELECT pg_advisory_xact_lock(CAST(:k AS integer), CAST(:s AS integer))"), {"k": key, "s": sub})


def take_named(db: Session, key: int, name: str) -> None:
    """The lock of the pair (key, hashtext(name)), until the end of the transaction."""
    db.execute(text("SELECT pg_advisory_xact_lock(CAST(:k AS integer), hashtext(:n))"), {"k": key, "n": name})
