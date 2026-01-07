# Session Module

Authentication session management module for FaultMaven.

## Overview

The Session module provides authentication session management capabilities for the FaultMaven platform, including:

- Session lifecycle management (create, validate, expire, delete)
- Multi-device support via client_id
- Session resumption for same client_id
- Session stores (in-memory, Redis)

## Structure

```
modules/session/
├── api/
│   └── routes.py                    # FastAPI session endpoints
├── domain/
│   ├── models/
│   │   └── session.py              # Session domain model
│   └── services/
│       └── session_service.py      # SessionService
└── infrastructure/
    └── persistence/
        ├── repository.py            # Database session repository
        └── stores/
            ├── inmemory.py         # In-memory session store
            ├── redis.py            # Redis session store
            └── redis_manager.py    # Redis manager

```

## Components

### Domain Models

- **Session** ([session.py](domain/models/session.py)) - Domain model for user sessions

### Services

- **SessionService** ([session_service.py](domain/services/session_service.py)) - Authentication session management

### Infrastructure

- **SessionRepository** - Database persistence
- **InMemorySessionStore** - In-memory session storage
- **RedisSessionStore** - Redis-backed session storage
- **RedisSessionManager** - Redis connection manager

## Usage

```python
from faultmaven.modules.session.domain.models.session import Session
from faultmaven.modules.session.domain.services.session_service import SessionService

# Create session service
session_service = SessionService(session_store=inmemory_store)

# Create new session
session, resumed = await session_service.create_session(
    user_id="user_123",
    client_id="browser_1",
    metadata={"ip": "192.168.1.1"}
)

# Validate session
is_valid = await session_service.validate_session(session_id)

# Delete session (logout)
await session_service.delete_session(session_id)
```

## Key Features

1. **Multi-Device Support**: Multiple concurrent sessions per user with client_id tracking
2. **Session Resumption**: Automatically resume existing sessions for same (user, client) pair
3. **Configurable TTL**: Session expiration and inactive timeout settings
4. **Analytics**: Session health metrics and analytics
5. **Storage Flexibility**: Pluggable session stores (in-memory, Redis)

## API Endpoints

- `POST /api/v1/sessions` - Create or resume session
- `GET /api/v1/sessions/{session_id}` - Get session by ID
- `DELETE /api/v1/sessions/{session_id}` - Delete session (logout)
- `GET /api/v1/sessions` - List sessions
- `POST /api/v1/sessions/{session_id}/archive` - Archive session

## Dependencies

### What Session Uses
- `faultmaven.services.base` - Base service class
- `faultmaven.exceptions` - Exception classes
- `faultmaven.infrastructure.observability.tracing` - Distributed tracing

### What Uses Session
- Auth module - For authentication session management
- API routes - For session-based authentication

## Note on Investigation Sessions

**Important**: This module is for **authentication sessions** only. Investigation sessions (related to case investigations) are managed separately in:
- `models/investigation_session.py`
- `services/investigation_session_service.py`
- `api/routes/sessions.py` (investigation session routes)

These will be moved to the Case module in a future extraction.
