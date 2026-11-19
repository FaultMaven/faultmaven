"""Token Revocation Store Implementations.

The single revocation store for the whole deployment (issue #767): every
revoke path (OAuth /revoke, refresh rotation in both auth modes, logout,
admin per-user revocation) writes here, rotation through the conditional
``claim_token`` (#2764), and the request-path check
(``AuthService._is_revoked``) reads here.

**Two implementations, chosen by DEPLOYMENT MODE** (#828).
``create_token_revocation_store`` picks between them:

- :class:`RedisTokenRevocationStore` — cloud. Its cache is a real Redis that
  outlives the API pod, Redis expiry handles cleanup, and the deployment-wide
  store is shared across replicas.
- :class:`SqlTokenRevocationStore` — standalone, whose cache is the in-process
  FakeRedis. That has no persistence, so an API restart resurrected
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
and whole users by revocation watermark (#769). Both live in this one store,
held for different lifetimes:

- a per-jti entry (``add_revoked_token``, ``claim_token``) for the token's
  remaining life plus ``REVOCATION_ENTRY_MARGIN_SECONDS``, capped at
  ``max_revocation_entry_ttl()`` (the schema ceiling on token lifetime) and
  given in full to a token with no ``exp`` (``revocation_entry_ttl``); no
  write shortens a live one (#2764);
- a watermark (``revoke_user_tokens_before``) for that ceiling plus
  ``MAX_MINT_BASIS_CARRY_SECONDS`` plus ``REVOCATION_CLOCK_ALLOWANCE`` rounded
  up to a second, whatever lifetime the user's tokens were minted with
  (``AuthService._watermark_ttl_seconds``).

**Watermark retention is the same on both stores**, because
``AuthService._watermark_ttl_seconds`` is store-agnostic and holds against the
schema ceiling on token lifetime rather than the configured one (#828). On the
Redis store, that means each per-user key is held in memory for the ceiling
rather than for the configured refresh lifetime. Every sign-out writes a
watermark (``logout`` → ``_revoke_account_wide``), as does every other caller
of ``AuthService.revoke_user_tokens``, and each write restarts the key's
lifetime. No sign-in clears one, in any auth mode (#2050): a mint after a
revocation is told apart from a straddling one by its full-precision basis,
not by removing the watermark. So a key lives its full lifetime, and the
population is one key per user who signed out or was revoked within it. The
alternative is the resurrection the ceiling exists to prevent, so the cost is
accepted rather than traded away.

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

import asyncio
import math
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import Float, and_, case, cast, delete, func, or_, select

from faultmaven.config.settings import REVOCATION_MONOTONE_WINDOW
from faultmaven.infrastructure.persistence.database import get_db_session
from faultmaven.infrastructure.persistence.db_compat import dialect_insert
from faultmaven.infrastructure.persistence.models import TokenRevocationModel
from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    CorruptRevocationEntry,
    ITokenRevocationStore,
    RefreshClaim,
    RevocationBasis,
    SequentialRevocationState,
)

#: ``scope`` values. Literals, not an enum: they are half of a primary key and
#: a column CHECK constraint names the same two strings.
_JTI_SCOPE = "jti"
_USER_SCOPE = "user"

#: Who wrote a jti entry (#2764). The Redis value of a claim starts with
#: ``claim:``; a SQL claim row has this ``revoked_at``, a revocation row ``0``.
_CLAIM_PREFIX = "claim:"
_CLAIM_MARK = -1.0


def escape_glob(text: str) -> str:
    """Escape Redis glob metacharacters so ``text`` matches only itself.

    The backslash goes first, or the escapes added for the other four would be
    escaped again.
    """
    for char in ("\\", "*", "?", "[", "]"):
        text = text.replace(char, "\\" + char)
    return text


def _watermark_covers(raw, basis: RevocationBasis) -> bool:
    """Whether a stored watermark covers a token's basis, or refuse (#1478, #2050).

    **One helper for all three compare sites** — the Redis arm's
    ``is_user_revoked`` and the SQL arm's ``is_user_revoked`` and
    ``revocation_state`` — because the rule is one rule and a second copy is
    the half that gets missed. A precise basis (``fm_basis``) is covered when
    ``basis <= watermark`` at full precision; a whole-second ``iat`` when
    ``iat <= floor(watermark)``. A watermark written by a pre-fraction build
    (``"1700000000"``) reads as the same instant.

    The ``try`` is deliberately this small. Everything inside it is the
    interpretation of a value that came out of storage, so a failure there can
    only mean the stored value is corrupt — which is what lets
    :class:`CorruptRevocationEntry` be classified as a store read failure
    without the classifier having to trust a broad exception type.

    Raises:
        CorruptRevocationEntry: The stored value is not readable as a finite
            number. ``TypeError`` as well as ``ValueError``: a ``NULL`` column
            gives ``float(None)``, the same corruption by another route.
    """
    try:
        if isinstance(raw, bytes):
            raw = raw.decode()
        watermark = float(raw)
    except (TypeError, ValueError) as exc:
        raise CorruptRevocationEntry(raw, field="watermark") from exc
    if not math.isfinite(watermark):
        raise CorruptRevocationEntry(raw, field="watermark")
    if basis.precise:
        return basis.seconds <= watermark
    return int(basis.seconds) <= math.floor(watermark)


class RedisTokenRevocationStore(SequentialRevocationState, ITokenRevocationStore):
    """Redis implementation of token revocation store.

    The CLOUD store. Standalone no longer constructs it: its cache is the
    in-process FakeRedis, which a restart forgets, so
    ``create_token_revocation_store`` gives standalone the durable SQL store
    instead (#828). It still WORKS against FakeRedis — every test in the shared
    contract suite drives this class over one — but no deployment resolves that
    pairing any more.

    Redis TTL handles expiration. Reads are free here, so both revocation arms
    are answered by the sequential mixin rather than batched.
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

    #: Write the entry if absent, else only lengthen it (#2764): a revocation
    #: never shortens a live entry, and a claim's value stays a claim's. One
    #: script, so the entry cannot lapse between the two steps. TTL, not
    #: ``EXPIRE GT``, which needs Redis 7.
    _REVOKE_NEVER_SHORTER = """
    if redis.call('SET', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2]) then return 1 end
    local left = redis.call('TTL', KEYS[1])
    if left >= 0 and left < tonumber(ARGV[2]) then
        redis.call('EXPIRE', KEYS[1], ARGV[2])
    end
    return 0
    """

    async def add_revoked_token(self, jti: str, ttl: int) -> None:
        key = self._make_key(jti)
        await self.redis.eval(self._REVOKE_NEVER_SHORTER, 1, key, "revoked", ttl)

    async def is_revoked(self, jti: str) -> bool:
        key = self._make_key(jti)
        return await self.redis.exists(key) > 0

    async def claim_token(self, jti: str, ttl: int) -> RefreshClaim:
        key = self._make_key(jti)
        # A per-call nonce, read back on nil (#2764). redis-py retries a command
        # whose reply was lost, so an applied SET NX can be resent and answer
        # nil to the caller that wrote it. ``is_revoked`` reads EXISTS, so the
        # value is free to identify the writer.
        nonce = _CLAIM_PREFIX + secrets.token_hex(16)
        if await self.redis.set(key, nonce, nx=True, ex=ttl):
            return RefreshClaim.CLAIMED
        stored = await self.redis.get(key)
        if isinstance(stored, bytes):
            stored = stored.decode()
        if stored == nonce:
            return RefreshClaim.CLAIMED
        if stored is not None and stored.startswith(_CLAIM_PREFIX):
            return RefreshClaim.ALREADY_REDEEMED
        return RefreshClaim.REVOKED

    #: Keep a stored watermark above the new one and at most the window
    #: (ARGV[3]) ahead of the later of the new one and Redis's own clock
    #: (``TIME``), with the new TTL (#2050). Kept only when it is a string in
    #: strict decimal-float form, which Lua's ``tonumber`` and Python's
    #: ``float`` read alike (``tonumber`` also reads hex and padding), and
    #: finite. Anything else, a non-string key included, is replaced. The kept
    #: string is written back as it was, so no precision is lost. SET EX, not
    #: SETEX: fakeredis floors a SETEX deadline to the second (#2400).
    _RAISE_WATERMARK = """
    local raw = false
    if redis.call('TYPE', KEYS[1]).ok == 'string' then
        raw = redis.call('GET', KEYS[1])
    end
    local stored = nil
    for _, form in ipairs({'^[+-]?%d+%.?%d*', '^[+-]?%.%d+'}) do
        local _, last = string.find(raw or '', form)
        if last then
            local rest = string.sub(raw, last + 1)
            if rest == '' or string.find(rest, '^[eE][+-]?%d+$') then
                stored = tonumber(raw)
            end
            break
        end
    end
    local new = tonumber(ARGV[1])
    local time = redis.call('TIME')
    local bound = math.max(new, tonumber(time[1]) + tonumber(time[2]) / 1e6)
    if not (stored and stored > new and stored <= bound + tonumber(ARGV[3])) then
        raw = ARGV[1]
    end
    redis.call('DEL', KEYS[1])
    redis.call('SET', KEYS[1], raw, 'EX', ARGV[2])
    return 0
    """

    async def revoke_user_tokens_before(
        self, user_id: str, revoked_at: float, ttl: int, *, sweep: bool = True
    ) -> None:
        key = self._make_user_key(user_id)
        # Stored at full precision: a precise basis is compared against it
        # unfloored (#2050). Redis has no sweep: TTL expires the keys.
        window = REVOCATION_MONOTONE_WINDOW.total_seconds()
        await self.redis.eval(
            self._RAISE_WATERMARK, 1, key, str(revoked_at), ttl, str(window)
        )

    async def is_user_revoked(self, user_id: str, basis: RevocationBasis) -> bool:
        key = self._make_user_key(user_id)
        raw = await self.redis.get(key)
        if raw is None:
            return False
        # A malformed watermark refuses, as ``CorruptRevocationEntry`` — the
        # request path reports it as REVOCATION_STATE_UNKNOWN and the
        # generators fail closed on it like any other error (#1478). Either
        # is preferable to silently guessing at a corrupt value here. The
        # decode lives inside the helper for the same reason the parse does:
        # a body that is not valid UTF-8 is corruption too.
        return _watermark_covers(raw, basis)

    async def cleanup_expired(self) -> int:
        return 0  # Redis handles expiration automatically

    async def count_user_watermarks(self) -> int:
        """Count the live ``user:`` keys with SCAN (never ``KEYS``, which blocks).

        The prefix is configurable, so it is glob-escaped. SCAN may return a key
        more than once, so the keys are collected into a set before counting.
        The ``jti:`` namespace sits beside ``user:`` and never matches it.
        """
        pattern = escape_glob(self._user_key_prefix) + "*"
        keys = set()
        async for key in self.redis.scan_iter(match=pattern, count=1000):
            keys.add(key)
        return len(keys)


class SqlTokenRevocationStore(ITokenRevocationStore):
    """Database implementation of the token revocation store (#828).

    Used where the cache client does not outlive the process — standalone,
    whose Redis is the in-process FakeRedis. Same two granularities,
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
    or not anything has deleted it — the property Redis gets from TTL.

    Rows are then reclaimed opportunistically, by **the writes that ADD an
    entry** (``add_revoked_token`` and ``revoke_user_tokens_before``, both
    through ``_upsert`` — a revocation's final stamps skip it, its first write
    having swept — and ``claim_token``) and by the explicit
    ``cleanup_expired``. That bounds
    the table without a scheduled job that could be forgotten at wiring time —
    and nothing calls ``cleanup_expired``, in this class or the Redis one, so
    the opportunistic path is the one that runs.

    The cost is bounded either way: measured over 20,000 live 90-day
    watermarks, a revocation write carrying the sweep costs 3.81ms, and the one
    write that reclaimed 5,000 elapsed rows cost 23ms (#828 review). It is an
    indexed range delete, not a scan.

    **Session per operation**, as the sibling sessionless repositories: the
    store is a process-wide singleton built at composition time, long before
    any request, so it cannot hold one.
    """

    def __init__(self, session_factory=None, read_timeout_seconds: float = 0.0):
        """
        Args:
            session_factory: Async context manager yielding an ``AsyncSession``.
                Defaults to the application's ``get_db_session``. A parameter
                so a caller can bind a different database — which is also how
                a restart is simulated in tests, by building a second store
                over a second engine on the same file.
            read_timeout_seconds: The deadline on ``revocation_state``, the
                read every authenticated request makes
                (``REVOCATION_READ_TIMEOUT_SECONDS``); ``0`` sets none.
        """
        self._session_factory = session_factory or get_db_session
        self.read_timeout_seconds: Optional[float] = (
            read_timeout_seconds if read_timeout_seconds > 0 else None
        )

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
        self, scope: str, subject: str, revoked_at: float, ttl: int, sweep=True
    ) -> None:
        async with self._session_factory() as session:
            if sweep:
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
            # A stored watermark above the written one, and at most
            # ``REVOCATION_MONOTONE_WINDOW`` ahead of the later of the written
            # one and the database's own clock, is kept (#2050); anything else
            # is overwritten. The upper bound also refuses what is not a finite
            # number: NaN sorts above every number on PostgreSQL, and text and
            # blobs above every number on SQLite. A jti entry is only ever
            # lengthened, and keeps its writer's mark, so a revocation can
            # neither shorten nor relabel a claim (#2764).
            statement = dialect_insert(session, TokenRevocationModel).values(
                scope=scope,
                subject=subject,
                revoked_at=revoked_at,
                expires_at=self._deadline(ttl),
            )
            current = TokenRevocationModel.expires_at
            proposed = statement.excluded.expires_at
            postgresql = session.get_bind().dialect.name == "postgresql"
            if scope == _JTI_SCOPE:
                later = (
                    func.greatest(current, proposed)
                    if postgresql
                    else func.max(current, proposed)
                )
                set_ = {"expires_at": later}
            else:
                stored = TokenRevocationModel.revoked_at
                written = statement.excluded.revoked_at
                window = REVOCATION_MONOTONE_WINDOW.total_seconds()
                if postgresql:
                    db_now = cast(func.extract("epoch", func.clock_timestamp()), Float)
                    newest = func.greatest(written, db_now)
                else:
                    # julianday('now') carries milliseconds; the Unix epoch is
                    # Julian day 2440587.5.
                    db_now = (func.julianday("now") - 2440587.5) * 86400.0
                    newest = func.max(written, db_now)
                keep = and_(stored > written, stored <= newest + window)
                set_ = {
                    "revoked_at": case((keep, stored), else_=written),
                    "expires_at": proposed,
                }
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["scope", "subject"], set_=set_
                )
            )
            # The store owns its unit of work. Leaving the commit to the
            # session factory made durability — the entire point of #828 — an
            # undocumented property of the injected callable: handed the
            # idiomatic ``async_sessionmaker()``, every revocation was dropped
            # on exit — and because ``AuthService._is_revoked`` failed open at
            # the time, the control would simply have been off with a log line
            # as the only signal. (That check fails closed since #1478, but a
            # dropped write is invisible to it either way: there is nothing to
            # read and nothing raises. The commit is what fixes this, not the
            # read posture.)
            await session.commit()

    async def add_revoked_token(self, jti: str, ttl: int) -> None:
        # A jti entry revokes unconditionally, so ``revoked_at`` is no
        # watermark here: it marks the writer, 0 for a revocation and
        # ``_CLAIM_MARK`` for a claim, never a timestamp somebody could mistake
        # for a watermark.
        await self._upsert(_JTI_SCOPE, jti, 0.0, ttl)

    async def claim_token(self, jti: str, ttl: int) -> RefreshClaim:
        async with self._session_factory() as session:
            # The sweep first, in the same transaction, so an elapsed entry for
            # this jti cannot make the insert conflict. Then ONE insert that
            # does nothing on conflict: the database's unique key decides the
            # single winner, and RETURNING says whether this caller's row was
            # written (#2764). ``rowcount`` is not consulted (see ``_sweep``).
            # A loser reads the holder's mark to say who holds the entry.
            await self._sweep(session)
            statement = dialect_insert(session, TokenRevocationModel).values(
                scope=_JTI_SCOPE,
                subject=jti,
                revoked_at=_CLAIM_MARK,
                expires_at=self._deadline(ttl),
            )
            result = await session.execute(
                statement.on_conflict_do_nothing(
                    index_elements=["scope", "subject"]
                ).returning(TokenRevocationModel.subject)
            )
            if result.first() is not None:
                outcome = RefreshClaim.CLAIMED
            else:
                mark = await session.scalar(
                    select(TokenRevocationModel.revoked_at).where(
                        TokenRevocationModel.scope == _JTI_SCOPE,
                        TokenRevocationModel.subject == jti,
                    )
                )
                outcome = (
                    RefreshClaim.ALREADY_REDEEMED
                    if mark == _CLAIM_MARK
                    else RefreshClaim.REVOKED
                )
            await session.commit()
            return outcome

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
        self, user_id: str, revoked_at: float, ttl: int, *, sweep: bool = True
    ) -> None:
        await self._upsert(_USER_SCOPE, user_id, revoked_at, ttl, sweep)

    async def is_user_revoked(self, user_id: str, basis: RevocationBasis) -> bool:
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
            # The same helper as the Redis arm, so the two arms cannot drift on
            # either the rule or what a corrupt value does. A NULL
            # ``revoked_at`` lands here as ``None`` and refuses rather than
            # raising a bare TypeError.
            return _watermark_covers(row[0], basis)

    async def revocation_state(self, jti, user_id, basis):
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

        **Bounded by** ``read_timeout_seconds`` (#2280): queue wait, ping,
        connect and query together. Past it the read raises ``TimeoutError``,
        which ``AuthService._is_revoked`` classifies as a store read failure, so
        the request fails closed with 503 (#1478) instead of waiting on a
        database that stopped answering. The PostgreSQL engine aborts the
        connection the cancellation lands on (``pg_connection``), so the
        deadline frees it rather than leaving it to the server.
        """
        async with asyncio.timeout(self.read_timeout_seconds):
            return await self._revocation_state(jti, user_id, basis)

    async def _revocation_state(self, jti, user_id, basis):
        if not jti and not user_id:
            return False, False

        wanted = []
        if jti:
            wanted.append(
                (TokenRevocationModel.scope == _JTI_SCOPE)
                & (TokenRevocationModel.subject == jti)
            )
        if user_id and basis is not None:
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
                elif basis is not None:
                    # The rule ``is_user_revoked`` applies, through the same
                    # helper. THIS is the site the authenticated request path
                    # reaches, so it is the one where a corrupt row used to
                    # produce a bare ValueError/TypeError and a generic 401
                    # (#1478).
                    user_revoked = _watermark_covers(revoked_at, basis)
            if token_revoked:
                return True, False
            return False, user_revoked

    async def count_user_watermarks(self) -> int:
        """Live per-user rows: ``scope='user'`` and not yet elapsed. No sweep."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(func.count())
                .select_from(TokenRevocationModel)
                .where(
                    TokenRevocationModel.scope == _USER_SCOPE,
                    TokenRevocationModel.expires_at > datetime.now(timezone.utc),
                )
            )
            return int(result.scalar_one())

    async def cleanup_expired(self) -> int:
        async with self._session_factory() as session:
            swept = await self._sweep(session)
            await session.commit()
            return swept
