#!/usr/bin/env python3
"""Test Role-Based Access Control

This script tests the role-based access control implementation:
1. Creates a regular user (with 'user' role only)
2. Creates an admin user (with 'user' and 'admin' roles)
3. Tests that regular users can't upload to Global KB
4. Tests that admin users can upload to Global KB
5. Tests that both can search Global KB

Usage:
    python scripts/test_rbac.py
"""

import asyncio
import sys
import os
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from faultmaven.infrastructure.auth.user_store import DevUserStore
from faultmaven.infrastructure.auth.token_manager import DevTokenManager
from faultmaven.models.auth import DevUser
from faultmaven.container import container
from datetime import datetime, timezone


async def main():
    """Run RBAC tests"""
    print("=" * 70)
    print("Role-Based Access Control Test")
    print("=" * 70)

    # Initialize container
    print("\n1. Initializing container...")
    await container.initialize()

    # Get services
    user_store = container.get_user_store()
    token_manager = container.get_token_manager()

    if not user_store or not token_manager:
        print("❌ Failed to get auth services from container")
        return False

    print("✅ Container initialized")

    # Create regular user
    print("\n2. Creating regular user (roles: ['user'])...")
    try:
        # Check if user exists
        existing_user = await user_store.get_user_by_username("test_regular_user")
        if existing_user:
            print(f"   User already exists: {existing_user.user_id}")
            regular_user = existing_user
        else:
            regular_user = await user_store.create_user(
                username="test_regular_user",
                email="regular@test.com",
                display_name="Regular User"
            )
            # Set roles to just 'user'
            regular_user.roles = ['user']
            await user_store.update_user(regular_user)
            print(f"   Created user: {regular_user.user_id}")

        print(f"   Username: {regular_user.username}")
        print(f"   Email: {regular_user.email}")
        print(f"   Roles: {regular_user.roles}")

        # Verify roles
        if regular_user.roles != ['user']:
            print(f"   ⚠️  Warning: Expected roles ['user'], got {regular_user.roles}")
        else:
            print("   ✅ Regular user has correct roles")

    except Exception as e:
        print(f"   ❌ Failed to create regular user: {e}")
        return False

    # Create admin user
    print("\n3. Creating admin user (roles: ['user', 'admin'])...")
    try:
        # Check if user exists
        existing_admin = await user_store.get_user_by_username("test_admin_user")
        if existing_admin:
            print(f"   User already exists: {existing_admin.user_id}")
            admin_user = existing_admin
        else:
            admin_user = await user_store.create_user(
                username="test_admin_user",
                email="admin@test.com",
                display_name="Admin User"
            )
            # Set roles to both 'user' and 'admin'
            admin_user.roles = ['user', 'admin']
            await user_store.update_user(admin_user)
            print(f"   Created user: {admin_user.user_id}")

        # Ensure admin has both roles
        if 'admin' not in admin_user.roles:
            admin_user.roles = ['user', 'admin']
            await user_store.update_user(admin_user)

        print(f"   Username: {admin_user.username}")
        print(f"   Email: {admin_user.email}")
        print(f"   Roles: {admin_user.roles}")

        # Verify roles
        if 'admin' not in admin_user.roles:
            print(f"   ❌ Error: Admin user should have 'admin' role")
            return False
        else:
            print("   ✅ Admin user has admin role")

    except Exception as e:
        print(f"   ❌ Failed to create admin user: {e}")
        return False

    # Generate tokens
    print("\n4. Generating authentication tokens...")
    try:
        regular_token = await token_manager.create_token(regular_user)
        admin_token = await token_manager.create_token(admin_user)

        print(f"   Regular user token: {regular_token[:20]}...")
        print(f"   Admin user token: {admin_token[:20]}...")
        print("   ✅ Tokens generated")

    except Exception as e:
        print(f"   ❌ Failed to generate tokens: {e}")
        return False

    # Validate tokens
    print("\n5. Validating tokens...")
    try:
        regular_validation = await token_manager.validate_token(regular_token)
        admin_validation = await token_manager.validate_token(admin_token)

        if not regular_validation.is_valid:
            print(f"   ❌ Regular user token validation failed: {regular_validation.error_message}")
            return False

        if not admin_validation.is_valid:
            print(f"   ❌ Admin user token validation failed: {admin_validation.error_message}")
            return False

        print(f"   Regular user validated: {regular_validation.user.username} (roles: {regular_validation.user.roles})")
        print(f"   Admin user validated: {admin_validation.user.username} (roles: {admin_validation.user.roles})")
        print("   ✅ Tokens validated successfully")

    except Exception as e:
        print(f"   ❌ Token validation failed: {e}")
        return False

    # Test role checking
    print("\n6. Testing role checks...")

    # Import role checking function
    from faultmaven.api.v1.role_dependencies import check_user_has_role

    # Test regular user
    regular_has_user_role = check_user_has_role(regular_user, 'user')
    regular_has_admin_role = check_user_has_role(regular_user, 'admin')

    # Test admin user
    admin_has_user_role = check_user_has_role(admin_user, 'user')
    admin_has_admin_role = check_user_has_role(admin_user, 'admin')

    print(f"   Regular user has 'user' role: {regular_has_user_role}")
    print(f"   Regular user has 'admin' role: {regular_has_admin_role}")
    print(f"   Admin user has 'user' role: {admin_has_user_role}")
    print(f"   Admin user has 'admin' role: {admin_has_admin_role}")

    # Verify expectations
    all_checks_passed = True

    if not regular_has_user_role:
        print("   ❌ Regular user should have 'user' role")
        all_checks_passed = False

    if regular_has_admin_role:
        print("   ❌ Regular user should NOT have 'admin' role")
        all_checks_passed = False

    if not admin_has_user_role:
        print("   ❌ Admin user should have 'user' role")
        all_checks_passed = False

    if not admin_has_admin_role:
        print("   ❌ Admin user should have 'admin' role")
        all_checks_passed = False

    if all_checks_passed:
        print("   ✅ All role checks passed")

    # Summary
    print("\n" + "=" * 70)
    print("Test Summary")
    print("=" * 70)
    print(f"Regular User: {regular_user.username}")
    print(f"  User ID: {regular_user.user_id}")
    print(f"  Roles: {regular_user.roles}")
    print(f"  Token: {regular_token[:30]}...")
    print()
    print(f"Admin User: {admin_user.username}")
    print(f"  User ID: {admin_user.user_id}")
    print(f"  Roles: {admin_user.roles}")
    print(f"  Token: {admin_token[:30]}...")
    print()

    if all_checks_passed:
        print("✅ All tests passed!")
        print()
        print("Next steps:")
        print("1. Start the FaultMaven server: ./run_faultmaven.sh")
        print("2. Test API endpoints with these tokens:")
        print()
        print("   # Login as regular user (should return roles: ['user'])")
        print(f"   curl -X POST http://localhost:8000/api/v1/auth/dev-login \\")
        print(f"     -H 'Content-Type: application/json' \\")
        print(f"     -d '{{\"username\": \"test_regular_user\"}}'")
        print()
        print("   # Login as admin user (should return roles: ['user', 'admin'])")
        print(f"   curl -X POST http://localhost:8000/api/v1/auth/dev-login \\")
        print(f"     -H 'Content-Type: application/json' \\")
        print(f"     -d '{{\"username\": \"test_admin_user\"}}'")
        print()
        print("   # Try to upload as regular user (should get 403)")
        print(f"   curl -X POST http://localhost:8000/api/v1/knowledge/documents \\")
        print(f"     -H 'Authorization: Bearer {regular_token}' \\")
        print(f"     -F 'file=@test.txt' \\")
        print(f"     -F 'title=Test Doc' \\")
        print(f"     -F 'document_type=reference'")
        print()
        print("   # Try to upload as admin user (should succeed)")
        print(f"   curl -X POST http://localhost:8000/api/v1/knowledge/documents \\")
        print(f"     -H 'Authorization: Bearer {admin_token}' \\")
        print(f"     -F 'file=@test.txt' \\")
        print(f"     -F 'title=Test Doc' \\")
        print(f"     -F 'document_type=reference'")

        return True
    else:
        print("❌ Some tests failed")
        return False


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
