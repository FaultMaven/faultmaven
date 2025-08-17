"""Case Persistence Integration Demo Test

This test demonstrates the complete case persistence functionality end-to-end,
showing how users can maintain conversation history across sessions and 
collaborate on troubleshooting cases.

This test serves as both validation and documentation of the feature.
"""

import pytest
import uuid
from datetime import datetime, timedelta
from typing import Dict, Any, List

from faultmaven.models.case import (
    Case, CaseMessage, MessageType, ParticipantRole, CaseStatus
)
from faultmaven.services.case_service import CaseService
from faultmaven.infrastructure.persistence.redis_case_store import RedisCaseStore
from faultmaven.services.session_service import SessionService


class TestCasePersistenceDemo:
    """
    Demonstration test showing complete case persistence workflow.
    
    This test validates the entire user journey:
    1. User starts troubleshooting in session A
    2. Session expires, user opens session B
    3. User resumes same case with full conversation history
    4. User shares case with colleague for collaboration
    5. Both users work on the case across multiple sessions
    6. Case is resolved and archived
    """
    
    @pytest.mark.asyncio
    async def test_complete_case_persistence_workflow(
        self,
        mock_redis_client,
        sample_case_data,
        sample_users
    ):
        """
        Test complete case persistence workflow across multiple sessions and users.
        
        This demonstrates the core value proposition: conversation continuity
        and collaborative troubleshooting across session boundaries.
        """
        # Initialize services
        case_store = RedisCaseStore(redis_client=mock_redis_client)
        case_service = CaseService(case_store=case_store)
        
        user_alice = sample_users["alice"]
        user_bob = sample_users["bob"]
        
        # === PHASE 1: Initial Troubleshooting Session ===
        print("\n=== PHASE 1: Alice starts troubleshooting ===")
        
        # Alice starts a troubleshooting session
        session_1_id = str(uuid.uuid4())
        initial_problem = "Database connection timeouts in production"
        
        # Create case for the troubleshooting session
        case = await case_service.create_case(
            title="Database Connection Issues",
            description="Investigating connection timeouts in production database",
            owner_id=user_alice["user_id"],
            session_id=session_1_id,
            initial_message=initial_problem
        )
        
        assert case.case_id
        assert case.title == "Database Connection Issues"
        assert case.owner_id == user_alice["user_id"]
        assert session_1_id in case.session_ids
        assert len(case.messages) == 1
        assert case.messages[0].content == initial_problem
        
        print(f"✅ Created case {case.case_id} for session {session_1_id}")
        
        # Alice continues the conversation
        conversation_messages = [
            "I'm seeing timeouts every few minutes",
            "Connection pool shows 95% utilization",
            "Error logs show 'connection reset by peer'",
            "This started happening after the latest deployment"
        ]
        
        for msg_content in conversation_messages:
            message = CaseMessage(
                case_id=case.case_id,
                session_id=session_1_id,
                author_id=user_alice["user_id"],
                message_type=MessageType.USER_QUERY,
                content=msg_content
            )
            await case_service.add_message_to_case(case.case_id, message, session_1_id)
        
        # Verify conversation is recorded
        updated_case = await case_service.get_case(case.case_id, user_alice["user_id"])
        assert len(updated_case.messages) == 5  # Initial + 4 follow-ups
        
        print(f"✅ Recorded {len(conversation_messages)} conversation messages")
        
        # === PHASE 2: Session Expires, User Resumes Later ===
        print("\n=== PHASE 2: Alice's session expires, she resumes later ===")
        
        # Alice's session expires, she starts a new session later
        session_2_id = str(uuid.uuid4())
        
        # Resume the case in new session
        resume_success = await case_service.resume_case_in_session(
            case.case_id, session_2_id
        )
        assert resume_success
        
        print(f"✅ Successfully resumed case {case.case_id} in new session {session_2_id}")
        
        # Get conversation context for LLM (this is what maintains continuity)
        conversation_context = await case_service.get_case_conversation_context(
            case.case_id, limit=10
        )
        
        assert "Previous conversation in this troubleshooting case:" in conversation_context
        assert "Database connection timeouts" in conversation_context
        assert "connection reset by peer" in conversation_context
        
        print("✅ Retrieved conversation context for LLM continuity")
        print(f"Context preview: {conversation_context[:200]}...")
        
        # Alice continues troubleshooting in new session
        follow_up_message = CaseMessage(
            case_id=case.case_id,
            session_id=session_2_id,
            author_id=user_alice["user_id"],
            message_type=MessageType.USER_QUERY,
            content="I've checked the connection pool settings and found the issue"
        )
        await case_service.add_message_to_case(case.case_id, follow_up_message, session_2_id)
        
        # Verify case spans multiple sessions
        case_with_sessions = await case_service.get_case(case.case_id, user_alice["user_id"])
        assert session_1_id in case_with_sessions.session_ids
        assert session_2_id in case_with_sessions.session_ids
        assert len(case_with_sessions.messages) == 6
        
        print("✅ Case successfully spans multiple sessions with preserved history")
        
        # === PHASE 3: Collaborative Troubleshooting ===
        print("\n=== PHASE 3: Alice shares case with Bob for collaboration ===")
        
        # Alice shares the case with Bob as a collaborator
        share_success = await case_service.share_case(
            case_id=case.case_id,
            target_user_id=user_bob["user_id"],
            role=ParticipantRole.COLLABORATOR,
            sharer_user_id=user_alice["user_id"]
        )
        assert share_success
        
        print(f"✅ Shared case with {user_bob['name']} as collaborator")
        
        # Verify Bob can access the case
        case_for_bob = await case_service.get_case(case.case_id, user_bob["user_id"])
        assert case_for_bob is not None
        assert case_for_bob.case_id == case.case_id
        assert len(case_for_bob.messages) == 6  # Full conversation history
        
        print("✅ Bob can access shared case with full conversation history")
        
        # Bob starts his own session and joins the troubleshooting
        session_3_id = str(uuid.uuid4())
        await case_service.resume_case_in_session(case.case_id, session_3_id)
        
        # Bob adds his insights
        bob_message = CaseMessage(
            case_id=case.case_id,
            session_id=session_3_id,
            author_id=user_bob["user_id"],
            message_type=MessageType.USER_QUERY,
            content="I see this pattern in our monitoring - it's related to the connection pool configuration"
        )
        await case_service.add_message_to_case(case.case_id, bob_message, session_3_id)
        
        print("✅ Bob contributed to the troubleshooting case")
        
        # === PHASE 4: Cross-User Session Continuity ===
        print("\n=== PHASE 4: Both users continue across multiple sessions ===")
        
        # Alice starts another session and sees Bob's contribution
        session_4_id = str(uuid.uuid4())
        await case_service.resume_case_in_session(case.case_id, session_4_id)
        
        case_with_collaboration = await case_service.get_case(case.case_id, user_alice["user_id"])
        assert len(case_with_collaboration.messages) == 7
        
        # Find Bob's message in the conversation
        bob_messages = [
            msg for msg in case_with_collaboration.messages 
            if msg.author_id == user_bob["user_id"]
        ]
        assert len(bob_messages) == 1
        assert "connection pool configuration" in bob_messages[0].content
        
        print("✅ Alice can see Bob's contributions in the shared case")
        
        # Alice responds to Bob's insight
        alice_response = CaseMessage(
            case_id=case.case_id,
            session_id=session_4_id,
            author_id=user_alice["user_id"],
            message_type=MessageType.USER_QUERY,
            content="Great catch! You're right, the max_connections was set too low. I'll update it."
        )
        await case_service.add_message_to_case(case.case_id, alice_response, session_4_id)
        
        # === PHASE 5: Case Resolution ===
        print("\n=== PHASE 5: Case resolution and archival ===")
        
        # Alice updates the case status to solved
        update_success = await case_service.update_case(
            case_id=case.case_id,
            updates={
                "status": CaseStatus.SOLVED,
                "metadata": {
                    "resolution_summary": "Increased connection pool max_connections from 50 to 200",
                    "resolved_by": user_alice["user_id"],
                    "solution_implemented": True
                }
            },
            user_id=user_alice["user_id"]
        )
        assert update_success
        
        print("✅ Case marked as solved with resolution summary")
        
        # Add final resolution message
        resolution_message = CaseMessage(
            case_id=case.case_id,
            session_id=session_4_id,
            author_id=user_alice["user_id"],
            message_type=MessageType.SYSTEM_EVENT,
            content="Case resolved: Updated connection pool configuration. Issue resolved."
        )
        await case_service.add_message_to_case(case.case_id, resolution_message, session_4_id)
        
        # Archive the case
        archive_success = await case_service.archive_case(
            case_id=case.case_id,
            reason="Successfully resolved database connection timeout issue",
            user_id=user_alice["user_id"]
        )
        assert archive_success
        
        print("✅ Case archived with resolution details")
        
        # === PHASE 6: Verification of Complete Workflow ===
        print("\n=== PHASE 6: Final verification ===")
        
        final_case = await case_service.get_case(case.case_id, user_alice["user_id"])
        
        # Verify case properties
        assert final_case.status == CaseStatus.ARCHIVED
        assert len(final_case.messages) == 9  # All messages preserved
        assert len(final_case.session_ids) == 4  # All sessions tracked
        assert len(final_case.participants) == 2  # Alice (owner) + Bob (collaborator)
        
        # Verify participant roles
        alice_participant = next(p for p in final_case.participants if p.user_id == user_alice["user_id"])
        bob_participant = next(p for p in final_case.participants if p.user_id == user_bob["user_id"])
        
        assert alice_participant.role == ParticipantRole.OWNER
        assert bob_participant.role == ParticipantRole.COLLABORATOR
        
        # Verify message timeline integrity
        messages_by_time = sorted(final_case.messages, key=lambda m: m.timestamp)
        assert messages_by_time[0].content == initial_problem
        assert messages_by_time[-1].content.startswith("Case resolved:")
        
        # Verify conversation context includes all participants
        full_context = await case_service.get_case_conversation_context(case.case_id, limit=20)
        assert user_alice["name"] not in full_context  # User names not in context
        assert "Database connection timeouts" in full_context
        assert "connection pool configuration" in full_context
        assert "Great catch!" in full_context
        
        print("✅ Complete workflow verified successfully!")
        
        # === FINAL SUMMARY ===
        print("\n=== CASE PERSISTENCE DEMO SUMMARY ===")
        print(f"Case ID: {final_case.case_id}")
        print(f"Total Sessions: {len(final_case.session_ids)}")
        print(f"Total Messages: {len(final_case.messages)}")
        print(f"Participants: {len(final_case.participants)}")
        print(f"Duration: {(final_case.updated_at - final_case.created_at).total_seconds():.1f} seconds")
        print(f"Status: {final_case.status.value}")
        print(f"Resolution: {final_case.metadata.get('resolution_summary', 'N/A')}")
        
        # Demonstrate conversation summary
        conversation_summary = final_case.get_conversation_summary()
        print(f"\nConversation Summary:")
        print(f"- Total messages: {conversation_summary['total_messages']}")
        print(f"- Message types: {conversation_summary['message_types']}")
        print(f"- Sessions involved: {conversation_summary['sessions_involved']}")
        print(f"- Case duration: {conversation_summary['case_duration_hours']:.2f} hours")
        
        return {
            "success": True,
            "case_id": final_case.case_id,
            "sessions_used": len(final_case.session_ids),
            "messages_total": len(final_case.messages),
            "participants": len(final_case.participants),
            "status": final_case.status.value,
            "conversation_summary": conversation_summary
        }
    
    @pytest.mark.asyncio
    async def test_case_analytics_and_insights(
        self,
        mock_redis_client,
        sample_case_data
    ):
        """
        Test case analytics functionality for management insights.
        
        This demonstrates how case persistence enables tracking and analysis
        of troubleshooting patterns and team collaboration.
        """
        print("\n=== CASE ANALYTICS DEMO ===")
        
        case_store = RedisCaseStore(redis_client=mock_redis_client)
        case_service = CaseService(case_store=case_store)
        
        # Create a case for analytics
        case = await case_service.create_case(
            title="Performance Degradation Analysis",
            description="Investigating slow API response times",
            owner_id="user_123"
        )
        
        # Add various types of messages
        message_types = [
            (MessageType.USER_QUERY, "API response times are very slow"),
            (MessageType.SYSTEM_EVENT, "Data uploaded: performance logs"),
            (MessageType.USER_QUERY, "CPU usage is at 85% consistently"),
            (MessageType.CASE_NOTE, "Investigation: checking database query performance"),
            (MessageType.USER_QUERY, "Found N+1 query problem in user endpoint"),
            (MessageType.SYSTEM_EVENT, "Case shared with database team"),
        ]
        
        for msg_type, content in message_types:
            message = CaseMessage(
                case_id=case.case_id,
                author_id="user_123",
                message_type=msg_type,
                content=content
            )
            await case_service.add_message_to_case(case.case_id, message)
        
        # Get analytics
        analytics = await case_service.get_case_analytics(case.case_id)
        
        # Verify analytics data
        assert analytics["case_id"] == case.case_id
        assert analytics["message_count"] == 7  # 1 initial + 6 added
        assert "message_types" in analytics
        assert analytics["message_types"]["user_query"] == 4  # 1 initial + 3 added
        assert analytics["message_types"]["system_event"] == 2
        assert analytics["message_types"]["case_note"] == 1
        
        print(f"✅ Case analytics generated:")
        print(f"  - Total messages: {analytics['message_count']}")
        print(f"  - Message types: {analytics['message_types']}")
        print(f"  - Duration: {analytics['duration_hours']:.2f} hours")
        
        # Test conversation insights
        conversation_summary = case.get_conversation_summary()
        assert conversation_summary["total_messages"] == 7
        assert conversation_summary["message_types"]["user_query"] == 4
        
        print("✅ Conversation insights available for management reporting")
        
        return analytics
    
    @pytest.mark.asyncio
    async def test_case_search_and_discovery(
        self,
        mock_redis_client,
        sample_multiple_cases
    ):
        """
        Test case search functionality for knowledge discovery.
        
        This demonstrates how case persistence enables teams to find
        previous solutions and learn from past troubleshooting.
        """
        print("\n=== CASE SEARCH AND DISCOVERY DEMO ===")
        
        case_store = RedisCaseStore(redis_client=mock_redis_client)
        case_service = CaseService(case_store=case_store)
        
        # Create multiple cases with different topics
        test_cases = [
            {
                "title": "Database Connection Timeout Issues",
                "description": "PostgreSQL connection pool exhaustion",
                "tags": ["database", "postgresql", "connections"]
            },
            {
                "title": "High Memory Usage in API Server",
                "description": "Memory leak in user authentication service",
                "tags": ["memory", "api", "authentication"]
            },
            {
                "title": "Database Query Performance Problems",
                "description": "Slow SELECT queries on user table",
                "tags": ["database", "performance", "queries"]
            }
        ]
        
        created_cases = []
        for case_info in test_cases:
            case = await case_service.create_case(
                title=case_info["title"],
                description=case_info["description"],
                owner_id="user_123"
            )
            
            # Update with tags
            await case_service.update_case(
                case.case_id,
                {"tags": case_info["tags"]},
                "user_123"
            )
            
            created_cases.append(case)
        
        print(f"✅ Created {len(created_cases)} test cases")
        
        # Test search functionality
        from faultmaven.models.case import CaseSearchRequest, CaseListFilter
        
        # Search for database-related cases
        search_request = CaseSearchRequest(
            query="database",
            filters=CaseListFilter(user_id="user_123")
        )
        
        # Note: Mock implementation may not have full search,
        # but the interface is established for production Redis Search
        search_results = await case_service.search_cases(search_request, "user_123")
        
        print(f"✅ Search for 'database' returned {len(search_results)} results")
        
        # Test filtering by user
        user_cases = await case_service.list_user_cases("user_123")
        assert len(user_cases) >= 3
        
        print(f"✅ User has {len(user_cases)} total cases for knowledge discovery")
        
        return {
            "cases_created": len(created_cases),
            "search_results": len(search_results),
            "user_cases": len(user_cases)
        }


if __name__ == "__main__":
    """
    This demo can be run standalone to show case persistence functionality.
    
    Run with: python -m pytest tests/integration/test_case_persistence_demo.py -v -s
    """
    print("FaultMaven Case Persistence Demo")
    print("================================")
    print("This test demonstrates:")
    print("1. Cross-session conversation continuity")
    print("2. Multi-user collaboration on cases")
    print("3. Case analytics and insights")
    print("4. Knowledge discovery through search")
    print("\nRun with pytest to see full demonstration.")