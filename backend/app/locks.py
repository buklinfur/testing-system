"""Every PostgreSQL advisory lock of the backend, in one place (D-075).

Transaction-level locks only (pg_advisory_xact_lock): released at commit or rollback, never left behind by a crashed
process. PostgreSQL has two key spaces that never meet: one bigint key, and a pair of int4 keys. Here every lock has
its own constant, and the constants are different numbers whatever space they are used in (tests/test_locks_units.py
checks that, and that no other module of the backend calls pg_advisory_* with a key of its own):

* CLAIM - one bigint key: claims of the queue (submissions and problem checks) one at a time (docs/QUEUE.md);
* COMMENT_RATE - the pair (COMMENT_RATE, user id): the comments of one student one at a time, so the rate limit
  counts every earlier one (docs/CLASSROOM.md §9);
* PROBLEM_IMPORT - the pair (PROBLEM_IMPORT, hashtext(slug)): imports of one problem one at a time
  (docs/PROBLEM_IMPORT.md). Two slugs with the same hash only wait for each other; nothing is wrong then;
* STUDENT_FILES - the pair (STUDENT_FILES, user id): the hand-ins with files of one student one at a time, so the
  quota (the distinct blobs of all their hand-ins) counts every earlier one (docs/CLASSROOM.md §10, 4v.3);
* LIBRARY - one bigint key: changes of the sections of the library («Справочник») and of the materials in them one at
  a time, so positions, the two levels and an order that must list exactly the current set are decided on what the
  other change wrote (docs/CLASSROOM.md §11, 4v.4). Changes of one material lock its row instead (FOR UPDATE);
* SUBMIT_RATE - the pair (SUBMIT_RATE, user id): the submissions of one student one at a time, so the rate limit
  (JUDGE_SUBMIT_RATE_PER_MIN) counts every earlier one; taken before the participation row (FOR UPDATE), always in
  this order (docs/STUDENT_API.md, 4b.1);
* ENTER_RATE - the pair (ENTER_RATE, user id): the attempts of one student to enter a contest one at a time, so
  JUDGE_ENTER_RATE counts every earlier attempt (contest_entry_attempts, D-093); taken before the participation row.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

CLAIM = 7_340_001
COMMENT_RATE = 7_340_002
PROBLEM_IMPORT = 7_340_003
STUDENT_FILES = 7_340_004
LIBRARY = 7_340_005
SUBMIT_RATE = 7_340_006
ENTER_RATE = 7_340_007

ALL = {"CLAIM": CLAIM, "COMMENT_RATE": COMMENT_RATE, "PROBLEM_IMPORT": PROBLEM_IMPORT,
       "STUDENT_FILES": STUDENT_FILES, "LIBRARY": LIBRARY, "SUBMIT_RATE": SUBMIT_RATE, "ENTER_RATE": ENTER_RATE}


def take(db: Session, key: int) -> None:
    """The lock of one bigint key, until the end of the transaction."""
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})


def take_pair(db: Session, key: int, sub: int) -> None:
    """The lock of the pair (key, sub), until the end of the transaction."""
    db.execute(text("SELECT pg_advisory_xact_lock(CAST(:k AS integer), CAST(:s AS integer))"), {"k": key, "s": sub})


def take_named(db: Session, key: int, name: str) -> None:
    """The lock of the pair (key, hashtext(name)), until the end of the transaction."""
    db.execute(text("SELECT pg_advisory_xact_lock(CAST(:k AS integer), hashtext(:n))"), {"k": key, "n": name})
