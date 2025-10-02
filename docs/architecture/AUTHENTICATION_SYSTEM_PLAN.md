# FaultMaven Authentication System Implementation Plan

## Overview

This document outlines the complete implementation plan for FaultMaven's token-based authentication system. The system is designed to be clean, maintainable, and easily replaceable with production OAuth2/JWT systems.

## Architecture Principles

### Core Design Goals
1. **Separation of Concerns**: Authentication logic completely decoupled from business logic
2. **Future Compatibility**: Easy migration to production IdP (OAuth2, JWT)
3. **Clean Dependencies**: Services receive authenticated user_id, never extract it themselves
4. **Testability**: Each component can be mocked and tested independently
5. **Consistency**: Uniform error handling and response formats

### Authentication Flow
```
Client Request → Bearer Token → Auth Dependency → User Context → Service Layer
     ↓              ↓              ↓               ↓              ↓
 HTTP Headers → Token Manager → User Resolver → Route Handler → Business Logic
```

## Phase-by-Phase Implementation

### Phase 1: Foundation - User Model & Token Management

**Goal**: Create the core data models and token management infrastructure.

**Files to Create:**
- `faultmaven/models/auth.py` - User and token data models
- `faultmaven/infrastructure/auth/token_manager.py` - Token CRUD operations
- `faultmaven/infrastructure/auth/user_store.py` - User storage operations

**Data Models:**
```python
@dataclass
class DevUser:
    user_id: str           # Primary identifier
    username: str          # Unique username
    email: str            # User email
    display_name: str     # Human-readable name
    created_at: datetime  # Account creation time
    is_dev_user: bool     # Development flag
    is_active: bool       # Account status

@dataclass
class AuthToken:
    token_id: str         # Token identifier
    user_id: str         # Associated user
    token_hash: str      # Hashed token value
    expires_at: datetime # Token expiration
    created_at: datetime # Token creation time
    last_used_at: Optional[datetime] # Last usage tracking
```

**Token Management Strategy:**
- Redis-based storage for performance
- 24-hour token expiration
- UUID-based token generation
- Automatic cleanup of expired tokens

### Phase 2: Authentication API Endpoints

**Goal**: Implement dev-login and user management endpoints.

**Files to Create:**
- `faultmaven/api/v1/routes/auth.py` - Authentication endpoints
- `faultmaven/models/api_auth.py` - Auth API request/response models

**Endpoints:**
- `POST /api/v1/auth/dev-login` - Developer login with username
- `POST /api/v1/auth/logout` - Token revocation
- `GET /api/v1/auth/me` - Current user profile

**Request/Response Models:**
```python
class DevLoginRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    email: Optional[str] = None
    display_name: Optional[str] = None

class AuthTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserProfile
```

### Phase 3: Clean Authentication Dependencies

**Goal**: Create FastAPI dependencies for token extraction and user resolution.

**Files to Create:**
- `faultmaven/api/v1/auth_dependencies.py` - Clean auth dependencies

**Dependency Functions:**
- `extract_bearer_token()` - Extract token from Authorization header
- `get_current_user()` - Resolve user from token (optional)
- `require_authenticated_user()` - Require auth, raise 401 if missing
- `optional_user()` - Get user if authenticated, None otherwise

**Error Handling:**
```python
class AuthenticationError(HTTPException):
    status_code = 401
    headers = {"WWW-Authenticate": "Bearer"}

class InvalidTokenError(AuthenticationError):
    detail = "Invalid or expired authentication token"
```

### Phase 4: Route Migration

**Goal**: Update all protected routes to use the new authentication system.

**Migration Pattern:**
```python
# OLD (remove):
user_id: str = Depends(_di_require_authenticated_user_dependency)

# NEW (replace with):
current_user: DevUser = Depends(require_authenticated_user)

# Service Layer (explicit user_id passing):
await case_service.create_case(owner_id=current_user.user_id, ...)
```

**Service Layer Updates:**
- Remove all authentication logic from services
- Services receive user_id as explicit parameters
- Pure business logic with clear contracts

### Phase 5: Legacy Code Cleanup

**Goal**: Remove all existing authentication code and patterns.

**Files to Clean:**
- `faultmaven/api/v1/dependencies.py` - Remove old auth functions
- `faultmaven/api/v1/routes/case.py` - Update to new auth pattern
- `faultmaven/api/v1/routes/session.py` - Remove session auth

**Cleanup Tasks:**
1. Remove session-based authentication
2. Remove mixed authentication patterns
3. Clean up dependency injection containers
4. Update all route handlers consistently

### Phase 6: Testing & Validation

**Goal**: Comprehensive testing of the authentication system.

**Testing Strategy:**
- Unit tests for token management
- Integration tests for auth endpoints
- Route-level tests for protected endpoints
- Error handling validation
- Performance testing for token operations

**Test Fixtures:**
```python
@pytest.fixture
def auth_client():
    """HTTP client with valid dev token"""

@pytest.fixture
def dev_user():
    """Sample dev user for testing"""

@pytest.fixture
def invalid_token():
    """Invalid token for error testing"""
```

## Technical Specifications

### Token Format
- **Type**: UUID v4 (e.g., `550e8400-e29b-41d4-a716-446655440000`)
- **Storage**: SHA-256 hash in Redis
- **Expiration**: 24 hours from creation
- **Cleanup**: Automatic removal of expired tokens

### Redis Storage Schema
```
# User storage
user:{user_id} → {user_json}
user:username:{username} → {user_id}

# Token storage
token:{token_hash} → {user_id}
token:user:{user_id} → [{token_id}, ...]
```

### HTTP Authentication Header
```
Authorization: Bearer 550e8400-e29b-41d4-a716-446655440000
```

### Error Response Format
```json
{
  "detail": "Authentication required",
  "error_type": "AuthenticationError",
  "correlation_id": "uuid-here",
  "timestamp": "2025-09-26T10:00:00Z"
}
```

## Security Considerations

### Development Mode Security
- Tokens stored as SHA-256 hashes
- Automatic token expiration
- Rate limiting on auth endpoints
- Input validation on all fields

### Production Migration Path
When migrating to production IdP:
1. Replace `DevTokenManager` with `JWTTokenManager`
2. Update `extract_bearer_token` to validate JWT signatures
3. Replace `DevUser` with `ProductionUser` model
4. All route handlers remain unchanged (clean interface)

## Implementation Dependencies

### Required Infrastructure
- Redis server for token/user storage
- Container service registration
- Logging integration
- Error handling middleware

### External Libraries
- `redis` - Token storage
- `hashlib` - Token hashing
- `uuid` - Token generation
- `datetime` - Expiration management

## Success Criteria

### Phase Completion Criteria
1. **Phase 1**: Token manager passes unit tests
2. **Phase 2**: Dev-login endpoint working end-to-end
3. **Phase 3**: New auth dependencies working
4. **Phase 4**: All routes migrated successfully
5. **Phase 5**: No legacy auth code remains
6. **Phase 6**: Full test coverage achieved

### Overall Success Metrics
- All protected endpoints require valid tokens
- Token generation/validation working reliably
- Clean separation between auth and business logic
- Easy testing with predictable auth tokens
- Ready for production IdP integration

## Production Migration Strategy

### Current Implementation Status

**Development System (Completed)**:
- ✅ **Core Infrastructure**: Token management, user storage, API endpoints
- ✅ **Frontend Integration**: Complete browser extension implementation
- ✅ **API Dependencies**: Clean authentication dependencies for FastAPI
- ✅ **Error Handling**: Comprehensive error handling and retry logic
- ❌ **Production Database**: Redis-based development storage only
- ❌ **User Management**: No registration, password reset, or email verification
- ❌ **Enterprise Features**: No MFA, SSO, or compliance features

### Third-Party Authentication Service Integration

#### Service Selection Criteria

**Primary Recommendation: Auth0**
- **Enterprise Security**: SOC2, GDPR, HIPAA compliance
- **Advanced Features**: MFA, SSO, SAML, OIDC, social login
- **Scalability**: Handles millions of users
- **Customization**: Extensive configuration options
- **Support**: Enterprise-grade support and documentation

**Alternative: Clerk**
- **Developer Experience**: Simpler setup and better DX
- **Modern Architecture**: Built for modern applications
- **Cost-Effective**: More affordable for smaller teams
- **UI Components**: Pre-built authentication components
- **Quick Setup**: Faster time to implementation

#### Integration Architecture

```mermaid
graph TD
    A[Browser Extension] --> B[Auth0/Clerk]
    B --> C[JWT Token]
    C --> D[FaultMaven Backend]
    D --> E[User Profile Lookup]
    E --> F[Business Data Access]
    F --> G[Cases, Sessions, Files]
    
    B --> H[Identity Management]
    D --> I[Business Logic & Data]
    
    style H fill:#e1f5fe
    style I fill:#f3e5f5
```

### Database Schema Design

#### Production User Profile Schema
```sql
-- User profiles table (links to Auth0/Clerk)
CREATE TABLE user_profiles (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  auth0_id VARCHAR(255) UNIQUE NOT NULL,  -- External auth provider ID
  email VARCHAR(255) NOT NULL,
  username VARCHAR(100) UNIQUE,
  display_name VARCHAR(200),
  avatar_url VARCHAR(500),
  subscription_tier VARCHAR(50) DEFAULT 'free',
  preferences JSONB DEFAULT '{}',
  metadata JSONB DEFAULT '{}',
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW(),
  last_login_at TIMESTAMP,
  is_active BOOLEAN DEFAULT true,
  email_verified BOOLEAN DEFAULT false
);

-- Indexes for performance
CREATE INDEX idx_user_profiles_auth0_id ON user_profiles(auth0_id);
CREATE INDEX idx_user_profiles_email ON user_profiles(email);
CREATE INDEX idx_user_profiles_username ON user_profiles(username);
CREATE INDEX idx_user_profiles_active ON user_profiles(is_active);
```

#### Business Data Integration
```sql
-- Add user references to existing tables
ALTER TABLE cases ADD COLUMN user_id UUID REFERENCES user_profiles(id);
ALTER TABLE sessions ADD COLUMN user_id UUID REFERENCES user_profiles(id);
ALTER TABLE case_messages ADD COLUMN user_id UUID REFERENCES user_profiles(id);
ALTER TABLE file_uploads ADD COLUMN user_id UUID REFERENCES user_profiles(id);

-- Add indexes for user-based queries
CREATE INDEX idx_cases_user_id ON cases(user_id);
CREATE INDEX idx_sessions_user_id ON sessions(user_id);
CREATE INDEX idx_messages_user_id ON case_messages(user_id);
```

### Migration Implementation Phases

#### Phase 7: Third-Party Auth Setup (Week 4-5)

**Auth0 Configuration**:
```python
# faultmaven/config/auth0.py
from pydantic import BaseSettings

class Auth0Settings(BaseSettings):
    domain: str
    client_id: str
    client_secret: str
    audience: str
    scope: str = "openid profile email"
    
    class Config:
        env_prefix = "AUTH0_"

auth0_settings = Auth0Settings()
```

**JWT Validation Service**:
```python
# faultmaven/infrastructure/auth/jwt_validator.py
import jwt
from auth0.authentication import Users
from faultmaven.config.auth0 import auth0_settings

class JWTValidator:
    def __init__(self):
        self.auth0 = Users(domain=auth0_settings.domain)
    
    async def validate_token(self, token: str) -> dict:
        """Validate JWT token and return user info"""
        try:
            # Verify token with Auth0
            user_info = self.auth0.get_user_info(token)
            return user_info
        except Exception as e:
            raise AuthenticationError(f"Invalid token: {str(e)}")
    
    async def get_user_profile(self, token: str) -> UserProfile:
        """Get user profile from JWT token"""
        auth0_user = await self.validate_token(token)
        
        # Look up user in our database
        user_profile = await self.user_service.get_by_auth0_id(auth0_user['sub'])
        
        if not user_profile:
            # Create new user profile on first login
            user_profile = await self.user_service.create_from_auth0(auth0_user)
        
        return user_profile
```

#### Phase 8: User Service Migration (Week 5-6)

**Updated User Service**:
```python
# faultmaven/services/user_service.py
from faultmaven.models.user_profile import UserProfile
from faultmaven.infrastructure.auth.jwt_validator import JWTValidator

class UserService:
    def __init__(self, db: Database, jwt_validator: JWTValidator):
        self.db = db
        self.jwt_validator = jwt_validator
    
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
            avatar_url=auth0_user.get('picture'),
            email_verified=auth0_user.get('email_verified', False),
            created_at=datetime.utcnow()
        )
        
        await self.db.user_profiles.create(user_profile)
        return user_profile
    
    async def update_last_login(self, user_id: str) -> None:
        """Update last login timestamp"""
        await self.db.user_profiles.update_last_login(user_id)
```

**Updated Authentication Dependencies**:
```python
# faultmaven/api/v1/auth_dependencies.py
from faultmaven.infrastructure.auth.jwt_validator import JWTValidator

async def get_current_user_from_jwt(
    token: str = Depends(extract_bearer_token),
    jwt_validator: JWTValidator = Depends(get_jwt_validator)
) -> UserProfile:
    """Get user profile from JWT token"""
    if not token:
        raise AuthenticationError("No token provided")
    
    return await jwt_validator.get_user_profile(token)

# Update existing dependencies to use JWT
async def require_authenticated_user(
    current_user: UserProfile = Depends(get_current_user_from_jwt)
) -> UserProfile:
    """Require authenticated user with JWT validation"""
    return current_user
```

#### Phase 9: Frontend Migration (Week 6-7)

**Auth0 Frontend Integration**:
```typescript
// src/lib/auth0.ts
import { createAuth0Client } from '@auth0/auth0-spa-js';

const auth0Config = {
  domain: process.env.VITE_AUTH0_DOMAIN,
  clientId: process.env.VITE_AUTH0_CLIENT_ID,
  audience: process.env.VITE_AUTH0_AUDIENCE,
  scope: 'openid profile email',
  redirectUri: window.location.origin
};

export const auth0Client = await createAuth0Client(auth0Config);

// Authentication state management
export class Auth0Manager {
  async login(): Promise<void> {
    await auth0Client.loginWithRedirect();
  }
  
  async logout(): Promise<void> {
    await auth0Client.logout({
      returnTo: window.location.origin
    });
  }
  
  async getAccessToken(): Promise<string | null> {
    try {
      return await auth0Client.getTokenSilently();
    } catch (error) {
      console.warn('[Auth0] Failed to get access token:', error);
      return null;
    }
  }
  
  async getUser(): Promise<any> {
    return await auth0Client.getUser();
  }
}
```

**Updated API Client**:
```typescript
// src/lib/api.ts - Updated for Auth0
import { auth0Manager } from './auth0';

class FaultMavenAPI {
  private async getAuthHeaders(): Promise<HeadersInit> {
    const headers: HeadersInit = { 'Content-Type': 'application/json' };

    try {
      // Get Auth0 access token
      const accessToken = await auth0Manager.getAccessToken();
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

  async apiCall(endpoint: string, options: RequestInit = {}) {
    const headers = await this.getAuthHeaders();
    
    const response = await fetch(`${API_BASE}${endpoint}`, {
      ...options,
      headers: {
        ...headers,
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
}
```

#### Phase 10: Data Migration (Week 7-8)

**Dev User Migration Script**:
```python
# scripts/migrate_dev_users.py
import asyncio
import json
from faultmaven.infrastructure.auth.dev_user_store import DevUserStore
from faultmaven.services.user_service import UserService
from faultmaven.infrastructure.database import Database

async def migrate_dev_users():
    """Migrate existing dev users to Auth0-based system"""
    dev_store = DevUserStore(redis_client)
    user_service = UserService(database)
    
    # Get all dev users from Redis
    dev_users = await dev_store.list_all_users()
    
    migration_results = {
        'success': 0,
        'failed': 0,
        'errors': []
    }
    
    for dev_user in dev_users:
        try:
            # Create Auth0 user (via API or manual process)
            auth0_user = await create_auth0_user(dev_user)
            
            # Create user profile in database
            user_profile = await user_service.create_from_auth0(auth0_user)
            
            # Migrate user's cases and sessions
            await migrate_user_data(dev_user.user_id, user_profile.id)
            
            migration_results['success'] += 1
            print(f"✅ Migrated user: {dev_user.username}")
            
        except Exception as e:
            migration_results['failed'] += 1
            migration_results['errors'].append({
                'user': dev_user.username,
                'error': str(e)
            })
            print(f"❌ Failed to migrate user: {dev_user.username} - {e}")
    
    return migration_results

async def migrate_user_data(old_user_id: str, new_user_id: str):
    """Migrate user's cases, sessions, and messages"""
    # Update cases
    await database.execute(
        "UPDATE cases SET user_id = %s WHERE user_id = %s",
        (new_user_id, old_user_id)
    )
    
    # Update sessions
    await database.execute(
        "UPDATE sessions SET user_id = %s WHERE user_id = %s",
        (new_user_id, old_user_id)
    )
    
    # Update messages
    await database.execute(
        "UPDATE case_messages SET user_id = %s WHERE user_id = %s",
        (new_user_id, old_user_id)
    )

if __name__ == "__main__":
    asyncio.run(migrate_dev_users())
```

### Benefits Analysis

#### Development Benefits
- **90% reduction** in authentication development time
- **No maintenance burden** for auth infrastructure
- **Built-in features** (MFA, SSO, social login, password reset)
- **Scalable** to millions of users without infrastructure changes
- **Security updates** handled automatically by provider

#### Business Benefits
- **Faster time to market** for production features
- **Enterprise-ready** authentication from day one
- **Reduced security risk** and compliance burden
- **Focus on core business logic** instead of auth infrastructure
- **Professional user experience** with modern auth flows

#### Cost Analysis

**Current Development System**:
- Development Time: 6-12 months for production auth
- Maintenance: 2-4 hours/week ongoing
- Security Risk: High (custom implementation)
- Total Annual Cost: $50,000-100,000

**Third-Party Service (Auth0)**:
- Setup Time: 2-4 weeks
- Monthly Cost: $23-240 (based on users)
- Maintenance: 1-2 hours/month
- Security Risk: Low (expert-managed)
- Total Annual Cost: $3,000-5,000

**ROI Calculation**:
- **Cost Savings**: $45,000-95,000 annually
- **Time Savings**: 6-11 months development time
- **Risk Reduction**: 90% reduction in security risk
- **Payback Period**: 2-4 weeks

### Implementation Timeline

```mermaid
gantt
    title Production Auth Migration Timeline
    dateFormat  YYYY-MM-DD
    section Development System
    Phase 1-6 Complete    :done, 2024-01-01, 2024-01-21
    section Third-Party Integration
    Auth0 Setup          :2024-01-22, 7d
    Backend JWT Integration :2024-01-29, 7d
    Frontend Migration   :2024-02-05, 7d
    Data Migration       :2024-02-12, 7d
    Testing & Deployment :2024-02-19, 7d
    section Production
    Go Live             :milestone, 2024-02-26, 0d
```

### Migration Checklist

#### Pre-Migration
- [ ] Set up Auth0/Clerk account and configuration
- [ ] Create production user database schema
- [ ] Set up JWT validation infrastructure
- [ ] Plan data migration strategy
- [ ] Test Auth0 integration in development

#### Migration Execution
- [ ] Implement JWT validation middleware
- [ ] Update user service for Auth0 integration
- [ ] Migrate frontend to OAuth flow
- [ ] Run data migration scripts
- [ ] Test end-to-end authentication flow
- [ ] Deploy to staging environment

#### Post-Migration
- [ ] Monitor authentication metrics
- [ ] Validate user data integrity
- [ ] Update documentation
- [ ] Train support team
- [ ] Deploy to production
- [ ] Clean up development auth code

## Migration Timeline

```
Week 1-3: Phase 1-6 (Development System - COMPLETED)
Week 4-5: Phase 7-8 (Third-Party Auth Setup + Backend Integration)
Week 6-7: Phase 9 (Frontend Migration)
Week 8: Phase 10 (Data Migration + Testing)
```

This implementation plan ensures a robust, maintainable authentication system that serves as a solid foundation for the FaultMaven application, with a clear path to production-ready third-party authentication integration.