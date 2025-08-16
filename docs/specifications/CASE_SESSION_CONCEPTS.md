# Case and Session Concepts in FaultMaven

## Overview

This document defines the fundamental concepts of **Cases** and **Sessions** in FaultMaven, clarifying their relationship, purpose, and implementation. This replaces the previous confusing "investigation" terminology with clearer, more precise concepts.

## Key Concepts

### Session
A **Session** represents a user's overall interaction period with FaultMaven. It is a persistent container that manages multiple troubleshooting conversations.

**Characteristics:**
- **Lifespan**: 24 hours by default (configurable)
- **Scope**: User-specific, can contain multiple cases
- **Purpose**: Maintains user state, uploaded data, and overall context
- **Persistence**: Redis-backed with TTL
- **Identifier**: `session_id` (UUID)

**Contains:**
- User authentication state
- Uploaded data files
- Case history (chronological list of all cases)
- Current active case ID
- Agent state and preferences

### Case
A **Case** represents a single troubleshooting conversation thread within a session. Each case focuses on one specific problem or question.

**Characteristics:**
- **Lifespan**: Until user starts a new conversation
- **Scope**: Single troubleshooting topic/conversation thread
- **Purpose**: Maintains conversation continuity and context
- **Persistence**: Stored within session case_history
- **Identifier**: `case_id` (UUID)

**Contains:**
- Conversation history (query/response pairs)
- Context specific to this troubleshooting thread
- Case-specific findings and recommendations
- Timestamps and confidence scores

## Relationship Hierarchy

```
User Session (24h TTL)
├── Session Metadata
│   ├── session_id: str
│   ├── user_id: str
│   ├── created_at: datetime
│   ├── last_activity: datetime
│   └── data_uploads: List[str]
├── Current State
│   ├── current_case_id: str
│   └── agent_state: Dict
└── Case History
    ├── Case 1 (Database slowness)
    │   ├── case_id: str
    │   ├── Query: "My database is slow"
    │   ├── Query: "It started after deployment"
    │   └── Query: "What should I check?"
    ├── Case 2 (Login issues)
    │   ├── case_id: str
    │   ├── Query: "Users can't log in"
    │   └── Query: "Error says invalid credentials"
    └── Case 3 (Current conversation)
        ├── case_id: str (matches current_case_id)
        └── Active conversation...
```

## Use Cases

### Starting a New Session
1. User opens FaultMaven browser extension
2. System creates new `session_id`
3. Session initialized with empty case_history
4. No current_case_id (will be created on first query)

### First Query in Session
1. User asks: "My server is down"
2. System generates new `case_id`
3. Sets `current_case_id = case_id`
4. Records query in case_history with case_id
5. LLM processes query (no conversation history)

### Follow-up Queries (Same Case)
1. User asks: "When will it be back up?"
2. System uses existing `current_case_id`
3. Retrieves conversation history for this case_id
4. Injects history into LLM prompt for context
5. Records new query in case_history with same case_id

### Starting New Conversation (New Case)
1. User clicks "New Chat" or calls `/new-case` endpoint
2. System generates new `case_id`
3. Updates `current_case_id = new_case_id`
4. Records "new_case_started" action in case_history
5. Next query starts fresh conversation with new case_id

### Session Continuation
1. User returns within 24-hour window
2. System retrieves existing session
3. Maintains current_case_id (conversation continues)
4. Full case_history available for context

## Implementation Details

### Data Models

```python
class SessionContext(BaseModel):
    """Session context for maintaining state across requests"""
    session_id: str
    user_id: Optional[str]
    created_at: datetime
    last_activity: datetime
    data_uploads: List[str]
    case_history: List[Dict[str, Any]]  # All cases in this session
    current_case_id: Optional[str]      # Active conversation thread
    agent_state: Optional[AgentState]

class CaseHistoryItem(BaseModel):
    """Individual item in case history"""
    action: str                    # "query_processed", "new_case_started", etc.
    case_id: str                  # Which case this belongs to
    query: Optional[str]          # User query (if applicable)
    timestamp: str                # ISO timestamp
    confidence_score: float       # Processing confidence
    context: Dict[str, Any]       # Additional context
```

### API Endpoints

```python
# Session Management
POST   /api/v1/sessions/                    # Create new session
GET    /api/v1/sessions/{session_id}        # Get session info
DELETE /api/v1/sessions/{session_id}        # End session

# Case Management  
POST   /api/v1/sessions/{session_id}/new-case  # Start new conversation
GET    /api/v1/sessions/{session_id}/cases     # List all cases in session
GET    /api/v1/sessions/{session_id}/cases/{case_id}/history  # Case conversation

# Query Processing (uses current case)
POST   /api/v1/agent/query                 # Process query in current case
```

### Service Layer

```python
class SessionService:
    async def get_or_create_current_case_id(self, session_id: str) -> str:
        """Get current case_id or create new one for session"""
        
    async def start_new_case(self, session_id: str) -> str:
        """Explicitly start new conversation thread"""
        
    async def get_case_conversation_history(self, session_id: str, case_id: str) -> List[Dict]:
        """Get conversation history for specific case"""
        
    async def format_conversation_context(self, session_id: str, case_id: str) -> str:
        """Format case history for LLM context injection"""

class AgentService:
    async def process_query(self, request: QueryRequest) -> AgentResponse:
        """Process query within current case context"""
        # 1. Get/create case_id for session
        # 2. Retrieve conversation history for case
        # 3. Inject history into LLM prompt
        # 4. Process with full context
        # 5. Record result in case_history
```

## Benefits of This Architecture

### 1. Clear Conceptual Separation
- **Session**: "Who is using the system and for how long?"
- **Case**: "What specific problem are we solving?"

### 2. Conversation Continuity
- Follow-up questions automatically include context
- "It", "this", "that" pronouns are understood
- Progressive information building works naturally

### 3. Multiple Problem Support
- Users can work on different issues within one session
- Each case maintains its own context
- Easy to switch between problems

### 4. Frontend Clarity
- Browser extension shows current case context
- Clear "New Chat" action starts new case
- Case history browseable within session

### 5. Privacy and Security
- Session timeout automatically cleans up data
- Case-level isolation for different problems
- Clear data lifecycle management

## Migration Notes

### Terminology Changes
- ~~`investigation_id`~~ → `case_id`
- ~~`investigation_history`~~ → `case_history`
- ~~`investigation_started`~~ → `case_analysis_started`
- ~~`investigation_completed`~~ → `case_analysis_completed`

### Backward Compatibility
- Legacy `investigation_history` field maintained during transition
- `add_investigation_history()` method aliased to `add_case_history()`
- Existing tests continue to work during migration

### Frontend Impact
- Browser extension should display current case context
- "New Chat" button calls `/new-case` endpoint
- Case history visualization in UI
- Session status indicator

## Examples

### Example 1: Database Troubleshooting Session
```
Session: session_abc123 (User: john@company.com)
├── Case 1: case_def456 (Database Performance)
│   ├── 10:30 "Database queries are slow"
│   ├── 10:32 "Started after recent deployment" 
│   ├── 10:35 "Should I check query execution plans?"
│   └── 10:37 "How do I optimize the slow queries?"
└── [Case 1 still active - current_case_id: case_def456]
```

### Example 2: Multi-Problem Session
```
Session: session_xyz789 (User: mary@company.com)
├── Case 1: case_ghi101 (Authentication Issues) [COMPLETED]
│   ├── 09:00 "Users can't log in"
│   ├── 09:05 "Getting invalid credentials error"
│   └── 09:10 "Fixed! It was the LDAP configuration"
├── Case 2: case_jkl202 (Performance Issues) [COMPLETED] 
│   ├── 11:00 "Website is loading slowly"
│   ├── 11:03 "Response times over 5 seconds"
│   └── 11:15 "Identified bottleneck in API calls"
└── Case 3: case_mno303 (Deployment Problems) [ACTIVE]
    ├── 14:30 "CI/CD pipeline is failing"
    ├── 14:35 "Error in Docker build step"
    └── [current_case_id: case_mno303]
```

## Conclusion

This Case/Session architecture provides:
- **Clear conceptual model** for users and developers
- **Proper conversation continuity** through case-level context
- **Multiple problem support** within persistent sessions
- **Clean separation of concerns** between user state and problem state
- **Intuitive frontend interactions** for troubleshooting workflows

The implementation maintains backward compatibility while providing a foundation for advanced features like case management, conversation history, and multi-problem troubleshooting workflows.