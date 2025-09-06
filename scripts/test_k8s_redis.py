#!/usr/bin/env python3
"""
Test script for K8s Redis integration.

This script tests the enhanced Redis client factory with K8s authentication.
"""

import asyncio
import os
import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from faultmaven.infrastructure.redis_client import (
    create_redis_client,
    create_k8s_redis_client, 
    validate_redis_connection
)
# SessionManager has been replaced by SessionService
# from faultmaven.services.session import SessionService as SessionManager


async def test_k8s_redis_integration():
    """Test K8s Redis integration."""
    print("🔍 Testing K8s Redis Integration")
    print("=================================")
    
    # Test 1: K8s Redis client with explicit parameters
    print("\n1. Testing K8s Redis client with explicit parameters:")
    try:
        k8s_client = create_redis_client(
            host='192.168.0.111',
            port=30379,
            password='faultmaven-dev-redis-2025'
        )
        
        print("   ✅ K8s Redis client created successfully")
        
        # Test connection
        await validate_redis_connection(k8s_client)
        print("   ✅ K8s Redis connection validated")
        
        # Test basic operations
        await k8s_client.set('test:k8s', 'hello kubernetes')
        value = await k8s_client.get('test:k8s')
        print(f"   ✅ K8s Redis operation test: {value.decode()}")
        
        try:
            await k8s_client.aclose()
        except AttributeError:
            await k8s_client.close()
        
    except Exception as e:
        print(f"   ❌ K8s Redis client test failed: {e}")
    
    # Test 2: Environment variable configuration
    print("\n2. Testing environment variable configuration:")
    
    # Set environment variables
    os.environ['REDIS_HOST'] = '192.168.0.111'
    os.environ['REDIS_PORT'] = '30379'
    os.environ['REDIS_PASSWORD'] = 'faultmaven-dev-redis-2025'
    
    try:
        env_client = create_redis_client()
        print("   ✅ Environment-based Redis client created")
        
        await validate_redis_connection(env_client)
        print("   ✅ Environment-based Redis connection validated")
        
        try:
            await env_client.aclose()
        except AttributeError:
            await env_client.close()
        
    except Exception as e:
        print(f"   ❌ Environment-based Redis test failed: {e}")
    
    # Test 3: SessionManager with K8s Redis
    print("\n3. Testing SessionManager with K8s Redis:")
    try:
        session_manager = SessionManager(
            redis_host='192.168.0.111',
            redis_port=30379,
            redis_password='faultmaven-dev-redis-2025'
        )
        
        print("   ✅ SessionManager with K8s Redis created")
        
        # Test connection validation
        is_connected = await session_manager.validate_connection()
        print(f"   ✅ SessionManager connection valid: {is_connected}")
        
        # Test session operations
        session = await session_manager.create_session(user_id="test-user")
        print(f"   ✅ Session created: {session.session_id}")
        
        retrieved_session = await session_manager.get_session(session.session_id)
        print(f"   ✅ Session retrieved: {retrieved_session.session_id}")
        
        await session_manager.close()
        
    except Exception as e:
        print(f"   ❌ SessionManager test failed: {e}")
    
    # Test 4: Fallback to local Redis
    print("\n4. Testing fallback to local Redis:")
    
    # Clear K8s environment variables
    for key in ['REDIS_HOST', 'REDIS_PORT', 'REDIS_PASSWORD']:
        os.environ.pop(key, None)
    
    try:
        local_client = create_redis_client()
        print("   ✅ Local Redis client created (fallback)")
        
        # This will likely fail if local Redis isn't running, which is expected
        try:
            await validate_redis_connection(local_client)
            print("   ✅ Local Redis connection validated")
        except Exception as e:
            print(f"   ⚠️  Local Redis not available (expected): {e}")
        
        try:
            await local_client.aclose()
        except AttributeError:
            await local_client.close()
        
    except Exception as e:
        print(f"   ❌ Local Redis fallback test failed: {e}")
    
    print("\n✅ K8s Redis integration tests completed")


if __name__ == "__main__":
    asyncio.run(test_k8s_redis_integration())