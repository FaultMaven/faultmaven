#!/usr/bin/env python3
"""
Built-in Accounts Creation Script

Purpose: Create essential built-in accounts for FaultMaven development and testing

This script creates three built-in accounts:
- admin@faultmaven.ai: Administrative access
- dev@faultmaven.ai: Developer testing account
- test@faultmaven.ai: Automated testing account

Features:
- Idempotent: Safe to run multiple times
- Uses the registration API endpoint
- Proper error handling and logging
- Returns exit codes for CI/CD integration

Usage:
    python scripts/create_builtin_accounts.py [--server-url http://localhost:8000]

Environment Variables:
    FAULTMAVEN_SERVER_URL: Override default server URL

Exit Codes:
    0: Success - all accounts created or already exist
    1: Error - failed to create one or more accounts
    2: Server unreachable
"""

import sys
import json
import argparse
import logging
from typing import Dict, List, Tuple
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Built-in accounts configuration
BUILTIN_ACCOUNTS = [
    {
        "username": "admin@faultmaven.ai",
        "email": "admin@faultmaven.ai",
        "display_name": "System Administrator",
        "description": "Administrative access account"
    },
    {
        "username": "dev@faultmaven.ai",
        "email": "dev@faultmaven.ai",
        "display_name": "Developer",
        "description": "Developer testing account"
    },
    {
        "username": "test@faultmaven.ai",
        "email": "test@faultmaven.ai",
        "display_name": "Test User",
        "description": "Automated testing account"
    }
]

class AccountCreationError(Exception):
    """Custom exception for account creation failures"""
    pass

def create_session() -> requests.Session:
    """Create HTTP session with retry strategy"""
    session = requests.Session()

    # Handle different versions of urllib3
    try:
        retry_strategy = Retry(
            total=3,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "PUT", "DELETE", "OPTIONS", "TRACE", "POST"],
            backoff_factor=1
        )
    except TypeError:
        # Fallback for older urllib3 versions
        retry_strategy = Retry(
            total=3,
            status_forcelist=[429, 500, 502, 503, 504],
            method_whitelist=["HEAD", "GET", "PUT", "DELETE", "OPTIONS", "TRACE", "POST"],
            backoff_factor=1
        )

    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    return session

def check_server_health(server_url: str, session: requests.Session) -> bool:
    """Check if FaultMaven server is reachable"""
    try:
        health_url = f"{server_url}/health"
        response = session.get(health_url, timeout=10)

        if response.status_code == 200:
            logger.info(f"Server is healthy at {server_url}")
            return True
        else:
            logger.warning(f"Server health check returned {response.status_code}")
            return False

    except requests.exceptions.RequestException as e:
        logger.error(f"Cannot reach server at {server_url}: {e}")
        return False

def create_account(account: Dict, server_url: str, session: requests.Session) -> Tuple[bool, str]:
    """
    Create a single account using the registration API

    Returns:
        (success: bool, message: str)
    """
    register_url = f"{server_url}/api/v1/auth/dev-register"

    try:
        # Prepare registration payload
        payload = {
            "username": account["username"],
            "email": account["email"],
            "display_name": account["display_name"]
        }

        response = session.post(
            register_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30
        )

        if response.status_code == 201:
            # Successfully created
            data = response.json()
            user_id = data.get("user", {}).get("user_id", "unknown")
            logger.info(f"✅ Created account: {account['username']} (ID: {user_id})")
            return True, f"Created {account['username']}"

        elif response.status_code == 409:
            # Account already exists - this is OK for idempotent behavior
            logger.info(f"✅ Account already exists: {account['username']}")
            return True, f"Already exists: {account['username']}"

        else:
            # Unexpected error
            error_msg = response.text
            try:
                error_data = response.json()
                error_msg = error_data.get("detail", error_msg)
            except:
                pass

            logger.error(f"❌ Failed to create {account['username']}: {response.status_code} - {error_msg}")
            return False, f"Failed {account['username']}: {error_msg}"

    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Network error creating {account['username']}: {e}")
        return False, f"Network error for {account['username']}: {str(e)}"

def create_builtin_accounts(server_url: str) -> int:
    """
    Create all built-in accounts

    Returns:
        Exit code (0 for success, 1 for partial failure, 2 for server error)
    """
    logger.info("🚀 Starting built-in accounts creation...")
    logger.info(f"Server URL: {server_url}")

    session = create_session()

    # Check server health first
    if not check_server_health(server_url, session):
        logger.error("❌ Server is not reachable. Please ensure FaultMaven is running.")
        return 2

    # Create accounts
    results = []
    success_count = 0

    for account in BUILTIN_ACCOUNTS:
        logger.info(f"Processing account: {account['username']} ({account['description']})")
        success, message = create_account(account, server_url, session)
        results.append((account["username"], success, message))

        if success:
            success_count += 1

    # Report results
    logger.info("\n" + "="*60)
    logger.info("BUILT-IN ACCOUNTS CREATION SUMMARY")
    logger.info("="*60)

    for username, success, message in results:
        status = "✅ SUCCESS" if success else "❌ FAILED"
        logger.info(f"{status}: {message}")

    logger.info(f"\nTotal accounts processed: {len(BUILTIN_ACCOUNTS)}")
    logger.info(f"Successful: {success_count}")
    logger.info(f"Failed: {len(BUILTIN_ACCOUNTS) - success_count}")

    if success_count == len(BUILTIN_ACCOUNTS):
        logger.info("\n🎉 All built-in accounts are ready!")
        return 0
    elif success_count > 0:
        logger.warning("\n⚠️  Some accounts failed to create.")
        return 1
    else:
        logger.error("\n💥 Failed to create any accounts.")
        return 1

def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="Create built-in accounts for FaultMaven",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )

    parser.add_argument(
        "--server-url",
        default="http://localhost:8000",
        help="FaultMaven server URL (default: http://localhost:8000)"
    )

    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging"
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Allow override from environment variable
    import os
    server_url = os.getenv("FAULTMAVEN_SERVER_URL", args.server_url)

    try:
        exit_code = create_builtin_accounts(server_url)
        sys.exit(exit_code)

    except KeyboardInterrupt:
        logger.info("\n⚠️  Operation cancelled by user")
        sys.exit(1)

    except Exception as e:
        logger.error(f"💥 Unexpected error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()