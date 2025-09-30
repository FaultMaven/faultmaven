"""Redis Case Store Implementation

Purpose: Redis-based implementation of ICaseStore interface

This module provides Redis-based persistence for troubleshooting cases,
implementing the ICaseStore interface for case data management.

Key Features:
- Redis-based case persistence with TTL support
- Efficient querying and filtering
- Message storage and retrieval
- User access management
- Analytics and metrics collection
"""

import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set
import asyncio

import redis.asyncio as redis

from faultmaven.models.case import (
    Case,
    CaseListFilter,
    CaseMessage,
    CaseSearchRequest,
    CaseSummary,
    CaseStatus,
    ParticipantRole,
    MessageType
)
from faultmaven.models.interfaces_case import ICaseStore
from faultmaven.models import parse_utc_timestamp
from faultmaven.infrastructure.redis_client import create_redis_client
from faultmaven.exceptions import ServiceException, ValidationException
import logging


class RedisCaseStore(ICaseStore):
    """Redis implementation of case persistence store"""

    def __init__(self, redis_client: Optional[redis.Redis] = None):
        """
        Initialize Redis case store

        Args:
            redis_client: Optional Redis client, creates default if None
        """
        self.logger = logging.getLogger(__name__)
        self.redis_client = redis_client or create_redis_client()
        
        # Redis key patterns
        self.case_key_pattern = "case:{case_id}"
        self.case_messages_key_pattern = "case:{case_id}:messages"
        self.user_cases_key_pattern = "user:{user_id}:cases"
        self.case_index_key = "cases:index"
        self.case_search_key_pattern = "cases:search:{field}"
        
        # Configuration
        self.default_case_ttl = 30 * 24 * 3600  # 30 days in seconds
        self.message_batch_size = 100
        self.search_result_limit = 1000

    async def _serialize_case(self, case: Case) -> Dict[str, Any]:
        """Serialize case object for Redis storage"""
        try:
            # Convert case to dict with proper serialization
            case_dict = case.dict()
            
            # Handle datetime serialization
            for field in ['created_at', 'updated_at', 'last_activity_at', 'expires_at']:
                if case_dict.get(field):
                    case_dict[field] = case_dict[field].isoformat() + 'Z'
            
            
            # Serialize participants
            if 'participants' in case_dict:
                case_dict['participants'] = [
                    {
                        **p,
                        'added_at': p['added_at'].isoformat() + 'Z' if p.get('added_at') else None,
                        'last_accessed': p['last_accessed'].isoformat() + 'Z' if p.get('last_accessed') else None
                    }
                    for p in case_dict['participants']
                ]
            
            # Serialize messages separately for efficiency
            messages = case_dict.pop('messages', [])
            case_dict['message_count'] = len(messages)
            
            return case_dict, messages
            
        except Exception as e:
            self.logger.error(f"Failed to serialize case: {e}")
            raise ServiceException(f"Case serialization failed: {str(e)}")

    async def _deserialize_case(self, case_data: Dict[str, Any], messages: Optional[List[Dict]] = None) -> Case:
        """Deserialize case object from Redis data"""
        try:
            # Parse datetime fields
            for field in ['created_at', 'updated_at', 'last_activity_at', 'expires_at']:
                if case_data.get(field):
                    case_data[field] = parse_utc_timestamp(case_data[field])
            
            
            # Parse participants
            if 'participants' in case_data:
                for participant in case_data['participants']:
                    if participant.get('added_at'):
                        participant['added_at'] = parse_utc_timestamp(participant['added_at'])
                    if participant.get('last_accessed'):
                        participant['last_accessed'] = parse_utc_timestamp(participant['last_accessed'])
            
            # Add messages if provided
            if messages:
                case_data['messages'] = [
                    CaseMessage(
                        **{
                            **msg,
                            'timestamp': parse_utc_timestamp(msg['timestamp']) if msg.get('timestamp') else datetime.utcnow()
                        }
                    )
                    for msg in messages
                ]
            else:
                case_data['messages'] = []
            
            return Case(**case_data)
            
        except Exception as e:
            self.logger.error(f"Failed to deserialize case: {e}")
            raise ServiceException(f"Case deserialization failed: {str(e)}")

    async def create_case(self, case: Case) -> bool:
        """Create a new case in Redis"""
        try:
            case_key = self.case_key_pattern.format(case_id=case.case_id)
            messages_key = self.case_messages_key_pattern.format(case_id=case.case_id)
            
            # Serialize case and messages
            case_data, messages = await self._serialize_case(case)
            
            # Use pipeline for atomic operations
            pipe = self.redis_client.pipeline()
            
            # Store case data
            pipe.hset(case_key, mapping={
                "data": json.dumps(case_data),
                "created_at": datetime.utcnow().isoformat() + 'Z',
                "case_id": case.case_id,
                "owner_id": case.owner_id or "",
                "status": case.status.value,
                "title": case.title
            })
            
            # Set TTL
            ttl = int((case.expires_at - datetime.utcnow()).total_seconds()) if case.expires_at else self.default_case_ttl
            pipe.expire(case_key, ttl)
            
            # Store messages if any
            if messages:
                for message in messages:
                    message_data = {
                        **message,
                        'timestamp': message['timestamp'].isoformat() + 'Z' if isinstance(message.get('timestamp'), datetime) else message.get('timestamp')
                    }
                    pipe.lpush(messages_key, json.dumps(message_data))
                pipe.expire(messages_key, ttl)
            
            # Add to case index
            pipe.sadd(self.case_index_key, case.case_id)
            
            # Add to user cases index if owner exists
            if case.owner_id:
                user_cases_key = self.user_cases_key_pattern.format(user_id=case.owner_id)
                pipe.sadd(user_cases_key, case.case_id)
                pipe.expire(user_cases_key, ttl)
            
            # Add to search indices
            pipe.sadd(f"cases:status:{case.status.value}", case.case_id)
            pipe.sadd(f"cases:priority:{case.priority.value}", case.case_id)
            
            # Execute pipeline
            results = await pipe.execute()
            
            if all(results[:4]):  # Check main operations succeeded
                self.logger.info(f"Created case {case.case_id} in Redis")
                return True
            else:
                self.logger.error(f"Failed to create case {case.case_id} - pipeline results: {results}")
                return False
                
        except Exception as e:
            self.logger.error(f"Failed to create case {case.case_id}: {e}")
            return False

    async def get_case(self, case_id: str) -> Optional[Case]:
        """Retrieve a case by ID"""
        try:
            case_key = self.case_key_pattern.format(case_id=case_id)
            messages_key = self.case_messages_key_pattern.format(case_id=case_id)
            
            # Get case data and messages in parallel
            case_data_raw, messages_raw = await asyncio.gather(
                self.redis_client.hgetall(case_key),
                self.redis_client.lrange(messages_key, 0, -1),
                return_exceptions=True
            )
            
            if isinstance(case_data_raw, Exception) or not case_data_raw:
                return None
            
            # Parse case data
            if 'data' not in case_data_raw:
                return None
                
            case_data = json.loads(case_data_raw['data'])
            
            # Parse messages
            messages = []
            if not isinstance(messages_raw, Exception) and messages_raw:
                for msg_raw in reversed(messages_raw):  # Reverse to get chronological order
                    try:
                        messages.append(json.loads(msg_raw))
                    except json.JSONDecodeError:
                        continue
            
            # Deserialize and return case
            return await self._deserialize_case(case_data, messages)
            
        except Exception as e:
            self.logger.error(f"Failed to get case {case_id}: {e}")
            return None

    async def update_case(self, case_id: str, updates: Dict[str, Any]) -> bool:
        """Update case data"""
        try:
            case_key = self.case_key_pattern.format(case_id=case_id)
            
            # Get current case data
            case_data_raw = await self.redis_client.hget(case_key, "data")
            if not case_data_raw:
                return False
            
            case_data = json.loads(case_data_raw)
            
            # Apply updates
            for key, value in updates.items():
                if key == 'participants' and isinstance(value, list):
                    # Handle participant updates specially
                    case_data[key] = value
                elif isinstance(value, datetime):
                    case_data[key] = value.isoformat() + 'Z'
                elif isinstance(value, set):
                    case_data[key] = list(value)
                else:
                    case_data[key] = value
            
            # Update metadata fields
            case_data['updated_at'] = datetime.utcnow().isoformat() + 'Z'
            
            # Use pipeline for atomic update
            pipe = self.redis_client.pipeline()
            
            # Update case data
            pipe.hset(case_key, "data", json.dumps(case_data))
            
            # Update searchable fields if changed
            if 'status' in updates:
                pipe.hset(case_key, "status", updates['status'])
            if 'title' in updates:
                pipe.hset(case_key, "title", updates['title'])
            
            results = await pipe.execute()
            
            if all(results):
                self.logger.debug(f"Updated case {case_id}")
                return True
            else:
                return False
                
        except Exception as e:
            self.logger.error(f"Failed to update case {case_id}: {e}")
            return False

    async def delete_case(self, case_id: str) -> bool:
        """Delete a case from Redis"""
        try:
            case_key = self.case_key_pattern.format(case_id=case_id)
            messages_key = self.case_messages_key_pattern.format(case_id=case_id)
            
            # Get case data to clean up indices
            case_data_raw = await self.redis_client.hgetall(case_key)
            
            pipe = self.redis_client.pipeline()
            
            # Delete main case data
            pipe.delete(case_key)
            pipe.delete(messages_key)
            
            # Remove from indices
            pipe.srem(self.case_index_key, case_id)
            
            if case_data_raw:
                # Remove from user cases
                if case_data_raw.get('owner_id'):
                    user_cases_key = self.user_cases_key_pattern.format(user_id=case_data_raw['owner_id'])
                    pipe.srem(user_cases_key, case_id)
                
                # Remove from search indices
                if case_data_raw.get('status'):
                    pipe.srem(f"cases:status:{case_data_raw['status']}", case_id)
                
                # Clean up case data to get priority
                try:
                    case_data = json.loads(case_data_raw.get('data', '{}'))
                    if case_data.get('priority'):
                        pipe.srem(f"cases:priority:{case_data['priority']}", case_id)
                except:
                    pass
            
            results = await pipe.execute()
            
            if results[0]:  # Check if main deletion succeeded
                self.logger.info(f"Deleted case {case_id}")
                return True
            else:
                return False
                
        except Exception as e:
            self.logger.error(f"Failed to delete case {case_id}: {e}")
            return False

    async def list_cases(self, filters: Optional[CaseListFilter] = None) -> List[CaseSummary]:
        """List cases with optional filtering"""
        try:
            # Get all case IDs or filtered set
            case_ids = set()
            
            if filters:
                # Apply filters to get candidate case IDs
                if filters.status:
                    status_cases = await self.redis_client.smembers(f"cases:status:{filters.status.value}")
                    case_ids = set(status_cases) if not case_ids else case_ids.intersection(status_cases)
                
                if filters.user_id:
                    user_cases_key = self.user_cases_key_pattern.format(user_id=filters.user_id)
                    user_cases = await self.redis_client.smembers(user_cases_key)
                    case_ids = set(user_cases) if not case_ids else case_ids.intersection(user_cases)

                if not case_ids and (filters.status or filters.user_id):
                    return []  # No matches for specific filters
            
            # Get all cases if no specific filters applied
            if not case_ids:
                case_ids = await self.redis_client.smembers(self.case_index_key)
            
            # Limit results
            limit = filters.limit if filters else 50
            offset = filters.offset if filters else 0
            
            case_ids_list = list(case_ids)[offset:offset + limit]
            
            # Get case summaries
            summaries = []
            for case_id in case_ids_list:
                case_key = self.case_key_pattern.format(case_id=case_id)
                case_data_raw = await self.redis_client.hgetall(case_key)
                
                if case_data_raw and 'data' in case_data_raw:
                    try:
                        case_data = json.loads(case_data_raw['data'])
                        
                        # Apply additional filters
                        if filters:
                            if filters.owner_id and case_data.get('owner_id') != filters.owner_id:
                                continue
                            if filters.priority and case_data.get('priority') != filters.priority.value:
                                continue
                            if filters.created_after:
                                created_at = parse_utc_timestamp(case_data.get('created_at'))
                                if created_at < filters.created_after:
                                    continue
                            if filters.created_before:
                                created_at = parse_utc_timestamp(case_data.get('created_at'))
                                if created_at > filters.created_before:
                                    continue
                        
                        # Create summary
                        summary = CaseSummary(
                            case_id=case_data['case_id'],
                            title=case_data['title'],
                            status=CaseStatus(case_data['status']),
                            priority=case_data.get('priority', 'medium'),
                            owner_id=case_data.get('owner_id'),
                            created_at=parse_utc_timestamp(case_data['created_at']),
                            updated_at=parse_utc_timestamp(case_data['updated_at']),
                            last_activity_at=parse_utc_timestamp(case_data['last_activity_at']),
                            message_count=case_data.get('message_count', 0),
                            participant_count=len(case_data.get('participants', [])),
                            tags=case_data.get('tags', []),
                        )
                        
                        summaries.append(summary)
                        
                    except Exception as e:
                        self.logger.warning(f"Failed to parse case {case_id}: {e}")
                        continue
            
            # Sort by last activity (most recent first)
            summaries.sort(key=lambda s: s.last_activity_at, reverse=True)
            
            return summaries
            
        except Exception as e:
            self.logger.error(f"Failed to list cases: {e}")
            return []

    async def search_cases(self, search_request: CaseSearchRequest) -> List[CaseSummary]:
        """Search cases by content"""
        try:
            # For basic implementation, we'll search case titles and descriptions
            # In production, this could use Redis search module or Elasticsearch
            
            query = search_request.query.lower()
            all_cases = await self.list_cases(search_request.filters)
            
            matching_cases = []
            
            for case_summary in all_cases:
                # Search in title
                if query in case_summary.title.lower():
                    matching_cases.append(case_summary)
                    continue
                
                # Search in messages if enabled
                if search_request.search_in_messages:
                    # Get case messages
                    messages = await self.get_case_messages(case_summary.case_id, limit=100)
                    for message in messages:
                        if query in message.content.lower():
                            matching_cases.append(case_summary)
                            break
            
            return matching_cases[:self.search_result_limit]
            
        except Exception as e:
            self.logger.error(f"Failed to search cases: {e}")
            return []

    async def add_message_to_case(self, case_id: str, message: CaseMessage) -> bool:
        """Add a message to a case"""
        try:
            messages_key = self.case_messages_key_pattern.format(case_id=case_id)
            case_key = self.case_key_pattern.format(case_id=case_id)
            
            # Serialize message
            message_data = {
                **message.dict(),
                'timestamp': message.timestamp.isoformat() + 'Z'
            }
            
            pipe = self.redis_client.pipeline()
            
            # Add message to list
            pipe.lpush(messages_key, json.dumps(message_data))
            
            # Update case message count
            case_data_raw = await self.redis_client.hget(case_key, "data")
            if case_data_raw:
                case_data = json.loads(case_data_raw)
                case_data['message_count'] = case_data.get('message_count', 0) + 1
                case_data['last_activity_at'] = datetime.utcnow().isoformat() + 'Z'
                pipe.hset(case_key, "data", json.dumps(case_data))
            
            # Keep TTL on messages
            ttl = await self.redis_client.ttl(case_key)
            if ttl > 0:
                pipe.expire(messages_key, ttl)
            
            results = await pipe.execute()
            
            if results[0]:  # Check if message was added
                self.logger.debug(f"Added message to case {case_id}")
                return True
            else:
                return False
                
        except Exception as e:
            self.logger.error(f"Failed to add message to case {case_id}: {e}")
            return False

    async def get_case_messages(
        self,
        case_id: str,
        limit: int = 50,
        offset: int = 0
    ) -> List[CaseMessage]:
        """Get messages for a case"""
        try:
            messages_key = self.case_messages_key_pattern.format(case_id=case_id)
            
            # Get messages (Redis list is in reverse chronological order)
            messages_raw = await self.redis_client.lrange(messages_key, offset, offset + limit - 1)
            
            messages = []
            for msg_raw in reversed(messages_raw):  # Reverse to get chronological order
                try:
                    message_data = json.loads(msg_raw)
                    message_data['timestamp'] = parse_utc_timestamp(message_data['timestamp'])
                    messages.append(CaseMessage(**message_data))
                except Exception as e:
                    self.logger.warning(f"Failed to parse message: {e}")
                    continue
            
            return messages
            
        except Exception as e:
            self.logger.error(f"Failed to get messages for case {case_id}: {e}")
            return []

    async def get_case_messages_enhanced(
        self,
        case_id: str,
        limit: int = 50,
        offset: int = 0,
        include_debug: bool = False
    ) -> "CaseMessagesResponse":
        """Enhanced message retrieval with debugging support and metadata."""
        # Import here to avoid circular dependencies
        from faultmaven.models.api import CaseMessagesResponse, MessageRetrievalDebugInfo, Message
        import time

        start_time = time.time()
        debug_info = None
        storage_errors = []
        message_parsing_errors = 0
        redis_key = f"case:{case_id}:messages"

        try:
            # Get total message count first
            messages_key = self.case_messages_key_pattern.format(case_id=case_id)
            total_count = await self.redis_client.llen(messages_key)

            # Get paginated messages (Redis list is in reverse chronological order)
            messages_raw = await self.redis_client.lrange(messages_key, offset, offset + limit - 1)
            retrieved_count = len(messages_raw)

            # Convert raw messages to API Message format
            messages = []
            for msg_raw in reversed(messages_raw):  # Reverse to get chronological order
                try:
                    message_data = json.loads(msg_raw)

                    # Parse message type and map to role
                    msg_type = message_data.get('message_type', 'system_event')
                    role = "system"  # default

                    if msg_type == MessageType.USER_QUERY.value or msg_type == "user_query":
                        role = "user"
                    elif msg_type == MessageType.AGENT_RESPONSE.value or msg_type == "agent_response":
                        role = "assistant"  # Use "assistant" as per API spec
                    elif msg_type == MessageType.CASE_NOTE.value or msg_type == "case_note":
                        role = "user"
                    # Keep system for other types

                    # Format timestamp
                    created_at = None
                    timestamp = message_data.get('timestamp')
                    if timestamp:
                        try:
                            if isinstance(timestamp, str):
                                created_at = timestamp
                            else:
                                created_at = timestamp.isoformat() + 'Z' if hasattr(timestamp, 'isoformat') else str(timestamp)
                        except Exception as e:
                            self.logger.warning(f"Failed to format timestamp for message: {e}")
                            created_at = str(timestamp)

                    # Create API Message object
                    api_message = Message(
                        message_id=message_data.get('message_id', f"msg_{len(messages)}"),
                        role=role,
                        content=message_data.get('content', ''),
                        created_at=created_at,
                        metadata=message_data.get('metadata', {})
                    )
                    messages.append(api_message)

                except json.JSONDecodeError as e:
                    message_parsing_errors += 1
                    self.logger.warning(f"Failed to parse message JSON: {e}")
                    if include_debug:
                        storage_errors.append(f"JSON parsing error: {str(e)}")
                except Exception as e:
                    message_parsing_errors += 1
                    self.logger.warning(f"Failed to convert message: {e}")
                    if include_debug:
                        storage_errors.append(f"Message conversion error: {str(e)}")

            # Calculate performance metrics
            processing_time_ms = int((time.time() - start_time) * 1000)

            # Create debug info if requested
            if include_debug:
                debug_info = MessageRetrievalDebugInfo(
                    storage_backend="redis",
                    redis_key=redis_key,
                    total_messages_in_storage=total_count,
                    messages_requested=limit,
                    messages_retrieved=retrieved_count,
                    offset_used=offset,
                    processing_time_ms=processing_time_ms,
                    storage_errors=storage_errors,
                    message_parsing_errors=message_parsing_errors
                )

            # Determine if there are more messages
            has_more = (offset + retrieved_count) < total_count

            # Create and return response
            response = CaseMessagesResponse(
                messages=messages,
                total_count=total_count,
                retrieved_count=retrieved_count,
                has_more=has_more,
                debug_info=debug_info
            )

            self.logger.debug(
                f"Enhanced retrieval: {retrieved_count}/{total_count} messages for case {case_id} "
                f"in {processing_time_ms}ms"
            )

            return response

        except Exception as e:
            self.logger.error(f"Failed to get enhanced messages for case {case_id}: {e}")

            # Return empty response with error info for graceful degradation
            processing_time_ms = int((time.time() - start_time) * 1000)
            if include_debug:
                debug_info = MessageRetrievalDebugInfo(
                    storage_backend="redis",
                    redis_key=redis_key,
                    total_messages_in_storage=0,
                    messages_requested=limit,
                    messages_retrieved=0,
                    offset_used=offset,
                    processing_time_ms=processing_time_ms,
                    storage_errors=[f"Redis error: {str(e)}"],
                    message_parsing_errors=0
                )

            return CaseMessagesResponse(
                messages=[],
                total_count=0,
                retrieved_count=0,
                has_more=False,
                debug_info=debug_info
            )

    async def get_user_cases(
        self,
        user_id: str,
        filters: Optional[CaseListFilter] = None
    ) -> List[CaseSummary]:
        """Get cases for a specific user"""
        try:
            # Create user-specific filter
            user_filter = filters or CaseListFilter()
            user_filter.user_id = user_id
            
            return await self.list_cases(user_filter)
            
        except Exception as e:
            self.logger.error(f"Failed to get cases for user {user_id}: {e}")
            return []

    async def add_case_participant(
        self,
        case_id: str,
        user_id: str,
        role: ParticipantRole,
        added_by: Optional[str] = None
    ) -> bool:
        """Add a participant to a case"""
        try:
            case_key = self.case_key_pattern.format(case_id=case_id)
            
            # Get current case data
            case_data_raw = await self.redis_client.hget(case_key, "data")
            if not case_data_raw:
                return False
            
            case_data = json.loads(case_data_raw)
            participants = case_data.get('participants', [])
            
            # Check if user is already a participant
            for participant in participants:
                if participant['user_id'] == user_id:
                    return False  # Already exists
            
            # Add new participant
            new_participant = {
                'user_id': user_id,
                'role': role.value,
                'added_at': datetime.utcnow().isoformat() + 'Z',
                'added_by': added_by,
                'last_accessed': None,
                'can_edit': role in [ParticipantRole.OWNER, ParticipantRole.COLLABORATOR],
                'can_share': role in [ParticipantRole.OWNER, ParticipantRole.COLLABORATOR],
                'can_archive': role == ParticipantRole.OWNER
            }
            
            participants.append(new_participant)
            case_data['participants'] = participants
            case_data['participant_count'] = len(participants)
            case_data['updated_at'] = datetime.utcnow().isoformat() + 'Z'
            
            # Update case data
            await self.redis_client.hset(case_key, "data", json.dumps(case_data))
            
            # Add to user's cases index
            user_cases_key = self.user_cases_key_pattern.format(user_id=user_id)
            await self.redis_client.sadd(user_cases_key, case_id)
            
            # Set TTL on user cases index
            ttl = await self.redis_client.ttl(case_key)
            if ttl > 0:
                await self.redis_client.expire(user_cases_key, ttl)
            
            self.logger.info(f"Added participant {user_id} to case {case_id}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to add participant to case {case_id}: {e}")
            return False

    async def remove_case_participant(self, case_id: str, user_id: str) -> bool:
        """Remove a participant from a case"""
        try:
            case_key = self.case_key_pattern.format(case_id=case_id)
            
            # Get current case data
            case_data_raw = await self.redis_client.hget(case_key, "data")
            if not case_data_raw:
                return False
            
            case_data = json.loads(case_data_raw)
            participants = case_data.get('participants', [])
            
            # Find and remove participant
            updated_participants = []
            removed = False
            
            for participant in participants:
                if participant['user_id'] == user_id:
                    # Don't remove owner
                    if participant['role'] == ParticipantRole.OWNER.value:
                        return False
                    removed = True
                else:
                    updated_participants.append(participant)
            
            if not removed:
                return False  # Participant not found
            
            case_data['participants'] = updated_participants
            case_data['participant_count'] = len(updated_participants)
            case_data['updated_at'] = datetime.utcnow().isoformat() + 'Z'
            
            pipe = self.redis_client.pipeline()
            
            # Update case data
            pipe.hset(case_key, "data", json.dumps(case_data))
            
            # Remove from user's cases index
            user_cases_key = self.user_cases_key_pattern.format(user_id=user_id)
            pipe.srem(user_cases_key, case_id)
            
            results = await pipe.execute()
            
            if results[0]:
                self.logger.info(f"Removed participant {user_id} from case {case_id}")
                return True
            else:
                return False
                
        except Exception as e:
            self.logger.error(f"Failed to remove participant from case {case_id}: {e}")
            return False

    async def update_case_activity(self, case_id: str, session_id: Optional[str] = None) -> bool:
        """Update case last activity timestamp"""
        try:
            case_key = self.case_key_pattern.format(case_id=case_id)
            
            # Get current case data
            case_data_raw = await self.redis_client.hget(case_key, "data")
            if not case_data_raw:
                return False
            
            case_data = json.loads(case_data_raw)
            case_data['last_activity_at'] = datetime.utcnow().isoformat() + 'Z'
            
            
            # Update case data
            await self.redis_client.hset(case_key, "data", json.dumps(case_data))
            
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to update activity for case {case_id}: {e}")
            return False

    async def cleanup_expired_cases(self, batch_size: int = 100) -> int:
        """Clean up expired cases"""
        try:
            cleaned_count = 0
            case_ids = await self.redis_client.smembers(self.case_index_key)
            
            for i in range(0, len(case_ids), batch_size):
                batch = list(case_ids)[i:i + batch_size]
                
                for case_id in batch:
                    case_key = self.case_key_pattern.format(case_id=case_id)
                    
                    # Check if case still exists (TTL expired)
                    exists = await self.redis_client.exists(case_key)
                    if not exists:
                        # Remove from index
                        await self.redis_client.srem(self.case_index_key, case_id)
                        cleaned_count += 1
                        continue
                    
                    # Check manual expiration
                    case_data_raw = await self.redis_client.hget(case_key, "data")
                    if case_data_raw:
                        try:
                            case_data = json.loads(case_data_raw)
                            if case_data.get('expires_at'):
                                expires_at = parse_utc_timestamp(case_data['expires_at'])
                                if expires_at < datetime.utcnow():
                                    # Case expired, delete it
                                    await self.delete_case(case_id)
                                    cleaned_count += 1
                        except Exception as e:
                            self.logger.warning(f"Failed to check expiration for case {case_id}: {e}")
            
            if cleaned_count > 0:
                self.logger.info(f"Cleaned up {cleaned_count} expired cases")
            
            return cleaned_count
            
        except Exception as e:
            self.logger.error(f"Failed to cleanup expired cases: {e}")
            return 0

    async def get_case_analytics(self, case_id: str) -> Dict[str, Any]:
        """Get analytics data for a case"""
        try:
            case = await self.get_case(case_id)
            if not case:
                return {}
            
            # Get message analytics
            messages = await self.get_case_messages(case_id, limit=1000)
            
            message_types = {}
            for message in messages:
                msg_type = message.message_type.value
                message_types[msg_type] = message_types.get(msg_type, 0) + 1
            
            # Calculate metrics
            duration_hours = 0
            if case.created_at:
                duration = datetime.utcnow() - case.created_at
                duration_hours = duration.total_seconds() / 3600
            
            return {
                'case_id': case_id,
                'duration_hours': round(duration_hours, 2),
                'message_count': len(messages),
                'message_types': message_types,
                'participant_count': len(case.participants),
                'status': case.status.value,
                'priority': case.priority.value,
                'is_expired': case.is_expired(),
                'created_at': case.created_at.isoformat() + 'Z' if case.created_at else None,
                'last_activity': case.last_activity_at.isoformat() + 'Z' if case.last_activity_at else None
            }
            
        except Exception as e:
            self.logger.error(f"Failed to get analytics for case {case_id}: {e}")
            return {}

    async def close(self):
        """Close Redis connection"""
        try:
            if hasattr(self.redis_client, 'aclose'):
                await self.redis_client.aclose()
            elif hasattr(self.redis_client, 'close'):
                await self.redis_client.close()
        except Exception as e:
            self.logger.warning(f"Error closing Redis case store connection: {e}")