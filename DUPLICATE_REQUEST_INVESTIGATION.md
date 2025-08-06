# Duplicate Request Investigation & Solution

## Issue Summary
**Problem**: Duplicate log entries with different correlation IDs for the same HTTP request:
```
01:47:15 [681e8edb] INFO Request started: POST /api/v1/sessions/f6d5a493-9efa-4faa-9e1f-2d8b030fd590/heartbeat
01:47:15 [dfcf989b] INFO Request started: POST /api/v1/sessions/f6d5a493-9efa-4faa-9e1f-2d8b030fd590/heartbeat
```

## Investigation Results

### ✅ **Ruled Out**: Server-Side Issues
1. **Middleware Registration**: Only one `RequestLoggingMiddleware` instance registered
2. **FastAPI Application**: Single app instance, no duplicate routes
3. **Uvicorn Reload**: `RELOAD=false` in configuration
4. **Development Server**: Manual testing shows no duplicates with same setup

### 🎯 **Most Likely Cause**: Client-Side Request Duplication

Based on investigation, the duplicate requests are likely originating from:

#### **Browser Extension Behavior**
- **Service Worker + Content Script**: Both components making the same request
- **Background Script + Popup**: Race condition between extension components  
- **Event Handler Duplication**: Event listeners registered multiple times
- **Auto-retry Logic**: Extension retrying failed/slow requests without deduplication

#### **Browser Behavior**
- **CORS Preflight + Actual Request**: Browser making OPTIONS + POST requests
- **DevTools Network Replay**: Developer accidentally replaying requests
- **Multiple Extension Tabs**: Same extension running in multiple tabs

## Solution Implemented

### 1. **Enhanced Request Tracking Middleware**
Added comprehensive duplicate detection in `/home/swhouse/projects/FaultMaven/faultmaven/infrastructure/request_logging.py`:

```python
class RequestTracker:
    """Thread-safe request tracking to detect duplicate processing."""
    
    def start_tracking(self, request_signature: str, correlation_id: str, details: Dict) -> bool:
        """Returns True if new request, False if duplicate."""
        # Tracks active requests to detect simultaneous processing
```

### 2. **Comprehensive Debug Logging**
Enhanced middleware now captures:
- Request signatures (method:path:client_ip)
- Client headers (User-Agent, X-Forwarded-For, etc.)
- Duplicate request detection with WARNING logs
- Middleware instance IDs for server-side debugging
- Request timing and correlation tracking

### 3. **Configuration Changes**
- **Debug Logging Enabled**: `LOG_LEVEL=DEBUG` in `.env`
- **Enhanced Log Fields**: Added request signatures, headers, duplicate flags

### 4. **Diagnostic Tools**
- **Detection Script**: `/home/swhouse/projects/FaultMaven/detect_duplicate_cause.py`
- **Debug Middleware**: `/home/swhouse/projects/FaultMaven/debug_middleware.py`

## How to Use the Solution

### **1. Monitor for Duplicates**
The enhanced middleware will now log warnings when detecting duplicate requests:
```
WARNING: DUPLICATE_REQUEST_DETECTED: Same request being processed twice! 
Original correlation_id: 681e8edb, New correlation_id: dfcf989b, 
Request: POST:/api/v1/sessions/.../heartbeat:127.0.0.1
```

### **2. Debug Client-Side Issues**
1. **Browser DevTools**: Check Network tab for duplicate entries
2. **Extension DevTools**: Monitor background scripts and content scripts
3. **Timing Analysis**: Same timestamp = client duplicate, different = server issue

### **3. Enable Detailed Debugging**
Already configured with `LOG_LEVEL=DEBUG` in `.env` file.

## Expected Behavior

### **Normal Operation**:
```
[12bba917] INFO Request started: POST /api/v1/sessions/abc/heartbeat
[12bba917] INFO Request completed: POST /api/v1/sessions/abc/heartbeat -> 200 in 0.014s
```

### **Duplicate Detection**:
```
[681e8edb] INFO Request started: POST /api/v1/sessions/abc/heartbeat (is_duplicate_request: false)
WARNING: DUPLICATE_REQUEST_DETECTED: Same request being processed twice!
[dfcf989b] INFO Request started: POST /api/v1/sessions/abc/heartbeat (is_duplicate_request: true)
```

## Next Steps for Client-Side Investigation

### **Browser Extension (faultmaven-copilot)**
1. **Check Service Worker**: Look for duplicate fetch calls in service worker
2. **Content Script Review**: Ensure heartbeat calls aren't duplicated
3. **Event Handler Audit**: Verify event listeners are registered only once
4. **Race Condition Fix**: Add request deduplication logic

### **Network Layer**
1. **CORS Configuration**: Verify preflight requests aren't being logged as duplicates
2. **Load Balancer**: Check if proxy/load balancer is duplicating requests
3. **Browser Cache**: Ensure cache headers prevent unwanted retries

## Files Modified

1. **`/home/swhouse/projects/FaultMaven/faultmaven/infrastructure/request_logging.py`**
   - Added `RequestTracker` class for duplicate detection
   - Enhanced `RequestLoggingMiddleware` with comprehensive debugging
   - Added request signature tracking and duplicate warnings

2. **`/home/swhouse/projects/FaultMaven/.env`**
   - Enabled `LOG_LEVEL=DEBUG` for detailed request logging

3. **`/home/swhouse/projects/FaultMaven/detect_duplicate_cause.py`** (New)
   - Diagnostic tool for identifying duplicate request causes

4. **`/home/swhouse/projects/FaultMaven/debug_middleware.py`** (New)
   - Test script for reproducing middleware behavior

## Conclusion

The duplicate log entries are **not caused by server-side issues** (middleware registration, FastAPI configuration, or uvicorn setup). The enhanced middleware now provides comprehensive duplicate request detection and debugging capabilities.

The issue is most likely **client-side request duplication** from the browser extension or browser behavior. The new logging system will help identify the exact source and timing of duplicate requests.

**Action Required**: Monitor the logs with the new debugging system to identify the client-side source of duplicate requests.