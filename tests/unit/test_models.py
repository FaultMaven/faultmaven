"""Test for models to improve coverage"""

import pytest
from datetime import datetime, timedelta
from faultmaven.models_original import SessionContext


def test_session_active_property():
    """Test the SessionContext.active property for coverage"""
    # Create a session with recent activity
    recent_session = SessionContext(
        session_id="test_session",
        user_id="test_user",
        created_at=datetime.utcnow(),
        last_activity=datetime.utcnow() - timedelta(hours=1)  # 1 hour ago
    )
    
    # Should be active (within 24 hours)
    assert recent_session.active is True
    
    # Create a session with old activity
    old_session = SessionContext(
        session_id="old_session",
        user_id="test_user", 
        created_at=datetime.utcnow() - timedelta(days=2),
        last_activity=datetime.utcnow() - timedelta(days=1, hours=1)  # Over 24 hours ago
    )
    
    # Should be inactive (over 24 hours)
    assert old_session.active is False