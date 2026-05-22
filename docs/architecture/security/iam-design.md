# FaultMaven Authentication System Design

## Overview

This document defines the **authoritative authentication architecture** for FaultMaven. It serves as the single source of truth for frontend and backend implementation, ensuring secure user identity management while maintaining clean separation between authentication (user identity) and sessions (conversation state).

## Design Principles

### Core Philosophy

FaultMaven uses a **dual-header approach** that cleanly separates concerns:

- **Authentication**: `Authorization: Bearer <token>` for user identity
- **Session Management**: `X-Session-Id: <session_id>` for conversation continuity

### Key Design Goals

1. **Deployment Agnostic**: Core business logic is unaware of authentication mode
2. **Unified Token Format**: JWT tokens in all deployment modes for middleware uniformity
3. **Clean Frontend Integration**: Single discovery endpoint determines auth flow
4. **Browser Extension Optimized**: Designed for multi-tab, persistent extension usage
5. **Secure by Default**: Tokens expire, proper error handling, secure storage
6. **Production Ready**: OAuth 2.0 with PKCE for multi-user deployments

## Authentication Modes

FaultMaven supports two authentication strategies, selected at deployment time:

### Local Mode (Self-Hosted / Single-User)

For users running FaultMaven locally or on their own infrastructure.

| Aspect | Specification |
|--------|---------------|
| **Authentication** | `/api/v1/auth/login` endpoint |
| **User Input** | Username (optional password for enhanced security) |
| **Token Format** | JWT signed with local secret key |
| **User Storage** | SQLite (local file) |
| **Use Case** | Single-user self-hosted deployments |
| **Registration** | `/api/v1/auth/register` for account creation |

### Cloud Mode (Multi-User / SaaS)

For FaultMaven Cloud or enterprise on-premises deployments.

| Aspect | Specification |
|--------|---------------|
| **Authentication** | OAuth 2.0 Authorization Code Flow with PKCE |
| **Identity Provider** | Dashboard acts as IdP for Extension |
| **Token Format** | JWT signed with RS256 (asymmetric key) |
| **User Storage** | PostgreSQL with proper user management |
| **Use Case** | Multi-user production deployments |

### Strategy Selection

Authentication strategy is determined by configuration at startup:

```yaml
# Local Mode (default for self-hosted)
auth:
  mode: local
  jwt_algorithm: HS256
  jwt_secret_key: ${JWT_SECRET_KEY}

# Cloud Mode (SaaS / Enterprise)
auth:
  mode: oauth
  jwt_algorithm: RS256
  jwt_private_key_path: /secrets/jwt_private.pem
  jwt_public_key_path: /secrets/jwt_public.pem
  oauth:
    dashboard_url: https://dashboard.faultmaven.ai
```

## Frontend Integration

### Auth Configuration Discovery

The frontend uses a single discovery endpoint to determine which authentication flow to use. This keeps deployment-specific logic out of the frontend codebase.

**Endpoint:** `GET /api/v1/auth/config`

```json
// Response for Local Mode
{
  "auth_mode": "local",
  "login_endpoint": "/api/v1/auth/login",
  "register_endpoint": "/api/v1/auth/register",
  "supports_registration": true,
  "oauth": null
}

// Response for Cloud Mode
{
  "auth_mode": "oauth",
  "login_endpoint": null,
  "register_endpoint": null,
  "supports_registration": false,
  "oauth": {
    "authorize_url": "/auth/oauth/authorize",
    "token_url": "/auth/oauth/token",
    "client_id": "faultmaven-copilot",
    "scopes": ["openid", "profile", "email", "cases:read", "cases:write"]
  }
}
```

**Frontend Implementation Pattern:**

```typescript
// src/lib/auth/auth-client.ts
interface IAuthClient {
  signIn(): Promise<AuthResult>;
  signOut(): Promise<void>;
  getAccessToken(): Promise<string>;
}

class AuthClientFactory {
  static async create(apiBase: string): Promise<IAuthClient> {
    const config = await fetch(`${apiBase}/api/v1/auth/config`).then(r => r.json());

    if (config.auth_mode === 'local') {
      return new LocalAuthClient(config);
    } else {
      return new OAuthClient(config);
    }
  }
}

// Usage - frontend code is deployment-agnostic
const authClient = await AuthClientFactory.create(API_BASE);
await authClient.signIn();  // Works for both local and cloud
```

This pattern ensures:
- Frontend has **zero hardcoded deployment logic**
- Auth mode is determined at runtime from backend config
- Same frontend bundle works for local and cloud deployments

## Token Architecture

### Unified JWT Format

**Critical Design Decision:** Both Local and Cloud modes use JWT tokens with identical structure. This ensures middleware and protected endpoints require no conditional logic.

**JWT Payload Structure:**

```json
{
  "sub": "user_abc123",
  "username": "alice",
  "email": "alice@example.com",
  "roles": ["user", "admin"],
  "scopes": ["openid", "profile", "email", "cases:read", "cases:write", "knowledge:read"],
  "organization_id": "org_default",
  "iat": 1706140800,
  "exp": 1706144400,
  "iss": "faultmaven",
  "aud": "faultmaven-api",
  "jti": "550e8400-e29b-41d4-a716-446655440000",
  "type": "access",
  "auth_mode": "local"
}
```

Both `HS256JWTTokenGenerator` (local mode) and `RS256JWTTokenGenerator` (cloud/OAuth mode) produce identical claim sets. The only difference is the signing algorithm and the `auth_mode` value (`"local"` vs `"oauth"`).

**Token Types:**

| Token | Lifetime | Purpose | Storage |
|-------|----------|---------|---------|
| Access Token | 1 hour | API authentication | Extension: `chrome.storage.local` |
| Refresh Token | 7 days | Obtain new access tokens | Extension: `chrome.storage.local` |

**Signing Algorithms:**

| Mode | Algorithm | Key Management |
|------|-----------|----------------|
| Local | HS256 | Symmetric key in `JWT_SECRET_KEY` env var |
| Cloud | RS256 | Asymmetric keypair, private key secured |

### Token Validation Middleware

Because tokens are uniformly JWT, validation middleware is identical for both modes:

```python
# faultmaven/api/middleware/auth.py
async def validate_token(token: str) -> TokenClaims:
    """Validate JWT token - works for both Local and Cloud modes."""
    try:
        # Decode and validate JWT
        claims = jwt.decode(
            token,
            key=get_verification_key(),  # Returns symmetric or public key based on config
            algorithms=[settings.auth.jwt_algorithm],
            audience="faultmaven-api",
            issuer="faultmaven"
        )
        return TokenClaims(**claims)
    except jwt.ExpiredSignatureError:
        raise AuthenticationError("Token expired")
    except jwt.InvalidTokenError:
        raise AuthenticationError("Invalid token")
```

## Local Mode Authentication

### Endpoint Design

Local Mode uses simple form-based authentication with JWT token issuance.

**Login Endpoint:** `POST /api/v1/auth/login`

```http
POST /api/v1/auth/login
Content-Type: application/json

{
  "username": "alice",
  "password": "optional-password"
}
```

**Response (200 OK):**

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "Bearer",
  "expires_in": 3600,
  "refresh_expires_in": 604800,
  "session_id": "session-41afd36b-3f3c-46dd-8794-1565984d843d",
  "user": {
    "user_id": "user_f939a782",
    "username": "alice",
    "email": "alice@example.com",
    "display_name": "Alice Smith",
    "roles": ["user", "admin"],
    "auth_mode": "local",
    "created_at": "2025-10-23T12:00:00Z"
  }
}
```

**Registration Endpoint:** `POST /api/v1/auth/register`

```http
POST /api/v1/auth/register
Content-Type: application/json

{
  "username": "alice",
  "email": "alice@example.com",
  "display_name": "Alice Smith",
  "password": "optional-password"
}
```

**Response (201 Created):** Same structure as login response.

### Error Responses (Login + Register)

Both endpoints share a common error-response contract sourced from the
service-layer typed exceptions and dispatched by the global handlers in
`api/exception_handlers.py`. Each shape maps to a single HTTP status:

| Status | Trigger | Service-layer exception |
|--------|---------|-------------------------|
| **401 Unauthorized** | User does not exist (login only) | route-raised `HTTPException(401)` |
| **404 Not Found** | User-id lookup miss on update flows | `NotFoundError(resource_type="user", resource_id=...)` |
| **409 Conflict** | Username or email already exists | `ConflictError(resource_type="user", conflict_reason="duplicate_username" \| "duplicate_email")` |
| **422 Unprocessable Entity** | Invalid username or email format | `ValidationException(...)` |
| **500 Internal Server Error** | Unforeseen failure | re-wrapped via blanket `except Exception` |

The 409 response carries `resource_type` / `resource_id` /
`conflict_reason` in the response body so clients can distinguish
duplicate-username from duplicate-email programmatically without
parsing the human-readable message.

Routes do **not** catch `ValueError`. Service layers (`DatabaseUserStore`,
`RedisUserStore`) raise the typed exceptions directly; the global
handlers translate them. The route's `try/except` block contains a
`FaultMavenException` pass-through ahead of the blanket `except
Exception` so typed exceptions reach the handlers instead of being
re-wrapped as 500. See the
[service exception contract specification](../specifications/exception-contract.md)
for the full mapping, route pattern, and the legacy anti-pattern
this contract replaces.

### Local Mode Security

Even in Local Mode, security best practices apply:

| Security Measure | Implementation |
|-----------------|----------------|
| **JWT Tokens** | HS256-signed JWTs (not UUIDs) |
| **Token Expiry** | Access: 1 hour, Refresh: 7 days |
| **Password Support** | Optional bcrypt-hashed password |
| **Rate Limiting** | 10 login attempts per minute per IP |
| **HTTPS** | Recommended even for localhost |

### Organization Context

FaultMaven JWT tokens include an `organization_id` claim that determines the user's organization context:

| Mode | Organization Strategy |
|------|----------------------|
| **Local Mode** | All users belong to default organization `00000000-0000-0000-0000-000000000001` (managed by `SingleTenantProvider`) |
| **Cloud Mode** | Users can belong to multiple organizations; `organization_id` represents active context |

**JWT Payload Structure (all modes):**

```json
{
  "sub": "user_abc123",
  "username": "alice",
  "organization_id": "00000000-0000-0000-0000-000000000001",
  "email": "alice@example.com",
  "roles": ["user"],
  "scopes": ["cases:read", "cases:write"],
  "exp": 1708012800,
  "iat": 1708009200,
  "type": "access",
  "auth_mode": "local"
}
```

> [!IMPORTANT]
> The `organization_id` claim is **always present** in both Local and Cloud mode tokens. Services can safely assume `AuthenticatedUser.organization_id` is never empty.

### Environment-Based Endpoint Exposure

Certain endpoints are only available in specific environments:

```python
# Endpoint visibility by auth mode
ENDPOINT_VISIBILITY = {
    "/api/v1/auth/login":    ["local"],           # Local mode only
    "/api/v1/auth/register": ["local"],           # Local mode only
    "/auth/oauth/authorize": ["oauth"],           # Cloud mode only
    "/auth/oauth/token":     ["oauth"],           # Cloud mode only
    "/api/v1/auth/config":   ["local", "oauth"],  # Always available
    "/api/v1/auth/me":       ["local", "oauth"],  # Always available
    "/api/v1/auth/logout":   ["local", "oauth"],  # Always available
}

# Debug endpoints — development only (no auth, internal topology exposed)
DEVELOPMENT_ONLY_ENDPOINTS = [
    "/debug/routes",
    "/debug/health",
    "/debug/config",
    "/debug/llm-providers",
]

# Admin-only endpoints — always registered, gated by require_admin
ADMIN_ENDPOINTS = [
    # User management (auth module)
    "/api/v1/auth/users",                        # GET: list all users
    "/api/v1/auth/users/{username}",             # DELETE: remove user
    "/api/v1/auth/users/{user_id}/revoke-tokens", # POST: revoke all tokens for user
    # Platform admin (admin module)
    "/api/v1/admin/users",                       # GET: user details
    "/api/v1/admin/users/{user_id}/roles",       # POST: assign role
    "/api/v1/admin/llm/config",                  # GET: LLM provider status
    "/api/v1/admin/llm/config/test",             # POST: test provider connection
    "/api/v1/admin/config/status",               # GET: env configuration status
]
```

**Implementation:**

```python
def create_app(config: AppConfig) -> FastAPI:
    app = FastAPI()

    # Register auth endpoints based on mode
    if config.auth.mode == "local":
        app.include_router(local_auth_router)
    else:
        app.include_router(oauth_router)

    # Always register common endpoints
    app.include_router(common_auth_router)

    # User management and admin routes — always registered, gated by require_admin
    app.include_router(admin_users_router)   # User management (admin only)
    app.include_router(admin_config_router)  # LLM config + env status (authenticated)

    return app
```

## Cloud Mode Authentication (OAuth 2.0)

### Why Dashboard-Centric Authentication?

**Security Benefits:**

1. **No Credentials in Extension**: Extension never handles passwords or MFA codes
2. **Simplified Extension**: No complex authentication UI in extension
3. **Centralized Auth Logic**: All authentication flows (social login, MFA, SSO) in one place
4. **Better User Experience**: Users see familiar dashboard login UI
5. **Easier Updates**: Authentication changes don't require extension updates

**Architecture Benefits:**

- Dashboard handles complex OAuth flows (Google, GitHub, Microsoft)
- Dashboard implements MFA, password reset, email verification
- Extension remains lightweight and focused on core functionality
- Backend issues standard JWT tokens for both dashboard and extension

### Complete OAuth Flow

```mermaid
sequenceDiagram
    participant User
    participant Ext as FaultMaven Copilot (Extension)
    participant Dash as FaultMaven Dashboard (Web)
    participant API as Backend API

    Note over Ext, API: Phase 1: Initiation
    User->>Ext: Clicks "Sign In"
    Ext->>Ext: Generate PKCE Verifier & Challenge<br/>verifier = random(43-128 chars)<br/>challenge = SHA256(verifier)
    Ext->>Ext: Save state + verifier in storage
    Ext->>Dash: Open new tab: /auth/authorize?<br/>client_id=copilot<br/>&redirect_uri=chrome-extension://...<br/>&code_challenge=SHA256_HASH<br/>&code_challenge_method=S256<br/>&state=RANDOM_STATE

    Note over Dash, API: Phase 2: User Login
    Dash->>User: Show Login UI (email/password, social, MFA)
    User->>Dash: Enter Credentials
    Dash->>API: POST /auth/login {credentials}
    API->>API: Validate Credentials
    API-->>Dash: {user_id, session}
    Dash->>API: POST /auth/authorize {user_id, client_id, code_challenge}
    API->>API: Generate authorization code<br/>Store: code → {user_id, challenge, expires}
    API-->>Dash: {authorization_code}
    Dash-->>User: Redirect to chrome-extension://.../callback?<br/>code=AUTH_CODE<br/>&state=RANDOM_STATE
    User->>Ext: Browser redirects to extension

    Note over Ext, API: Phase 3: Token Exchange
    Ext->>Ext: Verify state matches saved state
    Ext->>Ext: Retrieve code_verifier from storage
    Ext->>API: POST /auth/token {<br/>code: AUTH_CODE,<br/>code_verifier: ORIGINAL_VERIFIER,<br/>client_id: copilot,<br/>redirect_uri: chrome-extension://...<br/>}
    API->>API: Validate authorization code<br/>Verify SHA256(verifier) == stored challenge<br/>Check code not expired/used<br/>Validate redirect_uri matches
    API-->>Ext: {<br/>access_token: JWT,<br/>token_type: Bearer,<br/>expires_in: 3600,<br/>refresh_token: JWT,<br/>refresh_expires_in: 604800,<br/>session_id: SESSION_ID,<br/>user: {...}<br/>}
    Ext->>Ext: Store access_token & session_id
    Ext->>Ext: Clear PKCE verifier & state
    Ext->>API: API calls with dual headers<br/>Authorization: Bearer JWT<br/>X-Session-Id: SESSION_ID
```

### PKCE (Proof Key for Code Exchange)

#### Why PKCE is Required

- Browser extensions are **public clients** - they cannot securely store a `client_secret`
- Authorization codes can be intercepted by malicious apps monitoring browser redirects
- PKCE proves that the app exchanging the code is the same app that initiated the flow

#### How PKCE Works

1. Extension generates random `code_verifier` (43-128 characters)
2. Extension computes `code_challenge = SHA256(code_verifier)`
3. Extension sends `code_challenge` in authorization request
4. Backend stores `code_challenge` with authorization code
5. Extension sends original `code_verifier` when exchanging code for token
6. Backend verifies `SHA256(code_verifier)` matches stored `code_challenge`

#### Security Properties

- Even if authorization code is intercepted, attacker cannot exchange it without the `code_verifier`
- `code_verifier` never leaves the extension until token exchange
- Backend cryptographically verifies the extension's identity

#### PKCE Implementation

**Extension: Generate PKCE Parameters**

```typescript
// src/lib/auth/pkce.ts
import { createHash, randomBytes } from 'crypto';

export class PKCEGenerator {
  /**
   * Generate PKCE code verifier (43-128 characters, base64url-encoded)
   */
  static generateCodeVerifier(): string {
    const verifier = randomBytes(32)
      .toString('base64')
      .replace(/\+/g, '-')
      .replace(/\//g, '_')
      .replace(/=/g, '');
    return verifier; // 43 characters
  }

  /**
   * Generate PKCE code challenge (SHA256 hash of verifier)
   */
  static generateCodeChallenge(verifier: string): string {
    const hash = createHash('sha256')
      .update(verifier)
      .digest('base64')
      .replace(/\+/g, '-')
      .replace(/\//g, '_')
      .replace(/=/g, '');
    return hash;
  }

  /**
   * Generate state parameter for CSRF protection
   */
  static generateState(): string {
    return randomBytes(16).toString('hex'); // 32 characters
  }
}
```

**Backend: Verify PKCE**

```python
# faultmaven/modules/auth/domain/services/pkce.py
import hashlib
import base64
import hmac

def verify_pkce(code_verifier: str, code_challenge: str) -> bool:
    """Verify PKCE code_verifier matches code_challenge using constant-time comparison."""
    verifier_bytes = code_verifier.encode('utf-8')
    computed_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier_bytes).digest()
    ).decode('utf-8').rstrip('=')

    # Constant-time comparison to prevent timing attacks
    return hmac.compare_digest(computed_challenge, code_challenge)
```

### OAuth Endpoints

#### Authorization Endpoint

**GET** `/auth/oauth/authorize` - Display consent screen

```http
GET /auth/oauth/authorize
  ?client_id=faultmaven-copilot
  &redirect_uri=chrome-extension://{extension_id}/callback
  &code_challenge={sha256_hash}
  &code_challenge_method=S256
  &state={random_state}
  &scope=openid profile email cases:read cases:write
```

**Response:** Consent page HTML or auto-redirect with authorization code.

**POST** `/auth/oauth/authorize` - Submit consent approval

```http
POST /auth/oauth/authorize
Content-Type: application/json

{
  "approved": true,
  "client_id": "faultmaven-copilot",
  "redirect_uri": "chrome-extension://{extension_id}/callback",
  "code_challenge": "{sha256_hash}",
  "code_challenge_method": "S256",
  "scope": "openid profile email cases:read cases:write",
  "state": "{random_state}"
}
```

**Response:** Redirect to `redirect_uri` with authorization code.

#### Token Endpoint

**POST** `/auth/oauth/token` - Exchange code for tokens

```http
POST /auth/oauth/token
Content-Type: application/json

{
  "grant_type": "authorization_code",
  "code": "{authorization_code}",
  "code_verifier": "{original_verifier}",
  "client_id": "faultmaven-copilot",
  "redirect_uri": "chrome-extension://{extension_id}/callback"
}
```

**Response (200 OK):**

```json
{
  "access_token": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9...",
  "refresh_token": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "Bearer",
  "expires_in": 3600,
  "refresh_expires_in": 604800,
  "session_id": "session-xyz",
  "user": {
    "user_id": "user_123",
    "username": "alice",
    "email": "alice@example.com",
    "display_name": "Alice Smith",
    "roles": ["user", "admin"],
    "auth_mode": "oauth"
  }
}
```

#### Token Refresh

**POST** `/auth/oauth/token` - Refresh access token

```http
POST /auth/oauth/token
Content-Type: application/json

{
  "grant_type": "refresh_token",
  "refresh_token": "{refresh_token}",
  "client_id": "faultmaven-copilot"
}
```

**Response (200 OK):**

```json
{
  "access_token": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9...",
  "refresh_token": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "Bearer",
  "expires_in": 3600,
  "refresh_expires_in": 604800
}
```

## Security Design

### 1. State Parameter (CSRF Protection)

**Purpose:** Prevent CSRF attacks during OAuth redirect

**Implementation:**

- Extension generates random `state` parameter before redirect
- Extension stores `state` in `chrome.storage.local`
- Dashboard includes `state` in redirect URL
- Extension verifies returned `state` matches stored value

**Attack Prevented:** Malicious site cannot trick user into authorizing extension without knowing the `state` value

### 2. Authorization Code Properties

| Property | Value | Purpose |
|----------|-------|---------|
| **Single-Use** | Code deleted after exchange | Prevents replay attacks |
| **Short-Lived** | 10 minute TTL | Limits exposure window |
| **Client-Bound** | Validates client_id and redirect_uri | Prevents code injection |
| **PKCE Protected** | Requires correct code_verifier | Proves client identity |

### 3. Redirect URI Validation

#### Extension ID Registration

The backend validates redirect URIs using a pre-registered allowlist to prevent authorization code injection attacks.

#### Current implementation (production)

A flat allowlist driven by settings, plus a single global redirect-URI checker:

- `settings.oauth_allowed_clients` — flat list of accepted `client_id` values.
- `OAuthService._is_redirect_uri_allowed(redirect_uri)` — validates the redirect URI against the configured patterns (one global rule set; not per-client policy).
- `OAuthService.create_authorization_code()` rejects any request whose `client_id` is absent from the allowlist (`INVALID_CLIENT`) or whose `redirect_uri` fails the global check (`INVALID_REDIRECT_URI`).

This is sufficient for single-client (browser-extension) deployments today.

#### Planned upgrade — per-client policy via `OAuthClientRegistry`

> **Status: Planned (not yet implemented).** The registry below describes the target design — per-client `redirect_uris`, `allowed_scopes`, and `client_type` ("public" / "confidential"), loaded from `config/oauth_clients.yml`. None of these classes or that config file exist in the current code; the implementation will replace the flat allowlist when shipped.

**Configuration (planned):** `config/oauth_clients.yml`

```yaml
oauth_clients:
  faultmaven-copilot:
    client_id: faultmaven-copilot
    client_type: public
    redirect_uris:
      # Chrome Web Store (Production)
      - chrome-extension://abcdefghijklmnopqrstuvwxyz123456/callback
      # Chrome Web Store (Beta)
      - chrome-extension://bcdefghijklmnopqrstuvwxyz123456a/callback
      # Firefox Add-ons (Production)
      - moz-extension://12345678-1234-1234-1234-123456789abc/callback
      # Local Development (unpacked extension) - development only
      - chrome-extension://*/callback
    allowed_scopes:
      - openid
      - profile
      - email
      - cases:read
      - cases:write
      - knowledge:read
      - evidence:read
```

**Implementation (planned):**

```python
# Planned location: faultmaven/modules/auth/domain/services/oauth_client_registry.py
from typing import Dict, List
import re
from pydantic import BaseModel

class OAuthClientConfig(BaseModel):
    client_id: str
    client_type: str  # "public" or "confidential"
    redirect_uris: List[str]
    allowed_scopes: List[str]

class OAuthClientRegistry:
    """Registry of authorized OAuth clients with redirect URI validation."""

    def __init__(self, config: Dict[str, OAuthClientConfig]):
        self.clients = config

    def validate_redirect_uri(self, client_id: str, redirect_uri: str) -> bool:
        """Validate redirect URI against registered patterns."""
        if client_id not in self.clients:
            return False

        allowed_uris = self.clients[client_id].redirect_uris

        for allowed in allowed_uris:
            if allowed.endswith('*'):
                # Wildcard pattern (dev mode only)
                if settings.environment == Environment.DEVELOPMENT:
                    pattern = allowed.replace('*', '[a-z]{32}')
                    if re.match(pattern, redirect_uri):
                        return True
            elif allowed == redirect_uri:
                # Exact match
                return True

        return False

    def get_allowed_scopes(self, client_id: str) -> List[str]:
        """Get allowed scopes for client."""
        if client_id not in self.clients:
            return []
        return self.clients[client_id].allowed_scopes
```

**Security Properties (target):**

- Only pre-registered extension IDs can receive authorization codes (per-client policy, not a single global rule set)
- Prevents authorization code injection attacks
- Supports multiple browser extension stores (Chrome, Firefox, Edge) with per-client allowed-scope governance
- Wildcard patterns only allowed in development mode
- Exact match required in production (no regex vulnerabilities)

### 4. Token Security

**Token Lifecycle:**

| Token | Lifetime | Rotation | Revocation |
|-------|----------|----------|------------|
| Access Token | 1 hour | N/A | Immediate on logout |
| Refresh Token | 7 days | On each use | Immediate on logout |

**Storage Security:**

| Context | Storage Location | Security Properties |
|---------|------------------|---------------------|
| Extension | `chrome.storage.local` | Isolated per-extension, not synced |
| Dashboard | `httpOnly` cookie | XSS-protected, SameSite=Strict |
| Backend | JWT (stateless) | No server-side token storage needed |

#### Critical: Extension Token Storage

Refresh tokens **MUST** be stored in `chrome.storage.local` only. Never store in:

| Storage Type | Risk Level | Why Unsafe |
|--------------|------------|------------|
| `chrome.storage.sync` | **CRITICAL** | Tokens transmitted to Google servers |
| `localStorage` | **HIGH** | Accessible to content scripts (XSS) |
| `sessionStorage` | **HIGH** | Lost on restart, accessible to content scripts |
| `document.cookie` | **CRITICAL** | Transmitted with HTTP requests |
| URL parameters | **CRITICAL** | Logged in browser history, server logs |

**Correct Implementation:**

```typescript
// CORRECT: Store in chrome.storage.local (background script context)
async function storeTokens(tokens: AuthTokens) {
  await chrome.storage.local.set({
    access_token: tokens.access_token,
    refresh_token: tokens.refresh_token,
    expires_at: Date.now() + (tokens.expires_in * 1000),
    refresh_expires_at: Date.now() + (tokens.refresh_expires_in * 1000)
  });
}
```

### 5. Rate Limiting

OAuth endpoints are rate-limited to prevent abuse:

| Endpoint | Limit | Window | Scope |
|----------|-------|--------|-------|
| `/auth/oauth/authorize` | 10 requests | 1 minute | Per IP |
| `/auth/oauth/token` | 5 requests | 1 minute | Per IP |
| `/auth/oauth/revoke` | 20 requests | 1 minute | Per IP |
| `/api/v1/auth/login` | 10 requests | 1 minute | Per IP (via global rate limiter — no dedicated per-endpoint limit yet) |

**Response on limit exceeded:** `429 Too Many Requests` with `Retry-After` header.

### 6. HTTPS Enforcement

In production, redirect URIs must use secure schemes:

| Scheme | Allowed in Production | Allowed in Development |
|--------|----------------------|------------------------|
| `https://` | Yes | Yes |
| `chrome-extension://` | Yes | Yes |
| `moz-extension://` | Yes | Yes |
| `http://` | **No** | Yes (localhost only) |

**Configuration:**

```yaml
oauth:
  require_https_redirect: true  # Production default
  # Set to false for local development with http://localhost
```

## OAuth Scopes and Permissions

OAuth scopes control what resources the access token can access.

### Supported Scopes

| Scope | Permissions Granted | Description |
|-------|---------------------|-------------|
| `openid` | `read:user_id` | Access to user's unique identifier |
| `profile` | `read:user_profile` | Access to username, display_name |
| `email` | `read:user_email` | Access to user's email address |
| `cases:read` | `read:cases`, `list:cases`, `search:cases` | Read access to cases |
| `cases:write` | `create:cases`, `update:cases`, `delete:cases` | Write access to cases |
| `knowledge:read` | `read:knowledge`, `search:knowledge` | Read access to knowledge base |
| `knowledge:write` | `create:knowledge`, `update:knowledge`, `delete:knowledge` | Write access to knowledge base |
| `evidence:read` | `read:evidence`, `list:evidence` | Read access to evidence files |
| `evidence:write` | `upload:evidence`, `delete:evidence` | Write access to evidence files |

### Scope Validation

```python
# faultmaven/modules/auth/domain/services/scope_validator.py
from typing import List, Set

class ScopeValidator:
    """Validates OAuth scopes and checks permissions."""

    SCOPE_PERMISSIONS = {
        "openid": {"read:user_id"},
        "profile": {"read:user_profile"},
        "email": {"read:user_email"},
        "cases:read": {"read:cases", "list:cases", "search:cases"},
        "cases:write": {"create:cases", "update:cases", "delete:cases"},
        "knowledge:read": {"read:knowledge", "search:knowledge"},
        "knowledge:write": {"create:knowledge", "update:knowledge", "delete:knowledge"},
        "evidence:read": {"read:evidence", "list:evidence"},
        "evidence:write": {"upload:evidence", "delete:evidence"},
    }

    @classmethod
    def get_permissions_for_scopes(cls, scopes: List[str]) -> Set[str]:
        """Convert scopes to permissions."""
        permissions = set()
        for scope in scopes:
            if scope in cls.SCOPE_PERMISSIONS:
                permissions.update(cls.SCOPE_PERMISSIONS[scope])
        return permissions

    @classmethod
    def has_permission(cls, token_scopes: List[str], required_permission: str) -> bool:
        """Check if token has required permission."""
        permissions = cls.get_permissions_for_scopes(token_scopes)
        return required_permission in permissions
```

### Default Scopes for Copilot Extension

```text
openid profile email cases:read cases:write knowledge:read evidence:read
```

This provides:
- User identification and profile access
- Read/write access to cases
- Read-only access to knowledge base and evidence

## Role-Based Access Control

### User Roles

#### Regular User (`user` role)

Default role for all users:

- Login and authenticate
- Search Global KB (read-only)
- List Global KB documents (read-only)
- Upload to their own User KB
- Manage their own User KB documents
- Use all troubleshooting features
- **Cannot:** Upload/modify/delete Global KB

#### Admin User (`user` + `admin` roles)

Enhanced permissions:

- All regular user capabilities
- Upload documents to Global KB
- Update Global KB documents
- Delete Global KB documents
- Bulk operations on Global KB

### Role Implementation

**User contract (`UserDTO`):**

This is the public contract surface other modules consume. The internal domain entity (`User`, in `faultmaven/modules/auth/domain/models/user.py`) carries persistence-layer fields like `hashed_password`, `is_verified`, `updated_at`, `last_login_at`, and `metadata`; it should not be exported across module boundaries.

```python
# faultmaven/modules/auth/contracts.py
class UserDTO(BaseModel):
    user_id: str
    email: str
    full_name: str
    is_active: bool = True
    # Roles are not on UserDTO; they are loaded per-request from the RBAC system
    # (role_permissions / user_roles tables) and attached to the request context.
```

> **Auth mode** (`local` vs `oauth`) is a system-wide configuration setting (`AUTH_MODE`), not a per-user attribute — both modes operate on the same `UserDTO` shape.

**Protected Endpoints:**

```python
from faultmaven.api.dependencies import require_admin, require_authentication

@router.post("/knowledge/documents")
async def upload_document(
    file: UploadFile,
    current_user: User = Depends(require_admin)  # Admin only
):
    """Upload document to Global KB (admin only)"""
    ...

@router.post("/knowledge/search")
async def search_knowledge(
    request: SearchRequest,
    current_user: User = Depends(require_authentication)  # Any user
):
    """Search Global KB (all users)"""
    ...
```

## Common Endpoints

These endpoints work identically in both Local and Cloud modes.

### Token Validation

```http
GET /api/v1/auth/me
Authorization: Bearer {access_token}
```

**Response:**

```json
{
  "user_id": "user_123",
  "username": "alice",
  "email": "alice@example.com",
  "display_name": "Alice Smith",
  "roles": ["user", "admin"],
  "auth_mode": "local",
  "created_at": "2025-10-23T12:00:00Z",
  "token_count": 1
}
```

### Logout

```http
POST /api/v1/auth/logout
Authorization: Bearer {access_token}
```

**Response:**

```json
{
  "message": "Logged out successfully",
  "revoked_tokens": 1
}
```

### Session Invalidation on Auth Events

Authentication events have implications for session state management. The following table defines the expected behavior:

| Event | Session Action |
|-------|----------------|
| **Logout** | Delete all sessions for user and revoke all tokens |
| **Token Revocation** | Delete session associated with revoked token |
| **New Login (same client_id)** | Replace previous session with new one |
| **Password Change** | Invalidate all sessions (security measure) |
| **Account Deactivation** | Delete all sessions immediately |

**Implementation:**

```python
async def logout(
    auth_service: IAuthService,
    state_manager: StateManager,
    session_store: ISessionStore,
    token: str,
    session_id: str
) -> LogoutResponse:
    """Logout user and cleanup session state."""
    # 1. Revoke token
    await auth_service.revoke_token(token)

    # 2. Delete investigation state from Redis
    await state_manager.delete_investigation_state(session_id)

    # 3. Clean up client mapping
    await session_store.cleanup_client_session_mapping(session_id)

    return LogoutResponse(message="Logged out successfully", revoked_tokens=1)
```

> [!IMPORTANT]
> Session invalidation must be atomic with token revocation. If token revocation succeeds but session cleanup fails, the user may see stale session data on next login. Use a transaction or saga pattern to ensure consistency.

## Frontend Implementation

### Extension OAuth Client

```typescript
// src/lib/auth/oauth-client.ts
export class OAuthClient implements IAuthClient {
  private readonly dashboardUrl: string;
  private readonly extensionId = chrome.runtime.id;

  constructor(config: OAuthConfig) {
    this.dashboardUrl = config.oauth.authorize_url;
  }

  async signIn(): Promise<AuthResult> {
    // Generate PKCE parameters
    const codeVerifier = PKCEGenerator.generateCodeVerifier();
    const codeChallenge = PKCEGenerator.generateCodeChallenge(codeVerifier);
    const state = PKCEGenerator.generateState();

    // Store for later verification
    await chrome.storage.local.set({
      pkce_verifier: codeVerifier,
      auth_state: state,
      auth_initiated_at: Date.now()
    });

    // Build authorization URL
    const params = new URLSearchParams({
      response_type: 'code',
      client_id: 'faultmaven-copilot',
      redirect_uri: `chrome-extension://${this.extensionId}/callback`,
      code_challenge: codeChallenge,
      code_challenge_method: 'S256',
      state: state,
      scope: 'openid profile email cases:read cases:write'
    });

    // Open dashboard in new tab
    await chrome.tabs.create({
      url: `${this.dashboardUrl}?${params.toString()}`
    });

    // Return pending result - callback handler will complete
    return { pending: true };
  }
}
```

### Extension Local Auth Client

```typescript
// src/lib/auth/local-auth-client.ts
export class LocalAuthClient implements IAuthClient {
  private readonly loginEndpoint: string;

  constructor(config: LocalAuthConfig) {
    this.loginEndpoint = config.login_endpoint;
  }

  async signIn(credentials: { username: string; password?: string }): Promise<AuthResult> {
    const response = await fetch(this.loginEndpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(credentials)
    });

    if (!response.ok) {
      const error = await response.json();
      throw new AuthError(error.message);
    }

    const tokens = await response.json();

    // Store tokens
    await chrome.storage.local.set({
      access_token: tokens.access_token,
      refresh_token: tokens.refresh_token,
      expires_at: Date.now() + (tokens.expires_in * 1000),
      session_id: tokens.session_id,
      user: tokens.user
    });

    return { success: true, user: tokens.user };
  }
}
```

### Token Manager with Auto-Refresh

```typescript
// src/lib/auth/token-manager.ts
class TokenManager {
  private refreshPromise: Promise<void> | null = null;

  async getValidAccessToken(): Promise<string> {
    const tokens = await this.getStoredTokens();

    if (!tokens) {
      throw new Error('No tokens available');
    }

    // Check if access token is expired or expiring soon (< 5 minutes)
    const expiryBuffer = 5 * 60 * 1000;
    if (Date.now() + expiryBuffer >= tokens.expires_at) {
      await this.refreshTokens();
      const newTokens = await this.getStoredTokens();
      return newTokens!.access_token;
    }

    return tokens.access_token;
  }

  private async refreshTokens(): Promise<void> {
    // Prevent concurrent refresh requests
    if (this.refreshPromise) {
      return this.refreshPromise;
    }

    this.refreshPromise = this._doRefresh();
    try {
      await this.refreshPromise;
    } finally {
      this.refreshPromise = null;
    }
  }

  private async _doRefresh(): Promise<void> {
    const tokens = await this.getStoredTokens();

    if (!tokens?.refresh_token) {
      throw new Error('No refresh token available');
    }

    // Check if refresh token is expired
    if (Date.now() >= tokens.refresh_expires_at) {
      await this.clearTokens();
      throw new Error('Refresh token expired, re-authentication required');
    }

    const response = await fetch('/auth/oauth/token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        grant_type: 'refresh_token',
        refresh_token: tokens.refresh_token,
        client_id: 'faultmaven-copilot'
      })
    });

    if (!response.ok) {
      await this.clearTokens();
      throw new Error('Token refresh failed');
    }

    const newTokens = await response.json();
    await this.storeTokens(newTokens);
  }
}
```

### API Client with Dual Headers

```typescript
// src/lib/api.ts
class FaultMavenAPI {
  private tokenManager: TokenManager;

  /**
   * Get headers with token and session ID.
   * Session ID is fetched from storage on every call for Manifest V3 compatibility.
   */
  private async getHeaders(): Promise<Record<string, string>> {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json'
    };

    // Get valid access token (auto-refreshes if needed)
    try {
      const accessToken = await this.tokenManager.getValidAccessToken();
      headers['Authorization'] = `Bearer ${accessToken}`;
    } catch (error) {
      console.warn('No valid access token available', error);
    }

    // Fetch session ID from storage (Manifest V3 Service Worker safe)
    const storage = await chrome.storage.local.get(['session_id']);
    if (storage.session_id) {
      headers['X-Session-Id'] = storage.session_id;
    }

    return headers;
  }

  async apiCall(endpoint: string, options: RequestInit = {}) {
    const headers = await this.getHeaders();

    const response = await fetch(`${API_BASE}${endpoint}`, {
      ...options,
      headers: { ...headers, ...(options.headers || {}) }
    });

    if (response.status === 401) {
      await this.handleAuthError();
      throw new AuthenticationError('Authentication required');
    }

    return response;
  }
}
```

## Backend Implementation

### Service Contracts

```python
# faultmaven/modules/auth/contracts.py

class IOAuthService(ABC):
    """Contract for OAuth authentication operations."""

    @abstractmethod
    async def create_authorization_code(
        self, user_id: str, request: OAuthAuthorizationDTO
    ) -> str:
        """Generate authorization code for OAuth flow."""
        ...

    @abstractmethod
    async def exchange_code_for_token(
        self, code: str, code_verifier: str, redirect_uri: str
    ) -> OAuthTokenDTO:
        """Exchange authorization code for access token."""
        ...

    @abstractmethod
    async def validate_token(self, token: str) -> Optional[str]:
        """Validate access token and return user_id."""
        ...

    @abstractmethod
    async def refresh_access_token(
        self, refresh_token: str, client_id: str
    ) -> OAuthTokenDTO:
        """Refresh access token using refresh token."""
        ...

    @abstractmethod
    async def revoke_token(self, token: str) -> None:
        """Revoke access token (logout)."""
        ...


class ILocalAuthService(ABC):
    """Contract for Local Mode authentication operations."""

    @abstractmethod
    async def login(self, username: str, password: Optional[str] = None) -> AuthTokenDTO:
        """Authenticate user with username/password."""
        ...

    @abstractmethod
    async def register(
        self, username: str, email: str, display_name: str, password: Optional[str] = None
    ) -> AuthTokenDTO:
        """Register new user account."""
        ...

    @abstractmethod
    async def validate_token(self, token: str) -> Optional[str]:
        """Validate access token and return user_id."""
        ...


class IOAuthCodeRepository(ABC):
    """Storage abstraction for OAuth authorization codes."""

    @abstractmethod
    async def save_code(self, code_data: OAuthCodeDTO) -> None:
        """Store authorization code with PKCE challenge."""
        ...

    @abstractmethod
    async def get_code(self, code: str) -> Optional[OAuthCodeDTO]:
        """Retrieve authorization code data."""
        ...

    @abstractmethod
    async def mark_code_used(self, code: str) -> None:
        """Mark code as used (prevents replay attacks)."""
        ...

    @abstractmethod
    async def delete_expired_codes(self) -> int:
        """Clean up expired codes."""
        ...
```

### Storage Implementations

**In-Memory (Local Development):**

```python
class InMemoryOAuthCodeRepository(IOAuthCodeRepository):
    """In-memory implementation for local development."""

    def __init__(self):
        self._codes: Dict[str, OAuthCodeDTO] = {}
        self._lock = asyncio.Lock()

    async def save_code(self, code_data: OAuthCodeDTO) -> None:
        async with self._lock:
            self._codes[code_data.code] = code_data

    async def get_code(self, code: str) -> Optional[OAuthCodeDTO]:
        async with self._lock:
            code_data = self._codes.get(code)
            if code_data and code_data.expires_at > datetime.now(timezone.utc):
                return code_data
            return None
```

**Redis (Cloud Cache Layer):**

```python
class RedisOAuthCodeRepository(IOAuthCodeRepository):
    """Redis implementation for cloud deployments."""

    def __init__(self, redis_client):
        self.redis = redis_client
        self.key_prefix = "oauth:code:"
        self.ttl_seconds = 600  # 10 minutes

    async def save_code(self, code_data: OAuthCodeDTO) -> None:
        key = f"{self.key_prefix}{code_data.code}"
        value = json.dumps({
            "user_id": code_data.user_id,
            "redirect_uri": code_data.redirect_uri,
            "code_challenge": code_data.code_challenge,
            "expires_at": code_data.expires_at.isoformat(),
            "used": code_data.used
        })
        await self.redis.setex(key, self.ttl_seconds, value)
```

**PostgreSQL (Enterprise Persistent Storage):**

```python
class PostgresOAuthCodeRepository(IOAuthCodeRepository):
    """PostgreSQL implementation for enterprise deployments."""

    async def save_code(self, code_data: OAuthCodeDTO) -> None:
        query = """
            INSERT INTO auth.oauth_codes
            (code, user_id, redirect_uri, code_challenge, expires_at, used)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (code) DO UPDATE SET used = EXCLUDED.used
        """
        await self.db.execute(
            query,
            code_data.code,
            code_data.user_id,
            code_data.redirect_uri,
            code_data.code_challenge,
            code_data.expires_at,
            code_data.used
        )
```

### Dependency Injection

```python
# faultmaven/main.py (composition root)

def create_app(config: AppConfig) -> FastAPI:
    app = FastAPI()

    # Select auth service based on mode
    if config.auth.mode == "local":
        auth_service = LocalAuthService(
            user_repository=create_user_repository(config),
            token_generator=JWTTokenGenerator(config.auth.jwt_secret_key, "HS256")
        )
        app.include_router(local_auth_router)
    else:
        # Cloud mode with OAuth
        oauth_code_repo = create_oauth_code_repository(config)
        auth_service = OAuthService(
            code_repository=oauth_code_repo,
            token_generator=JWTTokenGenerator(config.auth.jwt_private_key, "RS256"),
            user_repository=create_user_repository(config)
        )
        app.include_router(oauth_router)

    # Inject into dependencies
    app.dependency_overrides[IAuthService] = lambda: auth_service

    return app


def create_oauth_code_repository(config: AppConfig) -> IOAuthCodeRepository:
    """Factory for OAuth code repository based on config."""
    if config.cache.backend == "redis":
        return RedisOAuthCodeRepository(config.cache.redis_url)
    elif config.database.backend == "postgres":
        return PostgresOAuthCodeRepository(config.database.url)
    else:
        return InMemoryOAuthCodeRepository()
```

## Observability

### Structured Logging

```python
import structlog

logger = structlog.get_logger(__name__)

class OAuthService:
    async def create_authorization_code(self, user_id: str, request: OAuthAuthorizationDTO) -> str:
        logger.info(
            "oauth.authorization.start",
            user_id=user_id,
            client_id=request.client_id,
            code_challenge_method=request.code_challenge_method
        )

        code = self._generate_code()
        await self.code_repository.save_code(...)

        logger.info(
            "oauth.authorization.success",
            user_id=user_id,
            code_expires_in_seconds=600
        )

        return code
```

### Prometheus Metrics

```python
from prometheus_client import Counter, Histogram

# Counters
oauth_code_issued = Counter(
    "oauth_code_issued_total",
    "Total authorization codes issued",
    ["client_id"]
)

oauth_token_exchanged = Counter(
    "oauth_token_exchanged_total",
    "Total successful token exchanges",
    ["client_id"]
)

oauth_token_refresh = Counter(
    "oauth_token_refresh_total",
    "Total token refreshes",
    ["client_id", "status"]
)

auth_login_attempts = Counter(
    "auth_login_attempts_total",
    "Total login attempts",
    ["auth_mode", "status"]
)

# Histograms
oauth_token_exchange_duration = Histogram(
    "oauth_token_exchange_duration_seconds",
    "Token exchange request duration"
)
```

## Error Handling

### OAuth Error Responses

All OAuth errors follow RFC 6749 format:

```json
{
  "error": "error_code",
  "error_description": "Human-readable description"
}
```

| Error Code | HTTP Status | Description |
|------------|-------------|-------------|
| `invalid_client` | 400 | Unknown or invalid client_id |
| `invalid_grant` | 400 | Code expired, used, or PKCE failed |
| `invalid_request` | 400 | Missing required parameters |
| `unauthorized_client` | 401 | Client not authorized for grant type |
| `access_denied` | 403 | User denied authorization |
| `server_error` | 500 | Internal server error |

### Frontend Error Handling

```typescript
class AuthErrorHandler {
  async handleAuthError(error: AuthError): Promise<void> {
    switch (error.code) {
      case 'invalid_grant':
        await this.clearAuthState();
        await this.showRetryPrompt();
        break;

      case 'access_denied':
        await this.showCancelledMessage();
        break;

      case 'server_error':
        await this.showErrorMessage(error.description);
        break;

      default:
        await this.showGenericError();
    }
  }
}
```

## Testing Strategy

### Unit Tests

**PKCE Verification:**

```python
@pytest.mark.asyncio
async def test_pkce_verification_success():
    """Test PKCE verification with correct verifier."""
    verifier = "test_verifier_1234567890abcdef"
    challenge = compute_pkce_challenge(verifier)

    assert verify_pkce(verifier, challenge) is True

@pytest.mark.asyncio
async def test_pkce_verification_failure():
    """Test PKCE verification with wrong verifier."""
    verifier = "correct_verifier"
    challenge = compute_pkce_challenge(verifier)

    assert verify_pkce("wrong_verifier", challenge) is False
```

**Token Exchange:**

```python
@pytest.mark.asyncio
async def test_token_exchange_pkce_mismatch():
    """Test that token exchange fails with incorrect code_verifier."""
    verifier = "test_verifier_1234567890"
    challenge = compute_pkce_challenge(verifier)
    code = await create_auth_code(user_id="user_123", challenge=challenge)

    response = await client.post("/auth/oauth/token", json={
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": "wrong_verifier",
        "client_id": "faultmaven-copilot",
        "redirect_uri": "chrome-extension://abc.../callback"
    })

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"
```

### Integration Tests

**Complete OAuth Flow:**

```typescript
describe('OAuth Flow Integration', () => {
  it('should complete full authorization flow', async () => {
    // 1. Initiate login
    const oauth = await AuthClientFactory.create(API_BASE);
    await oauth.signIn();

    // 2. Verify PKCE data stored
    const storage = await chrome.storage.local.get(['pkce_verifier', 'auth_state']);
    expect(storage.pkce_verifier).toBeDefined();
    expect(storage.auth_state).toBeDefined();

    // 3. Simulate callback with authorization code
    const result = await handleAuthCallback(callbackUrl);

    expect(result.success).toBe(true);
    expect(result.user).toBeDefined();

    // 4. Verify PKCE data cleaned up
    const cleaned = await chrome.storage.local.get(['pkce_verifier']);
    expect(cleaned.pkce_verifier).toBeUndefined();
  });
});
```

## Configuration Reference

### Environment Variables

| Variable | Mode | Description | Default |
|----------|------|-------------|---------|
| `AUTH_MODE` | Both | `local` or `oauth` | `local` |
| `JWT_SECRET_KEY` | Local | Symmetric key for HS256 | Required |
| `JWT_PRIVATE_KEY_PATH` | Cloud | Path to RS256 private key | Required |
| `JWT_PUBLIC_KEY_PATH` | Cloud | Path to RS256 public key | Required |
| `JWT_ACCESS_TOKEN_EXPIRY` | Both | Access token lifetime (minutes) | `60` |
| `JWT_REFRESH_TOKEN_EXPIRY` | Both | Refresh token lifetime (minutes) | `10080` (7 days) |
| `OAUTH_CODE_EXPIRY` | Cloud | Authorization code lifetime (minutes) | `10` |
| `OAUTH_REQUIRE_CONSENT` | Cloud | Require user consent screen | `false` |
| `OAUTH_REQUIRE_HTTPS_REDIRECT` | Cloud | Require HTTPS redirect URIs | `false` |
| `DASHBOARD_URL` | Both | Dashboard URL for OAuth redirects | `http://localhost:3333` |

### Configuration File

```yaml
# config/auth.yml
auth:
  mode: local  # or "oauth"

  jwt:
    algorithm: HS256  # or RS256 for cloud
    secret_key: ${JWT_SECRET_KEY}
    access_token_expiry_minutes: 60
    refresh_token_expiry_minutes: 10080  # 7 days

  oauth:  # Cloud mode only
    dashboard_url: https://dashboard.faultmaven.ai
    require_consent: false
    require_https_redirect: false
    code_expiry_minutes: 10

  rate_limiting:
    enabled: true
    login_attempts_per_minute: 10
    token_requests_per_minute: 5
```

## Architectural Compliance

This design adheres to FaultMaven's architectural principles:

| Principle | Implementation |
|-----------|----------------|
| **Vertical Modules with Contracts** | `IOAuthService`, `ILocalAuthService` interfaces in `contracts.py` |
| **Database-per-Module Boundaries** | Auth module owns `auth.oauth_codes` table exclusively |
| **Interface-Based Design** | All auth operations through abstract interfaces |
| **Deployment-Agnostic Core** | Strategy pattern selects Local vs Cloud at startup |
| **Composition Root Pattern** | Dependency injection in `main.py` |
| **Boundary Enforcement** | `.importlinter` rules prevent cross-module imports |
| **Unified Token Format** | JWT in both modes for middleware uniformity |
| **Frontend Strategy Pattern** | `AuthClientFactory` selects client based on config |

## Summary

| Aspect | Local Mode | Cloud Mode |
|--------|------------|------------|
| **Endpoint** | `/api/v1/auth/login` | `/auth/oauth/authorize` |
| **Flow** | Form POST → JWT | OAuth 2.0 + PKCE |
| **Token Signing** | HS256 (symmetric) | RS256 (asymmetric) |
| **User Storage** | SQLite | PostgreSQL |
| **Code Storage** | In-Memory | Redis / PostgreSQL |
| **JWT Structure** | Identical | Identical |
| **Middleware** | Identical | Identical |
| **Frontend Discovery** | `/api/v1/auth/config` | `/api/v1/auth/config` |

The core business logic (Cases, Knowledge, Evidence) interacts only with the uniform JWT token claims and never knows which authentication mode is active.
