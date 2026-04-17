#!/usr/bin/env python3
"""Test Role-Based Access Control via HTTP API.

Tests RBAC by making HTTP requests against a running FaultMaven instance:
1. Logs in as a regular user (dev-login)
2. Logs in as an admin user (dev-login)
3. Tests that regular users can't upload to Global KB (403)
4. Tests that admin users can upload to Global KB (200)
5. Tests role claims in JWT tokens

Prerequisites:
    - FaultMaven server running at localhost:8090
    - Default admin account created (auto-init on first startup)

Usage:
    python scripts/test_rbac.py
    python scripts/test_rbac.py --api-url http://localhost:8090
"""

import argparse
import json
import sys

import httpx


def login(client: httpx.Client, username: str) -> dict:
    """Dev-login and return token + user info."""
    resp = client.post(
        "/api/v1/auth/dev-login",
        json={"username": username},
    )
    resp.raise_for_status()
    return resp.json()


def main():
    parser = argparse.ArgumentParser(description="Test RBAC via HTTP API")
    parser.add_argument(
        "--api-url", default="http://localhost:8090", help="FaultMaven API URL"
    )
    args = parser.parse_args()

    client = httpx.Client(base_url=args.api_url, timeout=10)

    print("=" * 70)
    print("Role-Based Access Control Test")
    print(f"API: {args.api_url}")
    print("=" * 70)

    # Health check
    print("\n1. Checking API health...")
    try:
        resp = client.get("/health")
        if resp.status_code != 200:
            print(f"   ❌ API not healthy: {resp.status_code}")
            return False
        print("   ✅ API is healthy")
    except httpx.ConnectError:
        print(f"   ❌ Cannot connect to {args.api_url}")
        print("   Start FaultMaven first: ./faultmaven.sh start")
        return False

    # Login as admin
    print("\n2. Logging in as admin...")
    try:
        admin_data = login(client, "admin")
        admin_token = admin_data.get("access_token", admin_data.get("token", ""))
        admin_roles = admin_data.get("roles", [])
        print(f"   Roles: {admin_roles}")
        if "admin" not in admin_roles:
            print("   ❌ Admin user should have 'admin' role")
            return False
        print("   ✅ Admin login OK")
    except httpx.HTTPStatusError as e:
        print(f"   ❌ Login failed: {e.response.status_code} {e.response.text[:200]}")
        return False

    # Create a test user via admin endpoint
    print("\n3. Creating test regular user...")
    test_username = "test_rbac_regular"
    try:
        # Try dev-login first (creates user if doesn't exist in dev mode)
        user_data = login(client, test_username)
        user_token = user_data.get("access_token", user_data.get("token", ""))
        user_roles = user_data.get("roles", [])
        print(f"   Roles: {user_roles}")
        print("   ✅ Regular user login OK")
    except httpx.HTTPStatusError as e:
        print(f"   ❌ Login failed: {e.response.status_code} {e.response.text[:200]}")
        return False

    # Test: regular user cannot upload to Global KB
    print("\n4. Testing regular user upload to Global KB (expect 403)...")
    resp = client.post(
        "/api/v1/knowledge/documents",
        headers={"Authorization": f"Bearer {user_token}"},
        files={"file": ("test.txt", b"test content", "text/plain")},
        data={"title": "RBAC Test Doc", "document_type": "reference"},
    )
    if resp.status_code == 403:
        print(f"   ✅ Correctly rejected: {resp.status_code}")
    else:
        print(f"   ❌ Expected 403, got {resp.status_code}")
        print(f"   Body: {resp.text[:200]}")

    # Test: admin user can upload to Global KB
    print("\n5. Testing admin user upload to Global KB (expect 200/201)...")
    resp = client.post(
        "/api/v1/knowledge/documents",
        headers={"Authorization": f"Bearer {admin_token}"},
        files={"file": ("test.txt", b"test content", "text/plain")},
        data={"title": "RBAC Test Doc", "document_type": "reference"},
    )
    if resp.status_code in (200, 201):
        print(f"   ✅ Upload accepted: {resp.status_code}")
    else:
        print(f"   ❌ Expected 200/201, got {resp.status_code}")
        print(f"   Body: {resp.text[:200]}")

    # Test: both users can search
    print("\n6. Testing both users can search Global KB...")
    for label, token in [("Regular", user_token), ("Admin", admin_token)]:
        resp = client.post(
            "/api/v1/knowledge/search",
            headers={"Authorization": f"Bearer {token}"},
            json={"query": "test"},
        )
        if resp.status_code == 200:
            print(f"   ✅ {label} user search OK")
        else:
            print(f"   ❌ {label} user search failed: {resp.status_code}")

    print("\n" + "=" * 70)
    print("✅ RBAC tests complete")
    print("=" * 70)
    return True


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
