"""The driver writes (ADR-020), one spelling for both SQL repositories.

Every statement here is plain SQL that SQLite and PostgreSQL both run, so the
two dialect repositories share it rather than carry two copies of a rule that
must not drift: a change of driver bumps ``cases.version`` (so an in-flight
turn's save fails with a version conflict) and writes its
``case_driver_changed`` audit row in the SAME transaction.

The functions take primitives, not domain objects, so this infrastructure
module needs nothing from the domain layer.
"""

from typing import Any, List, Optional, Tuple

from sqlalchemy import text

from faultmaven.models.interfaces_user import AuditCategory, AuditEventType

_REASSIGN = text(
    "UPDATE cases SET driver_id = :driver_id, version = version + 1 "
    "WHERE case_id = :case_id AND version = :expected_version"
)

_RELEASE = text(
    "UPDATE cases SET driver_id = NULL, version = version + 1 "
    "WHERE case_id = :case_id AND driver_id = :driver_id"
)

_VERSION = text("SELECT version FROM cases WHERE case_id = :case_id")

_DRIVEN_BY = text(
    "SELECT case_id, enterprise_id, user_id, driver_id FROM cases "
    "WHERE driver_id = :user_id ORDER BY case_id"
)

# ``created_at`` takes the column's server default. ``event_category`` is
# AUTHORIZATION: a driver change moves who may write the case.
_AUDIT = text(
    "INSERT INTO user_audit_log (user_id, enterprise_id, event_type, "
    "event_category, resource_type, resource_id, details, success) VALUES "
    "(:user_id, :enterprise_id, :event_type, :event_category, 'case', "
    ":case_id, :details, :success)"
)


async def _audit(
    db: Any,
    *,
    case_id: str,
    enterprise_id: str,
    actor_user_id: Optional[str],
    details: str,
) -> None:
    await db.execute(
        _AUDIT,
        {
            "user_id": actor_user_id,
            "enterprise_id": enterprise_id,
            "event_type": AuditEventType.CASE_DRIVER_CHANGED.value,
            "event_category": AuditCategory.AUTHORIZATION.value,
            "case_id": case_id,
            "details": details,
            "success": True,
        },
    )


async def reassign_driver(
    db: Any,
    *,
    case_id: str,
    driver_id: Optional[str],
    expected_version: int,
    enterprise_id: str,
    actor_user_id: Optional[str],
    details: str,
) -> Optional[int]:
    """Compare-and-swap the stored driver on ``version``, bump it, audit it,
    commit. ``None`` (and a rollback) when the version moved."""
    result = await db.execute(
        _REASSIGN,
        {
            "case_id": case_id,
            "driver_id": driver_id,
            "expected_version": expected_version,
        },
    )
    if (result.rowcount or 0) != 1:
        await db.rollback()
        return None
    await _audit(
        db,
        case_id=case_id,
        enterprise_id=enterprise_id,
        actor_user_id=actor_user_id,
        details=details,
    )
    version = (await db.execute(_VERSION, {"case_id": case_id})).scalar()
    await db.commit()
    return version


async def release_driver(
    db: Any,
    *,
    case_id: str,
    driver_id: str,
    enterprise_id: str,
    actor_user_id: Optional[str],
    details: str,
) -> bool:
    """Clear ``driver_id`` iff ``driver_id`` still drives the case, bump the
    version, audit it, commit. ``False`` (and a rollback) when it no longer
    did."""
    result = await db.execute(_RELEASE, {"case_id": case_id, "driver_id": driver_id})
    if (result.rowcount or 0) != 1:
        await db.rollback()
        return False
    await _audit(
        db,
        case_id=case_id,
        enterprise_id=enterprise_id,
        actor_user_id=actor_user_id,
        details=details,
    )
    await db.commit()
    return True


async def list_cases_driven_by(
    db: Any, user_id: str
) -> List[Tuple[str, str, Optional[str], str]]:
    """``(case_id, enterprise_id, creator_id, driver_id)`` for every case
    ``user_id`` drives by assignment."""
    rows = (await db.execute(_DRIVEN_BY, {"user_id": user_id})).fetchall()
    return [(row[0], row[1], row[2], row[3]) for row in rows]
