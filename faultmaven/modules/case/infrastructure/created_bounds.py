"""The creation-date window, in ONE place for every repository.

Two things about these bounds are easy to get wrong in a way no test written in
UTC can see, so neither is left to the call site.

**The bound must be normalized to UTC before it is bound.** On SQLite the
``cases.created_at`` column is written through ``sqlite3``'s default datetime
adapter, so the stored value is the TEXT ``'2026-09-10 23:00:00+00:00'`` and the
comparison is a lexicographic string compare that knows nothing about the offset
suffix it is comparing. Two spellings of the same instant therefore answer
differently::

    stored               '2026-09-10 23:00:00+00:00'
    created_after (UTC)  '2026-09-10 18:30:00+00:00'  -> matches
    created_after (IST)  '2026-09-11 00:00:00+05:30'  -> DOES NOT MATCH

Both bounds name the same moment — ``18:30Z == 00:00+05:30`` — and only the
first is found. That is not hypothetical: the route tells clients to resolve a
calendar day to "the instants ITS user means", which is precisely an invitation
to send a non-UTC offset. Converting here makes the two spellings agree, and
keeps the predicate sargable against the ``created_at`` index (wrapping the
column in SQLite's ``datetime()`` would also fix it, and would cost the index).

**The upper bound is EXCLUSIVE — the window is ``[after, before)``.** An
inclusive upper bound cannot be expressed by a client whose clock has
millisecond resolution, which is every browser: ``Date.prototype.toISOString``
stops at milliseconds, while ``created_at`` keeps microseconds. A "14 Sept to
14 Sept" filter built from ``23:59:59.999`` silently drops a case created at
``23:59:59.9997``. Half-open removes the class rather than narrowing it — the
client sends the following midnight, every microsecond of the chosen day is
inside the window, and adjacent ranges neither overlap nor gap.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def to_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Return ``value`` as a UTC-aware datetime, or ``None``.

    A naive value is READ as UTC rather than rejected: the alternative is
    shifting it by whatever zone the server happens to run in, which is a
    silent, environment-dependent answer. An aware value is CONVERTED, which is
    the half that matters for correctness (see the module docstring).
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def created_bounds_where(
    params: Dict[str, Any],
    created_after: Optional[datetime],
    created_before: Optional[datetime],
    *,
    col_prefix: str = "",
) -> List[str]:
    """Return the SQL predicates bounding ``created_at``, binding into ``params``.

    Mirrors ``case_scope_where``: mutates ``params`` in place with the values it
    references and returns clauses for the caller to AND into its WHERE.

    ``created_after`` is inclusive, ``created_before`` exclusive — the window is
    ``[created_after, created_before)``. Both are normalized to UTC first.

    The predicates belong in the WHERE clause and not in a post-filter, for the
    same reason ``include_empty`` does: applied after the repository paginated,
    a bound would thin an already-sliced page and disagree with the total.
    """
    clauses: List[str] = []

    after = to_utc(created_after)
    if after is not None:
        clauses.append(f"{col_prefix}created_at >= :created_after")
        params["created_after"] = after

    before = to_utc(created_before)
    if before is not None:
        clauses.append(f"{col_prefix}created_at < :created_before")
        params["created_before"] = before

    return clauses
