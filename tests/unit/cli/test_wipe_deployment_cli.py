"""``fm-wipe-deployment``'s guards, classification and verdicts (#819).

The command replaces two scripts that hardcoded ``data/faultmaven.db`` and
printed a clean success report against a stale local SQLite file while the
PostgreSQL deployment sat untouched. The failure being guarded is therefore not
"the delete did not run" but **"the delete ran somewhere else, or somewhere
unreadable, and the operator believed it"** — so these tests concentrate on the
refusals, on the table classification staying complete as migrations land, and
on the two ways a verification is allowed to fail.
"""

from __future__ import annotations

import sys

import pytest

from faultmaven.cli import wipe_deployment as wd

pytestmark = pytest.mark.unit


def _run_main(argv):
    original = sys.argv
    sys.argv = argv
    try:
        wd.main()
    finally:
        sys.argv = original


class _Ns:
    """A settings namespace built from keywords."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Settings(_Ns):
    """Enough of FaultMavenSettings for the pure-decision helpers."""

    def __init__(self, **overrides):
        from faultmaven.config.settings import Environment

        super().__init__(
            security=_Ns(token_revocation_prefix="revoked:token:"),
            server=_Ns(environment=Environment.PRODUCTION),
            database=_Ns(
                redis_url=None,
                redis_host="redis.internal",
                redis_port=6379,
                redis_db=0,
                chromadb_url="",
                chromadb_kb_persist_dir="./data/chroma-kb",
                chromadb_evidence_persist_dir="./data/chroma-evidence",
            ),
            evidence_storage=_Ns(
                evidence_storage_root="./data/evidence",
                s3_bucket_name=None,
                s3_endpoint_url=None,
                s3_key_prefix="",
            ),
        )
        self.__dict__.update(overrides)


# ---------------------------------------------------------------------------
# argparse-level guards — these must fire before anything connects anywhere
# ---------------------------------------------------------------------------


def test_wipe_without_yes_exits_1(capsys):
    with pytest.raises(SystemExit) as exc:
        _run_main(["fm-wipe-deployment", "--wipe"])
    assert exc.value.code == 1
    assert "--yes" in capsys.readouterr().out


def test_wipe_without_confirm_target_exits_1(capsys):
    """--yes alone is not enough: the operator must name the resolved target."""
    with pytest.raises(SystemExit) as exc:
        _run_main(["fm-wipe-deployment", "--wipe", "--yes"])
    assert exc.value.code == 1
    assert "--confirm-target" in capsys.readouterr().out


def test_wipe_and_verify_together_is_a_usage_error():
    """They are consecutive steps; running both in one invocation would verify
    a slate the same process had just changed."""
    with pytest.raises(SystemExit) as exc:
        _run_main(["fm-wipe-deployment", "--wipe", "--yes", "--verify"])
    assert exc.value.code == 2


def test_unknown_flag_is_an_argparse_error():
    with pytest.raises(SystemExit) as exc:
        _run_main(["fm-wipe-deployment", "--drop-database"])
    assert exc.value.code == 2


def test_help_works_without_docstrings(capsys):
    """``python -OO`` strips ``__doc__``; argparse's description is a literal."""
    assert wd._SUMMARY and isinstance(wd._SUMMARY, str)
    with pytest.raises(SystemExit) as exc:
        _run_main(["fm-wipe-deployment", "--help"])
    assert exc.value.code == 0
    assert "--confirm-target" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Table classification
# ---------------------------------------------------------------------------


def test_every_orm_table_is_classified():
    """The gate that keeps the verification honest as the schema grows.

    A migration that adds a table without classifying it here would leave that
    table outside ``--verify`` — the wipe would report a clean slate while the
    new table still held the old deployment's rows. That must fail here, not in
    production.
    """
    assert wd.unclassified_tables() == frozenset(), (
        "these tables are in no bucket, so --verify ignores them: "
        f"{sorted(wd.unclassified_tables())}. Add each to MUST_BE_EMPTY, "
        "MUST_BE_SEEDED or INFORMATIONAL in faultmaven/cli/wipe_deployment.py"
    )


def test_the_buckets_do_not_overlap():
    """A table in two buckets would be asserted both empty and non-empty."""
    assert not (wd.MUST_BE_EMPTY & wd.MUST_BE_SEEDED)
    assert not (wd.MUST_BE_EMPTY & wd.INFORMATIONAL)
    assert not (wd.MUST_BE_SEEDED & wd.INFORMATIONAL)


def test_the_migration_seeded_tables_are_the_ones_that_break_sso_login():
    """Pinned deliberately. ``roles``/``permissions``/``role_permissions`` come
    from migration 029's bare ``bulk_insert``, which Alembic will not re-run on
    an already-stamped database: delete them and every SSO login fails closed on
    the membership write's ``role_id`` FK. ``enterprises`` is migration 006's
    default row. Dropping any of these from MUST_BE_SEEDED would make --verify
    accept a DELETE-based wipe as clean.
    """
    assert wd.MUST_BE_SEEDED == frozenset(
        {"roles", "permissions", "role_permissions", "enterprises"}
    )


def test_identity_and_untenanted_tables_must_be_emptied():
    """``sso_org_mappings`` is untenanted by design (#869), which is exactly why
    a tenant-scoped wipe misses it and the next SSO login lands in a stale
    tenant. The operator-access tables are append-only by trigger, so their
    presence after a "wipe" proves DELETE was used instead of a recreate."""
    for table in (
        "users",
        "organizations",
        "organization_members",
        "sso_org_mappings",
        "oauth_authorization_codes",
        "operator_access_audit",
        "operator_access_grants",
    ):
        assert table in wd.MUST_BE_EMPTY


def test_knowledge_items_is_never_asserted():
    """0 after the migrations and ~91 after kb_seed are both correct at their own
    point in the sequence, so asserting either way would fail a good wipe."""
    assert "knowledge_items" in wd.INFORMATIONAL


# ---------------------------------------------------------------------------
# Protected database
# ---------------------------------------------------------------------------


async def test_the_slack_database_is_refused_outright(monkeypatch, capsys):
    """Dropping ``faultmaven_slack`` uninstalls the bot from every workspace, so
    a copied-and-edited DSN pointing at it must not even reach the preflight."""

    class _URL:
        database = "faultmaven_slack"
        host = "pg"

    class _Engine:
        url = _URL()

    monkeypatch.setattr(
        "faultmaven.infrastructure.persistence.database.get_engine",
        lambda: _Engine(),
    )
    called = False

    async def _never(*a, **k):
        nonlocal called
        called = True

    monkeypatch.setattr(wd, "preflight_database_role", _never)

    code = await wd.wipe_deployment(
        mode="inventory", confirm_target=None, redis_all_keys=False
    )
    assert code == 1
    assert not called, "the refusal must precede any database probing"
    out = capsys.readouterr().out
    assert "faultmaven_slack" in out
    assert "slack_installations" in out, "the refusal must say what it protects"


async def test_an_rls_scoped_role_is_refused(monkeypatch, capsys):
    """A tenant-scoped role would make --verify pass on a database that still
    holds another tenant's data, so it is refused for every mode."""
    from faultmaven.config.deployment_coherence import DeploymentCoherenceError

    class _URL:
        database = "faultmaven"
        host = "pg"

    class _Engine:
        url = _URL()

    monkeypatch.setattr(
        "faultmaven.infrastructure.persistence.database.get_engine",
        lambda: _Engine(),
    )

    async def _refuse():
        raise DeploymentCoherenceError("this connection is using 'faultmaven_app'")

    monkeypatch.setattr(wd, "preflight_database_role", _refuse)

    code = await wd.wipe_deployment(
        mode="verify", confirm_target=None, redis_all_keys=False
    )
    assert code == 1
    assert "faultmaven_app" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Target confirmation
# ---------------------------------------------------------------------------


async def test_a_mismatched_confirm_target_wipes_nothing(monkeypatch, capsys):
    """The core guard against the bug that killed the old scripts: a wipe aimed
    at a target the process is not actually connected to."""

    class _URL:
        database = "faultmaven_rehearsal"
        host = "pg"

    class _Engine:
        url = _URL()

    monkeypatch.setattr(
        "faultmaven.infrastructure.persistence.database.get_engine",
        lambda: _Engine(),
    )

    def _never_vectors(settings):
        raise AssertionError("wipe_vectors must not run on a target mismatch")

    monkeypatch.setattr(wd, "wipe_vectors", _never_vectors)

    code = await wd.run_wipe(
        object(), confirm_target="faultmaven", redis_all_keys=False
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "faultmaven_rehearsal" in out, "the refusal must name what it found"
    assert "Nothing was written" in out


# ---------------------------------------------------------------------------
# Redis key classification
# ---------------------------------------------------------------------------


def test_known_prefixes_are_counted_per_namespace():
    prefixes = ("session:", "revoked:token:", "sso:state:")
    counts, unmatched = wd.classify_redis_keys(
        ["session:a", "session:b", "revoked:token:jti:x", "sso:state:s"], prefixes
    )
    assert counts["session:"] == 2
    assert counts["revoked:token:"] == 1
    assert counts["sso:state:"] == 1
    assert unmatched == []


def test_keys_outside_every_known_prefix_are_reported_not_dropped():
    """The scoped wipe leaves these behind, so they must be visible. Silently
    counting them as swept is how an incomplete wipe reads as a clean one."""
    counts, unmatched = wd.classify_redis_keys(["celery:task:1", "session:a"])
    assert unmatched == ["celery:task:1"]
    assert counts["session:"] == 1


def test_the_prefix_set_covers_every_namespace_the_app_writes():
    """These are the live keyspaces found in the code. A namespace missing here
    is one the scoped wipe silently leaves behind."""
    prefixes = wd.redis_prefixes(_Settings())
    for prefix in (
        "session:",
        "client_index:",  # the session store's own index
        "idempotency:",
        "sso:state:",
        "sso:login:",
        "oauth:code:",
        "password_reset:",
        "case_seq:",
        "redaction:",
    ):
        assert prefix in prefixes, f"{prefix} is written by the app but not swept"


def test_the_revocation_prefix_comes_from_settings_not_a_literal():
    """The app passes ``settings.security.token_revocation_prefix`` to the store,
    so a deployment that overrides it would otherwise keep every revocation
    watermark through a "successful" wipe."""
    settings = _Settings()
    settings.security.token_revocation_prefix = "custom:revoked:"
    prefixes = wd.redis_prefixes(settings)
    assert "custom:revoked:" in prefixes
    assert "revoked:token:" not in prefixes


def test_the_protection_namespaces_are_included():
    """Rate-limit and dedup keys are built from the protection presets'
    redis_key_prefix, not a literal."""
    prefixes = wd.redis_prefixes(_Settings())
    assert any(p.endswith(":rl") for p in prefixes), prefixes
    assert any(p.endswith(":dedup") for p in prefixes), prefixes


def test_every_preset_protection_namespace_is_included_not_just_the_resolved_one():
    """Both presets' namespaces are swept regardless of this process's
    ENVIRONMENT.

    Asserting the *specific* spellings, because ``any(endswith(":rl"))`` is
    satisfied by a single preset and so cannot fail on the bug this covers.
    """
    from faultmaven.config.protection import ALL_REDIS_KEY_PREFIXES

    prefixes = wd.redis_prefixes(_Settings())
    for key_prefix in ALL_REDIS_KEY_PREFIXES:
        assert f"{key_prefix}:rl" in prefixes, prefixes
        assert f"{key_prefix}:dedup" in prefixes, prefixes


@pytest.mark.parametrize("environment", ["development", "production", "staging"])
def test_production_rate_limit_keys_classify_under_any_resolved_environment(
    environment,
):
    """fm#1052 regression, reproducing the live #819 cutover failure.

    The wipe runs with the API scaled down, so it does NOT run in the API pod
    and ``ENVIRONMENT`` is typically unset — resolving to ``development`` while
    the keys on the server were written by the production preset. Classifying
    against only the resolved preset reported live rate-limit state as "under no
    known FaultMaven prefix", and because ``--verify`` judges residue against
    the same set the wipe deletes, both were wrong together.
    """
    from faultmaven.config.settings import Environment

    settings = _Settings()
    settings.server.environment = Environment(environment)

    observed = [
        "faultmaven_prod:rl:global:10.244.226.131",
        "faultmaven_prod:rl:global:10.244.236.202",
        "faultmaven_dev:dedup:abc123",
        # Staging is the third namespace and has no preset of its own: it runs
        # the production preset re-pointed by setup_protection_middleware. So it
        # is invisible to anything enumerating preset constructors, and was the
        # remaining half of this bug after the first fix.
        "faultmaven_staging:rl:global:10.244.1.7",
        "faultmaven_staging:dedup:xyz789",
    ]
    _counts, unmatched = wd.classify_redis_keys(observed, wd.redis_prefixes(settings))

    assert unmatched == [], (
        "protection keys written under a different ENVIRONMENT must still be "
        f"swept and verified; resolved={environment}"
    )


@pytest.mark.parametrize(
    "environment", ["development", "production", "staging", "an-unrecognised-value"]
)
def test_the_namespace_the_middleware_actually_installs_is_one_the_wipe_knows(
    environment,
):
    """Bind the wipe's known set to what protection ACTUALLY writes.

    The set cannot be discovered by calling the preset constructors: staging
    runs the production preset and is re-pointed afterwards by
    ``setup_protection_middleware``, so enumerating presets finds two
    namespaces while three exist. This asserts the real resolution path for
    every environment, so a fourth namespace introduced the same way fails
    here instead of surviving a "successful" wipe.
    """
    from faultmaven.api.protection import setup_protection_middleware
    from faultmaven.config.protection import ALL_REDIS_KEY_PREFIXES

    installed: list = []

    class _App:
        def add_middleware(self, _cls, **kwargs):
            installed.append(kwargs["settings"])

    setup_protection_middleware(_App(), environment=environment)

    assert installed, "no middleware was installed, so nothing was asserted"
    prefix = installed[0].redis_key_prefix
    assert prefix in ALL_REDIS_KEY_PREFIXES, (
        f"ENVIRONMENT={environment} writes keys under {prefix!r}, which "
        "fm-wipe-deployment does not know about — the scoped wipe would leave "
        "them and --verify would still pass"
    )


def test_unrelated_third_party_keys_are_still_reported_as_unknown():
    """The widened prefix set must not swallow keys FaultMaven never wrote —
    otherwise 'no residue' stops meaning anything."""
    _counts, unmatched = wd.classify_redis_keys(
        ["celery:task:1", "faultmaven_prod:rl:global:1"], wd.redis_prefixes(_Settings())
    )
    assert unmatched == ["celery:task:1"]


# ---------------------------------------------------------------------------
# Verification verdicts
# ---------------------------------------------------------------------------


def _clean(name="Surface"):
    return wd.Surface(name=name, target="resolved", detail=["all empty"])


async def _verify_with(monkeypatch, surfaces):
    """Drive ``run_verify`` with fixed surfaces, bypassing all I/O."""
    db, vectors, objects, redis = surfaces

    async def _db(*, verify):
        return db

    async def _vectors(settings, *, verify):
        return vectors

    async def _objects(settings, *, verify):
        return objects

    async def _redis(settings, *, verify):
        return redis

    monkeypatch.setattr(wd, "survey_database", _db)
    monkeypatch.setattr(wd, "survey_vectors", _vectors)
    monkeypatch.setattr(wd, "survey_objects", _objects)
    monkeypatch.setattr(wd, "survey_redis", _redis)
    return await wd.run_verify(object())


async def test_a_fully_clean_slate_verifies(monkeypatch, capsys):
    code = await _verify_with(monkeypatch, [_clean() for _ in range(4)])
    assert code == 0
    assert "Clean slate" in capsys.readouterr().out


async def test_residue_fails_verification(monkeypatch, capsys):
    dirty = wd.Surface(
        name="Database", target="pg/faultmaven", residue=["users still holds 2 row(s)"]
    )
    code = await _verify_with(monkeypatch, [dirty, _clean(), _clean(), _clean()])
    assert code == 5
    assert "NOT CLEAN" in capsys.readouterr().out


async def test_an_uninspectable_surface_fails_verification(monkeypatch, capsys):
    """The heart of it. A surface nobody could read has produced no evidence of
    cleanliness, and reporting success on its silence is exactly the bug the
    deleted scripts shipped. Inconclusive is a failure, not a pass.
    """
    blind = wd.Surface(
        name="Vector store",
        target="external server https://chroma:8000",
        unreachable="ConnectionError: refused",
    )
    code = await _verify_with(monkeypatch, [_clean(), blind, _clean(), _clean()])
    assert code == 5
    out = capsys.readouterr().out
    assert "INCONCLUSIVE" in out
    assert "could not be inspected" in out


def test_an_unreachable_surface_renders_as_not_inspected():
    """Rendering must not let an unreachable surface look empty."""
    rendered = wd.Surface(
        name="Object storage", target="s3: bucket=x", unreachable="AccessDenied"
    ).render()
    assert "NOT INSPECTED" in rendered
    assert "AccessDenied" in rendered


# ---------------------------------------------------------------------------
# Database survey
# ---------------------------------------------------------------------------


async def test_an_unreachable_database_is_reported_not_raised(monkeypatch):
    """An unhandled exception here would exit 1 — the code meaning "refused,
    nothing written" — making a database this command could not read look like a
    clean refusal. It must surface as uninspected, which --verify scores as
    INCONCLUSIVE (5) rather than passing or masquerading as a refusal.
    """

    class _URL:
        database = "faultmaven"
        host = "pg"

    class _Engine:
        url = _URL()
        dialect = type("D", (), {"name": "postgresql"})()

        def connect(self):
            raise OSError("connection refused")

    monkeypatch.setattr(
        "faultmaven.infrastructure.persistence.database.get_engine",
        lambda: _Engine(),
    )

    surface = await wd.survey_database(verify=True)
    assert surface.unreachable is not None
    assert "connection refused" in surface.unreachable
    assert (
        surface.residue == []
    ), "an unreadable database yields no clean-or-dirty claim"


class _FakeConn:
    """An async connection over a fixed {table: count-or-raise} map.

    ``None`` for a table means ``COUNT(*)`` raises for it — the case that used to
    poison every later count on PostgreSQL.
    """

    def __init__(self, counts: dict, present: set[str] | None = None, revision="rev1"):
        self._counts = counts
        self._present = present if present is not None else set(counts)
        self._revision = revision
        self.nested_entered = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def scalar(self, statement):
        sql = str(statement)
        if "alembic_version" in sql:
            return self._revision
        table = sql.rsplit(" ", 1)[-1]
        value = self._counts.get(table)
        if value is None:
            raise RuntimeError(f"count failed for {table}")
        return value

    def begin_nested(self):
        self.nested_entered += 1
        return _FakeConn._Savepoint()

    class _Savepoint:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def run_sync(self, fn):
        return set(self._present)


class _FakeEngine:
    def __init__(self, conn, database="faultmaven", dialect="postgresql"):
        self._conn = conn
        self.url = _Ns(database=database, host="pg")
        self.dialect = _Ns(name=dialect)

    def connect(self):
        return self._conn


def _install_engine(monkeypatch, engine):
    monkeypatch.setattr(
        "faultmaven.infrastructure.persistence.database.get_engine", lambda: engine
    )
    monkeypatch.setattr(wd, "_alembic_head", lambda: "rev1")


async def test_one_failed_count_does_not_poison_the_others(monkeypatch):
    """The PostgreSQL failure mode. All 38 counts share one connection, and a
    single failed statement aborts the surrounding transaction — so every later
    COUNT(*) raised InFailedSqlTransaction and was reported as uncountable,
    which also *suppressed* the real "still holds N rows" residue for tables that
    did hold rows. Each count now runs in its own SAVEPOINT.
    """
    counts = {t: 0 for t in wd.MUST_BE_EMPTY | wd.MUST_BE_SEEDED | wd.INFORMATIONAL}
    counts["causal_edges"] = None  # raises — alphabetically early
    counts["users"] = 7  # must still be seen as residue
    for seeded in wd.MUST_BE_SEEDED:
        counts[seeded] = 3
    conn = _FakeConn(counts)
    _install_engine(monkeypatch, _FakeEngine(conn))

    surface = await wd.survey_database(verify=True)

    assert conn.nested_entered > 1, "each count must be wrapped in its own SAVEPOINT"
    residue = " ".join(surface.residue)
    assert (
        "users still holds 7 row(s)" in residue
    ), "a later table's rows must still be reported after an earlier count failed"
    assert "causal_edges exists but could not be counted" in residue


async def test_inventory_never_claims_all_empty_about_tables_it_could_not_read(
    monkeypatch,
):
    """The mode an operator uses to decide whether to wipe must not make a
    positive emptiness claim about tables it never successfully read."""
    counts = {t: 0 for t in wd.MUST_BE_EMPTY | wd.MUST_BE_SEEDED | wd.INFORMATIONAL}
    counts["evidence"] = None  # exists, count raises
    present = set(counts)
    present.discard("cases")  # does not exist at all
    conn = _FakeConn(counts, present=present)
    _install_engine(monkeypatch, _FakeEngine(conn))

    surface = await wd.survey_database(verify=False)
    rendered = surface.render()

    assert "all empty" not in rendered, rendered
    assert "DO NOT EXIST" in rendered and "cases" in rendered
    assert "COULD NOT BE COUNTED" in rendered and "evidence" in rendered


async def test_a_missing_seed_and_an_absent_table_are_different_findings(monkeypatch):
    """Present-but-empty (a DELETE-based wipe) and not-there-at-all (migrations
    have not run) have different causes and different fixes, so one table must
    not produce both residue lines. Drives survey_database rather than
    re-implementing its classification."""
    counts = {t: 0 for t in wd.MUST_BE_EMPTY | wd.MUST_BE_SEEDED | wd.INFORMATIONAL}
    counts["permissions"] = 0  # present, empty  -> "seeded but EMPTY"
    present = set(counts)
    present.discard("roles")  # absent          -> "does not exist"
    conn = _FakeConn(counts, present=present)
    _install_engine(monkeypatch, _FakeEngine(conn))

    surface = await wd.survey_database(verify=True)

    empty_seed = [r for r in surface.residue if "EMPTY but the migrations seed it" in r]
    absent = [r for r in surface.residue if "does not exist" in r]
    assert any("permissions" in r for r in empty_seed)
    assert any("roles" in r for r in absent)
    assert not any(
        "roles" in r for r in empty_seed
    ), "an absent table must not also be reported as an emptied seed"


async def test_an_out_of_date_schema_is_residue(monkeypatch):
    counts = {t: 0 for t in wd.MUST_BE_EMPTY | wd.MUST_BE_SEEDED | wd.INFORMATIONAL}
    for seeded in wd.MUST_BE_SEEDED:
        counts[seeded] = 1
    conn = _FakeConn(counts, revision="older")
    _install_engine(monkeypatch, _FakeEngine(conn))

    surface = await wd.survey_database(verify=True)
    assert any("head is rev1" in r for r in surface.residue)


# ---------------------------------------------------------------------------
# The two false-clean bugs (#1, #2)
# ---------------------------------------------------------------------------


async def test_a_configured_but_unreachable_redis_is_not_reported_as_clean(monkeypatch):
    """The sharpest false-clean. ``get_async_redis_client`` substitutes the
    in-process FakeRedis whenever a *configured* real Redis fails its ping on a
    non-cloud deployment. Trusting ``is_fakeredis`` then printed "nothing durable
    to wipe" and --verify reported Redis clear, while the configured server still
    held every session and revocation watermark.
    """
    from faultmaven.infrastructure import redis_client as rc

    class _Fake:
        __module__ = "fakeredis.aioredis"

        async def aclose(self):
            pass

    monkeypatch.setattr(rc, "REDIS_AVAILABLE", True)
    monkeypatch.setattr(rc, "get_async_redis_client", lambda *a, **k: _ready(_Fake()))

    surface = await wd.survey_redis(_Settings(), verify=True)

    assert surface.unreachable is not None, "a substituted client is NOT a clean Redis"
    assert "redis.internal:6379/0" in surface.unreachable
    assert "NOT be wiped" in surface.unreachable
    assert surface.residue == [], "no verdict may be drawn from an uninspected surface"


async def test_wiping_a_substituted_redis_reports_an_error_not_success(monkeypatch):
    from faultmaven.infrastructure import redis_client as rc

    class _Fake:
        __module__ = "fakeredis.aioredis"

        async def aclose(self):
            pass

    monkeypatch.setattr(rc, "REDIS_AVAILABLE", True)
    monkeypatch.setattr(rc, "get_async_redis_client", lambda *a, **k: _ready(_Fake()))

    deleted, error = await wd.wipe_redis(_Settings(), all_keys=False)
    assert deleted == 0
    assert error and "NOTHING was wiped" in error


async def test_genuine_standalone_fakeredis_is_benign(monkeypatch):
    """The other half of the discrimination: with the redis package absent,
    FakeRedis is the intended backend and there is nothing durable to wipe. That
    must NOT be reported as a failure, or standalone could never verify."""
    from faultmaven.infrastructure import redis_client as rc

    class _Fake:
        __module__ = "fakeredis.aioredis"

    monkeypatch.setattr(rc, "REDIS_AVAILABLE", False)
    monkeypatch.setattr(rc, "get_async_redis_client", lambda *a, **k: _ready(_Fake()))

    surface = await wd.survey_redis(_Settings(), verify=True)
    assert surface.unreachable is None
    assert surface.residue == []
    assert "nothing durable to wipe" in " ".join(surface.detail)


def _ready(value):
    """An awaitable already holding ``value`` — for patching an async factory."""

    async def _await():
        return value

    return _await()


def test_a_local_client_is_not_mistaken_for_the_external_server():
    """``_is_server_backed`` must answer from the client that was created.
    ``chromadb.HttpClient`` raises at construction when the server is down, so
    the factory falls back to a local PersistentClient — and a caller that
    inferred "external" from CHROMADB_URL would sweep the wrong store."""

    class _Local:
        def get_settings(self):
            return _Ns(chroma_server_host=None)

    class _Http:
        def get_settings(self):
            return _Ns(chroma_server_host="chroma.internal")

    class _Opaque:
        def get_settings(self):
            raise RuntimeError("unknown chromadb shape")

    assert wd._is_server_backed(_Local()) is False
    assert wd._is_server_backed(_Http()) is True
    # Unknown shapes answer False: the caller then keeps BOTH local clients
    # rather than collapsing to one and missing a store.
    assert wd._is_server_backed(_Opaque()) is False


async def test_a_chroma_fallback_is_reported_as_not_inspected(monkeypatch):
    """With CHROMADB_URL set but unreachable, the vectors that matter live on a
    server this process never reached. Counting the local trees and reporting
    them clean is the false-clean bug."""
    settings = _Settings()
    settings.database.chromadb_url = "http://chroma.internal:8000"

    class _Local:
        def get_settings(self):
            return _Ns(chroma_server_host=None)

        def list_collections(self):
            return []

    monkeypatch.setattr(wd, "_chroma_clients", lambda s: ([_Local()], "local"))
    monkeypatch.setattr(
        "faultmaven.infrastructure.chroma_client.is_external_chroma_configured",
        lambda s: True,
    )

    surface = await wd.survey_vectors(settings, verify=True)
    assert surface.unreachable is not None
    assert "NOT be wiped" in surface.unreachable


def test_wiping_vectors_refuses_after_a_fallback(monkeypatch):
    """Refuse rather than delete: sweeping the local trees would destroy a store
    the deployment does not read from AND report success, leaving the server's
    collections intact."""
    settings = _Settings()
    settings.database.chromadb_url = "http://chroma.internal:8000"
    deleted_names = []

    class _Local:
        def get_settings(self):
            return _Ns(chroma_server_host=None)

        def list_collections(self):
            return ["faultmaven_kb"]

        def delete_collection(self, name):
            deleted_names.append(name)

    monkeypatch.setattr(wd, "_chroma_clients", lambda s: ([_Local()], "local"))
    monkeypatch.setattr(
        "faultmaven.infrastructure.chroma_client.is_external_chroma_configured",
        lambda s: True,
    )

    deleted, error = wd.wipe_vectors(settings)
    assert deleted == 0
    assert deleted_names == [], "nothing may be deleted from the wrong store"
    assert error and "Refusing to wipe" in error


# ---------------------------------------------------------------------------
# Object storage
# ---------------------------------------------------------------------------


def test_sidecars_are_counted_apart_from_objects():
    """The filesystem backend writes a ``<key>.meta`` beside every file, and
    list_keys walks the tree — so one count roughly doubles the real figure."""
    objects, sidecars = wd.split_sidecar_keys(
        ["case/a.log", "case/a.log.meta", "case/b.txt", "case/b.txt.meta.json"]
    )
    assert objects == ["case/a.log", "case/b.txt"]
    assert len(sidecars) == 2


async def test_a_partial_object_sweep_reports_what_it_deleted(monkeypatch):
    """Returning the literal 0 told the operator nothing was removed when most
    of the store was already gone — a retry decision made on a false premise."""

    class _Backend:
        async def list_keys(self, prefix=""):
            return ["a", "b", "c"]

        async def delete_file(self, key):
            if key == "c":
                raise PermissionError("AccessDenied")
            return True

    monkeypatch.setattr(
        "faultmaven.infrastructure.storage.factory.get_storage_backend",
        lambda *a, **k: _Backend(),
    )

    deleted, error = await wd.wipe_objects()
    assert deleted == 2, "the running count, not 0"
    assert error and "AccessDenied" in error


# ---------------------------------------------------------------------------
# Preflight failure modes (#6) and wipe-time target disclosure (#7)
# ---------------------------------------------------------------------------


def _engine_only(monkeypatch, database="faultmaven"):
    monkeypatch.setattr(
        "faultmaven.infrastructure.persistence.database.get_engine",
        lambda: _Ns(
            url=_Ns(database=database, host="pg"), dialect=_Ns(name="postgresql")
        ),
    )


async def test_an_unreachable_database_does_not_masquerade_as_a_refusal(
    monkeypatch, capsys
):
    """The preflight connects, so a down database or a bad password lands there.
    Letting it propagate exited 1 with a traceback — and 1 is the code documented
    as "refused, nothing written", which is a different statement. --verify must
    say INCONCLUSIVE(5) instead of implying a verdict.
    """
    _engine_only(monkeypatch)

    async def _boom():
        raise OSError("connection refused")

    monkeypatch.setattr(wd, "preflight_database_role", _boom)

    code = await wd.wipe_deployment(
        mode="verify", confirm_target=None, redis_all_keys=False
    )
    out = capsys.readouterr().out
    assert code == 5, "an uninspected database is inconclusive, not a refusal"
    assert "INCONCLUSIVE" in out
    assert "connection refused" in out


async def test_an_unreachable_database_refuses_a_wipe(monkeypatch, capsys):
    _engine_only(monkeypatch)

    async def _boom():
        raise OSError("connection refused")

    monkeypatch.setattr(wd, "preflight_database_role", _boom)

    code = await wd.wipe_deployment(
        mode="wipe", confirm_target="faultmaven", redis_all_keys=False
    )
    assert code == 1
    assert "Nothing was written" in capsys.readouterr().out


async def test_the_wipe_prints_the_targets_of_the_surfaces_it_deletes(
    monkeypatch, capsys
):
    """--confirm-target names the *database* — the one surface never touched.
    ChromaDB, object storage and Redis resolve from independent settings, and the
    documented invocation overrides only DATABASE_URL, so a scratch database that
    happens to be named `faultmaven` with ambient production S3/Redis would pass
    the guard. The targets therefore go on screen before the first delete.
    """
    _engine_only(monkeypatch)

    async def _surveys(settings, *, verify):
        return [
            wd.Surface(name="Vector store", target="external server https://chroma"),
            wd.Surface(name="Object storage", target="s3: bucket=PROD-EVIDENCE"),
            wd.Surface(name="Redis", target="real Redis: prod-redis:6379/0"),
        ]

    monkeypatch.setattr(wd, "_survey_all", _surveys)
    monkeypatch.setattr(wd, "wipe_vectors", lambda s: (0, None))
    monkeypatch.setattr(wd, "wipe_objects", lambda: _ready((0, None)))
    monkeypatch.setattr(wd, "wipe_redis", lambda s, **k: _ready((0, None)))

    code = await wd.run_wipe(
        _Settings(), confirm_target="faultmaven", redis_all_keys=False
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "bucket=PROD-EVIDENCE" in out, "the bucket about to be emptied must be shown"
    assert "prod-redis:6379/0" in out
    assert "https://chroma" in out
    assert out.index("bucket=PROD-EVIDENCE") < out.index(
        "Object storage: deleted"
    ), "targets must be printed BEFORE the deletions, not after"


# ---------------------------------------------------------------------------
# Printed guidance
# ---------------------------------------------------------------------------


def test_the_printed_next_steps_do_not_open_with_a_prerequisite():
    """_NEXT_STEPS is printed *after* the wipe. It used to start with "Scale the
    API down" — the step that must precede the wipe — so an operator following
    the on-screen list in order had already wiped with the API up."""
    first_step = wd._NEXT_STEPS.split("1.", 1)[1].splitlines()[0]
    assert "DROP DATABASE" in first_step
    assert "Scale the API down." not in wd._NEXT_STEPS.split("1.", 1)[1]


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------


def test_the_redis_target_line_omits_the_password():
    """This output lands in cutover notes and terminal scrollback, which the
    repo's pre-commit secret scanning does not reach."""

    class _Pool:
        connection_kwargs = {
            "host": "faultmaven-redis-master",
            "port": 6379,
            "db": 0,
            "password": "sup3r-s3cret",  # pragma: allowlist secret
        }

    class _Client:
        connection_pool = _Pool()

    target = wd._redis_target(_Client())
    assert target == "faultmaven-redis-master:6379/0"
    assert "s3cret" not in target
