# FaultMaven Authentication System Design

## Overview

This document defines the authentication design for FaultMaven, providing clear guidance for frontend and backend implementation. This design ensures secure user identity management while maintaining the clean separation between authentication (user identity) and sessions (conversation state).

## Design Principles

### Core Philosophy
FaultMaven uses a **dual-header approach** that cleanly separates concerns:
- **Authentication**: `Authorization: Bearer <token>` for user identity
- **Session Management**: `X-Session-Id: <session_id>` for conversation continuity

### Key Design Goals
1. **Clean Separation**: Authentication and session management are independent systems
2. **Browser Extension Optimized**: Designed for multi-tab, persistent extension usage
3. **Development Friendly**: Simple dev-login for testing and development
4. **Production Ready**: Clear migration path to OAuth2/OIDC for enterprise
5. **Secure by Default**: Tokens expire, proper error handling, secure storage

## Authentication Architecture

### System Components

```mermaid
graph TD
    A[Browser Extension] --> B[Authentication Flow]
    B --> C[Token Storage]
    B --> D[API Client]
    D --> E[Backend Auth System]

    C --> F[chrome.storage.local]
    E --> G[Token Manager]
    E --> H[User Store]

    subgraph "Headers Sent"
        I[Authorization: Bearer token]
        J[X-Session-Id: session]
    end

    D --> I
    D --> J
```

### Authentication Flow

```mermaid
sequenceDiagram
    participant FE as Frontend
    participant API as FaultMaven API
    participant Auth as Auth System
    participant Storage as Chrome Storage

    Note over FE,Storage: Initial Authentication
    FE->>API: POST /auth/dev-login {username}
    API->>Auth: Validate & create token
    Auth-->>API: {access_token, expires_in, user}
    API-->>FE: Authentication response
    FE->>Storage: Store token & expiry

    Note over FE,Storage: Subsequent API Calls
    FE->>Storage: Get stored token
    Storage-->>FE: {token, expiry}
    FE->>API: API call with Bearer token
    API->>Auth: Validate token
    Auth-->>API: User context
    API-->>FE: Protected resource

    Note over FE,Storage: Token Expiration
    FE->>API: API call with expired token
    API-->>FE: 401 Unauthorized
    FE->>FE: Clear stored auth data
    FE->>FE: Show login form
```

## Cross-Application Authentication Flow (Extension + Dashboard)

### Architecture Overview

FaultMaven uses a **dashboard-centric authentication** pattern where the Dashboard acts as the Identity Provider (IdP). This follows the **OAuth 2.0 Authorization Code Flow with PKCE**, ensuring secure delegation of identity from the Web App to the Extension without sharing credentials.

**Key Design Decisions**:
- **Dashboard as Identity Provider**: All authentication UI, social login, and MFA handled on dashboard
- **Extension as Public Client**: No client secrets stored in extension
- **PKCE Required**: Mandatory for browser extensions to prevent authorization code interception
- **Standard OAuth 2.0**: Industry-standard pattern for secure delegated authentication

### Why Dashboard-Centric Authentication?

**Security Benefits**:
1. **No Credentials in Extension**: Extension never handles passwords or MFA codes
2. **Simplified Extension**: No complex authentication UI in extension
3. **Centralized Auth Logic**: All authentication flows (social login, MFA, SSO) in one place
4. **Better User Experience**: Users see familiar dashboard login UI
5. **Easier Updates**: Authentication changes don't require extension updates

**Architecture Benefits**:
- Dashboard handles complex OAuth flows (Google, GitHub, Microsoft)
- Dashboard implements MFA, password reset, email verification
- Extension remains lightweight and focused on core functionality
- Backend issues standard JWT tokens for both dashboard and extension

### Complete Flow Diagram

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
    API-->>Ext: {<br/>access_token: JWT,<br/>token_type: Bearer,<br/>expires_in: 86400,<br/>session_id: SESSION_ID,<br/>user: {...}<br/>}
    Ext->>Ext: Store access_token & session_id
    Ext->>Ext: Clear PKCE verifier & state
    Ext->>API: API calls with dual headers<br/>Authorization: Bearer JWT<br/>X-Session-Id: SESSION_ID
```

### Implementation Details

#### Phase 1: Extension Initiates Auth

**Step 1: Generate PKCE Parameters**

```typescript
// src/lib/auth/pkce.ts
import { createHash, randomBytes } from 'crypto';

export class PKCEGenerator {
  /**
   * Generate PKCE code verifier (43-128 characters, base64url-encoded)
   */
  static generateCodeVerifier(): string {
    // Generate 32 random bytes, base64url encode
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

**Step 2: Redirect to Dashboard**

```typescript
// src/lib/auth/oauth-client.ts
export class OAuthClient {
  private readonly dashboardUrl = 'https://dashboard.faultmaven.ai';
  private readonly extensionId = chrome.runtime.id;

  async initiateLogin(): Promise<void> {
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

    const authUrl = `${this.dashboardUrl}/auth/authorize?${params.toString()}`;

    // Open dashboard in new tab
    await chrome.tabs.create({ url: authUrl });
  }
}
```

#### Phase 2: Dashboard Handles Login

**Dashboard Authorization Endpoint**

```typescript
// Dashboard: app/auth/authorize/route.ts
import { NextRequest, NextResponse } from 'next/server';

export async function GET(request: NextRequest) {
  const searchParams = request.nextUrl.searchParams;

  // Extract OAuth parameters
  const {
    response_type,
    client_id,
    redirect_uri,
    code_challenge,
    code_challenge_method,
    state,
    scope
  } = Object.fromEntries(searchParams);

  // Validate parameters
  if (response_type !== 'code') {
    return NextResponse.json({ error: 'unsupported_response_type' }, { status: 400 });
  }

  if (code_challenge_method !== 'S256') {
    return NextResponse.json({ error: 'invalid_request', error_description: 'PKCE required' }, { status: 400 });
  }

  // Validate client_id and redirect_uri
  const validClients = {
    'faultmaven-copilot': /^chrome-extension:\/\/[a-z]{32}\/callback$/
  };

  if (!validClients[client_id] || !validClients[client_id].test(redirect_uri)) {
    return NextResponse.json({ error: 'invalid_client' }, { status: 400 });
  }

  // Store authorization request in session
  await storeAuthorizationRequest({
    client_id,
    redirect_uri,
    code_challenge,
    state,
    scope,
    expires_at: Date.now() + 600_000 // 10 minutes
  });

  // Show login UI (redirect to login page)
  return NextResponse.redirect('/login?auth_request=true');
}
```

**After Successful Login**

```typescript
// Dashboard: app/api/auth/authorize/complete/route.ts
export async function POST(request: NextRequest) {
  const session = await getSession(request);
  const authRequest = await getAuthorizationRequest(session.id);

  if (!authRequest) {
    return NextResponse.json({ error: 'invalid_request' }, { status: 400 });
  }

  // Generate authorization code
  const authorizationCode = generateSecureCode(); // UUID v4

  // Store authorization code with PKCE challenge
  await redis.setex(`auth:code:${authorizationCode}`, 600, JSON.stringify({
    user_id: session.user_id,
    client_id: authRequest.client_id,
    redirect_uri: authRequest.redirect_uri,
    code_challenge: authRequest.code_challenge,
    scope: authRequest.scope,
    created_at: Date.now()
  }));

  // Redirect back to extension
  const redirectUrl = new URL(authRequest.redirect_uri);
  redirectUrl.searchParams.set('code', authorizationCode);
  redirectUrl.searchParams.set('state', authRequest.state);

  return NextResponse.redirect(redirectUrl.toString());
}
```

#### Phase 3: Extension Exchanges Code for Token

**Extension Callback Handler**

```typescript
// Extension: src/background/auth-callback.ts
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === 'auth_callback') {
    handleAuthCallback(message.url)
      .then(result => sendResponse({ success: true, result }))
      .catch(error => sendResponse({ success: false, error: error.message }));
    return true; // Async response
  }
});

async function handleAuthCallback(callbackUrl: string): Promise<AuthResult> {
  const url = new URL(callbackUrl);
  const code = url.searchParams.get('code');
  const state = url.searchParams.get('state');

  if (!code || !state) {
    throw new Error('Missing authorization code or state');
  }

  // Retrieve stored PKCE verifier and state
  const storage = await chrome.storage.local.get(['pkce_verifier', 'auth_state']);

  if (!storage.pkce_verifier || !storage.auth_state) {
    throw new Error('No pending authorization request');
  }

  // Verify state parameter (CSRF protection)
  if (state !== storage.auth_state) {
    throw new Error('State parameter mismatch');
  }

  // Exchange authorization code for access token
  const tokenResponse = await fetch('https://api.faultmaven.ai/auth/token', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      grant_type: 'authorization_code',
      code: code,
      code_verifier: storage.pkce_verifier,
      client_id: 'faultmaven-copilot',
      redirect_uri: `chrome-extension://${chrome.runtime.id}/callback`
    })
  });

  if (!tokenResponse.ok) {
    const error = await tokenResponse.json();
    throw new Error(`Token exchange failed: ${error.error_description}`);
  }

  const tokens = await tokenResponse.json();

  // Store tokens
  await chrome.storage.local.set({
    access_token: tokens.access_token,
    token_type: tokens.token_type,
    expires_at: Date.now() + (tokens.expires_in * 1000),
    session_id: tokens.session_id,
    user: tokens.user
  });

  // Clean up PKCE data
  await chrome.storage.local.remove(['pkce_verifier', 'auth_state', 'auth_initiated_at']);

  return {
    success: true,
    user: tokens.user
  };
}
```

**Backend Token Exchange Endpoint**

```python
# Backend: faultmaven/api/v1/auth/token.py
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
import hashlib
import base64

router = APIRouter()

class TokenRequest(BaseModel):
    grant_type: str
    code: str
    code_verifier: str
    client_id: str
    redirect_uri: str

@router.post("/auth/token")
async def exchange_token(request: TokenRequest):
    """Exchange authorization code for access token (OAuth 2.0 with PKCE)"""

    # Validate grant type
    if request.grant_type != "authorization_code":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "unsupported_grant_type"}
        )

    # Retrieve authorization code data
    code_data = await redis.get(f"auth:code:{request.code}")
    if not code_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_grant", "error_description": "Invalid or expired authorization code"}
        )

    code_info = json.loads(code_data)

    # Verify PKCE code_verifier
    verifier_bytes = request.code_verifier.encode('utf-8')
    computed_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier_bytes).digest()
    ).decode('utf-8').rstrip('=')

    if computed_challenge != code_info['code_challenge']:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_grant", "error_description": "PKCE verification failed"}
        )

    # Verify client_id and redirect_uri
    if request.client_id != code_info['client_id']:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_client"}
        )

    if request.redirect_uri != code_info['redirect_uri']:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_grant", "error_description": "Redirect URI mismatch"}
        )

    # Delete authorization code (single-use)
    await redis.delete(f"auth:code:{request.code}")

    # Get user profile
    user = await user_service.get_user(code_info['user_id'])

    # Generate access token (JWT)
    access_token = generate_jwt_token(user, scope=code_info['scope'])

    # Create session
    session = await session_service.create_session(user.user_id)

    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": 86400,  # 24 hours
        "session_id": session.session_id,
        "user": {
            "user_id": user.user_id,
            "username": user.username,
            "email": user.email,
            "display_name": user.display_name,
            "roles": user.roles
        }
    }
```

### Security Considerations

#### 1. PKCE (Proof Key for Code Exchange)

**Why PKCE is Required**:
- Browser extensions are **public clients** - they cannot securely store a `client_secret`
- Authorization codes can be intercepted by malicious apps monitoring browser redirects
- PKCE proves that the app exchanging the code is the same app that initiated the flow

**How PKCE Works**:
1. Extension generates random `code_verifier` (43-128 characters)
2. Extension computes `code_challenge = SHA256(code_verifier)`
3. Extension sends `code_challenge` in authorization request
4. Backend stores `code_challenge` with authorization code
5. Extension sends original `code_verifier` when exchanging code for token
6. Backend verifies `SHA256(code_verifier)` matches stored `code_challenge`

**Security Properties**:
- Even if authorization code is intercepted, attacker cannot exchange it without the `code_verifier`
- `code_verifier` never leaves the extension until token exchange
- Backend cryptographically verifies the extension's identity

#### 2. State Parameter

**Purpose**: Prevent CSRF attacks during OAuth redirect

**Implementation**:
- Extension generates random `state` parameter before redirect
- Extension stores `state` in `chrome.storage.local`
- Dashboard includes `state` in redirect URL
- Extension verifies returned `state` matches stored value

**Attack Prevented**: Malicious site cannot trick user into authorizing extension without knowing the `state` value

#### 3. Authorization Code Properties

**Single-Use**: Code deleted immediately after successful exchange
**Short-Lived**: Expires in 10 minutes
**Bound to Client**: Validates `client_id` and `redirect_uri` match
**PKCE Protected**: Cannot be exchanged without correct `code_verifier`

#### 4. Redirect URI Validation

**Strict Validation**:
```python
ALLOWED_REDIRECT_URIS = {
    'faultmaven-copilot': [
        r'^chrome-extension://[a-z]{32}/callback$',  # Regex pattern
        r'^moz-extension://[a-f0-9-]{36}/callback$'  # Firefox support
    ]
}
```

**Security Properties**:
- Only registered extension IDs can receive authorization codes
- Prevents authorization code injection attacks
- Supports multiple browser extension stores

#### 5. Token Security

**Storage**:
- Extension: `chrome.storage.local` (not sync storage, never transmitted)
- Backend: JWT signed with RS256 (asymmetric key)
- Authorization codes: Redis with 10-minute TTL

**Transmission**:
- Always HTTPS
- JWT in `Authorization: Bearer` header
- Never in URL parameters

### Error Handling

#### Authorization Errors

**Invalid Client**:
```json
{
  "error": "invalid_client",
  "error_description": "Unknown or invalid client_id"
}
```

**Invalid Grant**:
```json
{
  "error": "invalid_grant",
  "error_description": "Authorization code expired or already used"
}
```

**PKCE Verification Failed**:
```json
{
  "error": "invalid_grant",
  "error_description": "PKCE verification failed"
}
```

**Redirect URI Mismatch**:
```json
{
  "error": "invalid_grant",
  "error_description": "Redirect URI mismatch"
}
```

#### Frontend Error Handling

```typescript
class AuthErrorHandler {
  async handleAuthError(error: AuthError): Promise<void> {
    switch (error.code) {
      case 'invalid_grant':
        // Clear stored PKCE data and retry
        await this.clearAuthState();
        await this.showRetryPrompt();
        break;

      case 'access_denied':
        // User cancelled login on dashboard
        await this.showCancelledMessage();
        break;

      case 'server_error':
        // Backend error, show error message
        await this.showErrorMessage(error.description);
        break;

      default:
        // Unknown error
        await this.showGenericError();
    }
  }
}
```

### Testing Strategy

#### Unit Tests

**PKCE Generation**:
```typescript
describe('PKCEGenerator', () => {
  it('should generate verifier with correct length', () => {
    const verifier = PKCEGenerator.generateCodeVerifier();
    expect(verifier.length).toBeGreaterThanOrEqual(43);
    expect(verifier.length).toBeLessThanOrEqual(128);
  });

  it('should generate challenge matching verifier', () => {
    const verifier = PKCEGenerator.generateCodeVerifier();
    const challenge = PKCEGenerator.generateCodeChallenge(verifier);

    // Verify challenge is SHA256 of verifier
    const expected = crypto.createHash('sha256')
      .update(verifier)
      .digest('base64url');

    expect(challenge).toBe(expected);
  });
});
```

**Token Exchange Validation**:
```python
@pytest.mark.asyncio
async def test_token_exchange_pkce_mismatch():
    """Test that token exchange fails with incorrect code_verifier"""
    # Create authorization code with known challenge
    verifier = "test_verifier_1234567890"
    challenge = compute_pkce_challenge(verifier)

    code = await create_auth_code(user_id="user_123", challenge=challenge)

    # Try to exchange with wrong verifier
    response = await client.post("/auth/token", json={
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": "wrong_verifier",
        "client_id": "faultmaven-copilot",
        "redirect_uri": "chrome-extension://abc.../callback"
    })

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"
```

#### Integration Tests

**Complete OAuth Flow**:
```typescript
describe('OAuth Flow Integration', () => {
  it('should complete full authorization flow', async () => {
    // 1. Extension initiates login
    const oauth = new OAuthClient();
    await oauth.initiateLogin();

    // Verify PKCE data stored
    const storage = await chrome.storage.local.get(['pkce_verifier', 'auth_state']);
    expect(storage.pkce_verifier).toBeDefined();
    expect(storage.auth_state).toBeDefined();

    // 2. Simulate dashboard redirect with authorization code
    const mockCode = 'auth_code_123';
    const mockState = storage.auth_state;

    // 3. Handle callback
    const result = await handleAuthCallback(
      `chrome-extension://abc/callback?code=${mockCode}&state=${mockState}`
    );

    expect(result.success).toBe(true);
    expect(result.user).toBeDefined();

    // Verify tokens stored
    const tokens = await chrome.storage.local.get(['access_token', 'session_id']);
    expect(tokens.access_token).toBeDefined();
    expect(tokens.session_id).toBeDefined();

    // Verify PKCE data cleaned up
    const cleaned = await chrome.storage.local.get(['pkce_verifier', 'auth_state']);
    expect(cleaned.pkce_verifier).toBeUndefined();
    expect(cleaned.auth_state).toBeUndefined();
  });
});
```

### Comparison: Dashboard-Centric vs Direct Auth

| Aspect | Dashboard-Centric (OAuth) | Direct Extension Auth |
|--------|---------------------------|----------------------|
| **Security** | ✅ Industry standard, PKCE protected | ⚠️ Extension handles credentials |
| **User Experience** | ✅ Familiar dashboard UI | ⚠️ Limited extension UI |
| **Social Login** | ✅ Easy to add (Google, GitHub) | ❌ Complex OAuth in extension |
| **MFA** | ✅ Dashboard handles all flows | ❌ Complex MFA UI in extension |
| **Maintenance** | ✅ Auth updates don't need extension release | ⚠️ Extension updates for auth changes |
| **Complexity** | ⚠️ OAuth flow more complex | ✅ Simple dev-login endpoint |
| **Enterprise SSO** | ✅ SAML/OIDC on dashboard | ❌ Very difficult in extension |

### Migration Path

#### Current State (Direct Extension Auth)
- Extension calls `/api/v1/auth/dev-login` directly
- Simple but limited to username/password
- No social login or MFA support

#### Target State (Dashboard-Centric OAuth)
- Extension delegates to dashboard for authentication
- Full OAuth 2.0 with PKCE implementation
- Supports social login, MFA, enterprise SSO

#### Migration Steps

**Phase 1: Backend OAuth Endpoints** (Week 1)
- Implement `/auth/authorize` endpoint
- Implement `/auth/token` with PKCE validation
- Add authorization code storage in Redis

**Phase 2: Dashboard Authorization UI** (Week 1-2)
- Create `/auth/authorize` page
- Implement login UI with social providers
- Add authorization consent screen

**Phase 3: Extension OAuth Client** (Week 2)
- Implement PKCE generation
- Add OAuth initiation flow
- Implement callback handler

**Phase 4: Testing & Rollout** (Week 3)
- End-to-end OAuth flow testing
- Security audit of PKCE implementation
- Gradual rollout with feature flag

#### Backward Compatibility

During migration, support both authentication methods:

```python
@router.post("/auth/login")
async def login(request: LoginRequest):
    """Legacy direct login endpoint (deprecated)"""
    warnings.warn("Direct login is deprecated, use OAuth flow", DeprecationWarning)
    # ... existing implementation
```

**Deprecation Timeline**:
- Month 1-2: Both methods supported, OAuth recommended
- Month 3: OAuth enforced for new users, existing tokens honored
- Month 4: Direct login disabled, OAuth only

## Token Management

### Token Characteristics
- **Format**: UUID v4 (e.g., `550e8400-e29b-41d4-a716-446655440000`)
- **Lifespan**: 24 hours from creation
- **Storage**: SHA-256 hash in Redis (backend)
- **Transmission**: `Authorization: Bearer <token>` header

### Token Lifecycle

```mermaid
stateDiagram-v2
    [*] --> Unauthenticated
    Unauthenticated --> Authenticated: POST /auth/dev-login
    Authenticated --> Authenticated: Valid API calls
    Authenticated --> Expired: 24 hours elapsed
    Authenticated --> Revoked: POST /auth/logout
    Expired --> Unauthenticated: Frontend detects 401
    Revoked --> Unauthenticated: Logout complete

    note right of Authenticated: Token valid for 24 hours
    note right of Expired: Automatic cleanup by backend
    note right of Revoked: Explicit user logout
```

### Storage Strategy

**Frontend (Browser Extension)**:
```typescript
interface AuthState {
  access_token: string;
  token_type: "bearer";
  expires_at: number;  // Unix timestamp
  user: {
    user_id: string;
    username: string;
    display_name: string;
  };
}

// Storage
chrome.storage.local.set({
  authState: {
    access_token: "550e8400-e29b-41d4-a716-446655440000",
    token_type: "bearer",
    expires_at: 1640995200000,
    user: { user_id: "user_123", username: "user123", display_name: "User" }
  }
});
```

**Backend (Redis)**:
```
token:sha256(token_value) → user_id
user:{user_id} → {user_json}
token:user:{user_id} → [token_id1, token_id2, ...]  # Multiple tokens per user
```

## API Integration

### Authentication Endpoints

#### 1. Development Login
```http
POST /api/v1/auth/dev-login
Content-Type: application/json

{
  "username": "user123",
  "email": "user@example.com",
  "display_name": "User Name"
}
```

**Response (201 Created)**:
```json
{
  "access_token": "550e8400-e29b-41d4-a716-446655440000",
  "token_type": "bearer",
  "expires_in": 86400,
  "session_id": "session-41afd36b-3f3c-46dd-8794-1565984d843d",
  "user": {
    "user_id": "user_f939a782",
    "username": "user123",
    "email": "user@example.com",
    "display_name": "User Name",
    "roles": ["user"],
    "is_dev_user": true,
    "created_at": "2025-10-23T12:00:00Z"
  }
}
```

**Note:** Response now includes `roles` array for role-based access control (see [Role-Based Access Control](#role-based-access-control) section).

#### 2. Get Current User
```http
GET /api/v1/auth/me
Authorization: Bearer 550e8400-e29b-41d4-a716-446655440000
```

#### 3. Logout
```http
POST /api/v1/auth/logout
Authorization: Bearer 550e8400-e29b-41d4-a716-446655440000
```

#### 4. Token Refresh (Optional Enhancement)
**Note**: This endpoint is a recommended enhancement for better UX, not yet implemented.

```http
POST /api/v1/auth/refresh
Authorization: Bearer 550e8400-e29b-41d4-a716-446655440000
```

**Response (200 OK)**:
```json
{
  "access_token": "new-550e8400-e29b-41d4-a716-446655440001",
  "token_type": "bearer",
  "expires_in": 86400
}
```

**Implementation Consideration**: This would provide smoother UX by avoiding re-login, but adds complexity. Current design prioritizes simplicity with 24-hour tokens.

### Protected Endpoints

All case-related endpoints require authentication:

```http
GET /api/v1/cases
Authorization: Bearer 550e8400-e29b-41d4-a716-446655440000
X-Session-Id: 41afd36b-3f3c-46dd-8794-1565984d843d
```

**Key Point**: Both headers are required:
- `Authorization`: For user identity and access control
- `X-Session-Id`: For conversation continuity (unchanged from current implementation)

## Frontend Implementation Guide

### 1. Multi-Tab Coordination for Browser Extensions

**Cross-Tab Auth State Synchronization**:
```typescript
class AuthStateManager {
  async broadcastAuthStateChange(authState: AuthState | null): Promise<void> {
    // Notify all extension tabs of auth state changes
    await chrome.runtime.sendMessage({
      type: 'auth_state_changed',
      authState: authState
    });
  }

  setupCrossTabSync(): void {
    // Listen for auth state changes from other tabs
    chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
      if (message.type === 'auth_state_changed') {
        this.updateLocalAuthState(message.authState);
        this.notifyUIComponents();
      }
    });
  }

  private async updateLocalAuthState(authState: AuthState | null): Promise<void> {
    if (authState) {
      await apiClient.setAuthToken(authState.access_token);
    } else {
      await apiClient.clearAuth();
    }
  }
}
```

### 2. API Client Updates

**Enhanced API Client** (`/src/lib/api.ts`):
```typescript
interface ApiClientConfig {
  authToken?: string;
  sessionId?: string;
}

class FaultMavenAPI {
  private authToken?: string;
  private sessionId?: string;

  async setAuthToken(token: string) {
    this.authToken = token;
    await this.saveAuthState();
  }

  async setSessionId(sessionId: string) {
    this.sessionId = sessionId;
  }

  private getHeaders(): Record<string, string> {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json'
    };

    // Add auth header if token available
    if (this.authToken) {
      headers['Authorization'] = `Bearer ${this.authToken}`;
    }

    // Add session header if session available
    if (this.sessionId) {
      headers['X-Session-Id'] = this.sessionId;
    }

    return headers;
  }

  async apiCall(endpoint: string, options: RequestInit = {}) {
    const response = await fetch(`${API_BASE}${endpoint}`, {
      ...options,
      headers: {
        ...this.getHeaders(),
        ...(options.headers || {})
      }
    });

    // Handle auth errors
    if (response.status === 401) {
      await this.handleAuthError();
      throw new AuthenticationError('Authentication required');
    }

    return response;
  }

  private async handleAuthError() {
    // Clear stored auth data
    await chrome.storage.local.remove(['authState']);
    this.authToken = undefined;

    // Trigger re-authentication flow
    await this.showLoginForm();
  }
}
```

### 2. Authentication Storage

**Auth State Management**:
```typescript
interface AuthState {
  access_token: string;
  token_type: "bearer";
  expires_at: number;
  user: UserProfile;
}

class AuthManager {
  async saveAuthState(authState: AuthState): Promise<void> {
    await chrome.storage.local.set({ authState });
  }

  async getAuthState(): Promise<AuthState | null> {
    const result = await chrome.storage.local.get(['authState']);
    const authState = result.authState;

    if (!authState) return null;

    // Check if token is expired
    if (Date.now() >= authState.expires_at) {
      await this.clearAuthState();
      return null;
    }

    return authState;
  }

  async clearAuthState(): Promise<void> {
    await chrome.storage.local.remove(['authState']);
  }

  async isAuthenticated(): Promise<boolean> {
    const authState = await this.getAuthState();
    return authState !== null;
  }
}
```

### 3. Login Flow Integration

**Updated Login Component**:
```typescript
interface LoginFormData {
  username: string;
  email?: string;
  displayName?: string;
}

class LoginFlow {
  async handleLogin(formData: LoginFormData): Promise<void> {
    try {
      // Call dev-login endpoint
      const response = await fetch('/api/v1/auth/dev-login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: formData.username,
          email: formData.email,
          display_name: formData.displayName
        })
      });

      if (!response.ok) {
        throw new Error('Login failed');
      }

      const authResponse = await response.json();

      // Store auth state
      const authState: AuthState = {
        access_token: authResponse.access_token,
        token_type: authResponse.token_type,
        expires_at: Date.now() + (authResponse.expires_in * 1000),
        user: authResponse.user
      };

      await authManager.saveAuthState(authState);
      await apiClient.setAuthToken(authState.access_token);

      // Continue with existing session flow
      await this.initializeSession();

    } catch (error) {
      console.error('Login failed:', error);
      throw error;
    }
  }

  private async initializeSession(): Promise<void> {
    // Create session (unchanged from current logic)
    const sessionResponse = await apiClient.createSession();
    await apiClient.setSessionId(sessionResponse.session_id);
  }
}
```

## Session and Authentication Lifecycle Coordination

### When Authentication Expires

The relationship between session state and authentication state requires careful handling:

```typescript
interface LifecycleStrategy {
  authExpiration: 'preserve_session' | 'clear_session' | 'natural_expiry';
  sessionExpiration: 'require_reauth' | 'allow_anonymous';
}

class AuthSessionCoordinator {
  async handleAuthExpiration(): Promise<void> {
    // Strategy A: Preserve Session, Re-auth User (Recommended)
    // - Keep conversation context intact
    // - Show re-authentication prompt
    // - Continue session after successful re-auth

    const currentSession = await this.getCurrentSession();
    if (currentSession) {
      await this.showReauthPrompt(currentSession.session_id);
      // Session continues with new auth token
    }
  }

  async handleSessionExpiration(): Promise<void> {
    // Strategy: Sessions expire independently of auth
    // - Auth token may still be valid
    // - Create new session with existing auth
    // - Maintain user identity across sessions

    const authState = await authManager.getAuthState();
    if (authState) {
      await this.createNewSession(authState.user.user_id);
    }
  }
}
```

**Design Decision**: **Preserve Session on Auth Expiration**
- Sessions contain valuable conversation context
- Re-authentication maintains case continuity
- Better UX for users working on long troubleshooting sessions

## Error Handling Strategy

### Authentication Error Types

1. **401 Unauthorized**: Token missing, invalid, or expired
2. **403 Forbidden**: Valid token but insufficient permissions
3. **422 Validation Error**: Invalid login credentials

### Error Handling Flow

```typescript
class ErrorHandler {
  async handleApiError(error: Response): Promise<void> {
    switch (error.status) {
      case 401:
        // Token expired or invalid
        await this.handleAuthenticationError();
        break;

      case 403:
        // Valid auth but insufficient permissions
        await this.handlePermissionError();
        break;

      case 422:
        // Validation error in login
        await this.handleValidationError();
        break;
    }
  }

  private async handleAuthenticationError(): Promise<void> {
    // Clear auth state
    await authManager.clearAuthState();

    // Clear API client auth
    apiClient.clearAuth();

    // Show login form
    await this.showLoginForm();
  }
}
```

### Retry Strategy

```typescript
class ApiRetryHandler {
  async callWithRetry(apiCall: () => Promise<Response>): Promise<Response> {
    try {
      return await apiCall();
    } catch (error) {
      if (error instanceof AuthenticationError) {
        // Try to re-authenticate once
        const success = await this.attemptReauth();
        if (success) {
          return await apiCall(); // Retry once after reauth
        }
      }
      throw error;
    }
  }
}
```

## Answers to Frontend Questions

### 1. Token Lifecycle
- **Duration**: 24 hours from creation
- **Refresh**: No refresh mechanism - users re-login when token expires
- **Expiration Triggers**: Time-based only (24 hours)
- **Cleanup**: Automatic backend cleanup of expired tokens

### 2. Development Login Flow
- **Input Field**: Change from email to username (required field)
- **Endpoint Usage**: `/auth/dev-login` is for development/testing only
- **Production**: Will be replaced with OAuth2/OIDC integration
- **Timeline**: Current dev system supports full development workflow

### 3. Error Handling Strategy
- **401 Response**: Clear stored auth, show login form (no automatic retry)
- **Session Data**: Keep session data separate - only clear on explicit logout
- **Partial Failures**: Handle auth and session errors independently

### 4. Backward Compatibility and Progressive Enhancement

**Current Design**: Authentication required for all case operations

**Future Enhancement Consideration**: Optional guest mode for evaluation
```typescript
interface ApiClientConfig {
  authMode: 'required' | 'optional';
  fallbackToGuest?: boolean;
}

class ProgressiveAuthClient {
  async handleUnauthenticatedAccess(): Promise<void> {
    // Option A: Block access, require authentication
    // Option B: Allow limited guest access (read-only cases)
    // Option C: Anonymous case creation with upgrade prompt
  }
}
```

**Design Trade-offs**:
- **Current**: Simple, secure, consistent user experience
- **Future Option**: Guest mode for trial usage, then upgrade to authenticated

**Implementation Status**:
- **Immediate**: Authentication required for all case operations
- **Timeline**: No mixed mode during initial rollout
- **Migration**: Graceful cutover - old tokens continue working until expiration

## Implementation Phases

### Phase 1: API Client Enhancement (Week 1)
- Update api.ts to support dual headers
- Add token storage/retrieval logic
- Implement 401 error handling

### Phase 2: Authentication Flow (Week 1)
- Update login form for username input
- Implement dev-login integration
- Add token persistence

### Phase 3: Error Handling (Week 2)
- Implement retry logic
- Add token expiration detection
- Handle authentication failures gracefully

### Phase 4: Production Readiness (Week 2)
- Add comprehensive error states
- Implement session/auth lifecycle management
- Add authentication status indicators

## Security Considerations

### Frontend Security
- Store tokens in `chrome.storage.local` (not sync storage)
- Clear tokens on authentication errors
- Validate token expiration before API calls
- Never log token values

### Backend Security
- Tokens stored as SHA-256 hashes
- Automatic token cleanup (24-hour TTL)
- Rate limiting on authentication endpoints
- Comprehensive input validation

## Testing Strategy

### Unit Tests
- Auth state management
- Token storage/retrieval
- Error handling logic

### Integration Tests
- End-to-end login flow
- Token expiration handling
- Session + auth coordination

### Manual Testing
- Multi-tab authentication
- Token expiration scenarios
- Network failure recovery

---

## Role-Based Access Control

### Overview

FaultMaven implements role-based access control (RBAC) to manage user permissions for Global Knowledge Base operations. The system uses a simple, flexible role model stored with user profiles.

### User Roles

#### 1. Regular User (`user` role)
Default role for all users. Can:
- ✅ Login and authenticate
- ✅ Search Global KB (read-only)
- ✅ List Global KB documents (read-only)
- ✅ Upload to their own User KB
- ✅ Manage their own User KB documents
- ✅ Use all troubleshooting features
- ❌ Upload to Global KB
- ❌ Modify Global KB documents
- ❌ Delete Global KB documents

#### 2. Admin User (`user` + `admin` roles)
Enhanced permissions for KB management. Can do everything regular users can, PLUS:
- ✅ Upload documents to Global KB
- ✅ Update Global KB documents
- ✅ Delete Global KB documents
- ✅ Bulk update/delete operations on Global KB

### Role Implementation

#### User Model with Roles
```python
@dataclass
class DevUser:
    user_id: str
    username: str
    email: str
    display_name: str
    roles: List[str] = field(default_factory=lambda: ['user'])
    created_at: datetime
    is_dev_user: bool = True
    is_active: bool = True
```

#### API Response with Roles
```json
{
  "user": {
    "user_id": "user-123",
    "username": "alice",
    "email": "alice@company.com",
    "display_name": "Alice Smith",
    "roles": ["user", "admin"],
    "is_dev_user": true,
    "created_at": "2025-10-23T12:00:00Z"
  }
}
```

### Protected Endpoints

#### Admin-Only Endpoints
```python
from faultmaven.api.v1.role_dependencies import require_admin

@router.post("/knowledge/documents")
async def upload_document(
    file: UploadFile,
    current_user: DevUser = Depends(require_admin)  # Admin only
):
    """Upload document to Global KB (admin only)"""
    ...

@router.delete("/knowledge/documents/{doc_id}")
async def delete_document(
    doc_id: str,
    current_user: DevUser = Depends(require_admin)  # Admin only
):
    """Delete Global KB document (admin only)"""
    ...
```

#### Public Endpoints (All Authenticated Users)
```python
from faultmaven.api.v1.auth_dependencies import require_authentication

@router.post("/knowledge/search")
async def search_knowledge(
    request: SearchRequest,
    current_user: DevUser = Depends(require_authentication)  # Any user
):
    """Search Global KB (all users)"""
    ...

@router.get("/knowledge/documents")
async def list_documents(
    current_user: DevUser = Depends(require_authentication)  # Any user
):
    """List Global KB documents (all users)"""
    ...
```

### Authorization Decorators

#### Available Decorators
```python
# 1. Require admin role
from faultmaven.api.v1.role_dependencies import require_admin

@router.post("/admin-endpoint")
async def admin_only(current_user: DevUser = Depends(require_admin)):
    # Only admins can access
    ...

# 2. Require any of multiple roles
from faultmaven.api.v1.role_dependencies import require_roles

@router.post("/editor-endpoint")
async def editor_or_admin(
    current_user: DevUser = Depends(require_roles(['editor', 'admin']))
):
    # Users with 'editor' OR 'admin' role can access
    ...

# 3. Require admin or resource owner
from faultmaven.api.v1.role_dependencies import require_admin_or_owner

@router.delete("/users/{user_id}/resource")
async def delete_resource(
    user_id: str,
    current_user: DevUser = Depends(require_admin_or_owner(user_id))
):
    # Admins or the resource owner can access
    ...
```

### Error Responses

#### 403 Forbidden (Insufficient Permissions)
```json
{
  "error": "Forbidden",
  "message": "This operation requires administrator privileges",
  "required_role": "admin",
  "user_roles": ["user"]
}
```

### User Management

#### Using Management Scripts

**List all users:**
```bash
python scripts/auth/list_users.py
```

**Create a regular user:**
```bash
python scripts/auth/create_user.py --username alice --role user
```

**Create an admin user:**
```bash
python scripts/auth/create_user.py --username bob --role admin
```

**Promote user to admin:**
```bash
python scripts/auth/promote_to_admin.py alice
```

**Demote admin to regular user:**
```bash
python scripts/auth/demote_from_admin.py bob
```

#### Using the API

**Register a new user (defaults to 'user' role):**
```bash
curl -X POST http://localhost:8000/api/v1/auth/dev-register \
  -H 'Content-Type: application/json' \
  -d '{"username": "alice"}'
```

**Login and verify roles:**
```bash
curl -X POST http://localhost:8000/api/v1/auth/dev-login \
  -H 'Content-Type: application/json' \
  -d '{"username": "alice"}'
```

**Get current user profile (includes roles):**
```bash
curl http://localhost:8000/api/v1/auth/me \
  -H 'Authorization: Bearer YOUR_TOKEN'
```

### Frontend Integration

#### Check User Roles
```typescript
interface User {
  user_id: string;
  username: string;
  email: string;
  display_name: string;
  roles: string[];
  is_dev_user: boolean;
  created_at: string;
}

class RoleChecker {
  hasRole(user: User, role: string): boolean {
    return user.roles.includes(role);
  }

  isAdmin(user: User): boolean {
    return this.hasRole(user, 'admin');
  }

  canManageGlobalKB(user: User): boolean {
    return this.isAdmin(user);
  }
}
```

#### Conditional UI Rendering
```typescript
// Show/hide admin features based on roles
const LoginComponent = () => {
  const [user, setUser] = useState<User | null>(null);

  // After login
  const handleLogin = async (username: string) => {
    const response = await fetch('/api/v1/auth/dev-login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username })
    });

    const data = await response.json();
    setUser(data.user);  // Contains roles array
  };

  return (
    <div>
      <h1>Welcome, {user?.display_name}</h1>
      <p>Roles: {user?.roles.join(', ')}</p>

      {/* Show admin UI only for admins */}
      {user?.roles.includes('admin') && (
        <div className="admin-panel">
          <h2>Admin Features</h2>
          <button onClick={uploadToGlobalKB}>
            Upload to Global KB
          </button>
          <button onClick={manageGlobalKB}>
            Manage Global KB
          </button>
        </div>
      )}

      {/* Show user KB for all users */}
      <div className="user-panel">
        <h2>My Knowledge Base</h2>
        <button onClick={uploadToUserKB}>
          Upload to My KB
        </button>
      </div>
    </div>
  );
};
```

### Storage Details

**Redis Storage:**
- User data includes `roles` array
- Redis keys: `auth:user:{user_id}`
- Roles persisted with user profile
- Survives server restarts

**Example User Data in Redis:**
```json
{
  "user_id": "user-123",
  "username": "alice",
  "email": "alice@company.com",
  "display_name": "Alice Smith",
  "roles": ["user", "admin"],
  "created_at": "2025-10-23T12:00:00Z",
  "is_dev_user": true,
  "is_active": true
}
```

### Security Considerations

1. **Role Validation**: All role checks happen server-side
2. **Frontend Roles**: Only used for UI rendering, not security
3. **Token Integrity**: Roles verified on every request
4. **Audit Logging**: All admin operations logged with user ID
5. **Role Changes**: Require new token to reflect updated roles

### Testing

**Test RBAC implementation:**
```bash
cd /home/swhouse/projects/FaultMaven
python scripts/test_rbac.py
```

**Expected output:**
```
✅ Regular user has 'user' role: True
✅ Regular user has 'admin' role: False
✅ Admin user has 'user' role: True
✅ Admin user has 'admin' role: True
✅ All role checks passed!
```

### Migration to Production

When migrating to Auth0/Clerk:
1. **Roles Mapping**: Map FaultMaven roles to Auth0 roles
2. **Claims**: Include roles in JWT claims
3. **Backend Validation**: Verify roles from JWT
4. **Data Migration**: Preserve existing role assignments

**Auth0 Role Configuration:**
```javascript
// Auth0 Custom Claims
function addRolesToToken(user, context, callback) {
  const namespace = 'https://faultmaven.ai/';
  const assignedRoles = context.authorization.roles;

  context.idToken[namespace + 'roles'] = assignedRoles;
  context.accessToken[namespace + 'roles'] = assignedRoles;

  callback(null, user, context);
}
```

---

## Production Migration Strategy

### Current State Assessment

**Development System (Current)**:
- ✅ **Frontend Integration**: Complete browser extension implementation
- ✅ **API Client**: Dual-header system (`Authorization` + `X-Session-Id`)
- ✅ **Token Management**: 24-hour UUID-based tokens
- ✅ **Error Handling**: 401 detection and re-authentication flow
- ❌ **User Database**: Redis-based development storage only
- ❌ **User Management**: No registration, password reset, or email verification
- ❌ **Production Features**: No MFA, SSO, or enterprise authentication

**Production Requirements**:
- Real user database (PostgreSQL/MySQL)
- User registration and account management
- Password management and reset flows
- Email verification and notifications
- Multi-factor authentication (MFA)
- Enterprise SSO integration
- Compliance and security features

### Third-Party Authentication Service Integration

#### Recommended Service: Auth0

**Why Auth0**:
- Enterprise-grade security and compliance
- Extensive customization options
- Advanced features (MFA, SSO, SAML, OIDC)
- Excellent documentation and support
- SOC2, GDPR, HIPAA compliance certifications

**Alternative: Clerk**
- Simpler setup and better developer experience
- Built-in UI components
- Modern architecture
- More cost-effective for smaller teams

#### Integration Architecture

```mermaid
graph TD
    A[Browser Extension] --> B[Auth0/Clerk]
    B --> C[JWT Token]
    C --> D[FaultMaven Backend]
    D --> E[User Lookup by auth0_id]
    E --> F[User Profile + Business Data]
    F --> G[Cases, Conversations, Files]
    
    B --> H[Identity Management]
    D --> I[Business Logic & Data]
    
    style H fill:#e1f5fe
    style I fill:#f3e5f5
```

#### Data Flow with Third-Party Auth

1. **Authentication**: Auth0 handles login, MFA, password reset
2. **Token Generation**: Auth0 issues JWT tokens
3. **User Verification**: Backend validates JWT and looks up user
4. **Business Data**: Backend provides cases, conversations, files
5. **Session Management**: Existing session system remains unchanged

### Database Schema for Production

#### User Profile Table
```sql
CREATE TABLE user_profiles (
  id UUID PRIMARY KEY,
  auth0_id VARCHAR(255) UNIQUE NOT NULL,  -- Links to Auth0 user
  email VARCHAR(255) NOT NULL,
  username VARCHAR(100),
  display_name VARCHAR(200),
  subscription_tier VARCHAR(50) DEFAULT 'free',
  preferences JSONB,
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW(),
  last_login_at TIMESTAMP,
  is_active BOOLEAN DEFAULT true
);

-- Indexes for performance
CREATE INDEX idx_user_profiles_auth0_id ON user_profiles(auth0_id);
CREATE INDEX idx_user_profiles_email ON user_profiles(email);
```

#### Business Data Tables (Existing)
```sql
-- Cases table (existing, add user_id reference)
ALTER TABLE cases ADD COLUMN user_id UUID REFERENCES user_profiles(id);

-- Sessions table (existing, add user_id reference)  
ALTER TABLE sessions ADD COLUMN user_id UUID REFERENCES user_profiles(id);

-- Messages table (existing, add user_id reference)
ALTER TABLE case_messages ADD COLUMN user_id UUID REFERENCES user_profiles(id);
```

### Migration Implementation Plan

#### Phase 1: Auth0 Setup and Configuration (Week 1-2)

**Auth0 Configuration**:
```typescript
// Auth0 configuration
const auth0Config = {
  domain: 'faultmaven.auth0.com',
  clientId: process.env.AUTH0_CLIENT_ID,
  clientSecret: process.env.AUTH0_CLIENT_SECRET,
  audience: 'https://api.faultmaven.ai',
  scope: 'openid profile email',
  responseType: 'code',
  redirectUri: window.location.origin
};
```

**Frontend Integration**:
```typescript
// Replace dev-login with Auth0
import { useAuth0 } from '@auth0/nextjs-auth0';

const { user, getAccessTokenSilently, loginWithRedirect, logout } = useAuth0();

// Authentication flow
const handleLogin = () => loginWithRedirect();
const handleLogout = () => logout({ returnTo: window.location.origin });
```

#### Phase 2: Backend JWT Integration (Week 2-3)

**JWT Validation Middleware**:
```python
# faultmaven/api/v1/auth_dependencies.py
import jwt
from auth0.authentication import Users

async def validate_jwt_token(token: str) -> dict:
    """Validate JWT token from Auth0"""
    try:
        # Verify token with Auth0
        payload = jwt.decode(
            token,
            options={"verify_signature": False}  # Auth0 handles verification
        )
        
        # Get user info from Auth0
        auth0 = Users(domain=settings.AUTH0_DOMAIN)
        user_info = auth0.get_user_info(token)
        
        return user_info
    except jwt.InvalidTokenError:
        raise AuthenticationError("Invalid token")

async def get_current_user_from_jwt(
    token: str = Depends(extract_bearer_token)
) -> UserProfile:
    """Get user profile from JWT token"""
    if not token:
        raise AuthenticationError("No token provided")
    
    # Validate JWT and get Auth0 user info
    auth0_user = await validate_jwt_token(token)
    
    # Look up user in our database
    user_profile = await user_service.get_by_auth0_id(auth0_user['sub'])
    
    if not user_profile:
        # Create new user profile on first login
        user_profile = await user_service.create_from_auth0(auth0_user)
    
    return user_profile
```

**User Service Updates**:
```python
# faultmaven/services/user_service.py
class UserService:
    async def get_by_auth0_id(self, auth0_id: str) -> Optional[UserProfile]:
        """Get user by Auth0 ID"""
        return await self.db.user_profiles.find_by_auth0_id(auth0_id)
    
    async def create_from_auth0(self, auth0_user: dict) -> UserProfile:
        """Create user profile from Auth0 user data"""
        user_profile = UserProfile(
            auth0_id=auth0_user['sub'],
            email=auth0_user['email'],
            username=auth0_user.get('nickname', auth0_user['email']),
            display_name=auth0_user.get('name', auth0_user['email']),
            created_at=datetime.utcnow()
        )
        
        await self.db.user_profiles.create(user_profile)
        return user_profile
```

#### Phase 3: Frontend Migration (Week 3-4)

**API Client Updates**:
```typescript
// src/lib/api.ts - Updated authentication
class AuthManager {
  async getAuthHeaders(): Promise<HeadersInit> {
    const headers: HeadersInit = { 'Content-Type': 'application/json' };

    try {
      // Get Auth0 access token
      const { getAccessTokenSilently } = useAuth0();
      const accessToken = await getAccessTokenSilently();
      
      if (accessToken) {
        headers['Authorization'] = `Bearer ${accessToken}`;
      }

      // Keep existing session logic
      const sessionData = await browser.storage.local.get(['sessionId']);
      if (sessionData.sessionId) {
        headers['X-Session-Id'] = sessionData.sessionId;
      }
    } catch (error) {
      console.warn('[API] Failed to get auth headers:', error);
    }

    return headers;
  }
}
```

**Login Component Updates**:
```typescript
// src/shared/ui/components/LoginForm.tsx
import { useAuth0 } from '@auth0/nextjs-auth0';

export default function LoginForm() {
  const { loginWithRedirect, logout, user, isLoading } = useAuth0();

  if (isLoading) return <LoadingSpinner />;
  
  if (user) {
    return (
      <div className="user-profile">
        <img src={user.picture} alt={user.name} />
        <span>{user.name}</span>
        <button onClick={() => logout()}>Logout</button>
      </div>
    );
  }

  return (
    <div className="login-form">
      <h2>Welcome to FaultMaven</h2>
      <button onClick={() => loginWithRedirect()}>
        Login with Auth0
      </button>
    </div>
  );
}
```

#### Phase 4: Data Migration and Testing (Week 4-5)

**Dev User Migration**:
```python
# Migration script for existing dev users
async def migrate_dev_users():
    """Migrate existing dev users to Auth0-based system"""
    dev_users = await redis_client.keys("auth:user:*")
    
    for user_key in dev_users:
        user_data = await redis_client.get(user_key)
        dev_user = DevUser.from_dict(json.loads(user_data))
        
        # Create Auth0 user (via API or manual process)
        auth0_user = await create_auth0_user(dev_user)
        
        # Create user profile in database
        user_profile = UserProfile(
            auth0_id=auth0_user['user_id'],
            email=dev_user.email,
            username=dev_user.username,
            display_name=dev_user.display_name,
            created_at=dev_user.created_at
        )
        
        await db.user_profiles.create(user_profile)
```

### Benefits of Third-Party Integration

#### Security Benefits
- **Enterprise-grade security** with industry best practices
- **Automatic security updates** and vulnerability patches
- **Compliance certifications** (SOC2, GDPR, HIPAA)
- **Advanced threat protection** and fraud detection

#### Development Benefits
- **90% reduction** in authentication development time
- **No maintenance burden** for auth infrastructure
- **Built-in features** (MFA, SSO, social login)
- **Scalable** to millions of users

#### Business Benefits
- **Faster time to market** for production features
- **Enterprise-ready** authentication from day one
- **Reduced security risk** and liability
- **Focus on core business logic** instead of auth infrastructure

### Cost Analysis

#### Current Development System
- **Development Time**: 6-12 months for production auth
- **Maintenance**: 2-4 hours/week ongoing
- **Security Risk**: High (custom implementation)
- **Total Annual Cost**: $50,000-100,000

#### Third-Party Service (Auth0)
- **Setup Time**: 2-4 weeks
- **Monthly Cost**: $23-240 (based on users)
- **Maintenance**: 1-2 hours/month
- **Security Risk**: Low (expert-managed)
- **Total Annual Cost**: $3,000-5,000

### Implementation Timeline

```mermaid
gantt
    title Auth0 Migration Timeline
    dateFormat  YYYY-MM-DD
    section Phase 1
    Auth0 Setup           :2024-01-01, 7d
    Frontend Integration  :2024-01-08, 7d
    section Phase 2
    Backend JWT Integration :2024-01-15, 7d
    User Service Updates   :2024-01-22, 7d
    section Phase 3
    Frontend Migration     :2024-01-29, 7d
    API Client Updates     :2024-02-05, 7d
    section Phase 4
    Data Migration         :2024-02-12, 7d
    Testing & Deployment   :2024-02-19, 7d
```

## Architectural Compliance

This section documents how the authentication system observes FaultMaven's [architectural design principles](../core-architecture/architectural-design-principles.md).

### Module Boundaries and Contracts

**Principle**: Vertical modules with explicit contracts (Important)

**Implementation**:

```python
# faultmaven/modules/auth/contracts.py
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

@dataclass
class OAuthAuthorizationDTO:
    """Data Transfer Object for OAuth authorization request"""
    client_id: str
    redirect_uri: str
    state: str
    code_challenge: str
    code_challenge_method: str = "S256"
    scope: str = "openid profile email"

@dataclass
class OAuthTokenDTO:
    """Data Transfer Object for OAuth token response"""
    access_token: str
    token_type: str = "Bearer"
    expires_in: int = 86400  # 24 hours
    user_id: str
    username: str

@dataclass
class OAuthCodeDTO:
    """Internal representation of authorization code"""
    code: str
    user_id: str
    redirect_uri: str
    code_challenge: str
    expires_at: datetime
    used: bool = False

class IOAuthService(ABC):
    """
    Contract for OAuth authentication operations.

    This interface defines the boundary between the auth module
    and the rest of the system. All OAuth operations must go through
    this abstraction.
    """

    @abstractmethod
    async def create_authorization_code(
        self,
        user_id: str,
        request: OAuthAuthorizationDTO
    ) -> str:
        """
        Generate authorization code for OAuth flow.

        Args:
            user_id: Authenticated user's ID
            request: OAuth authorization request parameters

        Returns:
            Authorization code (short-lived, single-use)

        Raises:
            InvalidRequestError: If request parameters invalid
        """
        pass

    @abstractmethod
    async def exchange_code_for_token(
        self,
        code: str,
        code_verifier: str,
        redirect_uri: str
    ) -> OAuthTokenDTO:
        """
        Exchange authorization code for access token.

        Args:
            code: Authorization code from authorization endpoint
            code_verifier: PKCE code verifier
            redirect_uri: Must match original redirect_uri

        Returns:
            Access token and user information

        Raises:
            InvalidGrantError: If code invalid, expired, or already used
            PKCEVerificationError: If code_verifier doesn't match code_challenge
        """
        pass

    @abstractmethod
    async def validate_token(self, token: str) -> Optional[str]:
        """
        Validate access token and return user_id.

        Args:
            token: Access token from Authorization header

        Returns:
            user_id if token valid, None otherwise
        """
        pass

    @abstractmethod
    async def revoke_token(self, token: str) -> None:
        """Revoke access token (logout)"""
        pass

class IOAuthCodeRepository(ABC):
    """
    Storage abstraction for OAuth authorization codes.

    This repository handles persistence of short-lived authorization codes
    during the OAuth flow. Implementation can use Redis, PostgreSQL, or
    in-memory storage depending on deployment configuration.
    """

    @abstractmethod
    async def save_code(self, code_data: OAuthCodeDTO) -> None:
        """Store authorization code with PKCE challenge"""
        pass

    @abstractmethod
    async def get_code(self, code: str) -> Optional[OAuthCodeDTO]:
        """Retrieve authorization code data"""
        pass

    @abstractmethod
    async def mark_code_used(self, code: str) -> None:
        """Mark code as used (prevents replay attacks)"""
        pass

    @abstractmethod
    async def delete_expired_codes(self) -> int:
        """Clean up expired codes, returns count deleted"""
        pass
```

**Directory Structure**:
```text
faultmaven/modules/auth/
├── contracts.py                 # IOAuthService, DTOs (this is the boundary)
├── domain/
│   └── oauth_service.py        # OAuthServiceImpl implements IOAuthService
├── infrastructure/
│   ├── oauth_code_repository.py     # Redis/PostgreSQL implementation
│   └── token_repository.py          # Token storage
└── api/
    └── oauth_routes.py         # FastAPI routes (uses IOAuthService)
```

### Storage Abstraction and Database Boundaries

**Principle**: Database-per-module boundaries (Critical), Interface-based design (Critical)

**Implementation**:

```python
# faultmaven/modules/auth/infrastructure/oauth_code_repository.py
from faultmaven.modules.auth.contracts import IOAuthCodeRepository, OAuthCodeDTO
from datetime import datetime, timezone
from typing import Optional
import json

class RedisOAuthCodeRepository(IOAuthCodeRepository):
    """Redis implementation for OAuth code storage"""

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

    async def get_code(self, code: str) -> Optional[OAuthCodeDTO]:
        key = f"{self.key_prefix}{code}"
        value = await self.redis.get(key)
        if not value:
            return None

        data = json.loads(value)
        return OAuthCodeDTO(
            code=code,
            user_id=data["user_id"],
            redirect_uri=data["redirect_uri"],
            code_challenge=data["code_challenge"],
            expires_at=datetime.fromisoformat(data["expires_at"]),
            used=data["used"]
        )

    async def mark_code_used(self, code: str) -> None:
        code_data = await self.get_code(code)
        if code_data:
            code_data.used = True
            await self.save_code(code_data)

    async def delete_expired_codes(self) -> int:
        # Redis TTL handles automatic expiry
        return 0

class PostgresOAuthCodeRepository(IOAuthCodeRepository):
    """PostgreSQL implementation for OAuth code storage (optional)"""

    def __init__(self, db_session):
        self.db = db_session

    async def save_code(self, code_data: OAuthCodeDTO) -> None:
        query = """
            INSERT INTO auth.oauth_codes
            (code, user_id, redirect_uri, code_challenge, expires_at, used)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (code) DO UPDATE SET
                used = EXCLUDED.used
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

    async def get_code(self, code: str) -> Optional[OAuthCodeDTO]:
        query = """
            SELECT code, user_id, redirect_uri, code_challenge, expires_at, used
            FROM auth.oauth_codes
            WHERE code = $1 AND expires_at > NOW()
        """
        row = await self.db.fetchrow(query, code)
        if not row:
            return None

        return OAuthCodeDTO(
            code=row["code"],
            user_id=row["user_id"],
            redirect_uri=row["redirect_uri"],
            code_challenge=row["code_challenge"],
            expires_at=row["expires_at"],
            used=row["used"]
        )

    async def mark_code_used(self, code: str) -> None:
        query = "UPDATE auth.oauth_codes SET used = TRUE WHERE code = $1"
        await self.db.execute(query, code)

    async def delete_expired_codes(self) -> int:
        query = "DELETE FROM auth.oauth_codes WHERE expires_at <= NOW()"
        result = await self.db.execute(query)
        return int(result.split()[-1])  # Extract count from "DELETE N"
```

**Database Schema** (PostgreSQL option):
```sql
-- Schema: auth (owned by auth module)
CREATE SCHEMA IF NOT EXISTS auth;

CREATE TABLE auth.oauth_codes (
    code VARCHAR(128) PRIMARY KEY,
    user_id VARCHAR(20) NOT NULL,
    redirect_uri TEXT NOT NULL,
    code_challenge VARCHAR(128) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    used BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_oauth_codes_expires ON auth.oauth_codes(expires_at)
WHERE NOT used;
```

**No Cross-Module Database Access**:

- Auth module owns `auth.oauth_codes` table
- Other modules MUST NOT query this table directly
- All access through `IOAuthService` contract

### Boundary Enforcement

**Principle**: Enforced architectural boundaries (Important)

**Implementation**: `.import-linter`

```ini
[importlinter]
root_package = faultmaven

[importlinter:contract:auth-module-boundaries]
name = Auth module boundaries
type = layers
layers =
    faultmaven.modules.auth.api
    faultmaven.modules.auth.domain
    faultmaven.modules.auth.infrastructure

[importlinter:contract:auth-contracts-only]
name = Other modules use auth contracts only
type = forbidden
source_modules =
    faultmaven.modules.case
    faultmaven.modules.knowledge
    faultmaven.modules.evidence
    faultmaven.modules.agent
forbidden_modules =
    faultmaven.modules.auth.domain
    faultmaven.modules.auth.infrastructure
    faultmaven.modules.auth.api

[importlinter:contract:auth-no-db-leakage]
name = No direct database access to auth tables
type = forbidden
source_modules =
    faultmaven.modules.case
    faultmaven.modules.knowledge
    faultmaven.modules.evidence
forbidden_modules =
    faultmaven.modules.auth.infrastructure.oauth_code_repository
    faultmaven.modules.auth.infrastructure.token_repository
```

**Verification**:
```bash
# Run boundary checks in CI/CD
lint-imports

# Expected output:
# ✅ Auth module boundaries: PASSED
# ✅ Other modules use auth contracts only: PASSED
# ✅ No direct database access to auth tables: PASSED
```

### Dependency Injection

**Principle**: Composition root pattern (Critical)

**Implementation**:

```python
# faultmaven/main.py (composition root)
from faultmaven.modules.auth.contracts import IOAuthService, IOAuthCodeRepository
from faultmaven.modules.auth.domain.oauth_service import OAuthServiceImpl
from faultmaven.modules.auth.infrastructure.oauth_code_repository import (
    RedisOAuthCodeRepository,
    PostgresOAuthCodeRepository
)

def create_app(config: AppConfig) -> FastAPI:
    app = FastAPI()

    # Wire dependencies at startup
    if config.storage_type == "redis":
        oauth_code_repo: IOAuthCodeRepository = RedisOAuthCodeRepository(
            redis_client=app.state.redis
        )
    else:
        oauth_code_repo: IOAuthCodeRepository = PostgresOAuthCodeRepository(
            db_session=app.state.db
        )

    oauth_service: IOAuthService = OAuthServiceImpl(
        code_repository=oauth_code_repo,
        token_repository=app.state.token_repository,
        user_repository=app.state.user_repository
    )

    # Inject into API routes
    app.dependency_overrides[IOAuthService] = lambda: oauth_service

    return app
```

### Observability

**Principle**: Structured logging and tracing (Recommended)

**Implementation**:

```python
# faultmaven/modules/auth/domain/oauth_service.py
from faultmaven.observability.logger import get_logger
from faultmaven.observability.metrics import counter, histogram
import structlog

logger = structlog.get_logger(__name__)

oauth_code_issued = counter(
    "oauth_code_issued_total",
    "Total authorization codes issued"
)

oauth_token_exchanged = counter(
    "oauth_token_exchanged_total",
    "Total successful token exchanges"
)

oauth_token_exchange_duration = histogram(
    "oauth_token_exchange_duration_seconds",
    "Token exchange request duration"
)

class OAuthServiceImpl(IOAuthService):
    async def create_authorization_code(
        self,
        user_id: str,
        request: OAuthAuthorizationDTO
    ) -> str:
        logger.info(
            "oauth.authorization.start",
            user_id=user_id,
            client_id=request.client_id,
            code_challenge_method=request.code_challenge_method
        )

        code = self._generate_code()

        await self.code_repository.save_code(OAuthCodeDTO(
            code=code,
            user_id=user_id,
            redirect_uri=request.redirect_uri,
            code_challenge=request.code_challenge,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10)
        ))

        oauth_code_issued.inc()

        logger.info(
            "oauth.authorization.success",
            user_id=user_id,
            code_expires_in_seconds=600
        )

        return code

    async def exchange_code_for_token(
        self,
        code: str,
        code_verifier: str,
        redirect_uri: str
    ) -> OAuthTokenDTO:
        with oauth_token_exchange_duration.time():
            logger.info("oauth.token_exchange.start", code_prefix=code[:8])

            # Verify code
            code_data = await self.code_repository.get_code(code)
            if not code_data:
                logger.warning("oauth.token_exchange.invalid_code", code_prefix=code[:8])
                raise InvalidGrantError("Invalid or expired authorization code")

            if code_data.used:
                logger.warning(
                    "oauth.token_exchange.code_reuse_attempt",
                    user_id=code_data.user_id
                )
                raise InvalidGrantError("Authorization code already used")

            # Verify PKCE
            if not self._verify_pkce(code_verifier, code_data.code_challenge):
                logger.warning(
                    "oauth.token_exchange.pkce_failed",
                    user_id=code_data.user_id
                )
                raise PKCEVerificationError("PKCE verification failed")

            # Mark code as used
            await self.code_repository.mark_code_used(code)

            # Generate token
            token = await self.token_repository.create_token(code_data.user_id)

            oauth_token_exchanged.inc()

            logger.info(
                "oauth.token_exchange.success",
                user_id=code_data.user_id,
                token_expires_in_seconds=86400
            )

            return OAuthTokenDTO(
                access_token=token.value,
                user_id=code_data.user_id,
                username=token.username
            )
```

### Deployment Configuration

**Principle**: Deployment-agnostic design (Critical)

**Configuration Options**:

```yaml
# config/local.yml (single-user development)
auth:
  mode: dev-login
  oauth:
    enabled: false
  storage:
    type: in-memory

# config/production.yml (multi-user production)
auth:
  mode: oauth
  oauth:
    enabled: true
    dashboard_url: https://dashboard.faultmaven.com
    token_expiry_seconds: 86400
  storage:
    type: redis
    redis_url: redis://localhost:6379/0

# config/enterprise.yml (on-premises)
auth:
  mode: oauth
  oauth:
    enabled: true
    dashboard_url: https://faultmaven.acme.corp
    token_expiry_seconds: 28800  # 8 hours
  storage:
    type: postgresql
    postgres_url: postgresql://user:pass@localhost:5432/auth
```

**Runtime Selection**:
```python
def create_oauth_service(config: AuthConfig) -> IOAuthService:
    if not config.oauth.enabled:
        return DevLoginAuthService()  # Simple username-only

    # Production OAuth with configurable storage
    if config.storage.type == "redis":
        repo = RedisOAuthCodeRepository(config.storage.redis_url)
    else:
        repo = PostgresOAuthCodeRepository(config.storage.postgres_url)

    return OAuthServiceImpl(code_repository=repo, ...)
```

### Security Principles

**Alignment with OWASP and OAuth 2.0 Best Practices**:

1. **PKCE Mandatory**: Prevents authorization code interception attacks
2. **State Parameter**: CSRF protection during redirect
3. **Short-lived Codes**: 10-minute expiry for authorization codes
4. **Single-use Codes**: Prevents replay attacks
5. **Constant-time Comparison**: Prevents timing attacks on code verification
6. **Secure Token Storage**: SHA-256 hashing in Redis/PostgreSQL
7. **No Client Secrets**: Public client pattern for browser extension

### Test Safety Net

**Principle**: Comprehensive test safety net (Important)

**Test Structure**:

```python
# tests/unit/modules/auth/domain/test_oauth_service.py
import pytest
from faultmaven.modules.auth.contracts import (
    IOAuthService,
    OAuthAuthorizationDTO,
    OAuthCodeDTO
)
from faultmaven.modules.auth.domain.oauth_service import OAuthServiceImpl
from tests.fakes.fake_oauth_code_repository import FakeOAuthCodeRepository

class TestOAuthService:
    @pytest.fixture
    def fake_code_repo(self):
        return FakeOAuthCodeRepository()

    @pytest.fixture
    def oauth_service(self, fake_code_repo) -> IOAuthService:
        return OAuthServiceImpl(code_repository=fake_code_repo)

    async def test_create_authorization_code_success(self, oauth_service):
        request = OAuthAuthorizationDTO(
            client_id="copilot",
            redirect_uri="https://extension.local/callback",
            state="random_state_123",
            code_challenge="E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        )

        code = await oauth_service.create_authorization_code("user_123", request)

        assert len(code) == 128
        # Verify code stored in repository
        stored = await oauth_service.code_repository.get_code(code)
        assert stored.user_id == "user_123"
        assert stored.code_challenge == request.code_challenge

    async def test_exchange_code_for_token_success(self, oauth_service):
        # Setup: Create authorization code
        code = await oauth_service.create_authorization_code(
            "user_123",
            OAuthAuthorizationDTO(
                client_id="copilot",
                redirect_uri="https://extension.local/callback",
                state="state_123",
                code_challenge="E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
            )
        )

        # Execute: Exchange code for token
        token = await oauth_service.exchange_code_for_token(
            code=code,
            code_verifier="dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk",
            redirect_uri="https://extension.local/callback"
        )

        # Assert
        assert token.access_token is not None
        assert token.user_id == "user_123"

        # Verify code marked as used
        stored = await oauth_service.code_repository.get_code(code)
        assert stored.used is True

    async def test_exchange_code_twice_fails(self, oauth_service):
        code = await oauth_service.create_authorization_code(...)

        # First exchange succeeds
        await oauth_service.exchange_code_for_token(code, ...)

        # Second exchange fails
        with pytest.raises(InvalidGrantError, match="already used"):
            await oauth_service.exchange_code_for_token(code, ...)

    async def test_pkce_verification_failure(self, oauth_service):
        code = await oauth_service.create_authorization_code(
            "user_123",
            OAuthAuthorizationDTO(..., code_challenge="correct_challenge")
        )

        # Wrong code_verifier
        with pytest.raises(PKCEVerificationError):
            await oauth_service.exchange_code_for_token(
                code=code,
                code_verifier="wrong_verifier_that_doesnt_match",
                redirect_uri="..."
            )

# tests/integration/modules/auth/test_oauth_flow_e2e.py
class TestOAuthFlowEndToEnd:
    async def test_complete_oauth_flow(self, test_client, test_db):
        # Step 1: User logs into Dashboard
        login_response = await test_client.post("/auth/dev-login", json={
            "username": "alice"
        })
        dashboard_token = login_response.json()["access_token"]

        # Step 2: Dashboard initiates OAuth for Extension
        auth_response = await test_client.get(
            "/auth/oauth/authorize",
            params={
                "client_id": "copilot",
                "redirect_uri": "https://extension.local/callback",
                "state": "xyz",
                "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
                "code_challenge_method": "S256"
            },
            headers={"Authorization": f"Bearer {dashboard_token}"}
        )

        redirect_url = auth_response.headers["Location"]
        code = parse_qs(urlparse(redirect_url).query)["code"][0]

        # Step 3: Extension exchanges code for token
        token_response = await test_client.post("/auth/token", json={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://extension.local/callback",
            "code_verifier": "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        })

        extension_token = token_response.json()["access_token"]

        # Step 4: Extension calls API with token
        cases_response = await test_client.get(
            "/api/v1/cases",
            headers={"Authorization": f"Bearer {extension_token}"}
        )

        assert cases_response.status_code == 200
```

### Summary: Architectural Compliance

| Principle                           | Compliance Level | Implementation                                          |
|-------------------------------------|------------------|---------------------------------------------------------|
| **Vertical Modules with Contracts** | ✅ Full          | `contracts.py` with `IOAuthService`, DTOs               |
| **Database-per-Module Boundaries**  | ✅ Full          | `auth.oauth_codes` table, no cross-module JOINs         |
| **Interface-Based Design**          | ✅ Full          | `IOAuthService`, `IOAuthCodeRepository` abstractions    |
| **Deployment-Agnostic**             | ✅ Full          | Config-driven (dev-login vs OAuth, Redis vs PostgreSQL) |
| **Composition Root Pattern**        | ✅ Full          | Dependency injection in `main.py`                       |
| **Boundary Enforcement**            | ✅ Full          | `.import-linter` rules for auth module                  |
| **Errors as Domain Concepts**       | ✅ Full          | `InvalidGrantError`, `PKCEVerificationError`            |
| **Observability**                   | ✅ Full          | Structured logging, Prometheus metrics                  |
| **Test Safety Net**                 | ⚠️ Partial       | Unit tests designed, integration tests needed           |

**Migration Path**:
1. ✅ Document OAuth flow with PKCE (completed)
2. ⬜ Create `contracts.py` with interfaces
3. ⬜ Implement `OAuthServiceImpl` and repositories
4. ⬜ Add `.import-linter` rules
5. ⬜ Write comprehensive tests
6. ⬜ Deploy to staging for validation

## Quick Reference

### Required Changes Summary
1. **API Client**: Add `Authorization: Bearer <token>` header to all case operations
2. **Login Form**: Change email input to username input
3. **Storage**: Add token storage alongside existing session storage
4. **Error Handling**: Add 401 detection and re-authentication flow

### Key Integration Points
- Keep existing `X-Session-Id` logic unchanged
- Add `Authorization` header for user identity
- Both headers required for case operations
- Independent error handling for auth vs session failures

### Migration Checklist
- [ ] Set up Auth0/Clerk account and configuration
- [ ] Create production user database schema
- [ ] Implement JWT validation middleware
- [ ] Update frontend to use OAuth flow
- [ ] Migrate existing dev users
- [ ] Test end-to-end authentication flow
- [ ] Deploy to production environment