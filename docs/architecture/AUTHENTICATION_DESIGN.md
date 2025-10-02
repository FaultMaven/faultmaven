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
  "user": {
    "user_id": "user_f939a782",
    "username": "user123",
    "email": "user@example.com",
    "display_name": "User Name",
    "is_dev_user": true,
    "is_active": true
  }
}
```

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