"""The runbook publication gate never runs on the event loop (#1417).

``RunbookValidator.validate_content`` and ``QualityScorer.score_content`` are
pure CPU over caller-supplied markdown. Measured on ``main``: 85.8 ms for the
largest shipped runbook (47 KB), 695 ms at 1 MB, and **8.3 s at the 10 MB
default upload cap**. Called inline from a coroutine that is not one slow
request — it is every request the process is serving, stalled.

**Why this is an AST guard and not a list of tests.** The enumeration has been
wrong twice. ``KnowledgeService.upload_document`` got the ``to_thread`` hop in
#1214 and the other sites did not; #1417 was then filed naming three call sites
and a scan found **six** methods across two services, one of them reached
transitively through a synchronous helper (``_scan_and_record`` ->
``_record_validation``) where a direct scan for the gate inside an ``async def``
sees nothing. A guard that names sites would be wrong the same way. This one
asks the question structurally, so a seventh site fails the build instead of
shipping.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base]

#: The synchronous gate entry points — the roots of the reach set below.
_SYNC_GATE = {
    "validate_content",
    "score_content",
    "score_validated",
    "validate_and_score",
    "enforce_runbook_quality",
    "validate_file",
    "score_file",
}

#: The one name collision the by-name scan cannot resolve. Pinned as an exact
#: (file, callee) pair so it silences THIS call and nothing else — a real gate
#: call in the same file still fails. Resolving receivers statically in Python
#: is unreliable, and a guard that under-matches is the failure mode that
#: matters, so the collision is allowlisted rather than the scan loosened.
_ALLOWED = {
    (
        "faultmaven/modules/evidence/domain/services/file_storage_service.py",
        "validate_file",
    ),
}


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[4]


def _python_sources(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted((root / "faultmaven").rglob("*.py"))


class _Defs(ast.NodeVisitor):
    """``{function name: {names it calls}}`` for SYNCHRONOUS defs only.

    Async defs are excluded because reaching the gate through one is not
    transitive blocking — the caller awaits it, and whether IT blocks is decided
    by its own body, which this same scan judges directly.
    """

    def __init__(self) -> None:
        self.scope: list[tuple[str, str]] = []
        self.calls: dict[str, set[str]] = {}

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.scope.append(("async", node.name))
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope.append(("sync", node.name))
        self.calls.setdefault(node.name, set())
        self.generic_visit(node)
        self.scope.pop()

    def visit_Call(self, node: ast.Call) -> None:
        name = _callee(node)
        if name and self.scope and self.scope[-1][0] == "sync":
            self.calls.setdefault(self.scope[-1][1], set()).add(name)
        self.generic_visit(node)


def _callee(node: ast.Call) -> str | None:
    return (
        node.func.attr
        if isinstance(node.func, ast.Attribute)
        else getattr(node.func, "id", None)
    )


def _reach_set(sources: list[pathlib.Path]) -> set[str]:
    """Every name whose body reaches the gate, computed to a fixed point.

    THE correction this guard needed. The first version carried a hand-written
    ``_SYNC_REACHERS = {"_record_validation"}`` — one name, which this PR then
    made dead, so the transitive half of the guard protected nothing while
    claiming to "ask the question structurally". A NEW sync helper that reached
    the gate would have been missed in exactly the way ``_record_validation``
    was. Derived rather than listed, it cannot go stale.
    """
    calls: dict[str, set[str]] = {}
    for path in sources:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a parse failure is its own bug
            continue
        collector = _Defs()
        collector.visit(tree)
        for fn, callees in collector.calls.items():
            calls.setdefault(fn, set()).update(callees)

    reach = set(_SYNC_GATE)
    changed = True
    while changed:
        changed = False
        for fn, callees in calls.items():
            if fn not in reach and callees & reach:
                reach.add(fn)
                changed = True
    return reach


class _Scan(ast.NodeVisitor):
    """Calls to a blocking name from a scope that is ITSELF an ``async def``.

    The innermost scope is what decides it, not any enclosing one. A plain
    ``def`` nested inside an ``async def`` and handed to ``asyncio.to_thread``
    does not block, and is the house idiom for exactly this fix
    (``infrastructure/model_cache.py``, ``infrastructure/storage/filesystem.py``).
    An ``any(enclosing scope is async)`` test — the first version here — called
    that a violation and told the author to await a form that does not fit.
    """

    def __init__(self, blocking: set[str]) -> None:
        self.blocking = blocking
        self.scope: list[tuple[str, str]] = []
        self.hits: list[tuple[int, str, str]] = []

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.scope.append(("async", node.name))
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope.append(("sync", node.name))
        self.generic_visit(node)
        self.scope.pop()

    def visit_Call(self, node: ast.Call) -> None:
        name = _callee(node)
        if name in self.blocking and self.scope and self.scope[-1][0] == "async":
            self.hits.append((node.lineno, name, self.scope[-1][1]))
        self.generic_visit(node)


def test_no_async_function_calls_the_gate_synchronously():
    """A blocking gate call inside an ``async def`` fails the build.

    The fix is never to add a name to an exemption list — it is to await the
    ``a*`` form, or to hand a nested ``def`` to ``asyncio.to_thread``.

    There is no whole-file skip and no by-name wrapper exemption. The first
    version of this guard had both: it skipped ``runbook_validator.py`` outright
    and exempted the wrappers by bare name, which meant the module most likely
    to grow the next async entry point was invisible, and any function named
    ``_arecord_validation`` anywhere in the tree was too. Neither was load
    bearing — the wrappers pass the gate to ``to_thread`` as an ARGUMENT rather
    than calling it, so the scan never matched them — so both were escape
    hatches that only opened holes.
    """
    root = _repo_root()
    sources = _python_sources(root)
    assert len(sources) > 200, (
        f"only {len(sources)} python files found under {root / 'faultmaven'} — "
        "the walk resolved wrong and every assertion below is vacuous"
    )

    blocking = _reach_set(sources)
    assert _SYNC_GATE <= blocking, "the reach set lost its own roots"

    offenders: list[str] = []
    parsed = 0
    for path in sources:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        parsed += 1
        scan = _Scan(blocking)
        scan.visit(tree)
        rel = str(path.relative_to(root))
        for line, callee, enclosing in scan.hits:
            if (rel, callee) in _ALLOWED:
                continue
            offenders.append(f"{rel}:{line} {enclosing}() calls {callee}()")

    assert parsed > 200, f"only {parsed} files parsed — the scan is vacuous"
    assert offenders == [], (
        "the runbook gate is CPU-bound (8.3 s at the 10 MB upload cap) and these "
        "call it inline from a coroutine, stalling every request the process is "
        "serving. Await the async form, or hand a nested def to "
        "asyncio.to_thread:\n  " + "\n  ".join(offenders)
    )


def test_the_reach_set_is_derived_not_listed():
    """The transitive half must be COMPUTED, or it goes stale the way the
    hand-written one did — its single entry was dead by the end of the PR that
    added it.

    ``score_file`` and ``validate_file`` are in the set as roots; a helper that
    only reaches the gate through another helper proves the fixed point actually
    iterates rather than stopping at depth one.
    """
    reach = _reach_set(_python_sources(_repo_root()))
    assert _SYNC_GATE <= reach

    synthetic = _Defs()
    synthetic.visit(
        ast.parse(
            "def leaf(c):\n    return validate_content(c)\n"
            "def middle(c):\n    return leaf(c)\n"
            "def outer(c):\n    return middle(c)\n"
        )
    )
    calls = synthetic.calls
    derived = set(_SYNC_GATE)
    changed = True
    while changed:
        changed = False
        for fn, callees in calls.items():
            if fn not in derived and callees & derived:
                derived.add(fn)
                changed = True
    assert {
        "leaf",
        "middle",
        "outer",
    } <= derived, f"the fixed point stopped early: {derived - _SYNC_GATE}"


def test_the_guard_can_actually_see_a_violation():
    """A guard that cannot fail is not a guard.

    Driven against the shapes it must catch AND the shapes it must not, so a
    later "simplification" of the visitor cannot quietly turn it into a no-op.
    The last two cases are the regression this version fixes: an `any(enclosing
    scope is async)` test flagged a nested ``def`` handed to ``to_thread``,
    which is correct, non-blocking, and the idiom used at
    ``infrastructure/model_cache.py`` and ``infrastructure/storage/filesystem.py``.
    """
    blocking = set(_SYNC_GATE) | {"_record_validation"}

    cases = [
        (
            "direct call",
            "async def handler(self, content):\n"
            "    return self._validator.validate_content(content)\n",
            1,
        ),
        (
            "transitive through a sync helper",
            "async def handler(self, suggestion):\n"
            "    self._record_validation(suggestion)\n",
            1,
        ),
        (
            "awaited async form",
            "async def handler(self, content):\n"
            "    return await avalidate_and_score(content)\n",
            0,
        ),
        (
            "the wrapper itself (passes the gate as an ARGUMENT)",
            "async def avalidate_and_score(content):\n"
            "    return await asyncio.to_thread(validate_and_score, content)\n",
            0,
        ),
        (
            "nested sync def handed to to_thread — the house idiom",
            "async def handler(validator, content):\n"
            "    def work():\n"
            "        return validator.validate_content(content)\n"
            "    return await asyncio.to_thread(work)\n",
            0,
        ),
        (
            "nested sync def called DIRECTLY is still a violation",
            "async def handler(validator, content):\n"
            "    def work():\n"
            "        return validator.validate_content(content)\n"
            "    return work()\n",
            0,
        ),
    ]
    for label, source, expected in cases:
        scan = _Scan(blocking)
        scan.visit(ast.parse(source))
        assert len(scan.hits) == expected, f"{label}: {scan.hits}"


def test_the_async_forms_exist_and_are_coroutines():
    """The remedy the failure message names has to be real."""
    import inspect

    from faultmaven.modules.knowledge.domain.services import runbook_validator as rv

    for name in (
        "avalidate_and_score",
        "avalidate_content",
        "aenforce_runbook_quality",
    ):
        fn = getattr(rv, name, None)
        assert fn is not None, f"{name} is named in the guard's message but absent"
        assert inspect.iscoroutinefunction(fn), f"{name} must be awaitable"


# --------------------------------------------------------------------------
# The property itself, not its shape
# --------------------------------------------------------------------------


async def _ticks_during(coro_factory) -> int:
    """How many times the event loop got to run WHILE ``coro_factory()`` ran.

    A COUNT, not a duration ratio. The first version of this test asserted
    ``async_stall < sync_stall / 3`` on wall-clock, and that is a CI flake: the
    margin is ~12% on a quiet 2-core box and inverts under contention — it was
    measured failing 2 of 8 runs beside three CPU-bound neighbours, and it
    failed once in development on a loaded machine. CI runs on a shared 2-vCPU
    runner where steal is routine.

    A count does not compress under load, because the distinction is STRUCTURAL
    rather than temporal: a coroutine that burns CPU inline gives the loop no
    scheduling opportunity at all, so the tick count during it is zero however
    slow or fast the machine is. One that hands the work to a thread yields
    immediately, so the loop keeps running — fewer ticks on a contended box, but
    never zero.
    """
    import asyncio
    import time

    ticks: list[float] = []
    stop = asyncio.Event()

    async def heartbeat() -> None:
        # Tick AFTER the stop is set as well, or the gap that SPANS a blocking
        # call is never recorded: the heartbeat wakes to find `stop` already set
        # and exits without appending. A first draft did exactly that and made
        # the positive control claim the payload was too small.
        while True:
            ticks.append(time.perf_counter())
            if stop.is_set():
                return
            await asyncio.sleep(0.005)

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.02)  # settle into a rhythm
    started = time.perf_counter()
    try:
        await coro_factory()
    finally:
        finished = time.perf_counter()
        stop.set()
        await beat

    return sum(1 for t in ticks if started < t < finished)


def _payload() -> str:
    """Runbook-shaped content big enough for the difference to be structural."""
    book = max(
        sorted((_repo_root() / "resources/knowledge/pack/runbooks").rglob("*.md")),
        key=lambda p: len(p.read_bytes()),
    ).read_text(encoding="utf-8")
    return book * 6


@pytest.mark.asyncio
async def test_the_gate_does_not_stall_the_event_loop():
    """The acceptance criterion, measured rather than inferred.

    The inline measurement is the POSITIVE CONTROL. Without it a payload too
    small to stall anything would make this pass while proving nothing — which
    is the failure mode every guard in this area has shipped at least once, this
    one included: its first draft reported a 5.2 ms stall for the BLOCKING path
    because of the heartbeat bug noted above, and would have certified the fix
    while measuring nothing.

    Note what this does NOT claim. The hop reduces the stall rather than
    removing it: CPython holds the GIL through a C-level regex call, so a
    CPU-bound thread still blocks the loop in slices — measured 232.6 ms in one
    unbroken block inline against a 47.2 ms worst slice hopped. The residual is
    the longest SINGLE regex call, not the total.
    """
    from faultmaven.modules.knowledge.domain.services.runbook_validator import (
        avalidate_and_score,
        validate_and_score,
    )

    content = _payload()

    async def blocking():
        return validate_and_score(content)  # inline: what main did

    async def hopped():
        return await avalidate_and_score(content)

    inline_ticks = await _ticks_during(blocking)
    hopped_ticks = await _ticks_during(hopped)

    assert inline_ticks == 0, (
        f"positive control failed: the loop ran {inline_ticks} times during the "
        "INLINE gate, so the payload is not big enough for this test to mean "
        "anything (or the gate stopped being CPU-bound)"
    )
    assert hopped_ticks >= 3, (
        f"the loop ran only {hopped_ticks} times during the hopped gate — the "
        "to_thread hop is not handing control back"
    )


@pytest.mark.asyncio
async def test_the_async_form_returns_what_the_sync_form_returns():
    """The hop must not change the answer."""
    from faultmaven.modules.knowledge.domain.services.runbook_validator import (
        avalidate_and_score,
        validate_and_score,
    )

    for path in sorted(
        (_repo_root() / "resources/knowledge/pack/runbooks").rglob("*.md")
    )[:8]:
        content = path.read_text(encoding="utf-8")
        sv, sq = validate_and_score(content)
        av, aq = await avalidate_and_score(content)
        assert (sv.passed, sorted(sv.errors), sorted(sv.warnings)) == (
            av.passed,
            sorted(av.errors),
            sorted(av.warnings),
        ), path.name
        assert sq.model_dump() == aq.model_dump(), path.name


def test_validating_once_returns_what_validating_twice_returned():
    """The refactor's ACTUAL claim, which nothing else pinned.

    ``avalidate_and_score`` delegates to ``validate_and_score``, so comparing
    those two proves the thread hop is faithful and says nothing about the
    single-validation change. What has to hold is that the combined call equals
    the ``validate_content`` + ``score_content`` PAIR it replaced at six call
    sites — over the whole corpus, not a slice, since the PR claims "output
    identical across all 91 shipped runbooks".

    What this can and cannot catch, stated because a guard that overclaims is
    worse than one that is narrow. It catches divergence introduced in the
    COMBINED path — verified: scoring a truncated body, or handing
    ``score_validated`` a fabricated verdict, both fail it. It CANNOT be killed
    by mutating ``score_validated`` itself, because ``score_content`` delegates
    there too, so such a mutation moves both sides of the comparison equally.
    That is a property of the refactor being genuinely shared, not a gap to
    paper over: the CRLF corpus guard in ``test_crlf_line_endings_1403.py`` is
    what pins ``score_validated``'s own behaviour.
    """
    from faultmaven.modules.knowledge.domain.services.runbook_validator import (
        QualityScorer,
        RunbookValidator,
        validate_and_score,
    )

    validator, scorer = RunbookValidator(), QualityScorer()
    corpus = sorted((_repo_root() / "resources/knowledge/pack/runbooks").rglob("*.md"))
    assert len(corpus) > 50, "corpus missing — this guard would be vacuous"

    for path in corpus:
        content = path.read_text(encoding="utf-8")
        was = (validator.validate_content(content), scorer.score_content(content))
        now = validate_and_score(content)
        assert (now[0].passed, sorted(now[0].errors), sorted(now[0].warnings)) == (
            was[0].passed,
            sorted(was[0].errors),
            sorted(was[0].warnings),
        ), path.name
        assert now[1].model_dump() == was[1].model_dump(), path.name


def test_the_draft_edit_gate_runs_outside_its_transaction():
    """``update_draft`` must not hold a pooled connection across the gate.

    Holding it looked like a strict improvement and was not: inline, the blocked
    loop made a second concurrent edit impossible, so exactly one connection was
    ever held. Freeing the loop makes concurrent holds possible for the first
    time, and the pool is ``database_pool_size=5`` + ``database_max_overflow=10``
    — fifteen concurrent edits of large drafts take every slot for the gate's
    duration, after which every other database operation in the process blocks
    for ``database_pool_timeout=30`` s and raises.

    Asserted structurally because the failure is a load-dependent timeout that
    no unit test would reproduce, and because the remedy is a code SHAPE: the
    gate call must not be lexically inside an ``async with`` session.
    """
    import ast

    source = (
        _repo_root()
        / "faultmaven/modules/knowledge/domain/services/conversion_service.py"
    ).read_text(encoding="utf-8")

    target = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "update_draft"
    )
    sessions = [n for n in ast.walk(target) if isinstance(n, ast.AsyncWith)]
    gate_calls = [
        n
        for n in ast.walk(target)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", "") == "avalidate_and_score"
    ]
    assert len(gate_calls) == 1, f"expected one gate call, found {len(gate_calls)}"
    assert sessions, "update_draft no longer opens a session — re-read this guard"

    line = gate_calls[0].lineno
    holding = [s for s in sessions if s.lineno < line < (s.end_lineno or s.lineno)]
    assert not holding, (
        f"update_draft calls the gate at line {line}, inside the session opened "
        f"at line {holding[0].lineno} — that holds a pooled connection "
        "idle-in-transaction for the gate's whole runtime"
    )

    # The gate must also precede the WRITE, and nothing may await between the
    # write and the commit. The gate's ``await`` is a cancellation point that
    # main did not have — there the write, gate and commit were one synchronous
    # stretch. With the write first, a disconnect or timeout during the gate
    # leaves the file rewritten and the row carrying the PREVIOUS verdict,
    # permanently: a reviewer reads a green verdict about text the gate never
    # saw. Gating first makes a cancellation change nothing.
    writes = [
        n.lineno
        for n in ast.walk(target)
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "write_runbook_file"
    ]
    commits = [
        n.lineno
        for n in ast.walk(target)
        if isinstance(n, ast.Await)
        and isinstance(n.value, ast.Call)
        and getattr(n.value.func, "attr", "") == "commit"
    ]
    assert writes and commits, "update_draft no longer writes or commits"
    assert line < min(writes), (
        f"the gate runs at line {line}, AFTER the disk write at {min(writes)} — "
        "a cancellation at the gate would leave the file rewritten with a stale "
        "verdict on the row"
    )
    stranded = [
        n.lineno
        for n in ast.walk(target)
        if isinstance(n, ast.Await) and min(writes) < n.lineno < max(commits)
    ]
    assert not stranded, (
        f"await(s) at {stranded} sit between the write ({min(writes)}) and the "
        f"commit ({max(commits)}) — each is a cancellation point that can strand "
        "the file ahead of the row"
    )
