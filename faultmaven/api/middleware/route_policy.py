"""What a composed deployment may declare about a route it serves.

Two middlewares withhold a route from a repeat-suppressing mechanism, and both
expressed that as a literal list owned by *this package*:
``IdempotencyMiddleware.EXCLUDED_EXACT_PATHS`` and
``DeduplicationMiddleware._should_skip``. The served route table is larger than
this package — ``faultmaven-cloud`` mounts its routers onto the same ``app``
singleton — so neither list can name a composed route. fm#1299 closed that for
idempotency; fm#1303 is the same gap in deduplication.

Rather than a second, independently-shaped attribute, the composition root makes
**one** declaration here and each middleware reads what it needs from it. The
declaration is a property of the *route* ("this response is a credential, so the
operation must genuinely re-run"), not of whichever middleware happens to ask.

Why the two flags are not one
-----------------------------
They are genuinely different questions, and the core's own lists prove it:
``POST /api/v1/cases`` is exempt from deduplication *because* it participates in
idempotency — the copilot retries it with a stable ``Idempotency-Key`` and
expects the cached replay, which a dedup 409 would pre-empt. So
"not collapsed" does not imply "not replayed", and collapsing them into a single
boolean would make that case inexpressible.

Why the implication is structural rather than asserted
------------------------------------------------------
The other direction *does* hold, and is enforced in ``declare_route_policy``
instead of being left for a startup check to catch. Deduplication is installed
after idempotency and therefore sits **further out**, so it sees a request first:
a route whose response must never be replayed but which dedup may still collapse
gets a ``409`` on the retry, which is the same operation blocked by a different
door. There is no route for which the withheld-replay reason (the response is a
credential; the caller needs a fresh one) stops applying to the collapse. Making
a half-declaration impossible to express beats reporting it — a guard is only as
good as the run that reaches it.

``assert_policy_coherent`` therefore exists for what the constructor cannot
reach: a declaration assigned straight onto ``app.state`` by hand, which bypasses
the helper and can express exactly the half this module refuses to build.
"""

import logging
from collections.abc import Collection
from dataclasses import dataclass, replace
from typing import Dict, Mapping, Optional

logger = logging.getLogger(__name__)

#: Where the declaration lives. Read off ``app.state`` — the channel the auth
#: service, the tenant services and the Redis client already travel on — because
#: ``BaseHTTPMiddleware.dispatch`` runs before routing, so no matched route is in
#: scope at the point either middleware decides.
APP_STATE_POLICY_ATTR = "route_middleware_policy"

#: Retained from fm#1299, which shipped it as the idempotency-only seam. A
#: deployment that assigned it directly still works; ``policy_for`` folds it in.
LEGACY_IDEMPOTENCY_ATTR = "idempotency_excluded_paths"


@dataclass(frozen=True)
class RoutePolicy:
    """What repeat-suppression a route is withheld from.

    Both default to False: an undeclared route participates in everything, which
    is what every route in this repository does today.
    """

    #: ``IdempotencyMiddleware`` must not cache this response or serve it again.
    #: Replaying a token mint is not idempotency — it hands one caller's
    #: credential to whoever presents the key next.
    never_replayed: bool = False

    #: ``DeduplicationMiddleware`` must not answer a repeat of this request with
    #: a 409. The repeat is the point: re-running is how a caller recovers a
    #: credential it lost, so collapsing it returns a conflict where the caller
    #: needed the operation to happen.
    never_collapsed: bool = False


def normalize_path(path: str) -> str:
    """Reduce a path to the form policy is compared in.

    One function across both middlewares and the declaration, so a declared path
    and an incoming request path can never be normalised by two different rules.
    A silent non-match here is an open hole rather than a visible failure.

    Both middlewares are installed *outside* ``TrailingSlashMiddleware``, so they
    see the path as the client sent it — ``/x/`` must match a declaration of
    ``/x`` or the exclusion is skipped by a spelling.
    """
    return path.rstrip("/") or "/"


def _post_route_paths(routes) -> frozenset:
    """Every path in a route table that answers POST, including nested ones.

    Nested tables are reached through ``route.routes``, never ``route.app``: a
    ``Mount`` constructed with ``middleware=`` exposes the *wrapper* on ``.app``,
    and the wrapper has no ``routes`` at all — measured on starlette 1.3.1,
    ``.app`` yields nothing where ``.routes`` yields the five real routes.

    Duck-typed rather than isinstance-checked because ``Host`` carries ``routes``
    but no ``path`` (host routing contributes no prefix), and a container this
    function has not heard of should degrade to "walk it if it has routes"
    rather than to a refusal — a refusal a composer learns to route around is
    worse than no check.
    """
    found: set = set()
    for route in routes:
        nested = getattr(route, "routes", None)
        if nested:
            prefix = (getattr(route, "path", "") or "").rstrip("/")
            for path in _post_route_paths(nested):
                found.add(normalize_path(prefix + path))
        elif "POST" in (getattr(route, "methods", None) or ()):
            found.add(normalize_path(route.path))
    return frozenset(found)


def declare_route_policy(
    app,
    *paths: str,
    never_replayed: bool = False,
    never_collapsed: bool = False,
) -> Mapping[str, RoutePolicy]:
    """Declare how the repeat-suppressing middlewares must treat these routes.

    For composition roots: call it after the routers are mounted, and build each
    path from the router that serves it rather than retyping a literal, so the
    declaration cannot drift away from the route it protects.

    Every path is checked against ``app``'s real route table, because the failure
    mode this guards against is silent in both directions. A declaration that
    matches nothing does not fail — it simply leaves the route unprotected, which
    looks exactly like a working exclusion from the outside. So a path that names
    no POST route is a ``ValueError`` at composition time, not a hole discovered
    in production. Templated paths are refused for the same reason:
    ``/orgs/{org_id}/tokens`` can never equal the concrete path a request
    carries.

    ``never_replayed`` implies ``never_collapsed``. Deduplication sits further
    out and would answer the retry with a 409, blocking the same operation
    through a different door — so the half-declaration is not built rather than
    reported. See the module docstring.

    Declarations accumulate and merge per path, so several composed units can
    each declare their own without knowing about each other, and a later
    declaration can only add withholdings, never remove one.

    Returns the resulting policy map, so a caller (or a test) can assert what
    took effect rather than trusting that it did.
    """
    if never_replayed:
        never_collapsed = True

    served = _post_route_paths(app.routes)
    declared = RoutePolicy(
        never_replayed=never_replayed, never_collapsed=never_collapsed
    )

    existing = dict(_read_policy_map(getattr(app.state, APP_STATE_POLICY_ATTR, None)))
    for path in paths:
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError(
                f"route policy must name an absolute path string, got {path!r}"
            )
        candidate = normalize_path(path)
        if "{" in candidate:
            raise ValueError(
                f"route policy {path!r} is templated; an exact-path declaration "
                "can never equal the concrete path a request carries"
            )
        if candidate not in served:
            raise ValueError(
                f"route policy {path!r} names no POST route on this app. Declare "
                "it after the router is mounted, and derive it from the router "
                "rather than retyping the path."
            )
        current = existing.get(candidate, RoutePolicy())
        existing[candidate] = replace(
            current,
            never_replayed=current.never_replayed or declared.never_replayed,
            never_collapsed=current.never_collapsed or declared.never_collapsed,
        )

    combined = _PolicyMap(existing)
    setattr(app.state, APP_STATE_POLICY_ATTR, combined)
    logger.info(
        "Route middleware policy declared by the composition root: %s",
        {path: vars(policy) for path, policy in sorted(combined.items())},
    )
    return combined


def declare_credential_mint(app, *paths: str) -> Mapping[str, RoutePolicy]:
    """Declare that these routes return a credential in their response body.

    The named case, and the one every composed caller wants: the response must
    never be replayed *and* a repeat must never be collapsed, both because the
    operation has to genuinely re-run. Prefer this over spelling the flags —
    it records the reason, which is what a future reader needs in order to decide
    whether the declaration still applies.
    """
    return declare_route_policy(app, *paths, never_replayed=True)


class _PolicyMap(Dict[str, RoutePolicy]):
    """A policy map this module built and has already normalised.

    Marker only. It lets the per-request read hand the map back directly instead
    of rebuilding it, while a value assigned to ``app.state`` by hand still takes
    the defensive read below.
    """


#: Composition-time mistakes already reported. The policy is read on every POST,
#: so an unconditional log would emit a line per request for the life of the
#: process — flooding the structured log and the error-pattern detection behind
#: ``GET /health/patterns`` with a static condition. Reporting is per-process
#: because the mistake is.
_REPORTED_BAD_DECLARATIONS: set = set()


def _report_once(key: str, message: str, *args) -> None:
    if key in _REPORTED_BAD_DECLARATIONS:
        return
    _REPORTED_BAD_DECLARATIONS.add(key)
    logger.error(message, *args)


def _read_policy_map(declared) -> Mapping[str, RoutePolicy]:
    """Read a policy declaration defensively into a normalised map.

    ``app.state`` is assignable by hand, so everything this refuses, it refuses
    *loudly*. The failure mode this whole mechanism exists to prevent is a
    declaration that looks like it closed a hole and did not, so silently
    yielding an empty map would reproduce fm#1299 with a declaration sitting in
    the source.

    Anything unreadable is reported and ignored rather than raised: this runs
    outside each middleware's error handling, and a broken declaration must not
    take every POST with it.
    """
    if isinstance(declared, _PolicyMap):
        return declared
    if declared is None:
        return {}
    if not isinstance(declared, Mapping):
        _report_once(
            f"policy-type:{type(declared).__name__}",
            "app.state.%s is %s, not a mapping of path -> RoutePolicy; "
            "route policy ignored",
            APP_STATE_POLICY_ATTR,
            type(declared).__name__,
        )
        return {}
    try:
        items = list(declared.items())
    except Exception as exc:  # a declaration must never break the request path
        _report_once(
            f"policy-iter:{type(declared).__name__}",
            "app.state.%s could not be read (%s: %s); route policy ignored",
            APP_STATE_POLICY_ATTR,
            type(exc).__name__,
            exc,
        )
        return {}

    usable: Dict[str, RoutePolicy] = {}
    unusable = []
    for path, policy in items:
        if isinstance(path, str) and isinstance(policy, RoutePolicy):
            usable[normalize_path(path)] = policy
        else:
            unusable.append((path, policy))
    if unusable:
        _report_once(
            f"policy-entries:{sorted(type(p).__name__ for p, _ in unusable)}",
            "app.state.%s has %d entr(y/ies) that are not (path, RoutePolicy) "
            "pairs and cannot match any request: %r; those are NOT in effect",
            APP_STATE_POLICY_ATTR,
            len(unusable),
            unusable,
        )
    return usable


def _read_legacy_exclusions(declared) -> frozenset:
    """Fold fm#1299's idempotency-only attribute into the policy.

    That seam shipped one release before this module and named only replay, so a
    deployment still assigning it gets ``never_replayed`` — and, by the same
    implication ``declare_route_policy`` enforces, ``never_collapsed`` with it.
    A lone string is read as the one path it means: the value is compared with
    ``in``, and reading a ``str`` as an iterable of characters would turn the
    exact comparison into substring containment.
    """
    if declared is None:
        return frozenset()
    if isinstance(declared, str):
        declared = (declared,)
    if not isinstance(declared, Collection):
        _report_once(
            f"legacy-type:{type(declared).__name__}",
            "app.state.%s is %s, which cannot be read on every request "
            "(a set, list or tuple is required); those exclusions are ignored",
            LEGACY_IDEMPOTENCY_ATTR,
            type(declared).__name__,
        )
        return frozenset()
    try:
        entries = list(declared)
    except Exception as exc:
        _report_once(
            f"legacy-iter:{type(declared).__name__}",
            "app.state.%s could not be read (%s: %s); those exclusions are ignored",
            LEGACY_IDEMPOTENCY_ATTR,
            type(exc).__name__,
            exc,
        )
        return frozenset()
    unusable = [entry for entry in entries if not isinstance(entry, str)]
    if unusable:
        _report_once(
            f"legacy-entries:{sorted(type(e).__name__ for e in unusable)}",
            "app.state.%s contains %d entr(y/ies) that are not path strings and "
            "cannot match any request path: %s; those are NOT in effect",
            LEGACY_IDEMPOTENCY_ATTR,
            len(unusable),
            [repr(entry) for entry in unusable],
        )
    return frozenset(normalize_path(e) for e in entries if isinstance(e, str))


def policy_for(request) -> Mapping[str, RoutePolicy]:
    """The declared policy for the app serving this request.

    The app is read off the raw scope rather than through ``request.app``, which
    raises ``KeyError`` when the key is absent: both call sites decide before
    entering their own error handling, so an app-less scope would 500 every POST
    instead of degrading to an ordinary uncollapsed, uncached request.
    """
    app = request.scope.get("app")
    state = getattr(app, "state", None)
    policy = _read_policy_map(getattr(state, APP_STATE_POLICY_ATTR, None))
    legacy = _read_legacy_exclusions(getattr(state, LEGACY_IDEMPOTENCY_ATTR, None))
    if not legacy:
        return policy

    merged = dict(policy)
    for path in legacy:
        current = merged.get(path, RoutePolicy())
        merged[path] = replace(current, never_replayed=True, never_collapsed=True)
    return merged


def assert_policy_coherent(app) -> Optional[str]:
    """Refuse a declaration that withholds replay but permits collapsing.

    ``declare_route_policy`` cannot produce that state — it applies the
    implication itself — so this exists for the one path that bypasses it: a
    value assigned straight onto ``app.state``. The failure it catches is a
    ``409`` on a credential-recovery path, raised by the middleware *further
    out* than the one the declaration named, which is close to undiagnosable
    from the outside: the operator sees a conflict on an operation whose whole
    purpose is to be repeatable.

    Returns the problem as a string rather than raising, so the caller decides
    whether this deployment should refuse to boot. Returns ``None`` when
    coherent.
    """
    policy = _read_policy_map(getattr(app.state, APP_STATE_POLICY_ATTR, None))
    incoherent = sorted(
        path
        for path, entry in policy.items()
        if entry.never_replayed and not entry.never_collapsed
    )
    if not incoherent:
        return None
    return (
        f"Route policy withholds idempotent replay but permits deduplication "
        f"for: {incoherent}. Deduplication is installed further out, so a repeat "
        f"of one of these is answered 409 before the route runs — blocking the "
        f"very retry the replay exclusion exists to allow. Declare these with "
        f"declare_credential_mint(), which applies both."
    )
