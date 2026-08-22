"""OAuth 2.0 Authentication Routes.

This module implements OAuth 2.0 Authorization Code Flow with PKCE for
Dashboard-centric authentication where the Dashboard acts as IdP for
the Browser Extension.

Key Endpoints:
- GET /auth/oauth/authorize: Authorization code generation
- POST /auth/oauth/token: Token exchange and refresh
- POST /auth/oauth/revoke: Token revocation

OAuth Flow:
1. Extension redirects user to Dashboard authorization page
2. User authenticates in Dashboard (dev-login or existing session)
3. Dashboard generates authorization code with PKCE challenge
4. Extension exchanges code + verifier for access/refresh tokens
5. Extension uses access token for API requests
6. Extension silently refreshes tokens before expiry

Security:
- PKCE (SHA256) prevents authorization code interception
- Single-use authorization codes (10 minute expiry)
- Short-lived access tokens (15 minutes)
- Long-lived refresh tokens (7 days) with rotation
- Constant-time comparison for PKCE verification

Wire format:
- POST /token and POST /revoke accept BOTH RFC 6749 §3.2 form encoding and
  JSON, and answer errors in the RFC 6749 §5.2 shape. See the "RFC 6749 wire
  format" section below for why the routes parse their own bodies.
- GET /authorize takes query parameters, which is already the encoding RFC 6749
  §3.1 prescribes for the authorization endpoint. Its *errors* are JSON rather
  than the §4.1.2.1 redirect, because its client is the Dashboard consent
  screen rather than a browser following redirects.
"""

import json
import logging
import re
from typing import Any, Literal, Optional, Type, TypeVar, get_args
from urllib.parse import parse_qsl

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, ValidationError

from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.models.exceptions import (
    InvalidGrantError,
    InvalidRequestError,
    OAuthProtocolError,
)
from faultmaven.modules.auth.api.rate_limiting import (
    require_oauth_rate_limit_authorize,
    require_oauth_rate_limit_revoke,
    require_oauth_rate_limit_token,
)
from faultmaven.modules.auth.contracts import (
    IOAuthService,
    OAuthAuthorizationDTO,
    OAuthTokenDTO,
)
from faultmaven.modules.auth.domain.models.auth import DevUser

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/oauth", tags=["oauth"])


# ============================================================
# Request/Response Models
# ============================================================


# NOTE: there is deliberately no `AuthorizationRequest` model here. One existed,
# declaring `response_type: Literal["code"]` and describing `redirect_uri` as
# "must match allowed patterns" — but `GET /authorize` takes loose `str = Query()`
# parameters and never bound it, so nothing in the process read it and none of
# the constraints it named were enforced (#1053). It documented a validation that
# did not happen. The constraints now live in
# `IOAuthService.validate_authorization_request`, which the route calls.


class AuthorizationConsentRequest(BaseModel):
    """Authorization consent information for Dashboard UI.

    Returned when user needs to approve/deny the authorization request.
    """

    client_id: str = Field(description="OAuth client ID requesting access")
    client_name: str = Field(description="Human-readable client name")
    redirect_uri: str = Field(description="Callback URI after authorization")
    scope: str = Field(description="Requested scopes (space-separated)")
    state: str = Field(description="Client state (for CSRF protection)")
    user_id: str = Field(description="Authenticated user ID")
    username: str = Field(description="Authenticated username")


class AuthorizationApprovalRequest(BaseModel):
    """User's approval/denial of authorization request (POST body).

    Submitted by Dashboard after user reviews consent screen.
    """

    approved: bool = Field(description="True if user approved, False if denied")
    code_challenge: str = Field(description="PKCE code challenge")
    code_challenge_method: Optional[Literal["S256"]] = Field(default="S256")
    client_id: str = Field(description="OAuth client ID")
    redirect_uri: str = Field(description="Callback URI")
    scope: str = Field(description="Requested scopes")
    state: str = Field(description="Client state")


class AuthorizationResponse(BaseModel):
    """OAuth authorization response (redirect parameters).

    Extension receives this as query parameters after redirect.
    """

    code: str = Field(description="Authorization code (10 minute expiry)")
    state: str = Field(description="Client state (echoed back for verification)")


# The supported grant types, named once. `token()` checks `grant_type` against
# `SUPPORTED_GRANT_TYPES` before model validation so an unknown value answers
# RFC 6749 §5.2's `unsupported_grant_type` rather than a generic parameter
# error; deriving the tuple from the annotation keeps the check from drifting
# out of step with the model, which is how the endpoint's original
# "Unsupported grant_type" branch became unreachable.
GrantType = Literal["authorization_code", "refresh_token"]
SUPPORTED_GRANT_TYPES: tuple[str, ...] = get_args(GrantType)

# RFC 7009 §2.1: the hint is optional, and §2.2.1 gives an unknown one its own
# error code.
TokenTypeHint = Literal["access_token", "refresh_token"]
SUPPORTED_TOKEN_TYPE_HINTS: tuple[str, ...] = get_args(TokenTypeHint)


class TokenRequest(BaseModel):
    """OAuth token request (authorization_code or refresh_token grant).

    Two grant types supported:
    1. authorization_code: Exchange code for tokens
    2. refresh_token: Refresh access token using refresh token
    """

    grant_type: GrantType = Field(
        description="Grant type: 'authorization_code' or 'refresh_token'"
    )

    # For authorization_code grant
    code: Optional[str] = Field(
        default=None,
        description="Authorization code (required for authorization_code grant)",
    )
    redirect_uri: Optional[str] = Field(
        default=None,
        description="Redirect URI (required for authorization_code grant, must match)",
    )
    code_verifier: Optional[str] = Field(
        default=None,
        description="PKCE code verifier (required for authorization_code grant)",
    )

    # For refresh_token grant
    refresh_token: Optional[str] = Field(
        default=None, description="Refresh token (required for refresh_token grant)"
    )

    # Common parameters
    #
    # `min_length=1` because form encoding makes an empty parameter easy to
    # send by accident (`-d client_id=`) where a JSON client would have had to
    # write `""`; an empty client id identifies nobody and must be refused as a
    # malformed request rather than carried into the grant.
    client_id: str = Field(min_length=1, description="OAuth client ID")


class TokenResponse(BaseModel):
    """OAuth token response (access token + refresh token).

    Returned for both authorization_code and refresh_token grants.
    """

    access_token: str = Field(description="JWT access token (15 minute expiry)")
    refresh_token: str = Field(description="JWT refresh token (7 days expiry)")
    token_type: str = Field(default="Bearer", description="Token type (always Bearer)")
    expires_in: int = Field(description="Access token expiry in seconds (900)")
    refresh_expires_in: int = Field(
        description="Refresh token expiry in seconds (604800)"
    )
    user_id: str = Field(description="User ID")
    username: str = Field(description="Username")


class OAuthErrorResponse(BaseModel):
    """An RFC 6749 §5.2 error, as `/token` and `/revoke` answer it.

    Declared for the OpenAPI document; the body itself is written by
    ``api.exception_handlers.oauth_protocol_error_handler``. It carries these
    two fields and no others — notably no `correlation_id`, which travels in
    the `X-Correlation-ID` / `X-Request-ID` response headers instead (the
    middleware that stamps the latter is skipped in test environments, so a
    body field sourced from it would exist in production and nowhere a client
    author could try it).
    """

    error: str = Field(
        description="RFC 6749 §5.2 error code, e.g. 'invalid_grant'",
    )
    error_description: str = Field(
        description="Human-readable explanation, for the developer holding the request",
    )


class RevokeRequest(BaseModel):
    """OAuth token revocation request.

    Supports revoking both access tokens and refresh tokens.
    """

    token: str = Field(min_length=1, description="Token to revoke (access or refresh)")
    token_type_hint: Optional[TokenTypeHint] = Field(
        default=None, description="Hint about token type (optional)"
    )
    client_id: str = Field(min_length=1, description="OAuth client ID")


# ============================================================
# RFC 6749 wire format
# ============================================================

# RFC 6749 §3.2 prescribes `application/x-www-form-urlencoded` at the token
# endpoint, and RFC 7009 §2.1 says the same for revocation. FastAPI's
# `Body(...)` parses JSON only: every other content type reaches Pydantic as
# raw bytes and fails as `model_attributes_type`, so a standards-written client
# — or anyone reaching for `curl -d` — was refused with an error about the
# body's *shape* when the problem was its *encoding* (#1150).
#
# FastAPI cannot declare two body encodings on one signature, so `token()` and
# `revoke()` take the raw Request, dispatch on content type and validate by
# hand. JSON stays supported alongside form encoding: every first-party client
# sends it (copilot `background.ts` / `token-manager.ts` / `auth-service.ts`,
# faultmaven-slack-agent `client.py`), and these endpoints are the OAuth
# surface of an otherwise-JSON API.
#
# Errors move to the RFC 6749 §5.2 shape in the same change, deliberately:
# accepting the prescribed encoding while answering `{"detail": ...}` would
# leave a client that can now *reach* the endpoint unable to read its refusals.

FORM_MEDIA_TYPE = "application/x-www-form-urlencoded"

# RFC 6749 §5.1: a token response carries credentials, so it must not be
# cached. The same convention already applies to POST /auth/refresh and the SSO
# exchange; the OAuth token endpoint was the one credential-bearing response
# that omitted it.
_NO_STORE_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}

_OAuthRequestModel = TypeVar("_OAuthRequestModel", bound=BaseModel)

# A refusal that names the offending value is what makes it diagnosable, but
# the value is client-supplied and these endpoints are unauthenticated: echoed
# unbounded, a megabyte `grant_type` becomes a megabyte error body and a
# megabyte log line. #1048 capped the same class of reflection in the
# validation handler. The bound is in UTF-8 BYTES because that is what the
# response is encoded as — counting characters lets a CJK value through at
# roughly three times the stated ceiling — and it is small because every value
# echoed here is short by nature: a grant type, a media type, a parameter name.
_MAX_ECHOED_BYTES = 120


def _echo(value: Any) -> str:
    """Render a client-supplied value for an error message, bounded."""
    encoded = str(value).encode("utf-8")
    if len(encoded) <= _MAX_ECHOED_BYTES:
        return str(value)
    # Truncation can land inside a multi-byte character; drop the partial one.
    return encoded[:_MAX_ECHOED_BYTES].decode("utf-8", "ignore") + "…"


# `OAuthProtocolError` lives in `faultmaven.models.exceptions` beside the other
# OAuth exceptions and is rendered centrally by
# `api.exception_handlers.oauth_protocol_error_handler`. The endpoints below
# raise it and never build an error response themselves: a `return` inside an
# `except` block is how internal exception text reaches an unauthenticated body,
# and `tests/unit/modules/auth/api/test_auth_error_text_not_echoed.py` refuses
# that shape structurally rather than site by site.


async def _read_oauth_params(request: Request) -> dict[str, Any]:
    """Decode a token/revocation body into a parameter mapping.

    Accepts RFC 6749 §3.2 form encoding and JSON. A body sent with no
    Content-Type at all is read as JSON, which is what FastAPI did before these
    routes parsed their own bodies — a client that omits the header keeps
    working.

    Raises:
        OAuthProtocolError: the body is empty, undecodable, or sent under a
            content type that is neither of the two.
    """
    raw = await request.body()
    media_type = (
        (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    )

    if not raw:
        raise OAuthProtocolError("invalid_request", "A request body is required.")

    if media_type == FORM_MEDIA_TYPE:
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OAuthProtocolError(
                "invalid_request",
                "Form-encoded body must be UTF-8 (RFC 6749 §3.2).",
            ) from exc

        params: dict[str, Any] = {}
        for name, value in parse_qsl(decoded, keep_blank_values=True):
            if name in params:
                # RFC 6749 §3.1: a request parameter MUST NOT be sent more than
                # once. Silently taking the first or the last would let whoever
                # controls the duplicate choose which value the server reads.
                raise OAuthProtocolError(
                    "invalid_request",
                    f"Parameter '{_echo(name)}' is included more than once.",
                )
            params[name] = value
        return params

    if media_type in ("", "application/json") or (
        media_type.startswith("application/") and media_type.endswith("+json")
    ):
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise OAuthProtocolError(
                "invalid_request", "Request body is not valid JSON."
            ) from exc
        if not isinstance(payload, dict):
            raise OAuthProtocolError("invalid_request", "JSON body must be an object.")
        return payload

    raise OAuthProtocolError(
        "invalid_request",
        f"Unsupported Content-Type '{_echo(media_type)}'. Send "
        f"'{FORM_MEDIA_TYPE}' (RFC 6749 §3.2) or 'application/json'.",
        status_code=415,
    )


def _validate_oauth_params(
    model: Type[_OAuthRequestModel], params: dict[str, Any]
) -> _OAuthRequestModel:
    """Validate decoded parameters, reporting failures as `invalid_request`.

    RFC 6749 §5.2 has no structured slot for per-field errors, so the detail
    that made the old 422 diagnosable is folded into `error_description` rather
    than dropped. Only `loc` and `msg` are read: a Pydantic error's `input` can
    be any object, and rendering it is what turned a 422 into a 500 (#1048).
    """
    try:
        return model.model_validate(params)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'body'}: {error['msg']}"
            for error in exc.errors()
        )
        raise OAuthProtocolError(
            "invalid_request", details or "Request parameters failed validation."
        ) from exc


def _oauth_request_body(model: Type[BaseModel], description: str) -> dict[str, Any]:
    """The `openapi_extra` requestBody declaring both accepted encodings.

    Hand-written because the routes take a raw Request: FastAPI derives a
    requestBody from a `Body(...)` parameter, and there is no such parameter to
    derive it from. The schema is inlined rather than `$ref`-ed for the same
    reason — nothing else puts these models in `components.schemas`.
    """
    schema = model.model_json_schema()
    return {
        "requestBody": {
            "required": True,
            "description": description,
            "content": {
                FORM_MEDIA_TYPE: {"schema": schema},
                "application/json": {"schema": schema},
            },
        }
    }


_RFC6749_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {
        "model": OAuthErrorResponse,
        "description": (
            "RFC 6749 §5.2 error: `invalid_request`, `invalid_grant`, "
            "`unsupported_grant_type`, or `unsupported_token_type`."
        ),
    },
    415: {
        "model": OAuthErrorResponse,
        "description": (
            "Body is neither `application/x-www-form-urlencoded` nor "
            "`application/json`."
        ),
    },
}


# ============================================================
# Dependency Injection
# ============================================================


async def get_oauth_service(request: Request) -> IOAuthService:
    """Get OAuth service from app.state (Composition Root pattern).

    This dependency accesses the OAuth service via request.app.state,
    following the Composition Root principle (no Service Locator pattern).

    Returns:
        OAuth service instance

    Raises:
        HTTPException: If OAuth service not configured
    """
    # Use dependency injection from app.state (Composition Root)
    from faultmaven.api.v1.dependencies import get_oauth_service as get_oauth_dep

    oauth_service = await get_oauth_dep(request)
    if oauth_service is None:
        raise HTTPException(
            status_code=503, detail="OAuth authentication not configured"
        )
    return oauth_service


def _is_first_party(client_id: str, redirect_uri: str, settings: Any) -> bool:
    """Whether this client is one FaultMaven ships, and so skips consent.

    Takes the caller's settings rather than sourcing its own: the authorize
    endpoint already builds one, and a second instance from a different accessor
    is how two reads of "the same" configuration start disagreeing.

    BOTH halves are required, and only the second one proves anything.
    ``client_id`` is a caller-supplied string — an impostor extension presents
    ``faultmaven-copilot`` as easily as the real one does, so membership in
    ``oauth_first_party_clients`` narrows the field but identifies nobody.
    (Membership is still exact rather than prefix-matched: "starts with
    faultmaven-" would let an attacker widen even that by naming itself
    convincingly.) What an impostor cannot do is receive the code at OUR
    extension's redirect, because the browser derives that host from the
    extension's own id. So the skip turns on ``redirect_uri``, and on the
    deployment having pinned which redirect is ours.

    ``oauth_first_party_redirect_patterns`` is empty by default, so by default
    this returns False for every client and the consent screen renders as it
    always did. Skipping a prompt is not a thing to do on a guess: the failure
    is silent by construction, since what goes wrong is that nothing appears.

    ``re.match`` against ``^…$``-anchored patterns, matching how
    ``OAuthService._is_valid_redirect_uri`` reads its own list — two different
    match semantics over redirect patterns is how the consent decision and the
    access decision would start disagreeing about the same URI.
    """
    if client_id not in set(settings.auth.oauth_first_party_clients):
        return False

    return any(
        re.match(pattern, redirect_uri)
        for pattern in settings.auth.oauth_first_party_redirect_patterns
    )


# ============================================================
# OAuth 2.0 Endpoints
# ============================================================


@router.get(
    "/authorize",
    dependencies=[Depends(require_oauth_rate_limit_authorize)],
)
async def get_authorization_request(
    response_type: str = Query(..., description="Must be 'code'"),
    client_id: str = Query(..., description="OAuth client ID"),
    redirect_uri: str = Query(..., description="Extension callback URI"),
    state: str = Query(..., description="Client state for CSRF protection"),
    code_challenge: str = Query(..., description="PKCE code challenge"),
    code_challenge_method: str = Query(default="S256", description="PKCE method"),
    scope: str = Query(default="openid profile email", description="OAuth scopes"),
    user: DevUser = Depends(require_authentication),
    oauth_service: IOAuthService = Depends(get_oauth_service),
    request: Request = None,
):
    """OAuth 2.0 Authorization Endpoint (GET) - Consent Request.

    Returns authorization request details for user consent screen.
    If oauth_require_consent=false, auto-approves and returns code immediately.

    Production Flow (oauth_require_consent=true):
    1. User authenticates in Dashboard (dev-login or existing session)
    2. Extension redirects to GET /authorize with PKCE challenge
    3. Dashboard displays consent screen with AuthorizationConsentRequest
    4. User approves/denies → Dashboard POSTs to /authorize
    5. Backend generates code and redirects to Extension

    Dev/Test Flow (oauth_require_consent=false):
    1-2. Same as production
    3. Backend auto-approves and returns AuthorizationResponse immediately
    4. Dashboard redirects to Extension with code

    Args:
        response_type: Must be "code" (authorization code flow)
        client_id: OAuth client ID (validated against allowed clients)
        redirect_uri: Extension callback URI (validated against allowed patterns)
        state: Client state for CSRF protection (echoed back)
        code_challenge: PKCE code challenge (SHA256 of code_verifier)
        code_challenge_method: PKCE method (only "S256" supported)
        scope: OAuth scopes requested
        user: Authenticated user from Dashboard session
        oauth_service: OAuth service dependency
        request: FastAPI request object (for accessing settings)

    Returns:
        - AuthorizationConsentRequest (if consent required) - for Dashboard UI
        - AuthorizationResponse (if auto-approve) - immediate authorization code

    Raises:
        HTTPException: 400 if request invalid, 401 if user not authenticated
    """
    try:
        # Validate response_type
        if response_type != "code":
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported response_type: {response_type}. Only 'code' supported.",
            )

        auth_request = OAuthAuthorizationDTO(
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            scope=scope,
        )

        # Refuse an unknown client or an unlisted redirect target HERE, before a
        # consent screen exists for it (#1053).
        #
        # These checks also run at mint time, so this cannot reject anything that
        # would otherwise have completed — every flow that works today passes
        # them already. What changes is *when*: the consent leg used to return
        # 200 for a request the POST would refuse, which handed the dashboard an
        # attacker-chosen `redirect_uri` to navigate to on Cancel (the deny leg
        # leaves this origin; Approve does not). Refusing early also keeps
        # `client_name` below off the consent screen for clients we do not know.
        await oauth_service.validate_authorization_request(
            auth_request, user_id=user.user_id
        )

        # Get settings to check if consent is required
        from faultmaven.config.settings import FaultMavenSettings

        settings = FaultMavenSettings()

        # Decided once and reused below, so the branch that skips the screen and
        # the branch that logs why can never disagree about which one happened.
        first_party = _is_first_party(client_id, redirect_uri, settings)

        # If consent required, return consent request for Dashboard UI
        if settings.auth.oauth_require_consent and not first_party:
            logger.info(
                f"Authorization consent required for user {user.user_id}, client {client_id}"
            )

            # Map client_id to human-readable name. The fallback is reached only
            # by a client the validation above admitted, so it can name nothing
            # but an operator-configured `oauth_allowed_clients` entry — it is no
            # longer a caller-chosen string.
            client_names = {
                "faultmaven-copilot": "FaultMaven Copilot Browser Extension",
                "faultmaven-cli": "FaultMaven CLI Tool",
            }
            client_name = client_names.get(client_id, client_id)

            return AuthorizationConsentRequest(
                client_id=client_id,
                client_name=client_name,
                redirect_uri=redirect_uri,
                scope=scope,
                state=state,
                user_id=user.user_id,
                username=user.username,
            )

        if first_party:
            logger.info(
                f"First-party client {client_id} auto-approved for user "
                f"{user.user_id} (redirect {redirect_uri})"
            )
        else:
            # Consent globally disabled — dev/test only.
            logger.warning(
                f"AUTO-APPROVE enabled for user {user.user_id}, client {client_id} (dev/test only)"
            )

        # Generate authorization code using authenticated user's ID.
        #
        # ``user.organization_id`` is sourced from the request-scoped tenant
        # contextvar the global ``bind_request_org_context`` dependency resolved
        # (see ``get_current_user_optional``), so it is definitionally the org
        # this request is RLS-scoped to — not a raw, possibly-forged claim. It
        # rides with the code so the unauthenticated token exchange can mint from
        # it (#872).
        code = await oauth_service.create_authorization_code(
            user.user_id, auth_request, organization_id=user.organization_id
        )

        logger.info(
            f"Generated authorization code (auto-approved) for user {user.user_id}, client {client_id}"
        )

        return AuthorizationResponse(code=code, state=state)

    except InvalidRequestError as e:
        logger.warning(f"Invalid authorization request: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        # Re-raise HTTPException (don't convert to 500)
        raise
    except Exception as e:
        logger.error(f"Authorization error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post(
    "/authorize",
    response_model=AuthorizationResponse,
    dependencies=[Depends(require_oauth_rate_limit_authorize)],
)
async def post_authorization_approval(
    approval: AuthorizationApprovalRequest = Body(...),
    user: DevUser = Depends(require_authentication),
    oauth_service: IOAuthService = Depends(get_oauth_service),
) -> AuthorizationResponse:
    """OAuth 2.0 Authorization Endpoint (POST) - User Approval.

    Handles user's approval/denial of authorization request after consent screen.
    Generates authorization code if approved, returns error if denied.

    Flow:
    1. User reviews consent screen (GET /authorize returned AuthorizationConsentRequest)
    2. Dashboard submits user's decision (approved/denied) to this endpoint
    3. If approved: Generate authorization code and return for redirect
    4. If denied: Return error

    Args:
        approval: User's approval decision and authorization parameters
        user: Authenticated user from Dashboard session
        oauth_service: OAuth service dependency

    Returns:
        AuthorizationResponse with authorization code and state

    Raises:
        HTTPException: 400 if denied or invalid, 401 if user not authenticated
    """
    try:
        # Check if user denied the request
        if not approval.approved:
            logger.info(
                f"User {user.user_id} denied authorization for client {approval.client_id}"
            )
            raise HTTPException(
                status_code=400, detail="User denied authorization request"
            )

        # Create authorization request DTO
        auth_request = OAuthAuthorizationDTO(
            client_id=approval.client_id,
            redirect_uri=approval.redirect_uri,
            state=approval.state,
            code_challenge=approval.code_challenge,
            code_challenge_method=approval.code_challenge_method,
            scope=approval.scope,
        )

        # Generate authorization code using authenticated user's ID. The org is
        # the request's RLS-bound tenant — see the GET handler for why (#872).
        code = await oauth_service.create_authorization_code(
            user.user_id, auth_request, organization_id=user.organization_id
        )

        logger.info(
            f"Generated authorization code (user approved) for user {user.user_id}, client {approval.client_id}"
        )

        return AuthorizationResponse(code=code, state=approval.state)

    except InvalidRequestError as e:
        logger.warning(f"Invalid authorization approval: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Authorization approval error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post(
    "/token",
    response_model=TokenResponse,
    responses={
        **_RFC6749_ERROR_RESPONSES,
        500: {
            "model": OAuthErrorResponse,
            "description": "RFC 6749 §5.2 `server_error`.",
        },
    },
    dependencies=[Depends(require_oauth_rate_limit_token)],
    openapi_extra=_oauth_request_body(
        TokenRequest,
        "RFC 6749 §3.2 form encoding, or the same parameters as a JSON object.",
    ),
)
async def token(
    request: Request,
    response: Response,
    oauth_service: IOAuthService = Depends(get_oauth_service),
) -> Any:
    """OAuth 2.0 Token Endpoint.

    Accepts `application/x-www-form-urlencoded` (RFC 6749 §3.2) or
    `application/json`; errors are RFC 6749 §5.2 objects
    (`{"error": ..., "error_description": ...}`), not FastAPI's `detail` shape.

    Handles two grant types:
    1. authorization_code: Exchange authorization code for access/refresh tokens
    2. refresh_token: Refresh access token using refresh token

    Authorization Code Flow:
    1. Extension calls /authorize and gets authorization code
    2. Extension exchanges code + verifier for tokens
    3. Extension stores tokens securely
    4. Extension uses access token for API requests

    Refresh Token Flow:
    1. Extension detects access token expiring soon (< 5 minutes)
    2. Extension exchanges refresh token for new access token
    3. Extension gets new access token + rotated refresh token
    4. Extension updates stored tokens

    Args:
        request: Raw request; the body is parsed per its content type
        response: Used to attach the RFC 6749 §5.1 no-store headers
        oauth_service: OAuth service dependency

    Returns:
        TokenResponse on success; an RFC 6749 §5.2 error body otherwise
        (400 invalid_request / invalid_grant / unsupported_grant_type,
        415 unsupported content type, 500 server_error).
    """
    try:
        params = await _read_oauth_params(request)

        # Checked before model validation so an unknown grant answers
        # `unsupported_grant_type` (RFC 6749 §5.2) instead of being reported as
        # a bad value for a field.
        grant_type = params.get("grant_type")
        if grant_type is None:
            raise OAuthProtocolError(
                "invalid_request", "Missing required parameter: grant_type."
            )
        if grant_type not in SUPPORTED_GRANT_TYPES:
            raise OAuthProtocolError(
                "unsupported_grant_type",
                f"Unsupported grant_type '{_echo(grant_type)}'. Supported: "
                + ", ".join(SUPPORTED_GRANT_TYPES)
                + ".",
            )

        token_request = _validate_oauth_params(TokenRequest, params)

        if token_request.grant_type == "authorization_code":
            for parameter in ("code", "redirect_uri", "code_verifier"):
                if not getattr(token_request, parameter):
                    raise OAuthProtocolError(
                        "invalid_request",
                        f"Missing required parameter: {parameter}.",
                    )

            # Exchange authorization code for tokens
            token_dto = await oauth_service.exchange_code_for_token(
                code=token_request.code,
                code_verifier=token_request.code_verifier,
                redirect_uri=token_request.redirect_uri,
            )

            logger.info(
                f"Exchanged authorization code for tokens (user: {token_dto.user_id})"
            )

        elif token_request.grant_type == "refresh_token":
            if not token_request.refresh_token:
                raise OAuthProtocolError(
                    "invalid_request", "Missing required parameter: refresh_token."
                )

            # Refresh access token
            token_dto = await oauth_service.refresh_access_token(
                refresh_token=token_request.refresh_token,
                client_id=token_request.client_id,
            )

            logger.info(f"Refreshed access token (user: {token_dto.user_id})")

        else:
            # Reachable only if GrantType gains a member without a branch here:
            # the membership check above has already answered every value that
            # is not in the Literal.
            raise OAuthProtocolError(
                "unsupported_grant_type",
                f"grant_type '{token_request.grant_type}' is accepted but not "
                "implemented.",
            )

    except OAuthProtocolError:
        # Already the client's answer: re-raise for the RFC renderer.
        raise
    except InvalidRequestError as e:
        logger.warning(f"Invalid token request: {e}")
        raise OAuthProtocolError("invalid_request", str(e)) from e
    except InvalidGrantError as e:
        # RFC 6749 §5.2 puts invalid_grant at 400. It was a 401 before this
        # endpoint had an RFC error body to carry the code in.
        #
        # The message crosses to the client because these are curated, literal,
        # caller-facing strings ("PKCE verification failed", "Authorization code
        # expired") — the 4xx domain-message case the leak guard deliberately
        # allows. The 5xx arm below carries nothing from its exception.
        logger.warning(f"Invalid grant: {e}")
        raise OAuthProtocolError("invalid_grant", str(e)) from e
    except Exception as e:
        logger.error(f"Token endpoint error: {e}")
        raise OAuthProtocolError("server_error", "Internal server error.", 500) from e

    # RFC 6749 §5.1: the body carries fresh credentials.
    response.headers.update(_NO_STORE_HEADERS)

    # Convert DTO to response model
    return TokenResponse(
        access_token=token_dto.access_token,
        refresh_token=token_dto.refresh_token,
        token_type=token_dto.token_type,
        expires_in=token_dto.expires_in,
        refresh_expires_in=token_dto.refresh_expires_in,
        user_id=token_dto.user_id,
        username=token_dto.username,
    )


@router.post(
    "/revoke",
    status_code=200,
    responses={
        **_RFC6749_ERROR_RESPONSES,
        503: {
            "model": OAuthErrorResponse,
            "description": "Revocation could not be recorded (RFC 7009 §2.2.1).",
        },
    },
    dependencies=[Depends(require_oauth_rate_limit_revoke)],
    openapi_extra=_oauth_request_body(
        RevokeRequest,
        "RFC 7009 §2.1 form encoding, or the same parameters as a JSON object.",
    ),
)
async def revoke(
    request: Request,
    response: Response,
    oauth_service: IOAuthService = Depends(get_oauth_service),
) -> Any:
    """OAuth 2.0 Token Revocation Endpoint.

    Revokes access tokens or refresh tokens (for logout). Accepts
    `application/x-www-form-urlencoded` (RFC 7009 §2.1) or `application/json`;
    errors use the RFC 6749 §5.2 shape that RFC 7009 §2.2.1 refers to.

    When to revoke:
    - User logs out: Revoke both access and refresh tokens
    - Security event: Revoke all tokens for user
    - Token rotation: Old refresh token revoked automatically

    Args:
        request: Raw request; the body is parsed per its content type
        response: Used to attach the no-store headers
        oauth_service: OAuth service dependency

    Returns:
        Empty object (200 OK per RFC 7009), or an RFC 6749 §5.2 error body.

    Note: Returns 200 even if token doesn't exist (per RFC 7009)
    """
    try:
        params = await _read_oauth_params(request)

        # RFC 7009 §2.2.1 gives an unrecognised hint its own error code, so it
        # is answered before the model reports it as a bad field value.
        hint = params.get("token_type_hint")
        if hint is not None and hint not in SUPPORTED_TOKEN_TYPE_HINTS:
            raise OAuthProtocolError(
                "unsupported_token_type",
                f"Unsupported token_type_hint '{_echo(hint)}'. Supported: "
                + ", ".join(SUPPORTED_TOKEN_TYPE_HINTS)
                + ".",
            )

        revoke_request = _validate_oauth_params(RevokeRequest, params)

        # Determine token type
        token_type = revoke_request.token_type_hint or "access_token"

        if token_type == "refresh_token":
            await oauth_service.revoke_refresh_token(revoke_request.token)
            logger.info(f"Revoked refresh token for client {revoke_request.client_id}")
        else:
            await oauth_service.revoke_token(revoke_request.token)
            logger.info(f"Revoked access token for client {revoke_request.client_id}")

    except OAuthProtocolError:
        raise
    except Exception as e:
        logger.error(f"Token revocation error: {e}")
        # Invalid/unknown tokens are already treated as success inside the
        # generator (RFC 7009), so an exception here means the revocation was
        # NOT recorded (e.g. store outage). Surface it (RFC 7009 §2.2.1
        # permits 503) so the client retries instead of believing the token
        # is dead.
        raise OAuthProtocolError(
            "temporarily_unavailable",
            "Token revocation temporarily unavailable. Please retry.",
            503,
        ) from e

    response.headers.update(_NO_STORE_HEADERS)

    # Per RFC 7009, return 200 OK even if token invalid/not found
    return {}
