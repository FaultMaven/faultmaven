"""Raw exception text never reaches a response from anywhere on the API surface.

#866 (knowledge), #966 (case), #1061 (auth) and #1394 (``main.py``) each swept
one file and left a guard behind watching that one file. #1400 measured what
those four guards do **not** watch: running the same shared analysis
(``tests/error_text_ast``) over the rest of the response-producing surface found
19 sites in 9 files with no guard at all, 15 of them real leaks. CodeQL's
``py/stack-trace-exposure`` flagged none of them — its alerts are the subset
where the value provably reaches an HTTP response, and a middleware method or a
dependency function returning a dict is one hop too far.

So this guard is not a tenth per-file guard. It is scoped to **the surface**,
derived from the tree rather than listed, because the failure the per-file
guards have is structural: adding a tenth file cannot trip a guard that names
nine. The repository's oldest error-text guard has been green for eight months
while watching directories that contain none of the violations it exists to
catch, and a hardcoded file list is how that happens.

**Where the rule can be violated**, and therefore what is scanned:

* everything under ``faultmaven/api/`` — routers, middleware, dependencies,
  ``protection.py``; two of #1400's nine were dependency/bootstrap modules
  rather than routers, which is why this is the directory and not a router
  list;
* every ``faultmaven/modules/*/api/`` package;
* ``faultmaven/main.py``, which mounts the health, readiness and metrics
  endpoints directly;
* any file anywhere that constructs an ``APIRouter`` — today that adds exactly
  one, ``infrastructure/observability/metrics_exporters/prometheus.py``, and
  tomorrow it adds whatever a new module puts somewhere unexpected;
* any file anywhere that defines ASGI middleware — a ``BaseHTTPMiddleware``
  subclass or an ``async def dispatch(self, request, call_next)``. #1400's
  mutation matrix added a leaking middleware class under
  ``faultmaven/infrastructure/`` and the router arm alone did not see it: a
  middleware answers requests without ever touching an ``APIRouter``, and two
  of the nine files this issue swept were middleware.

The derivation is asserted, not trusted: ``test_the_surface_derivation_still_
finds_every_known_response_producer`` fails if a rename or a move drops one of
the files that is known to be able to violate the rule, and the floors below
fail if the derivation silently collapses. A guard that scans nothing passes
every assertion in it.

**Relationship to the four per-file guards.** They stay. Each carries an
end-to-end test driving a real request through a real handler and asserting on
the body a client receives — this file asserts only about source, and a source
assertion about a leak is worth much less without one execution proving the
analysis describes a real wire format. Their class assertions are now also
covered here, which is deliberate: the per-file floor (``main.py`` has ≥ 40
bound handlers) and the surface floor answer different questions.
"""

import ast
import functools
import pathlib

import pytest

import faultmaven
from tests.error_text_ast import (
    http_exception_leak_sites,
    returned_body_leak_sites,
)

_PKG = pathlib.Path(faultmaven.__file__).parent
_REPO = _PKG.parent


# Files that are known, today, to be able to put bytes on the wire. The
# derivation below must contain every one of them. This is the half of the
# "state where the rule can be violated" gate that a floor cannot do: a floor
# notices the scan collapsing to nothing, and notices nothing at all when one
# router is renamed out of the glob's reach.
_KNOWN_RESPONSE_PRODUCERS = frozenset(
    {
        "faultmaven/main.py",
        "faultmaven/api/exception_handlers.py",
        "faultmaven/api/protection.py",
        "faultmaven/api/v1/auth_dependencies.py",
        "faultmaven/api/middleware/auth.py",
        "faultmaven/api/middleware/body_size.py",
        "faultmaven/api/middleware/contract_probe.py",
        "faultmaven/api/middleware/deduplication.py",
        "faultmaven/api/middleware/idempotency.py",
        "faultmaven/api/middleware/performance.py",
        "faultmaven/api/middleware/rate_limiting.py",
        "faultmaven/api/middleware/route_policy.py",
        "faultmaven/api/middleware/tenant_scope.py",
        "faultmaven/api/middleware/trailing_slash.py",
        "faultmaven/api/routes/admin.py",
        "faultmaven/api/routes/admin_cases.py",
        "faultmaven/api/routes/admin_config.py",
        "faultmaven/api/routes/admin_grants.py",
        "faultmaven/api/routes/sessions.py",
        "faultmaven/modules/auth/api/auth.py",
        "faultmaven/modules/auth/api/invitations.py",
        "faultmaven/modules/auth/api/oauth.py",
        "faultmaven/modules/auth/api/session.py",
        "faultmaven/modules/auth/api/sso.py",
        "faultmaven/modules/auth/api/teams.py",
        "faultmaven/modules/case/api/routes.py",
        "faultmaven/modules/knowledge/api/conversion_routes.py",
        "faultmaven/modules/knowledge/api/routes.py",
        "faultmaven/modules/report/api/routes.py",
        # Outside every ``api/`` directory — found only by the APIRouter arm.
        "faultmaven/infrastructure/observability/metrics_exporters/prometheus.py",
    }
)

# Vacuity floors. The surface was 56 files carrying 254 bound ``except ... as``
# handlers when this guard was written; the floors only have to be high enough
# that a broken glob, a failed parse or a gutted tree cannot pass by inspecting
# nothing.
_MIN_SURFACE_FILES = 45
_MIN_BOUND_HANDLERS = 200


# Sites the analysis reports that are NOT leaks, each with the reason it is
# safe where it sits. Keyed on (path, enclosing function, unparsed expression)
# rather than a line number, so an edit elsewhere in the file does not silently
# re-point an entry at a different statement, and an edit to the statement
# ITSELF re-opens the question rather than inheriting the old justification.
#
# Every entry is also justified in a comment at the site, because the next
# person to read the code will not be reading this file.
_ALLOWED: dict[tuple[str, str, str], str] = {
    (
        "faultmaven/api/middleware/contract_probe.py",
        "_analyze_response_shape",
        "{'analysis_error': str(e)}",
    ): (
        "Not a response. The dict lands in `probe_data`, whose only consumer "
        "is `_log_contract_probe`; `dispatch` returns the downstream response "
        "object untouched."
    ),
    (
        "faultmaven/api/middleware/rate_limiting.py",
        "dispatch",
        "self._create_rate_limit_response(e, request)",
    ): (
        "Typed domain exception. `RateLimitError` is raised by this codebase "
        "and the 429 body is built from its declared fields, not `str(e)`. A "
        "caller told to back off has to be told what it exceeded — the "
        "#866/#966 carve-out."
    ),
    (
        "faultmaven/api/protection.py",
        "setup_protection_middleware",
        "setup_info",
    ): (
        "Not a response. Bootstrap-only: `main.py` logs two keys and parks the "
        "dict in `app.extra['protection_info']`, which no route, dependency or "
        "middleware reads into a body."
    ),
    (
        "faultmaven/api/routes/admin_config.py",
        "check_llm_connection",
        "LLMConnectionTestResponse(provider=provider_name, connected=False, "
        "response_time_ms=elapsed_ms, error_message=f'Connection test failed "
        "({type(e).__name__})', timestamp=datetime.now(timezone.utc))",
    ): (
        "The exception's CLASS, not its message. Safe because a class name is "
        "a literal in the SDK's source, chosen at import time and never "
        "assembled from a URL, a `host:port`, a key fragment or an upstream "
        "body — the analysis reports it only because shape D cannot know that "
        "`type` is safe. Kept rather than redacted because the Dashboard's LLM "
        "Config page renders `error_message` verbatim (ProviderCard.tsx:293) "
        "with no other channel, so a constant string makes a wrong key, a "
        "wrong base URL, a rate limit and a DNS failure identical on the one "
        "endpoint whose purpose is to say which."
    ),
    (
        "faultmaven/modules/knowledge/api/conversion_routes.py",
        "convert_document",
        "JSONResponse(status_code=status, content={'detail': str(e), "
        "'error_code': error_code})",
    ): (
        "Typed domain exception. Every `ConversionRejectedError` construction "
        "is a hand-written caller-facing sentence; the one that interpolated a "
        "parse exception was made static in #1400. The broad `except "
        "Exception` below it is separately sanitised."
    ),
}


@functools.lru_cache(maxsize=1)
def _surface() -> tuple[pathlib.Path, ...]:
    """Every file that can put bytes on the wire.

    Derived, not listed. A list is what the four per-file guards are, and what
    #1400 found nine files sitting outside of.

    Cached because the derivation parses every module in the package (478 files,
    2.3s) and three tests here call it; the source cannot change inside one
    pytest session, so the second and third calls can only re-derive the same
    answer. A tuple, because ``lru_cache`` hands every caller the same object
    and a list would let one test mutate what the next one sees.
    """
    found: set[pathlib.Path] = set()
    found.update((_PKG / "api").rglob("*.py"))
    found.update(_PKG.glob("modules/*/api/**/*.py"))
    found.add(_PKG / "main.py")

    # Plus any router OR middleware defined somewhere the two globs above do
    # not reach. Both arms exist because a real file needed each:
    # `metrics_exporters/prometheus.py` is a router outside every `api/`
    # directory, and #1400's mutation matrix showed a leaking middleware class
    # placed under `infrastructure/` sailing past a router-only derivation.
    for path in _PKG.rglob("*.py"):
        if path in found:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - defensive
            continue
        if _defines_a_response_producer(tree):
            found.add(path)
    return tuple(sorted(found))


def _defines_a_response_producer(tree: ast.AST) -> bool:
    """Does this module construct a router, or define ASGI middleware?

    Middleware is recognised two ways because the codebase writes it both
    ways: by base class (``BaseHTTPMiddleware``, whatever it is imported as)
    and by the ASGI/Starlette hook name ``dispatch``. Either is enough — the
    question is only whether this file can be the thing that writes a body.
    """
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and (getattr(node.func, "id", None) or getattr(node.func, "attr", None))
            == "APIRouter"
        ):
            return True
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                if (
                    getattr(base, "id", None) or getattr(base, "attr", None)
                ) == "BaseHTTPMiddleware":
                    return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name == "dispatch"
        ):
            return True
    return False


def _rel(path: pathlib.Path) -> str:
    return path.relative_to(_REPO).as_posix()


def _offender_keys(path: pathlib.Path) -> list[tuple[str, str, str, int]]:
    """``(path, function, expression, line)`` for every site the analysis reports.

    The shared analysis returns ``file:line``. The expression is recovered here
    rather than there so that widening the key does not change the four guards
    that already depend on that return shape.
    """
    lines = {
        int(site.rsplit(":", 1)[1])
        for site in (*http_exception_leak_sites(path), *returned_body_leak_sites(path))
    }
    if not lines:
        return []

    tree = ast.parse(path.read_text(encoding="utf-8"))
    enclosing: dict[int, str] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(fn):
                # Innermost wins: `ast.walk` visits outer functions first, so a
                # nested def overwrites its parent's claim on its own lines.
                enclosing[getattr(node, "lineno", -1)] = fn.name

    keys: list[tuple[str, str, str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Return, ast.Raise)) or node.lineno not in lines:
            continue
        expr = node.value if isinstance(node, ast.Return) else node.exc
        if expr is None:  # pragma: no cover - the analysis never reports these
            continue
        keys.append(
            (
                _rel(path),
                enclosing.get(node.lineno, "<module>"),
                ast.unparse(expr),
                node.lineno,
            )
        )
    return keys


@pytest.mark.unit
def test_the_surface_derivation_still_finds_every_known_response_producer():
    """The scan looked where the rule can be violated.

    A guard's answer on the current tree says nothing about its reach, and the
    way an error-text guard goes quiet is not by being wrong — it is by being
    pointed somewhere the violations are not. This asserts the pointing.

    A file renamed or moved out of the derivation fails here rather than
    disappearing from the scan, and a genuinely-retired module is removed from
    ``_KNOWN_RESPONSE_PRODUCERS`` deliberately, in the same commit.
    """
    scanned = {_rel(p) for p in _surface()}
    missing = sorted(_KNOWN_RESPONSE_PRODUCERS - scanned)

    assert missing == [], (
        "these files can put bytes on the wire and the derivation no longer "
        f"reaches them, so the leak guard below is not looking at them: {missing}"
    )


@pytest.mark.unit
def test_the_surface_scan_is_not_vacuous():
    """Floors, so a broken glob cannot pass by inspecting nothing.

    Both numbers, because they fail differently: the file count catches the
    globs collapsing, the handler count catches a tree where the files are
    found and there is nothing in them to analyse.
    """
    surface = _surface()
    handlers = 0
    for path in surface:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ExceptHandler) and node.name:
                handlers += 1

    assert len(surface) >= _MIN_SURFACE_FILES, (
        f"the API surface derivation found only {len(surface)} files — the "
        "leak guard below would pass without inspecting the surface"
    )
    assert handlers >= _MIN_BOUND_HANDLERS, (
        f"the API surface carries only {handlers} bound except handlers — the "
        "leak guard below would pass without inspecting anything"
    )


@pytest.mark.unit
def test_no_api_surface_site_puts_the_caught_exception_on_the_wire():
    """The class guard, over the whole surface.

    Covers every shape the shared analysis knows: a 5xx ``HTTPException``
    whose ``detail`` carries the caught exception (directly, through a local
    alias, or through a local the handler tainted and a later statement
    raises), a ``return`` carrying it into a body — the shape a handler that
    degrades to a 200 uses, which the ``HTTPException`` half structurally
    cannot see — and either of those rendering the live exception with
    ``traceback.format_exc()`` under a handler that binds no name at all.

    Two shapes are deliberately NOT covered and are recorded in #1598 rather
    than left to be rediscovered: a 4xx raised from a *broad* ``except``
    (#866/#966 scoped this rule to 5xx, and changing that is a policy call),
    and a custom typed exception carrying the text into one of the four
    domain handlers that render ``str(exc)``. Both were measured at zero live
    sites that are actually leaks.
    """
    offenders: list[str] = []
    for path in _surface():
        for rel, fn, expr, line in _offender_keys(path):
            if (rel, fn, expr) in _ALLOWED:
                continue
            offenders.append(f"{rel}:{line} in {fn}(): {expr}")

    assert offenders == [], (
        "sites carrying a caught exception's text into a response (log it "
        "server-side and answer with a static message; if the site is safe, "
        "justify it at the site and add it to _ALLOWED with a reason):\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.unit
def test_every_allowlist_entry_still_matches_a_real_site():
    """An allowlist entry that matches nothing is a stale exemption.

    Without this, a fixed site leaves its justification behind to cover
    whatever later occupies the same (file, function, expression) — which is
    exactly the "narrow the rule until it is quiet" failure that put the
    original blind spot there. The four entries here were measured: widening
    the scan from the nine #1400 files to the whole 56-file surface added forty
    previously-unguarded files and **zero** further findings.
    """
    live = set()
    for path in _surface():
        for rel, fn, expr, _line in _offender_keys(path):
            live.add((rel, fn, expr))

    stale = sorted(key for key in _ALLOWED if key not in live)

    assert stale == [], (
        "allowlist entries matching no site — the code changed under them; "
        f"delete them rather than leaving a blanket exemption behind: {stale}"
    )
