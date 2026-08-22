"""The token and revocation endpoints speak the OAuth wire format (#1150).

RFC 6749 §3.2 prescribes `application/x-www-form-urlencoded` at the token
endpoint, and RFC 7009 §2.1 says the same for revocation. Both endpoints took a
JSON Pydantic body, so a standards-written client — or anyone reaching for
`curl -d` — was refused with a Pydantic error about the body's *shape*
(`model_attributes_type`) when the problem was its *encoding*. That refusal was
found during the fm#819 T3 rehearsal, where it first arrived as a 500 (#1048).

These tests pin the endpoints' wire contract, which has three parts:

1. **Both encodings are accepted.** Form encoding because the RFCs prescribe
   it; JSON because every first-party client sends it (copilot
   `background.ts` / `token-manager.ts` / `auth-service.ts`, faultmaven-slack-agent
   `client.py`) and those clients are not being asked to change.
2. **Errors are RFC 6749 §5.2 objects**, not FastAPI's `{"detail": ...}`.
   Accepting the prescribed encoding while answering refusals in a shape the
   same client cannot read would be half a contract.
3. **Token responses are uncacheable** (RFC 6749 §5.1). The endpoint's own
   sibling `POST /auth/refresh` already sets no-store; the OAuth token endpoint
   was the credential-bearing response that did not.

The service layer is mocked: the subject here is the HTTP wire, and the grant
logic is covered by the unit tests for `OAuthService`.
"""

import os
from unittest.mock import AsyncMock

# Set environment variables FIRST - before ANY imports (see
# test_oauth_public_endpoints for why the settings singleton is cleared rather
# than the module dropped from sys.modules).
os.environ["SKIP_SERVICE_CHECKS"] = "true"
os.environ["OAUTH_ENABLED"] = "true"

from faultmaven.config.settings import reset_settings
from tests.integration._app_rebuild import rebuild_app

reset_settings()

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient

app = rebuild_app()

from faultmaven.models.exceptions import InvalidGrantError
from faultmaven.modules.auth.contracts import OAuthTokenDTO

TOKEN_URL = "/api/v1/auth/oauth/token"
REVOKE_URL = "/api/v1/auth/oauth/revoke"
FORM = {"Content-Type": "application/x-www-form-urlencoded"}

# A realistic token value: base64url alphabet plus dots. Sent through form
# encoding it exercises `+`-as-space and `=`-in-value decoding, which is where a
# hand-rolled parser would corrupt a credential without failing.
JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJhbGljZSJ9.s1g-n_a4ture=="


@pytest.fixture
def oauth_service():
    service = AsyncMock()
    service.exchange_code_for_token.return_value = OAuthTokenDTO(
        access_token="access-from-code",
        refresh_token="refresh-from-code",
        token_type="Bearer",
        expires_in=900,
        refresh_expires_in=604800,
        user_id="user-1",
        username="alice",
    )
    service.refresh_access_token.return_value = OAuthTokenDTO(
        access_token="access-from-refresh",
        refresh_token="refresh-rotated",
        token_type="Bearer",
        expires_in=900,
        refresh_expires_in=604800,
        user_id="user-1",
        username="alice",
    )
    service.revoke_token.return_value = None
    service.revoke_refresh_token.return_value = None
    return service


@pytest.fixture
async def client(oauth_service):
    """HTTP client with the OAuth service mocked.

    The rate limiter is reset per test: /token allows 5 requests per minute per
    IP, and several tests below make more than one call.
    """
    from faultmaven.modules.auth.api.oauth import get_oauth_service
    from faultmaven.modules.auth.api.rate_limiting import reset_rate_limiter

    reset_rate_limiter()

    async def _get_oauth_service(request: Request):
        return oauth_service

    app.dependency_overrides[get_oauth_service] = _get_oauth_service
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


class TestFormEncodingIsAccepted:
    """RFC 6749 §3.2 / RFC 7009 §2.1: the prescribed request encoding."""

    @pytest.mark.asyncio
    async def test_refresh_grant_accepts_a_form_encoded_body(
        self, client, oauth_service
    ):
        response = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": JWT,
                "client_id": "faultmaven-copilot",
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["access_token"] == "access-from-refresh"

        # The credential must survive form decoding byte for byte: a token
        # mangled by `+`/`%`/`=` handling would fail at the far end of the
        # grant, where the cause is no longer visible.
        oauth_service.refresh_access_token.assert_awaited_once_with(
            refresh_token=JWT, client_id="faultmaven-copilot"
        )

    @pytest.mark.asyncio
    async def test_authorization_code_grant_accepts_a_form_encoded_body(
        self, client, oauth_service
    ):
        response = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": "auth-code-1",
                "code_verifier": "verifier-1",
                "redirect_uri": "https://abc.chromiumapp.org/",
                "client_id": "faultmaven-copilot",
            },
        )

        assert response.status_code == 200, response.text
        oauth_service.exchange_code_for_token.assert_awaited_once_with(
            code="auth-code-1",
            code_verifier="verifier-1",
            redirect_uri="https://abc.chromiumapp.org/",
        )

    @pytest.mark.asyncio
    async def test_revocation_accepts_a_form_encoded_body(self, client, oauth_service):
        response = await client.post(
            REVOKE_URL,
            data={
                "token": JWT,
                "token_type_hint": "refresh_token",
                "client_id": "faultmaven-copilot",
            },
        )

        assert response.status_code == 200, response.text
        oauth_service.revoke_refresh_token.assert_awaited_once_with(JWT)

    @pytest.mark.asyncio
    async def test_a_charset_parameter_does_not_hide_the_media_type(
        self, client, oauth_service
    ):
        """`Content-Type` may carry parameters; the media type is the prefix.

        A client that spells out the charset is sending form encoding just the
        same, and comparing the header verbatim would refuse it.
        """
        response = await client.post(
            TOKEN_URL,
            content=(
                "grant_type=refresh_token"
                f"&refresh_token={JWT.replace('=', '%3D')}"
                "&client_id=faultmaven-copilot"
            ),
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
            },
        )

        assert response.status_code == 200, response.text
        oauth_service.refresh_access_token.assert_awaited_once_with(
            refresh_token=JWT, client_id="faultmaven-copilot"
        )

    @pytest.mark.asyncio
    async def test_a_repeated_parameter_is_refused(self, client, oauth_service):
        """RFC 6749 §3.1: a parameter MUST NOT be sent more than once.

        Taking the first or the last would let whoever controls the duplicate —
        a proxy, or an attacker who can append to the body — choose which value
        the server reads.
        """
        response = await client.post(
            TOKEN_URL,
            content=(
                "grant_type=refresh_token"
                "&refresh_token=attacker&refresh_token=victim"
                "&client_id=faultmaven-copilot"
            ),
            headers=FORM,
        )

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_request"
        assert "more than once" in response.json()["error_description"]
        oauth_service.refresh_access_token.assert_not_awaited()


class TestJsonRemainsAccepted:
    """Every first-party client sends JSON; none is being asked to change."""

    @pytest.mark.asyncio
    async def test_refresh_grant_accepts_json(self, client, oauth_service):
        response = await client.post(
            TOKEN_URL,
            json={
                "grant_type": "refresh_token",
                "refresh_token": JWT,
                "client_id": "faultmaven-copilot",
            },
        )

        assert response.status_code == 200, response.text
        oauth_service.refresh_access_token.assert_awaited_once_with(
            refresh_token=JWT, client_id="faultmaven-copilot"
        )

    @pytest.mark.asyncio
    async def test_revocation_accepts_json(self, client, oauth_service):
        response = await client.post(
            REVOKE_URL,
            json={"token": JWT, "client_id": "faultmaven-copilot"},
        )

        assert response.status_code == 200, response.text
        oauth_service.revoke_token.assert_awaited_once_with(JWT)

    @pytest.mark.asyncio
    async def test_a_body_with_no_content_type_is_read_as_json(
        self, client, oauth_service
    ):
        """What FastAPI did before these routes parsed their own bodies.

        A client that omits the header keeps working rather than discovering a
        new refusal it never had to satisfy.
        """
        response = await client.post(
            TOKEN_URL,
            content=(
                '{"grant_type": "refresh_token", "refresh_token": "'
                + JWT
                + '", "client_id": "faultmaven-copilot"}'
            ),
            headers={"Content-Type": ""},
        )

        assert response.status_code == 200, response.text


class TestErrorsUseTheRfcShape:
    """RFC 6749 §5.2 — the codes a client dispatches on."""

    @pytest.mark.asyncio
    async def test_an_unknown_grant_type_is_unsupported_grant_type(
        self, client, oauth_service
    ):
        """The endpoint's original branch for this was unreachable.

        `grant_type` is a `Literal`, so Pydantic rejected an unknown value
        before the handler's own "Unsupported grant_type" 400 could run. The
        value is now checked before model validation, which is what makes the
        RFC's dedicated code reachable.
        """
        response = await client.post(
            TOKEN_URL,
            data={"grant_type": "client_credentials", "client_id": "some-client"},
        )

        assert response.status_code == 400
        body = response.json()
        assert body["error"] == "unsupported_grant_type"
        assert "client_credentials" in body["error_description"]
        assert "authorization_code" in body["error_description"]

    @pytest.mark.asyncio
    async def test_a_missing_parameter_is_invalid_request(self, client):
        response = await client.post(
            TOKEN_URL,
            data={"grant_type": "refresh_token", "client_id": "faultmaven-copilot"},
        )

        assert response.status_code == 400
        body = response.json()
        assert body["error"] == "invalid_request"
        assert "refresh_token" in body["error_description"]

    @pytest.mark.asyncio
    async def test_a_rejected_grant_is_invalid_grant_at_400(
        self, client, oauth_service
    ):
        """RFC 6749 §5.2 puts invalid_grant at 400; this endpoint answered 401.

        Both first-party consumers treat any definitive 4xx the same way —
        copilot's TokenManager clears its tokens for 4xx other than 408/429,
        and the Slack agent maps 400 and 401 alike onto a rejected credential —
        so the status moves with the error code rather than being kept for
        compatibility it does not buy.
        """
        oauth_service.refresh_access_token.side_effect = InvalidGrantError(
            "Refresh token expired or revoked"
        )

        response = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": JWT,
                "client_id": "faultmaven-copilot",
            },
        )

        assert response.status_code == 400
        body = response.json()
        assert body["error"] == "invalid_grant"
        assert "expired or revoked" in body["error_description"]

    @pytest.mark.asyncio
    async def test_an_unsupported_content_type_names_both_encodings(self, client):
        """The refusal must say what to send.

        This is the case that produced the issue: a body the endpoint cannot
        read used to come back as a Pydantic complaint about the body's shape,
        which sends the reader looking at their parameters rather than their
        Content-Type.
        """
        response = await client.post(
            TOKEN_URL,
            content=b"grant_type=refresh_token",
            headers={"Content-Type": "text/plain"},
        )

        assert response.status_code == 415
        description = response.json()["error_description"]
        assert "text/plain" in description
        assert "application/x-www-form-urlencoded" in description
        assert "application/json" in description

    @pytest.mark.asyncio
    async def test_an_echoed_value_is_bounded(self, client):
        """A refusal names the offending value, but does not mirror a payload.

        These endpoints are unauthenticated, so an unbounded echo turns a large
        request into a large response and a large log line — the reflection
        #1048 capped in the validation handler.
        """
        response = await client.post(
            TOKEN_URL,
            data={"grant_type": "z" * 100_000, "client_id": "faultmaven-copilot"},
        )

        assert response.status_code == 400
        assert response.json()["error"] == "unsupported_grant_type"
        assert len(response.content) < 1_000

    @pytest.mark.asyncio
    async def test_an_empty_body_is_invalid_request(self, client):
        response = await client.post(TOKEN_URL, headers=FORM)

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_request"

    @pytest.mark.asyncio
    async def test_a_malformed_json_body_is_invalid_request(self, client):
        response = await client.post(
            TOKEN_URL,
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_request"

    @pytest.mark.asyncio
    async def test_an_empty_client_id_is_refused(self, client, oauth_service):
        """`-d client_id=` is easy to send by accident; it identifies nobody."""
        response = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": JWT,
                "client_id": "",
            },
        )

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_request"
        oauth_service.refresh_access_token.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_error_bodies_do_not_carry_the_fastapi_detail_shape(self, client):
        """The shape really changed, rather than gaining a second spelling.

        A body carrying both would let a client keep reading `detail` and never
        learn the error code it is supposed to dispatch on.
        """
        response = await client.post(
            TOKEN_URL, data={"grant_type": "nope", "client_id": "c"}
        )

        body = response.json()
        assert "detail" not in body
        assert set(body) == {"error", "error_description"}

    @pytest.mark.asyncio
    async def test_an_unknown_token_type_hint_is_unsupported_token_type(
        self, client, oauth_service
    ):
        """RFC 7009 §2.2.1 gives the unrecognised hint its own code."""
        response = await client.post(
            REVOKE_URL,
            data={"token": JWT, "token_type_hint": "id_token", "client_id": "c"},
        )

        assert response.status_code == 400
        assert response.json()["error"] == "unsupported_token_type"
        oauth_service.revoke_token.assert_not_awaited()
        oauth_service.revoke_refresh_token.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unrecorded_revocation_is_temporarily_unavailable(
        self, client, oauth_service
    ):
        """RFC 7009 §2.2.1 permits a 503 when the revocation was not recorded.

        Unknown tokens are a success by the RFC, so an exception here means the
        token is still live — the client must retry rather than believe it dead.
        """
        oauth_service.revoke_token.side_effect = RuntimeError("store outage")

        response = await client.post(REVOKE_URL, data={"token": JWT, "client_id": "c"})

        assert response.status_code == 503
        assert response.json()["error"] == "temporarily_unavailable"


class TestTokenResponsesAreNotCacheable:
    """RFC 6749 §5.1: the body carries fresh credentials."""

    @pytest.mark.asyncio
    async def test_a_successful_grant_is_no_store(self, client):
        response = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": JWT,
                "client_id": "faultmaven-copilot",
            },
        )

        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["pragma"] == "no-cache"

    @pytest.mark.asyncio
    async def test_a_refusal_is_no_store(self, client):
        """A refusal names an expired or revoked credential; do not cache it."""
        response = await client.post(
            TOKEN_URL, data={"grant_type": "refresh_token", "client_id": "c"}
        )

        assert response.status_code == 400
        assert response.headers["cache-control"] == "no-store"
