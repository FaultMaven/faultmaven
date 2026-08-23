"""Adversarial probe: attack the tenant boundary at the request layer.

Multi-tenant isolation in FaultMaven is a three-link chain:

1. a token is minted carrying an ``organization_id`` claim;
2. ``bind_request_org_context`` — a FastAPI **global dependency**, so it runs for
   every route — verifies that token and binds the claim to the tenant contextvar;
3. the engine ``begin`` listener applies that contextvar as ``app.current_org_id``,
   and the PostgreSQL RLS policies (migration 018) scope reads to it.

Link 3 is exercised adversarially by ``tests/integration/test_rls_tenant_isolation.py``,
which queries as a deliberately non-superuser, non-owner role because a superuser
bypasses RLS and would prove nothing. Link 2's *ordering and cross-request*
properties are exercised by ``tests/integration/api/test_tenant_scope_request_isolation.py``.

**This module attacks link 2's input**: the claim itself, and everything a
caller controls about it. Its question is not "does the binder work when handed a
good token" — that is covered — but "what can a caller hand it, and does any of
that move the boundary". So every test here is written from the attacker's side:
each one is a thing someone would actually try, and the assertion is that it
does not work.

Why this exists at all
----------------------
Every link is tested; the *composition* was not. The failure this guards is the
one that survives unit tests by construction — a token the app accepts, carrying
an org it should not have, reaching an endpoint through a path nobody enumerated.
Multi-tenant isolation is the gate on opening beta, and a gate that has only ever
been exercised by its own happy path is not evidence.

Shown to fail against a broken boundary
---------------------------------------
A probe that has only ever been green is indistinguishable from one that asserts
nothing, so each guard here was checked against a deliberate break of the code it
watches (``bind_request_org_context``, reverted after each run):

======================================================  ====================================
Mutation                                                Caught by
======================================================  ====================================
honour a caller-supplied ``X-Organization-Id``          the two Attack-2 cases
skip the revocation check on a verified token           ``test_a_revoked_token_binds_no_tenant``
fall through to the Standalone org instead of 403-ing   the three Attack-3 cases
bind the claim without verifying the signature          eight of the nine Attack-1 cases
======================================================  ====================================

The one Attack-1 case the last mutation does not catch is ``empty-bearer``: an
empty token is refused before any decode, so trusting an unverified claim cannot
make it bind. Left in the battery anyway — it costs nothing and covers the
adjacent slip of treating an empty ``Authorization: Bearer`` as absent-but-fine
in some future rewrite of the extraction.

Re-run those mutations rather than trusting this table if you change the binder.

What "does not work" means here
-------------------------------
The binder is deliberately quiet about bad tokens: an unauthenticated, invalid,
or revoked request binds the **non-org sentinel** (``""``) and lets the
endpoint's own auth dependency answer 401. That sentinel matches no org-owned
rows and — unlike the contextvar's Standalone default — can never satisfy the
platform-tier global-WRITE arm of the ``knowledge_items`` policies (migration
033, #770). So "the attack failed" reads as *bound to the sentinel*, and the
assertions say so explicitly rather than merely checking the attacker's target
org is absent: binding some **third** org would also satisfy "not org B", and
would be a worse bug than the one being probed.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import jwt
import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from faultmaven.api.middleware.auth import get_auth_service
from faultmaven.api.middleware.tenant_scope import bind_request_org_context
from faultmaven.config.constants import STANDALONE_ORG_ID
from faultmaven.config.tenant_context import get_current_org_id, set_current_org_id
from faultmaven.modules.auth.domain.services.auth_service import AuthService
from faultmaven.providers.tenancy import factory as tenancy_factory
from faultmaven.providers.tenancy.factory import BUILTIN_MULTI
from tests.utils import InMemoryRevocationStore

pytestmark = [pytest.mark.integration, pytest.mark.security]

ORG_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"  # the caller's own tenant
ORG_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"  # the tenant being attacked
USER_A = "11111111-1111-1111-1111-111111111111"

ISSUER = "faultmaven"
AUDIENCE = "faultmaven-api"
SECRET = "probe-secret-not-a-real-key-padded-to-32-bytes"
#: What an attacker who has *not* compromised the signing key would use.
WRONG_SECRET = "attacker-secret-also-padded-out-to-32-bytes-ok"

#: The binder's "no tenant" binding. Named rather than inlined because every
#: negative assertion in this file is really an assertion about this value:
#: anything else — including a different real org — is a finding.
UNSCOPED = ""


# =============================================================================
# Real crypto, real verification, real ASGI stack
# =============================================================================


def _settings():
    """Settings as an ``AUTH_MODE=local`` deployment declares them.

    HS256 is the arm most of this file attacks because a symmetric key makes the
    interesting forgeries expressible: sign with the wrong key, strip the
    algorithm, splice a payload. The RS256 arm appears once, for the one attack
    that only exists with asymmetric keys (algorithm confusion).
    """
    return SimpleNamespace(
        auth=SimpleNamespace(
            auth_mode="local",
            jwt_refresh_token_expire_days=7,
            jwt_access_token_expire_minutes=60,
        ),
        security=SimpleNamespace(
            jwt_algorithm="HS256",
            jwt_issuer=ISSUER,
            jwt_audience=AUDIENCE,
            token_revocation_prefix="revoked:token:",
            # No RSA pair configured, which is what an AUTH_MODE=local
            # deployment looks like. `_load_keys` refuses a *half*-configured
            # pair, so both halves and both paths must be absent together.
            jwt_private_key=None,
            jwt_public_key=None,
            jwt_private_key_path=None,
            jwt_public_key_path=None,
            jwt_secret_key=SimpleNamespace(get_secret_value=lambda: SECRET),
        ),
    )


@pytest.fixture
def revocation_store():
    return InMemoryRevocationStore()


@pytest.fixture
def auth_service(revocation_store):
    """The real ``AuthService`` — real signature, issuer, audience and type checks.

    Not a double. A double would let this file assert what it already believes
    about verification, which is exactly the assumption a probe is supposed to
    test.
    """
    with patch(
        "faultmaven.modules.auth.domain.services.auth_service.get_settings",
        return_value=_settings(),
    ):
        return AuthService(revocation_store=revocation_store)


@pytest.fixture(autouse=True)
def multi_tenant(monkeypatch):
    """Force ``TENANT_PROVIDER=multi`` for the whole module.

    Patched at ``factory.requested_tenant_provider`` — the single attribute both
    the binder's arm selection and ``usable_tenant_id``'s sentinel rule resolve
    through, so the two cannot disagree under test. Patching the binder's own
    name would move only the arm and leave the sentinel rule reading the real
    configuration.
    """
    monkeypatch.setattr(
        tenancy_factory, "requested_tenant_provider", lambda: BUILTIN_MULTI
    )


@pytest.fixture(autouse=True)
def _reset_org_context():
    set_current_org_id(STANDALONE_ORG_ID)
    yield
    set_current_org_id(STANDALONE_ORG_ID)


def _mint(
    *,
    organization_id: str | None = ORG_A,
    user_id: str = USER_A,
    secret: str = SECRET,
    token_type: str = "access",
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    issued_at: datetime | None = None,
    expires_in: timedelta = timedelta(minutes=15),
) -> str:
    """Sign a token by hand, so any claim can be made without a minting path.

    Deliberately not routed through ``IJWTTokenGenerator``: the generators
    already refuse to mint an org-less or sentinel-valued claim under
    multi-tenant (#629), so going through them could not produce the tokens this
    file needs. The binder's own guard is supposed to hold *independently of
    every minting path, present and future* — which is only testable by
    constructing what a future path might emit.
    """
    now = issued_at or datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "username": "probe-user",
        "email": "probe@example.com",
        "roles": ["user"],
        "exp": now + expires_in,
        "iat": now,
        "iss": issuer,
        "aud": audience,
        "jti": f"jti-{user_id}-{organization_id}-{now.timestamp()}",
        "type": token_type,
        "auth_mode": "local",
    }
    if organization_id is not None:
        payload["organization_id"] = organization_id
    return jwt.encode(payload, secret, algorithm="HS256")


def _probe_app(auth_service) -> FastAPI:
    """An app wired exactly like ``main.py``: the binder as a global dependency.

    A ``BaseHTTPMiddleware`` would not do — Starlette runs its downstream app in
    a separate task, so a contextvar it set would not reach the endpoint. The
    real app's registration is asserted separately, below.
    """
    app = FastAPI(dependencies=[Depends(bind_request_org_context)])

    @app.get("/probe")
    async def probe():
        """Report the tenant this request was bound to — the thing under attack."""
        return {"org": get_current_org_id()}

    @app.post("/probe")
    async def probe_post(body: dict | None = None):
        return {"org": get_current_org_id()}

    @app.get("/probe-slow")
    async def probe_slow():
        # Long enough for other requests' binders to run in between this
        # request's binder and its read.
        await asyncio.sleep(0.05)
        return {"org": get_current_org_id()}

    app.dependency_overrides[get_auth_service] = lambda: auth_service
    return app


@pytest.fixture
def client(auth_service):
    app = _probe_app(auth_service)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://probe")


async def _bound_org(client, token: str | None = None, **kwargs) -> tuple[int, str]:
    """Drive one real request; return (status, the org it bound)."""
    headers = kwargs.pop("headers", {})
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    async with client:
        response = await client.get("/probe", headers=headers, **kwargs)
    return response.status_code, response.json().get("org", "<no-org-key>")


# =============================================================================
# Attack 1 — forge the organization claim
# =============================================================================
#
# The claim is the whole boundary: whatever it says, the request reads. So the
# first question is whether a caller holding a legitimate token for ORG_A can
# make it say ORG_B. Each variant below is a real technique, and the sentinel
# assertion is what proves the attack failed rather than landing somewhere else.


def _alg_none(organization_id: str) -> str:
    """The classic: declare ``alg: none`` and omit the signature entirely.

    Assembled by hand because a JWT library will not emit this without being
    asked twice — which is the point. A verifier that honours the token's own
    ``alg`` header rather than the one it configured accepts it.
    """

    def seg(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    now = datetime.now(timezone.utc)
    header = seg({"alg": "none", "typ": "JWT"})
    payload = seg(
        {
            "sub": USER_A,
            "organization_id": organization_id,
            "roles": ["user"],
            "exp": int((now + timedelta(minutes=15)).timestamp()),
            "iat": int(now.timestamp()),
            "iss": ISSUER,
            "aud": AUDIENCE,
            "jti": "forged",
            "type": "access",
        }
    )
    return f"{header}.{payload}."


def _spliced_payload(organization_id: str) -> str:
    """Keep a genuine token's header and signature; swap the payload for ORG_B.

    What an attacker with a valid token of their own actually does first, and
    the reason signature verification must cover the payload rather than a
    digest computed after decoding.
    """
    genuine = _mint(organization_id=ORG_A)
    header, _payload, signature = genuine.split(".")
    claims = jwt.decode(
        genuine, SECRET, algorithms=["HS256"], audience=AUDIENCE, issuer=ISSUER
    )
    claims["organization_id"] = organization_id
    raw = json.dumps(claims, separators=(",", ":"), default=str).encode()
    forged = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return f"{header}.{forged}.{signature}"


FORGERIES = [
    pytest.param(lambda: _alg_none(ORG_B), id="alg-none"),
    pytest.param(lambda: _spliced_payload(ORG_B), id="payload-spliced"),
    pytest.param(
        lambda: _mint(organization_id=ORG_B, secret=WRONG_SECRET),
        id="signed-with-wrong-key",
    ),
    pytest.param(
        lambda: _mint(organization_id=ORG_B, token_type="refresh"),
        id="refresh-token-as-access",
    ),
    pytest.param(
        lambda: _mint(organization_id=ORG_B, issuer="some-other-deployment"),
        id="wrong-issuer",
    ),
    pytest.param(
        lambda: _mint(organization_id=ORG_B, audience="some-other-audience"),
        id="wrong-audience",
    ),
    pytest.param(
        lambda: _mint(
            organization_id=ORG_B,
            issued_at=datetime.now(timezone.utc) - timedelta(hours=2),
            expires_in=timedelta(minutes=15),
        ),
        id="expired",
    ),
    pytest.param(lambda: "not-a-jwt-at-all", id="garbage"),
    pytest.param(lambda: "", id="empty-bearer"),
]


@pytest.mark.parametrize("forge", FORGERIES)
async def test_a_forged_org_claim_binds_no_tenant(client, forge):
    """No forgery may put the request inside ORG_B — or inside anything.

    The second half matters as much as the first: an attack that bound some
    other real organization would still be a cross-tenant breach, so this
    asserts the exact sentinel rather than merely `!= ORG_B`.
    """
    status, org = await _bound_org(client, forge())

    assert org != ORG_B, "a forged claim reached the tenant contextvar"
    assert org != ORG_A
    assert org == UNSCOPED, (
        f"an unverifiable token bound {org!r} instead of the non-org sentinel; "
        "anything other than the sentinel is a tenant this request may act in"
    )
    # The binder does not answer 401 itself — it defers to the endpoint's own
    # auth dependency. The probe endpoint has none, so a bad token reaches it
    # unscoped rather than refused. Pinned so a future reader does not read this
    # 200 as "the token was accepted".
    assert status == 200


async def test_a_genuine_token_still_binds_its_own_org(client):
    """The control. Without it every assertion above passes on a broken binder."""
    status, org = await _bound_org(client, _mint(organization_id=ORG_A))

    assert status == 200
    assert org == ORG_A


# =============================================================================
# Attack 2 — supply the organization out-of-band
# =============================================================================
#
# If the claim cannot be forged, the next move is to get the app to read the org
# from somewhere the caller *does* control. These are the surfaces someone would
# reach for, and the shape of the "helpful" change that would open the door:
# accepting an explicit org so an operator can act on another tenant.


ORG_INJECTION_SURFACES = [
    pytest.param(
        {"headers": {"X-Organization-Id": ORG_B}}, id="header-x-organization-id"
    ),
    pytest.param({"headers": {"X-Org-Id": ORG_B}}, id="header-x-org-id"),
    pytest.param({"headers": {"X-Tenant-Id": ORG_B}}, id="header-x-tenant-id"),
    pytest.param({"headers": {"Organization": ORG_B}}, id="header-organization"),
    pytest.param({"params": {"organization_id": ORG_B}}, id="query-organization-id"),
    pytest.param({"params": {"org_id": ORG_B}}, id="query-org-id"),
    pytest.param({"params": {"tenant": ORG_B}}, id="query-tenant"),
]


@pytest.mark.parametrize("surface", ORG_INJECTION_SURFACES)
async def test_an_out_of_band_org_never_overrides_the_claim(client, surface):
    """A caller-supplied organization must not reach the tenant binding.

    Asserting a negative on purpose. The binder reads exactly one thing — the
    verified ``organization_id`` claim — and this is the guard against that
    quietly gaining a second input. ``test_standalone_isolation_guard.py``
    forbids header-sourced tenancy in the core by source scan; this is its
    behavioural counterpart on the multi-tenant arm, where an injected org would
    name a *real* other tenant rather than being ignored by construction.
    """
    status, org = await _bound_org(client, _mint(organization_id=ORG_A), **surface)

    assert status == 200
    assert org == ORG_A, (
        f"a caller-supplied organization moved the binding to {org!r}; the "
        "verified claim must be the only input"
    )


async def test_an_injected_org_cannot_rescue_an_unauthenticated_request(client):
    """Nor may it *supply* a tenant where the token supplied none.

    The subtler half: overriding a good claim is the obvious attack, but a
    surface that only fills in a missing org would read as harmless while
    handing an anonymous caller a tenant.
    """
    status, org = await _bound_org(client, None, headers={"X-Organization-Id": ORG_B})

    assert status == 200
    assert org == UNSCOPED


# =============================================================================
# Attack 3 — reach a tenant by having none
# =============================================================================
#
# The contextvar defaults to the Standalone org. Under multi-tenant that id is
# not a tenant — it identifies the single-tenant deployment, and migration 033
# keys the global-KB WRITE policy on it. So a verified user who arrives without
# a usable org must be refused, never allowed to fall through to that default:
# falling through would hand them the one org that holds a global write licence.


ORGLESS_TOKENS = [
    pytest.param(lambda: _mint(organization_id=None), id="claim-absent"),
    pytest.param(lambda: _mint(organization_id=""), id="claim-empty"),
    pytest.param(
        lambda: _mint(organization_id=STANDALONE_ORG_ID), id="claim-standalone-sentinel"
    ),
]


@pytest.mark.parametrize("mint", ORGLESS_TOKENS)
async def test_a_verified_user_without_a_usable_tenant_is_refused(client, mint):
    """403, not a silent fall-through to the Standalone org.

    These tokens cannot come from today's generators — #629 stopped them being
    minted. They are constructed here anyway, because the binder's guarantee is
    supposed to hold independently of every minting path, and "no current path
    emits this" is a property of today's code rather than of the boundary.
    """
    async with client:
        response = await client.get(
            "/probe", headers={"Authorization": f"Bearer {mint()}"}
        )

    assert response.status_code == 403, (
        "a verified user with no usable tenant was allowed through; under "
        "multi-tenant that means the contextvar's Standalone default, which "
        "carries the global-KB write licence"
    )


# =============================================================================
# Attack 4 — outlive the revocation
# =============================================================================
#
# This is where #874 and #1042 land at the request layer. Removing a member, or
# demoting one, writes a per-user revocation watermark; the pairing is only
# worth anything if the watermark actually stops the token binding its org on
# the very next request.


async def test_a_revoked_token_binds_no_tenant(client, revocation_store):
    """ "Removed from the organization" has to mean "outstanding tokens die".

    Membership and role are verified at login only — nothing on the request path
    re-reads ``organization_members`` — so the watermark is the only mechanism
    that ends a live session. If a revoked token still bound its org, the paired
    write (#874 removal, #1042 role change) would be doing bookkeeping and
    nothing else.
    """
    token = _mint(organization_id=ORG_A)
    claims = jwt.decode(
        token, SECRET, algorithms=["HS256"], audience=AUDIENCE, issuer=ISSUER
    )

    # Everything issued at or before now is dead — the watermark a paired
    # removal or demotion writes.
    await revocation_store.revoke_user_tokens_before(
        USER_A, float(claims["iat"]) + 1, ttl=3600
    )

    status, org = await _bound_org(client, token)

    assert status == 200
    assert org == UNSCOPED, (
        f"a revoked token still bound {org!r}; the revocation watermark does "
        "not reach the tenant binding, so a removed or demoted member keeps "
        "acting inside the organization"
    )


async def test_revocation_is_scoped_to_the_revoked_user(client, revocation_store):
    """Revoking one user must not unbind everyone else.

    The inverse failure, and the one that would be discovered as an outage
    rather than a breach: a watermark applied deployment-wide would sign out
    every tenant on the next offboarding.
    """
    await revocation_store.revoke_user_tokens_before(
        USER_A, datetime.now(timezone.utc).timestamp() + 1, ttl=3600
    )

    status, org = await _bound_org(
        client, _mint(organization_id=ORG_B, user_id="some-other-user")
    )

    assert status == 200
    assert org == ORG_B


# =============================================================================
# Attack 5 — bleed across concurrent requests
# =============================================================================


async def test_many_interleaved_tenants_do_not_bleed(auth_service):
    """Twelve concurrent requests across four tenants, each reading its own org.

    ``test_tenant_scope_request_isolation.py`` pins this for two requests, which
    establishes the mechanism (a task gets a *copy* of its parent's context).
    This turns the pressure up: the slow endpoint guarantees every request's
    binder runs while others are mid-flight, so a shared context would show up
    as a wrong org rather than as a flaky ordering.
    """
    orgs = [ORG_A, ORG_B, "cccccccc-cccc-cccc-cccc-cccccccccccc", STANDALONE_ORG_ID]
    # The Standalone id is included on purpose: under multi it is refused, and a
    # refusal running alongside successes is the interleaving most likely to
    # leave a stale binding behind for whoever runs next.
    plan = [(org, i) for i in range(3) for org in orgs]

    app = _probe_app(auth_service)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://probe"
    ) as client:

        async def one(org: str, i: int):
            token = _mint(organization_id=org, user_id=f"user-{org}-{i}")
            response = await client.get(
                "/probe-slow", headers={"Authorization": f"Bearer {token}"}
            )
            return org, response

        results = await asyncio.gather(*(one(org, i) for org, i in plan))

    for org, response in results:
        if org == STANDALONE_ORG_ID:
            assert response.status_code == 403
            continue
        assert response.status_code == 200
        assert response.json()["org"] == org, (
            f"a request for {org} read {response.json()['org']!r} — a tenant "
            "binding crossed between concurrent requests"
        )


# =============================================================================
# Attack 6 — find a route the binder never sees
# =============================================================================


def test_the_real_app_binds_every_route():
    """The binder is registered globally, and nothing is mounted around it.

    Both halves are needed. A global dependency covers every route on the
    router — so no per-route audit is required — but a ``Mount``ed sub-app has
    its own router and its own dependencies, and routes underneath it would be
    served without ever binding a tenant. That is the one way a route escapes,
    and it escapes silently: the sub-app works, and every request inside it
    reads whatever the contextvar happened to hold.
    """
    import os

    from starlette.routing import Mount

    from faultmaven.config.settings import reset_settings
    from tests.integration._app_rebuild import rebuild_app

    previous = os.environ.get("OAUTH_ENABLED")
    os.environ["OAUTH_ENABLED"] = "true"  # widest surface: include the OAuth router
    reset_settings()
    try:
        app = rebuild_app()
    finally:
        if previous is None:
            os.environ.pop("OAUTH_ENABLED", None)
        else:
            os.environ["OAUTH_ENABLED"] = previous
        reset_settings()

    registered = [
        getattr(dependency.dependency, "__name__", None)
        for dependency in app.router.dependencies
    ]
    assert bind_request_org_context.__name__ in registered, (
        "the real app does not register bind_request_org_context as a global "
        "dependency, so no route binds a tenant"
    )

    mounted = [
        route.path
        for route in app.routes
        if isinstance(route, Mount) and route.path not in ("/static",)
    ]
    assert not mounted, (
        f"sub-app(s) mounted at {mounted}: routes under a Mount are served by "
        "that app's own router and never reach the global tenant binder. If the "
        "mount is genuinely static assets, add it to the exemption above with "
        "the reason."
    )
