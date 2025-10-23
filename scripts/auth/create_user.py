#!/usr/bin/env python3
"""Create New User Account

This script creates a new user account in the FaultMaven system.
You can specify the role (regular user or admin).

Usage:
    # Interactive mode
    python scripts/auth/create_user.py

    # Command-line mode
    python scripts/auth/create_user.py --username alice --email alice@example.com --role user
    python scripts/auth/create_user.py --username bob --role admin
"""

import asyncio
import sys
import argparse
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from faultmaven.container import container


async def create_user(username: str, email: str = None, display_name: str = None, is_admin: bool = False):
    """Create a new user account"""
    print("=" * 80)
    print("Create New User Account")
    print("=" * 80)

    # Initialize container
    print("\nInitializing...")
    await container.initialize()

    # Get user store
    user_store = container.get_user_store()
    if not user_store:
        print("❌ Failed to get user store from container")
        return False

    # Check if user already exists
    print(f"\nChecking if user '{username}' exists...")
    existing_user = await user_store.get_user_by_username(username)
    if existing_user:
        print(f"❌ User '{username}' already exists!")
        print(f"   User ID: {existing_user.user_id}")
        print(f"   Email: {existing_user.email}")
        print(f"   Roles: {existing_user.roles}")
        return False

    # Create user
    print(f"\nCreating user '{username}'...")
    try:
        user = await user_store.create_user(
            username=username,
            email=email,
            display_name=display_name
        )

        # Set roles based on admin flag
        if is_admin:
            user.roles = ['user', 'admin']
        else:
            user.roles = ['user']

        # Update user with correct roles
        user = await user_store.update_user(user)

        print("✅ User created successfully!")
        print()
        print(f"User Details:")
        print(f"  User ID: {user.user_id}")
        print(f"  Username: {user.username}")
        print(f"  Email: {user.email}")
        print(f"  Display Name: {user.display_name}")
        print(f"  Roles: {user.roles}")
        print(f"  Created: {user.created_at}")
        print()
        print("Next steps:")
        print(f"  1. Login with: curl -X POST http://localhost:8000/api/v1/auth/dev-login \\")
        print(f"       -H 'Content-Type: application/json' \\")
        print(f"       -d '{{\"username\": \"{user.username}\"}}'")
        print()
        return True

    except ValueError as e:
        print(f"❌ Validation error: {e}")
        return False
    except Exception as e:
        print(f"❌ Failed to create user: {e}")
        return False


async def interactive_create():
    """Interactive user creation"""
    print("=" * 80)
    print("Create New User Account (Interactive Mode)")
    print("=" * 80)
    print()

    # Get username
    username = input("Username (required): ").strip()
    if not username:
        print("❌ Username is required")
        return False

    # Get email (optional)
    email = input("Email (optional, will auto-generate if empty): ").strip()
    if not email:
        email = None

    # Get display name (optional)
    display_name = input("Display Name (optional, will auto-generate if empty): ").strip()
    if not display_name:
        display_name = None

    # Get role
    role_input = input("Role (user/admin) [default: user]: ").strip().lower()
    is_admin = role_input == 'admin'

    print()
    print("Creating user with:")
    print(f"  Username: {username}")
    print(f"  Email: {email or '(auto-generated)'}")
    print(f"  Display Name: {display_name or '(auto-generated)'}")
    print(f"  Role: {'admin' if is_admin else 'user'}")
    print()

    confirm = input("Create this user? (yes/no): ").strip().lower()
    if confirm not in ['yes', 'y']:
        print("❌ Cancelled")
        return False

    return await create_user(username, email, display_name, is_admin)


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="Create a new FaultMaven user account")
    parser.add_argument('--username', '-u', help='Username (required)')
    parser.add_argument('--email', '-e', help='Email address (optional, auto-generated if not provided)')
    parser.add_argument('--display-name', '-d', help='Display name (optional, auto-generated if not provided)')
    parser.add_argument('--role', '-r', choices=['user', 'admin'], default='user',
                       help='User role (default: user)')
    parser.add_argument('--interactive', '-i', action='store_true',
                       help='Interactive mode (prompt for all values)')

    args = parser.parse_args()

    # Interactive mode
    if args.interactive or not args.username:
        success = asyncio.run(interactive_create())
    else:
        # Command-line mode
        is_admin = args.role == 'admin'
        success = asyncio.run(create_user(
            username=args.username,
            email=args.email,
            display_name=args.display_name,
            is_admin=is_admin
        ))

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
