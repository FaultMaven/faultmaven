"""Centralized test utilities.

Provides common utilities for test ID generation, test data creation,
and other shared test infrastructure.
"""

from typing import Iterable, Optional
from uuid import uuid4

# Default enterprise UUID — mirrors SingleTenantProvider.DEFAULT_ENTERPRISE_ID
# and migration 006's seeded row. Tests that build orgs/users without a
# specific enterprise context anchor to this row.
DEFAULT_TEST_ENTERPRISE_ID = "00000000-0000-0000-0000-000000000002"


async def seed_default_enterprise(session) -> None:
    """Insert the default enterprise row used as parent for test orgs/users.

    Idempotent: returns silently if the row already exists.
    """
    from faultmaven.infrastructure.persistence.models import EnterpriseModel

    existing = await session.get(EnterpriseModel, DEFAULT_TEST_ENTERPRISE_ID)
    if existing is not None:
        return
    session.add(
        EnterpriseModel(
            enterprise_id=DEFAULT_TEST_ENTERPRISE_ID,
            name="Default Test Enterprise",
            slug="default-test",
        )
    )
    await session.commit()


async def seed_users(
    session,
    user_ids: Iterable[str],
    enterprise_id: Optional[str] = None,
) -> None:
    """Insert user rows so FK-bound tests can reference them.

    `cases.user_id` is a nullable FK to `users.user_id` with
    `ondelete="SET NULL"`. With PRAGMA foreign_keys=ON, hand-crafted user IDs
    that don't exist in `users` fail the FK. Seed minimal valid stubs.

    `users.enterprise_id` is NOT NULL (FK to `enterprises.enterprise_id`).
    The default enterprise row is created automatically; pass an explicit
    `enterprise_id` to anchor users elsewhere.

    The UserModel has unique constraints on `username` and `email`; we derive
    both from the user_id to avoid collisions in batch.
    """
    from faultmaven.infrastructure.persistence.models import UserModel

    enterprise_id = enterprise_id or DEFAULT_TEST_ENTERPRISE_ID
    await seed_default_enterprise(session)

    seen_usernames: set[str] = set()
    seen_emails: set[str] = set()
    for user_id in user_ids:
        existing = await session.get(UserModel, user_id)
        if existing is not None:
            continue
        username = user_id
        suffix = 0
        while username in seen_usernames:
            suffix += 1
            username = f"{user_id}-{suffix}"
        seen_usernames.add(username)
        email = f"{username}@test.local"
        while email in seen_emails:
            suffix += 1
            email = f"{username}-{suffix}@test.local"
        seen_emails.add(email)
        session.add(
            UserModel(
                user_id=user_id,
                enterprise_id=enterprise_id,
                username=username,
                email=email,
                display_name=f"Test User {user_id}",
                hashed_password="x" * 60,  # bcrypt-shaped placeholder
            )
        )
    await session.commit()


async def seed_organizations(
    session,
    org_ids: Iterable[str],
    enterprise_id: Optional[str] = None,
) -> None:
    """Insert organization rows so FK-bound tests can reference them.

    `cases.organization_id` is a NOT NULL FK to `organizations`. Tests that
    hand-craft org IDs must seed the matching organization row first to
    satisfy the constraint when PRAGMA foreign_keys=ON.

    `organizations.enterprise_id` is NOT NULL. The default enterprise row
    is created automatically; pass an explicit `enterprise_id` to anchor
    orgs elsewhere.
    """
    from faultmaven.infrastructure.persistence.models import OrganizationModel

    enterprise_id = enterprise_id or DEFAULT_TEST_ENTERPRISE_ID
    await seed_default_enterprise(session)

    seen_slugs: set[str] = set()
    for org_id in org_ids:
        existing = await session.get(OrganizationModel, org_id)
        if existing is not None:
            continue
        # Slugs are unique within an enterprise; derive from org_id but
        # ensure no collision in batch.
        slug = org_id
        suffix = 0
        while slug in seen_slugs:
            suffix += 1
            slug = f"{org_id}-{suffix}"
        seen_slugs.add(slug)
        session.add(
            OrganizationModel(
                organization_id=org_id,
                enterprise_id=enterprise_id,
                name=f"Test Org {org_id}",
                slug=slug,
            )
        )
    await session.commit()


def install_org_autoseed(sync_session) -> None:
    """Auto-create OrganizationModel rows for any new tenanted ORM object.

    Phase 9 added FK on tenanted tables. This hook seeds the parent org row
    inside the same flush so FK constraints succeed without churning each test
    to call seed_organizations() manually. Uses Core INSERT (not session.add)
    so the rows materialize in the current transaction before child INSERTs;
    objects added via session.add() inside before_flush defer to the next
    flush, which is too late to satisfy the FK.

    NOTE: This hook only fires for ORM-mediated writes (via session flush).
    Tests that exercise raw-SQL repositories like ``SQLiteCaseRepository``
    bypass the ORM flush path; those tests should call
    ``seed_organizations(session, [...])`` explicitly before the first save,
    or wrap the case repository with the ``seed_orgs_for_repo`` helper.

    Call once per AsyncSession with `session.sync_session` as the argument.
    """
    from sqlalchemy import event, insert, select

    from faultmaven.infrastructure.persistence.models import (
        EnterpriseModel,
        OrganizationModel,
    )

    @event.listens_for(sync_session, "before_flush")
    def _seed_referenced_orgs(session, flush_context, instances):
        org_ids: set[str] = set()
        for obj in session.new:
            if isinstance(obj, OrganizationModel):
                continue
            oid = getattr(obj, "organization_id", None)
            if oid:
                org_ids.add(oid)
        if not org_ids:
            return
        existing = {
            row[0]
            for row in session.execute(
                select(OrganizationModel.organization_id).where(
                    OrganizationModel.organization_id.in_(org_ids)
                )
            ).all()
        }
        missing = org_ids - existing
        if missing:
            # Ensure the default enterprise exists so the orgs.enterprise_id
            # FK target is satisfied.
            default_ent = session.get(EnterpriseModel, DEFAULT_TEST_ENTERPRISE_ID)
            if default_ent is None:
                session.execute(
                    insert(EnterpriseModel),
                    [
                        {
                            "enterprise_id": DEFAULT_TEST_ENTERPRISE_ID,
                            "name": "Default Test Enterprise",
                            "slug": "default-test",
                        }
                    ],
                )
            session.execute(
                insert(OrganizationModel),
                [
                    {
                        "organization_id": oid,
                        "enterprise_id": DEFAULT_TEST_ENTERPRISE_ID,
                        "name": f"Test Org {oid}",
                        "slug": oid,
                    }
                    for oid in missing
                ],
            )


def generate_case_id() -> str:
    """Generate a valid case ID matching the pattern ^case_[a-f0-9]{12}$."""
    return f"case_{uuid4().hex[:12]}"


def generate_item_id() -> str:
    """Generate a valid knowledge item ID."""
    return f"ki_{uuid4().hex[:12]}"


def generate_org_id() -> str:
    """Generate a valid organization ID."""
    return f"org_{uuid4().hex[:12]}"


def generate_evidence_id() -> str:
    """Generate a valid evidence ID."""
    return f"ev_{uuid4().hex[:12]}"


def generate_user_id() -> str:
    """Generate a valid user ID."""
    return f"user_{uuid4().hex[:12]}"


def generate_session_id() -> str:
    """Generate a valid session ID."""
    return f"sess_{uuid4().hex[:12]}"


def generate_agent_execution_id() -> str:
    """Generate a valid agent execution ID."""
    return f"agex_{uuid4().hex[:12]}"


def generate_investigation_session_id() -> str:
    """Generate a valid investigation session ID."""
    return f"is_{uuid4().hex[:12]}"
