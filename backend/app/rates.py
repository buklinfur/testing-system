"""Rate limits of students counted over a sliding window (4b.1, D-091): a pure rule over the moments of the recent
actions, which the caller reads from the database under a lock of the student (locks.SUBMIT_RATE / ENTER_RATE).

Submissions: JUDGE_SUBMIT_RATE_PER_MIN per 60 s, all contests together (the contest's own submit_cooldown_sec is
per problem and is checked by contests/access.can_submit). Entering a contest: JUDGE_ENTER_RATE attempts per 10
minutes - each attempt counts, right or wrong, so a code cannot be guessed by trying.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Iterable, Optional

from .deadline_rules import Decision

SUBMIT_WINDOW = timedelta(seconds=60)
ENTER_WINDOW = timedelta(minutes=10)


def window_ok(moments: Iterable[datetime], now: datetime, limit: Optional[int], window: timedelta) -> Decision:
    """At most `limit` actions within (now - window, now]; `moments` may hold older ones. A refusal says when the
    oldest action in the window leaves it (retry_after_s, at least 1). limit None or 0: no limit."""
    if not limit:
        return Decision(True)
    inside = sorted(m for m in moments if now - window < m <= now)
    if len(inside) < limit:
        return Decision(True)
    free_at = inside[-limit] + window  # once this many fewer are inside, one more fits
    return Decision(False, "rate_limited", retry_after_s=max(1, math.ceil((free_at - now).total_seconds())))
