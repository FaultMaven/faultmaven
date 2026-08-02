"""OAuth Authorization Code Repository Implementations.

Three implementations of IOAuthCodeRepository:
1. InMemoryOAuthCodeRepository - For local development (zero dependencies)
2. RedisOAuthCodeRepository - For cloud deployment (ephemeral storage)
3. PostgresOAuthCodeRepository - For enterprise deployment (persistent storage, ORM)
"""

import asyncio
import dataclasses
import logging
from datetime import datetime, timezone
from typing import Dict, Optional

from faultmaven.modules.auth.contracts import IOAuthCodeRepository, OAuthCodeDTO

logger = logging.getLogger(__name__)


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

    async def claim_code(self, code: str) -> bool:
        # The test and the set happen under one lock hold. Splitting them — as a
        # separate `get_code(); if not used: mark_used()` did — lets two
        # coroutines both observe `used=False` and both proceed.
        async with self._lock:
            code_data = self._codes.get(code)
            if code_data is None or code_data.used:
                return False
            # Expiry is checked here too, not just in ``get_code``. Redis gets
            # this free — an expired key is simply gone, so its claim fails —
            # and a backend that disagreed with Redis about what is claimable
            # would make the guarantee depend on the deployment.
            if datetime.now(timezone.utc) > code_data.expires_at:
                del self._codes[code]
                return False
            # ``replace`` rather than a field-by-field rebuild: a rebuild
            # silently drops any field added to the DTO later (the
            # organization claim was exactly such a field, #872).
            self._codes[code] = dataclasses.replace(code_data, used=True)
            return True

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
        # Serialize every DTO field rather than an enumerated subset: an
        # enumerated list is a second place to lose a field, which is how the
        # organization claim went missing from this hop (#872). Only
        # ``expires_at`` needs coaxing out of its native type.
        payload = dataclasses.asdict(code_data)
        payload["expires_at"] = code_data.expires_at.isoformat()
        value = json.dumps(payload)
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
        # Keep only fields the DTO still declares. During a rolling deploy this
        # store holds payloads written by the other version: one missing a field
        # takes the DTO default, one carrying a retired field is tolerated rather
        # than raising TypeError, which would surface as a 500 on a code the user
        # legitimately holds.
        known = {f.name for f in dataclasses.fields(OAuthCodeDTO)}
        fields = {k: v for k, v in data.items() if k in known}
        fields["expires_at"] = datetime.fromisoformat(data["expires_at"])
        return OAuthCodeDTO(**fields)

    async def claim_code(self, code: str) -> bool:
        """Claim the code via ``SET NX``, which is atomic in Redis itself.

        The claim is a **separate key**, not the ``used`` flag inside the stored
        JSON. Flipping a field inside a JSON blob is a read-modify-write: two
        callers both read ``used=False``, both write ``used=True``, and both
        believe they won. Only the server can arbitrate, and ``SET … NX`` is the
        primitive that does — exactly one caller creates the key.

        No Lua, deliberately: a script would need `EVAL` support from every
        Redis-compatible backend in play (including FakeRedis in standalone),
        and buys nothing a single `SET NX` does not already give.

        The claim key inherits the code's remaining TTL, so it cannot outlive
        what it protects and cannot accumulate. If the code key has already
        expired there is nothing to claim and this returns False.

        The ``used`` flag is still updated afterwards, best-effort: it is what
        ``get_code`` reports and what makes a replayed code fail early with
        CODE_ALREADY_USED rather than only failing here. Its write is not the
        gate, so losing it costs an error message, not the guarantee.
        """
        import json

        key = self._make_key(code)
        ttl = await self.redis.ttl(key)
        if ttl is None or ttl <= 0:
            return False

        claimed = await self.redis.set(f"{key}:claimed", "1", nx=True, ex=ttl)
        if not claimed:
            return False

        # Best-effort in fact, not just in intent. The claim is already won; a
        # failure here must not turn a successful redemption into a 500, because
        # the caller would retry and lose the claim it already holds — the burned
        # code this ordering exists to prevent. The flag only feeds `get_code`'s
        # early replay message; the guarantee is the claim key above.
        try:
            value = await self.redis.get(key)
            if value:
                data = json.loads(value)
                data["used"] = True
                await self.redis.setex(key, ttl, json.dumps(data))
        except Exception:  # noqa: BLE001 - see above; the claim already stands
            logger.warning(
                "OAuth code claimed but the used-flag write failed; "
                "single-use still holds via the claim key",
                exc_info=True,
            )
        return True

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
                organization_id=code_data.organization_id,
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
                organization_id=model.organization_id,
            )

    async def claim_code(self, code: str) -> bool:
        """Claim via a conditional UPDATE, arbitrated by the database.

        ``WHERE … AND used = false`` plus ``rowcount`` is the compare-and-swap:
        the row is locked for the duration of the UPDATE, so of two concurrent
        statements exactly one matches an unused row and reports 1. Without the
        predicate both would report success and both callers would mint.
        """
        from sqlalchemy import update

        from faultmaven.infrastructure.persistence.models import (
            OAuthAuthorizationCodeModel,
        )

        async with self.session_factory() as session:
            stmt = (
                update(OAuthAuthorizationCodeModel)
                .where(
                    OAuthAuthorizationCodeModel.code == code,
                    OAuthAuthorizationCodeModel.used.is_(False),
                    # Expiry belongs in the predicate, not in a prior SELECT:
                    # rows here are deleted by a sweep, not by a TTL, so an
                    # expired row lingers and would otherwise be claimable.
                    # Redis gets this free (the key is gone) and the in-memory
                    # store checks it explicitly — a backend that disagreed
                    # would make single-use depend on the deployment.
                    OAuthAuthorizationCodeModel.expires_at > datetime.now(timezone.utc),
                )
                .values(used=True)
            )
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount == 1

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
