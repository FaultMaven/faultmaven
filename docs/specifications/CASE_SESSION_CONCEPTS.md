# Case and Session Concepts in FaultMaven

## Overview

This document defines the fundamental concepts of **Cases** and **Sessions** in FaultMaven, clarifying their **correct relationship, purpose, and implementation**. This document has been updated to reflect the proper architectural design where cases are **top-level resources** and sessions are used **only for authentication**.

## Key Concepts

### Session (Temporary Connection)
A **Session** represents a user's **temporary authentication state** with FaultMaven. It is a short-lived identifier used for authentication and authorization purposes only.

**Characteristics:**
- **Lifespan**: 24 hours by default (configurable)
- **Scope**: User-specific authentication token
- **Purpose**: **Authentication and authorization only** - NOT a container for cases
- **Persistence**: Redis-backed with TTL for security
- **Identifier**: `session_id` (UUID)

**Contains:**
- User authentication state
- Session creation/expiry timestamps
- User identity information

**Does NOT Contain:**
- ❌ Cases (cases are independent resources)
- ❌ Case history (cases manage their own history)  
- ❌ Current active case ID (frontend UI state)

### Case (Permanent Investigation Record)
A **Case** represents a **permanent, independent troubleshooting investigation**. Each case is a top-level resource that maintains its own complete lifecycle and data.

**Characteristics:**
- **Lifespan**: Permanent until explicitly deleted
- **Scope**: Single troubleshooting investigation (independent of sessions)
- **Purpose**: Complete investigation record with full conversation history
- **Persistence**: **Independent storage** (not tied to session lifecycle)
- **Identifier**: `case_id` (UUID)

**Contains:**
- Complete conversation history (query/response pairs)
- Case metadata (title, status, priority, timestamps)
- Investigation findings and recommendations
- Uploaded data specific to this case
- User who created the case (for authorization)
- Complete audit trail

## Relationship Architecture

**CORRECT: Independent Top-Level Resources**
```
User Authentication Session (24h TTL) ──── Authentication ────┐
│                                                             │
├── session_id: str                                           │
├── user_id: str                                              │
├── created_at: datetime                                      │
├── last_activity: datetime                                   │ 
└── expires_at: datetime                                      │
                                                              │
                                                              │ 
Case (Permanent Resource) ◄──── Authorized Access ───────────┘
│
├── case_id: str (PRIMARY KEY)
├── user_id: str (authorization reference)
├── title: str  
├── status: enum ("active", "investigating", "solved", "stalled", "archived")
├── priority: enum ("low", "medium", "high", "critical")
├── created_at: datetime
├── last_updated: datetime
├── conversation_history: List[QueryResponse]
├── uploaded_data: List[UploadedData]
├── investigation_findings: List[Finding]
└── recommended_actions: List[Action]
```

**Key Architectural Principle**: Cases are **NOT** nested under sessions. Sessions provide **authentication context only**.

## Correct Usage Flows

### User Authentication
1. User opens FaultMaven browser extension
2. System creates new `session_id` (24h TTL for authentication only)
3. Session stores: user_id, timestamps, expiry
4. **No case data stored in session**

### Creating a New Case
1. User clicks "New Case" or submits first query
2. Frontend sends: `POST /api/v1/cases` with `X-Session-ID: {session_id}` header
3. Backend validates session authentication
4. System generates new `case_id` as **top-level resource**
5. Case stores: title, user_id, initial query, timestamps
6. Returns complete ViewState for frontend rendering

### Querying an Existing Case
1. User submits follow-up query in existing case
2. Frontend sends: `POST /api/v1/cases/{case_id}/query` with `X-Session-ID: {session_id}` header
3. Backend validates session authentication AND case authorization
4. System retrieves conversation history from case record
5. Injects full conversation context into LLM prompt
6. Records new query/response in case conversation history
7. Returns updated ViewState with complete case context

### Case List and Navigation
1. User wants to see all their cases
2. Frontend sends: `GET /api/v1/cases` with `X-Session-ID: {session_id}` header  
3. Backend validates session and returns cases where case.user_id matches session.user_id
4. User can switch between cases without losing any conversation history

### Session Expiry and Case Persistence
1. User session expires after 24 hours (authentication timeout)
2. **Cases remain permanently accessible** - they are independent resources
3. User re-authenticates → new session_id created
4. All existing cases remain available through case_id references

## Implementation Details

### Data Models

```python
class SessionContext(BaseModel):
    """Authentication session - DOES NOT contain case data"""
    session_id: str
    user_id: str
    created_at: datetime
    last_activity: datetime
    expires_at: datetime
    # NO case_history, NO current_case_id - sessions are for auth only

class Case(BaseModel):
    """Independent case resource with complete lifecycle"""
    id: str                       # Primary key - NOT nested under sessions
    title: str                    # Generated or user-provided title
    user_id: str                  # Authorization reference (NOT foreign key to session)
    status: Literal["active", "investigating", "solved", "stalled", "archived"]
    priority: Literal["low", "medium", "high", "critical"]
    created_at: str               # UTC ISO 8601 format
    last_updated: str             # UTC ISO 8601 format
    conversation_count: int       # Number of query/response exchanges
    data_count: int              # Number of uploaded files
    summary: str                 # Auto-generated case summary

class QueryRequest(BaseModel):
    """Query within a specific case context"""
    session_id: str              # For authentication (NOT case ownership)
    query: str                   # User's question
    context: Optional[Dict[str, Any]] = None
    priority: Literal["low", "medium", "high", "critical"] = "medium"
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat() + 'Z')
    # NOTE: case_id comes from URL path, not request body

class ViewState(BaseModel):
    """Complete frontend rendering state for a case"""
    session_id: str              # Current authentication session
    case_id: str                 # Current case being viewed
    user_id: str                 # Authorized user
    case_title: str              # Display title for case
    case_status: Literal["active", "investigating", "solved", "stalled", "archived"]
    running_summary: str         # AI-generated case summary
    uploaded_data: List[UploadedData]  # Files uploaded to this case
    conversation_count: int      # Number of exchanges in this case
    last_updated: str            # When case was last modified
    can_upload_data: bool        # Whether user can upload more data
    needs_more_info: bool        # Whether case needs more information
    available_actions: List[AvailableAction]  # Next steps user can take
    progress_indicators: List[ProgressIndicator]  # Investigation progress
```

### API Endpoints

**CORRECT: Cases as Top-Level Resources**

```python
# Authentication Session Management
POST   /api/v1/sessions/                    # Create new authentication session
GET    /api/v1/sessions/{session_id}        # Get session info (auth status only)
DELETE /api/v1/sessions/{session_id}        # End authentication session

# Case Management (Top-Level Resources)
POST   /api/v1/cases                       # Create new case (auth via X-Session-ID header)
GET    /api/v1/cases                       # List user's cases (auth via X-Session-ID header)
GET    /api/v1/cases/{case_id}             # Get specific case (auth via X-Session-ID header)
PUT    /api/v1/cases/{case_id}             # Update case metadata
DELETE /api/v1/cases/{case_id}             # Archive/delete case

# Query Processing (Case-Specific)
POST   /api/v1/cases/{case_id}/query       # Process query within specific case
GET    /api/v1/cases/{case_id}/history     # Get case conversation history

# Data Management (Case-Specific)
POST   /api/v1/cases/{case_id}/data        # Upload data to specific case
GET    /api/v1/cases/{case_id}/data        # List case data
DELETE /api/v1/cases/{case_id}/data/{data_id}  # Remove data from case

# Headers for ALL Case Operations:
# X-Session-ID: {session_id}  # Required for authentication
```

**Key REST Principles:**
- `case_id` always in URL path (resource identifier)
- `session_id` always in header (authentication context)
- Cases are **never** nested under `/sessions/{session_id}/...`

### Service Layer

```python
class SessionService:
    """Authentication session management only"""
    async def create_session(self, user_id: str) -> SessionContext:
        """Create new authentication session with TTL"""
        
    async def validate_session(self, session_id: str) -> bool:
        """Validate session is active and not expired"""
        
    async def get_user_from_session(self, session_id: str) -> str:
        """Get user_id from valid session for authorization"""

class CaseService:
    """Case management as top-level resources"""
    async def create_case(self, user_id: str, title: str, initial_query: Optional[str]) -> Case:
        """Create new case as independent resource"""
        
    async def get_user_cases(self, user_id: str) -> List[Case]:
        """Get all cases belonging to user"""
        
    async def get_case(self, case_id: str, user_id: str) -> Case:
        """Get specific case with authorization check"""
        
    async def get_case_conversation_history(self, case_id: str, user_id: str) -> List[Dict]:
        """Get conversation history for case with auth check"""

class AgentService:
    async def process_case_query(self, case_id: str, user_id: str, request: QueryRequest) -> AgentResponse:
        """Process query within specific case context"""
        # 1. Validate case authorization (case.user_id == user_id)
        # 2. Retrieve conversation history from case record
        # 3. Inject history into LLM prompt for context
        # 4. Process query with full conversation context
        # 5. Record query/response in case conversation history
        # 6. Return complete ViewState for frontend rendering
```

## Benefits of This Architecture

### 1. True REST Compliance
- **Cases as Resources**: Top-level resources with proper CRUD operations
- **Clean URLs**: `/api/v1/cases/{case_id}` instead of nested routes
- **Stateless Authentication**: X-Session-ID header for auth context only

### 2. Clear Separation of Concerns
- **Session**: "Who is authenticated and for how long?" (24h TTL)
- **Case**: "What specific problem needs solving?" (permanent record)
- **Authorization**: user_id links cases to sessions for access control

### 3. Permanent Case Management
- Cases survive session expiry (authentication timeout)
- Users can return to cases across different browser sessions
- Complete case lifecycle independent of authentication state

### 4. Conversation Continuity
- Full conversation history stored in case record
- Follow-up questions automatically include context
- "It", "this", "that" pronouns understood through context injection
- Progressive information building works naturally

### 5. Multiple Problem Support
- Users can manage multiple independent cases
- Each case maintains its own conversation context
- Easy switching between cases without losing history

### 6. Frontend Implementation Clarity
- Single API calls return complete ViewState (no waterfalls)
- Clear case navigation and "New Case" actions
- Complete case metadata available for UI rendering

### 7. Scalable Authorization Model
- Case-level access control through user_id validation
- Session-based authentication decoupled from case ownership
- Clear audit trail for case access and modifications

## Implementation Migration

### Critical Architectural Changes
- **ELIMINATED**: Cases as session sub-resources (was: `/sessions/{session_id}/cases`)
- **IMPLEMENTED**: Cases as top-level resources (now: `/cases/{case_id}`)
- **IMPLEMENTED**: X-Session-ID header authentication pattern
- **IMPLEMENTED**: Complete ViewState response model
- **IMPLEMENTED**: REST-compliant URL structures

### API Contract Changes
- All case operations use `X-Session-ID` header for authentication
- `case_id` always in URL path, never in request body
- Single API calls return complete ViewState (no multiple requests needed)
- Session management decoupled from case data storage

### Data Model Evolution
- `SessionContext` no longer contains case data
- `Case` model includes complete conversation and metadata
- `ViewState` provides single source of truth for frontend rendering
- `AgentResponse` includes unified response types and view state

### Service Layer Updates
- `CaseService` created for case management as top-level resources
- `SessionService` focused only on authentication sessions
- `AgentService` processes queries within case context with conversation history
- Authorization validation at case level through user_id matching

### Frontend Integration Requirements
- Use `X-Session-ID` header for all API requests
- Handle complete ViewState responses for UI rendering
- Implement case navigation independent of session management
- Support permanent case persistence beyond session expiry

## Real-World Examples

### Example 1: Database Troubleshooting Case
```
Authentication Session: session_abc123 (User: john@company.com, TTL: 24h)
│
└── Case (Independent Resource): case_def456
    ├── Title: "Database Performance Issues"
    ├── Status: "investigating" 
    ├── User: john@company.com (authorization reference)
    ├── Created: 2024-01-15T10:30:00Z
    ├── Conversation History:
    │   ├── 10:30 Q: "Database queries are slow"
    │   ├── 10:30 A: "Let's analyze the performance metrics..."
    │   ├── 10:32 Q: "Started after recent deployment" 
    │   ├── 10:32 A: "Recent deployment changes can affect performance..."
    │   ├── 10:35 Q: "Should I check query execution plans?"
    │   ├── 10:35 A: "Yes, query execution plans will show..."
    │   ├── 10:37 Q: "How do I optimize the slow queries?"
    │   └── 10:37 A: "Here are the optimization steps..."
    ├── Uploaded Data: [slow_queries.log, performance_metrics.csv]
    └── Investigation Findings: [Index missing on user_id column, Query N+1 pattern detected]

API Flow:
- POST /api/v1/cases (with X-Session-ID: session_abc123) → Create case_def456
- POST /api/v1/cases/case_def456/query (with X-Session-ID) → Process each query
- GET /api/v1/cases/case_def456 (with X-Session-ID) → Get complete case view
```

### Example 2: Multi-Case User Workflow
```
Authentication Session: session_xyz789 (User: mary@company.com, TTL: 24h)
│
├── Case 1: case_ghi101 (Authentication Issues) [SOLVED]
│   ├── Title: "User Login Problems"
│   ├── User: mary@company.com
│   ├── Created: 2024-01-15T09:00:00Z
│   ├── Conversation: 3 exchanges (login errors → LDAP config fix)
│   └── Status: "solved"
│
├── Case 2: case_jkl202 (Performance Issues) [SOLVED]
│   ├── Title: "Website Loading Slowly"
│   ├── User: mary@company.com
│   ├── Created: 2024-01-15T11:00:00Z
│   ├── Conversation: 4 exchanges (response times → API bottleneck)
│   └── Status: "solved"
│
└── Case 3: case_mno303 (Deployment Problems) [ACTIVE]
    ├── Title: "CI/CD Pipeline Failures"
    ├── User: mary@company.com
    ├── Created: 2024-01-15T14:30:00Z
    ├── Conversation: 2 exchanges (Docker build errors → ongoing)
    └── Status: "investigating"

API Flow:
- GET /api/v1/cases (with X-Session-ID: session_xyz789) → List all user's cases
- POST /api/v1/cases/case_mno303/query (with X-Session-ID) → Continue active case
- Case persistence: All cases remain accessible even after session_xyz789 expires
```

### Example 3: Session Expiry and Case Persistence
```
Day 1: session_abc123 expires after 24 hours
Day 2: User returns → new session_def456 created
Day 2: GET /api/v1/cases (with X-Session-ID: session_def456)
       → Returns same cases (case_def456, case_ghi101, case_jkl202, case_mno303)
       → Cases are permanent, sessions are temporary authentication
```

## Conclusion

This corrected Case/Session architecture provides:
- **True REST compliance** with cases as top-level resources
- **Clear separation of concerns** between authentication (sessions) and problem-solving (cases)
- **Permanent case persistence** independent of authentication session lifecycle
- **Complete conversation continuity** through case-level context storage
- **Single-request frontend pattern** with complete ViewState responses
- **Scalable authorization model** with user-based case access control
- **Multiple problem support** with independent case management
- **Clean API contracts** using standard HTTP methods and resource patterns

**Critical Fix**: This design **eliminates the fundamental flaw** where cases were incorrectly treated as sub-resources of sessions. Cases are now properly implemented as independent, permanent resources with session-based authentication for access control.

The implementation provides a solid foundation for advanced features like case sharing, case templates, multi-user collaboration, and sophisticated case analytics while maintaining clean architectural boundaries and REST compliance.