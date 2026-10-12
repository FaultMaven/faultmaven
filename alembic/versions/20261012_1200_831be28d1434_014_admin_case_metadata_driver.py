"""014_admin_case_metadata_driver

The cross-enterprise operator case list names each case's driver (#1926).

``admin_case_metadata_page`` — revision 003's operator read, as revision 005
left it — gains one result column, ``driver_id text``, right after ``user_id``:
``COALESCE(c.driver_id, c.user_id)::text``. That is the EFFECTIVE driver, the
account that holds the case's investigation writes (ADR-020 D1): a NULL
``cases.driver_id`` means the creator drives (revision 012), so it is the rule
``Case.effective_driver_id`` applies, and it is NULL only when neither account
exists any more. ADR-020 D5 has every case list show the driver; on this view
ADR-012 D9 bounds that to the id. Display names stay off it: D9 classes them
with titles, as text a user typed, and an operator maps an id to a name through
the ambient ``/admin/users`` list. The bound revision 003 states is unchanged —
system ids, closed-vocabulary strings, timestamps, integers, booleans and arrays
of those.

``CREATE OR REPLACE`` cannot change a function's result type, so on PostgreSQL
the upgrade issues five statements:

1. ``DROP FUNCTION`` — without ``IF EXISTS``: a database that lacks the
   function fails the revision rather than gaining one;
2. ``CREATE FUNCTION`` — not ``OR REPLACE`` — with revision 005's settings
   (``LANGUAGE sql``, ``STABLE``, ``SECURITY DEFINER``,
   ``search_path = pg_catalog, pg_temp``, ``row_security = off``) and 005's
   ``public.``-qualified body plus the one select-list line;
3. ``REVOKE ALL … FROM PUBLIC`` — a new function is executable by ``PUBLIC``;
4. ``COMMENT ON FUNCTION`` with revision 003's text — the drop takes the comment
   with it;
5. revision 005's grant of ``EXECUTE`` to ``faultmaven_app``, guarded on that
   role existing.

The drop also takes any grant made by hand: a deployment whose runtime role has
another name re-grants ``EXECUTE`` on the page function after this revision, as
after 003, and until it does the list fails closed with a 503. The new function
is owned by the migrating role — the table owner, as when 003 created it — so
it spans every enterprise exactly as before. ``admin_case_metadata_count`` is
untouched.

Both definitions are written out here, not imported — migrations are history,
and ``alembic/versions`` is not a package. ``_PREVIOUS_PAGE_RESULT`` and
``_PREVIOUS_PAGE_BODY`` are revision 005's text verbatim; ``_PAGE_RESULT`` and
``_PAGE_BODY`` differ from them by exactly one line each.
``tests/unit/infrastructure/persistence/test_admin_case_metadata_driver_migration.py``
asserts both, and that every relation in the body stays qualified.

The reader passes every column the function returns to
``CaseMetadata.from_stored``, which names each one, so this revision and that
code ship together: application code on either side of it refuses the other's
row shape (a 500 on the operator list) rather than reading a column it has not
classified. The CD pipeline runs the migration Job before the rolling update,
so the cloud operator list (``TENANT_PROVIDER=multi`` only) answers 500 from
the pods not yet replaced: a few minutes, bounded by the rollout. If the deploy
then fails, its rollback restores the old pods but not the old function, and
the operator list stays 500 until a deploy of this revision's code succeeds.

``downgrade()`` issues the same five statements with revision 005's result and
body. The column is derived, so stepping down loses nothing stored.

SQLite has no functions — the operator list there reads cases directly — so the
revision is a no-op there in both directions.

Revision ID: 831be28d1434
Revises: 9b3e5c71d2a4
Create Date: 2026-10-12 12:00:00
"""

from typing import Sequence, Union

from alembic import op

revision: str = "831be28d1434"  # pragma: allowlist secret
down_revision: Union[str, Sequence[str], None] = "9b3e5c71d2a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PAGE_FUNCTION = "admin_case_metadata_page"

#: The page function as GRANT, REVOKE, COMMENT and DROP name it.
_SIGNATURE = f"public.{PAGE_FUNCTION}(text, text, bigint, bigint)"

#: The same, with its argument names, as CREATE declares it.
_SIGNATURE_DECLARED = (
    f"public.{PAGE_FUNCTION}("
    "p_state text, p_source text, p_limit bigint, p_offset bigint)"
)

#: The runtime role revision 003 grants ``EXECUTE``.
RUNTIME_ROLE = "faultmaven_app"

#: Revision 005's path: no schema a caller can create in.
SEARCH_PATH = "pg_catalog, pg_temp"

#: Frozen snapshot of ``TurnOutcome.OUT_OF_BAND.value``, the literal both bodies
#: compare outcomes with; a unit test asserts the two agree.
OUT_OF_BAND_OUTCOME = "out_of_band"

#: Revision 003's comment, which the drop removes.
_COMMENT = (
    "Cross-enterprise operator case list (ADR-012 D9). Returns metadata only; "
    "executable by the runtime role, not PUBLIC."
)

# The page function's two definitions. Revision 005's, verbatim — its
# declared result and its body — restored on downgrade:

#: Revision 005's declared result: 003's columns.
_PREVIOUS_PAGE_RESULT = """TABLE (
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
)"""

_PREVIOUS_PAGE_BODY = """
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
"""

# And this revision's: the same, with ``driver_id`` after ``user_id`` in the
# result and its one select-list line after ``c.user_id::text``.

#: The declared result: system ids, closed-vocabulary strings, timestamps,
#: integers, booleans and arrays of those. ``driver_id`` is the effective
#: driver's id — never a name (ADR-012 D9).
_PAGE_RESULT = """TABLE (
    case_id text,
    enterprise_id text,
    organization_id text,
    user_id text,
    driver_id text,
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
)"""

_PAGE_BODY = """
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
        COALESCE(c.driver_id, c.user_id)::text,
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
"""


def _create(result: str, body: str) -> str:
    """The ``CREATE FUNCTION`` for one definition, with revision 005's settings."""
    return (
        f"CREATE FUNCTION {_SIGNATURE_DECLARED}\n"
        f"RETURNS {result}\n"
        "LANGUAGE sql\n"
        "STABLE\n"
        "SECURITY DEFINER\n"
        f"SET search_path = {SEARCH_PATH}\n"
        "SET row_security = off\n"
        f"AS $${body}$$"
    )


#: Revision 005's grant to the runtime role, for the page function: guarded on
#: the role existing, ``pg_catalog``-qualified because the migrating session's
#: path includes ``public``.
_GRANT_TO_RUNTIME_ROLE = (
    "DO $$\nBEGIN\n"
    f"    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = "
    f"'{RUNTIME_ROLE}') THEN\n"
    f"        GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO {RUNTIME_ROLE};\n"
    "    END IF;\nEND\n$$"
)


def _redefine(result: str, body: str) -> list[str]:
    """The five statements that replace the page function with ``result`` and
    ``body``, in order."""
    return [
        f"DROP FUNCTION {_SIGNATURE}",
        _create(result, body),
        f"REVOKE ALL ON FUNCTION {_SIGNATURE} FROM PUBLIC",
        f"COMMENT ON FUNCTION {_SIGNATURE} IS '{_COMMENT}'",
        _GRANT_TO_RUNTIME_ROLE,
    ]


def upgrade() -> None:
    """Add the effective driver to the page; SQLite has no function to change."""
    if op.get_context().dialect.name != "postgresql":
        return
    for statement in _redefine(_PAGE_RESULT, _PAGE_BODY):
        op.execute(statement)


def downgrade() -> None:
    """Restore revision 005's page function."""
    if op.get_context().dialect.name != "postgresql":
        return
    for statement in _redefine(_PREVIOUS_PAGE_RESULT, _PREVIOUS_PAGE_BODY):
        op.execute(statement)
