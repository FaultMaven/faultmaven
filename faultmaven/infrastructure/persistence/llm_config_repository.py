"""Repository for LLM configuration overrides.

Provides async read/write access to the llm_config_overrides table.
Overrides stored here take precedence over environment variables.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import delete, select

from faultmaven.infrastructure.persistence.database import get_db_session
from faultmaven.infrastructure.persistence.db_compat import dialect_insert
from faultmaven.infrastructure.persistence.models import LLMConfigOverrideModel

logger = logging.getLogger(__name__)


async def get_all_overrides() -> dict[str, str]:
    """Read all config overrides as a key-value dict."""
    async with get_db_session() as session:
        result = await session.execute(
            select(LLMConfigOverrideModel.key, LLMConfigOverrideModel.value)
        )
        return {row.key: row.value for row in result}


async def get_override(key: str) -> Optional[str]:
    """Read a single config override value."""
    async with get_db_session() as session:
        result = await session.execute(
            select(LLMConfigOverrideModel.value).where(
                LLMConfigOverrideModel.key == key
            )
        )
        row = result.scalar_one_or_none()
        return row


async def set_override(key: str, value: str, user_id: Optional[str] = None) -> None:
    """Set a config override (upsert — insert or update)."""
    async with get_db_session() as session:
        now = datetime.now(timezone.utc)

        # Use SQLite-compatible upsert
        stmt = dialect_insert(session, LLMConfigOverrideModel).values(
            key=key,
            value=value,
            updated_at=now,
            updated_by=user_id,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["key"],
            set_={
                "value": value,
                "updated_at": now,
                "updated_by": user_id,
            },
        )
        await session.execute(stmt)
        await session.commit()
        logger.info(f"LLM config override set: {key} (by {user_id or 'system'})")


async def set_overrides(
    overrides: dict[str, str], user_id: Optional[str] = None
) -> None:
    """Set multiple config overrides atomically."""
    async with get_db_session() as session:
        now = datetime.now(timezone.utc)

        for key, value in overrides.items():
            stmt = dialect_insert(session, LLMConfigOverrideModel).values(
                key=key,
                value=value,
                updated_at=now,
                updated_by=user_id,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["key"],
                set_={
                    "value": value,
                    "updated_at": now,
                    "updated_by": user_id,
                },
            )
            await session.execute(stmt)

        await session.commit()
        logger.info(
            f"LLM config overrides set: {list(overrides.keys())} "
            f"(by {user_id or 'system'})"
        )


async def delete_override(key: str) -> None:
    """Remove a config override (reverts to env var default)."""
    async with get_db_session() as session:
        await session.execute(
            delete(LLMConfigOverrideModel).where(LLMConfigOverrideModel.key == key)
        )
        await session.commit()
        logger.info(f"LLM config override deleted: {key}")
