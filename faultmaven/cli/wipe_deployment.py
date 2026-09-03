"""Wipe a deployment to a clean slate, and prove the wipe landed (#819).

Why this exists
---------------
The Cloud cutover wipes the deployment's test data before the multi-tenant flip,
and there was no tooling for it. What existed covered a fraction of the surface
and lied about the rest: ``scripts/purge_case_data.py`` and
``scripts/wipe_case_data.py`` (deleted with this command) opened a hardcoded
``data/faultmaven.db``, so against the PostgreSQL deployment they printed a
success report for a stale local SQLite file that nothing reads.

The failure this command is built around is therefore **not** "the delete did not
run" — it is "the delete ran somewhere else, and the operator believed it".
So every surface is reported with its **resolved** target before anything is
touched, and the destructive mode refuses unless the operator names the target
back (``--confirm-target``).

What it does NOT do: the database
---------------------------------
The SQL wipe is ``DROP DATABASE`` + ``CREATE DATABASE`` + the migration Job, run
by the operator with the owner DSN. This command will not ``DELETE`` or
``TRUNCATE`` its way to a clean database, because that does not produce one:

* Migration 029 seeds ``roles`` / ``permissions`` / ``role_permissions`` with a
  bare ``op.bulk_insert``, and Alembic will not re-run it on an already-stamped
  database. Delete those rows and the SSO login path's membership write fails its
  ``role_id`` FK — **every SSO login fails closed**, with nothing to restore the
  seed but a hand-written INSERT.
* Migration 006 seeds the default ``enterprises`` row that the user repository
  still falls back to.
* ``operator_access_grants`` and ``operator_access_audit`` **reject DELETE and
  TRUNCATE by trigger** (migration 036), so a blanket truncate aborts part-way
  and leaves the wipe half-applied.

You cannot drop the database you are connected to in any case. This command
covers the surfaces a ``DROP DATABASE`` does *not* reach — the vector store,
object storage and Redis — and then **verifies** the whole slate, SQL included:
``--verify`` is the positive check that the drop-and-recreate plus migrations
actually happened, which a green pod does not demonstrate (both the admin
bootstrap and the KB bootstrap fail non-fatally, and ``/health`` is mostly
stubs).

Why it insists on an RLS-exempt database role
---------------------------------------------
Under ``TENANT_PROVIDER=multi`` the application role is scoped by row-level
security, so it sees only its own tenant's rows. An inventory taken through it
under-reports, and — the dangerous half — a ``--verify`` through it **passes on a
database that still holds another tenant's data**. A verification that can only
confirm what it is allowed to see is not a verification, so the preflight refuses
that role for every mode (:func:`assert_rls_exempt_db_role`, the same posture
decision ``fm-provision-sso-org`` uses). Pass the owner DSN explicitly.

Usage (``fm-wipe-deployment``, installed with the package)
---------------------------------------------------------
    fm-wipe-deployment                       # inventory: resolve + report, no writes
    fm-wipe-deployment --verify              # positive post-wipe verification
    fm-wipe-deployment --wipe --confirm-target faultmaven --yes

In a Kubernetes deployment, with the owner DSN (the pod's own DATABASE_URL is the
RLS-scoped application role by design)::

    kubectl exec -it deploy/faultmaven-api -- \\
      env DATABASE_URL='postgresql+asyncpg://faultmaven:...@host/faultmaven' \\
      fm-wipe-deployment

**Scale the API down first for a local vector store.** A ``PersistentClient``
directory that a running API holds open is the same hazard ``fm-reset-kb``
documents: the server can go on serving reads from deleted files or recreate a
partial tree under the one just removed. Cloud's external ChromaDB has no such
problem — but a wipe with the API up will simply be re-populated by live traffic.

``faultmaven_slack`` is never a target
--------------------------------------
The Slack workspace **installations** live in a separate database
(``slack_bots``, ``slack_installations``, ``slack_oauth_states``). Dropping it
uninstalls the bot from every workspace and forces a re-install in each one — it
is the integration's identity, not test data. This command refuses outright if
the resolved database is ``faultmaven_slack``, and refuses ``--wipe`` unless
``--confirm-target`` names the resolved database, so a copied-and-edited DSN
cannot point the sweep at it by accident.

Exit codes
----------
| 0 | inventory printed, wipe completed, or verification clean |
| 1 | refused: no ``--yes``, ``--confirm-target`` mismatch, RLS-scoped role, or a
     protected database — nothing written |
| 2 | argparse usage error (a bad flag), reserved by argparse — nothing written |
| 3 | the wipe ran but at least one surface failed; re-run and re-verify |
| 5 | ``--verify`` found residue, or a seed the migrations should have restored is
     missing, or a surface could not be inspected at all (INCONCLUSIVE — an
     unreachable database, a ChromaDB that fell back to a local tree, a real
     Redis substituted by the in-process stand-in). Inconclusive is a failure:
     silence from a surface nobody could read is not a clean slate |
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field

from sqlalchemy import text

#: argparse's ``description``. A literal, not derived from ``__doc__``: ``python
#: -OO`` strips docstrings, and that expression would raise before argparse ran.
_SUMMARY = (
    "Wipe a deployment's non-SQL surfaces to a clean slate and verify the whole "
    "slate, SQL included."
)

#: The one database that must never be wiped: it holds the Slack workspace
#: installations, so dropping it uninstalls the bot from every workspace.
PROTECTED_DATABASES = frozenset({"faultmaven_slack"})

# ---------------------------------------------------------------------------
# Table classification
#
# Every table in the ORM metadata is in exactly one bucket, and
# ``unclassified_tables`` is asserted empty by the test suite. A migration that
# adds a table therefore fails a test rather than quietly landing outside the
# verification — the failure mode being guarded is a wipe that reports a clean
# slate while a table nobody classified still holds the old deployment's rows.
# ---------------------------------------------------------------------------

#: Tenant/case/identity data. A clean slate means zero rows here.
MUST_BE_EMPTY = frozenset(
    {
        # Case domain
        "cases",
        "case_actions",
        "case_checkpoints",
        "case_entities",
        "case_messages",
        "case_tags",
        "causal_edges",
        "causal_node_evidence",
        "causal_nodes",
        "conversion_drafts",
        "conversion_jobs",
        "evidence",
        "evidence_need_fulfillment",
        "evidence_needs",
        "hypotheses",
        "hypothesis_evidence",
        "investigation_sessions",
        "knowledge_suggestions",
        "reports",
        "solutions",
        "uploaded_files",
        # Identity / tenancy. ``sso_org_mappings`` is deliberately untenanted
        # (the callback that reads it is unauthenticated, so no tenant is bound
        # yet, #869) — which is exactly why a tenant-scoped wipe would miss it
        # and the next SSO login would land in a stale tenant.
        "oauth_authorization_codes",
        "organization_members",
        "organizations",
        "resource_shares",
        "sso_org_mappings",
        "team_members",
        "teams",
        "user_audit_log",
        "users",
        # Operator governance. Append-only by trigger, so these survive only a
        # drop-and-recreate — their presence after a "wipe" proves DELETE was
        # used instead, and that the RBAC seed is probably gone with it.
        "operator_access_audit",
        "operator_access_grants",
        # Cloud dashboard-managed settings.
        "config_overrides",
    }
)

#: Rows the **migrations** create. Empty here is a failed or DELETE-based wipe,
#: not a clean one — and for the RBAC tables it means every SSO login is broken.
MUST_BE_SEEDED = frozenset(
    {
        "enterprises",  # migration 006's default enterprise row
        "permissions",  # migration 029
        "role_permissions",  # migration 029
        "roles",  # migration 029
    }
)

#: Reported, never asserted: the KB pack is 0 rows immediately after the
#: migrations and ~91 after seeding, and both are correct at their own point in
#: the sequence. Under multi it does not re-seed on startup — it comes from
#: ``python -m faultmaven.jobs.run kb_seed --cross-tenant-maintenance``.
INFORMATIONAL = frozenset({"knowledge_items"})

#: Redis key namespaces with a fixed spelling. The configurable ones (the
#: token-revocation prefix and every protection preset's ``rl``/``dedup`` pair)
#: are added by :func:`redis_prefixes` — the set must be COMPLETE, because
#: ``--verify`` treats any surviving key as residue: a namespace missing here is
#: one the scoped wipe leaves behind and the verification then refuses forever.
_FIXED_REDIS_PREFIXES = (
    "session:",  # RedisSessionStore
    "client_index:",  # RedisSessionStore's own per-client index
    "idempotency:",  # IdempotencyMiddleware
    "sso:state:",  # SSOEphemeralStore (login CSRF)
    "sso:login:",  # SSOEphemeralStore (completion code)
    "oauth:code:",  # OAuthCodeRepository
    "password_reset:",  # UserService reset tokens
    "case_seq:",  # CaseService per-user daily sequence
    "redaction:",  # pseudonym-key fingerprint agreement
)


def redis_prefixes(settings) -> tuple[str, ...]:
    """Every Redis namespace this deployment writes, including configurable ones.

    Two cannot be hardcoded without going stale against configuration:

    * the token-revocation prefix is ``settings.security.token_revocation_prefix``
      (the container passes it to the store), so a deployment that overrides it
      would otherwise keep every revocation watermark through a "successful" wipe;
    * the rate-limit and dedup namespaces come from the protection presets'
      ``redis_key_prefix``. **Every** preset's namespaces are included, not the
      one the current ``ENVIRONMENT`` selects — see below.
    """
    prefixes = list(_FIXED_REDIS_PREFIXES)

    revocation = getattr(
        getattr(settings, "security", None), "token_revocation_prefix", None
    )
    prefixes.append(revocation or "revoked:token:")

    # Deliberately NOT discriminated by ``ENVIRONMENT``, even though
    # ``setup_protection_middleware`` picks exactly one preset. The keys on the
    # server were written by whatever preset was live *when they were written*,
    # which need not be the one this process resolves:
    #
    #   * this command runs with the API scaled down, so it runs in a one-off pod
    #     or a shell — not the API pod whose env sets ENVIRONMENT. ENVIRONMENT
    #     defaults to ``development``, so the prod keys of the very deployment
    #     being wiped went unmatched (fm#1052, seen live on the #819 cutover);
    #   * an overlay roll or preset change re-points the prefix while the old
    #     keys are still there, TTL-bearing.
    #
    # Matching only the resolved preset made those keys "under no known
    # FaultMaven prefix", and since ``--verify`` judges residue against the same
    # set the wipe deletes, the two stayed consistent while both were wrong.
    # Enumerating both is safe: these are TTL-bearing rate-limit counters and
    # dedup markers, and the scoped wipe already deletes un-namespaced fixed
    # prefixes (``session:``, ``oauth:code:``, ...) that would collide first if a
    # Redis database were ever shared between deployments.
    from faultmaven.config.protection import ALL_REDIS_KEY_PREFIXES

    for key_prefix in ALL_REDIS_KEY_PREFIXES:
        prefixes.extend([f"{key_prefix}:rl", f"{key_prefix}:dedup"])

    # Deduplicate while keeping a stable order for the inventory breakdown.
    return tuple(dict.fromkeys(prefixes))


def unclassified_tables() -> frozenset[str]:
    """ORM tables in no bucket. Asserted empty by the suite; see the note above."""
    from faultmaven.infrastructure.persistence.models import Base

    classified = MUST_BE_EMPTY | MUST_BE_SEEDED | INFORMATIONAL
    return frozenset(Base.metadata.tables) - classified


@dataclass
class Surface:
    """One wipe surface: what it resolved to, what is in it, what is wrong.

    ``residue`` is the verification verdict — every entry is a reason the slate
    is not clean. ``unreachable`` is held separately because it is a *different*
    answer: "this surface could not be inspected" must never read as "this
    surface is clean", which is the whole failure mode of the scripts this
    command replaces.
    """

    name: str
    target: str
    detail: list[str] = field(default_factory=list)
    residue: list[str] = field(default_factory=list)
    unreachable: str | None = None

    def render(self) -> str:
        lines = [f"{self.name}", f"  target: {self.target}"]
        if self.unreachable:
            lines.append(f"  ⚠ NOT INSPECTED: {self.unreachable}")
        lines.extend(f"  {line}" for line in self.detail)
        lines.extend(f"  ✗ {line}" for line in self.residue)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

_WIPE_RATIONALE = (
    "A role that row-level security scopes sees only its own tenant's rows, so "
    "this command's inventory would under-report and its --verify would pass on "
    "a database that still holds another tenant's data. Nothing was written."
)
_WIPE_REMEDY = (
    "    kubectl exec -it deploy/faultmaven-api -- \\\n"
    "      env DATABASE_URL='postgresql+asyncpg://faultmaven:...@host/faultmaven' \\\n"
    "      fm-wipe-deployment"
)


async def preflight_database_role() -> str | None:
    """Refuse an RLS-scoped role for every mode. Returns the verified role name.

    ``None`` off PostgreSQL, where there is no RLS and nothing to verify.
    """
    from faultmaven.infrastructure.persistence.rls_role_guard import (
        assert_rls_exempt_db_role,
    )

    return await assert_rls_exempt_db_role(
        operation="Wiping or verifying a deployment",
        rationale=_WIPE_RATIONALE,
        remedy=_WIPE_REMEDY,
    )


def resolved_database_name(engine) -> str:
    """The database this process is actually connected to.

    The token ``--confirm-target`` must match, and the value the protected-database
    refusal is checked against. Read off the engine URL rather than the
    environment so it reflects the connection that will be used, not what a
    ``.env`` on disk says.
    """
    return engine.url.database or "(none)"


async def _count_rows(conn, table: str) -> int | None:
    """``COUNT(*)`` for one table, or ``None`` if it could not be counted.

    Each count runs inside its own SAVEPOINT. All 38 share one connection, and
    on PostgreSQL a single failed statement aborts the surrounding transaction —
    every later ``COUNT(*)`` would then raise ``InFailedSqlTransaction`` and this
    function would report the whole rest of the schema as uncountable. Worse than
    noise: tables that *do* hold rows would be classified as absent instead of as
    residue, so ``--verify`` would stop reporting the very thing it exists to
    find. SQLite tolerates the unscoped version, which is why only PostgreSQL
    showed it. The SAVEPOINT confines the rollback to the one failed count.
    """
    try:
        async with conn.begin_nested():
            return int((await conn.scalar(text(f"SELECT COUNT(*) FROM {table}"))) or 0)
    except Exception:
        return None


async def survey_database(*, verify: bool) -> Surface:
    """Count every classified table and, under ``verify``, judge the result."""
    from faultmaven.infrastructure.persistence.database import get_engine

    engine = get_engine()
    name = resolved_database_name(engine)
    surface = Surface(
        name="Database (PostgreSQL/SQLite)",
        target=f"{engine.dialect.name}: {engine.url.host or 'local'} / {name}",
    )

    # Table names come from the module-level frozensets, which the suite pins to
    # the ORM metadata — never from input — so interpolating them is safe.
    tables = sorted(MUST_BE_EMPTY | MUST_BE_SEEDED | INFORMATIONAL)
    try:
        async with engine.connect() as conn:
            revision = (
                await conn.scalar(text("SELECT version_num FROM alembic_version"))
                or "(unstamped)"
            )
            present = await _existing_tables(conn)
            counts = {
                table: (await _count_rows(conn, table) if table in present else None)
                for table in tables
            }
    except Exception as exc:
        # Reported like any other unreachable surface rather than raised. An
        # unhandled exception here would exit 1 — the code that means "refused,
        # nothing written" — so a database this command could not even read
        # would be indistinguishable from a clean refusal. --verify treats an
        # uninspected surface as INCONCLUSIVE (5), which is the honest answer.
        surface.unreachable = f"{type(exc).__name__}: {exc}"
        return surface

    surface.detail.append(f"alembic revision: {revision}")
    surface.detail.append(f"expected head: {_alembic_head() or '(not resolvable)'}")

    populated = {t: n for t, n in counts.items() if n and t in MUST_BE_EMPTY}
    absent = sorted(t for t in tables if t not in present)
    uncounted = sorted(t for t, n in counts.items() if n is None and t in present)
    # Present but empty. A table that does not exist at all is a different
    # finding (``absent``), so excluding it here keeps one table from producing
    # two residue lines that blame two different causes.
    missing = [t for t in sorted(MUST_BE_SEEDED) if counts.get(t) == 0]

    # "all empty" is a positive claim, so it may only be made about tables that
    # were actually read. Anything unread is named in BOTH modes — inventory is
    # the mode an operator uses to decide whether to wipe, and a silent "all
    # empty" there about tables nobody could count is the same false-clean this
    # command exists to prevent.
    if populated:
        detail = ", ".join(f"{t}={n}" for t, n in sorted(populated.items()))
    elif absent or uncounted:
        detail = f"no rows in the {len(present & set(tables))} table(s) read"
    else:
        detail = "all empty"
    surface.detail.append(f"data tables: {detail}")
    if absent:
        surface.detail.append(
            f"⚠ {len(absent)} table(s) DO NOT EXIST, so nothing is known about "
            f"them: {', '.join(absent)}"
        )
    if uncounted:
        surface.detail.append(
            f"⚠ {len(uncounted)} table(s) exist but COULD NOT BE COUNTED, so "
            f"nothing is known about them: {', '.join(uncounted)}"
        )
    surface.detail.append(
        "migration seeds: "
        + ", ".join(
            f"{t}={counts.get(t) if counts.get(t) is not None else 'unknown'}"
            for t in sorted(MUST_BE_SEEDED)
        )
    )
    for table in sorted(INFORMATIONAL):
        value = counts.get(table)
        surface.detail.append(
            f"{table}={value if value is not None else 'unknown'} "
            "(KB pack — 0 until kb_seed runs; not asserted)"
        )

    if not verify:
        return surface

    head = _alembic_head()
    if head and revision != head:
        surface.residue.append(
            f"alembic_version is {revision} but the migrations' head is {head} — "
            "the migration Job has not brought this database up to date"
        )
    for table, count in sorted(populated.items()):
        surface.residue.append(f"{table} still holds {count} row(s)")
    for table in missing:
        surface.residue.append(
            f"{table} is EMPTY but the migrations seed it — this is the signature "
            "of a DELETE/TRUNCATE wipe instead of drop-and-recreate. For the RBAC "
            "tables it means every SSO login will fail closed on the membership "
            "write's role_id FK"
        )
    for table in absent:
        surface.residue.append(
            f"{table} does not exist — the schema is older than this build, or "
            "the migration Job has not run"
        )
    for table in uncounted:
        surface.residue.append(
            f"{table} exists but could not be counted, so this verification "
            "cannot say whether it is empty"
        )
    return surface


async def _existing_tables(conn) -> set[str]:
    """The tables that actually exist, asked of the schema rather than inferred.

    Determining absence positively — instead of from a failed ``COUNT(*)`` —
    separates "the migration Job has not run" from "the table is there but the
    count failed" (a permissions problem, say). Those want different operator
    actions, and a bare ``except`` collapses them into one misleading message.
    """
    from sqlalchemy import inspect as sa_inspect

    def _names(sync_conn) -> set[str]:
        return set(sa_inspect(sync_conn).get_table_names())

    return await conn.run_sync(_names)


def _alembic_head() -> str | None:
    """The migrations' head revision, or ``None`` if the scripts are not present.

    Resolved rather than hardcoded: a pinned constant is one more thing that goes
    stale on the next migration and then reports a false mismatch. Degrades to
    ``None`` (comparison skipped, stamped revision still reported) where the
    ``alembic/`` tree is not on disk.
    """
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        from faultmaven.bootstrap.data_init import get_project_root

        ini = get_project_root() / "alembic.ini"
        if not ini.exists():
            return None
        config = Config(str(ini))
        config.set_main_option("script_location", str(ini.parent / "alembic"))
        heads = ScriptDirectory.from_config(config).get_heads()
        return heads[0] if len(heads) == 1 else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Vector store (ChromaDB)
# ---------------------------------------------------------------------------


def _is_server_backed(client) -> bool:
    """Whether this client talks to a ChromaDB *server* rather than a local tree.

    Asked of the client that was actually created, never of the configuration.
    ``chromadb.HttpClient`` raises at construction when the server is
    unreachable, so ``_create_chromadb_client`` catches that and — on standalone
    — falls back to a local ``PersistentClient``. A caller that inferred "this is
    the external server" from ``CHROMADB_URL`` being set would then mislabel a
    local tree as the shared server and sweep the wrong store.

    ``chroma_server_host`` is populated by ``HttpClient`` and left unset by
    ``PersistentClient`` (verified against the pinned chromadb). Unknown shapes
    answer ``False``, which is the safe direction: the caller then keeps BOTH
    local clients instead of collapsing to one.
    """
    try:
        return bool(getattr(client.get_settings(), "chroma_server_host", None))
    except Exception:
        return False


def _chroma_clients(settings) -> tuple[list, str]:
    """The ChromaDB clients to sweep, and a description of what they resolved to.

    Both clients are always created, and what they *are* decides the rest. When
    an external server is configured and reachable, the KB and evidence clients
    address the **same** server and are isolated only by collection name — the
    caller keys collections by name, so the duplicate view collapses on its own.
    When they are local trees they are genuinely separate directories with
    separate lifecycles, and both must be swept: a fallback that returned only
    the KB client would leave every ``case_*`` collection behind while reporting
    the vector store clean.
    """
    from faultmaven.container.providers.infrastructure import (
        create_evidence_chromadb_client,
        create_kb_chromadb_client,
    )

    clients = [
        c
        for c in (
            create_kb_chromadb_client(settings),
            create_evidence_chromadb_client(settings),
        )
        if c
    ]
    if not clients:
        return [], "(no client could be created)"

    if any(_is_server_backed(c) for c in clients):
        url = (getattr(settings.database, "chromadb_url", "") or "").strip()
        return clients, f"external server {url}"

    # Through the shared resolvers, like the bootstrap and fm-reset-kb: one
    # spelling of "where is the local store", and an ABSOLUTE one. A bare
    # getattr reported the raw relative string, so an operator comparing this
    # inventory against fm-reset-kb's resolved path was comparing two different
    # spellings of the same question (fm#936).
    from faultmaven.bootstrap.data_init import (
        UnusableDataDirError,
        resolve_evidence_chroma_dir,
        resolve_kb_chroma_dir,
    )

    def _dir(resolve) -> str:
        try:
            return str(resolve(settings))
        except UnusableDataDirError as exc:
            return f"(unusable: {exc})"

    kb_dir = _dir(resolve_kb_chroma_dir)
    ev_dir = _dir(resolve_evidence_chroma_dir)
    target = f"local PersistentClient: {kb_dir}, {ev_dir}"
    # Say so loudly: an external server was asked for and is not what we got, so
    # the store being swept is NOT the one the deployment reads from.
    from faultmaven.infrastructure.chroma_client import is_external_chroma_configured

    if is_external_chroma_configured(settings):
        url = (getattr(settings.database, "chromadb_url", "") or "").strip()
        target = (
            f"⚠ CHROMADB_URL={url} is configured but unreachable — fell back to "
            f"{target}"
        )
    return clients, target


def _fell_back_to_local(settings, clients: list) -> bool:
    """An external server was configured, but every client we got is local.

    The signature of ``_create_chromadb_client``'s standalone fallback. Under
    cloud that path raises instead, so this only ever fires on standalone/on-prem
    — where it still means the wipe would sweep a tree the deployment does not
    read from.
    """
    from faultmaven.infrastructure.chroma_client import is_external_chroma_configured

    return (
        bool(clients)
        and is_external_chroma_configured(settings)
        and not any(_is_server_backed(c) for c in clients)
    )


def _collection_names(client) -> list[str]:
    """Collection names, across the ChromaDB versions that differ on the type.

    ``list_collections`` returns ``Collection`` objects on 0.5.x and bare names
    on 0.6+. Both are handled so the sweep does not silently find nothing on a
    dependency bump.
    """
    return [c if isinstance(c, str) else c.name for c in client.list_collections()]


async def survey_vectors(settings, *, verify: bool) -> Surface:
    """Report every ChromaDB collection and its size."""
    surface = Surface(name="Vector store (ChromaDB)", target="(unresolved)")
    try:
        clients, target = _chroma_clients(settings)
    except Exception as exc:
        surface.unreachable = f"{type(exc).__name__}: {exc}"
        return surface

    surface.target = target
    if not clients:
        surface.unreachable = (
            "no ChromaDB client could be created (SKIP_SERVICE_CHECKS=true, or "
            "the server is unreachable) — vectors were NOT inspected"
        )
        return surface

    if _fell_back_to_local(settings, clients):
        # The deployment reads from a server this process could not reach, so
        # whatever the local trees contain says nothing about the store that
        # matters. Counting them and reporting "clean" is the false-clean bug.
        url = (getattr(settings.database, "chromadb_url", "") or "").strip()
        surface.unreachable = (
            f"CHROMADB_URL={url} is configured but unreachable, so a local "
            "PersistentClient was substituted. The server's collections were "
            "NOT inspected and would NOT be wiped — fix connectivity and re-run"
        )
        return surface

    sizes: dict[str, int] = {}
    for client in clients:
        try:
            for name in _collection_names(client):
                sizes[name] = client.get_collection(name).count()
        except Exception as exc:
            surface.unreachable = f"{type(exc).__name__}: {exc}"
            return surface

    if not sizes:
        surface.detail.append("no collections")
        return surface

    # A deployment carries one ``case_*`` collection per case, most of them
    # empty. Listing all of them buries the two lines that matter, so the
    # populated ones are named and the empties are counted.
    populated = {name: n for name, n in sizes.items() if n}
    empty = len(sizes) - len(populated)
    surface.detail.append(
        f"{len(sizes)} collection(s), {sum(sizes.values())} embedding(s)"
    )
    for name, count in sorted(populated.items(), key=lambda kv: -kv[1]):
        surface.detail.append(f"{name}: {count} embedding(s)")
    if empty:
        surface.detail.append(f"({empty} empty collection(s) not listed)")

    if verify and sizes:
        # An empty collection is still residue: it is a leftover ``case_*`` name
        # from a case that no longer exists, and a drop-and-recreate of the
        # database does not remove it.
        surface.residue.append(
            f"{len(sizes)} collection(s) holding {sum(sizes.values())} embedding(s) "
            "remain — searches can still return chunks whose SQL rows no longer exist"
        )
    return surface


def wipe_vectors(settings) -> tuple[int, str | None]:
    """Delete every ChromaDB collection. Returns (deleted, error)."""
    try:
        clients, _ = _chroma_clients(settings)
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}"
    if not clients:
        return 0, "no ChromaDB client could be created — nothing was wiped"

    if _fell_back_to_local(settings, clients):
        # Refuse rather than delete. Sweeping the local trees here would destroy
        # a store the deployment does not read from, report success, and leave
        # the configured server's collections fully intact.
        url = (getattr(settings.database, "chromadb_url", "") or "").strip()
        return 0, (
            f"CHROMADB_URL={url} is configured but unreachable, so a local "
            "PersistentClient was substituted. Refusing to wipe: that would "
            "delete a store the deployment does not read from while leaving the "
            "server untouched. Fix connectivity and re-run"
        )

    deleted = 0
    for client in clients:
        try:
            for name in _collection_names(client):
                client.delete_collection(name)
                deleted += 1
        except Exception as exc:
            return deleted, f"{type(exc).__name__}: {exc}"
    return deleted, None


# ---------------------------------------------------------------------------
# Object storage (filesystem / S3 / MinIO)
# ---------------------------------------------------------------------------


def _storage_target(backend, settings) -> str:
    """A resolved description of where evidence bytes actually live."""
    if backend.get_storage_type().value == "s3":
        endpoint = settings.evidence_storage.s3_endpoint_url or "AWS"
        return (
            f"s3: {endpoint} bucket={settings.evidence_storage.s3_bucket_name} "
            f"prefix={settings.evidence_storage.s3_key_prefix or '(none)'}"
        )
    return f"filesystem: {settings.evidence_storage.evidence_storage_root}"


async def survey_objects(settings, *, verify: bool) -> Surface:
    """Count stored evidence objects through the backend the app itself uses."""
    surface = Surface(name="Object storage (evidence)", target="(unresolved)")
    try:
        from faultmaven.infrastructure.storage.factory import get_storage_backend

        backend = get_storage_backend()
        surface.target = _storage_target(backend, settings)
        keys = await backend.list_keys()
    except Exception as exc:
        surface.unreachable = f"{type(exc).__name__}: {exc}"
        return surface

    objects, sidecars = split_sidecar_keys(keys)
    # Stored objects and their metadata sidecars are counted apart. The
    # filesystem backend writes a ``<key>.meta`` beside every file (and evidence
    # adds ``.meta.json``), and ``list_keys`` walks the tree, so a single count
    # roughly doubles the real figure — an operator sizing the wipe from it is
    # reading a number that does not correspond to anything.
    surface.detail.append(f"{len(objects)} object(s)")
    if sidecars:
        surface.detail.append(f"{len(sidecars)} metadata sidecar(s)")
    if verify and keys:
        sample = ", ".join(sorted(objects or sidecars)[:3])
        surface.residue.append(
            f"{len(objects)} object(s) and {len(sidecars)} sidecar(s) remain "
            f"(e.g. {sample})"
        )
    return surface


#: Suffixes the storage backends use for metadata written beside an object.
_SIDECAR_SUFFIXES = (".meta", ".meta.json")


def split_sidecar_keys(keys: list[str]) -> tuple[list[str], list[str]]:
    """Partition storage keys into real objects and metadata sidecars."""
    sidecars = [k for k in keys if k.endswith(_SIDECAR_SUFFIXES)]
    objects = [k for k in keys if not k.endswith(_SIDECAR_SUFFIXES)]
    return objects, sidecars


async def wipe_objects() -> tuple[int, str | None]:
    """Delete every stored object. Returns (deleted, error).

    Goes through :class:`IFileStorageBackend`, so filesystem, S3 and MinIO are
    all covered by one path and the keys deleted are exactly the keys the
    application can address. ``list_keys`` paginates and strips the backend
    prefix that ``delete_file`` re-applies, so the round-trip is exact.

    Sidecar keys are passed to ``delete_file`` too, deliberately: the filesystem
    backend removes a sidecar along with its base file, so the later sidecar key
    simply reports ``False`` — but on a store where one was orphaned, deleting it
    by key is how it gets cleaned up.

    **One key that cannot be deleted must not end the sweep.** The filesystem
    backend refuses a key whose resolved path leaves the storage root (#1235),
    and ``list_keys`` can hand us exactly such a key: ``rglob`` yields a file
    symlink inside the root and ``is_file()`` follows it, so a link pointing
    outside is listed and then refused (measured). Letting that raise out of the
    loop stopped the wipe at the offending key and left every object after it in
    place, with ``--verify`` then exiting 5 and no way to finish through the
    tool. Refusals are counted and named instead, the same skip-don't-abort
    shape the orphan sweep uses — a wipe is a bulk operation and one unreachable
    object is a residue to report, not a reason to abandon the rest.
    """
    deleted = 0
    refused: list[str] = []
    try:
        from faultmaven.infrastructure.storage.factory import get_storage_backend

        backend = get_storage_backend()
        for key in await backend.list_keys():
            try:
                if await backend.delete_file(key):
                    deleted += 1
            except Exception as exc:  # one bad key must not end the sweep
                refused.append(f"{key} ({type(exc).__name__}: {exc})")
        if refused:
            # Reported as an error so --verify's exit 5 still has a cause the
            # operator can act on, but AFTER every deletable object is gone.
            return deleted, (
                f"{len(refused)} object(s) could not be deleted and remain: "
                + "; ".join(refused[:5])
                + ("; ..." if len(refused) > 5 else "")
            )
        return deleted, None
    except Exception as exc:
        # The running count, not 0. A sweep that failed at object 900 of 1522 did
        # delete 899, and reporting "0" tells the operator the store is intact
        # when it is half gone.
        return deleted, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------


async def _scan_keys(client, pattern: str = "*") -> list[str]:
    """Every key matching ``pattern``, via SCAN rather than KEYS.

    KEYS blocks the server for the length of the keyspace; on the shared cloud
    Redis that is a latency spike for every other caller.
    """
    keys: list[str] = []
    cursor = 0
    while True:
        cursor, batch = await client.scan(cursor=cursor, match=pattern, count=500)
        keys.extend(k.decode() if isinstance(k, bytes) else k for k in batch)
        if cursor == 0:
            return keys


def classify_redis_keys(
    keys: list[str], prefixes: tuple[str, ...] = _FIXED_REDIS_PREFIXES
) -> tuple[dict[str, int], list[str]]:
    """Split keys into per-prefix counts and the ones matching no known prefix.

    The unmatched list is the point: a namespace this command has never heard of
    is *reported*, so an incomplete scoped wipe is visible to the operator
    instead of being mistaken for a clean sweep.
    """
    counts = {prefix: 0 for prefix in prefixes}
    unmatched = []
    for key in keys:
        for prefix in prefixes:
            if key.startswith(prefix):
                counts[prefix] += 1
                break
        else:
            unmatched.append(key)
    return counts, unmatched


def _real_redis_was_configured(settings) -> str | None:
    """The Redis this deployment *intends*, or ``None`` if it intends FakeRedis.

    The distinction the wipe turns on. ``get_async_redis_client`` returns the
    in-process FakeRedis in two very different situations, and only one of them
    means "there is nothing durable to wipe":

    * the ``redis`` package is not installed — a base/standalone install, where
      FakeRedis is the intended backend. Benign.
    * the package IS installed and a server is configured, but the connection
      failed, so ``fakeredis_or_fail`` substituted the stand-in (it only raises
      under cloud). **A wipe that trusted ``is_fakeredis`` here would report
      "nothing durable to wipe" while the real server kept every session and
      revocation watermark** — a false clean bill of health on the one surface
      whose staleness can shadow the newly provisioned tenant.

    Returns a printable ``host:port/db`` (or the masked URL form) when a real
    server is configured and reachable-in-principle, else ``None``.
    """
    from faultmaven.infrastructure.redis_client import REDIS_AVAILABLE

    if not REDIS_AVAILABLE:
        return None

    database = getattr(settings, "database", None)
    url = getattr(database, "redis_url", None)
    if url:
        # Never print the URL itself: it carries the password.
        from faultmaven.infrastructure.redis_client import RedisClientFactory

        return RedisClientFactory._mask_url(url)
    host = getattr(database, "redis_host", None)
    if host:
        port = getattr(database, "redis_port", 6379)
        db = getattr(database, "redis_db", 0)
        return f"{host}:{port}/{db}"
    return None


def _redis_target(client) -> str:
    """host:port/db for the operator to check — and nothing else.

    Built field by field rather than printing ``connection_kwargs``, which
    carries the password: this output goes into cutover notes and terminal
    scrollback, and the repo's pre-commit secret scanning does not reach there.
    """
    kwargs = getattr(getattr(client, "connection_pool", None), "connection_kwargs", {})
    host = kwargs.get("host", "(unknown)")
    port = kwargs.get("port", "(unknown)")
    db = kwargs.get("db", 0)
    return f"{host}:{port}/{db}"


async def survey_redis(settings, *, verify: bool) -> Surface:
    """Report the Redis keyspace, or say plainly that there is nothing durable."""
    surface = Surface(
        name="Redis (sessions, revocation, idempotency)", target="(unresolved)"
    )
    configured = _real_redis_was_configured(settings)
    try:
        from faultmaven.infrastructure.redis_client import (
            get_async_redis_client,
            is_fakeredis,
        )

        client = await get_async_redis_client()
    except Exception as exc:
        surface.unreachable = f"{type(exc).__name__}: {exc}"
        return surface

    if is_fakeredis(client):
        if configured:
            # The dangerous branch. A real server is configured but unreachable,
            # so the client factory silently handed back the in-process
            # stand-in. Reporting "nothing durable to wipe" here would be a
            # false clean: the configured server still holds every session and
            # revocation watermark, and nothing this command did touched it.
            surface.target = f"real Redis configured at {configured} — NOT REACHED"
            surface.unreachable = (
                f"a real Redis is configured ({configured}) but could not be "
                "reached, so the client factory substituted the in-process "
                "FakeRedis stand-in. Its keyspace was NOT inspected and would "
                "NOT be wiped. Fix connectivity (or REDIS_URL/REDIS_HOST) and "
                "re-run — do not read this as an empty Redis"
            )
            await _close_quietly(client)
            return surface

        # Benign: the redis package is not installed, so FakeRedis is the
        # intended backend. It lives inside one process, so this command's copy
        # is empty by construction and the API's copy is unreachable from here.
        surface.target = "in-process FakeRedis (standalone)"
        surface.detail.append(
            "nothing durable to wipe — the API's keyspace lives in the API "
            "process and is discarded when it restarts"
        )
        return surface

    prefixes = redis_prefixes(settings)
    try:
        surface.target = f"real Redis: {_redis_target(client)}"
        keys = await _scan_keys(client)
    except Exception as exc:
        surface.unreachable = f"{type(exc).__name__}: {exc}"
        return surface
    finally:
        await _close_quietly(client)

    counts, unmatched = classify_redis_keys(keys, prefixes)
    known = len(keys) - len(unmatched)
    surface.detail.append(f"{len(keys)} key(s) total")
    for prefix, count in counts.items():
        if count:
            surface.detail.append(f"{prefix}* : {count}")
    if unmatched:
        surface.detail.append(
            f"{len(unmatched)} key(s) under no known FaultMaven prefix "
            f"(e.g. {', '.join(sorted(unmatched)[:3])}) — the scoped wipe leaves "
            "these; --redis-all-keys removes them"
        )
    if verify and known:
        # Residue is judged against the SAME prefix set the wipe deletes, so the
        # two cannot disagree. Judging every key would make --verify unpassable
        # on a Redis shared with anything else — a permanent false failure — and
        # unmatched keys are already reported above for the operator to judge.
        surface.residue.append(
            f"{known} key(s) under FaultMaven prefixes remain — a stale session "
            "or revocation watermark can shadow the newly provisioned tenant"
        )
    return surface


async def _close_quietly(client) -> None:
    """Release the connection pool; a close failure is not actionable."""
    try:
        await client.aclose()
    except Exception:  # pragma: no cover
        pass


async def wipe_redis(settings, *, all_keys: bool) -> tuple[int, str | None]:
    """Delete FaultMaven's Redis keys. Returns (deleted, error).

    Scoped to :func:`redis_prefixes` by default, because this command cannot
    prove the logical database is FaultMaven's alone and a blind FLUSHDB would
    take another tenant of that database with it. ``all_keys=True`` is the
    operator's explicit statement that it is exclusive.
    """
    try:
        from faultmaven.infrastructure.redis_client import (
            get_async_redis_client,
            is_fakeredis,
        )

        client = await get_async_redis_client()
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}"

    if is_fakeredis(client):
        await _close_quietly(client)
        configured = _real_redis_was_configured(settings)
        if configured:
            # Same false-clean trap as in survey_redis: a configured server was
            # substituted by the in-process stand-in, so "deleted 0 keys" would
            # read as a clean sweep of a keyspace this process never touched.
            return 0, (
                f"a real Redis is configured ({configured}) but could not be "
                "reached; the in-process FakeRedis stand-in was substituted and "
                "NOTHING was wiped. Fix connectivity and re-run"
            )
        return 0, None  # Benign: FakeRedis is the intended backend.

    deleted = 0
    try:
        if all_keys:
            targets = await _scan_keys(client)
        else:
            targets = [
                key
                for prefix in redis_prefixes(settings)
                for key in await _scan_keys(client, f"{prefix}*")
            ]
        for batch_start in range(0, len(targets), 500):
            batch = targets[batch_start : batch_start + 500]
            if batch:
                deleted += int(await client.delete(*batch) or 0)
        return deleted, None
    except Exception as exc:
        # The running count, not 0: a sweep that failed at key 900 of 1522 did
        # delete 899, and telling the operator "0" invites a retry decision made
        # on a false premise.
        return deleted, f"{type(exc).__name__}: {exc}"
    finally:
        await _close_quietly(client)


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

_NEXT_STEPS = """\
Remaining steps, in this order (getting it wrong breaks SSO login, not the wipe).
The API should already be scaled down — it is a PREREQUISITE of the wipe you just
ran, not a next step; if it was up, re-run the wipe with it down.

  1. DROP DATABASE faultmaven; CREATE DATABASE faultmaven OWNER faultmaven;
     Never DELETE/TRUNCATE — migration 029's RBAC seed will not re-run, and the
     operator-access tables reject both by trigger.
     ⚠ Never faultmaven_slack: it holds the Slack workspace installations.
  2. Re-grant, BEFORE migrating, so ALTER DEFAULT PRIVILEGES covers the tables
     the migrations are about to create — DROP DATABASE destroyed the
     per-database grants even though the cluster roles survived:
       scripts/apps/provision-rls-app-role.sh
       scripts/apps/provision-maintenance-role.sh
  3. Run the migration Job (RUN_STARTUP_MIGRATIONS is false on k8s).
  4. Delete BOTH credentials.db and cases.db from the Slack agent PVC. cases.db
     is the thread->case map: it holds case ids from the database you just
     dropped, on a volume no surface below can see. The cleanup pod must run as
     the agent's own uid/gid, or the delete is refused.
  5. fm-wipe-deployment --verify   ← before provisioning anything
  6. Provision: fm-provision-sso-org -> SSO sign-in -> fm-promote-platform-admin
     -> fm-provision-service-account -> the kb_seed job.
  7. Scale the API back up; redeploy the Slack agent.

The full cutover runbook is docs/operations/cloud-mode-cutover.md in the infra
repo; the core-repo half is docs/operations/deployment-wipe.md."""


async def _survey_all(settings, *, verify: bool) -> list[Surface]:
    """Every surface, in a fixed order. One definition, so inventory, the
    pre-wipe target report and verification cannot drift into surveying
    different things."""
    return [
        await survey_database(verify=verify),
        await survey_vectors(settings, verify=verify),
        await survey_objects(settings, verify=verify),
        await survey_redis(settings, verify=verify),
    ]


async def run_inventory(settings) -> int:
    """Resolve and report every surface. Writes nothing."""
    surfaces = await _survey_all(settings, verify=False)
    print("=" * 78)
    print("Deployment inventory — resolved targets, nothing written")
    print("=" * 78)
    for surface in surfaces:
        print()
        print(surface.render())
    print()
    print(
        "Check every 'target:' line against the deployment you intend to wipe "
        "before running --wipe."
    )
    return 0


async def run_verify(settings) -> int:
    """Positively verify the clean slate. Exit 5 on any residue."""
    surfaces = await _survey_all(settings, verify=True)
    print("=" * 78)
    print("Deployment wipe verification")
    print("=" * 78)
    for surface in surfaces:
        print()
        print(surface.render())

    residue = [line for s in surfaces for line in s.residue]
    unreachable = [s for s in surfaces if s.unreachable]
    print()
    if residue:
        print(f"❌ NOT CLEAN — {len(residue)} finding(s) above.")
        return 5
    if unreachable:
        # Silence from a surface that was never inspected is the exact failure
        # this command exists to prevent, so it is not allowed to pass.
        print(
            f"❌ INCONCLUSIVE — {len(unreachable)} surface(s) could not be "
            "inspected, so this is not a verification. Resolve the access "
            "problem above and re-run."
        )
        return 5
    print("✅ Clean slate: data tables empty, migration seeds present, vector")
    print("   store empty, object storage empty, Redis clear.")
    return 0


async def run_wipe(settings, *, confirm_target: str, redis_all_keys: bool) -> int:
    """Wipe the non-SQL surfaces after the target check. Exit 3 on any failure."""
    from faultmaven.infrastructure.persistence.database import get_engine

    resolved = resolved_database_name(get_engine())
    if confirm_target != resolved:
        print(
            f"❌ Refusing: --confirm-target is '{confirm_target}' but this process "
            f"is connected to '{resolved}'.\n"
            "   Nothing was written. Run without --wipe to see every resolved "
            "target, then name the database back exactly."
        )
        return 1

    print("=" * 78)
    print(f"Wiping non-SQL surfaces for '{resolved}'")
    print("=" * 78)

    # Print what is about to be deleted, resolved, BEFORE deleting it.
    #
    # --confirm-target names the *database* — the one surface this command never
    # touches. ChromaDB, object storage and Redis resolve from independent
    # settings, and the documented invocation overrides only DATABASE_URL. So an
    # operator pointing at a scratch database that also happens to be called
    # `faultmaven`, while the ambient S3_BUCKET_NAME and REDIS_URL still resolve
    # to production, would pass the guard and have production evidence bytes
    # deleted. The confirm token cannot cover surfaces it does not name, so the
    # targets are put on screen where a wrong one is visible in the transcript.
    for surface in await _survey_all(settings, verify=False):
        print()
        print(surface.render())
    print()
    print("↑ These are the surfaces about to be wiped. The database is NOT one.")
    print()

    failures = []

    deleted, error = wipe_vectors(settings)
    print(f"Vector store: deleted {deleted} collection(s)")
    if error:
        print(f"  ✗ {error}")
        failures.append(f"vector store: {error}")

    deleted, error = await wipe_objects()
    print(f"Object storage: deleted {deleted} object(s)")
    if error:
        print(f"  ✗ {error}")
        failures.append(f"object storage: {error}")

    deleted, error = await wipe_redis(settings, all_keys=redis_all_keys)
    scope = "all keys" if redis_all_keys else "FaultMaven prefixes only"
    print(f"Redis: deleted {deleted} key(s) ({scope})")
    if error:
        print(f"  ✗ {error}")
        failures.append(f"redis: {error}")

    print()
    if failures:
        print(f"❌ {len(failures)} surface(s) failed; the wipe is incomplete.")
        print("   Fix the access problem and re-run, then --verify.")
        return 3

    print("✅ Non-SQL surfaces wiped. The database was NOT touched.")
    print()
    print(_NEXT_STEPS)
    return 0


async def wipe_deployment(
    *,
    mode: str,
    confirm_target: str | None,
    redis_all_keys: bool,
) -> int:
    """Preflight, then dispatch to the requested mode."""
    from faultmaven.config.deployment_coherence import DeploymentCoherenceError
    from faultmaven.config.settings import get_settings
    from faultmaven.infrastructure.persistence.database import get_engine

    settings = get_settings()

    # A malformed DSN raises here, before any surface has been touched.
    try:
        resolved = resolved_database_name(get_engine())
    except Exception as exc:
        print(
            f"❌ Refusing: the database URL could not be resolved "
            f"({type(exc).__name__}: {exc}). Nothing was written."
        )
        return 1

    if resolved in PROTECTED_DATABASES:
        print(
            f"❌ Refusing: '{resolved}' is a protected database. It holds the "
            "Slack workspace installations (slack_bots, slack_installations, "
            "slack_oauth_states); wiping it uninstalls the bot from every "
            "workspace and forces a re-install in each one.\n"
            "   Point DATABASE_URL at the 'faultmaven' database instead. "
            "Nothing was written."
        )
        return 1

    try:
        role = await preflight_database_role()
    except DeploymentCoherenceError as exc:
        # A posture refusal: the role is real but wrong. Exit 1 — refused,
        # nothing written — is exactly right.
        print(f"❌ {exc}")
        return 1
    except Exception as exc:
        # NOT a refusal: the preflight connects, so an unreachable database or a
        # bad password lands here. Letting it propagate exited 1 with a
        # traceback, and 1 is the code this command documents as "refused,
        # nothing written" — indistinguishable from a clean refusal. A wipe
        # cannot proceed either way, but --verify must say INCONCLUSIVE rather
        # than imply a verdict, so the modes diverge.
        print(f"❌ The database could not be reached: {type(exc).__name__}: {exc}")
        if mode == "wipe":
            print("   Refusing to wipe. Nothing was written.")
            return 1
        if mode == "verify":
            print(
                "   ❌ INCONCLUSIVE — the database was not inspected, so this is "
                "not a verification. Fix connectivity and re-run."
            )
            return 5
        print("   Continuing: the remaining surfaces are reported below.\n")
        return await run_inventory(settings)
    if role:
        print(f"Database role: {role} (RLS-exempt — full visibility)\n")

    if mode == "verify":
        return await run_verify(settings)
    if mode == "wipe":
        assert confirm_target is not None  # argparse guarantees it
        return await run_wipe(
            settings,
            confirm_target=confirm_target,
            redis_all_keys=redis_all_keys,
        )
    return await run_inventory(settings)


def main() -> None:
    """Console entrypoint (``fm-wipe-deployment``)."""
    parser = argparse.ArgumentParser(description=_SUMMARY)
    parser.add_argument(
        "--wipe",
        action="store_true",
        help="Delete the non-SQL surfaces (vectors, object storage, Redis). "
        "Requires --yes and --confirm-target.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Positively verify the clean slate; exits 5 on residue.",
    )
    parser.add_argument(
        "--confirm-target",
        metavar="DATABASE",
        help="The database name this process must be connected to, named back "
        "exactly. Required with --wipe.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm the wipe (required with --wipe).",
    )
    parser.add_argument(
        "--redis-all-keys",
        action="store_true",
        help="Delete every key in the logical Redis database, not just the known "
        "FaultMaven prefixes. Only if that database is FaultMaven's alone.",
    )
    args = parser.parse_args()

    if args.wipe and args.verify:
        parser.error("--wipe and --verify are separate steps; run them in order")
    if args.wipe and not args.yes:
        print("Refusing to wipe without --yes (run with no flags to inventory first).")
        sys.exit(1)
    if args.wipe and not args.confirm_target:
        print(
            "Refusing to wipe without --confirm-target: name the database this "
            "process is connected to, exactly. Run with no flags to see it."
        )
        sys.exit(1)

    mode = "wipe" if args.wipe else "verify" if args.verify else "inventory"

    # sys.exit inside main() keeps the CLI modules uniform: every one of them
    # exits from main() rather than returning a code for a caller to forward.
    sys.exit(
        asyncio.run(
            wipe_deployment(
                mode=mode,
                confirm_target=args.confirm_target,
                redis_all_keys=args.redis_all_keys,
            )
        )
    )


if __name__ == "__main__":
    main()
