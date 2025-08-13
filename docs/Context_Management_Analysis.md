# FaultMaven Context Management Analysis

## 🔍 **Issue Summary**

You're absolutely correct - there is a **significant context continuity bug** in FaultMaven. The user context (`user_id`, `session_id`) is **not being properly maintained** across requests within the same session, leading to broken context continuity.

---

## 🏗️ **Current Context Architecture**

### **Context Storage Mechanism**
- **Primary**: `request_context: ContextVar[Optional[RequestContext]]` (thread-safe, request-scoped)
- **Location**: `faultmaven/infrastructure/logging/coordinator.py:168-171`
- **Scope**: Per HTTP request only (not persistent across requests)

### **Context Lifecycle**

#### **1. Context Creation** 
```python
# In LoggingMiddleware.dispatch() - faultmaven/api/middleware/logging.py:59
context = self.coordinator.start_request(attributes=http_context)
```

#### **2. Context Population**
```python
# Only HTTP metadata is captured:
http_context = {
    'method': request.method,
    'path': request.url.path,
    'client_ip': request.client.host,
    'user_agent': request.headers.get('user-agent'),
    'query_params': str(request.query_params)
}
```

#### **3. Context End**
```python
# In LoggingCoordinator.end_request() - coordinator.py:263
request_context.set(None)  # Context is CLEARED at end of each request
```

---

## 🚨 **The Core Problem**

### **Missing Context Population**
The `RequestContext` is **never populated with `session_id` or `user_id`** during request processing:

1. **LoggingMiddleware** only captures HTTP metadata, not business context
2. **No mechanism** to extract `session_id`/`user_id` from requests and populate context
3. **Context is destroyed** at the end of each HTTP request
4. **New requests start with empty context** even within the same session

### **Evidence of the Bug**

#### **Request Context Definition** (coordinator.py:17-45):
```python
@dataclass
class RequestContext:
    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: Optional[str] = None     # ❌ Never populated
    user_id: Optional[str] = None        # ❌ Never populated  
    investigation_id: Optional[str] = None
    agent_phase: Optional[str] = None
    # ... other fields
```

#### **Context Initialization** (logging.py:59):
```python
# Only HTTP context, no session/user data
context = self.coordinator.start_request(attributes=http_context)
```

#### **Missing Context Bridge**
There's **no code** that:
- Extracts `session_id` from request body/headers/query params
- Looks up `user_id` from session
- Populates the `RequestContext` with this data

---

## 📊 **Session vs Context Relationship**

### **Current State: Disconnected**
```
HTTP Request → LoggingMiddleware → RequestContext (empty session/user)
     ↓
API Endpoint → Dependencies → SessionService.get_session()
     ↓
Business Logic → Uses session data locally
     ↓
Tracing/Logging → Uses empty RequestContext (❌ no session/user)
```

### **Expected State: Connected**
```
HTTP Request → Extract session_id → Look up session → Populate RequestContext
     ↓
All downstream code has access to session/user context
     ↓
Consistent tracing, logging, and business logic
```

---

## 🛠️ **Root Cause Analysis**

### **1. Architecture Gap**
- **Session management** exists (`SessionService`, `SessionManager`)
- **Context management** exists (`RequestContext`, `LoggingCoordinator`)
- **No bridge** between them

### **2. Missing Middleware**
There's no middleware to:
- Extract `session_id` from requests
- Populate `RequestContext` with session data
- Maintain context across the request lifecycle

### **3. Inconsistent Data Flow**
- **API dependencies** handle session resolution per endpoint
- **Context system** operates independently
- **No shared state** between these systems

---

## 💡 **Proposed Solutions**

### **✅ SELECTED SOLUTION: Enhanced LoggingMiddleware (Architecture-Aligned)**

Based on architectural review, the optimal solution is to enhance the existing `LoggingMiddleware` to populate context with session data, following FaultMaven's design principles:

**Why this approach:**
1. **Fits Interface-Based Architecture** - Uses existing `LoggingCoordinator.start_request()` 
2. **Follows Layered Pattern** - Middleware → Service → Infrastructure
3. **Maintains Single Responsibility** - LoggingMiddleware already manages request context
4. **Zero Breaking Changes** - Extends existing functionality without new components

### **Implementation Plan:**

**Phase 1: Enhance LoggingMiddleware**
```python
# In faultmaven/api/middleware/logging.py - extend existing dispatch method
async def dispatch(self, request: Request, call_next: Callable) -> Response:
    # ... existing HTTP context code ...
    
    # NEW: Extract session context
    session_id = await self._extract_session_id(request)
    user_id = None
    investigation_id = await self._extract_investigation_id(request)
    
    if session_id:
        # Look up session to get user_id (with graceful degradation)
        try:
            from faultmaven.api.v1.dependencies import get_session_service
            session_service = await get_session_service()
            session = await session_service.get_session(session_id, validate=False)
            user_id = session.user_id if session else None
        except Exception:
            # Graceful degradation - continue without user context
            pass
    
    # Create context with business data using existing coordinator
    context = self.coordinator.start_request(
        session_id=session_id,
        user_id=user_id, 
        investigation_id=investigation_id,
        attributes=http_context  # existing HTTP metadata
    )
```

**Phase 2: Session Extraction Logic**
```python
async def _extract_session_id(self, request: Request) -> Optional[str]:
    # Priority order: Header → Query → Body
    # 1. Header: X-Session-ID (preferred for API clients)
    if session_id := request.headers.get("x-session-id"):
        return session_id
        
    # 2. Query parameter (for GET requests)
    if session_id := request.query_params.get("session_id"):
        return session_id
        
    # 3. Request body (for POST/PUT requests)
    if request.method in ["POST", "PUT", "PATCH"]:
        try:
            body = await request.body()
            if body:
                import json
                data = json.loads(body)
                if session_id := data.get("session_id"):
                    return session_id
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
            
    return None
```

### **Solution 2: Enhanced LoggingMiddleware**

Extend the existing `LoggingMiddleware` to populate context:

```python
# In faultmaven/api/middleware/logging.py
async def dispatch(self, request: Request, call_next: Callable) -> Response:
    # ... existing HTTP context code ...
    
    # NEW: Extract and populate business context
    session_id = await self._extract_session_id(request)
    user_id = None
    
    if session_id:
        # Look up session to get user_id
        try:
            session = await self._get_session_service().get_session(session_id)
            user_id = session.user_id if session else None
        except:
            pass  # Continue without user context
    
    # Create context with business data
    context = self.coordinator.start_request(
        session_id=session_id,
        user_id=user_id,
        attributes=http_context
    )
```

### **Solution 3: Context-Aware Dependencies**

Modify FastAPI dependencies to automatically populate context:

```python
# In faultmaven/api/v1/dependencies.py
async def get_current_session_with_context(
    session_id: str,
    session_service: SessionService = Depends(get_session_service),
) -> SessionContext:
    session = await session_service.get_session(session_id, validate=True)
    
    # Update request context
    ctx = request_context.get()
    if ctx:
        ctx.session_id = session_id
        ctx.user_id = session.user_id
        request_context.set(ctx)
    
    return session
```

---

## 🚀 **Implementation Recommendations**

### **Phase 1: Quick Fix (Immediate)**
1. **Implement Solution 2** - extend `LoggingMiddleware`
2. **Add session_id extraction** from common request patterns
3. **Test with existing endpoints**

### **Phase 2: Proper Architecture (Follow-up)**
1. **Implement Solution 1** - dedicated `ContextMiddleware`
2. **Standardize session_id passing** (recommend `X-Session-ID` header)
3. **Update client code** to include session headers

### **Phase 3: Validation (Testing)**
1. **Add context continuity tests**
2. **Verify tracing includes session/user data**
3. **Test targeted tracing functionality**

---

## 🧪 **Testing the Fix**

### **Before Fix:**
```python
# In any endpoint, check context
ctx = request_context.get()
print(f"Session ID: {ctx.session_id}")  # None
print(f"User ID: {ctx.user_id}")        # None
```

### **After Fix:**
```python
# Context should contain session data
ctx = request_context.get()  
print(f"Session ID: {ctx.session_id}")  # "abc-123-def"
print(f"User ID: {ctx.user_id}")        # "user_456"
```

---

## 📋 **Files to Modify**

### **Primary Changes:**
1. **`faultmaven/api/middleware/logging.py`** - Add context population
2. **`faultmaven/infrastructure/logging/coordinator.py`** - Ensure context persistence

### **Secondary Changes:**
3. **`faultmaven/api/v1/dependencies.py`** - Update session dependencies
4. **`faultmaven/main.py`** - Register middleware in correct order

### **Testing:**
5. **Add integration tests** for context continuity
6. **Update targeted tracing tests** to verify session context

---

## 🎯 **Expected Impact**

### **Immediate Benefits:**
- ✅ **Context continuity** within sessions
- ✅ **Proper targeted tracing** (session/user-based)
- ✅ **Consistent logging** with session correlation
- ✅ **Better debugging** capabilities

### **Long-term Benefits:**
- 🔄 **Session-aware caching**
- 📊 **User behavior analytics**
- 🚨 **Session-based alerting**
- 🔍 **Enhanced observability**

This analysis confirms your observation - the context management system is fundamentally broken for session continuity, and implementing the proposed solutions will restore proper context management across requests within the same session.
