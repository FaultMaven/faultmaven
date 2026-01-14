# Authentication Flow Across Components

This document explains how users get authorized through the UI and how the three components (faultmaven backend, faultmaven-dashboard, and faultmaven-copilot) communicate to allow access to API services.

## Overview

The authentication system uses **JWT (JSON Web Tokens)** with Bearer token authentication. All three components share the same authentication mechanism but store tokens differently based on their runtime environment.

## Component Roles

1. **faultmaven** (Backend API): Issues and validates JWT tokens
2. **faultmaven-dashboard** (Web UI): Web-based interface for authentication and management
3. **faultmaven-copilot** (Browser Extension): Browser extension that uses the same authentication

## Authentication Flow

### Step 1: User Initiates Login

#### Dashboard Flow
1. User navigates to the dashboard login page (`LoginPage.tsx`)
2. User enters username (development mode) or email/password (production)
3. Dashboard calls `devLogin(username)` from `src/lib/auth/functions.ts`

```typescript
// Dashboard: src/lib/auth/functions.ts
export async function devLogin(username: string): Promise<AuthState> {
  const response = await fetch(`${config.apiUrl}/api/v1/auth/dev-login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username }),
  });
  // ... handles response and stores auth state
}
```

#### Copilot Extension Flow
1. User clicks login in the extension UI
2. Extension calls `devLogin()` from `src/lib/api/services/auth-service.ts`
3. Same API endpoint is used: `/api/v1/auth/dev-login`

```typescript
// Copilot: src/lib/api/services/auth-service.ts
export async function devLogin(
  username: string,
  email?: string,
  displayName?: string
): Promise<AuthTokenResponse> {
  const response = await fetch(`${await getApiUrl()}/api/v1/auth/dev-login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, email, display_name: displayName }),
  });
  // ... stores auth state in browser.storage.local
}
```

### Step 2: Backend Validates and Issues Token

The backend receives the login request at `/api/v1/auth/dev-login`:

```python
# Backend: faultmaven/api/routes/auth.py
@router.post("/dev-login", response_model=AuthTokenResponse)
async def dev_login(
    request: DevLoginRequest,
    response: Response,
    user_store: DevUserStore = Depends(get_user_store),
    token_manager: DevTokenManager = Depends(get_token_manager)
) -> AuthTokenResponse:
    # 1. Look up user by username
    user = await user_store.get_user_by_username(request.username)
    
    # 2. Generate JWT access token
    access_token = await token_manager.create_token(user)
    
    # 3. Build user profile with roles
    user_profile = UserProfile(
        user_id=user.user_id,
        username=user.username,
        email=user.email,
        display_name=user.display_name,
        roles=user.roles if user.roles else ['admin']
    )
    
    # 4. Return token response
    return AuthTokenResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=3600,  # 1 hour
        user=user_profile
    )
```

**Response Structure:**
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer",
  "expires_in": 3600,
  "user": {
    "user_id": "user-123",
    "username": "john_doe",
    "email": "john@example.com",
    "display_name": "John Doe",
    "roles": ["admin"]
  }
}
```

### Step 3: Client Stores Authentication State

#### Dashboard Storage
The dashboard stores auth state in **localStorage** (web browser storage):

```typescript
// Dashboard: src/lib/auth/AuthManager.ts
export class AuthManager {
  async saveAuthState(authState: AuthState): Promise<void> {
    if (browser?.storage) {
      await browser.storage.local.set({ authState });
    }
  }
}
```

**AuthState Structure:**
```typescript
interface AuthState {
  access_token: string;
  token_type: "bearer";
  expires_at: number;  // Unix timestamp (ms)
  user: {
    user_id: string;
    username: string;
    email: string;
    display_name: string;
    roles?: string[];
  };
}
```

#### Copilot Extension Storage
The extension stores auth state in **browser.storage.local** (extension storage):

```typescript
// Copilot: src/lib/auth/auth-manager.ts
export class AuthManager {
  async saveAuthState(authState: AuthState): Promise<void> {
    if (typeof browser !== 'undefined' && browser.storage) {
      await browser.storage.local.set({ authState });
    }
  }
}
```

Both use the same structure, but different storage APIs based on their environment.

### Step 4: Subsequent API Requests Include Token

When making authenticated API requests, both components attach the token in the `Authorization` header.

#### Dashboard API Calls
```typescript
// Dashboard uses fetch directly with token from AuthManager
const token = await authManager.getAccessToken();
fetch(url, {
  headers: {
    'Authorization': `Bearer ${token}`,
    'Content-Type': 'application/json'
  }
});
```

#### Copilot Extension API Calls
The extension uses a centralized `authenticatedFetch` wrapper:

```typescript
// Copilot: src/lib/api/client.ts
export async function authenticatedFetch(url: string, options: RequestInit = {}): Promise<Response> {
  const headers = await getAuthHeaders();  // Gets token from storage
  
  const response = await fetch(url, {
    ...options,
    headers: {
      ...headers,  // Includes Authorization: Bearer <token>
      ...(options.headers || {})
    }
  });
  
  // Handles 401 errors and token expiration
  if (response.status === 401) {
    // Triggers re-authentication
  }
  
  return response;
}
```

**Header Construction:**
```typescript
// Copilot: src/lib/api/fetch-utils.ts
export async function getAuthHeaders(): Promise<HeadersInit> {
  const headers: HeadersInit = { 'Content-Type': 'application/json' };
  
  const authState = await authManager.getAuthState();
  if (authState?.access_token) {
    headers['Authorization'] = `Bearer ${authState.access_token}`;
  }
  
  // Also includes session ID for troubleshooting sessions
  const sessionData = await browser.storage.local.get(['sessionId']);
  if (sessionData.sessionId) {
    headers['X-Session-Id'] = sessionData.sessionId;
  }
  
  return headers;
}
```

### Step 5: Backend Validates Token on Each Request

The backend validates the JWT token on every authenticated request:

```python
# Backend: faultmaven/api/middleware/auth.py
async def get_current_user(
    authorization: Optional[str] = Header(None, alias="Authorization"),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    auth_service: AuthService = Depends(get_auth_service),
) -> AuthenticatedUser:
    # 1. Extract token from Authorization header
    token = _extract_token(authorization, credentials)
    
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")
    
    # 2. Verify token and check revocation
    user = await auth_service.extract_user_from_token_with_revocation_check(token)
    
    # 3. Return authenticated user with roles and permissions
    return user
```

**Token Validation Process:**
1. Extract `Bearer <token>` from `Authorization` header
2. Verify JWT signature using secret key
3. Check token expiration (`exp` claim)
4. Check token revocation (if Redis is configured)
5. Extract user claims (user_id, organization_id, email, roles)
6. Return `AuthenticatedUser` object

**Usage in API Routes:**
```python
# Backend: Example route with authentication
@router.get("/api/v1/cases")
async def list_cases(
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    # current_user is automatically validated
    # Contains: user_id, organization_id, email, roles, permissions
    return await case_service.list_cases(user_id=current_user.user_id)
```

## Cross-Component Communication

### Dashboard ↔ Backend
- **Protocol**: HTTP/REST
- **Authentication**: Bearer token in `Authorization` header
- **Storage**: Browser localStorage
- **Token Refresh**: Manual re-login when token expires

### Copilot Extension ↔ Backend
- **Protocol**: HTTP/REST
- **Authentication**: Bearer token in `Authorization` header + Session ID in `X-Session-Id` header
- **Storage**: `browser.storage.local` (extension storage)
- **Token Refresh**: Automatic retry with session refresh on 401 errors

### Dashboard ↔ Copilot Extension
- **Communication**: Indirect (both communicate with backend separately)
- **Shared State**: Both use same JWT tokens from same backend
- **Extension Login Flow**: Extension can open dashboard login page with `?source=extension` parameter

## Session Management (Copilot Extension Only)

The copilot extension uses an additional **session** concept for troubleshooting workflows:

1. **Session Creation**: Creates a session via `/api/v1/sessions` endpoint
2. **Session ID**: Stored separately from auth token
3. **Session Header**: Included as `X-Session-Id` in API requests
4. **Session Expiration**: Handled separately from token expiration
5. **Session Refresh**: Automatic refresh on `SESSION_EXPIRED` errors

```typescript
// Copilot includes both headers
headers = {
  'Authorization': 'Bearer <jwt_token>',  // For authentication
  'X-Session-Id': '<session_id>'          // For troubleshooting session
}
```

## Error Handling

### Token Expiration
- **Dashboard**: Detects expired token on API call, redirects to login
- **Copilot**: Detects 401 response, clears auth state, shows login UI

### Authentication Errors
- **401 Unauthorized**: Token missing or invalid → Clear auth state, show login
- **403 Forbidden**: Token revoked → Clear auth state, show login
- **Session Expired**: (Copilot only) Refresh session automatically

### Network Errors
- Both components handle network failures gracefully
- Show user-friendly error messages
- Allow retry without losing form data

## Security Considerations

1. **Token Storage**: 
   - Dashboard: localStorage (accessible to JavaScript, vulnerable to XSS)
   - Extension: browser.storage.local (isolated, more secure)

2. **Token Transmission**: Always over HTTPS in production

3. **Token Expiration**: Tokens expire after 1 hour (configurable)

4. **Token Revocation**: Supported via Redis (if configured)

5. **CORS**: Backend must allow requests from dashboard origin

## Summary Flow Diagram

```
┌─────────────┐         ┌─────────────┐         ┌─────────────┐
│  Dashboard  │         │   Backend   │         │   Copilot   │
│   (Web UI)  │         │    (API)    │         │ (Extension) │
└──────┬──────┘         └──────┬──────┘         └──────┬──────┘
       │                       │                       │
       │  1. POST /dev-login   │                       │
       │──────────────────────>│                       │
       │                       │                       │
       │  2. Validate user     │                       │
       │     Generate JWT      │                       │
       │                       │                       │
       │  3. Return token      │                       │
       │<──────────────────────│                       │
       │                       │                       │
       │  4. Store in          │                       │
       │     localStorage      │                       │
       │                       │                       │
       │                       │  1. POST /dev-login   │
       │                       │<──────────────────────│
       │                       │                       │
       │                       │  2. Validate user     │
       │                       │     Generate JWT      │
       │                       │                       │
       │                       │  3. Return token      │
       │                       │──────────────────────>│
       │                       │                       │
       │                       │  4. Store in         │
       │                       │     browser.storage   │
       │                       │                       │
       │  5. API Request       │                       │
       │     + Bearer token    │                       │
       │──────────────────────>│                       │
       │                       │                       │
       │                       │  6. Validate token    │
       │                       │     Extract user      │
       │                       │                       │
       │  7. API Response      │                       │
       │<──────────────────────│                       │
       │                       │                       │
       │                       │  5. API Request       │
       │                       │     + Bearer token    │
       │                       │     + Session ID      │
       │                       │<──────────────────────│
       │                       │                       │
       │                       │  6. Validate token    │
       │                       │     Validate session  │
       │                       │                       │
       │                       │  7. API Response      │
       │                       │──────────────────────>│
       │                       │                       │
```

## Key Files Reference

### Backend (faultmaven)
- `faultmaven/api/routes/auth.py` - Login endpoint
- `faultmaven/api/middleware/auth.py` - Token validation middleware
- `faultmaven/api/v1/auth_dependencies.py` - Auth dependencies

### Dashboard (faultmaven-dashboard)
- `src/lib/auth/functions.ts` - Login/logout functions
- `src/lib/auth/AuthManager.ts` - Auth state management
- `src/context/AuthContext.tsx` - React context for auth state
- `src/pages/LoginPage.tsx` - Login UI

### Copilot (faultmaven-copilot)
- `src/lib/api/services/auth-service.ts` - Login service
- `src/lib/auth/auth-manager.ts` - Auth state management
- `src/lib/api/client.ts` - Authenticated fetch wrapper
- `src/lib/api/fetch-utils.ts` - Header construction
- `src/shared/ui/hooks/useAuth.ts` - React hook for auth
