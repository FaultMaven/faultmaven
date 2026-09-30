"""003_admin_case_metadata

The cross-enterprise operator case list (ADR-012 D9): two ``SECURITY DEFINER``
functions that read every enterprise's cases and return **metadata only**.

Why a definer function
----------------------

Under ``TENANT_PROVIDER=multi`` the web process connects as a role that
row-level security scopes to one enterprise per transaction, so no ordinary
query can answer "every tenant's cases". The policies are ``ENABLE``d and never
``FORCE``d, and PostgreSQL exempts a table's owner from a non-forced policy. A
function created here is owned by the role that runs the migrations — the table
owner — and ``SECURITY DEFINER`` runs it with that role's rights, so it spans
every enterprise.

Two things bound it, and both are needed.

**The result type.** The page function returns system ids, timestamps,
integers, booleans, arrays of those, and three short strings. No column is
sourced from a title, a description, a message or a JSON blob. Of the strings,
``state`` is a closed vocabulary the database enforces (``cases_state_check``);
``source`` and ``closure_reason`` are closed vocabularies the application's
writers enforce (the ``Case`` model's ``Literal`` and its closure-reason
validator) — the columns themselves carry no CHECK, so for those two the bound
is the writer, not the type. Two derived fields an operator sees are not
columns — the investigation stage (four gate milestones in ``progress``) and the
investigation turn (the out-of-band entries of ``metadata.turn_history``) — so
the function returns their primitive inputs and the application applies the same
rules a loaded case applies. Neither rule is re-implemented here.

**An explicit grant.** A new function is executable by ``PUBLIC`` by default,
and every login role on the cluster holds ``CONNECT`` through ``PUBLIC`` — so
left at the default, any role able to connect could read every enterprise's case
metadata. Both functions therefore revoke ``PUBLIC`` and grant ``EXECUTE`` to
the runtime role, ``faultmaven_app``, when that role exists at migration time.
A deployment whose runtime role has a different name, or creates it after this
revision ran, must grant ``EXECUTE`` on both functions itself; until it does,
the list fails closed with a 503 that says so.

``search_path`` is pinned to ``pg_catalog, public, pg_temp``. Listing
``pg_temp`` LAST matters: left out, PostgreSQL searches the caller's temporary
schema FIRST for relations, so a caller's temporary ``cases`` would shadow the
real table inside a function running with the owner's rights.

``row_security`` is pinned ``off``. The functions span every enterprise only
while their owner is exempt from the policies; if that ever stops being true
(``FORCE ROW LEVEL SECURITY`` on ``cases`` or ``resource_shares``, or a
migrating role that does not own the tables), a query the policies would
filter RAISES under ``row_security = off`` instead of silently returning the
caller's enterprise — a partial list presented as the whole. The failure is an
error the application answers with a 503, never a short page.

The rejected alternatives: a ``BYPASSRLS`` engine in the web process (bounded
only by call-site discipline, and able to read every transcript), the offline
maintenance role in a request path, and looping a rebind over every enterprise
(a rebind is authorised by a break-glass grant, not ambient).

Two functions, one read
-----------------------

``admin_case_metadata_page(state, source, limit, offset)`` returns one page,
newest update first with ``case_id`` breaking ties (the order the single-tenant
list uses too, so a page boundary between two cases updated in one transaction
falls in the same place on both paths); ``admin_case_metadata_count(state,
source)`` returns the number of matches in every enterprise, which a page
cannot carry when it is empty. A ``NULL`` filter matches everything. ``limit``
and ``offset`` are ``bigint``: the API bounds neither to 32 bits. The page is
chosen on narrow columns first and only its rows are joined back for their
JSON, so the sort never carries a blob.

A malformed stored value never fails the page. Every cast from the JSON blobs
is guarded: a key that is MISSING reads as the model's default (a gate the
blob does not record is ``false``, as the ``Case`` model defaults it), while a
value of the WRONG TYPE — a gate that is not a boolean, a turn entry that is
not an object or has no integral ``turn_number`` — reads as ``NULL``, and the
application serves that case with the fields it cannot derive left null. That
is the one place the two read paths differ: the single-tenant list cannot load
such a case at all.

SQLite (standalone) gets nothing: it is single-tenant, has no row-level
security and no functions, and the operator list there reads cases directly.
``downgrade()`` drops both functions, and their grants with them.

Revision ID: baa28e79ebab
Revises: 65913afe773c
Create Date: 2026-09-30 09:00:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "baa28e79ebab"
down_revision: Union[str, Sequence[str], None] = "65913afe773c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PAGE_FUNCTION = "admin_case_metadata_page"
COUNT_FUNCTION = "admin_case_metadata_count"

#: Frozen snapshot of ``TurnOutcome.OUT_OF_BAND.value``. Migrations are history,
#: so they state the text they were written against rather than importing it;
#: ``tests/unit/infrastructure/persistence/test_admin_case_metadata_migration.py``
#: asserts the two agree, so the snapshot cannot drift silently.
OUT_OF_BAND_OUTCOME = "out_of_band"

#: The one filter both functions apply. ``k`` is ``cases``.
_MATCHES_FILTERS = (
    "(p_state IS NULL OR k.state = p_state) "
    "AND (p_source IS NULL OR k.source = p_source)"
)

#: The runtime role the deployment connects as (the RLS-subject role the
#: baseline's infra requirement names). Granted EXECUTE when it exists.
RUNTIME_ROLE = "faultmaven_app"

#: ``pg_temp`` LAST: when it is not listed, PostgreSQL searches it FIRST for
#: relations, and a caller's temporary table would shadow ``cases``.
SEARCH_PATH = "pg_catalog, public, pg_temp"

_DEFINER = f"""
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = {SEARCH_PATH}
SET row_security = off
"""


def _gate(parent: str, key: str) -> str:
    """One gate milestone as a boolean, or ``NULL`` when it is malformed.

    ``parent`` is a JSON path expression that must be an object; ``key`` is the
    gate. Missing key → ``false`` (the model's default); a JSON boolean → its
    value; anything else — including an explicit JSON ``null``, which the model
    would reject — → ``NULL``.
    """
    value = f"{parent} -> '{key}'"
    return (
        f"CASE WHEN jsonb_typeof({parent}) IS DISTINCT FROM 'object' THEN NULL "
        f"WHEN {value} IS NULL THEN false "
        f"WHEN jsonb_typeof({value}) = 'boolean' THEN ({parent} ->> '{key}')::boolean "
        "END"
    )


def _mitigation_gate(key: str) -> str:
    """A mitigation gate. A mitigation that was never recorded — no key, or a
    JSON ``null`` (``Optional[MitigationRecord]``) — has neither gate; one that
    is not an object is malformed."""
    mitigation = "c.progress -> 'mitigation'"
    return (
        "CASE WHEN jsonb_typeof(c.progress) IS DISTINCT FROM 'object' THEN NULL "
        f"WHEN {mitigation} IS NULL OR jsonb_typeof({mitigation}) = 'null' "
        "THEN false "
        f"ELSE {_gate(mitigation, key)} END"
    )


#: The turn history's state, from the loader's point of view: a missing or null
#: history is empty; anything but an array (or a metadata blob that is not an
#: object) is malformed.
_HISTORY = "c.metadata -> 'turn_history'"
_HISTORY_MALFORMED = (
    "jsonb_typeof(c.metadata) IS DISTINCT FROM 'object' "
    f"OR ({_HISTORY} IS NOT NULL AND jsonb_typeof({_HISTORY}) "
    "NOT IN ('array', 'null'))"
)
_HISTORY_EMPTY = f"{_HISTORY} IS NULL OR jsonb_typeof({_HISTORY}) = 'null'"

_CREATE_PAGE_FUNCTION = f"""
CREATE FUNCTION {PAGE_FUNCTION}(
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
{_DEFINER}
AS $$
    WITH page AS (
        -- The page on narrow columns: the sort carries no JSON, and only the
        -- page's rows are joined back below. case_id breaks updated_at ties.
        SELECT k.case_id, k.updated_at
          FROM cases AS k
         WHERE {_MATCHES_FILTERS}
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
        {_mitigation_gate("accepted")},
        {_mitigation_gate("verified")},
        {_gate("c.progress", "solution_accepted")},
        {_gate("c.progress", "solution_verified")},
        turns.numbers,
        turns.out_of_band,
        COALESCE(teams.ids, '{{}}'::text[])
      FROM page AS p
      JOIN cases AS c ON c.case_id = p.case_id
    -- Every turn_history entry's number and whether it was an aside, in
    -- stored order: the application repairs the sequence the way a case load
    -- does before counting, so it needs the order and the duplicates. NULL
    -- (both arrays) when the history or any entry in it is malformed.
    LEFT JOIN LATERAL (
        SELECT
            CASE
                WHEN {_HISTORY_MALFORMED} THEN NULL
                WHEN {_HISTORY_EMPTY} THEN '{{}}'::integer[]
                WHEN bool_and(e.ok) IS FALSE THEN NULL
                ELSE COALESCE(array_agg(e.number ORDER BY e.position),
                              '{{}}'::integer[])
            END AS numbers,
            CASE
                WHEN {_HISTORY_MALFORMED} THEN NULL
                WHEN {_HISTORY_EMPTY} THEN '{{}}'::boolean[]
                WHEN bool_and(e.ok) IS FALSE THEN NULL
                ELSE COALESCE(array_agg(e.aside ORDER BY e.position),
                              '{{}}'::boolean[])
            END AS out_of_band
          FROM (
            SELECT r.position,
                   r.raw IS NOT NULL AND r.raw = trunc(r.raw)
                       AND r.raw BETWEEN 0 AND 2147483647 AS ok,
                   CASE WHEN r.raw = trunc(r.raw)
                             AND r.raw BETWEEN 0 AND 2147483647
                        THEN r.raw::integer END AS number,
                   r.outcome = '{OUT_OF_BAND_OUTCOME}' AS aside
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
                                     AND jsonb_typeof({_HISTORY}) = 'array'
                                THEN {_HISTORY} END
                       ) WITH ORDINALITY AS t(entry, position)
              ) AS r
          ) AS e
    ) AS turns ON true
    -- The teams the case is shared to, within its own enterprise.
    LEFT JOIN LATERAL (
        -- Codepoint order ("C"), the order Python's sorted() gives the
        -- single-tenant path; the database's collation may sort otherwise.
        SELECT array_agg(s.scope_id::text ORDER BY s.scope_id COLLATE "C") AS ids
          FROM resource_shares AS s
         WHERE s.resource_type = 'case'
           AND s.resource_id = c.case_id
           AND s.scope_type = 'team'
           AND s.enterprise_id = c.enterprise_id
    ) AS teams ON true
     ORDER BY p.updated_at DESC, p.case_id
$$
"""

_CREATE_COUNT_FUNCTION = f"""
CREATE FUNCTION {COUNT_FUNCTION}(p_state text, p_source text)
RETURNS bigint
{_DEFINER}
AS $$
    SELECT count(*) FROM cases AS k WHERE {_MATCHES_FILTERS}
$$
"""

_COMMENT = (
    "Cross-enterprise operator case list (ADR-012 D9). Returns metadata only; "
    "executable by the runtime role, not PUBLIC."
)

#: Both functions by signature, as GRANT/REVOKE/COMMENT name them.
_SIGNATURES = (
    f"{PAGE_FUNCTION}(text, text, bigint, bigint)",
    f"{COUNT_FUNCTION}(text, text)",
)

_GRANTS = "\n        ".join(
    f"GRANT EXECUTE ON FUNCTION {signature} TO {RUNTIME_ROLE};"
    for signature in _SIGNATURES
)

#: Granted only if the role exists, so a database without it (a fresh test
#: cluster, a deployment that names its role differently) still migrates.
_GRANT_TO_RUNTIME_ROLE = f"""
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{RUNTIME_ROLE}') THEN
        {_GRANTS}
    END IF;
END
$$
"""


def upgrade() -> None:
    """Create both functions on PostgreSQL; SQLite has nothing to create."""
    if op.get_context().dialect.name != "postgresql":
        return
    op.execute(_CREATE_PAGE_FUNCTION)
    op.execute(_CREATE_COUNT_FUNCTION)
    for signature in _SIGNATURES:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"COMMENT ON FUNCTION {signature} IS '{_COMMENT}'")
    op.execute(_GRANT_TO_RUNTIME_ROLE)


def downgrade() -> None:
    """Drop both functions; their grants go with them."""
    if op.get_context().dialect.name != "postgresql":
        return
    for signature in _SIGNATURES:
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
