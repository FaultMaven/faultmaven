"""
Redis-based Session Manager implementation.

This module provides a high-level session manager that wraps the RedisSessionStore
and provides session lifecycle management operations expected by the SessionService.
"""

import logging
import uuid
from datetime import datetime, timezone
from faultmaven.utils.serialization import to_json_compatible
from faultmaven.models import parse_utc_timestamp
from typing import Dict, List, Optional, Any

from faultmaven.models.interfaces import ISessionStore
from faultmaven.models.common import SessionContext


class RedisSessionManager:
    """Session manager that provides high-level session operations using RedisSessionStore"""

    def __init__(self, session_store: ISessionStore):
        """Initialize with a session store interface"""
        self.session_store = session_store
        self.logger = logging.getLogger(__name__)

    async def create_session(self, user_id: Optional[str] = None) -> SessionContext:
        """Create a new session"""
        session_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc)

        session_data = {
            'session_id': session_id,
            'user_id': user_id,
            'created_at': to_json_compatible(created_at),
            'last_activity': to_json_compatible(created_at),
            'data_uploads': [],
            'case_history': [],
            'metadata': {}
        }

        # Store in Redis
        await self.session_store.set(session_id, session_data)

        # Return SessionContext object
        return SessionContext(
            session_id=session_id,
            user_id=user_id,
            created_at=created_at,
            last_activity=created_at,
            data_uploads=[],
            case_history=[],
            metadata={}
        )

    async def get_session(self, session_id: str, validate: bool = True) -> Optional[SessionContext]:
        """Get session by ID"""
        session_data = await self.session_store.get(session_id)
        if not session_data:
            return None

        # Convert ISO strings back to datetime
        created_at = parse_utc_timestamp(session_data['created_at'])
        last_activity = parse_utc_timestamp(session_data['last_activity'])

        return SessionContext(
            session_id=session_data['session_id'],
            user_id=session_data.get('user_id'),
            created_at=created_at,
            last_activity=last_activity,
            data_uploads=session_data.get('data_uploads', []),
            case_history=session_data.get('case_history', []),
            metadata=session_data.get('metadata', {})
        )

    async def update_session(self, session_id: str, updates: Dict[str, Any]) -> bool:
        """Update session with new data"""
        session_data = await self.session_store.get(session_id)
        if not session_data:
            return False

        # Update the session data
        session_data.update(updates)
        session_data['last_activity'] = to_json_compatible(datetime.now(timezone.utc))

        # Save back to Redis
        await self.session_store.set(session_id, session_data)
        return True

    async def delete_session(self, session_id: str) -> bool:
        """Delete session"""
        return await self.session_store.delete(session_id)

    async def extend_session(self, session_id: str) -> bool:
        """Extend session TTL"""
        return await self.session_store.extend_ttl(session_id)

    async def update_last_activity(self, session_id: str) -> bool:
        """Update last activity timestamp"""
        return await self.update_session(session_id, {})  # This updates last_activity automatically

    async def list_sessions(self, user_id: Optional[str] = None) -> List[SessionContext]:
        """List sessions, optionally filtered by user_id"""
        # Note: This is a simplified implementation. In production, you'd want
        # to maintain separate indexes for efficient querying
        sessions = []

        # For now, return empty list since RedisSessionStore doesn't have
        # a native way to list all sessions. This would need additional indexing.
        self.logger.warning("list_sessions is not fully implemented - would need session indexing")
        return sessions

    async def get_all_sessions(self) -> List[SessionContext]:
        """Get all sessions"""
        return await self.list_sessions()

    async def get_session_stats(self) -> Dict[str, Any]:
        """Get session statistics"""
        # Basic stats - in production you'd maintain counters
        return {
            'total_sessions': 0,  # Would need Redis counters or indexing
            'active_sessions': 0,
            'timestamp': to_json_compatible(datetime.now(timezone.utc))
        }

    async def add_data_upload(self, session_id: str, data_id: str) -> bool:
        """Add data upload to session"""
        session_data = await self.session_store.get(session_id)
        if not session_data:
            return False

        if 'data_uploads' not in session_data:
            session_data['data_uploads'] = []

        session_data['data_uploads'].append(data_id)
        session_data['last_activity'] = to_json_compatible(datetime.now(timezone.utc))

        await self.session_store.set(session_id, session_data)
        return True

    async def add_case_history(self, session_id: str, case_record: Dict[str, Any]) -> bool:
        """Add case history record to session"""
        session_data = await self.session_store.get(session_id)
        if not session_data:
            return False

        if 'case_history' not in session_data:
            session_data['case_history'] = []

        session_data['case_history'].append(case_record)
        session_data['last_activity'] = to_json_compatible(datetime.now(timezone.utc))

        await self.session_store.set(session_id, session_data)
        return True

    async def cleanup_session_data(self, session_id: str) -> bool:
        """Clean up session data (for now, just delete the session)"""
        return await self.delete_session(session_id)