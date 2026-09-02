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
    - Every data directory is resolved from the setting its consumer reads,
      falling back to the project root only where no setting was configured
      (see ``resolve_data_dir``). A second, project-root-derived spelling of a
      configured path does not merely miss the store — it CREATES an empty one
      beside it, which is what made ``fm-reset-kb`` wipe a decoy and report
      success (fm#936).
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


class UnusableDataDirError(ValueError):
    """A data-directory knob is set to something no consumer can open.

    Raised rather than substituted, because substituting is how fm#936 works.
    A blank ``CHROMADB_KB_PERSIST_DIR=`` is the reachable case: pydantic-settings
    runs with ``env_ignore_empty`` off, so an unpopulated ConfigMap key or a
    trailing ``=`` in a .env arrives as a SET field holding ``""``. The
    consumers do not fall back either — ``getattr(db, "chromadb_kb_persist_dir",
    "./data/chroma-kb")`` returns ``""``, because the attribute exists, and
    ``create_persistent_client("")`` dies in ``os.makedirs("")`` with
    ``FileNotFoundError`` (measured). A resolver that quietly answered
    ``./data/chroma-kb`` there would have the bootstrap create a tree the
    container cannot open, log that the default is in effect, and hand
    ``fm-reset-kb`` a decoy — the exact shape of the bug, rebuilt inside its
    own fix.

    Callers decide what to do with it: startup logs and skips that one
    directory (the deployment is broken, but it is not this function's job to
    kill the process), and a destructive command refuses.
    """


def _absolute(path: Path) -> Path:
    """The cwd-anchored absolute form of ``path``, symlinks left alone.

    Anchoring is the point: a relative configured value means "relative to this
    process's working directory", and that is only useful to a human — or to a
    log line an operator compares against the server's — once it is spelled
    out. ``fm-reset-kb`` printing ``data/chroma-kb`` tells nobody which tree it
    is about to delete.

    ``.absolute()``, never ``.resolve()``: resolving would follow symlinks, and
    the identity that matters here is the path a caller was configured with and
    will operate on. A symlinked persist directory must still read back as the
    link (``shutil.rmtree`` refuses symlinks, and the operator needs to see the
    name they configured, not its target).
    """
    return path if path.is_absolute() else Path.cwd() / path


def resolve_data_dir(section: Any, field: str) -> Path:
    """Resolve a data directory EXACTLY as its consumer resolves it.

    Every one of these directories has a live settings knob, and every consumer
    hands that knob's raw string straight to the filesystem —
    ``create_persistent_client`` does ``os.makedirs(path)``,
    ``FilesystemStorageBackend`` takes ``storage_root`` as given. So a relative
    value is relative to the **process's working directory** there, and must be
    read the same way here.

    Deriving the same directory a second way — anchoring it on
    ``get_project_root()`` — is correct only while the two happen to coincide,
    and when they stop coinciding nothing says so: the bootstrap creates an
    EMPTY directory at the project root that no server ever writes to, and that
    empty directory then reads as "the store is here" to anything that looks
    (fm#936). ``fm-reset-kb`` looked, wiped the decoy, printed "Removed …" and
    exited 0 while the real store kept its vectors and the SQL rows were gone.

    There is therefore **no project-root fallback**, not even for the unset
    case: the unset case is `./data/chroma-kb`, which the consumer reads
    cwd-relative like any other value, and a fallback would reintroduce the
    second spelling for exactly the configuration nobody overrides. In every
    shipped configuration the two agree anyway — the image's ``WORKDIR`` is
    ``/app`` and the dev scripts ``cd`` to the checkout, and the ``PROJECT_ROOT``
    environment variable is set by none of them — so this changes nothing that
    ships and closes the case where they diverge.

    ``get_project_root()`` keeps its job: locating **repo-layout artifacts**
    (``alembic.ini``, the bundled KB pack), which really do live at a fixed
    place in the tree. It is not a source of truth for runtime data paths.

    Args:
        section: The settings sub-model holding the field (``settings.database``,
            ``settings.evidence_storage``). A real settings object, not a mock —
            this reads the field's declared default off the model class.
        field: The field name on that sub-model.
    """
    configured = str(getattr(section, field) or "")
    if configured.strip():
        # Stripped only to decide EMPTINESS — the value itself is passed
        # through unstripped, because the consumer does not strip it either.
        # ``CHROMADB_KB_PERSIST_DIR=" /data/kb "`` names a directory whose
        # components carry spaces, and a caller that tidied it would resolve a
        # different tree from the one the server opens: this bug, one layer in.
        return _absolute(Path(configured))

    # BLANK, not absent, and therefore NOT the default either — see
    # UnusableDataDirError. ``Path("")`` is ``Path(".")``, the working
    # directory, which is what ``fm-reset-kb`` would then ``rmtree``.
    # Whitespace-only is the same thing wearing a hat.
    # Named as the ENVIRONMENT VARIABLE, not the pydantic field. These models
    # carry an empty ``env_prefix``, so the variable is the field name upper-
    # cased — and ``chromadb_kb_persist_dir is set but empty`` sends an
    # operator looking for something they never typed.
    raise UnusableDataDirError(
        f"{field.upper()} is set but empty. An empty path resolves to the "
        "working directory, not to a store, and the consumers do not fall back "
        "to the documented default either (they read the attribute, which "
        "exists and is blank). Unset the variable to get the default, or give "
        "it a path."
    )


def resolve_kb_chroma_dir(settings: Any) -> Path:
    """The local ChromaDB KB tree the server opens (``CHROMADB_KB_PERSIST_DIR``).

    The single spelling shared by the startup bootstrap and ``fm-reset-kb``.
    Says nothing about whether a local tree is the store at all — under an
    external ChromaDB it is not; ask
    :func:`~faultmaven.config.deployment_coherence.is_remote_chroma_configured`
    first.
    """
    return resolve_data_dir(settings.database, "chromadb_kb_persist_dir")


def resolve_evidence_chroma_dir(settings: Any) -> Path:
    """The local ChromaDB evidence tree the server opens (``CHROMADB_EVIDENCE_PERSIST_DIR``)."""
    return resolve_data_dir(settings.database, "chromadb_evidence_persist_dir")


def resolve_evidence_storage_dir(settings: Any) -> Path:
    """The evidence-file tree the storage factory opens (``EVIDENCE_STORAGE_ROOT``)."""
    return resolve_data_dir(settings.evidence_storage, "evidence_storage_root")


def _extend(directories: list, resolver: Any, settings: Any) -> None:
    """Append ``resolver(settings)``, or log why that directory has no path.

    A knob set to something no consumer can open (see
    :class:`UnusableDataDirError`) is a misconfiguration the deployment will
    hit on its own, loudly, when the consumer opens it. Startup's job is to
    name it — not to invent a location that nothing will read, and not to kill
    the process over one directory.
    """
    try:
        directories.append(resolver(settings))
    except UnusableDataDirError as exc:
        logger.error("Not creating a data directory: %s", exc)


def ensure_data_directories() -> None:
    """Create the data directories THIS deployment actually reads and writes.

    Every directory here is resolved from the setting the corresponding
    consumer reads — not from the project root — so the bootstrap cannot
    manufacture an empty look-alike beside the real store (fm#936; see
    :func:`resolve_data_dir`).

    Creates:
        - ``data/``                          (shared parent, cwd-relative like
          the SQLite DSN default ``sqlite+aiosqlite:///./data/faultmaven.db``
          that needs it)
        - ``CHROMADB_KB_PERSIST_DIR``        (ChromaDB KB vectors — permanent)
        - ``CHROMADB_EVIDENCE_PERSIST_DIR``  (ChromaDB case evidence — ephemeral)
        - ``EVIDENCE_STORAGE_ROOT``          (uploaded evidence files)
        - ``data/knowledge/`` + ``global/``  (runbook source files, from
          :func:`~faultmaven.utils.runbook_id.knowledge_root` — the anchor the
          scan and upload paths resolve against, which is relative on purpose)

    The two ChromaDB trees are created ONLY when this deployment opens a local
    store. Under an external ``CHROMADB_URL`` the server never touches a local
    tree, so creating one is precisely the decoy #936 is about — and nothing
    needs the pre-creation in any case: ``create_persistent_client`` does its
    own ``makedirs`` on the path it opens.

    That gate is :func:`is_external_chroma_configured`, deliberately NOT the
    wider :func:`is_remote_chroma_configured`: the container's client factory
    dispatches on the narrow one, so under the host-only opt-in
    (``CHROMADB_HOST`` set, ``CHROMADB_URL`` unset) the container really does
    open a local tree and this must create it. Only the ingester goes remote
    there — an inconsistency this function has to accommodate rather than paper
    over.

    Takes no settings override on purpose: the whole point is that it reads
    the SAME source the server reads, and a settings parameter is a second
    source — one that tests would then exercise instead of the real path, and
    that a caller could hand a value diverging from the running configuration.

    Idempotent: safe to call on every startup.
    """
    from faultmaven.config.deployment_coherence import is_external_chroma_configured
    from faultmaven.config.settings import get_settings
    from faultmaven.utils.runbook_id import knowledge_root

    settings = get_settings()

    # The one literal here, and the one knob-derived path this does NOT resolve
    # from its setting. It exists for the SQLite default
    # (``sqlite+aiosqlite:///./data/faultmaven.db``), which is cwd-relative, so
    # the literal is spelled the way that DSN spells it. Deriving it from
    # ``DATABASE_URL`` would mean parsing a DSN whose non-SQLite forms name no
    # directory at all, for a directory that costs nothing when unneeded and
    # that `mkdir(parents=True)` on the trees below already creates in the
    # default configuration. If that changes — a knob for the database file's
    # location, say — this becomes the next instance of fm#936 and should be
    # resolved like the rest.
    directories = [_absolute(Path("data"))]

    if is_external_chroma_configured(settings):
        logger.info(
            "External ChromaDB is configured — not creating local vector "
            "directories (an empty local tree beside an external store is a "
            "decoy, not a store)."
        )
    else:
        _extend(directories, resolve_kb_chroma_dir, settings)
        _extend(directories, resolve_evidence_chroma_dir, settings)

    _extend(directories, resolve_evidence_storage_dir, settings)
    directories.append(_absolute(knowledge_root()))
    directories.append(_absolute(knowledge_root() / "global"))

    for path in directories:
        if path.exists():
            continue
        # These paths are operator-supplied now, so a mkdir here can fail on a
        # read-only mount, a PVC that did not attach, or a knob pointing
        # somewhere this process cannot write. Before fm#936 the bootstrap only
        # touched the project root and could reasonably assume success; now a
        # bad knob must not turn into a CrashLoopBackOff. Every consumer
        # creates its own tree lazily anyway (``create_persistent_client`` does
        # ``os.makedirs``), so the honest response is to say so and continue —
        # a startup that dies here takes down the whole API over one directory.
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error(
                "Could not create data directory %s: %s: %s. Startup "
                "continues; whatever reads this path will fail on its own if "
                "it genuinely needs it.",
                path,
                type(exc).__name__,
                exc,
            )
        else:
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


async def assign_operator_roles(
    user_store: Any,
    user: Any,
    invoked_via: str = "startup-regrant",
) -> tuple[Any, list[str], Optional[Exception]]:
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

    Being the single writer, it is also the single **auditor**: every grant is
    recorded to ``operator_access_audit``, whether it came from the CLI or from
    the startup re-grant. Recording it in the CLI instead left the re-grant
    silent, so a demotion followed by a restart showed a revocation and no
    re-grant while the account held ``platform_admin`` again (fm#1050).

    Args:
        user_store: Initialised user store instance
        user: DevUser to check and update
        invoked_via: What performed the grant, recorded in the audit row so the
            trail distinguishes a hand-run promotion from the startup re-grant.

    Returns:
        ``(user, granted, audit_error)`` — the user (updated if roles were
        granted, unchanged otherwise), the list of roles this call added (empty
        when it was a no-op), and the exception from the audit write if it
        failed. Callers that report to a human surface ``audit_error``; startup
        ignores it, having already logged it.
    """
    missing = [r for r in PLATFORM_ADMIN_ROLE_SET if r not in (user.roles or [])]
    if not missing:
        return user, [], None

    logger.info(f"User '{user.username}' missing operator roles {missing} — granting")
    user.roles = list(user.roles or []) + missing
    user = await user_store.update_user(user)
    logger.info(f"Operator roles granted to '{user.username}': {user.roles}")

    # Audited HERE, not in the CLI, for the same reason the grant itself lives
    # here: this is the single writer. Recording it in the promote command left
    # the startup re-grant silent, so a demotion followed by a restart produced
    # a trail showing a revocation and no re-grant while the account held
    # `platform_admin` again (fm#1050).
    #
    # The exception is returned rather than raised: the grant is already durable,
    # so raising could not undo it, and startup must not fail over an audit sink
    # — a standalone deployment with no operator is unusable. Callers that report
    # to a human surface it; startup logs and continues.
    audit_error: Optional[Exception] = None
    try:
        from faultmaven.cli._operator_role_audit import record_operator_role_change
        from faultmaven.models.interfaces_operator_audit import OperatorAction

        await record_operator_role_change(
            action=OperatorAction.ROLE_GRANTED,
            user=user,
            roles_changed=missing,
            invoked_via=invoked_via,
        )
    except Exception as exc:
        audit_error = exc
        logger.error(
            f"operator_role_grant_unaudited: granted {missing} to "
            f"'{user.username}' via {invoked_via}, but the audit record failed: "
            f"{exc}"
        )

    return user, missing, audit_error


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
