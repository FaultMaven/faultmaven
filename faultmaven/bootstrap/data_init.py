"""Data Directory and Database Initialization (Bootstrap).

Handles first-run initialization tasks:
1. Create data directories (./data/chroma, ./data/evidence)
2. Run Alembic migrations to ensure schema is up-to-date
3. Create default local admin user if database is empty

This runs at application startup to ensure a smooth out-of-the-box experience
for local deployments without requiring manual setup steps.

Design Notes:
    - Idempotent: Safe to call multiple times
    - Silent on subsequent runs (only logs on first-time setup)
    - Does not modify existing data
"""

import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Default admin credentials for local development
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_EMAIL = "admin@local.faultmaven"
DEFAULT_ADMIN_DISPLAY_NAME = "Local Admin"


def ensure_data_directories() -> None:
    """Create data directories if they don't exist.

    Creates:
        - ./data/              (root data directory)
        - ./data/chroma/       (ChromaDB vector storage)
        - ./data/evidence/     (uploaded evidence files)

    These directories are gitignored and store runtime data.
    """
    directories = [
        "./data",
        "./data/chroma",
        "./data/evidence",
    ]

    for directory in directories:
        path = Path(directory)
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            logger.info(f"Created data directory: {directory}")


def run_alembic_migrations() -> bool:
    """Run Alembic migrations programmatically.

    Ensures the database schema is up-to-date by running
    'alembic upgrade head' programmatically.

    Returns:
        True if migrations ran successfully, False otherwise.

    Notes:
        - Uses alembic.ini configuration
        - Safe to run multiple times (Alembic tracks applied migrations)
        - Creates the database file if it doesn't exist (SQLite)
        - Searches for alembic.ini in multiple locations for deployment flexibility
    """
    try:
        from alembic import command
        from alembic.config import Config

        # Find alembic.ini - check multiple locations for deployment flexibility
        # Priority: 1) env var, 2) current working dir, 3) relative to this file
        alembic_ini = None
        search_paths = []

        # 1. Environment variable override (for Docker/custom deployments)
        env_path = os.environ.get("ALEMBIC_CONFIG")
        if env_path:
            search_paths.append(Path(env_path))

        # 2. Current working directory (typical for deployed apps)
        search_paths.append(Path.cwd() / "alembic.ini")

        # 3. Relative to this file (development mode)
        project_root = Path(__file__).parent.parent.parent
        search_paths.append(project_root / "alembic.ini")

        for path in search_paths:
            if path.exists():
                alembic_ini = path
                break

        if not alembic_ini:
            logger.warning(
                f"alembic.ini not found in: {[str(p) for p in search_paths]}"
            )
            return False

        # Configure Alembic
        alembic_cfg = Config(str(alembic_ini))

        # Run migrations
        logger.debug(f"Running Alembic migrations (config: {alembic_ini})...")
        command.upgrade(alembic_cfg, "head")
        logger.debug("Alembic migrations complete")
        return True

    except ImportError:
        logger.warning("Alembic not installed - skipping migrations")
        return False
    except Exception as e:
        logger.error(f"Alembic migration failed: {e}")
        return False


async def ensure_default_admin_exists(container: Any) -> Optional[Any]:
    """Create default admin user if no users exist.

    For local development, creates a default admin account so users
    can log in immediately without running setup scripts.

    Args:
        container: DI container with initialized services

    Returns:
        Created user if new, None if user already exists or on error.

    Default Credentials:
        - Username: admin
        - Email: admin@local.faultmaven
        - Roles: ['user', 'admin']

    Notes:
        - Only creates user if database is empty (no users exist)
        - Uses dev-login mode (no password required for local auth)
        - Safe to call multiple times (idempotent)
    """
    try:
        # Get user store from container
        user_store = container.get_user_store()
        if not user_store:
            logger.debug("User store not available - skipping admin creation")
            return None

        # Check if any users exist
        existing_user = await user_store.get_user_by_username(DEFAULT_ADMIN_USERNAME)
        if existing_user:
            logger.debug(f"Admin user '{DEFAULT_ADMIN_USERNAME}' already exists")
            return None

        # Also check by email
        existing_by_email = await user_store.get_user_by_email(DEFAULT_ADMIN_EMAIL)
        if existing_by_email:
            logger.debug(f"User with email '{DEFAULT_ADMIN_EMAIL}' already exists")
            return None

        # Create default admin user
        logger.info("Creating default local admin account...")
        user = await user_store.create_user(
            username=DEFAULT_ADMIN_USERNAME,
            email=DEFAULT_ADMIN_EMAIL,
            display_name=DEFAULT_ADMIN_DISPLAY_NAME,
        )

        # Set admin roles
        user.roles = ["user", "admin"]
        user = await user_store.update_user(user)

        logger.info(
            f"Default admin account created: {user.username} ({user.email})"
        )
        logger.info(
            "  Login via: POST /api/v1/auth/dev-login "
            f'with {{"username": "{user.username}"}}'
        )
        return user

    except Exception as e:
        logger.warning(f"Could not create default admin: {e}")
        return None


async def initialize_data_layer(container: Any) -> None:
    """Initialize the data layer on application startup.

    Orchestrates all initialization tasks:
    1. Create data directories
    2. Run database migrations
    3. Create default admin user

    Args:
        container: DI container with initialized services

    This function is idempotent and safe to call on every startup.
    It only performs setup actions that are needed.
    """
    logger.debug("Initializing data layer...")

    # Step 1: Ensure data directories exist
    ensure_data_directories()

    # Step 2: Run database migrations
    # Note: This uses synchronous Alembic - runs in the event loop's executor
    run_alembic_migrations()

    # Step 3: Create default admin user if needed
    await ensure_default_admin_exists(container)

    logger.debug("Data layer initialization complete")
