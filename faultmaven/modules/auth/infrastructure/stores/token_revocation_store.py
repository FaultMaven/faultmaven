"""Token Revocation Store Implementations.

The single revocation store for the whole deployment (issue #767): every
revoke path (OAuth /revoke, refresh rotation in both auth modes, logout,
admin per-user revocation) writes here, and the request-path check
(``AuthService._is_revoked``) reads here.

**Two implementations, chosen by DEPLOYMENT MODE** (#828).
``create_token_revocation_store`` picks between them:

- :class:`RedisTokenRevocationStore` — cloud. Its cache is a real Redis that
  outlives the API pod, Redis expiry handles cleanup, and the deployment-wide
  store is shared across replicas.
- :class:`SqlTokenRevocationStore` — standalone, whose cache is the in-process
  FakeRedis singleton. That has no persistence, so an API restart resurrected
  every revoked-but-unexpired token for the remainder of its natural life.
  Revocation there lives in ``token_revocations`` instead, beside the account
  state (deactivation) that already survived a restart.

The choice is deliberately made on configuration rather than on what the cache
client turned out to BE: a Redis ping failure or ``SKIP_SERVICE_CHECKS`` can
substitute FakeRedis at boot, and keying on that made the store identity differ
between two boots of one deployment — see ``create_token_revocation_store``.

The two are held to ONE behaviour by a shared contract suite
(``tests/unit/modules/auth/test_revocation_store_contract.py``), which is
parametrised over both classes: a rule added to one and not the other fails
there rather than in whichever deployment happens to run the other.

Two granularities (see ``ITokenRevocationStore``): individual tokens by JTI,
and whole users by revocation watermark (#769). Both live in this one store
with TTLs matching token expiration.

**Watermark retention is 90 days on BOTH arms**, because
``AuthService._watermark_ttl_seconds`` is store-agnostic and holds against the
schema ceiling on token lifetime rather than the configured one (#828). On the
Redis arm that raises per-user key retention from the configured refresh
lifetime (7 days by default) to 90 — a real memory cost, not only a row. It is
one small key per user who has been revoked and has not signed back in since (a
sign-in clears the watermark), and the alternative is the resurrection the
ceiling exists to prevent, so the cost is accepted rather than traded away.

In the Redis store the key prefix comes from
``settings.security.token_revocation_prefix`` so writers and the reader can
never disagree on the namespace. Per-token entries live under a ``jti:``
sub-namespace and per-user watermarks under ``user:``; see
``ITokenRevocationStore`` for why they must not overlap. The SQL store has no
prefix to agree on — the table IS the namespace, and the two granularities are
separated by a ``scope`` column that is half of the primary key.

Deploy note (Redis): moving per-token entries under ``jti:`` orphans any
revocation keys written by a previous build (they sat directly under the
prefix), so tokens revoked before the upgrade are no longer seen as revoked.
Acceptable pre-production; flush the ``{prefix}*`` keyspace on upgrade if any
revoked refresh token must stay dead across it. Standalone deployments crossing
#828 are in the same position for a different reason: entries written to the
in-process FakeRedis were never on disk to carry over.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, or_, select

from faultmaven.infrastructure.persistence.database import get_db_session
from faultmaven.infrastructure.persistence.db_compat import dialect_insert
from faultmaven.infrastructure.persistence.models import TokenRevocationModel
from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    ITokenRevocationStore,
)

#: ``scope`` values. Literals, not an enum: they are half of a primary key and
#: a column CHECK constraint names the same two strings.
_JTI_SCOPE = "jti"
_USER_SCOPE = "user"


class RedisTokenRevocationStore(ITokenRevocationStore):
    """Redis implementation of token revocation store.

    Works against real Redis (cloud) or FakeRedis (standalone).
    Uses Redis TTL for automatic expiration.
    """

    def __init__(self, redis_client, key_prefix: str = "revoked:token:"):
        self.redis = redis_client
        self._key_prefix = key_prefix
        # Both namespaces derive from the same prefix, so they can never be
        # pointed at different Redis instances or drift apart. They sit under
        # DISTINCT literal segments ("jti:" vs "user:") so no jti value can
        # ever produce a per-user key. That separation does not depend on how
        # trustworthy the jti is: it arrives inside a submitted token, on an
        # endpoint (POST /auth/oauth/revoke) that is unauthenticated by RFC
        # 7009 design. A jti of "user:<victim>" must not be able to overwrite
        # that victim's watermark, whatever else changes upstream.
        self._jti_key_prefix = f"{key_prefix}jti:"
        self._user_key_prefix = f"{key_prefix}user:"

    def _make_key(self, jti: str) -> str:
        return f"{self._jti_key_prefix}{jti}"

    def _make_user_key(self, user_id: str) -> str:
        return f"{self._user_key_prefix}{user_id}"

    async def add_revoked_token(self, jti: str, ttl: int) -> None:
        key = self._make_key(jti)
        await self.redis.setex(key, ttl, "revoked")

    async def is_revoked(self, jti: str) -> bool:
        key = self._make_key(jti)
        return await self.redis.exists(key) > 0

    async def revoke_user_tokens_before(
        self, user_id: str, revoked_at: float, ttl: int
    ) -> None:
        key = self._make_user_key(user_id)
        # Stored at full precision, compared at whole seconds. `is_user_revoked`
        # floors it back, so the revocation rule is bit-for-bit what it was when
        # this held an int — the fraction exists only for
        # `clear_user_revocation_if_before`, which has to order this instant
        # against a caller's capture and cannot do that at second granularity.
        await self.redis.setex(key, ttl, str(revoked_at))

    async def is_user_revoked(self, user_id: str, issued_at: int) -> bool:
        key = self._make_user_key(user_id)
        raw = await self.redis.get(key)
        if raw is None:
            return False
        if isinstance(raw, bytes):
            raw = raw.decode()
        # A malformed watermark raises, and the caller's error posture decides:
        # the request path fails open, generator validation fails closed. Both
        # are preferable to silently guessing at a corrupt value here.
        #
        # `float` then `int`, so a watermark written by a pre-fraction build
        # ("1700000000") reads identically to one written by this one.
        return issued_at <= int(float(raw))

    #: Delete the watermark only if it is strictly older than ARGV[1].
    #:
    #: Server-side so the read and the delete are one operation. Read-then-
    #: delete in Python would let a revocation landing between the two be
    #: deleted on the strength of having inspected the *previous* watermark —
    #: which is the straddle (#831) this comparison exists to respect, arrived
    #: at by a different route.
    _CLEAR_IF_BEFORE = """
    local raw = redis.call('GET', KEYS[1])
    if raw and tonumber(raw) and tonumber(raw) < tonumber(ARGV[1]) then
        redis.call('DEL', KEYS[1])
        return 1
    end
    return 0
    """

    async def clear_user_revocation_if_before(
        self, user_id: str, instant: float
    ) -> bool:
        key = self._make_user_key(user_id)
        cleared = await self.redis.eval(self._CLEAR_IF_BEFORE, 1, key, repr(instant))
        return bool(cleared)

    async def cleanup_expired(self) -> int:
        return 0  # Redis handles expiration automatically


class SqlTokenRevocationStore(ITokenRevocationStore):
    """Database implementation of the token revocation store (#828).

    Used where the cache client does not outlive the process — standalone,
    whose Redis is the in-process FakeRedis singleton. Same two granularities,
    same rules, same ``ITokenRevocationStore``; the difference is only that the
    state survives a restart.

    #767 deliberately deleted a ``PostgresTokenRevocationStore`` because
    NOTHING constructed it: the request-path check read Redis, so a database
    row could not revoke anything and the class was dead code pretending to be
    a control. This one is reintroduced with the opposite property — it is what
    ``create_token_revocation_store`` returns in standalone, so it is the store
    every revoke path writes to and the request path reads. What #767 removed
    was an unwired second store; what this adds is the only store its
    deployment has.

    **Expiry is a predicate, not a sweeper.** Every read filters on
    ``expires_at``, so an elapsed entry stops revoking at its deadline whether
    or not anything has deleted it — the property Redis gets from TTL. Rows are
    then reclaimed opportunistically: each WRITE deletes elapsed rows in its
    own transaction, which bounds the table without a scheduled job that could
    be forgotten at wiring time (nothing calls ``cleanup_expired``, in this
    class or the Redis one). Writes are revocations, so the sweep runs rarely
    and against an index.

    **Session per operation**, as the sibling sessionless repositories: the
    store is a process-wide singleton built at composition time, long before
    any request, so it cannot hold one.
    """

    def __init__(self, session_factory=None):
        """
        Args:
            session_factory: Async context manager yielding an ``AsyncSession``.
                Defaults to the application's ``get_db_session``. A parameter
                so a caller can bind a different database — which is also how
                a restart is simulated in tests, by building a second store
                over a second engine on the same file.
        """
        self._session_factory = session_factory or get_db_session

    @staticmethod
    def _deadline(ttl: int) -> datetime:
        return datetime.now(timezone.utc) + timedelta(seconds=ttl)

    async def _sweep(self, session) -> int:
        """Delete entries that can no longer revoke anything."""
        result = await session.execute(
            delete(TokenRevocationModel).where(
                TokenRevocationModel.expires_at <= datetime.now(timezone.utc)
            )
        )
        # ``rowcount`` is -1 where a driver cannot report it; a sweep that
        # could not count is "nothing to report", never a negative total.
        return max(result.rowcount or 0, 0)

    async def _upsert(
        self, scope: str, subject: str, revoked_at: float, ttl: int
    ) -> None:
        async with self._session_factory() as session:
            await self._sweep(session)
            # ONE statement, via the repository's dialect helper. The
            # delete-then-insert this replaced was not atomic: two concurrent
            # revocations of the same (scope, subject) — a double-submitted
            # logout, or an admin revoke racing a password change — both found
            # nothing to delete and both inserted, and PostgreSQL raised a
            # unique violation that ``revoke_token``/``revoke_user_tokens``
            # propagate as a failed revocation. ``dialect_insert`` is what the
            # seven sibling upsert sites already use, so the SQLite and
            # PostgreSQL constructs cannot drift (they are NOT interchangeable
            # — see ``db_compat``).
            #
            # A later revocation overwrites an earlier one unconditionally,
            # matching ``SETEX`` and the interface's contract ("the caller has
            # already decided to invalidate everything outstanding").
            statement = dialect_insert(session, TokenRevocationModel).values(
                scope=scope,
                subject=subject,
                revoked_at=revoked_at,
                expires_at=self._deadline(ttl),
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["scope", "subject"],
                    set_={
                        "revoked_at": statement.excluded.revoked_at,
                        "expires_at": statement.excluded.expires_at,
                    },
                )
            )
            # The store owns its unit of work. Leaving the commit to the
            # session factory made durability — the entire point of #828 — an
            # undocumented property of the injected callable: handed the
            # idiomatic ``async_sessionmaker()``, every revocation was dropped
            # on exit, and ``AuthService._is_revoked`` fails open, so the
            # control would simply have been off with a log line as the only
            # signal.
            await session.commit()

    async def add_revoked_token(self, jti: str, ttl: int) -> None:
        # ``revoked_at`` is meaningless on this arm — a jti entry revokes
        # unconditionally — so it is stored as 0 rather than as a timestamp
        # somebody could later mistake for a watermark.
        await self._upsert(_JTI_SCOPE, jti, 0.0, ttl)

    async def is_revoked(self, jti: str) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                select(TokenRevocationModel.subject).where(
                    TokenRevocationModel.scope == _JTI_SCOPE,
                    TokenRevocationModel.subject == jti,
                    TokenRevocationModel.expires_at > datetime.now(timezone.utc),
                )
            )
            return result.first() is not None

    async def revoke_user_tokens_before(
        self, user_id: str, revoked_at: float, ttl: int
    ) -> None:
        await self._upsert(_USER_SCOPE, user_id, revoked_at, ttl)

    async def is_user_revoked(self, user_id: str, issued_at: int) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                select(TokenRevocationModel.revoked_at).where(
                    TokenRevocationModel.scope == _USER_SCOPE,
                    TokenRevocationModel.subject == user_id,
                    TokenRevocationModel.expires_at > datetime.now(timezone.utc),
                )
            )
            row = result.first()
            if row is None:
                return False
            # Floored to whole seconds, exactly as the Redis arm does: the
            # revocation rule is ``iat <= watermark`` at second granularity,
            # and the fraction exists only so
            # ``clear_user_revocation_if_before`` can order two instants.
            return issued_at <= int(float(row[0]))

    async def revocation_state(self, jti, user_id, issued_at):
        """Both arms in ONE session and ONE query (see the base class).

        The request path asks this on every authenticated request, and here a
        read is not free: SQLite's engine uses ``NullPool``, so each session is
        a fresh connection paying seven PRAGMAs, and PostgreSQL additionally
        runs the RLS ``set_config`` per transaction. Inheriting the default
        opened two of those per request — measured at 17.7ms against 10.8ms for
        one (#828 review).

        This composes nothing: it returns the two raw booleans and
        ``revocation_reason`` still decides which wins. The short-circuit the
        default has (a revoked jti never consults the watermark) is not worth a
        second round trip here, so both rows are fetched and the jti answer is
        still reported alone when it is True.
        """
        if not jti and not user_id:
            return False, False

        wanted = []
        if jti:
            wanted.append(
                (TokenRevocationModel.scope == _JTI_SCOPE)
                & (TokenRevocationModel.subject == jti)
            )
        if user_id and issued_at is not None:
            wanted.append(
                (TokenRevocationModel.scope == _USER_SCOPE)
                & (TokenRevocationModel.subject == user_id)
            )
        if not wanted:
            return False, False

        async with self._session_factory() as session:
            rows = await session.execute(
                select(
                    TokenRevocationModel.scope,
                    TokenRevocationModel.revoked_at,
                ).where(
                    TokenRevocationModel.expires_at > datetime.now(timezone.utc),
                    or_(*wanted),
                )
            )
            token_revoked = False
            user_revoked = False
            for scope, revoked_at in rows.all():
                if scope == _JTI_SCOPE:
                    token_revoked = True
                elif issued_at is not None:
                    # Floored to whole seconds, the same rule ``is_user_revoked``
                    # applies — one comparison, spelled once per arm.
                    user_revoked = issued_at <= int(float(revoked_at))
            if token_revoked:
                return True, False
            return False, user_revoked

    async def clear_user_revocation_if_before(
        self, user_id: str, instant: float
    ) -> bool:
        async with self._session_factory() as session:
            # ONE statement, for the same reason the Redis arm uses a Lua
            # script: a read-then-delete in Python would let a revocation that
            # landed between the two be deleted on the strength of the
            # PREVIOUS watermark — the straddle (#831) this comparison exists
            # to respect.
            result = await session.execute(
                delete(TokenRevocationModel).where(
                    TokenRevocationModel.scope == _USER_SCOPE,
                    TokenRevocationModel.subject == user_id,
                    TokenRevocationModel.revoked_at < instant,
                    # An ELAPSED watermark is not there to clear. Redis answers
                    # False because its key is already gone; without this the
                    # SQL arm answered True, because nothing had swept the row
                    # yet — the two arms disagreeing about whether a login
                    # superseded a revocation that had already stopped
                    # revoking. Every read here filters on the deadline, and
                    # this is a read that happens to delete.
                    TokenRevocationModel.expires_at > datetime.now(timezone.utc),
                )
            )
            await session.commit()
            # ``> 0``, not ``bool(...)``: -1 means "the driver could not count",
            # and bool(-1) is True — reporting a clear that may not have
            # happened. Same guard as ``_sweep``.
            return max(result.rowcount or 0, 0) > 0

    async def cleanup_expired(self) -> int:
        async with self._session_factory() as session:
            swept = await self._sweep(session)
            await session.commit()
            return swept
