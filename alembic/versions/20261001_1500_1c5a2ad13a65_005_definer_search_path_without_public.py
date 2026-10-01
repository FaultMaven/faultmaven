"""005_definer_search_path_without_public

Every ``SECURITY DEFINER`` function the chain creates — the baseline's trigger
guards ``organization_members_last_admin_guard`` and
``team_members_same_enterprise_guard``, revision 003's operator reads
``admin_case_metadata_page`` and ``admin_case_metadata_count`` — is re-created
with the body it had, every relation in it schema-qualified, and two settings.
A definer function runs with its owner's rights, so these decide what its body
can be made to run and to read.

``search_path = pg_catalog, pg_temp`` — no schema a caller can create in.
PostgreSQL resolves a function or operator name by argument types before
search-path position: an exact match in ANY schema on the path wins over a
``pg_catalog`` candidate that needs an implicit cast, wherever ``pg_catalog``
sits. The bodies compare ``character varying`` columns with ``text``
(``k.state = p_state``, ``organization_id = v_org_id``) and with each other
(``team_id = NEW.team_id``), and ``pg_catalog`` has no ``=`` for either pair of
types. With ``public`` on the path, a role that may create in ``public`` could
define one there and have the body run it with the owner's rights. PostgreSQL
15 and later grant no one ``CREATE`` on ``public`` by default; 14 and earlier
grant it to every role. ``pg_temp`` is listed so that it is searched LAST for
relations — left out, it is searched FIRST, and a caller's temporary table
would stand in for one the body names. It is never searched for functions or
operators.

Relations schema-qualified (``public.cases``, ``public.resource_shares``,
``public.teams``, ``public.users``, ``public.organizations``,
``public.organization_members``) — with ``public`` off the path an unqualified
name would not resolve, or would resolve to a temporary table.

``row_security = off`` — unchanged from revisions 003 and 004. The bodies rely
on their owner being exempt from row-level security; if that exemption is ever
lost, a read the policies would filter raises instead of returning a filtered
set.

The bodies are frozen copies of 001's and 003's, written out here rather than
imported, because migrations are history. They differ from those only in the
``public.`` qualifiers and in line breaks;
``tests/unit/infrastructure/persistence/test_definer_search_path_migration.py``
asserts exactly that. ``CREATE OR REPLACE`` keeps each function's OID, so its
owner, its grants (003's ``EXECUTE`` for ``faultmaven_app`` and none for
``PUBLIC``), its comment and the triggers that call it carry over; the
signatures and result types are unchanged, which ``CREATE OR REPLACE`` requires.

``downgrade()`` restores the path revision 004 left on all four functions,
``pg_catalog, public, pg_temp``; ``row_security = off`` is already what 004
left. It keeps the qualified bodies. Under 004's path, which lists ``public``
before ``pg_temp``, ``public.cases`` and ``cases`` name the same table in every
session, so the functions behave exactly as 004's did; restoring the
unqualified text would change nothing but add a second copy of every body here.

SQLite has no definer functions (its triggers are plain DDL), so the revision is
a no-op there in both directions.

Revision ID: 1c5a2ad13a65
Revises: 14d4bfdd406e
Create Date: 2026-10-01 15:00:00
"""

from typing import Sequence, Union

from alembic import op

revision: str = "1c5a2ad13a65"
down_revision: Union[str, Sequence[str], None] = "14d4bfdd406e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: The path every definer function runs under. Asserted of every definer
#: function in the schema by ``tests/integration/test_rls_tenant_isolation.py``.
SEARCH_PATH = "pg_catalog, pg_temp"

_CREATE_LAST_ADMIN_GUARD = """
CREATE OR REPLACE FUNCTION public.organization_members_last_admin_guard()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET row_security = off
AS $$
DECLARE
    v_org_id      text;
    v_org_rows    integer;
    v_admin_count integer;
BEGIN
    -- An UPDATE that moved neither the role nor the organization cannot have
    -- cost anyone their admin. Checked before anything else so routine column
    -- writes never reach the serialisation point.
    IF TG_OP = 'UPDATE'
       AND NEW.role_id = OLD.role_id
       AND NEW.organization_id = OLD.organization_id THEN
        RETURN NULL;
    END IF;

    -- The row being demoted or removed was not an admin, so this event cannot
    -- have reduced the admin count. Nothing to check, and nothing to lock.
    IF OLD.role_id IS DISTINCT FROM '50551907-a02c-5bf7-9aa4-4a98f3c4eb64' THEN
        RETURN NULL;
    END IF;

    v_org_id := OLD.organization_id;

    -- Cascade from `users`: the account itself is being deleted and the
    -- membership is going with it.
    IF NOT EXISTS (SELECT 1 FROM public.users WHERE user_id = OLD.user_id) THEN
        RETURN NULL;
    END IF;

    -- Existence check AND serialisation point in one statement: a no-op
    -- self-update, so it cannot revert a concurrent write to that row the way
    -- a full-row write would, while still producing a real row version that
    -- makes a REPEATABLE READ transaction fail rather than count a stale
    -- roster.
    UPDATE public.organizations
       SET updated_at = updated_at
     WHERE organization_id = v_org_id;
    GET DIAGNOSTICS v_org_rows = ROW_COUNT;

    -- Cascade from `organizations`: the organization is being deleted.
    IF v_org_rows = 0 THEN
        RETURN NULL;
    END IF;

    -- Taken after the serialisation point, so it sees every guard evaluation
    -- for this organization that has already committed.
    SELECT count(*) INTO v_admin_count
      FROM public.organization_members
     WHERE organization_id = v_org_id
       AND role_id = '50551907-a02c-5bf7-9aa4-4a98f3c4eb64';

    IF v_admin_count = 0 THEN
        RAISE EXCEPTION
            'organization % would be left with no admin', v_org_id
            USING ERRCODE = '23514',
                  CONSTRAINT = 'organization_members_last_admin',
                  HINT = 'Grant another member the admin role first.';
    END IF;

    RETURN NULL;
END;
$$;
"""

_CREATE_TEAM_MEMBER_GUARD = """
CREATE OR REPLACE FUNCTION public.team_members_same_enterprise_guard()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET row_security = off
AS $$
DECLARE
    v_team_enterprise text;
    v_user_enterprise text;
BEGIN
    SELECT enterprise_id INTO v_team_enterprise
      FROM public.teams WHERE team_id = NEW.team_id;
    SELECT enterprise_id INTO v_user_enterprise
      FROM public.users WHERE user_id = NEW.user_id;

    -- A missing team or user is the foreign keys' business, not this guard's;
    -- refusing here would report the wrong constraint for a plain bad id.
    IF v_team_enterprise IS NULL OR v_user_enterprise IS NULL THEN
        RETURN NEW;
    END IF;

    IF v_team_enterprise IS DISTINCT FROM v_user_enterprise THEN
        RAISE EXCEPTION
            'team member % is not in the same enterprise as team %',
            NEW.user_id, NEW.team_id
            USING ERRCODE = '23514',
                  CONSTRAINT = 'team_members_same_enterprise',
                  HINT = 'A team may only hold accounts of its own enterprise.';
    END IF;

    RETURN NEW;
END;
$$;
"""

_CREATE_PAGE_FUNCTION = """
CREATE OR REPLACE FUNCTION public.admin_case_metadata_page(
    p_state text,
    p_source text,
    p_limit bigint,
    p_offset bigint
)
RETURNS TABLE (
    case_id text,
    enterprise_id text,
    organization_id text,
    user_id text,
    state text,
    source text,
    closure_reason text,
    created_at timestamptz,
    updated_at timestamptz,
    last_activity_at timestamptz,
    resolved_at timestamptz,
    closed_at timestamptz,
    current_turn integer,
    turns_without_progress integer,
    mitigation_accepted boolean,
    mitigation_verified boolean,
    solution_accepted boolean,
    solution_verified boolean,
    turn_numbers integer[],
    turn_is_out_of_band boolean[],
    shared_team_ids text[]
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET row_security = off
AS $$
    WITH page AS (
        -- The page on narrow columns: the sort carries no JSON, and only the
        -- page's rows are joined back below. case_id breaks updated_at ties.
        SELECT k.case_id, k.updated_at
          FROM public.cases AS k
         WHERE (p_state IS NULL OR k.state = p_state)
           AND (p_source IS NULL OR k.source = p_source)
         ORDER BY k.updated_at DESC, k.case_id
         LIMIT p_limit OFFSET p_offset
    )
    SELECT
        c.case_id::text,
        c.enterprise_id::text,
        c.organization_id::text,
        c.user_id::text,
        c.state::text,
        c.source::text,
        c.closure_reason::text,
        c.created_at,
        c.updated_at,
        c.last_activity_at,
        c.resolved_at,
        c.closed_at,
        c.current_turn,
        c.turns_without_progress,
        -- The four gate milestones: missing → false, malformed → NULL.
        CASE WHEN jsonb_typeof(c.progress) IS DISTINCT FROM 'object' THEN NULL
             WHEN c.progress -> 'mitigation' IS NULL
               OR jsonb_typeof(c.progress -> 'mitigation') = 'null' THEN false
             ELSE CASE WHEN jsonb_typeof(c.progress -> 'mitigation')
                            IS DISTINCT FROM 'object' THEN NULL
                       WHEN c.progress -> 'mitigation' -> 'accepted' IS NULL
                       THEN false
                       WHEN jsonb_typeof(c.progress -> 'mitigation' -> 'accepted')
                            = 'boolean'
                       THEN (c.progress -> 'mitigation' ->> 'accepted')::boolean
                  END
        END,
        CASE WHEN jsonb_typeof(c.progress) IS DISTINCT FROM 'object' THEN NULL
             WHEN c.progress -> 'mitigation' IS NULL
               OR jsonb_typeof(c.progress -> 'mitigation') = 'null' THEN false
             ELSE CASE WHEN jsonb_typeof(c.progress -> 'mitigation')
                            IS DISTINCT FROM 'object' THEN NULL
                       WHEN c.progress -> 'mitigation' -> 'verified' IS NULL
                       THEN false
                       WHEN jsonb_typeof(c.progress -> 'mitigation' -> 'verified')
                            = 'boolean'
                       THEN (c.progress -> 'mitigation' ->> 'verified')::boolean
                  END
        END,
        CASE WHEN jsonb_typeof(c.progress) IS DISTINCT FROM 'object' THEN NULL
             WHEN c.progress -> 'solution_accepted' IS NULL THEN false
             WHEN jsonb_typeof(c.progress -> 'solution_accepted') = 'boolean'
             THEN (c.progress ->> 'solution_accepted')::boolean
        END,
        CASE WHEN jsonb_typeof(c.progress) IS DISTINCT FROM 'object' THEN NULL
             WHEN c.progress -> 'solution_verified' IS NULL THEN false
             WHEN jsonb_typeof(c.progress -> 'solution_verified') = 'boolean'
             THEN (c.progress ->> 'solution_verified')::boolean
        END,
        turns.numbers,
        turns.out_of_band,
        COALESCE(teams.ids, '{}'::text[])
      FROM page AS p
      JOIN public.cases AS c ON c.case_id = p.case_id
    -- Every turn_history entry's number and whether it was an aside, in
    -- stored order: the application repairs the sequence the way a case load
    -- does before counting, so it needs the order and the duplicates. NULL
    -- (both arrays) when the history or any entry in it is malformed.
    LEFT JOIN LATERAL (
        SELECT
            CASE
                WHEN jsonb_typeof(c.metadata) IS DISTINCT FROM 'object'
                     OR (c.metadata -> 'turn_history' IS NOT NULL
                         AND jsonb_typeof(c.metadata -> 'turn_history')
                             NOT IN ('array', 'null'))
                THEN NULL
                WHEN c.metadata -> 'turn_history' IS NULL
                     OR jsonb_typeof(c.metadata -> 'turn_history') = 'null'
                THEN '{}'::integer[]
                WHEN bool_and(e.ok) IS FALSE THEN NULL
                ELSE COALESCE(array_agg(e.number ORDER BY e.position),
                              '{}'::integer[])
            END AS numbers,
            CASE
                WHEN jsonb_typeof(c.metadata) IS DISTINCT FROM 'object'
                     OR (c.metadata -> 'turn_history' IS NOT NULL
                         AND jsonb_typeof(c.metadata -> 'turn_history')
                             NOT IN ('array', 'null'))
                THEN NULL
                WHEN c.metadata -> 'turn_history' IS NULL
                     OR jsonb_typeof(c.metadata -> 'turn_history') = 'null'
                THEN '{}'::boolean[]
                WHEN bool_and(e.ok) IS FALSE THEN NULL
                ELSE COALESCE(array_agg(e.aside ORDER BY e.position),
                              '{}'::boolean[])
            END AS out_of_band
          FROM (
            SELECT r.position,
                   r.raw IS NOT NULL AND r.raw = trunc(r.raw)
                       AND r.raw BETWEEN 0 AND 2147483647 AS ok,
                   CASE WHEN r.raw = trunc(r.raw)
                             AND r.raw BETWEEN 0 AND 2147483647
                        THEN r.raw::integer END AS number,
                   r.outcome = 'out_of_band' AS aside
              FROM (
                -- A well-formed entry is an object with a numeric turn_number
                -- and a string outcome; the numeric cast is only reached
                -- behind that check.
                SELECT t.position,
                       CASE WHEN jsonb_typeof(t.entry) = 'object'
                                 AND jsonb_typeof(t.entry -> 'turn_number') = 'number'
                                 AND jsonb_typeof(t.entry -> 'outcome') = 'string'
                            THEN (t.entry ->> 'turn_number')::numeric END AS raw,
                       CASE WHEN jsonb_typeof(t.entry) = 'object'
                            THEN t.entry ->> 'outcome' END AS outcome
                  FROM jsonb_array_elements(
                           CASE WHEN jsonb_typeof(c.metadata) = 'object'
                                     AND jsonb_typeof(c.metadata -> 'turn_history')
                                         = 'array'
                                THEN c.metadata -> 'turn_history' END
                       ) WITH ORDINALITY AS t(entry, position)
              ) AS r
          ) AS e
    ) AS turns ON true
    -- The teams the case is shared to, within its own enterprise.
    LEFT JOIN LATERAL (
        -- Codepoint order ("C"), the order Python's sorted() gives the
        -- single-tenant path; the database's collation may sort otherwise.
        SELECT array_agg(s.scope_id::text ORDER BY s.scope_id COLLATE "C") AS ids
          FROM public.resource_shares AS s
         WHERE s.resource_type = 'case'
           AND s.resource_id = c.case_id
           AND s.scope_type = 'team'
           AND s.enterprise_id = c.enterprise_id
    ) AS teams ON true
     ORDER BY p.updated_at DESC, p.case_id
$$
"""

_CREATE_COUNT_FUNCTION = """
CREATE OR REPLACE FUNCTION public.admin_case_metadata_count(
    p_state text,
    p_source text
)
RETURNS bigint
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET row_security = off
AS $$
    SELECT count(*)
      FROM public.cases AS k
     WHERE (p_state IS NULL OR k.state = p_state)
       AND (p_source IS NULL OR k.source = p_source)
$$
"""

#: In the order they are re-created: each one whole, settings included.
_CREATE_FUNCTIONS = (
    _CREATE_LAST_ADMIN_GUARD,
    _CREATE_TEAM_MEMBER_GUARD,
    _CREATE_PAGE_FUNCTION,
    _CREATE_COUNT_FUNCTION,
)

#: Every function this revision re-creates, by signature.
_SIGNATURES = (
    "public.organization_members_last_admin_guard()",
    "public.team_members_same_enterprise_guard()",
    "public.admin_case_metadata_page(text, text, bigint, bigint)",
    "public.admin_case_metadata_count(text, text)",
)

#: The path revision 004 left on all four, restored on downgrade.
_PRE_005_SEARCH_PATH = "pg_catalog, public, pg_temp"


def upgrade() -> None:
    """Re-create all four definer functions; SQLite has none."""
    if op.get_context().dialect.name != "postgresql":
        return
    for ddl in _CREATE_FUNCTIONS:
        op.execute(ddl)


def downgrade() -> None:
    """Restore 004's path; ``row_security = off`` and the bodies stay."""
    if op.get_context().dialect.name != "postgresql":
        return
    for signature in _SIGNATURES:
        op.execute(
            f"ALTER FUNCTION {signature} SET search_path = {_PRE_005_SEARCH_PATH}"
        )
