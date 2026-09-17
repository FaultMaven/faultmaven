"""The session router, driven with no credential at all.

The gap #1447 §5 names, in one sentence: the repository's adversarial probe
(``test_two_enterprise_surface_probe.py``) asks *can authenticated A reach
authenticated B's data?*, and **both parties hold tokens in every case**. There
was no parametrisation in which a caller presents no credential, so five session
routes that took no auth dependency at all were found by a person reading rather
than by CI.

This module is that missing arm for the surface it was found on, and it is
driven against the session service that made the finding LIVE rather than
theoretical. ``RedisSessionStore.list_sessions()`` is a stub returning ``[]``,
which is what made the enumeration look like a future hazard — but that store is
one of **two** implementations. ``MinimalSessionService``
(``_container_impl._create_minimal_session_service``) ships a working
``list_sessions`` with the ``user_id`` filter, and ``create_session_service``
installs it whenever the real service cannot be constructed — *"Reachable in
PRODUCTION, not only under test"*, in its own docstring. Measured on that path
with no ``Authorization`` header, before the fix:

    GET /api/v1/sessions?user_id=<victim>     -> 200, that user's session id
    GET /api/v1/sessions                      -> 200, every session id + user
    GET /api/v1/sessions/<victim's session>   -> 200, its bound user_id
    DELETE /api/v1/sessions/<victim's session>-> 204, and the session was gone

So the service under test here is chosen, not convenient: a probe run against
the stub would pass on an empty list and prove nothing.

Two batteries, because the routes have two properties and one does not imply the
other. **Anonymous** — a caller with no credential is refused. **Foreign** — an
authenticated caller is refused someone else's session, and the listing answers
with the caller's own sessions whatever the request asked for.

The heartbeat is asserted AS IT BEHAVES, with the issue that tracks it (#1460),
which is the ``_FINDING`` idiom the surface probe defines and had never used. It
is still open on purpose: closing it is coupled to the mint, which cannot gain
auth in this repository alone.
"""

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.api.v1.dependencies import get_session_service
from faultmaven.modules.auth.api.session import router as session_router
from faultmaven.modules.auth.domain.models.auth import DevUser
from tests.utils import minimal_session_service

pytestmark = [pytest.mark.integration, pytest.mark.security]

#: Subjects that name nobody — the three shapes a claim set can carry.
#: ``None`` and ABSENT are different cases in Python and were different
#: cases in the code: one reached the guard, the other raised KeyError.
SUBJECTLESS = ["", "   ", None]

VICTIM = "victim-user-a"
OTHER = "other-user-b"


def _principal(user_id: str) -> DevUser:
    return DevUser(
        user_id=user_id,
        username=user_id,
        email=f"{user_id}@example.com",
        display_name=user_id,
        created_at=datetime.now(timezone.utc),
    )


class _Surface:
    """A mounted session router, its service, and the two minted sessions."""

    def __init__(self, caller: DevUser | None):
        self.service = minimal_session_service()

        app = FastAPI()
        # ``prefix="/api/v1"`` exactly as ``main.py`` mounts it — the router
        # carries its own ``/sessions``. Mounting it at ``/api/v1/sessions``
        # instead doubles the segment and every assertion below quietly stops
        # describing a served route.
        app.include_router(session_router, prefix="/api/v1")

        async def _service():
            return self.service

        app.dependency_overrides[get_session_service] = _service
        if caller is not None:
            # ONLY in the foreign battery. The anonymous battery leaves
            # ``require_authentication`` real, because whether it refuses is the
            # whole question there.
            async def _caller():
                return caller

            app.dependency_overrides[require_authentication] = _caller

        self.app = app
        self.client = TestClient(app, raise_server_exceptions=False)

        self.victim = asyncio.run(self.service.create_session(VICTIM))[0]
        self.other = asyncio.run(self.service.create_session(OTHER))[0]

    def alive(self, session_id: str) -> bool:
        return session_id in self.service.sessions


@pytest.fixture
def anonymous():
    """No credential anywhere: the arm the existing probe never had."""
    return _Surface(caller=None)


@pytest.fixture
def as_blank_principal():
    """Authenticated, and named nobody: the middle layer's own observable.

    A principal with an empty ``user_id`` cannot arrive through the identity
    layer any more (the token test below is what says so), so this fixture
    injects one — which is exactly the state the route guard exists for if some
    future identity path regresses. Each layer is asserted on its own terms
    rather than through the choke point in front of it.
    """
    return _Surface(caller=_principal(""))


@pytest.fixture
def as_victim():
    """Authenticated as the owner of ``victim``, reaching for ``other``."""
    return _Surface(caller=_principal(VICTIM))


# =============================================================================
# The anonymous battery — #1447 §1 and §2
# =============================================================================


def test_the_probe_is_aimed_at_the_service_that_makes_the_listing_work(anonymous):
    """Vacuity control, and the reason this module exists at all.

    The exemption this replaces argued that ``GET /api/v1/sessions`` was
    unprobeable because *"with no store the listing answers the same empty page
    to every caller, so no control exists"*. That is a property of
    ``RedisSessionStore``, not of the route. If this assertion ever fails, the
    battery below has gone back to measuring an empty list and every 401 it
    asserts would hold for a route that returns nothing to anyone.
    """
    assert type(anonymous.service).__name__ == "MinimalSessionService"
    listed = asyncio.run(anonymous.service.list_sessions())
    assert {session.user_id for session in listed} == {VICTIM, OTHER}
    filtered = asyncio.run(anonymous.service.list_sessions(user_id=VICTIM))
    assert [session.session_id for session in filtered] == [anonymous.victim.session_id]


def test_an_anonymous_caller_cannot_list_sessions(anonymous):
    """#1447 §1: the enumeration, both shapes."""
    filtered = anonymous.client.get(f"/api/v1/sessions?user_id={VICTIM}")
    assert filtered.status_code == 401, filtered.text
    assert VICTIM not in filtered.text

    unfiltered = anonymous.client.get("/api/v1/sessions")
    assert unfiltered.status_code == 401, unfiltered.text
    assert VICTIM not in unfiltered.text
    assert OTHER not in unfiltered.text


def test_an_anonymous_caller_cannot_read_a_session(anonymous):
    """#1447 §2: the route that published a session's bound ``user_id``."""
    response = anonymous.client.get(f"/api/v1/sessions/{anonymous.victim.session_id}")
    assert response.status_code == 401, response.text
    assert VICTIM not in response.text


def test_an_anonymous_caller_cannot_delete_a_session(anonymous):
    """#1447 §2: the one with a write effect — refused, and the row survives.

    The row check is the half that matters. A 401 whose handler had already
    deleted the session would be a green test over a destroyed session.
    """
    response = anonymous.client.delete(
        f"/api/v1/sessions/{anonymous.victim.session_id}"
    )
    assert response.status_code == 401, response.text
    assert anonymous.alive(anonymous.victim.session_id)


def test_the_heartbeat_is_still_open_and_that_is_tracked(anonymous):
    """A FINDING, asserted as it behaves so the fix turns this red (#1460).

    ``POST /sessions/{id}/heartbeat`` still admits an anonymous caller, and
    that is a decision rather than the omission its three siblings were: while
    ``POST /api/v1/sessions`` mints anonymously, requiring auth here refuses
    sessions that have no authenticated owner to check against — and a
    signed-in copilot can hold one, because the panel mints at mount and
    sign-in does not re-mint. See the route's docstring for the consumer
    evidence.

    When #1460 closes the mint and the heartbeat, this assertion fails, and its
    failure is the reminder to finish the job here.
    """
    response = anonymous.client.post(
        f"/api/v1/sessions/{anonymous.victim.session_id}/heartbeat"
    )
    assert response.status_code == 200, (
        "the heartbeat now refuses an anonymous caller — #1460 has landed. "
        "Flip this test to assert 401 and drop the two entries from "
        "PUBLIC_OPERATIONS in "
        "tests/integration/api/test_no_unauthenticated_operations.py."
    )


# =============================================================================
# The foreign battery — authenticated, but not as the owner
# =============================================================================


def test_the_listing_answers_with_the_callers_own_sessions(as_victim):
    """The ``user_id`` filter is gone, so naming someone else changes nothing.

    FastAPI ignores an undeclared query parameter, so this is what a client
    still sending the removed filter now gets: its own sessions, which is the
    only answer the route has left.
    """
    own = as_victim.client.get("/api/v1/sessions")
    assert own.status_code == 200, own.text
    assert [row["user_id"] for row in own.json()["sessions"]] == [VICTIM]

    named = as_victim.client.get(f"/api/v1/sessions?user_id={OTHER}")
    assert named.status_code == 200, named.text
    assert [row["user_id"] for row in named.json()["sessions"]] == [VICTIM]
    assert as_victim.other.session_id not in named.text


def test_an_authenticated_caller_cannot_read_a_foreign_session(as_victim):
    """The ownership check the three sibling routes have always had."""
    positive_control = as_victim.client.get(
        f"/api/v1/sessions/{as_victim.victim.session_id}"
    )
    assert positive_control.status_code == 200, positive_control.text

    attack = as_victim.client.get(f"/api/v1/sessions/{as_victim.other.session_id}")
    assert attack.status_code == 403, attack.text
    assert OTHER not in attack.text


def test_an_authenticated_caller_cannot_delete_a_foreign_session(as_victim):
    """Refused, and the other party's session is still there afterwards."""
    attack = as_victim.client.delete(f"/api/v1/sessions/{as_victim.other.session_id}")
    assert attack.status_code == 403, attack.text
    assert as_victim.alive(as_victim.other.session_id)

    # The positive control: the same call on the caller's OWN session works, so
    # the 403 above is the ownership check rather than a route that refuses
    # everybody.
    own = as_victim.client.delete(f"/api/v1/sessions/{as_victim.victim.session_id}")
    assert own.status_code == 204, own.text
    assert not as_victim.alive(as_victim.victim.session_id)


# =============================================================================
# A falsy caller id is not a scope — three layers, three observables (#1447 review)
# =============================================================================
#
# The shape both implementations share is ``if user_id: [filter] else: return
# everything``, so a caller whose id is falsy does not get a narrower answer,
# it gets the WHOLE TABLE — session ids and the identity each is bound to,
# which is the §1 payload behind a 200 rather than an anonymous call.
#
# Three layers fix it, and each is asserted where it lives, because a choke
# point in front of a guard makes that guard untestable through the front door
# and therefore unmaintained:
#
#   1. the identity layer refuses a token whose ``sub`` names no subject;
#   2. the route refuses a principal whose id is blank anyway;
#   3. the services treat a SUPPLIED filter literally, so a blank one matches
#      nothing rather than skipping the filter.


def test_layer_three_a_supplied_blank_filter_matches_nothing(anonymous):
    """The service, called directly. Both implementations, one shape.

    ``None`` still means "no filter, all sessions" — that is the maintenance
    API and several callers depend on it. What changes is that a filter which
    was SUPPLIED is honoured, so ``""`` selects the sessions owned by ``""``,
    of which there are none.
    """
    service = anonymous.service

    everything = asyncio.run(service.list_sessions())
    assert {session.user_id for session in everything} == {VICTIM, OTHER}, (
        "the positive control failed: the service under test holds no sessions, "
        "so 'a blank filter returns none' would hold for a broken service too"
    )

    assert asyncio.run(service.list_sessions(user_id="")) == []
    assert [s.user_id for s in asyncio.run(service.list_sessions(user_id=VICTIM))] == [
        VICTIM
    ]


def test_layer_three_holds_for_the_real_service_too(anonymous):
    """``AuthSessionService`` shares the shape, so it gets the same assertion.

    Driven against a stub store rather than Redis: the only thing under test is
    the filter branch, and wiring a real store would test the store.
    """
    from faultmaven.modules.auth.domain.services.auth_session_service import (
        AuthSessionService,
    )

    class _Store:
        async def list_sessions(self):
            return list(anonymous.service.sessions.values())

    service = AuthSessionService(session_store=_Store())

    assert {s.user_id for s in asyncio.run(service.list_sessions())} == {VICTIM, OTHER}
    assert asyncio.run(service.list_sessions(user_id="")) == []


def test_layer_two_the_route_refuses_a_principal_with_no_subject(as_blank_principal):
    """The route, with the identity layer bypassed by an override.

    401 and not 200-with-nothing: a credential that names nobody is not a
    narrower caller, it is an unauthenticated one, and the body is byte-identical
    to ``require_authentication``'s so the distinction is not published.
    """
    listing = as_blank_principal.client.get("/api/v1/sessions")

    assert listing.status_code == 401, listing.text
    assert VICTIM not in listing.text
    assert OTHER not in listing.text

    search = as_blank_principal.client.post("/api/v1/sessions/search", json={})
    assert search.status_code == 401, search.text


def test_layer_one_a_token_naming_no_subject_authenticates_nobody(caplog):
    """The identity layer, called directly with the claims a real token carries.

    Direct rather than over HTTP, because over HTTP this layer sits behind
    ``require_authentication`` and in front of the route guard, and a 401 there
    would not say WHICH of the three produced it. What is under test is one
    function's answer to one claim set.

    The measurement that makes this necessary, re-asserted here as the control:
    PyJWT's ``require`` asserts a claim is PRESENT, not that it says anything.
    ``sub: ""`` decodes clean; an absent ``sub`` does not. If the first ever
    stops being true this guard is moot and should be deleted rather than left
    passing for the wrong reason.
    """
    import time

    import jwt

    from faultmaven.api.v1.auth_dependencies import get_current_user_optional

    secret = "probe-secret-not-a-real-key-0123456789"
    now = int(time.time())
    required = ["sub", "iss", "aud", "exp", "iat", "jti", "type"]

    def claims(sub):
        body = {
            "iss": "faultmaven",
            "aud": "faultmaven-api",
            "exp": now + 600,
            "iat": now,
            "jti": "probe-jti",
            "type": "access",
        }
        if sub is not None:
            body["sub"] = sub
        return body

    def decode(sub):
        return jwt.decode(
            jwt.encode(claims(sub), secret, algorithm="HS256"),
            secret,
            algorithms=["HS256"],
            issuer="faultmaven",
            audience="faultmaven-api",
            options={"require": required},
        )

    assert decode("")["sub"] == "", (
        "PyJWT no longer admits an empty `sub`, so this guard now passes for a "
        "reason that has nothing to do with the code it guards"
    )
    with pytest.raises(jwt.MissingRequiredClaimError):
        decode(None)

    class _AuthService:
        """Verifies nothing; the claim set is the input under test."""

        def __init__(self, verified):
            self._verified = verified

        async def verify_token_with_revocation_check(self, token, token_type):
            return self._verified

    async def resolve(sub):
        return await get_current_user_optional(
            request=SimpleNamespace(),
            token="a.b.c",
            auth_service=_AuthService(claims(sub)),
        )

    # The positive control: a token that names somebody still authenticates,
    # so the refusals below are about the subject and not about the stub.
    assert asyncio.run(resolve("somebody")).user_id == "somebody"

    # THROUGH THE GUARD, not through the catch-all, and that distinction is why
    # this is asserted on the log rather than only on the return value. All
    # three shapes return None either way — an ABSENT ``sub`` used to raise
    # KeyError from ``claims["sub"]`` and land in the function's bottom
    # ``except Exception``, which also returns None. So this test was green
    # while the check it names was unreachable for that case. The guard logs at
    # DEBUG, the catch-all at WARNING with a correlation id; asserting no
    # WARNING is what tells them apart (#1447 review).
    for subject in SUBJECTLESS:
        with caplog.at_level(
            logging.DEBUG, logger="faultmaven.api.v1.auth_dependencies"
        ):
            caplog.clear()
            assert asyncio.run(resolve(subject)) is None, f"sub={subject!r}"

        messages = [record.getMessage() for record in caplog.records]
        assert any(
            "names no subject" in message for message in messages
        ), f"sub={subject!r} did not reach the guard: {messages}"
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
            f"sub={subject!r} took the catch-all rather than the guard: "
            f"{[r.getMessage() for r in caplog.records]}"
        )


# =============================================================================
# The refusal must precede the service (#1447 review)
# =============================================================================


def test_the_refusal_precedes_service_resolution():
    """An anonymous caller is refused before anything reads app.state.

    FastAPI solves a dependant's dependencies in DECLARATION ORDER, and every
    route on this router declared ``session_service`` before ``current_user``.
    ``get_session_service`` is a bare ``request.app.state.session_service``, so
    on a deployment whose Composition Root did not populate that slot the
    service raised first and an unauthenticated caller got the 500 — a pre-auth
    availability oracle on exactly the routes #1447 closes. Measured before the
    fix: ``GET /api/v1/sessions`` with no Authorization answered
    ``500 Internal Server Error``.

    The app here has NO ``session_service`` at all, which is what makes this a
    measurement rather than a restatement of the anonymous battery above: if the
    gate ran after the service, every row would be 500.

    Route-level ``dependencies=[...]`` is what makes the ordering
    unconditional — a handler that declares ``current_user`` first works too,
    but is one careless parameter edit from regressing, and nothing else would
    catch it. This is that something.
    """
    app = FastAPI()
    app.include_router(session_router, prefix="/api/v1")
    # Deliberately NOT set: app.state.session_service
    client = TestClient(app, raise_server_exceptions=False)

    refused = {
        ("GET", "/api/v1/sessions"): None,
        ("GET", "/api/v1/sessions/any-id"): None,
        ("DELETE", "/api/v1/sessions/any-id"): None,
        ("POST", "/api/v1/sessions/search"): {},
        ("PUT", "/api/v1/sessions/any-id"): {},
        ("POST", "/api/v1/sessions/any-id/archive"): None,
    }
    answered = {}
    for (method, path), payload in refused.items():
        response = client.request(
            method, path, **({"json": payload} if payload is not None else {})
        )
        answered[f"{method} {path}"] = response.status_code

    assert set(answered.values()) == {401}, (
        "a session route answered something other than 401 to an anonymous "
        "caller on a service-less app — the auth gate is being solved after "
        f"the service dependency: {answered}"
    )


def test_the_heartbeat_is_the_only_ungated_route_on_this_router():
    """And the exception is named, not left as whatever the file happens to do.

    The list above is hand-written, so it is worth asking the router what it
    actually serves: a new session route added without a gate would not appear
    in that dict and nothing would notice. This closes that by walking the
    router instead of a list.
    """
    app = FastAPI()
    app.include_router(session_router, prefix="/api/v1")

    ungated = sorted(
        f"{method} {route.path}"
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
        if method not in {"HEAD", "OPTIONS"}
        and not any(
            getattr(dependency.call, "__name__", "") == "require_authentication"
            for dependency in route.dependant.dependencies
        )
    )

    assert ungated == [
        "POST /api/v1/sessions",
        "POST /api/v1/sessions/{session_id}/heartbeat",
    ], (
        "the set of session routes reachable without authentication changed. "
        "Both entries are deliberate and tracked by #1460 (the mint, and the "
        f"heartbeat coupled to it); anything else is not: {ungated}"
    )
