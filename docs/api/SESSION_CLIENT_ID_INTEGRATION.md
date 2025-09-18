# Frontend Integration Guide: Client-Based Session Management

## Overview

This document outlines the API contract changes for client-based session management, enabling the frontend to implement session resumption across browser restarts and multiple tabs/windows.

## Key Changes

### 1. Session Creation API Updates

**Endpoint**: `POST /api/v1/sessions`

#### Request Model Changes

```typescript
interface SessionCreateRequest {
    timeout_minutes?: number; // 1 min to 24 hours (default: 30)
    session_type?: string; // default: "troubleshooting"
    metadata?: Record<string, any>;
    client_id?: string; // NEW - Client/device identifier for session resumption
}
```

#### Response Model Changes

```typescript
interface SessionCreateResponse {
    session_id: string;
    user_id?: string;
    client_id?: string; // NEW - Echoed back from request
    status: string; // "active"
    created_at: string; // UTC ISO 8601 format
    session_type: string;
    session_resumed?: boolean; // NEW - true if existing session was resumed
    message: string; // "Session created successfully" or "Session resumed successfully"
}
```

### 2. Frontend Requirements

#### A. Client ID Generation and Persistence

The frontend must:

1. **Generate Unique Client ID**: Create a persistent, unique identifier per browser instance
2. **Store Client ID**: Persist client_id across browser sessions using localStorage
3. **Send Client ID**: Include client_id in all session creation requests

**Recommended Implementation**:

```typescript
class ClientSessionManager {
    private static CLIENT_ID_KEY = 'faultmaven_client_id';
    
    static getOrCreateClientId(): string {
        let clientId = localStorage.getItem(this.CLIENT_ID_KEY);
        
        if (!clientId) {
            // Generate UUID v4 or similar
            clientId = crypto.randomUUID();
            localStorage.setItem(this.CLIENT_ID_KEY, clientId);
        }
        
        return clientId;
    }
    
    static async createSession(userContext?: any): Promise<SessionCreateResponse> {
        const clientId = this.getOrCreateClientId();
        
        const response = await fetch('/api/v1/sessions', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                client_id: clientId,
                session_type: 'troubleshooting',
                timeout_minutes: 60
            })
        });
        
        return await response.json();
    }
}
```

#### B. Session Flow Updates

**New Session Creation Flow**:

1. Frontend generates/retrieves client_id from localStorage
2. Sends session creation request with client_id
3. Backend checks for existing session with (user_id, client_id)
4. If found and active: Resume existing session (session_resumed: true)
5. If not found: Create new session (session_resumed: false)
6. Frontend handles both scenarios appropriately

**Example Usage**:

```typescript
// During app initialization or user login
const sessionResponse = await ClientSessionManager.createSession();

if (sessionResponse.session_resumed) {
    // Existing session resumed - load previous state
    console.log(`Resumed session: ${sessionResponse.session_id}`);
    await loadPreviousSessionState(sessionResponse.session_id);
} else {
    // New session created - initialize fresh state
    console.log(`New session: ${sessionResponse.session_id}`);
    initializeFreshSession(sessionResponse.session_id);
}
```

#### C. Multi-Tab/Window Considerations

**Same Client ID Behavior**:
- All tabs/windows in same browser share the same client_id
- Opening new tab: Resumes existing session (if active)
- All tabs share the same session_id for collaborative experience

**Different Client ID Behavior**:
- Different browsers/devices have different client_ids
- Each gets independent sessions
- Supports multi-device workflows

### 3. Authentication Integration

#### Update Login Flow

```typescript
class AuthService {
    static async login(username: string): Promise<void> {
        // 1. Authenticate user
        const authResponse = await fetch('/api/v1/auth/dev/login', {
            method: 'POST',
            body: JSON.stringify({ username })
        });
        
        const { user } = await authResponse.json();
        
        // 2. Create/resume session with client_id
        const sessionResponse = await ClientSessionManager.createSession();
        
        // 3. Store both user and session context
        this.setUserContext(user);
        this.setSessionContext(sessionResponse);
        
        // 4. Navigate to appropriate view based on session state
        if (sessionResponse.session_resumed) {
            // Resume previous workflow
            router.navigate('/dashboard?resumed=true');
        } else {
            // Start fresh workflow
            router.navigate('/dashboard?fresh=true');
        }
    }
}
```

### 4. Session Management UI Updates

#### A. Session Status Indicator

Add UI elements to indicate session state:

```typescript
interface SessionStatusProps {
    sessionId: string;
    sessionResumed: boolean;
    createdAt: string;
}

const SessionStatusIndicator: React.FC<SessionStatusProps> = ({ 
    sessionId, 
    sessionResumed, 
    createdAt 
}) => {
    return (
        <div className="session-status">
            {sessionResumed ? (
                <span className="status-resumed">
                    ↻ Session Resumed ({formatDateTime(createdAt)})
                </span>
            ) : (
                <span className="status-new">
                    ✓ New Session Started
                </span>
            )}
            <small>Session: {sessionId.slice(0, 8)}...</small>
        </div>
    );
};
```

#### B. Session History/Management

```typescript
interface SessionManagerProps {
    currentSessionId: string;
    onNewSession: () => void;
}

const SessionManager: React.FC<SessionManagerProps> = ({ 
    currentSessionId, 
    onNewSession 
}) => {
    const startNewSession = async () => {
        // Clear client_id to force new session
        localStorage.removeItem('faultmaven_client_id');
        
        const newSession = await ClientSessionManager.createSession();
        onNewSession();
        
        // Redirect to fresh session
        router.navigate(`/session/${newSession.session_id}`);
    };
    
    return (
        <div className="session-controls">
            <button onClick={startNewSession}>
                Start New Session
            </button>
        </div>
    );
};
```

### 5. Error Handling

#### Handle Session Resumption Failures

```typescript
const handleSessionCreation = async () => {
    try {
        const sessionResponse = await ClientSessionManager.createSession();
        return sessionResponse;
    } catch (error) {
        if (error.status === 404 || error.status === 410) {
            // Session expired or invalid - clear client_id and retry
            localStorage.removeItem('faultmaven_client_id');
            return await ClientSessionManager.createSession();
        }
        throw error; // Re-throw other errors
    }
};
```

### 6. Testing Requirements

#### Unit Tests

```typescript
describe('ClientSessionManager', () => {
    beforeEach(() => {
        localStorage.clear();
    });
    
    test('generates and persists client ID', () => {
        const clientId1 = ClientSessionManager.getOrCreateClientId();
        const clientId2 = ClientSessionManager.getOrCreateClientId();
        
        expect(clientId1).toBe(clientId2);
        expect(localStorage.getItem('faultmaven_client_id')).toBe(clientId1);
    });
    
    test('handles session resumption', async () => {
        // Mock API response for resumed session
        fetch.mockResolvedValueOnce({
            ok: true,
            json: () => Promise.resolve({
                session_id: 'existing-session-123',
                session_resumed: true
            })
        });
        
        const response = await ClientSessionManager.createSession();
        
        expect(response.session_resumed).toBe(true);
        expect(response.session_id).toBe('existing-session-123');
    });
});
```

#### Integration Tests

1. **Cross-Tab Session Sharing**: Verify same session_id across multiple tabs
2. **Session Resumption**: Test browser restart scenario
3. **Multi-User Support**: Test different users on same device
4. **Session Expiration**: Handle expired session cleanup

### 7. Backwards Compatibility

#### Graceful Fallback

```typescript
// Support both old and new session creation patterns
const createSession = async (options?: SessionCreateOptions) => {
    const payload: any = {
        session_type: options?.sessionType || 'troubleshooting',
        timeout_minutes: options?.timeoutMinutes || 60
    };
    
    // Only add client_id if feature is enabled
    if (FEATURES.CLIENT_SESSION_MANAGEMENT) {
        payload.client_id = ClientSessionManager.getOrCreateClientId();
    }
    
    const response = await fetch('/api/v1/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    });
    
    return await response.json();
};
```

### 8. Configuration

#### Feature Flags

```typescript
interface FeatureFlags {
    CLIENT_SESSION_MANAGEMENT: boolean;
    MULTI_TAB_SESSION_SHARING: boolean;
    SESSION_RESUMPTION_UI: boolean;
}

// Enable progressive rollout
const FEATURES: FeatureFlags = {
    CLIENT_SESSION_MANAGEMENT: true,
    MULTI_TAB_SESSION_SHARING: true,
    SESSION_RESUMPTION_UI: true
};
```

### 9. Performance Considerations

#### Optimized Client ID Storage

```typescript
// Use crypto.randomUUID() for performance (vs UUID libraries)
// Store in localStorage for persistence across sessions
// Consider sessionStorage for tab-specific behavior if needed

class ClientIdManager {
    private static instance: ClientIdManager;
    private clientId: string | null = null;
    
    static getInstance(): ClientIdManager {
        if (!this.instance) {
            this.instance = new ClientIdManager();
        }
        return this.instance;
    }
    
    getClientId(): string {
        if (!this.clientId) {
            this.clientId = localStorage.getItem('faultmaven_client_id');
            if (!this.clientId) {
                this.clientId = crypto.randomUUID();
                localStorage.setItem('faultmaven_client_id', this.clientId);
            }
        }
        return this.clientId;
    }
}
```

## Summary

This client-based session management system provides:

1. **Seamless Session Resumption**: Users can resume troubleshooting sessions across browser restarts
2. **Multi-Device Support**: Each device maintains independent sessions with same user
3. **Collaborative Experience**: Multiple tabs share the same session for better UX
4. **Backward Compatibility**: Existing clients continue to work without changes
5. **Progressive Enhancement**: Feature can be rolled out gradually

The frontend team should implement client_id generation, persistence, and the updated session creation flow to enable these capabilities.