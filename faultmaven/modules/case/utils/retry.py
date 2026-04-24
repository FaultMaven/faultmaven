"""Optimistic concurrency retry helper for the Case aggregate.

Callers that do read-modify-write on a ``Case`` and need to survive a
concurrent writer should use ``update_case_with_retry``. On each
attempt the helper loads a fresh ``Case``, invokes the caller's
mutator on it, then saves. If the save raises
``StaleCaseException``, the helper retries from a fresh load — up to
``max_attempts`` times.

We deliberately chose a helper function over a decorator because this
pattern isn't an idempotent "call the function again" retry: the
mutation has to be re-applied to a NEW Case instance each attempt.
Making that explicit at the call site — the mutator receives the
reloaded case as its argument — is a feature.

**Do NOT use this to retry LLM turns.** ``/turns`` is an expensive,
non-idempotent operation (tool calls trigger external side effects,
background tasks fire, tokens get spent). On OCC conflict a turn
handler should surface 409 to the client and let the client decide
whether to retry; a silent retry doubles latency, cost, and risk of
duplicated side effects.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from faultmaven.modules.case.contracts import Case, ICaseRepository
from faultmaven.modules.case.exceptions import (
    CaseNotFoundError,
    StaleCaseException,
)

CaseMutator = Callable[[Case], Awaitable[None]]


async def update_case_with_retry(
    repo: ICaseRepository,
    case_id: str,
    mutator: CaseMutator,
    max_attempts: int = 3,
) -> Case:
    """Load the case, apply ``mutator``, save — retry on version conflict.

    The mutator sees a fresh ``Case`` every attempt (loaded via
    ``repo.get(case_id)``). It should be idempotent with respect to the
    case state — given the same loaded case, it should produce the same
    mutation — because on conflict we re-run it against a fresh load.

    Args:
        repo: Case repository (usually ``self.repository`` on a service).
        case_id: Identifier of the case to mutate.
        mutator: Async callable that receives the loaded case and mutates
            it in place. Must not call ``repo.save(case)`` itself — this
            helper is the save path.
        max_attempts: How many times to reload-and-retry on OCC conflict.
            Default 3; raise ``StaleCaseException`` if exhausted.

    Returns:
        The saved ``Case`` (the same instance the mutator operated on,
        with ``version`` bumped by the underlying save).

    Raises:
        CaseNotFoundError: If ``case_id`` doesn't resolve to a case.
        StaleCaseException: If the mutator's save keeps losing the
            version race for ``max_attempts`` attempts. Callers that
            care about this tail can either bump ``max_attempts`` (for
            low-contention paths where a few retries are cheap) or
            surface the exception upward.
    """
    last_stale: StaleCaseException | None = None
    for attempt in range(max_attempts):
        case = await repo.get(case_id)
        if case is None:
            raise CaseNotFoundError(case_id=case_id)
        await mutator(case)
        try:
            return await repo.save(case)
        except StaleCaseException as exc:
            last_stale = exc
            # Reload and re-apply on the next iteration.
            continue

    # All attempts lost the race; re-raise the last conflict.
    assert last_stale is not None  # max_attempts >= 1 in practice
    raise last_stale
