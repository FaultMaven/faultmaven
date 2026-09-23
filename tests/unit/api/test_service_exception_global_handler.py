"""An LLM failure that reaches a route as ``ServiceException`` is answered precisely
whether or not the route remembers to map it (#552).

Before #552 the precise mapping — ``llm_service_error_http_exception``: 402 for
billing, 429/503/504/502 for provider conditions — existed only as a helper that
a route's ``except ServiceException`` arm had to call. Neither
``ServiceException`` nor ``LLMException`` had a registered handler, so a route
that let one escape answered a bare 500, and the operator-actionable "the AI
provider is out of credits" read to the user as a FaultMaven bug.

Four things are pinned here:

1. **The global path gives the inline path's answer.** A route that raises and
   a route that catches-and-maps return the same status, body and error
   headers, for billing and for a provider 429.
2. **A non-LLM ServiceException is not misclassified** — it is a 500
   ``SERVICE_ERROR``, not a 402 or a retryable 503 — and **no body carries the
   exception text**, which the global handler would otherwise have spread from
   ``/turns`` to every route.
3. **The inventory of route arms that catch the class themselves** (state N):
   such an arm bypasses the global handler, so each one either calls the
   helper, re-raises, or is listed here with the reason it cannot see an LLM
   failure. A new arm fails this file until someone makes that call.
4. **Every ``ServiceException`` raised inside an ``except`` links its cause**
   (item 2 of #552). The billing signal is read off the ``__cause__`` chain
   rather than copied at wrap time, so a wrap is only lossless if it links.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import faultmaven
from faultmaven.api.exception_handlers import (
    QUOTA_EXHAUSTED_DETAIL,
    SERVICE_ERROR_DETAIL,
    get_exception_handlers,
    http_exception_handler,
    is_quota_exhausted_service_error,
    llm_service_error_http_exception,
    service_exception_handler,
)
from faultmaven.exceptions import (
    QUOTA_EXHAUSTED,
    LLMException,
    ServiceException,
)

pytestmark = pytest.mark.unit

_PKG = pathlib.Path(faultmaven.__file__).parent
_REPO = _PKG.parent

_BILLING_TEXT = (
    "You exceeded your current quota, please check your plan and billing details"
)
_INTERNAL_TEXT = (
    "(psycopg2.OperationalError) FATAL: relation "
    'sqlalchemy table "reports" does not exist at db.internal:5432'
)


def _billing_wrap() -> ServiceException:
    """A billing failure wrapped the way a service wraps it — WITHOUT the
    ``details={"error_code": ...}`` copy the two shipped wrap sites still make.
    This is the wrap #552 item 2 is about: a site that forgets the copy."""
    try:
        try:
            raise LLMException(_BILLING_TEXT, status_code=429)
        except LLMException as e:
            raise ServiceException("Report generation failed") from e
    except ServiceException as wrapped:
        return wrapped


def _raise(exc_factory):
    async def endpoint():
        raise exc_factory()

    return endpoint


def _inline(exc_factory):
    """The shape `/turns` ships: catch the class and call the helper."""

    async def endpoint():
        try:
            raise exc_factory()
        except (ServiceException, LLMException) as e:
            raise llm_service_error_http_exception(e)

    return endpoint


_CASES = {
    "billing-wrapped": _billing_wrap,
    "billing-raw": lambda: LLMException(_BILLING_TEXT, status_code=402),
    "rate-limit-raw": lambda: LLMException("slow down", status_code=429),
    "overloaded-wrapped": lambda: _wrap(LLMException("upstream", status_code=503)),
    "plain-service": lambda: ServiceException(_INTERNAL_TEXT),
}


def _wrap(cause: BaseException) -> ServiceException:
    try:
        raise ServiceException(f"Turn processing failed: {cause}") from cause
    except ServiceException as wrapped:
        return wrapped


@pytest.fixture(scope="module")
def client() -> TestClient:
    # Registered exactly as `faultmaven/main.py` registers them — the loop over
    # `get_exception_handlers()` plus the explicit HTTPException handler. That
    # `main.py` really does this is `tests/integration/api/
    # test_exception_handlers_are_registered.py`'s job, which reads the
    # registration off the production app for every key of the mapping.
    app = FastAPI()
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    for name, factory in _CASES.items():
        app.add_api_route(f"/raise/{name}", _raise(factory), methods=["POST"])
        app.add_api_route(f"/inline/{name}", _inline(factory), methods=["POST"])
    return TestClient(app, raise_server_exceptions=False)


def test_both_classes_are_mapped_to_the_llm_handler():
    handlers = get_exception_handlers()
    assert handlers[ServiceException] is service_exception_handler
    assert handlers[LLMException] is service_exception_handler


@pytest.mark.parametrize("name", sorted(_CASES))
def test_the_global_path_answers_what_the_inline_path_answers(client, name):
    """The equality that lets a route stop remembering the mapping."""
    uncaught = client.post(f"/raise/{name}")
    inline = client.post(f"/inline/{name}")

    assert uncaught.status_code == inline.status_code
    assert uncaught.json() == inline.json()
    for header in ("x-error-code", "retry-after"):
        assert uncaught.headers.get(header) == inline.headers.get(header), header


def test_uncaught_billing_is_402_even_without_the_details_copy(client):
    """#552 item 1 and item 2 at once: nothing caught it, and the wrap did not
    copy ``error_code`` into ``details`` — still 402, not a bare 500."""
    resp = client.post("/raise/billing-wrapped")

    assert resp.status_code == 402
    assert resp.headers["x-error-code"] == QUOTA_EXHAUSTED
    assert "retry-after" not in resp.headers  # waiting cannot add credits
    assert resp.json() == {"detail": QUOTA_EXHAUSTED_DETAIL}


def test_uncaught_raw_llm_exception_keeps_its_provider_status(client):
    resp = client.post("/raise/rate-limit-raw")

    assert resp.status_code == 429
    assert resp.headers["x-error-code"] == "RATE_LIMIT_EXCEEDED"
    assert resp.headers["retry-after"] == "60"


def test_a_non_llm_service_exception_is_not_misclassified(client):
    """Not a 402, not a retryable provider 503: an internal failure is a 500."""
    resp = client.post("/raise/plain-service")

    assert resp.status_code == 500
    assert resp.headers["x-error-code"] == "SERVICE_ERROR"
    assert resp.json() == {"detail": SERVICE_ERROR_DETAIL}


@pytest.mark.parametrize("prefix", ["raise", "inline"])
@pytest.mark.parametrize("name", sorted(_CASES))
def test_no_response_body_carries_the_exception_text(client, prefix, name):
    """The fallback used to append ``str(exc)[:200]``. That was one route's
    leak; as the global handler it would have been every route's."""
    body = client.post(f"/{prefix}/{name}").text

    for fragment in ("psycopg2", "db.internal:5432", "sqlalchemy", _BILLING_TEXT):
        assert fragment not in body, (prefix, name, fragment)


class TestQuotaPredicateReadsTheCauseChain:
    """``is_quota_exhausted_service_error`` is what the report route keys 402 on;
    it used to read the wrapper's own ``details`` only."""

    def test_wrap_without_the_details_copy(self):
        assert is_quota_exhausted_service_error(_billing_wrap()) is True

    def test_wrap_with_the_details_copy(self):
        exc = ServiceException("x", details={"error_code": QUOTA_EXHAUSTED})
        assert is_quota_exhausted_service_error(exc) is True

    def test_non_billing_chain(self):
        assert (
            is_quota_exhausted_service_error(
                _wrap(LLMException("upstream", status_code=503))
            )
            is False
        )

    def test_no_marker_fallback_on_a_non_llm_message(self):
        """Narrower than ``is_billing_error`` on purpose: wording alone on an
        exception that declared nothing is not billing here."""
        assert (
            is_quota_exhausted_service_error(ServiceException(_BILLING_TEXT)) is False
        )


# =============================================================================
# State N — route arms that catch the class themselves, and so bypass the
# global handler.
# =============================================================================

#: The classes whose catching bypasses ``service_exception_handler``.
_BYPASSING = {"ServiceException", "LLMException", "FaultMavenException"}

#: Arms that neither call ``llm_service_error_http_exception`` nor re-raise,
#: each with the reason no LLM failure can reach it. Keyed on (file, enclosing
#: function, caught types) rather than a line number, so an edit elsewhere does
#: not re-point an entry, and a changed ``except`` re-opens the question.
_ALLOWED_ARMS: dict[tuple[str, str, str], str] = {
    (
        "faultmaven/modules/case/api/routes.py",
        "create_case",
        "(ServiceException, SessionException)",
    ): "Wraps only `session_service.get_session` — the ownership gate. 503.",
    (
        "faultmaven/modules/case/api/routes.py",
        "resume_case_in_session",
        "(ServiceException, SessionException)",
    ): "Wraps only `session_service.get_session` — the ownership gate. 503.",
    (
        "faultmaven/modules/case/api/routes.py",
        "create_case",
        "ServiceException",
    ): (
        "`CaseService.create_case` makes no LLM call: a title left unset is "
        "filled later by `_generate_and_persist_title`, not here."
    ),
    (
        "faultmaven/modules/case/api/routes.py",
        "list_cases",
        "ServiceException",
    ): "Repository reads only.",
    (
        "faultmaven/modules/case/api/routes.py",
        "_generate_and_persist_title",
        "ServiceException",
    ): (
        "Its `try` wraps the title PERSISTENCE (`update_case`); the LLM call "
        "is in the separate `try` above it, whose failures fall back or raise "
        "ValidationException."
    ),
    (
        "faultmaven/modules/report/api/routes.py",
        "generate_report",
        "ServiceException",
    ): (
        "Maps billing to 402 via `is_quota_exhausted_service_error`, which "
        "reads the cause chain. Billing is the only LLM failure that leaves "
        "`generate_reports`: every other per-type failure is swallowed "
        "(`continue`) in `_generate_reports_locked`."
    ),
    (
        "faultmaven/modules/report/api/routes.py",
        "delete_report",
        "ServiceException",
    ): "Repository delete only.",
}

#: Files the inventory must look in because a ``ServiceException`` arm is known
#: to live there today. A rename that drops one out of the derivation below
#: fails here rather than making the inventory silently smaller.
_KNOWN_ARM_FILES = {
    "faultmaven/modules/case/api/routes.py",
    "faultmaven/modules/report/api/routes.py",
}


def _route_surface() -> list[pathlib.Path]:
    """Where a route arm can be: every `api/` package, `main.py`, and any file
    that constructs an ``APIRouter`` — the same derivation the error-text
    guard uses, so a router outside `api/` is not invisible here."""
    found = set((_PKG / "api").rglob("*.py"))
    found.update(_PKG.glob("modules/*/api/**/*.py"))
    found.add(_PKG / "main.py")
    for path in _PKG.rglob("*.py"):
        if path in found:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(
            isinstance(n, ast.Call)
            and (getattr(n.func, "id", None) or getattr(n.func, "attr", None))
            == "APIRouter"
            for n in ast.walk(tree)
        ):
            found.add(path)
    return sorted(found)


def _caught(node: ast.expr | None) -> set[str]:
    if node is None:
        return set()
    if isinstance(node, ast.Tuple):
        return set().union(*(_caught(e) for e in node.elts))
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Attribute):
        return {node.attr}
    return set()


def _arms(path: pathlib.Path):
    """(enclosing function, caught-types text, handler) for every bypassing arm."""
    tree = ast.parse(path.read_text(encoding="utf-8"))

    def visit(node, fn):
        for child in ast.iter_child_nodes(node):
            inner = (
                child.name
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                else fn
            )
            if (
                isinstance(child, ast.ExceptHandler)
                and _caught(child.type) & _BYPASSING
            ):
                yield fn, ast.unparse(child.type), child
            yield from visit(child, inner)

    yield from visit(tree, None)


def _handles(handler: ast.ExceptHandler) -> bool:
    """Calls the helper, or re-raises what it caught so the global handler runs."""
    for node in ast.walk(handler):
        if isinstance(node, ast.Call) and (
            getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        ) in {"llm_service_error_http_exception"}:
            return True
        if isinstance(node, ast.Raise) and node.exc is None:
            return True
        if (
            isinstance(node, ast.Raise)
            and isinstance(node.exc, ast.Name)
            and handler.name is not None
            and node.exc.id == handler.name
        ):
            return True
    return False


def _inventory() -> list[tuple[tuple[str, str, str], bool]]:
    """A list, not a dict: two arms with the same key must both be seen, or a
    second arm added beside an allowlisted one would inherit its reason."""
    found = []
    for path in _route_surface():
        rel = path.relative_to(_REPO).as_posix()
        for fn, caught, handler in _arms(path):
            found.append(((rel, fn or "<module>", caught), _handles(handler)))
    return found


def test_the_inventory_looks_where_the_arms_are():
    surface = {p.relative_to(_REPO).as_posix() for p in _route_surface()}
    assert _KNOWN_ARM_FILES <= surface
    assert len(surface) >= 45  # vacuity floor: 56 files when written


def test_every_arm_that_bypasses_the_global_handler_is_accounted_for():
    """State N. When #552 landed: 16 arms on the route surface catch
    ``ServiceException``/``LLMException``/``FaultMavenException``. 9 hand the
    failure on (1 calls the helper — `submit_turn`; 8 are bare re-raises of
    ``FaultMavenException``), and the 7 above are listed with their reasons."""
    inventory = _inventory()
    keys = [key for key, _ in inventory]

    unaccounted = sorted(
        key for key, handles in inventory if not handles and key not in _ALLOWED_ARMS
    )
    assert not unaccounted, (
        "A route arm catches ServiceException/LLMException itself, so the global "
        "handler never sees an LLM failure it swallows. Either call "
        "`llm_service_error_http_exception(e)` (or re-raise), or add the arm to "
        f"_ALLOWED_ARMS with the reason no LLM failure can reach it: {unaccounted}"
    )

    stale = sorted(set(_ALLOWED_ARMS) - set(keys))
    assert not stale, f"_ALLOWED_ARMS names arms that no longer exist: {stale}"

    doubled = sorted(key for key in _ALLOWED_ARMS if keys.count(key) > 1)
    assert not doubled, (
        "a second arm shares an allowlisted arm's key, so it would inherit a "
        f"reason written for the first: {doubled}"
    )

    turn = [h for key, h in inventory if key[1] == "submit_turn"]
    assert turn and all(
        turn
    ), "`/turns` stopped calling llm_service_error_http_exception"


# =============================================================================
# Item 2 — a wrap preserves typed metadata by linking its cause.
# =============================================================================


def _unlinked_service_exception_wraps() -> tuple[int, list[str]]:
    total, unlinked = 0, []
    for path in sorted(_PKG.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for handler in ast.walk(tree):
            if not isinstance(handler, ast.ExceptHandler):
                continue
            for node in ast.walk(handler):
                if (
                    isinstance(node, ast.Raise)
                    and isinstance(node.exc, ast.Call)
                    and (
                        getattr(node.exc.func, "id", None)
                        or getattr(node.exc.func, "attr", None)
                    )
                    == "ServiceException"
                ):
                    total += 1
                    if node.cause is None:
                        unlinked.append(
                            f"{path.relative_to(_REPO).as_posix()}:{node.lineno}"
                        )
    return total, unlinked


def test_every_service_exception_wrap_links_its_cause():
    """The readers (`llm_service_error_http_exception`, the quota predicate,
    `is_billing_error`, `LLMErrorHandler._first_error_code`) walk ``__cause__``;
    ``__context__`` is deliberately not walked. So ``raise ServiceException(...)``
    inside an ``except`` without ``from`` drops every typed field the wrapped
    exception carried — the loss #552 item 2 is about. 19 such wraps when #552
    landed; 8 were unlinked (none on an LLM path) and were linked then."""
    total, unlinked = _unlinked_service_exception_wraps()

    assert total >= 15, "the scan found almost nothing — is it looking?"
    assert not unlinked, (
        "`raise ServiceException(...)` inside an `except` without `from e` "
        f"drops the cause chain the error mapping reads: {unlinked}"
    )
