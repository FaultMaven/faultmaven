"""Centralized test utilities.

Provides common utilities for test ID generation, test data creation,
and other shared test infrastructure.
"""

from typing import Iterable
from uuid import uuid4


async def seed_organizations(session, org_ids: Iterable[str]) -> None:
    """Insert organization rows so FK-bound tests can reference them.

    Phase 9 made organization_id a FK on tenanted tables. Tests that hand-craft
    org IDs must seed the matching organization row first to satisfy the
    constraint when PRAGMA foreign_keys=ON.
    """
    from sqlalchemy import select

    from faultmaven.infrastructure.persistence.models import OrganizationModel

    seen_slugs: set[str] = set()
    for org_id in org_ids:
        existing = await session.get(OrganizationModel, org_id)
        if existing is not None:
            continue
        # Slugs are unique; derive from org_id but ensure no collision in batch.
        slug = org_id
        suffix = 0
        while slug in seen_slugs:
            suffix += 1
            slug = f"{org_id}-{suffix}"
        seen_slugs.add(slug)
        session.add(
            OrganizationModel(
                organization_id=org_id,
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

    from faultmaven.infrastructure.persistence.models import OrganizationModel

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
            session.execute(
                insert(OrganizationModel),
                [
                    {
                        "organization_id": oid,
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
