"""The scoped wipe covers every Redis family FaultMaven writes (#2958).

The keys are written by the real writers (or, where a writer is a route or
middleware too heavy to drive here, composed from the constant the writer builds
from) on one ``FakeServer``, then surveyed and wiped through the command's own
functions. On ``main`` before the fix, ``oauth:claim:``, the case leases and
tombstones, the turn/KB/middleware claims and the config version were "under no
known FaultMaven prefix": left by the scoped wipe while ``--verify`` passed.
Keys no bootable process writes (``auth:``, fm#1647) or that only share a stem
with a FaultMaven key stay another application's, and survive.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fakeredis import FakeServer
from fakeredis.aioredis import FakeRedis

import faultmaven.config.llm_config_overrides as llm_config
import faultmaven.infrastructure.persistence.llm_config_repository as config_repo
from faultmaven.cli import wipe_deployment as wd
from faultmaven.config.idempotency_key import IDEMPOTENCY_CLAIM_PREFIX
from faultmaven.infrastructure import redis_client
from faultmaven.infrastructure.concurrency.inflight import CaseDeleteGate, hold_case
from faultmaven.infrastructure.concurrency.owned_claim import take_claim
from faultmaven.infrastructure.security.case_redaction import CaseRedactionContext
from faultmaven.infrastructure.security.pseudonym_key import (
    verify_pseudonym_key_agreement,
)
from faultmaven.models.common import SessionContext
from faultmaven.modules.auth.contracts import OAuthCodeDTO
from faultmaven.modules.auth.infrastructure.repositories.oauth_code_repository import (
    RedisOAuthCodeRepository,
)
from faultmaven.modules.auth.infrastructure.stores.redis_session_store import (
    RedisSessionStore,
)
from faultmaven.modules.auth.infrastructure.stores.sso_ephemeral_store import (
    SSOEphemeralStore,
)
from faultmaven.modules.case.api import turn_idempotency
from faultmaven.modules.knowledge.api import request_idempotency
from faultmaven.modules.knowledge.domain.request_receipts import KbReceiptKey

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Keys another application on a shared Redis may hold: no bootable FaultMaven
#: process writes ``auth:`` (the Redis user store, fm#1647), and the others
#: only share a stem with an exact key or the registry family.
FOREIGN = [
    "auth:user:x",
    "faultmaven:llm_config:versionX",
    "redaction:pseudonym_key_fingerprintX",
    "redaction:registryX",
]


class _ServerRedis(FakeRedis):
    """Defined outside ``fakeredis``, so ``is_fakeredis`` reads it as real."""


class _OneIpSanitizer:
    """Finds one IP, as the real sanitizer would, into the case's registry."""

    def sanitize_text_with_registry(self, text, registry):
        registry.setdefault("IP_ADDRESS", {})["10.0.0.7"] = "<IP_ADDRESS_1>"
        return text.replace("10.0.0.7", "<IP_ADDRESS_1>")


def _settings(revocation="revoked:token:"):
    return SimpleNamespace(
        security=SimpleNamespace(token_revocation_prefix=revocation),
        database=SimpleNamespace(redis_url=None, redis_host="redis.internal"),
    )


def _clients_on(server, monkeypatch) -> AsyncMock:
    """A new client per call, as survey and wipe each close theirs."""
    factory = AsyncMock(
        side_effect=lambda *a, **k: _ServerRedis(server=server, decode_responses=True)
    )
    monkeypatch.setattr(redis_client, "get_async_redis_client", factory)
    return factory


async def _write_every_family(redis, monkeypatch, held: list) -> None:
    """Write one key per family; each lease's release joins ``held`` as taken."""
    sessions = RedisSessionStore(redis, session_ttl_seconds=600)
    await sessions.save(SessionContext(session_id="s-1", user_id="u-1"))
    await sessions.index_session_by_client("u-1", "client-1", "s-1", 600)
    await sessions.increment_counter("case_seq:u-1:2026-11-26", ttl=600)

    sso = SSOEphemeralStore(redis)
    await sso.put_state("st-1", {"n": 1}, 60)
    await sso.put_login("lg-1", {"n": 1}, 60)

    oauth = RedisOAuthCodeRepository(redis)
    await oauth.save_code(
        OAuthCodeDTO(
            code="code-1",
            user_id="u-1",
            redirect_uri="https://example.invalid/cb",
            code_challenge="c",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
    )
    assert await oauth.claim_code("code-1")

    # A lease and a tombstone exclude each other on one case.
    hold = await hold_case(redis, ("ent-1", "case-a"))
    held.append(hold.release)
    tombstone = await CaseDeleteGate(redis).begin(("ent-1", "case-b"))
    held.append(lambda: tombstone.end(deleted=True))

    turn = await take_claim(
        redis,
        turn_idempotency.claim_name(
            SimpleNamespace(enterprise_id="ent-1", case_id="case-a"), "u-1", "key-0001"
        ),
        ttl_seconds=60,
        purpose="keyed turns",
        backstop="the receipt",
    )
    held.append(turn.release)

    # Config version, through its writer: the DB write and reload are stubbed.
    monkeypatch.setattr(llm_config, "_get_redis", AsyncMock(return_value=redis))
    monkeypatch.setattr(llm_config, "_aclose", AsyncMock())
    monkeypatch.setattr(llm_config, "reload_llm_config", AsyncMock())
    monkeypatch.setattr(llm_config, "_last_applied_version", None)
    monkeypatch.setattr(config_repo, "set_overrides", AsyncMock())
    await llm_config.save_and_reload({"openai_model": "m"}, user_id="u-1")

    # The redaction registry and the key-fingerprint agreement, through theirs.
    registry = CaseRedactionContext("case-a", _OneIpSanitizer(), redis_client=redis)
    assert registry.sanitize("from 10.0.0.7") == "from <IP_ADDRESS_1>"
    await registry.save()
    await verify_pseudonym_key_agreement(b"k" * 32, redis)

    # Composed from the constant: their writers are a route and a middleware.
    kb_key = KbReceiptKey("ent-1", "u-1", "upload", "key-0002", "fp")
    await redis.set(request_idempotency.claim_name(kb_key), "1", ex=60)
    await redis.set(f"{IDEMPOTENCY_CLAIM_PREFIX}key-0003:abc", "1", ex=60)


async def test_a_scoped_wipe_removes_every_family_and_spares_foreign_keys(
    monkeypatch,
):
    server = FakeServer()
    _clients_on(server, monkeypatch)
    writer = _ServerRedis(server=server, decode_responses=True)
    settings = _settings()
    held: list = []
    try:
        await _write_every_family(writer, monkeypatch, held)
        ours = sorted(await writer.keys("*"))
        assert any(k.startswith("session:case_seq:") for k in ours), ours
        for key in FOREIGN:
            await writer.set(key, "theirs")

        # While the leases are held: every FaultMaven key is under a namespace.
        _counts, unmatched = wd.classify_redis_keys(
            ours + FOREIGN, wd.redis_namespaces(settings)
        )
        assert sorted(unmatched) == sorted(FOREIGN), unmatched

        before = await wd.survey_redis(settings, verify=True)
        assert before.unreachable is None, before.unreachable
        assert before.residue, before.render()
        assert any(
            f"{len(FOREIGN)} key(s) under no known FaultMaven prefix" in d
            for d in before.detail
        ), before.detail
        for line in (
            "oauth:claim:* : 1",
            "case-inflight:* : 1",
            "case-deleting:* : 1",
            "turn-inflight:* : 1",
            "kb-inflight:* : 1",
            "idem-inflight:* : 1",
            "redaction:registry:* : 1",
            "faultmaven:llm_config:version : 1",
            "redaction:pseudonym_key_fingerprint : 1",
        ):
            assert line in before.detail, (line, before.detail)

        deleted, error = await wd.wipe_redis(settings, all_keys=False)
        assert error is None
        assert deleted == len(ours)
        assert sorted(await writer.keys("*")) == sorted(FOREIGN)

        after = await wd.survey_redis(settings, verify=True)
        assert after.residue == [], after.render()
    finally:
        for release in reversed(held):
            await release()
        await writer.aclose()


async def test_a_namespace_failure_reads_unreachable_and_opens_no_client(
    monkeypatch,
):
    """The namespaces import ~85 modules; a failure there is reported, before
    any client is opened (so none is left open)."""
    factory = _clients_on(FakeServer(), monkeypatch)
    monkeypatch.setattr(
        wd, "redis_namespaces", lambda _s: (_ for _ in ()).throw(ImportError("x"))
    )

    surface = await wd.survey_redis(_settings(), verify=True)
    deleted, error = await wd.wipe_redis(_settings(), all_keys=False)

    assert surface.unreachable == "ImportError: x"
    assert (deleted, error) == (0, "ImportError: x")
    factory.assert_not_awaited()


@pytest.mark.parametrize("blank", ["", None])
def test_a_blank_revocation_prefix_never_enters_the_set(blank):
    """``""*`` would match every key on the Redis a scoped wipe exists to spare."""
    prefixes, _exact = wd.redis_namespaces(_settings(revocation=blank))
    assert "" not in prefixes and None not in prefixes
    assert all(prefixes), prefixes


def test_guard_redis_namespaces_loads_no_api_layer_in_a_fresh_interpreter():
    """A guard, passing by construction today: the wipe runs in a one-off pod,
    and importing the families' defining modules must not drag in FastAPI, a
    middleware, a routes module or the container."""
    probe = (
        "import json, sys\n"
        "from types import SimpleNamespace as N\n"
        "from faultmaven.cli import wipe_deployment as wd\n"
        "wd.redis_namespaces(N(security=N(token_revocation_prefix='r:')))\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    loaded = json.loads(out)
    heavy = [
        m
        for m in loaded
        if m == "fastapi"
        or m.startswith(
            ("fastapi.", "faultmaven.api.middleware", "faultmaven.container")
        )
        or m.endswith(".api.routes")
        or ".api.routes." in m
    ]
    assert "faultmaven.cli.wipe_deployment" in loaded
    assert heavy == [], heavy
