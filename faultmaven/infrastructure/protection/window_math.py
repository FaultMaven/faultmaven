"""The sliding window's client-facing arithmetic, defined once.

Three enforcers derive the same two numbers — when the next unit of quota frees,
and how long a refused caller must wait for it. They had three hand-expanded
copies of the formula, so a correction to one was a silent divergence from the
other two. The formula lives here instead, and every enforcer calls it.

Deliberately free of I/O and of any clock read: ``now`` is passed in. The check
path derives ``reset_time`` and ``Retry-After`` from a single ``frees_at``, so
the two cannot name different instants, and a caller that freezes its clock for
a test freezes both together.
"""

import math
from typing import Optional

__all__ = ["quota_frees_at", "retry_after_seconds"]


def quota_frees_at(oldest_score: Optional[float], window: float, now: float) -> float:
    """The instant the next unit of quota frees.

    The oldest entry still inside the window ages out exactly one window after
    it arrived, so ``oldest + window`` is that instant — the only honest answer
    to "when may I retry". ``None`` means the window held nothing to age out, in
    which case the entry that just went in is the oldest and a full window from
    ``now`` is the same number.

    ``oldest_score`` is clamped to ``now`` before the window is added. Scores are
    wall-clock and shared across hosts, so a replica whose clock runs ahead can
    write an entry scored in *this* host's future; unclamped, the answer would
    exceed one full window, which no sliding window of that width can honestly
    produce. The clamp bounds it at exactly one window while leaving the measured
    value untouched whenever the clocks agree — which is every entry this host
    wrote itself.
    """
    if oldest_score is None:
        return now + window
    return min(oldest_score, now) + window


def retry_after_seconds(frees_at: float, now: float) -> int:
    """``frees_at`` expressed as the whole-second wait a client is told to take.

    Rounded up and floored at one: a sub-second answer reads as "retry
    immediately", which is exactly what a refused client must not do. Uncapped
    and unjittered — the value is already per-client, so herd de-synchronization
    is a property of the data rather than something randomness must add.
    """
    return max(1, math.ceil(frees_at - now))
