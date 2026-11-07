"""025_admin_case_metadata_mitigation_disposed

The cross-enterprise operator case list derives the stage from the mitigation's
disposition too (#2100).

A mitigation now leaves the MITIGATION stage on its derived verdict or on a
disposition (``declared_stable``, the user's own account that it holds, or
``proceed_unmitigated``, the stall's choice to go on without one):
``progress.investigation_stage`` reads a fifth input. The operator list derives
the stage from the stored blob without loading the case, so
``admin_case_metadata_page`` — revision 014's — gains one result column,
``mitigation_disposed boolean``, right after ``mitigation_verified``:

- false when ``progress.mitigation`` or its ``disposition`` is absent or JSON
  null — every persisted record dumps ``"disposition": null``, so null is not
  "malformed" here, unlike the four gate columns;
- true when the disposition is the string ``declared_stable`` or
  ``proceed_unmitigated``;
- NULL for any other value or type, and when ``progress`` or the mitigation is
  not an object: unreadable, as a case load would refuse it, and
  ``CaseMetadata.from_stored`` serves the case without a stage.

The bound revision 003 states is unchanged: a boolean.

``CREATE OR REPLACE`` cannot change a function's result type, so on PostgreSQL
the upgrade issues revision 014's five statements:

1. ``DROP FUNCTION`` — without ``IF EXISTS``: a database that lacks the
   function fails the revision rather than gaining one;
2. ``CREATE FUNCTION`` — not ``OR REPLACE`` — with revision 005's settings
   (``LANGUAGE sql``, ``STABLE``, ``SECURITY DEFINER``,
   ``search_path = pg_catalog, pg_temp``, ``row_security = off``) and 014's
   ``public.``-qualified body plus the one select-list expression;
3. ``REVOKE ALL … FROM PUBLIC`` — a new function is executable by ``PUBLIC``;
4. ``COMMENT ON FUNCTION`` with revision 003's text — the drop takes the comment
   with it;
5. revision 005's grant of ``EXECUTE`` to ``faultmaven_app``, guarded on that
   role existing.

The drop also takes any grant made by hand: a deployment whose runtime role has
another name re-grants ``EXECUTE`` on the page function after this revision, as
after 003 and 014, and until it does the list fails closed with a 503.
``admin_case_metadata_count`` is untouched.

Both definitions are written out here, not imported — migrations are history,
and ``alembic/versions`` is not a package. ``_PREVIOUS_PAGE_RESULT`` and
``_PREVIOUS_PAGE_BODY`` are revision 014's text verbatim; ``_PAGE_RESULT`` and
``_PAGE_BODY`` differ from them by one result column and one select-list
expression.
``tests/unit/infrastructure/persistence/test_admin_case_metadata_disposed_migration.py``
asserts both, and that every relation in the body stays qualified.

The reader passes every column the function returns to
``CaseMetadata.from_stored``, which names each one, so this revision and that
code ship together, as 014 and its code did: the cloud operator list answers
500 from the pods not yet replaced during the rollout, and a failed deploy's
rollback leaves it 500 until a deploy of this revision's code succeeds.

``downgrade()`` issues the same five statements with revision 014's result and
body. The column is derived, so stepping down loses nothing stored.

SQLite has no functions — the operator list there reads cases directly — so the
revision is a no-op there in both directions.

Revision ID: 45970adab01d
Revises: d6cfb801d9f5
Create Date: 2026-11-07 12:00:00
"""

from typing import Sequence, Union

from alembic import op

revision: str = "45970adab01d"  # pragma: allowlist secret
down_revision: Union[str, Sequence[str], None] = (
    "d6cfb801d9f5"  # pragma: allowlist secret
)
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

#: Frozen snapshot of the two ``MitigationRecord.disposition`` values; a unit
#: test asserts they agree.
DISPOSITIONS = ("declared_stable", "proceed_unmitigated")

#: Revision 003's comment, which the drop removes.
_COMMENT = (
    "Cross-enterprise operator case list (ADR-012 D9). Returns metadata only; "
    "executable by the runtime role, not PUBLIC."
)

# The page function's two definitions. Revision 014's, verbatim — its
# declared result and its body — restored on downgrade:

#: Revision 014's declared result.
_PREVIOUS_PAGE_RESULT = """TABLE (
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

# And this revision's: the same, with ``mitigation_disposed`` after
# ``mitigation_verified`` in the result and its one select-list expression after
# the ``mitigation_verified`` one.

#: The declared result: system ids, closed-vocabulary strings, timestamps,
#: integers, booleans and arrays of those.
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
    mitigation_disposed boolean,
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
        -- The mitigation's disposition (#2100): absent or JSON null → false,
        -- declared_stable or proceed_unmitigated → true, anything else → NULL.
        CASE WHEN jsonb_typeof(c.progress) IS DISTINCT FROM 'object' THEN NULL
             WHEN c.progress -> 'mitigation' IS NULL
               OR jsonb_typeof(c.progress -> 'mitigation') = 'null' THEN false
             ELSE CASE WHEN jsonb_typeof(c.progress -> 'mitigation')
                            IS DISTINCT FROM 'object' THEN NULL
                       WHEN c.progress -> 'mitigation' -> 'disposition' IS NULL
                         OR jsonb_typeof(c.progress -> 'mitigation' -> 'disposition')
                            = 'null'
                       THEN false
                       WHEN jsonb_typeof(c.progress -> 'mitigation' -> 'disposition')
                            = 'string'
                        AND c.progress -> 'mitigation' ->> 'disposition'
                            IN ('declared_stable', 'proceed_unmitigated')
                       THEN true
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
    """Add the disposition to the page; SQLite has no function to change."""
    if op.get_context().dialect.name != "postgresql":
        return
    for statement in _redefine(_PAGE_RESULT, _PAGE_BODY):
        op.execute(statement)


def downgrade() -> None:
    """Restore revision 014's page function."""
    if op.get_context().dialect.name != "postgresql":
        return
    for statement in _redefine(_PREVIOUS_PAGE_RESULT, _PREVIOUS_PAGE_BODY):
        op.execute(statement)
