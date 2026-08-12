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
    counts, unmatched = wd.classify_redis_keys(
        ["session:a", "session:b", "revoked:token:jti:x", "sso:state:s"]
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

    async def _redis(*, verify):
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


def test_a_missing_table_and_an_empty_seed_are_different_findings():
    """``_count_rows`` returns None for a table that does not exist and 0 for one
    that is present and empty. Those have different causes — schema older than
    this build vs a DELETE-based wipe — so one table must not produce both
    residue lines."""
    counts = {"roles": None, "permissions": 0}
    absent = [t for t, n in sorted(counts.items()) if n is None]
    missing = [t for t in sorted(wd.MUST_BE_SEEDED) if counts.get(t) == 0]
    assert absent == ["roles"]
    assert missing == ["permissions"]
    assert "roles" not in missing


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
