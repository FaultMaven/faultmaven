"""Idempotency Middleware

Purpose: Handle Idempotency-Key headers for POST operations with Redis persistence

This middleware implements idempotency semantics for POST requests by:
- Storing request/response pairs in Redis with TTL
- Returning cached responses for duplicate idempotency keys
- Ensuring atomic operations across server restarts
- Supporting proper error handling and replay scenarios

Architecture Integration:
- Uses container.py dependency injection for Redis client
- Integrates with logging system for correlation tracking
- Follows FastAPI middleware patterns
"""

import json
import logging
import hashlib
from typing import Optional, Dict, Any
from uuid import uuid4

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """Middleware to handle idempotency keys for POST operations."""
    
    def __init__(self, app: ASGIApp, redis_client=None):
        super().__init__(app)
        self.redis_client = redis_client
        self.ttl_seconds = 3600  # 1 hour TTL for idempotency keys
        self.key_prefix = "idempotency:"
        
    async def dispatch(self, request: Request, call_next):
        """Process request with idempotency checking."""
        
        # Only handle POST requests
        if request.method != "POST":
            return await call_next(request)
            
        # Check for idempotency key
        idempotency_key = request.headers.get("Idempotency-Key")
        if not idempotency_key:
            return await call_next(request)
            
        # Validate idempotency key format (UUID-like)
        if not self._is_valid_idempotency_key(idempotency_key):
            return JSONResponse(
                status_code=400,
                content={
                    "detail": "Invalid Idempotency-Key format. Must be a valid UUID or similar identifier.",
                    "error_type": "InvalidIdempotencyKey",
                    "correlation_id": str(uuid4()),
                    "timestamp": self._get_timestamp()
                }
            )
            
        # Check if we have Redis client available
        if not self.redis_client:
            logger.warning("Redis client not available for idempotency - processing request normally")
            return await call_next(request)
            
        try:
            # Create cache key
            cache_key = self._create_cache_key(idempotency_key, request)
            
            # Check for existing response
            cached_response = await self._get_cached_response(cache_key)
            if cached_response:
                logger.info(f"Returning cached response for idempotency key: {idempotency_key}")
                return self._create_response_from_cache(cached_response)
                
            # Process request normally
            response = await call_next(request)
            
            # Cache successful responses (2xx status codes)
            if 200 <= response.status_code < 300:
                await self._cache_response(cache_key, response, idempotency_key)
                
            return response
            
        except Exception as e:
            logger.error(f"Error in idempotency middleware: {e}")
            # Continue processing on middleware errors
            return await call_next(request)
    
    def _is_valid_idempotency_key(self, key: str) -> bool:
        """Validate idempotency key format."""
        if not key or len(key) < 8 or len(key) > 255:
            return False
            
        # Allow UUID-like strings, alphanumeric with hyphens/underscores
        import re
        pattern = r'^[a-zA-Z0-9_-]+$'
        return bool(re.match(pattern, key))
    
    def _create_cache_key(self, idempotency_key: str, request: Request) -> str:
        """Create Redis cache key with request context."""
        # Include method and path for additional safety
        method_path = f"{request.method}:{request.url.path}"
        # Hash the combination to ensure consistent key length
        combined = f"{idempotency_key}:{method_path}"
        hash_suffix = hashlib.sha256(combined.encode()).hexdigest()[:16]
        return f"{self.key_prefix}{idempotency_key}:{hash_suffix}"
    
    async def _get_cached_response(self, cache_key: str) -> Optional[Dict[str, Any]]:
        """Retrieve cached response from Redis."""
        try:
            cached_data = await self.redis_client.get(cache_key)
            if cached_data:
                return json.loads(cached_data)
        except Exception as e:
            logger.error(f"Error retrieving cached response: {e}")
        return None
    
    async def _cache_response(self, cache_key: str, response: Response, idempotency_key: str):
        """Cache response in Redis with TTL."""
        try:
            # Read response body
            body = b""
            async for chunk in response.body_iterator:
                body += chunk
                
            # Prepare cache data
            cache_data = {
                "status_code": response.status_code,
                "headers": dict(response.headers),
                "body": body.decode('utf-8') if body else "",
                "content_type": response.headers.get("content-type", "application/json"),
                "idempotency_key": idempotency_key,
                "cached_at": self._get_timestamp()
            }
            
            # Store in Redis with TTL
            await self.redis_client.setex(
                cache_key,
                self.ttl_seconds,
                json.dumps(cache_data)
            )
            
            logger.info(f"Cached response for idempotency key: {idempotency_key}")
            
            # Recreate response with same body for return
            response.body_iterator = self._create_body_iterator(body)
            
        except Exception as e:
            logger.error(f"Error caching response: {e}")
    
    def _create_response_from_cache(self, cached_data: Dict[str, Any]) -> Response:
        """Create FastAPI response from cached data."""
        headers = cached_data.get("headers", {})
        
        # Add cache indicator header
        headers["X-Idempotency-Replayed"] = "true"
        
        # Create appropriate response type
        content_type = cached_data.get("content_type", "application/json")
        body = cached_data.get("body", "")
        
        if content_type.startswith("application/json"):
            try:
                json_body = json.loads(body) if body else {}
                return JSONResponse(
                    status_code=cached_data["status_code"],
                    content=json_body,
                    headers=headers
                )
            except json.JSONDecodeError:
                pass
        
        # Fallback to generic response
        return Response(
            status_code=cached_data["status_code"],
            content=body,
            headers=headers,
            media_type=content_type
        )
    
    def _create_body_iterator(self, body: bytes):
        """Create async iterator for response body."""
        async def body_iterator():
            yield body
        return body_iterator()
    
    def _get_timestamp(self) -> str:
        """Get ISO timestamp for caching."""
        from datetime import datetime
        return datetime.utcnow().isoformat() + "Z"


def create_idempotency_middleware(app: ASGIApp, redis_client=None) -> IdempotencyMiddleware:
    """Factory function to create idempotency middleware with Redis client."""
    return IdempotencyMiddleware(app, redis_client=redis_client)