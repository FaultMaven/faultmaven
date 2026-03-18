"""OAuth Authorization Code Repository Implementations.

Three implementations of IOAuthCodeRepository:
1. InMemoryOAuthCodeRepository - For local development (zero dependencies)
2. RedisOAuthCodeRepository - For cloud deployment (ephemeral storage)
3. PostgresOAuthCodeRepository - For enterprise deployment (persistent storage, ORM)
"""

import asyncio
from datetime import datetime, timezone
from typing import Dict, Optional

from faultmaven.modules.auth.contracts import IOAuthCodeRepository, OAuthCodeDTO


class InMemoryOAuthCodeRepository(IOAuthCodeRepository):
    """In-memory implementation of OAuth code repository.

    Stores authorization codes in Python dictionaries with automatic TTL expiration.
    Suitable for local development with zero infrastructure dependencies.
    """

    def __init__(self):
        self._codes: Dict[str, OAuthCodeDTO] = {}
        self._lock = asyncio.Lock()

    async def save_code(self, code_data: OAuthCodeDTO) -> None:
        async with self._lock:
            self._codes[code_data.code] = code_data

    async def get_code(self, code: str) -> Optional[OAuthCodeDTO]:
        async with self._lock:
            code_data = self._codes.get(code)
            if not code_data:
                return None
            if datetime.now(timezone.utc) > code_data.expires_at:
                del self._codes[code]
                return None
            return code_data

    async def mark_code_used(self, code: str) -> None:
        async with self._lock:
            if code in self._codes:
                existing = self._codes[code]
                self._codes[code] = OAuthCodeDTO(
                    code=existing.code,
                    user_id=existing.user_id,
                    redirect_uri=existing.redirect_uri,
                    code_challenge=existing.code_challenge,
                    expires_at=existing.expires_at,
                    used=True,
                )

    async def delete_expired_codes(self) -> int:
        async with self._lock:
            now = datetime.now(timezone.utc)
            expired_codes = [
                code
                for code, code_data in self._codes.items()
                if now > code_data.expires_at
            ]
            for code in expired_codes:
                del self._codes[code]
            return len(expired_codes)


class RedisOAuthCodeRepository(IOAuthCodeRepository):
    """Redis implementation of OAuth code repository.

    Uses Redis TTL for automatic expiration. Suitable for cloud deployment.
    """

    def __init__(self, redis_client):
        self.redis = redis_client
        self._key_prefix = "oauth:code:"

    def _make_key(self, code: str) -> str:
        return f"{self._key_prefix}{code}"

    async def save_code(self, code_data: OAuthCodeDTO) -> None:
        import json

        key = self._make_key(code_data.code)
        value = json.dumps(
            {
                "code": code_data.code,
                "user_id": code_data.user_id,
                "redirect_uri": code_data.redirect_uri,
                "code_challenge": code_data.code_challenge,
                "expires_at": code_data.expires_at.isoformat(),
                "used": code_data.used,
            }
        )
        now = datetime.now(timezone.utc)
        ttl = int((code_data.expires_at - now).total_seconds())
        if ttl > 0:
            await self.redis.setex(key, ttl, value)

    async def get_code(self, code: str) -> Optional[OAuthCodeDTO]:
        import json

        key = self._make_key(code)
        value = await self.redis.get(key)
        if not value:
            return None
        data = json.loads(value)
        return OAuthCodeDTO(
            code=data["code"],
            user_id=data["user_id"],
            redirect_uri=data["redirect_uri"],
            code_challenge=data["code_challenge"],
            expires_at=datetime.fromisoformat(data["expires_at"]),
            used=data["used"],
        )

    async def mark_code_used(self, code: str) -> None:
        import json

        key = self._make_key(code)
        value = await self.redis.get(key)
        if not value:
            return
        data = json.loads(value)
        data["used"] = True
        ttl = await self.redis.ttl(key)
        if ttl > 0:
            await self.redis.setex(key, ttl, json.dumps(data))

    async def delete_expired_codes(self) -> int:
        return 0  # Redis handles expiration automatically


class PostgresOAuthCodeRepository(IOAuthCodeRepository):
    """SQLAlchemy ORM implementation of OAuth code repository."""

    def __init__(self, db_session_factory):
        self.session_factory = db_session_factory

    async def save_code(self, code_data: OAuthCodeDTO) -> None:
        from faultmaven.infrastructure.persistence.models import (
            OAuthAuthorizationCodeModel,
        )

        async with self.session_factory() as session:
            model = OAuthAuthorizationCodeModel(
                code=code_data.code,
                user_id=code_data.user_id,
                redirect_uri=code_data.redirect_uri,
                code_challenge=code_data.code_challenge,
                expires_at=code_data.expires_at,
                used=code_data.used,
            )
            session.add(model)
            await session.commit()

    async def get_code(self, code: str) -> Optional[OAuthCodeDTO]:
        from sqlalchemy import select

        from faultmaven.infrastructure.persistence.models import (
            OAuthAuthorizationCodeModel,
        )

        async with self.session_factory() as session:
            now = datetime.now(timezone.utc)
            stmt = select(OAuthAuthorizationCodeModel).where(
                OAuthAuthorizationCodeModel.code == code,
                OAuthAuthorizationCodeModel.expires_at > now,
            )
            result = await session.execute(stmt)
            model = result.scalar_one_or_none()

            if not model:
                return None

            return OAuthCodeDTO(
                code=model.code,
                user_id=model.user_id,
                redirect_uri=model.redirect_uri,
                code_challenge=model.code_challenge,
                expires_at=model.expires_at,
                used=model.used or False,
            )

    async def mark_code_used(self, code: str) -> None:
        from sqlalchemy import update

        from faultmaven.infrastructure.persistence.models import (
            OAuthAuthorizationCodeModel,
        )

        async with self.session_factory() as session:
            stmt = (
                update(OAuthAuthorizationCodeModel)
                .where(OAuthAuthorizationCodeModel.code == code)
                .values(used=True)
            )
            await session.execute(stmt)
            await session.commit()

    async def delete_expired_codes(self) -> int:
        from sqlalchemy import delete

        from faultmaven.infrastructure.persistence.models import (
            OAuthAuthorizationCodeModel,
        )

        async with self.session_factory() as session:
            now = datetime.now(timezone.utc)
            stmt = delete(OAuthAuthorizationCodeModel).where(
                OAuthAuthorizationCodeModel.expires_at <= now
            )
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount
