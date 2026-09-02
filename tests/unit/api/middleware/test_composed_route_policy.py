"""One declaration, read by both repeat-suppressing middlewares (fm#1303).

fm#1299 gave ``IdempotencyMiddleware`` a way for a composed deployment to
declare exclusions for routes this repository does not serve.
``DeduplicationMiddleware`` has the same shape — a literal ladder of paths in
``_should_skip`` — and was not touched, so the class was only half closed.

That half matters because deduplication is installed *after* idempotency and
therefore sits **further out**: it sees a request first. A composed credential
mint that is excluded from replay but still collapsible is answered ``409``
before the route runs, which blocks the same recovery path through a different
door. Its own docstring already says the current safety is incidental — "safe
today only because both paths the copilot sends a key on are exempt below" — and
that is precisely the guarantee a composed route cannot obtain from a list it
cannot appear in.

**On writing these tests.** A "not a duplicate" or "nothing was cached"
assertion is worth nothing alone: a broken check also returns 200, and a broken
glob also matches nothing. Every exemption case here is paired with the
undeclared control that proves the mechanism was live in the first place.
"""

from contextlib import asynccontextmanager

import fakeredis.aioredis as fakeredis_aio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.api.middleware.deduplication import DeduplicationMiddleware
from faultmaven.api.middleware.route_policy import (
    APP_STATE_POLICY_ATTR,
    LEGACY_IDEMPOTENCY_ATTR,
    RoutePolicy,
    assert_policy_coherent,
    declare_credential_mint,
    declare_route_policy,
)
from faultmaven.config.protection import get_development_protection_settings

pytestmark = [pytest.mark.unit, pytest.mark.security]

BIND = "/api/v1/admin/integrations/slack/workspaces"
BODY = b'{"slack_team_id":"T0123ABCD","team_name":"Acme Platform"}'


def _build_app(declare=None):
    """A composed app: the OSS dedup middleware, a Cloud-shaped mint route."""
    settings = get_development_protection_settings()
    settings.deduplication_enabled = True

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Built here, not at fixture time: fakeredis.aioredis binds its queues to
        # the running loop and TestClient serves on its own. Constructed outside,
        # every Redis call raises and the middleware reads it as "not a duplicate".
        app.state.redis_client = fakeredis_aio.FakeRedis(decode_responses=True)
        yield

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(DeduplicationMiddleware, settings=settings)

    mints = {"n": 0}

    @app.post(BIND)
    async def bind():
        mints["n"] += 1
        return {"refresh_token": f"live-refresh-token-{mints['n']}"}

    @app.post("/api/v1/agent/query")
    async def query():
        return {"ok": True}

    @app.post("/api/v1/cases")
    async def create_case():
        """In the core's literal ladder, and must stay there: it is dedup-exempt
        *because* the copilot retries it with an Idempotency-Key and expects the
        cached replay, which a 409 from further out would pre-empt."""
        return {"case_id": "c-1"}

    app.state.mints = mints
    if declare is not None:
        declare(app)
    return app


def _post(client, path=BIND, session="sess-1"):
    return client.post(
        path,
        content=BODY,
        headers={"X-Session-ID": session, "content-type": "application/json"},
    )


# ---------------------------------------------------------------------------
# The defect, kept as the control for the exemption below.
# ---------------------------------------------------------------------------


def test_an_undeclared_composed_mint_is_collapsed_to_a_409():
    """fm#1303 as filed. The control, not an aspiration.

    If this stops holding, the mechanism under test is unobservable and the
    exemption assertions below hold whether or not anything reads the policy.
    """
    with TestClient(_build_app()) as client:
        first = _post(client)
        second = _post(client)

    assert first.status_code == 200
    assert (
        second.status_code == 409
    ), "dedup must be live for the exemption to mean anything"
    assert second.json()["error_code"] == "DUPLICATE_REQUEST"


def test_a_declared_credential_mint_is_never_collapsed():
    app = _build_app(lambda a: declare_credential_mint(a, BIND))
    with TestClient(app) as client:
        first = _post(client)
        second = _post(client)

    assert first.status_code == 200
    assert second.status_code == 200, "the re-bind must reach the route, not a 409"
    assert app.state.mints["n"] == 2, "the re-bind must mint a fresh credential"
    assert first.json()["refresh_token"] != second.json()["refresh_token"]


def test_the_exemption_does_not_widen_to_other_routes():
    """An exemption silently disables a protection, so it must not spread."""
    app = _build_app(lambda a: declare_credential_mint(a, BIND))
    with TestClient(app) as client:
        assert _post(client, "/api/v1/agent/query").status_code == 200
        assert _post(client, "/api/v1/agent/query").status_code == 409


def test_the_exemption_holds_when_the_request_carries_a_trailing_slash():
    """This middleware sits outside ``TrailingSlashMiddleware`` too."""
    app = _build_app(lambda a: declare_credential_mint(a, BIND))
    with TestClient(app) as client:
        first = _post(client, BIND + "/")
        second = _post(client, BIND + "/")

    assert (first.status_code, second.status_code) == (200, 200)


def test_the_cores_own_exemptions_are_unchanged():
    """The literal ladder still answers for the routes it always did.

    ``/api/v1/cases`` is dedup-exempt *because* it participates in idempotency —
    the asymmetry the two flags exist to preserve. Asserted against a repeat that
    would be collapsed without the exemption, since a single 200 holds on any
    implementation, including one where deduplication stopped working; the
    control above proves it is live.
    """
    app = _build_app()
    with TestClient(app) as client:
        first = _post(client, "/api/v1/cases")
        second = _post(client, "/api/v1/cases")

    assert (first.status_code, second.status_code) == (200, 200)


# ---------------------------------------------------------------------------
# The implication, made structural rather than asserted.
# ---------------------------------------------------------------------------


def test_a_replay_exclusion_implies_a_dedup_exemption():
    """Withholding replay while permitting collapsing is not expressible.

    Dedup sits further out, so that combination is answered 409 before the route
    runs — the same operation blocked by a different door. A state that cannot be
    built beats one that is reported.
    """
    app = _build_app()
    policy = declare_route_policy(app, BIND, never_replayed=True)

    assert policy[BIND] == RoutePolicy(never_replayed=True, never_collapsed=True)


def test_a_dedup_exemption_does_not_imply_a_replay_exclusion():
    """The converse must stay expressible: ``/api/v1/cases`` is exempt from
    deduplication precisely *because* it participates in idempotency, and
    collapsing the two flags into one boolean would erase that case."""
    app = _build_app()
    policy = declare_route_policy(app, BIND, never_collapsed=True)

    assert policy[BIND] == RoutePolicy(never_replayed=False, never_collapsed=True)


def test_declarations_merge_per_path_and_only_ever_add():
    """Composed units declare independently; a later one must not un-withhold."""
    app = _build_app()
    declare_route_policy(app, BIND, never_replayed=True)
    policy = declare_route_policy(app, BIND, never_collapsed=True)

    assert policy[BIND] == RoutePolicy(never_replayed=True, never_collapsed=True)


def test_the_legacy_idempotency_attribute_still_yields_both():
    """fm#1299's attribute shipped naming only replay. A deployment still
    assigning it must not be left half-protected."""
    app = _build_app()
    setattr(app.state, LEGACY_IDEMPOTENCY_ATTR, frozenset({BIND}))

    with TestClient(app) as client:
        first = _post(client)
        second = _post(client)

    assert (first.status_code, second.status_code) == (200, 200)
    assert app.state.mints["n"] == 2


# ---------------------------------------------------------------------------
# The coherence gate, for the one path the constructor cannot reach.
# ---------------------------------------------------------------------------


def test_the_gate_passes_a_coherent_declaration():
    app = _build_app(lambda a: declare_credential_mint(a, BIND))

    assert assert_policy_coherent(app) is None


def test_the_gate_catches_a_hand_assigned_half_declaration():
    """``declare_route_policy`` cannot build this; a direct assignment can."""
    app = _build_app()
    setattr(
        app.state,
        APP_STATE_POLICY_ATTR,
        {BIND: RoutePolicy(never_replayed=True, never_collapsed=False)},
    )

    problem = assert_policy_coherent(app)

    assert problem is not None
    assert BIND in problem
    assert "409" in problem, "the message must name the symptom, not just the rule"


def test_the_gate_is_silent_on_an_undeclared_app():
    assert assert_policy_coherent(_build_app()) is None


def test_the_coherence_gate_is_wired_into_the_lifespan():
    """The function is unit-tested above; this pins that something calls it.

    Asserted against the source rather than by booting the app: the gate sits
    inside the lifespan beside the deployment-coherence and credential gates, and
    exercising it end-to-end would mean standing up the whole composition root
    for one branch.
    """
    import inspect

    import faultmaven.main

    source = inspect.getsource(faultmaven.main)
    assert "assert_policy_coherent" in source
    assert "raise RuntimeError" in source
