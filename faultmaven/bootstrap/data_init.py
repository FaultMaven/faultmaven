"""Data Directory and Database Initialization (Bootstrap).

Handles first-run initialization tasks:
1. Create data directories (data/chroma-kb, data/chroma-evidence, data/evidence)
2. Copy built-in runbooks to data/knowledge/global/ if empty
3. Run Alembic migrations to ensure schema is up-to-date
4. Create default local admin user if database is empty

This runs at application startup to ensure a smooth out-of-the-box experience
for local deployments without requiring manual setup steps.

Design Notes:
    - Idempotent: Safe to call multiple times
    - Silent on subsequent runs (only logs on first-time setup)
    - Does not modify existing data
    - Uses absolute paths based on project root for deployment flexibility
"""

import logging
import os
import shutil
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Default admin credentials for local development
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_EMAIL = "admin@local.faultmaven"
DEFAULT_ADMIN_DISPLAY_NAME = "Local Admin"


def get_project_root() -> Path:
    """Get the project root directory.

    Determines project root using multiple strategies for deployment flexibility:
    1. PROJECT_ROOT environment variable (Docker/custom deployments)
    2. Current working directory if it contains alembic.ini (deployed apps)
    3. Relative to this file (development mode)

    Returns:
        Path to project root directory
    """
    # 1. Environment variable override
    env_root = os.environ.get("PROJECT_ROOT")
    if env_root:
        return Path(env_root)

    # 2. Current working directory (if it looks like project root)
    cwd = Path.cwd()
    if (cwd / "alembic.ini").exists() or (cwd / "pyproject.toml").exists():
        return cwd

    # 3. Relative to this file (faultmaven/bootstrap/data_init.py -> project root)
    return Path(__file__).parent.parent.parent


def ensure_data_directories() -> None:
    """Create data directories if they don't exist.

    Creates (relative to project root):
        - data/                  (root data directory)
        - data/chroma-kb/        (ChromaDB KB vector storage — permanent)
        - data/chroma-evidence/  (ChromaDB case evidence vector storage — ephemeral)
        - data/evidence/         (uploaded evidence files)
        - data/knowledge/        (runbook source files by scope)

    These directories are gitignored and store runtime data.
    Uses absolute paths based on project root for deployment flexibility.
    """
    project_root = get_project_root()

    directories = [
        project_root / "data",
        project_root / "data" / "chroma-kb",
        project_root / "data" / "chroma-evidence",
        project_root / "data" / "evidence",
        project_root / "data" / "knowledge",
        project_root / "data" / "knowledge" / "global",
    ]

    for path in directories:
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            logger.info(f"Created data directory: {path}")


def seed_builtin_runbooks() -> int:
    """Copy built-in runbooks to data/knowledge/global/ if the directory is empty.

    Ships 59 runbooks from resources/knowledge/builtin/ (checked into the repo)
    into the runtime data directory. Only copies if the target directory has no
    .md files, so user modifications are preserved on subsequent startups.

    Returns:
        Number of runbooks copied (0 if already populated).
    """
    project_root = get_project_root()
    builtin_dir = project_root / "resources" / "knowledge" / "builtin"
    target_dir = project_root / "data" / "knowledge" / "global"

    if not builtin_dir.exists():
        logger.debug("No built-in runbooks directory found — skipping seed")
        return 0

    # Check all of data/knowledge/ for any .md files (not just global/)
    # If personal runbooks exist but global is empty, user chose not to have global ones
    knowledge_root = project_root / "data" / "knowledge"
    sources_dir = knowledge_root / "sources"
    existing = []
    if knowledge_root.exists():
        existing = [
            p for p in knowledge_root.rglob("*.md") if not p.is_relative_to(sources_dir)
        ]
    if existing:
        logger.debug(
            f"Knowledge directory has {len(existing)} runbooks — skipping seed"
        )
        return 0

    # Copy built-in runbooks preserving subdirectory structure
    target_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src_file in sorted(builtin_dir.rglob("*.md")):
        relative = src_file.relative_to(builtin_dir)
        dest = target_dir / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dest)
        copied += 1

    if copied:
        logger.info(f"Seeded {copied} built-in runbooks into data/knowledge/global/")

    return copied


def run_alembic_migrations() -> bool:
    """Run Alembic migrations using subprocess.

    Ensures the database schema is up-to-date by running
    'alembic upgrade head' in a subprocess.

    Returns:
        True if migrations ran successfully, False otherwise.

    Notes:
        - Uses subprocess to avoid blocking issues with async startup
        - Safe to run multiple times (Alembic tracks applied migrations)
        - Creates the database file if it doesn't exist (SQLite)
        - Searches for alembic.ini in multiple locations for deployment flexibility
    """
    import subprocess
    import sys

    try:
        # Find alembic.ini - check multiple locations for deployment flexibility
        alembic_ini = None
        search_paths = []

        # 1. Environment variable override (for Docker/custom deployments)
        env_path = os.environ.get("ALEMBIC_CONFIG")
        if env_path:
            search_paths.append(Path(env_path))

        # 2. Project root (determined by get_project_root helper)
        project_root = get_project_root()
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

        # Run migrations via subprocess to avoid blocking in async context
        # Using the same Python interpreter ensures we use the right virtualenv
        logger.info(f"Running Alembic migrations (config: {alembic_ini})...")
        logger.info(f"Working directory: {project_root}")
        logger.info(f"Python executable: {sys.executable}")

        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=60,  # 60 second timeout
        )

        if result.returncode == 0:
            logger.info("✅ Alembic migrations complete")
            # Log stdout for visibility
            if result.stdout:
                logger.debug(f"Migration output: {result.stdout}")
            return True
        else:
            # Enhanced error logging
            logger.error(
                f"❌ Alembic migration failed with exit code {result.returncode}"
            )
            logger.error(f"STDERR: {result.stderr}")
            if result.stdout:
                logger.error(f"STDOUT: {result.stdout}")
            raise RuntimeError(
                f"Alembic migration failed with exit code {result.returncode}: {result.stderr}"
            )

    except subprocess.TimeoutExpired:
        logger.error("❌ Alembic migration timed out after 60 seconds")
        raise RuntimeError("Alembic migration timed out after 60 seconds")
    except FileNotFoundError as e:
        # This one might be optional if dev env without alembic, but in prod it should fail
        logger.warning(
            f"⚠️ Alembic command not found - assuming manual schema management: {e}"
        )
        return False
    except Exception as e:
        logger.error(f"❌ Alembic migration failed with exception: {e}", exc_info=True)
        raise RuntimeError(f"Alembic migration failed: {e}") from e


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
        logger.info("Checking for default admin user...")

        # Get user store from container
        user_store = container.get_user_store()
        if not user_store:
            logger.warning("User store not available - skipping admin creation")
            return None

        logger.info(f"User store type: {type(user_store).__name__}")

        # Check if any users exist
        existing_user = await user_store.get_user_by_username(DEFAULT_ADMIN_USERNAME)
        if existing_user:
            if "admin" not in (existing_user.roles or []):
                logger.info(
                    f"Admin user '{DEFAULT_ADMIN_USERNAME}' exists but lacks admin role — fixing"
                )
                existing_user.roles = list(existing_user.roles or []) + ["admin"]
                await user_store.update_user(existing_user)
                logger.info(f"Admin role added to '{DEFAULT_ADMIN_USERNAME}'")
            else:
                logger.info(f"Admin user '{DEFAULT_ADMIN_USERNAME}' already exists")
            return None

        # Also check by email
        existing_by_email = await user_store.get_user_by_email(DEFAULT_ADMIN_EMAIL)
        if existing_by_email:
            logger.info(f"User with email '{DEFAULT_ADMIN_EMAIL}' already exists")
            return None

        # Create default admin user
        logger.info("Creating default local admin account...")
        user = await user_store.create_user(
            username=DEFAULT_ADMIN_USERNAME,
            email=DEFAULT_ADMIN_EMAIL,
            display_name=DEFAULT_ADMIN_DISPLAY_NAME,
        )
        logger.info(f"User created with ID: {user.user_id}")

        # Set admin roles
        user.roles = ["user", "admin"]
        user = await user_store.update_user(user)
        logger.info(f"Admin roles assigned: {user.roles}")

        logger.info(f"Default admin account created: {user.username} ({user.email})")
        logger.info(
            "  Login via: POST /api/v1/auth/dev-login "
            f'with {{"username": "{user.username}"}}'
        )
        return user

    except Exception as e:
        logger.warning(f"Could not create default admin: {e}", exc_info=True)
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
    logger.info("Initializing data layer...")

    # Step 1: Ensure data directories exist
    ensure_data_directories()

    # Step 2: Seed built-in runbooks if global KB directory is empty
    seed_builtin_runbooks()

    # Step 3: Run database migrations
    # Note: This uses synchronous Alembic - runs in the event loop's executor
    run_alembic_migrations()

    # Step 4: Create default admin user if needed
    # Moved to startup.py (Step 3) to ensure it runs after the default enterprise is created
    # otherwise it fails with a foreign key constraint violation.

    logger.info("Data layer initialization complete")
