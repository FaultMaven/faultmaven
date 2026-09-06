"""A retry that straddles an access-token refresh must replay, not re-execute (fm#1087).

``IdempotencyMiddleware`` scoped its cache to ``sha256(Authorization)``. The
copilot refreshes its access token periodically, so a retry issued after a
refresh carried a *different* bearer string for the *same* principal — a
different bucket, a guaranteed miss, and the non-idempotent turn handler ran a
second time. Observed live: one user message committed twice to
``case_b2769d770218``, with the key logged as *cached* twice and never replayed.

The property asserted here is the one that matters: **the handler runs once**.
Asserting that two cache keys are equal would pass while the duplicate still
committed.

Both turn submissions in the live trace were ``multipart/form-data``, which
``_body_fingerprint`` never fingerprints — so the replay branch these requests
take is "both unfingerprinted". A fix that only worked for JSON bodies would
miss the copilot's hot retry path entirely, so the multipart case is pinned
alongside the JSON one.
"""

from unittest.mock import MagicMock, patch

import fakeredis.aioredis as fakeredis_aio
import httpx
import jwt as pyjwt
import pytest
from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import JSONResponse

from faultmaven.api.middleware.idempotency import IdempotencyMiddleware
from faultmaven.modules.auth.domain.services.auth_service import AuthService
from faultmaven.modules.auth.infrastructure.stores.token_revocation_store import (
    RedisTokenRevocationStore,
)
from tests.utils import forge_access_token

USER_ID = "11111111-1111-1111-1111-111111111111"
OTHER_USER_ID = "33333333-3333-3333-3333-333333333333"
ENTERPRISE_ID = "22222222-2222-2222-2222-222222222222"
SECRET = "test-secret-key-0123456789abcdef"  # 32+ bytes: HS256 minimum
KEY = "opt_msg_1787041101005_2"  # the live key from the fm#1087 trace


def _mock_settings():
    settings = MagicMock()
    settings.auth.auth_mode = "local"
    settings.security.jwt_algorithm = "HS256"
    settings.auth.jwt_access_token_expire_minutes = 15
    settings.auth.jwt_refresh_token_expire_days = 7
    settings.security.jwt_issuer = "faultmaven"
    settings.security.jwt_audience = "faultmaven-api"
    settings.security.token_revocation_prefix = "revoked:token:"
    settings.security.jwt_private_key = None
    settings.security.jwt_public_key = None
    settings.security.jwt_private_key_path = None
    settings.security.jwt_public_key_path = None
    settings.security.jwt_secret_key = MagicMock()
    settings.security.jwt_secret_key.get_secret_value.return_value = SECRET
    return settings


@pytest.fixture
def auth_service():
    """Real AuthService: HS256 local mode, production revocation store on FakeRedis."""
    store = RedisTokenRevocationStore(
        fakeredis_aio.FakeRedis(decode_responses=True), key_prefix="revoked:token:"
    )
    with patch(
        "faultmaven.modules.auth.domain.services.auth_service.get_settings",
        return_value=_mock_settings(),
    ):
        yield AuthService(revocation_store=store)


def _access_token(auth_service: AuthService, user_id: str = USER_ID) -> str:
    """Mint an access token the real AuthService verifies.

    Every call produces a distinct ``jti`` (and therefore a distinct signed
    string), which is exactly what a refresh produces on the wire.
    """
    return forge_access_token(
        auth_service,
        user_id=user_id,
        enterprise_id=ENTERPRISE_ID,
        email="user@example.com",
        roles=["member"],
    )


def _build_app(auth_service):
    """A turn-shaped app wired like production: auth is route-level, not middleware."""
    app = FastAPI()
    route_calls = {"turns": 0, "json": 0, "guarded": 0}

    @app.post("/api/v1/cases/{case_id}/turns")
    async def submit_turn(case_id: str, query: str = "", file: UploadFile = None):
        """Mirrors the real multipart turn route — the shape that duplicated."""
        route_calls["turns"] += 1
        return {"turn": route_calls["turns"], "case_id": case_id, "query": query}

    @app.post("/api/v1/json-turn")
    async def json_turn(request: Request):
        route_calls["json"] += 1
        return {"turn": route_calls["json"]}

    @app.post("/api/v1/guarded-turn")
    async def guarded_turn(request: Request):
        """Mirrors ``Depends(require_authentication)``, which every real turn route takes.

        The route — not the middleware — is what rejects a credential that no
        longer verifies. That is what makes a cache *miss* on such a request
        harmless: it lands on a 401, not on a second execution.
        """
        token = request.headers.get("Authorization", "")[7:]
        try:
            await auth_service.verify_token_with_revocation_check(
                token, token_type="access"
            )
        except Exception:
            return JSONResponse(
                status_code=401, content={"detail": "Not authenticated"}
            )
        route_calls["guarded"] += 1
        return {"turn": route_calls["guarded"]}

    fake = fakeredis_aio.FakeRedis(decode_responses=True)
    app.add_middleware(IdempotencyMiddleware, redis_client=fake)
    app.state.auth_service = auth_service
    app.state.route_calls = route_calls
    return app, fake


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


def _replayed(response) -> bool:
    return response.headers.get("X-Idempotency-Replayed") == "true"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_multipart_retry_after_token_refresh_replays_and_does_not_re_execute(
    auth_service,
):
    """The fm#1087 headline: same key, refreshed token, multipart body."""
    app, _ = _build_app(auth_service)
    before_refresh = _access_token(auth_service)
    after_refresh = _access_token(auth_service)
    assert before_refresh != after_refresh, "a refresh must change the bearer string"

    async with _client(app) as client:
        first = await client.post(
            "/api/v1/cases/case_b2769d770218/turns",
            headers={
                "Authorization": f"Bearer {before_refresh}",
                "Idempotency-Key": KEY,
            },
            data={"query": "why is the pod crashlooping"},
            files={"file": ("evidence.log", b"OOMKilled", "application/octet-stream")},
        )
        retry = await client.post(
            "/api/v1/cases/case_b2769d770218/turns",
            headers={
                "Authorization": f"Bearer {after_refresh}",
                "Idempotency-Key": KEY,
            },
            data={"query": "why is the pod crashlooping"},
            files={"file": ("evidence.log", b"OOMKilled", "application/octet-stream")},
        )

    assert first.status_code == 200
    assert not _replayed(first)
    assert retry.status_code == 200, "the retry must not be refused"
    assert _replayed(retry), "a refreshed token must not fork the bucket"
    assert retry.json() == first.json()
    assert app.state.route_calls["turns"] == 1, "the turn handler must run exactly once"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_json_retry_after_token_refresh_replays_and_does_not_re_execute(
    auth_service,
):
    """Same property on the fingerprinted (JSON) path, so neither branch regresses."""
    app, _ = _build_app(auth_service)
    before_refresh = _access_token(auth_service)
    after_refresh = _access_token(auth_service)

    async with _client(app) as client:
        first = await client.post(
            "/api/v1/json-turn",
            headers={
                "Authorization": f"Bearer {before_refresh}",
                "Idempotency-Key": KEY,
            },
            json={"query": "same body"},
        )
        retry = await client.post(
            "/api/v1/json-turn",
            headers={
                "Authorization": f"Bearer {after_refresh}",
                "Idempotency-Key": KEY,
            },
            json={"query": "same body"},
        )

    assert not _replayed(first)
    assert _replayed(retry)
    assert app.state.route_calls["json"] == 1, "the handler must run exactly once"


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_a_different_principal_still_gets_its_own_bucket(auth_service):
    """The fm#958 guarantee must survive: two *principals* never share a bucket.

    Both tokens here are genuinely valid — this is not a forgery test, it is the
    proof that widening the bucket to survive a refresh did not widen it to
    survive a change of user.
    """
    app, _ = _build_app(auth_service)
    mine = _access_token(auth_service, USER_ID)
    theirs = _access_token(auth_service, OTHER_USER_ID)

    async with _client(app) as client:
        first = await client.post(
            "/api/v1/json-turn",
            headers={"Authorization": f"Bearer {mine}", "Idempotency-Key": KEY},
            json={"query": "same body"},
        )
        other = await client.post(
            "/api/v1/json-turn",
            headers={"Authorization": f"Bearer {theirs}", "Idempotency-Key": KEY},
            json={"query": "same body"},
        )

    assert not _replayed(first)
    assert not _replayed(other), "another principal must never be served my response"
    assert app.state.route_calls["json"] == 2


# ---------------------------------------------------------------------------
# The surface that keying on ``sub`` opens, and that the previous
# raw-credential scope did not have: a token nobody signed must not be able to
# choose whose bucket a request lands in. A cache hit returns before
# ``call_next``, so route-level auth never runs on one — a forged bucket
# selection would be a cross-tenant read of another principal's response body,
# strictly worse than the duplicate turn this change fixes.
# ---------------------------------------------------------------------------


def _claims_of(token: str) -> dict:
    return pyjwt.decode(token, options={"verify_signature": False})


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_forged_token_claiming_the_victims_sub_cannot_reach_their_bucket(
    auth_service,
):
    """Same ``sub``, wrong signing key: verification fails, so it names nobody."""
    app, _ = _build_app(auth_service)
    genuine = _access_token(auth_service)
    forged = pyjwt.encode(
        _claims_of(genuine), "attacker-key-not-the-deployments", algorithm="HS256"
    )
    assert _claims_of(forged)["sub"] == USER_ID, "the forgery must claim the victim"

    async with _client(app) as client:
        victim = await client.post(
            "/api/v1/json-turn",
            headers={"Authorization": f"Bearer {genuine}", "Idempotency-Key": KEY},
            json={"query": "victim body"},
        )
        attacker = await client.post(
            "/api/v1/json-turn",
            headers={"Authorization": f"Bearer {forged}", "Idempotency-Key": KEY},
            json={"query": "victim body"},
        )

    assert victim.status_code == 200
    assert not _replayed(attacker), "an unsigned claim must not select a bucket"
    assert app.state.route_calls["json"] == 2


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_unsigned_alg_none_token_cannot_reach_the_victims_bucket(auth_service):
    """The classic bypass: ``alg: none`` over the victim's claims."""
    app, _ = _build_app(auth_service)
    genuine = _access_token(auth_service)
    unsigned = pyjwt.encode(_claims_of(genuine), key=None, algorithm="none")

    async with _client(app) as client:
        await client.post(
            "/api/v1/json-turn",
            headers={"Authorization": f"Bearer {genuine}", "Idempotency-Key": KEY},
            json={"query": "victim body"},
        )
        attacker = await client.post(
            "/api/v1/json-turn",
            headers={"Authorization": f"Bearer {unsigned}", "Idempotency-Key": KEY},
            json={"query": "victim body"},
        )

    assert not _replayed(attacker)
    assert app.state.route_calls["json"] == 2


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_revoked_token_does_not_replay_under_the_principal_scope(auth_service):
    """Revocation participates in scoping, so a killed credential loses the bucket.

    Without the revocation check a stolen-then-revoked token would still verify
    and could replay a response the principal's *live* token had cached, without
    the route (and its own revocation check) ever running.
    """
    app, _ = _build_app(auth_service)
    live = _access_token(auth_service)
    stolen = _access_token(auth_service)

    async with _client(app) as client:
        first = await client.post(
            "/api/v1/json-turn",
            headers={"Authorization": f"Bearer {live}", "Idempotency-Key": KEY},
            json={"query": "same body"},
        )
        stolen_claims = _claims_of(stolen)
        await auth_service.revoke_token(stolen_claims["jti"], stolen_claims["exp"])
        after_revocation = await client.post(
            "/api/v1/json-turn",
            headers={"Authorization": f"Bearer {stolen}", "Idempotency-Key": KEY},
            json={"query": "same body"},
        )

    assert not _replayed(first)
    assert not _replayed(after_revocation)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unverifiable_credentials_keep_the_raw_narrow_scope(auth_service):
    """No verifier wired: fall back to the pre-fm#1087 raw-credential scope.

    Not a hypothetical — an app that never ran the composition-root lifespan has
    no ``auth_service`` on ``app.state``. The fallback must still both replay for
    a byte-identical credential and split for a different one, so idempotency
    degrades in scope rather than switching off.
    """
    app, _ = _build_app(auth_service)
    del app.state.auth_service
    opaque = "Bearer opaque-not-a-jwt-credential"

    async with _client(app) as client:
        first = await client.post(
            "/api/v1/json-turn",
            headers={"Authorization": opaque, "Idempotency-Key": KEY},
            json={"query": "same body"},
        )
        same = await client.post(
            "/api/v1/json-turn",
            headers={"Authorization": opaque, "Idempotency-Key": KEY},
            json={"query": "same body"},
        )
        other = await client.post(
            "/api/v1/json-turn",
            headers={"Authorization": opaque + "x", "Idempotency-Key": KEY},
            json={"query": "same body"},
        )

    assert not _replayed(first)
    assert _replayed(same), "an identical credential must still replay"
    assert not _replayed(other), "a different credential must still split the bucket"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retry_with_an_expired_token_cannot_duplicate(auth_service):
    """The one fork this change introduces, shown to be harmless.

    A token that verified on the first attempt and has since expired moves from
    the principal scope to the raw one, so its retry misses where the old code
    replayed. It reaches the route, and the route — which takes
    ``require_authentication`` on every turn surface — rejects it. A 401, not a
    duplicate, and a 401 is never cached.
    """
    app, fake = _build_app(auth_service)
    live = _access_token(auth_service)
    expired = forge_access_token(
        auth_service,
        user_id=USER_ID,
        enterprise_id=ENTERPRISE_ID,
        email="user@example.com",
        roles=["member"],
        expires_in_minutes=-5,
    )

    async with _client(app) as client:
        first = await client.post(
            "/api/v1/guarded-turn",
            headers={"Authorization": f"Bearer {live}", "Idempotency-Key": KEY},
            json={"query": "same body"},
        )
        retry = await client.post(
            "/api/v1/guarded-turn",
            headers={"Authorization": f"Bearer {expired}", "Idempotency-Key": KEY},
            json={"query": "same body"},
        )

    assert first.status_code == 200
    assert retry.status_code == 401, "the route must reject the dead credential"
    assert app.state.route_calls["guarded"] == 1, "no second turn was committed"
    assert len(await fake.keys("idempotency:*")) == 1, "a 401 must not be cached"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_broken_verifier_degrades_instead_of_500ing(auth_service):
    """Naming the principal is best-effort; serving the request is not.

    ``_verified_principal`` runs *before* ``dispatch``'s try block, so anything
    that escapes it 500s every POST carrying an ``Idempotency-Key``. Simulated
    with a verifier that raises something no auth path models — the same shape
    an ImportError from the request-time ``from .auth import _extract_token``
    would take.
    """

    class _BrokenVerifier:
        async def verify_token_with_revocation_check(self, *args, **kwargs):
            raise TypeError("verifier contract drifted")

    app, _ = _build_app(auth_service)
    app.state.auth_service = _BrokenVerifier()
    token = _access_token(auth_service)

    async with _client(app) as client:
        first = await client.post(
            "/api/v1/json-turn",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": KEY},
            json={"query": "same body"},
        )
        retry = await client.post(
            "/api/v1/json-turn",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": KEY},
            json={"query": "same body"},
        )

    assert first.status_code == 200, "a broken verifier must not 500 the request"
    assert retry.status_code == 200
    assert _replayed(retry), "it must still replay under the raw fallback scope"


# ---------------------------------------------------------------------------
# Tenancy: the raw-credential scope distinguished tenants as a side effect (the
# claim rides inside the signed token). Keying on ``sub`` alone would drop that
# and lean on ``resolve_enterprise_claim`` reading the tenant off the user
# record — an invariant that lives in another module, is not enforced here, and
# would put two different-tenant requests in one bucket if it ever stopped
# holding. The ENTERPRISE is carried in the scope instead: it is what isolates
# (ADR-017 D1), where the organization only says who pays and is absent from
# most tokens.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_one_sub_under_two_enterprises_does_not_share_a_bucket(auth_service):
    """Same principal, different verified tenant — must not replay across them.

    A cache hit returns before ``call_next``, so a shared bucket here would hand
    the second request the first tenant's response body without the route ever
    running.
    """
    app, _ = _build_app(auth_service)
    OTHER_ENTERPRISE = "44444444-4444-4444-4444-444444444444"
    in_ent_a = forge_access_token(
        auth_service,
        user_id=USER_ID,
        enterprise_id=ENTERPRISE_ID,
        email="user@example.com",
        roles=["member"],
    )
    in_ent_b = forge_access_token(
        auth_service,
        user_id=USER_ID,
        enterprise_id=OTHER_ENTERPRISE,
        email="user@example.com",
        roles=["member"],
    )
    assert _claims_of(in_ent_a)["sub"] == _claims_of(in_ent_b)["sub"]

    async with _client(app) as client:
        first = await client.post(
            "/api/v1/json-turn",
            headers={"Authorization": f"Bearer {in_ent_a}", "Idempotency-Key": KEY},
            json={"query": "same body"},
        )
        cross_tenant = await client.post(
            "/api/v1/json-turn",
            headers={"Authorization": f"Bearer {in_ent_b}", "Idempotency-Key": KEY},
            json={"query": "same body"},
        )

    assert not _replayed(first)
    assert not _replayed(cross_tenant), "a different tenant must not be served my body"
    assert app.state.route_calls["json"] == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_refresh_fix_still_holds_with_the_org_term(auth_service):
    """Carrying the org must not undo fm#1087: a refresh keeps sub *and* org."""
    app, _ = _build_app(auth_service)
    before_refresh = _access_token(auth_service)
    after_refresh = _access_token(auth_service)

    async with _client(app) as client:
        first = await client.post(
            "/api/v1/json-turn",
            headers={
                "Authorization": f"Bearer {before_refresh}",
                "Idempotency-Key": KEY,
            },
            json={"query": "same body"},
        )
        retry = await client.post(
            "/api/v1/json-turn",
            headers={
                "Authorization": f"Bearer {after_refresh}",
                "Idempotency-Key": KEY,
            },
            json={"query": "same body"},
        )

    assert not _replayed(first)
    assert _replayed(retry)
    assert app.state.route_calls["json"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_claims_without_a_usable_sub_degrade_instead_of_500ing(auth_service):
    """The claim reads are inside the guard, not protected by another module's type.

    ``verify_token_with_revocation_check`` is typed ``-> Dict[str, Any]`` and
    raises on every failure, so ``claims`` is never ``None`` today. That is a
    contract in ``auth_service``, not structure here — a verifier that returned
    ``None`` must degrade to the raw scope, not 500 every idempotent POST.
    """

    class _NullReturningVerifier:
        async def verify_token_with_revocation_check(self, *args, **kwargs):
            return None

    app, _ = _build_app(auth_service)
    app.state.auth_service = _NullReturningVerifier()
    token = _access_token(auth_service)

    async with _client(app) as client:
        first = await client.post(
            "/api/v1/json-turn",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": KEY},
            json={"query": "same body"},
        )
        retry = await client.post(
            "/api/v1/json-turn",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": KEY},
            json={"query": "same body"},
        )

    assert first.status_code == 200
    assert _replayed(retry), "must still replay under the raw fallback scope"
