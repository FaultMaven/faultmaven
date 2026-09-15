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

#: The synchronous gate entry points. An ``async def`` must not call these.
_SYNC_GATE = {
    "validate_content",
    "score_content",
    "score_validated",
    "validate_and_score",
    "enforce_runbook_quality",
    "validate_file",
    "score_file",
}

#: Names whose bodies reach the gate synchronously. Calling one of these from an
#: ``async def`` blocks exactly as much as calling the gate directly — this is
#: the transitive case that made #1417's own enumeration miss two sites.
_SYNC_REACHERS = {"_record_validation"}

#: Where the gate legitimately appears inside an ``async def``: the ``a*``
#: wrappers whose whole body is the ``to_thread`` hop.
_ASYNC_WRAPPERS = {
    "avalidate_and_score",
    "avalidate_content",
    "aenforce_runbook_quality",
    "_arecord_validation",
}


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[4]


def _module_defines_the_gate(path: pathlib.Path) -> bool:
    """The validator module itself defines both the sync and async forms."""
    return path.name == "runbook_validator.py"


class _Scan(ast.NodeVisitor):
    """Every call to a blocking gate entry point from inside an ``async def``.

    ``validate_file`` is deliberately matched by NAME rather than by resolving
    the receiver, which costs one known false positive:
    ``FileStorageService.validate_file`` is an unrelated method that happens to
    share it. Resolving receivers statically in Python is unreliable, and a
    guard that under-matches is the failure mode that matters here — so the
    collision is allowlisted explicitly below rather than papered over by
    loosening the scan.
    """

    def __init__(self) -> None:
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
        name = (
            node.func.attr
            if isinstance(node.func, ast.Attribute)
            else getattr(node.func, "id", None)
        )
        if name in _SYNC_GATE | _SYNC_REACHERS:
            enclosing = next(
                (n for kind, n in reversed(self.scope) if kind in ("async", "sync")),
                "<module>",
            )
            if (
                any(kind == "async" for kind, _ in self.scope)
                and enclosing not in _ASYNC_WRAPPERS
            ):
                self.hits.append((node.lineno, name, enclosing))
        self.generic_visit(node)


#: The one name collision the by-name scan cannot distinguish. Pinned as an
#: exact (file, callee) pair, so it silences THIS call and nothing else — a
#: real gate call appearing in the same file still fails.
_ALLOWED = {
    (
        "faultmaven/modules/evidence/domain/services/file_storage_service.py",
        "validate_file",
    ),
}


def test_no_async_function_calls_the_gate_synchronously():
    """A blocking gate call inside an ``async def`` fails the build.

    The fix is never to add a name here — it is to await the ``a*`` form
    (``avalidate_and_score`` / ``avalidate_content`` /
    ``aenforce_runbook_quality``), which does the ``asyncio.to_thread`` hop.
    """
    root = _repo_root()
    offenders: list[str] = []

    for path in sorted((root / "faultmaven").rglob("*.py")):
        if _module_defines_the_gate(path):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a parse failure is its own bug
            continue
        scan = _Scan()
        scan.visit(tree)
        rel = str(path.relative_to(root))
        for line, callee, enclosing in scan.hits:
            if (rel, callee) in _ALLOWED:
                continue
            offenders.append(f"{rel}:{line} {enclosing}() calls {callee}()")

    assert offenders == [], (
        "the runbook gate is CPU-bound (8.3 s at the 10 MB upload cap) and these "
        "call it inline from a coroutine, stalling every request the process is "
        "serving. Await the async form instead:\n  " + "\n  ".join(offenders)
    )


def test_the_guard_can_actually_see_a_violation():
    """A guard that cannot fail is not a guard.

    The scan is driven against a synthetic module carrying the exact shapes it
    has to catch — a direct call, and the transitive one through a sync helper
    that made the original enumeration miss two sites — and against the shape it
    must NOT flag, so a future "simplification" of the visitor cannot quietly
    turn it into a no-op.
    """
    offending = ast.parse(
        "async def handler(self, content):\n"
        "    return self._validator.validate_content(content)\n"
    )
    transitive = ast.parse(
        "async def handler(self, suggestion):\n"
        "    self._record_validation(suggestion)\n"
    )
    clean = ast.parse(
        "async def handler(self, content):\n"
        "    return await avalidate_and_score(content)\n"
    )
    wrapper = ast.parse(
        "async def avalidate_and_score(content):\n"
        "    return await asyncio.to_thread(validate_and_score, content)\n"
    )

    for label, tree, expected in (
        ("direct", offending, 1),
        ("transitive", transitive, 1),
        ("awaited async form", clean, 0),
        ("the wrapper itself", wrapper, 0),
    ):
        scan = _Scan()
        scan.visit(tree)
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


async def _max_stall_while(coro_factory) -> float:
    """Longest gap between heartbeat ticks while ``coro_factory()`` runs.

    A coroutine that yields lets the heartbeat tick on schedule; one that burns
    CPU inline starves it for exactly as long as it runs. The gap IS the stall
    every other request in the process would see.
    """
    import asyncio
    import time

    ticks: list[float] = []
    stop = asyncio.Event()

    async def heartbeat() -> None:
        # Tick AFTER the stop is set as well, or the gap that SPANS the blocking
        # call is never recorded: the heartbeat wakes to find `stop` already
        # set and exits without appending, and the measurement reports the
        # 5 ms idle rhythm instead of the 230 ms stall. A first draft of this
        # harness did exactly that and made the positive control claim the
        # payload was too small.
        while True:
            ticks.append(time.perf_counter())
            if stop.is_set():
                return
            await asyncio.sleep(0.005)

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.02)  # let it settle into a rhythm
    try:
        await coro_factory()
    finally:
        stop.set()
        await beat

    return max((b - a) for a, b in zip(ticks, ticks[1:])) if len(ticks) > 2 else 0.0


def _payload() -> str:
    """Runbook-shaped content big enough for the stall to dominate jitter."""
    book = max(
        sorted((_repo_root() / "resources/knowledge/pack/runbooks").rglob("*.md")),
        key=lambda p: len(p.read_bytes()),
    ).read_text(encoding="utf-8")
    return book * 6


@pytest.mark.asyncio
async def test_the_gate_does_not_stall_the_event_loop():
    """The acceptance criterion, measured rather than inferred.

    Asserted as a RATIO against the synchronous path rather than an absolute
    bound, for the reason the ReDoS guards in this package give: wall-clock
    bounds flake on a shared runner, but "blocks the loop" and "does not" differ
    by an order of magnitude on any machine.

    The synchronous measurement is also the POSITIVE CONTROL. Without it a
    payload too small to stall anything would make this pass while proving
    nothing — which is the failure mode every guard in this area has shipped at
    least once.
    """
    import asyncio

    from faultmaven.modules.knowledge.domain.services.runbook_validator import (
        avalidate_and_score,
        validate_and_score,
    )

    content = _payload()

    async def blocking():
        return validate_and_score(content)  # inline: what main does today

    async def hopped():
        return await avalidate_and_score(content)

    sync_stall = await _max_stall_while(blocking)
    async_stall = await _max_stall_while(hopped)

    assert sync_stall > 0.05, (
        "positive control failed: the synchronous path did not stall the loop "
        f"measurably ({sync_stall*1000:.1f} ms), so this test proves nothing. "
        "Increase the payload."
    )
    # A RATIO, and a modest one, because the hop reduces the stall rather than
    # removing it. CPython holds the GIL through a C-level regex call, so a
    # CPU-bound thread still blocks the loop in slices: measured 232.6 ms in one
    # unbroken block inline, against a 47.2 ms worst slice hopped — a 4.9x
    # improvement, with the loop making 13 ticks of progress instead of 5. The
    # residual is the longest SINGLE regex call, not the total gate time.
    assert async_stall < sync_stall / 3, (
        f"the async form stalled the loop for {async_stall*1000:.1f} ms against "
        f"{sync_stall*1000:.1f} ms for the inline call — the to_thread hop is "
        "not taking the work off the loop"
    )


@pytest.mark.asyncio
async def test_the_async_form_returns_what_the_sync_form_returns():
    """The hop must not change the answer — including on the shipped corpus,
    since the verdict is persisted and the score drives a user-visible warning.
    """
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
