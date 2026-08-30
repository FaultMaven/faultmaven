"""#1258 — the collision CLASS, not the one measured pair.

``('redis', 'Redis OOM')`` / ``('redis', 'redis oom')`` is what the issue
measured, but case folding is only one of the things ``_slug`` collapses. The
guard in ``_partition_by_minted_runbook_id`` compares minted IDS rather than
titles, so it covers every such shape by construction; this file is what turns
that claim into a measurement, by enumerating the shapes and driving each one
through the guard.

The mint itself is deliberately NOT changed — it is a persisted id, and
re-minting it would orphan rows that already exist (the boundary #1230 and #1243
both drew) — so the last test here pins that the fix left every id alone.
"""

from __future__ import annotations

import pytest

from faultmaven.modules.knowledge.domain.models.conversion import FailureModeAnalysis
from faultmaven.modules.knowledge.domain.services.conversion_service import (
    _partition_by_minted_runbook_id,
)
from faultmaven.utils.runbook_id import runbook_id_from_parts

pytestmark = pytest.mark.unit


#: ``(shape, (service_a, title_a), (service_b, title_b))`` — pairs the slug rule
#: normalises to one id. Measured against ``runbook_id_from_parts`` on
#: ``e1cf27371``; the ``test_every_enumerated_pair_really_does_collide`` guard
#: below fails if any of them stops colliding, which would make its partition
#: assertion vacuous.
COLLIDING_SHAPES = [
    ("case folding", ("redis", "Redis OOM"), ("redis", "redis oom")),
    ("punctuation", ("redis", "Redis: OOM!"), ("redis", "Redis - OOM")),
    ("underscore vs hyphen", ("redis", "Redis_OOM"), ("redis", "redis-oom")),
    ("whitespace runs", ("redis", "Redis   OOM"), ("redis", "Redis OOM")),
    ("leading/trailing trim", ("redis", "  Redis OOM  "), ("redis", "Redis OOM")),
    ("tab as separator", ("redis", "Redis\tOOM"), ("redis", "Redis OOM")),
    ("non-breaking space", ("redis", "Redis\xa0OOM"), ("redis", "Redis OOM")),
    ("zero-width joiner", ("redis", "Redis​OOM"), ("redis", "Redis OOM")),
    ("emoji dropped", ("redis", "Redis OOM \U0001f525"), ("redis", "Redis OOM")),
    ("non-latin dropped", ("redis", "Redis OOM (メモリ)"), ("redis", "Redis OOM")),
    ("full-width forms", ("redis", "ＲＥＤＩＳ OOM"), ("redis", "OOM")),
    ("accent dropped", ("redis", "Rédis OOM"), ("redis", "R dis OOM")),
    # The service and the title are joined with a hyphen before slugging, which
    # makes the delimiter part of the data — the same ambiguity the empty-slug
    # branch was hardened against with ``repr()``, still present on this branch.
    ("service/title boundary", ("redis-cache", "OOM"), ("redis", "cache OOM")),
    # The title contributes nothing, so both mint the bare service. The mint's
    # own docstring names this one as deliberately uncovered THERE.
    ("title filters to nothing", ("redis", "!!!"), ("redis", "???")),
    # Over-length: both titles exceed the 60-char bound, and they differ only
    # where the slug rule normalises — so they share a slug, and therefore share
    # both the kept prefix AND the md5 taken over that slug.
    (
        "truncation of one slug",
        (
            "checkout-api",
            "Connection pool exhaustion under sustained load, WIDESPREAD 503s",
        ),
        (
            "checkout-api",
            "connection-pool exhaustion; under sustained load: widespread 503s",
        ),
    ),
]


def _mode(
    fm_id: str, service: str, title: str, symptom_class: str
) -> FailureModeAnalysis:
    return FailureModeAnalysis(
        id=fm_id,
        title=title,
        domain="platform",
        service=service,
        symptom_class=[symptom_class],
        severity="high",
        symptoms_summary="x",
        resolution_summary="y",
    )


@pytest.mark.parametrize(
    "shape,a,b", COLLIDING_SHAPES, ids=[s[0] for s in COLLIDING_SHAPES]
)
def test_every_enumerated_pair_really_does_collide(shape, a, b):
    """The premise of the test below it.

    If a pair stopped colliding, its partition assertion would pass for the
    wrong reason — the guard would never have been asked anything.
    """
    assert runbook_id_from_parts(*a) == runbook_id_from_parts(*b), shape


@pytest.mark.parametrize(
    "shape,a,b", COLLIDING_SHAPES, ids=[s[0] for s in COLLIDING_SHAPES]
)
def test_the_second_mode_of_a_colliding_pair_is_refused(shape, a, b):
    survivors, errors = _partition_by_minted_runbook_id(
        [
            _mode("fm-a", a[0], a[1], "saturation"),
            _mode("fm-b", b[0], b[1], "latency"),
        ]
    )

    assert [fm.id for fm in survivors] == ["fm-a"], shape
    assert [e.failure_mode_id for e in errors] == ["fm-b"], shape
    # The refusal names BOTH modes, or it is not actionable. Titles are
    # compared as ``repr`` because that is how the message renders them — see
    # ``test_an_invisible_difference_is_shown_escaped``.
    message = errors[0].error
    assert "fm-a" in message, message
    assert repr(a[1]) in message, message
    assert repr(b[1]) in message, message
    assert runbook_id_from_parts(*a) in message, message
    assert errors[0].retryable is False


def test_an_invisible_difference_is_shown_escaped():
    """The message renders titles with ``repr``, and that is load-bearing.

    The hardest shape to act on is two titles that differ only in a character
    with no glyph. Rendered raw, the refusal reads "'Redis OOM' and 'Redis OOM'
    mint the same id", which looks like a bug in FaultMaven rather than a
    duplicate in the document. ``repr`` shows the ``\\xa0``.
    """
    _, errors = _partition_by_minted_runbook_id(
        [
            _mode("fm-a", "redis", "Redis\xa0OOM", "saturation"),
            _mode("fm-b", "redis", "Redis OOM", "latency"),
        ]
    )

    assert "\\xa0" in errors[0].error, errors[0].error


def test_distinct_failure_modes_are_untouched():
    """The negative control. Without it every assertion above is satisfied by a
    guard that refuses the second of any pair."""
    survivors, errors = _partition_by_minted_runbook_id(
        [
            _mode("fm-a", "redis", "Redis OOM", "saturation"),
            _mode("fm-b", "redis", "Redis Slow", "latency"),
            _mode("fm-c", "postgres", "Replication Lag", "latency"),
        ]
    )

    assert [fm.id for fm in survivors] == ["fm-a", "fm-b", "fm-c"]
    assert errors == []


def test_the_survivors_do_not_depend_on_the_orders_arrival():
    """Which mode wins is positional; WHAT is minted is not.

    The disk scan reconciles a file to its row by ``runbook_id``, so the ids a
    job commits must not depend on how the analysis happened to order its
    failure modes. They do not: the guard removes repeats of an id rather than
    inventing a new one, so the set of surviving ids — and the count — is the
    same under any permutation.
    """
    modes = [
        _mode("fm-a", "redis", "Redis OOM", "saturation"),
        _mode("fm-b", "redis", "redis oom", "latency"),
        _mode("fm-c", "redis", "Redis Slow", "errors"),
    ]
    forward, forward_errors = _partition_by_minted_runbook_id(modes)
    reverse, reverse_errors = _partition_by_minted_runbook_id(list(reversed(modes)))

    assert (
        {runbook_id_from_parts(fm.service, fm.title) for fm in forward}
        == {runbook_id_from_parts(fm.service, fm.title) for fm in reverse}
        == {"redis-redis-oom", "redis-redis-slow"}
    )
    assert len(forward) == len(reverse) == 2
    assert len(forward_errors) == len(reverse_errors) == 1


def test_three_modes_on_one_id_refuse_two_and_keep_one():
    """The guard is not a pairwise check that a third repeat slips past."""
    survivors, errors = _partition_by_minted_runbook_id(
        [
            _mode("fm-a", "redis", "Redis OOM", "saturation"),
            _mode("fm-b", "redis", "redis oom", "latency"),
            _mode("fm-c", "redis", "REDIS  OOM!", "errors"),
        ]
    )

    assert [fm.id for fm in survivors] == ["fm-a"]
    assert [e.failure_mode_id for e in errors] == ["fm-b", "fm-c"]
    # Both losers are told which mode holds the id, not just the first.
    assert all("fm-a" in e.error for e in errors)


def test_an_empty_batch_is_not_a_special_case():
    assert _partition_by_minted_runbook_id([]) == ([], [])


def test_the_mint_itself_is_unchanged():
    """#1258 fixes the COLLISION HANDLING, not the id.

    ``runbook_id`` is persisted to ``conversion_drafts`` and written into
    runbook frontmatter, so re-minting would orphan rows that already exist.
    These are the values the issue and its predecessors measured; a change here
    means the fix crossed the boundary #1230 and #1243 both drew.
    """
    assert runbook_id_from_parts("redis", "Redis OOM") == "redis-redis-oom"
    assert runbook_id_from_parts("redis", "redis oom") == "redis-redis-oom"
    assert runbook_id_from_parts("redis", "!!!") == "redis"
    assert runbook_id_from_parts("checkout-api", "Connection pool exhausted") == (
        "checkout-api-connection-pool-exhausted"
    )
    assert len(runbook_id_from_parts("svc", "x" * 500)) == 60
