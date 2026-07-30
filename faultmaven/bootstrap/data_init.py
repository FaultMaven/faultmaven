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
from pathlib import Path
from typing import Any, Optional

from faultmaven.modules.auth.contracts import PLATFORM_ADMIN_ROLE_SET

logger = logging.getLogger(__name__)

# Default admin credentials for local development
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_EMAIL = "admin@local.faultmaven"
DEFAULT_ADMIN_DISPLAY_NAME = "Local Admin"


def get_project_root() -> Path:
    """Get the project root directory.

    Determines project root using multiple strategies for deployment flexibility:
    1. PROJECT_ROOT environment variable (Docker/custom deployments)
    2. Current working directory if it contains alembic.ini or pyproject.toml
       (deployed apps — the image's WORKDIR holds both)
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


async def _create_admin_user(user_store: Any) -> Any:
    """Create the default admin user if it doesn't already exist.

    Checks by username first, then by email, to avoid duplicates.

    Args:
        user_store: Initialised user store instance

    Returns:
        Existing or newly-created user

    Raises:
        Exception: If user creation fails unexpectedly
    """
    existing = await user_store.get_user_by_username(DEFAULT_ADMIN_USERNAME)
    if existing:
        return existing

    existing_by_email = await user_store.get_user_by_email(DEFAULT_ADMIN_EMAIL)
    if existing_by_email:
        return existing_by_email

    logger.info("Creating default local admin account...")
    user = await user_store.create_user(
        username=DEFAULT_ADMIN_USERNAME,
        email=DEFAULT_ADMIN_EMAIL,
        display_name=DEFAULT_ADMIN_DISPLAY_NAME,
    )
    logger.info(
        f"Default admin account created: {user.username} ({user.email})\n"
        f"  Login via: POST /api/v1/auth/login "
        f'with {{"username": "{user.username}"}}'
    )
    return user


async def assign_operator_roles(user_store: Any, user: Any) -> tuple[Any, list[str]]:
    """Ensure the given user holds the operator roles; grants any that are missing.

    The standalone deployment's single account is legitimately both its
    organization's admin and the deployment operator (ADR-012 D9); see
    ``PLATFORM_ADMIN_ROLE_SET`` for why the two are granted together.

    This runs on EVERY startup, not only at creation, so that upgrading a
    deployment whose account predates the operator/org role split re-grants the
    operator role without manual intervention. The consequence is that the
    bootstrap account cannot be demoted durably — the next restart restores it.
    That is intended for *this* account (a standalone deployment with no
    operator is unusable), and ``fm-demote-platform-admin`` says so when
    aimed at it. Every other account demotes permanently.

    **The single writer of this grant.** ``fm-promote-platform-admin`` calls it
    too rather than reimplementing the same three lines: an operator promoted by
    hand and one re-granted at startup must end up with identical roles, and two
    copies of "which roles make an operator" is exactly how they drift.

    Args:
        user_store: Initialised user store instance
        user: DevUser to check and update

    Returns:
        ``(user, granted)`` — the user (updated if roles were granted, unchanged
        otherwise) and the list of roles this call added, empty when it was a
        no-op. Callers that report to a human use ``granted``; startup ignores it.
    """
    missing = [r for r in PLATFORM_ADMIN_ROLE_SET if r not in (user.roles or [])]
    if not missing:
        return user, []

    logger.info(f"User '{user.username}' missing operator roles {missing} — granting")
    user.roles = list(user.roles or []) + missing
    user = await user_store.update_user(user)
    logger.info(f"Operator roles granted to '{user.username}': {user.roles}")
    return user, missing


async def ensure_default_admin_exists(container: Any) -> Optional[Any]:
    """Ensure the default local admin account exists and has the admin role.

    Orchestrates _create_admin_user() + assign_operator_roles() so each
    step is independently testable.

    Args:
        container: DI container with initialized services

    Returns:
        Newly-created user if this was a first-run, None otherwise.

    Notes:
        - Idempotent: safe to call on every startup
        - Only creates the user in local/dev mode (no password required)
    """
    try:
        logger.info("Checking for default admin user...")

        user_store = container.get_user_store()
        if not user_store:
            logger.warning("User store not available — skipping admin creation")
            return None

        logger.info(f"User store type: {type(user_store).__name__}")

        was_new = not bool(
            await user_store.get_user_by_username(DEFAULT_ADMIN_USERNAME)
            or await user_store.get_user_by_email(DEFAULT_ADMIN_EMAIL)
        )

        user = await _create_admin_user(user_store)
        await assign_operator_roles(user_store, user)

        if was_new:
            return user

        logger.info(f"Default admin user check complete: '{DEFAULT_ADMIN_USERNAME}'")
        return None

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

    # Step 2: (removed) Built-in runbooks are no longer copied to
    # data/knowledge/global. They ship pre-chunked + pre-embedded in the KB pack
    # (resources/knowledge/pack, or KB_PACK_DIR) and are ingested directly into
    # knowledge_items + ChromaDB by the KB bootstrap. data/knowledge/ remains the
    # workspace for authored/converted runbooks and the /knowledge/scan flow.

    # Step 3: Run database migrations (only when this app owns its schema).
    # When an external migration Job owns schema (the K8s deployment) the app
    # connects as a non-owner, no-DDL role, so a startup `alembic upgrade` that
    # needs DDL would be permission-denied and crash-loop the pod. Gate it on
    # RUN_STARTUP_MIGRATIONS (default True for self-contained docker/local).
    from faultmaven.config.settings import get_settings

    if get_settings().run_startup_migrations:
        # Note: This uses synchronous Alembic - runs in the event loop's executor
        run_alembic_migrations()
    else:
        logger.info(
            "Skipping startup Alembic migrations (RUN_STARTUP_MIGRATIONS=false); "
            "schema is owned by an external migration Job."
        )

    # Step 4: Create default admin user if needed
    # Moved to startup.py (Step 3) to ensure it runs after the default enterprise is created
    # otherwise it fails with a foreign key constraint violation.

    logger.info("Data layer initialization complete")
