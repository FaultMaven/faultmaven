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

## Migration Timeline

```
Week 1: Phase 1 + Phase 2 (Foundation + API)
Week 2: Phase 3 + Phase 4 (Dependencies + Migration)
Week 3: Phase 5 + Phase 6 (Cleanup + Testing)
```

This implementation plan ensures a robust, maintainable authentication system that serves as a solid foundation for the FaultMaven application.